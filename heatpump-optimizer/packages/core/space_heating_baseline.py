"""Historical room-heating duty estimation for Phase C."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.heating_evidence import classify_space_heating
from packages.core.models import DeviceStatusRecord
from packages.core.panasonic_control_state import classify_panasonic_operation_mode

_LOOKBACK_DAYS = 14
_ROW_LIMIT = 10_000
_MAX_INTERVAL = dt.timedelta(minutes=15)
_HOUR = dt.timedelta(hours=1)


@dataclass(frozen=True)
class BaselineDutyPoint:
    timestamp: dt.datetime
    expected_fraction: float
    floor_fraction: float
    source: str
    floor_source: str
    bucket: str | None
    sample_count: int


@dataclass(frozen=True)
class BaselineDutyProfile:
    points: tuple[BaselineDutyPoint, ...]
    history_rows: int
    accepted_hours: int
    truncated: bool


@dataclass(frozen=True)
class _HourlyDuty:
    timestamp: dt.datetime
    outdoor_temp: float
    fraction: float


async def load_recent_heating_evidence(
    session: AsyncSession, device_id: str, now: dt.datetime
) -> tuple[list[Any], bool]:
    """Load a bounded, chronological device-specific evidence slice."""

    start = now - dt.timedelta(days=_LOOKBACK_DAYS)
    columns = (
        DeviceStatusRecord.device_id,
        DeviceStatusRecord.ts,
        DeviceStatusRecord.outdoor_temp,
        DeviceStatusRecord.heat_pump_outdoor_temp,
        DeviceStatusRecord.space_heating_active,
        DeviceStatusRecord.space_heating_evidence,
        DeviceStatusRecord.operation_status,
        DeviceStatusRecord.mode,
        DeviceStatusRecord.zone1_operation_status,
        DeviceStatusRecord.holiday_mode,
        DeviceStatusRecord.direction,
        DeviceStatusRecord.pump_duty,
        DeviceStatusRecord.device_action,
        DeviceStatusRecord.defrost_active,
    )
    result = await session.execute(
        select(*columns)
        .where(
            DeviceStatusRecord.device_id == device_id,
            DeviceStatusRecord.ts >= start,
            DeviceStatusRecord.ts <= now,
        )
        .order_by(desc(DeviceStatusRecord.ts))
        .limit(_ROW_LIMIT)
    )
    rows = list(result.all())
    return list(reversed(rows)), len(rows) == _ROW_LIMIT


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(name, default)
    return getattr(row, name, default)


def _utc(timestamp: dt.datetime) -> dt.datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone.utc)


def _hour_start(timestamp: dt.datetime) -> dt.datetime:
    return _utc(timestamp).replace(minute=0, second=0, microsecond=0)


def _state_for_hour(gate_states: Any, timestamp: dt.datetime, index: int) -> str | None:
    if gate_states is None:
        return None
    if isinstance(gate_states, Mapping):
        value = gate_states.get(timestamp, gate_states.get(_hour_start(timestamp)))
    elif isinstance(gate_states, Sequence) and not isinstance(gate_states, str):
        value = gate_states[index] if index < len(gate_states) else None
    else:
        value = gate_states
    return getattr(value, "value", value) if value is not None else None


def _forecast_timestamp_and_temperature(hour: Any) -> tuple[dt.datetime, float | None]:
    if isinstance(hour, dt.datetime):
        return _hour_start(hour), None
    timestamp = _value(hour, "timestamp", _value(hour, "ts"))
    temperature = _value(hour, "outdoor_temp", _value(hour, "temperature"))
    return _hour_start(timestamp), float(temperature) if temperature is not None else None


def _heating_capable(row: Any) -> bool:
    return (
        _value(row, "operation_status") == 1
        and classify_panasonic_operation_mode(_value(row, "mode")) == "heating"
        and _value(row, "zone1_operation_status") != 0
        and _value(row, "holiday_mode") != 1
    )


def _evidence_state(row: Any) -> str:
    evidence = classify_space_heating(
        operation_status=_value(row, "operation_status"),
        mode=_value(row, "mode"),
        direction=_value(row, "direction"),
        pump_duty=_value(row, "pump_duty"),
        device_action=_value(row, "device_action"),
        defrost_active=_value(row, "defrost_active"),
        zone1_operation_status=_value(row, "zone1_operation_status"),
    )
    if evidence.active:
        return "active"
    if evidence.code in {"domestic_hot_water", "cooling", "defrost", "idle", "device_off"}:
        return "inactive"
    return "unknown"


def _hourly_duties(rows: Iterable[Any]) -> list[_HourlyDuty]:
    ordered = sorted(rows, key=lambda row: _utc(_value(row, "ts")))
    coverage: dict[dt.datetime, list[float]] = {}
    temperatures: dict[dt.datetime, list[float]] = {}
    for current, following in zip(ordered, ordered[1:]):
        start = _utc(_value(current, "ts"))
        end = _utc(_value(following, "ts"))
        if end <= start or end - start > _MAX_INTERVAL or not _heating_capable(current):
            continue
        temperature = _value(current, "outdoor_temp", _value(current, "heat_pump_outdoor_temp"))
        if temperature is None:
            continue
        state = _evidence_state(current)
        cursor = start
        while cursor < end:
            boundary = min(_hour_start(cursor) + _HOUR, end)
            seconds = (boundary - cursor).total_seconds()
            hour = _hour_start(cursor)
            values = coverage.setdefault(hour, [0.0, 0.0])
            if state != "unknown":
                values[0] += seconds
                if state == "active":
                    values[1] += seconds
            temperatures.setdefault(hour, []).append(float(temperature))
            cursor = boundary
    result: list[_HourlyDuty] = []
    for hour, (known, active) in coverage.items():
        if known < 0.8 * _HOUR.total_seconds() or hour not in temperatures:
            continue
        result.append(
            _HourlyDuty(hour, sum(temperatures[hour]) / len(temperatures[hour]), active / known)
        )
    return sorted(result, key=lambda item: item.timestamp)


def _temperature_band(temperature: float) -> int:
    return math.floor(temperature / 2.0) * 2


def _local_band(timestamp: dt.datetime, tz: ZoneInfo) -> int:
    return timestamp.astimezone(tz).hour // 3


def _qualifies(values: list[_HourlyDuty], minimum: int, active_minimum: int) -> bool:
    return (
        len(values) >= minimum
        and len({value.timestamp.date() for value in values}) >= 3
        and sum(value.fraction > 0 for value in values) >= active_minimum
    )


def _p10(values: list[_HourlyDuty]) -> float:
    duties = sorted(value.fraction for value in values)
    return duties[max(0, math.ceil(0.1 * len(duties)) - 1)]


def build_baseline_duty_profile(
    rows: Iterable[Any],
    forecast_hours: Iterable[Any],
    gate_states: Any,
    heat_off_threshold: float,
    tz: str | ZoneInfo,
    zone_available: bool,
    default_fraction: float,
    *,
    truncated: bool = False,
) -> BaselineDutyProfile:
    """Build immutable expected and conservative floor duty fractions."""

    source_rows = list(rows)
    timezone = ZoneInfo(tz) if isinstance(tz, str) else tz
    duties = _hourly_duties(source_rows)
    points: list[BaselineDutyPoint] = []
    for index, forecast_hour in enumerate(forecast_hours):
        timestamp, temperature = _forecast_timestamp_and_temperature(forecast_hour)
        gate = _state_for_hour(gate_states, timestamp, index)
        if not zone_available or temperature is None or str(gate).upper() in {"BLOCKED", "UNKNOWN"}:
            points.append(BaselineDutyPoint(timestamp, 0.0, 0.0, "none", "none", None, 0))
            continue
        matching_band = [
            duty
            for duty in duties
            if _temperature_band(duty.outdoor_temp) == _temperature_band(temperature)
        ]
        matching_time = [
            duty
            for duty in matching_band
            if _local_band(duty.timestamp, timezone) == _local_band(timestamp, timezone)
        ]
        below_cutoff = [duty for duty in duties if duty.outdoor_temp <= heat_off_threshold]
        if temperature > heat_off_threshold:
            if not _qualifies(matching_time, 12, 1):
                points.append(BaselineDutyPoint(timestamp, 0.0, 0.0, "none", "none", None, 0))
                continue
            expected_bucket = matching_time
            bucket = "band_3h"
        elif _qualifies(matching_time, 6, 1):
            expected_bucket = matching_time
            bucket = "band_3h"
        elif _qualifies(matching_band, 12, 2):
            expected_bucket = matching_band
            bucket = "band_all_hours"
        elif _qualifies(below_cutoff, 24, 2):
            expected_bucket = below_cutoff
            bucket = "below_cutoff"
        else:
            expected = min(1.0, max(0.0, float(default_fraction)))
            points.append(BaselineDutyPoint(timestamp, expected, 0.0, "default", "none", None, 0))
            continue

        floor_candidates = (
            (matching_time,)
            if temperature > heat_off_threshold
            else (matching_time, matching_band, below_cutoff)
        )
        floor_bucket = next(
            (candidate for candidate in floor_candidates if len(candidate) >= 20),
            [],
        )
        expected = min(
            1.0, max(0.0, sum(value.fraction for value in expected_bucket) / len(expected_bucket))
        )
        floor = min(1.0, max(0.0, _p10(floor_bucket))) if floor_bucket else 0.0
        points.append(
            BaselineDutyPoint(
                timestamp,
                expected,
                floor,
                "history",
                "history_p10" if floor_bucket else "none",
                bucket,
                len(expected_bucket),
            )
        )
    return BaselineDutyProfile(tuple(points), len(source_rows), len(duties), truncated)

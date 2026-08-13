"""Trusted indoor-temperature input shared by control and presentation code."""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.models import IndoorTempReading, SettingRecord

CONTROL_READING_MAX_AGE = dt.timedelta(minutes=15)
MIN_PLAUSIBLE_INDOOR_C = 5.0
MAX_PLAUSIBLE_INDOOR_C = 35.0
OUTLIER_DISTANCE_C = 3.0


@dataclass(frozen=True)
class ControlTemperature:
    """A robust indoor temperature and the evidence behind it."""

    value: float | None
    confidence: str
    sensor_count: int
    sample_count: int
    latest_reading: dt.datetime | None
    reason: str | None = None
    reference_sensor_id: str | None = None
    reference_sensor_label: str | None = None
    reference_room: str | None = None
    spread_c: float | None = None
    sensors: tuple["SensorTemperature", ...] = ()

    @property
    def is_usable(self) -> bool:
        return self.value is not None and self.confidence in {"medium", "high"}


@dataclass(frozen=True)
class SensorTemperature:
    """One current, plausible room sensor used to explain control input."""

    device_id: str
    device_label: str | None
    room: str | None
    temperature: float
    timestamp: dt.datetime


async def get_control_temperature(
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta = CONTROL_READING_MAX_AGE,
    session: AsyncSession | None = None,
) -> ControlTemperature:
    """Return a fresh, selected-sensor, outlier-resistant control value.

    We use one latest *fresh* sample per selected device, then a median.  This
    prevents a chatty sensor, a stale SmartThings cache entry, or one unusual
    room from silently determining a whole-house heating decision.
    """

    from packages.poller.smartthings import get_selected_device_ids

    now = now or dt.datetime.now(dt.timezone.utc)
    selected = await get_selected_device_ids()
    cutoff = now - max_age
    stmt = (
        select(IndoorTempReading)
        .where(
            IndoorTempReading.timestamp >= cutoff,
            IndoorTempReading.is_stale.is_(False),
        )
        .order_by(IndoorTempReading.timestamp.desc(), IndoorTempReading.id.desc())
    )
    if selected:
        stmt = stmt.where(IndoorTempReading.device_id.in_(selected))

    async def load_rows_and_reference(active_session: AsyncSession) -> tuple[list, str]:
        rows = (await active_session.execute(stmt)).scalars().all()
        reference_result = await active_session.execute(
            select(SettingRecord.value).where(SettingRecord.key == "comfort_reference_sensor_id")
        )
        reference_sensor_id = reference_result.scalar_one_or_none() or ""
        return rows, str(reference_sensor_id).strip()

    if session is None:
        async with get_session() as owned_session:
            rows, reference_sensor_id = await load_rows_and_reference(owned_session)
    else:
        rows, reference_sensor_id = await load_rows_and_reference(session)

    latest_by_device: dict[str, IndoorTempReading] = {}
    for row in rows:
        latest_by_device.setdefault(row.device_id, row)

    latest_rows = list(latest_by_device.values())
    plausible = [
        row
        for row in latest_rows
        if MIN_PLAUSIBLE_INDOOR_C <= float(row.temperature) <= MAX_PLAUSIBLE_INDOOR_C
    ]
    if not plausible:
        return ControlTemperature(
            value=None,
            confidence="low",
            sensor_count=0,
            sample_count=len(rows),
            latest_reading=None,
            reason="no_fresh_selected_sensor_readings",
        )

    median = statistics.median(float(row.temperature) for row in plausible)
    inliers = [
        row
        for row in plausible
        if len(plausible) < 3 or abs(float(row.temperature) - median) <= OUTLIER_DISTANCE_C
    ]
    if not inliers:
        return ControlTemperature(
            value=None,
            confidence="low",
            sensor_count=0,
            sample_count=len(rows),
            latest_reading=None,
            reason="all_sensor_readings_rejected_as_outliers",
        )

    sensor_rows = tuple(
        SensorTemperature(
            device_id=row.device_id,
            device_label=getattr(row, "device_label", None),
            room=getattr(row, "room", None),
            temperature=round(float(row.temperature), 2),
            timestamp=row.timestamp,
        )
        for row in sorted(
            inliers,
            key=lambda item: (
                getattr(item, "room", None) or "",
                getattr(item, "device_label", None) or item.device_id,
            ),
        )
    )
    spread = round(
        max(row.temperature for row in inliers) - min(row.temperature for row in inliers), 2
    )
    reference_row = next((row for row in inliers if row.device_id == reference_sensor_id), None)
    if reference_sensor_id and reference_row is not None:
        return ControlTemperature(
            value=round(float(reference_row.temperature), 2),
            confidence="high",
            sensor_count=len(inliers),
            sample_count=len(rows),
            latest_reading=reference_row.timestamp,
            reason="reference_sensor",
            reference_sensor_id=reference_row.device_id,
            reference_sensor_label=getattr(reference_row, "device_label", None),
            reference_room=getattr(reference_row, "room", None),
            spread_c=spread,
            sensors=sensor_rows,
        )

    value = round(float(statistics.median(float(row.temperature) for row in inliers)), 2)
    latest = max(row.timestamp for row in inliers)
    confidence = "high" if len(inliers) >= 2 else "medium"
    reason = None if len(inliers) == len(plausible) else "outlier_sensor_readings_excluded"
    return ControlTemperature(
        value=value,
        confidence=confidence,
        sensor_count=len(inliers),
        sample_count=len(rows),
        latest_reading=latest,
        reason="reference_sensor_not_fresh" if reference_sensor_id else reason,
        spread_c=spread,
        sensors=sensor_rows,
    )

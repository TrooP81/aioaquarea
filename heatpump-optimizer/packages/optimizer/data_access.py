"""Shared data access for optimizer layers (rules, MILP)."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from dataclasses import asdict, dataclass, fields

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.device_status_values import DeviceStatusSnapshot
from packages.core.models import DeviceStatusRecord, SpaceHeatingGateRecord
from packages.core.planning_data_quality import load_planning_input_batch


async def _input_batch(session, start, end):
    batch = session.info.get("planning_input_batch")
    if batch is None or batch.start != start or batch.end != end:
        batch = await load_planning_input_batch(session, start, end)
        session.info["planning_input_batch"] = batch
    return batch


async def get_prices(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, float]]:
    """Fetch prices from DB for the active provider's area."""
    batch = await _input_batch(session, start, end)
    return [(row.ts, row.value) for row in batch.prices]


async def get_weather(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, float]]:
    """Fetch weather forecast (temperature only) from DB."""
    batch = await _input_batch(session, start, end)
    return [(row.ts, row.temperature) for row in batch.weather]


async def get_weather_full(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[dict]:
    """Fetch full weather data for the indoor model from DB."""
    batch = await _input_batch(session, start, end)
    return [asdict(row) for row in batch.weather]


async def get_last_status(session: AsyncSession) -> DeviceStatusSnapshot | None:
    """Capture latest status while its persistent attributes are still loaded."""
    result = await session.execute(
        select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return DeviceStatusSnapshot(
        **{field.name: deepcopy(getattr(row, field.name)) for field in fields(DeviceStatusSnapshot)}
    )


# Maximum age before data is considered stale
STALE_THRESHOLD = dt.timedelta(hours=6)


def check_data_staleness(
    prices: list[tuple[dt.datetime, float]],
    weather: list[tuple[dt.datetime, float]],
) -> list[str]:
    """Return warning messages for any stale data sources."""
    warnings = []
    now = dt.datetime.now(dt.timezone.utc)

    if not prices:
        warnings.append("No price data available")
    elif prices[-1][0] < now - STALE_THRESHOLD:
        warnings.append(f"Price data is stale (latest: {prices[-1][0].isoformat()})")

    if not weather:
        warnings.append("No weather data available")
    elif weather[-1][0] < now - STALE_THRESHOLD:
        warnings.append(f"Weather data is stale (latest: {weather[-1][0].isoformat()})")

    return warnings


@dataclass(frozen=True, slots=True)
class GateSnapshot:
    state: str
    config_fingerprint: str
    reason_code: str
    last_raw_outdoor_c: float | None


async def get_gate_snapshot(session: AsyncSession, device_id: str | None) -> GateSnapshot | None:
    """Capture only the persisted evidence used by effective-gate resolution."""
    if device_id is None:
        return None
    row = (
        await session.execute(
            select(SpaceHeatingGateRecord).where(SpaceHeatingGateRecord.device_id == device_id)
        )
    ).scalar_one_or_none()
    return (
        GateSnapshot(row.state, row.config_fingerprint, row.reason_code, row.last_raw_outdoor_c)
        if row is not None
        else None
    )


@dataclass(frozen=True, slots=True)
class ConsumptionSnapshot:
    device_id: str
    ts: dt.datetime
    source_date: dt.date | None
    source_timezone: str | None
    heat_kwh: float | None
    cool_kwh: float | None
    tank_kwh: float | None

    @classmethod
    def from_row(cls, row) -> ConsumptionSnapshot:
        """Preserve counter-day provenance before closing the source session."""
        return cls(**{field.name: getattr(row, field.name) for field in fields(cls)})

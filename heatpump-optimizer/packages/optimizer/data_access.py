"""Shared data access for optimizer layers (rules, MILP)."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import DeviceStatusRecord
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


async def get_last_status(session: AsyncSession):
    """Get latest device status record."""
    result = await session.execute(
        select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
    )
    return result.scalar_one_or_none()


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

"""Shared data access for optimizer layers (rules, MILP)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import DeviceStatusRecord, PriceRecord, WeatherRecord
from packages.core.settings_service import get_setting


async def get_prices(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, float]]:
    """Fetch prices from DB for the active provider's area."""
    provider = await get_setting("price_provider")
    if provider == "entsoe":
        area = (await get_setting("entsoe_area")) or "10YNL----------L"
    elif provider == "manual":
        area = "manual"
    else:
        area = "tibber"

    result = await session.execute(
        select(PriceRecord.ts, PriceRecord.price_eur_per_kwh)
        .where(
            and_(
                PriceRecord.ts >= start,
                PriceRecord.ts < end,
                PriceRecord.area == area,
            )
        )
        .order_by(PriceRecord.ts)
    )
    return [(row.ts, row.price_eur_per_kwh) for row in result.all()]


async def get_weather(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, float]]:
    """Fetch weather forecast (temperature only) from DB."""
    result = await session.execute(
        select(WeatherRecord.ts, WeatherRecord.temperature)
        .where(and_(WeatherRecord.ts >= start, WeatherRecord.ts < end))
        .order_by(WeatherRecord.ts)
    )
    return [(row.ts, row.temperature) for row in result.all()]


async def get_weather_full(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> list[dict]:
    """Fetch full weather data (temp, wind, irradiance, precipitation) from DB."""
    result = await session.execute(
        select(
            WeatherRecord.ts,
            WeatherRecord.temperature,
            WeatherRecord.wind_speed,
            WeatherRecord.irradiance,
            WeatherRecord.precipitation,
        )
        .where(and_(WeatherRecord.ts >= start, WeatherRecord.ts < end))
        .order_by(WeatherRecord.ts)
    )
    return [
        {
            "ts": row.ts,
            "temperature": row.temperature,
            "wind_speed": row.wind_speed,
            "irradiance": row.irradiance,
            "precipitation": row.precipitation,
        }
        for row in result.all()
    ]


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

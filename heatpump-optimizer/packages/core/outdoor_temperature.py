"""Resolve the outdoor temperature used by planning and learning.

The Aquarea outdoor sensor can be strongly affected by its physical placement.
The effective value therefore comes from the configured weather provider by
default, while the raw heat-pump sensor remains available for diagnostics.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import WeatherRecord
from packages.core.settings_service import (
    get_float_setting,
    get_int_setting,
    get_string_setting,
)


@dataclass(frozen=True)
class OutdoorTemperatureReading:
    effective_c: float | None
    heat_pump_c: float | None
    weather_c: float | None
    source: str
    weather_provider: str | None = None
    weather_ts: dt.datetime | None = None
    weather_issued_at: dt.datetime | None = None
    compensation_c: float | None = None
    fallback_reason: str | None = None


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def nearest_weather_temperature(
    rows: Iterable[object],
    at: dt.datetime,
    *,
    max_gap_seconds: float = 7200,
) -> object | None:
    """Return the closest temperature-bearing weather row within the allowed gap."""

    at = _as_utc(at)
    candidates = [
        row
        for row in rows
        if getattr(row, "temperature", None) is not None
        and isinstance(getattr(row, "ts", None), dt.datetime)
    ]
    if not candidates:
        return None
    closest = min(
        candidates,
        key=lambda row: abs((_as_utc(getattr(row, "ts")) - at).total_seconds()),
    )
    gap = abs((_as_utc(getattr(closest, "ts")) - at).total_seconds())
    return closest if gap <= max_gap_seconds else None


async def resolve_outdoor_temperature(
    session: AsyncSession,
    *,
    heat_pump_c: float | None,
    at: dt.datetime | None = None,
) -> OutdoorTemperatureReading:
    """Resolve one effective reading with explicit provenance and safe fallback."""

    at = _as_utc(at or dt.datetime.now(dt.timezone.utc))
    configured_source = (await get_string_setting("outdoor_temperature_source")).strip()
    provider = (await get_string_setting("weather_provider")).strip() or "open-meteo"
    adjustment_c = await get_float_setting("outdoor_temperature_weather_offset_c")
    max_age_minutes = await get_int_setting("outdoor_temperature_weather_max_age_minutes")

    if configured_source == "heat_pump":
        return OutdoorTemperatureReading(
            effective_c=float(heat_pump_c) if heat_pump_c is not None else None,
            heat_pump_c=float(heat_pump_c) if heat_pump_c is not None else None,
            weather_c=None,
            source="heat_pump",
            weather_provider=provider,
        )

    window = dt.timedelta(hours=2)
    result = await session.execute(
        select(WeatherRecord)
        .where(
            and_(
                WeatherRecord.source == provider,
                WeatherRecord.temperature.is_not(None),
                WeatherRecord.ts >= at - window,
                WeatherRecord.ts <= at + window,
            )
        )
        .order_by(WeatherRecord.ts)
    )
    weather = nearest_weather_temperature(result.scalars().all(), at)
    fallback_reason: str | None = None
    if weather is not None and weather.forecast_issued_at is not None:
        issued_at = _as_utc(weather.forecast_issued_at)
        if issued_at < at - dt.timedelta(minutes=max_age_minutes):
            weather = None
            fallback_reason = "weather_report_stale"

    raw_pump = float(heat_pump_c) if heat_pump_c is not None else None
    if weather is None:
        return OutdoorTemperatureReading(
            effective_c=raw_pump,
            heat_pump_c=raw_pump,
            weather_c=None,
            source="heat_pump_fallback",
            weather_provider=provider,
            fallback_reason=fallback_reason or "weather_report_unavailable",
        )

    weather_c = float(weather.temperature)
    effective_c = weather_c + adjustment_c
    return OutdoorTemperatureReading(
        effective_c=effective_c,
        heat_pump_c=raw_pump,
        weather_c=weather_c,
        source="weather",
        weather_provider=provider,
        weather_ts=_as_utc(weather.ts),
        weather_issued_at=(
            _as_utc(weather.forecast_issued_at) if weather.forecast_issued_at is not None else None
        ),
        compensation_c=(effective_c - raw_pump if raw_pump is not None else None),
    )

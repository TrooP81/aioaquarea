"""Frozen provider inputs and explainable planning readiness."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import PriceRecord, WeatherRecord
from packages.core.price_scope import resolve_price_area, tariff_metadata
from packages.core.settings_service import get_all_settings
from packages.core.time_slots import next_hour_boundary

DEFAULT_HORIZON_HOURS = 24
MINIMUM_CONTROL_HOURS = 6
MAX_PRICE_AGE = dt.timedelta(hours=2)
MAX_WEATHER_ISSUE_AGE = dt.timedelta(hours=4)


def _utc_hour(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)


def _contiguous_horizon_hours(hours: Iterable[dt.datetime], start: dt.datetime, limit: int) -> int:
    available = set(hours)
    count = 0
    for offset in range(limit):
        if start + dt.timedelta(hours=offset) not in available:
            break
        count += 1
    return count


@dataclass(frozen=True)
class PriceInput:
    ts: dt.datetime
    value: float
    currency: str
    source: str
    fetched_at: dt.datetime | None
    interval_schema: str | None


@dataclass(frozen=True)
class WeatherInput:
    ts: dt.datetime
    temperature: float | None
    wind_speed: float | None
    irradiance: float | None
    precipitation: float | None
    humidity: float | None
    cloud_cover: float | None
    source: str
    forecast_issued_at: dt.datetime | None
    units_schema: str | None


@dataclass(frozen=True)
class PlanningInputBatch:
    captured_at: dt.datetime
    start: dt.datetime
    end: dt.datetime
    area: str
    price_provider: str
    weather_provider: str
    prices: tuple[PriceInput, ...]
    weather: tuple[WeatherInput, ...]


async def load_planning_input_batch(session, start, end, *, now=None) -> PlanningInputBatch:
    """Read settings and both feeds in the caller's consistent read-only transaction."""
    captured_at = now or dt.datetime.now(dt.timezone.utc)
    values = await get_all_settings(session=session)
    provider = values["price_provider"]
    area = await resolve_price_area(values, session=session)
    weather_provider = values["weather_provider"] or "open-meteo"
    price_rows = (
        (
            await session.execute(
                select(PriceRecord)
                .where(PriceRecord.area == area, PriceRecord.ts >= start, PriceRecord.ts < end)
                .order_by(PriceRecord.ts)
            )
        )
        .scalars()
        .all()
    )
    weather_rows = (
        (
            await session.execute(
                select(WeatherRecord)
                .where(
                    WeatherRecord.source == weather_provider,
                    WeatherRecord.ts >= start,
                    WeatherRecord.ts < end,
                )
                .order_by(WeatherRecord.ts)
            )
        )
        .scalars()
        .all()
    )
    return PlanningInputBatch(
        captured_at,
        start,
        end,
        area,
        provider,
        weather_provider,
        tuple(
            PriceInput(
                row.ts,
                row.price_eur_per_kwh,
                row.price_currency,
                row.price_source,
                row.fetched_at,
                row.interval_schema,
            )
            for row in price_rows
        ),
        tuple(
            WeatherInput(
                row.ts,
                row.temperature,
                row.wind_speed,
                row.irradiance,
                row.precipitation,
                row.humidity,
                row.cloud_cover,
                row.source,
                row.forecast_issued_at,
                row.units_schema,
            )
            for row in weather_rows
        ),
    )


def planning_batch_quality(
    batch: PlanningInputBatch, *, minimum_control_hours=MINIMUM_CONTROL_HOURS
) -> dict[str, object]:
    horizon_hours = int((batch.end - batch.start).total_seconds() / 3600)
    now = batch.captured_at

    def finite(value):
        return (
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
        )

    def fresh(clock, maximum):
        return (
            isinstance(clock, dt.datetime)
            and clock.tzinfo is not None
            and dt.timedelta(0) <= now - clock <= maximum
        )

    currencies = {row.currency for row in batch.prices}
    price_context_valid = (
        len(currencies) == 1
        and all(isinstance(currency, str) and len(currency) == 3 for currency in currencies)
        and all(row.source == batch.price_provider for row in batch.prices)
    )
    price_hours = _contiguous_horizon_hours(
        (
            row.ts
            for row in batch.prices
            if finite(row.value) and row.interval_schema == "hourly_duration_weighted_v1"
        ),
        batch.start,
        horizon_hours,
    )
    weather_hours = _contiguous_horizon_hours(
        (
            row.ts
            for row in batch.weather
            if row.units_schema == "weather_c_mps_wm2_mm_v1"
            and row.source == batch.weather_provider
            and all(finite(value) for value in (row.temperature, row.wind_speed, row.irradiance))
        ),
        batch.start,
        horizon_hours,
    )
    price_fresh = bool(batch.prices) and all(
        fresh(row.fetched_at, MAX_PRICE_AGE) for row in batch.prices
    )
    weather_fresh = bool(batch.weather) and all(
        fresh(row.forecast_issued_at, MAX_WEATHER_ISSUE_AGE) for row in batch.weather
    )
    weather_coherent = len({row.forecast_issued_at for row in batch.weather}) == 1
    price_coherent = len({row.fetched_at for row in batch.prices}) == 1
    prices_unique = len({row.ts for row in batch.prices}) == len(batch.prices)
    weather_unique = len({row.ts for row in batch.weather}) == len(batch.weather)
    reasons = []
    if price_hours < minimum_control_hours:
        reasons.append(f"Only {price_hours} contiguous future price hours are available.")
    if not price_fresh:
        reasons.append("Price feed clocks are missing, future or stale.")
    if not price_context_valid or not price_coherent or not prices_unique:
        reasons.append("Price provider, currency or retrieval batch is incoherent.")
    if weather_hours < minimum_control_hours:
        reasons.append(f"Only {weather_hours} contiguous future weather hours are available.")
    if not weather_fresh or not weather_coherent or not weather_unique:
        reasons.append("Weather forecast clocks are missing, future, stale or incoherent.")

    def oldest_clock(clocks):
        # Invalid clocks already deny readiness above. Do not let diagnostic
        # formatting compare naive legacy values with aware batch clocks.
        return min(
            (
                clock
                for clock in clocks
                if isinstance(clock, dt.datetime) and clock.tzinfo is not None
            ),
            default=None,
        )

    latest_price = oldest_clock(row.fetched_at for row in batch.prices)
    latest_weather = oldest_clock(row.forecast_issued_at for row in batch.weather)

    def age(clock):
        return round((now - clock).total_seconds()) if clock and clock.tzinfo is not None else None

    allowed = not reasons
    return {
        "control_allowed": allowed,
        "status": "ready" if allowed else "degraded",
        "horizon_start": batch.start.isoformat(),
        "horizon_hours": horizon_hours,
        "minimum_control_hours": minimum_control_hours,
        "effective_horizon_hours": min(price_hours, weather_hours),
        "price_horizon_limited": price_hours < horizon_hours,
        "reoptimization_when_prices_extend": price_hours < horizon_hours and allowed,
        "price": {
            **(
                tariff_metadata(next(iter(currencies), ""))
                if batch.price_provider == "tibber"
                else {}
            ),
            "area": batch.area,
            "source": batch.price_provider,
            "currency": next(iter(currencies)) if price_context_valid else None,
            "contiguous_hours": price_hours,
            "complete_horizon": price_hours >= horizon_hours,
            "latest_fetched_at": latest_price.isoformat() if latest_price else None,
            "age_seconds": age(latest_price),
            "fresh": price_fresh,
            "next_publication_check_seconds": 900 if price_hours < horizon_hours else None,
        },
        "weather": {
            "source": batch.weather_provider,
            "contiguous_hours": weather_hours,
            "complete_horizon": weather_hours >= horizon_hours,
            "latest_issued_at": latest_weather.isoformat() if latest_weather else None,
            "age_seconds": age(latest_weather),
            "fresh": weather_fresh,
        },
        "captured_at": now.isoformat(),
        "reasons": reasons,
    }


async def get_planning_data_quality(
    *,
    now=None,
    horizon_hours=DEFAULT_HORIZON_HOURS,
    minimum_control_hours=MINIMUM_CONTROL_HOURS,
    batch=None,
) -> dict[str, object]:
    if batch is None:
        now = now or dt.datetime.now(dt.timezone.utc)
        start = next_hour_boundary(now)
        async with get_session(read_only=True, isolation_level="REPEATABLE READ") as session:
            batch = await load_planning_input_batch(
                session, start, start + dt.timedelta(hours=horizon_hours), now=now
            )
    return planning_batch_quality(batch, minimum_control_hours=minimum_control_hours)

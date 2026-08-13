"""Freshness and horizon checks for data that can influence new control plans."""

from __future__ import annotations

import datetime as dt
from typing import Iterable

from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import PriceRecord, WeatherRecord
from packages.core.pricing import get_active_price_area
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
    available = {_utc_hour(value) for value in hours}
    count = 0
    for offset in range(limit):
        if start + dt.timedelta(hours=offset) not in available:
            break
        count += 1
    return count


async def get_planning_data_quality(
    *,
    now: dt.datetime | None = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    minimum_control_hours: int = MINIMUM_CONTROL_HOURS,
) -> dict[str, object]:
    """Return explainable input readiness without inventing missing forecasts.

    The optimizer may produce a shorter plan when a provider has not published
    tomorrow yet, but it must have enough contiguous, freshly fetched inputs to
    make the next decision safely.  Future market timestamps alone do not prove
    that a price feed was retrieved recently, hence ``PriceRecord.fetched_at``.
    """

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    horizon_start = next_hour_boundary(now)
    horizon_end = horizon_start + dt.timedelta(hours=horizon_hours)
    area = await get_active_price_area()

    async with get_session() as session:
        price_rows = (
            await session.execute(
                select(PriceRecord.ts, PriceRecord.fetched_at)
                .where(
                    PriceRecord.area == area,
                    PriceRecord.ts >= horizon_start,
                    PriceRecord.ts < horizon_end,
                )
                .order_by(PriceRecord.ts)
            )
        ).all()
        latest_price_fetch = (
            await session.execute(
                select(PriceRecord.fetched_at)
                .where(PriceRecord.area == area, PriceRecord.fetched_at.is_not(None))
                .order_by(PriceRecord.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        weather_rows = (
            await session.execute(
                select(WeatherRecord.ts, WeatherRecord.forecast_issued_at)
                .where(WeatherRecord.ts >= horizon_start, WeatherRecord.ts < horizon_end)
                .order_by(WeatherRecord.ts)
            )
        ).all()

    price_hours = _contiguous_horizon_hours(
        (row.ts for row in price_rows), horizon_start, horizon_hours
    )
    weather_hours = _contiguous_horizon_hours(
        (row.ts for row in weather_rows), horizon_start, horizon_hours
    )

    if latest_price_fetch is None:
        price_age_seconds = None
        # Older rows predate the provenance field. They can still be displayed,
        # but are never considered fresh for autonomous re-planning.
        price_fresh = False
    else:
        if latest_price_fetch.tzinfo is None:
            latest_price_fetch = latest_price_fetch.replace(tzinfo=dt.timezone.utc)
        price_age_seconds = max(0, round((now - latest_price_fetch).total_seconds()))
        price_fresh = now - latest_price_fetch <= MAX_PRICE_AGE

    issued_values = [row.forecast_issued_at for row in weather_rows if row.forecast_issued_at]
    latest_weather_issue = max(issued_values) if issued_values else None
    if latest_weather_issue is None:
        weather_issue_age_seconds = None
        weather_fresh = False
    else:
        if latest_weather_issue.tzinfo is None:
            latest_weather_issue = latest_weather_issue.replace(tzinfo=dt.timezone.utc)
        weather_issue_age_seconds = max(0, round((now - latest_weather_issue).total_seconds()))
        weather_fresh = now - latest_weather_issue <= MAX_WEATHER_ISSUE_AGE

    reasons: list[str] = []
    if price_hours < minimum_control_hours:
        reasons.append(f"Only {price_hours} contiguous future price hours are available.")
    if not price_fresh:
        reasons.append("Price feed has not been fetched within the safe freshness window.")
    if weather_hours < minimum_control_hours:
        reasons.append(f"Only {weather_hours} contiguous future weather hours are available.")
    if not weather_fresh:
        reasons.append("Weather forecast issue time is missing or stale.")

    control_allowed = not reasons
    effective_horizon_hours = min(price_hours, weather_hours)
    price_horizon_limited = price_hours < horizon_hours
    return {
        "control_allowed": control_allowed,
        "status": "ready" if control_allowed else "degraded",
        "horizon_start": horizon_start.isoformat(),
        "horizon_hours": horizon_hours,
        "minimum_control_hours": minimum_control_hours,
        "effective_horizon_hours": effective_horizon_hours,
        "price_horizon_limited": price_horizon_limited,
        "reoptimization_when_prices_extend": price_horizon_limited and control_allowed,
        "price": {
            "area": area,
            "contiguous_hours": price_hours,
            "complete_horizon": price_hours >= horizon_hours,
            "latest_fetched_at": latest_price_fetch.isoformat() if latest_price_fetch else None,
            "age_seconds": price_age_seconds,
            "fresh": price_fresh,
            # Tomorrow's Nordic day-ahead prices may be published after a
            # partial plan already exists. The poller rechecks this small
            # endpoint every 15 minutes and queues one reoptimization only
            # when the horizon genuinely extends.
            "next_publication_check_seconds": 15 * 60 if price_hours < horizon_hours else None,
        },
        "weather": {
            "contiguous_hours": weather_hours,
            "complete_horizon": weather_hours >= horizon_hours,
            "latest_issued_at": latest_weather_issue.isoformat() if latest_weather_issue else None,
            "age_seconds": weather_issue_age_seconds,
            "fresh": weather_fresh,
        },
        "reasons": reasons,
    }

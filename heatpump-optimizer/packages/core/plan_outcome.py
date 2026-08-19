"""Measured plan and period outcomes with explicit counterfactual limits.

The heat pump exposes cumulative energy counters, not per-command energy.  This
module therefore never claims that one command caused a particular kWh amount.
It measures the electricity used during a plan window and compares its
price-weighting with an unshifted, flat-price baseline for the same measured
energy.  The result is useful for evaluating load shifting while staying honest
about what the available telemetry can prove.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    PlanRecord,
    PriceRecord,
)

MATCHED_WEATHER_MAX_OUTDOOR_DELTA_C = 2.0
MATCHED_WEATHER_MAX_WINDOWS = 8


def hour_start(value: dt.datetime) -> dt.datetime:
    """Return an aware timestamp rounded down to the market-price hour."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)


def counter_value(record: ConsumptionRecord) -> float:
    """Return one cumulative electricity-counter total, safely treating gaps as zero."""

    return float(record.heat_kwh or 0) + float(record.cool_kwh or 0) + float(record.tank_kwh or 0)


def cumulative_counter_delta(current: float, previous: float, *, day_changed: bool) -> float:
    """Return a positive interval delta, accounting for a daily counter reset."""

    if current >= previous:
        return current - previous
    return current if day_changed else 0.0


def cumulative_intervals(
    records: Iterable[ConsumptionRecord], timezone_name: str = "UTC"
) -> list[tuple[dt.datetime, float]]:
    """Convert cumulative readings to positive interval kWh values.

    Counters reset at a local day boundary on several Aquarea installations.
    The first post-reset value is the energy accumulated since local midnight.
    """

    timezone = ZoneInfo(timezone_name)
    intervals: list[tuple[dt.datetime, float]] = []
    previous: ConsumptionRecord | None = None
    for record in sorted(records, key=lambda row: row.ts):
        if previous is not None:
            day_changed = record.ts.astimezone(timezone).date() != previous.ts.astimezone(
                timezone
            ).date()
            # Meter counters arrive as floats; round only the interval to
            # prevent binary representation noise from leaking into costs/UI.
            delta = round(
                cumulative_counter_delta(
                    counter_value(record),
                    counter_value(previous),
                    day_changed=day_changed,
                ),
                6,
            )
            if delta > 0:
                intervals.append((record.ts, delta))
        previous = record
    return intervals


def cost_outcome(
    intervals: Iterable[tuple[dt.datetime, float]],
    prices: dict[dt.datetime, float],
) -> dict[str, float | int | None]:
    """Price metered energy and compare it with a flat-price load-shift baseline."""

    measured_kwh = 0.0
    priced_kwh = 0.0
    cost = 0.0
    for timestamp, kwh in intervals:
        measured_kwh += kwh
        price = prices.get(hour_start(timestamp))
        if price is None:
            continue
        priced_kwh += kwh
        cost += kwh * price

    coverage = 100.0 if measured_kwh <= 0.0001 else 100.0 * priced_kwh / measured_kwh
    available_prices = list(prices.values())
    flat_price = sum(available_prices) / len(available_prices) if available_prices else None
    baseline = flat_price * priced_kwh if flat_price is not None else None
    savings = baseline - cost if baseline is not None and priced_kwh > 0 else None
    return {
        "measured_kwh": round(measured_kwh, 3),
        "priced_kwh": round(priced_kwh, 3),
        "unpriced_kwh": round(max(0.0, measured_kwh - priced_kwh), 3),
        "coverage_pct": round(max(0.0, min(100.0, coverage)), 1),
        "actual_cost": round(cost, 3) if priced_kwh > 0 else None,
        "flat_price_baseline_cost": round(baseline, 3) if baseline is not None else None,
        "estimated_price_shift_savings": round(savings, 3) if savings is not None else None,
        "flat_price_per_kwh": round(flat_price, 5) if flat_price is not None else None,
        "priced_hours": len(available_prices),
    }


def comfort_outcome(
    readings: Iterable[IndoorTempReading],
    comfort_min_c: float,
    comfort_max_c: float,
) -> dict[str, float | int | None]:
    """Summarise trusted sensor readings in a time window."""

    values = [float(reading.temperature) for reading in readings]
    if not values:
        return {
            "samples": 0,
            "average_c": None,
            "minimum_c": None,
            "maximum_c": None,
            "within_range_pct": None,
            "below_range_samples": 0,
            "above_range_samples": 0,
        }
    below = sum(value < comfort_min_c for value in values)
    above = sum(value > comfort_max_c for value in values)
    in_range = len(values) - below - above
    return {
        "samples": len(values),
        "average_c": round(sum(values) / len(values), 2),
        "minimum_c": round(min(values), 2),
        "maximum_c": round(max(values), 2),
        "within_range_pct": round(100 * in_range / len(values), 1),
        "below_range_samples": below,
        "above_range_samples": above,
    }


def weather_matched_energy_comparison(
    *,
    current_intervals: Iterable[tuple[dt.datetime, float]],
    all_intervals: Iterable[tuple[dt.datetime, float]],
    outdoor_samples: Iterable[tuple[dt.datetime, float]],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, object]:
    """Compare a period with prior windows that had similar outdoor weather.

    This is deliberately an observational comparison, not a claim that a plan
    caused an energy delta. It makes the baseline more useful while preserving
    the causal limitation of one whole-pump cumulative meter.
    """

    duration = end - start
    current_energy = sum(kwh for _, kwh in current_intervals)
    outdoor = list(outdoor_samples)

    def average_outdoor(window_start: dt.datetime, window_end: dt.datetime) -> float | None:
        # Consumption intervals are represented by their end timestamp.  The
        # same half-open convention keeps adjacent comparison windows fully
        # disjoint at midnight.
        values = [temp for timestamp, temp in outdoor if window_start < timestamp <= window_end]
        return round(sum(values) / len(values), 2) if values else None

    current_outdoor = average_outdoor(start, end)
    if duration < dt.timedelta(hours=12) or current_outdoor is None or current_energy <= 0:
        return {
            "status": "waiting_for_period_or_weather",
            "candidate_windows": 0,
            "note": "Weather-matched comparison needs a longer measured period with outdoor observations.",
        }

    intervals = list(all_intervals)
    candidates: list[tuple[float, float]] = []
    # Use non-overlapping prior windows. A one-day gap avoids comparing the
    # same counter transition in adjacent windows.
    for index in range(1, MATCHED_WEATHER_MAX_WINDOWS + 1):
        candidate_end = start - dt.timedelta(days=1) - duration * (index - 1)
        candidate_start = candidate_end - duration
        candidate_outdoor = average_outdoor(candidate_start, candidate_end)
        if candidate_outdoor is None:
            continue
        difference = abs(candidate_outdoor - current_outdoor)
        if difference > MATCHED_WEATHER_MAX_OUTDOOR_DELTA_C:
            continue
        candidate_energy = sum(
            kwh for timestamp, kwh in intervals if candidate_start < timestamp <= candidate_end
        )
        if candidate_energy > 0:
            candidates.append((candidate_energy, difference))

    if len(candidates) < 2:
        return {
            "status": "insufficient_similar_weather",
            "candidate_windows": len(candidates),
            "current_average_outdoor_c": current_outdoor,
            "max_outdoor_delta_c": MATCHED_WEATHER_MAX_OUTDOOR_DELTA_C,
            "note": "No causal saving is inferred until at least two earlier windows with similar outdoor weather are available.",
        }

    baseline = sum(energy for energy, _ in candidates) / len(candidates)
    return {
        "status": "observational_comparison",
        "candidate_windows": len(candidates),
        "current_average_outdoor_c": current_outdoor,
        "max_outdoor_delta_c": MATCHED_WEATHER_MAX_OUTDOOR_DELTA_C,
        "matched_average_energy_kwh": round(baseline, 3),
        "energy_delta_vs_matched_kwh": round(baseline - current_energy, 3),
        "average_outdoor_difference_c": round(
            sum(difference for _, difference in candidates) / len(candidates), 2
        ),
        "note": "Comparison uses earlier, non-overlapping windows with similar average outdoor temperature. It is observational, not proof that a plan caused the energy difference.",
    }


async def measured_window_outcome(
    session: AsyncSession,
    *,
    start: dt.datetime,
    end: dt.datetime,
    price_area: str,
    price_currency: str | None,
    price_source: str | None,
    comfort_min_c: float,
    comfort_max_c: float,
    timezone_name: str = "UTC",
) -> dict[str, object]:
    """Measure electricity price exposure and indoor comfort for one window."""

    # A preceding reading is needed to calculate the first in-window delta.
    comparison_start = start - (end - start) * MATCHED_WEATHER_MAX_WINDOWS - dt.timedelta(days=1)
    meter_rows = (
        (
            await session.execute(
                select(ConsumptionRecord)
                .where(ConsumptionRecord.ts >= start - dt.timedelta(hours=1))
                .where(ConsumptionRecord.ts <= end)
                .order_by(ConsumptionRecord.ts)
            )
        )
        .scalars()
        .all()
    )
    comparison_meter_rows = (
        (
            await session.execute(
                select(ConsumptionRecord)
                .where(ConsumptionRecord.ts >= comparison_start - dt.timedelta(hours=1))
                .where(ConsumptionRecord.ts <= end)
                .order_by(ConsumptionRecord.ts)
            )
        )
        .scalars()
        .all()
    )
    outdoor_rows = (
        await session.execute(
            select(DeviceStatusRecord.ts, DeviceStatusRecord.outdoor_temp)
            .where(DeviceStatusRecord.ts >= comparison_start)
            .where(DeviceStatusRecord.ts <= end)
            .where(DeviceStatusRecord.outdoor_temp.is_not(None))
            .order_by(DeviceStatusRecord.ts)
        )
    ).all()
    readings = (
        (
            await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= start)
                .where(IndoorTempReading.timestamp <= end)
                .where(IndoorTempReading.is_stale.is_(False))
                .order_by(IndoorTempReading.timestamp)
            )
        )
        .scalars()
        .all()
    )
    prices_query = (
        select(PriceRecord.ts, PriceRecord.price_eur_per_kwh)
        .where(PriceRecord.ts >= hour_start(start))
        .where(PriceRecord.ts <= hour_start(end))
        .where(PriceRecord.area == price_area)
    )
    if price_currency:
        prices_query = prices_query.where(PriceRecord.price_currency == price_currency)
    if price_source:
        prices_query = prices_query.where(PriceRecord.price_source == price_source)
    price_rows = (await session.execute(prices_query)).all()
    prices = {hour_start(timestamp): float(price) for timestamp, price in price_rows}

    # Keep only meter intervals that belong to the requested window.  The
    # leading row may be before ``start`` and is only used as a baseline.
    intervals = [
        (timestamp, kwh)
        for timestamp, kwh in cumulative_intervals(meter_rows, timezone_name)
        if start <= timestamp <= end
    ]
    comparison_intervals = cumulative_intervals(comparison_meter_rows, timezone_name)
    return {
        "cost": cost_outcome(intervals, prices),
        "comfort": comfort_outcome(readings, comfort_min_c, comfort_max_c),
        "weather_matched_comparison": weather_matched_energy_comparison(
            current_intervals=intervals,
            all_intervals=comparison_intervals,
            outdoor_samples=[
                (timestamp, float(temperature)) for timestamp, temperature in outdoor_rows
            ],
            start=start,
            end=end,
        ),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }


async def plan_measurement(
    session: AsyncSession,
    *,
    plan: PlanRecord,
    price_area: str,
    comfort_min_c: float,
    comfort_max_c: float,
    now: dt.datetime | None = None,
    timezone_name: str = "UTC",
) -> dict[str, object]:
    """Return the measured portion of one immutable plan."""

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    end = min(now.astimezone(dt.timezone.utc), plan.horizon_end)
    if end <= plan.horizon_start:
        return {
            "state": "not_started",
            "progress_pct": 0.0,
            "note": "The plan window has not started, so no measured outcome exists yet.",
        }

    outcome = await measured_window_outcome(
        session,
        start=plan.horizon_start,
        end=end,
        price_area=price_area,
        price_currency=plan.price_currency,
        price_source=plan.price_source,
        comfort_min_c=comfort_min_c,
        comfort_max_c=comfort_max_c,
        timezone_name=timezone_name,
    )
    duration = max(1.0, (plan.horizon_end - plan.horizon_start).total_seconds())
    elapsed = min(duration, max(0.0, (end - plan.horizon_start).total_seconds()))
    state = "completed" if end >= plan.horizon_end else "in_progress"
    return {
        "state": state,
        "progress_pct": round(100 * elapsed / duration, 1),
        **outcome,
        "baseline_method": (
            "Estimated price-shift savings compare measured energy with the same energy "
            "bought at the simple average available market price in this plan window."
        ),
        "note": (
            "Energy meters are cumulative for the whole heat pump. This evaluates the plan window, "
            "not an unprovable kWh amount for an individual command."
        ),
    }

import datetime as dt
from types import SimpleNamespace

from packages.core.plan_outcome import (
    comfort_outcome,
    cost_outcome,
    cumulative_intervals,
    weather_matched_energy_comparison,
)


UTC = dt.timezone.utc


def _meter(hour: int, kwh: float):
    return SimpleNamespace(
        ts=dt.datetime(2026, 7, 15, hour, tzinfo=UTC),
        heat_kwh=kwh,
        cool_kwh=0.0,
        tank_kwh=0.0,
    )


def test_cumulative_intervals_skips_unknown_first_reading_and_day_resets():
    records = [_meter(8, 3.0), _meter(9, 3.6), _meter(10, 4.0)]

    assert cumulative_intervals(records) == [
        (dt.datetime(2026, 7, 15, 9, tzinfo=UTC), 0.6),
        (dt.datetime(2026, 7, 15, 10, tzinfo=UTC), 0.4),
    ]


def test_cumulative_intervals_uses_local_day_reset_value():
    records = [
        SimpleNamespace(
            ts=dt.datetime(2026, 7, 15, 21, 55, tzinfo=UTC),
            heat_kwh=4.0,
            cool_kwh=0.0,
            tank_kwh=1.0,
        ),
        SimpleNamespace(
            ts=dt.datetime(2026, 7, 15, 22, 5, tzinfo=UTC),
            heat_kwh=0.2,
            cool_kwh=0.0,
            tank_kwh=0.1,
        ),
    ]

    assert cumulative_intervals(records, "Europe/Stockholm") == [
        (dt.datetime(2026, 7, 15, 22, 5, tzinfo=UTC), 0.3)
    ]


def test_cost_outcome_reports_price_shift_estimate_only_for_metered_energy():
    intervals = [
        (dt.datetime(2026, 7, 15, 9, 15, tzinfo=UTC), 1.0),
        (dt.datetime(2026, 7, 15, 10, 15, tzinfo=UTC), 1.0),
    ]
    prices = {
        dt.datetime(2026, 7, 15, 9, tzinfo=UTC): 0.1,
        dt.datetime(2026, 7, 15, 10, tzinfo=UTC): 0.3,
    }

    outcome = cost_outcome(intervals, prices)

    assert outcome["actual_cost"] == 0.4
    assert outcome["flat_price_baseline_cost"] == 0.4
    assert outcome["estimated_price_shift_savings"] == 0.0
    assert outcome["coverage_pct"] == 100.0


def test_comfort_outcome_reports_measured_range_without_claiming_duration():
    readings = [
        SimpleNamespace(temperature=20.0),
        SimpleNamespace(temperature=21.0),
        SimpleNamespace(temperature=22.0),
    ]

    outcome = comfort_outcome(readings, comfort_min_c=20.5, comfort_max_c=21.5)

    assert outcome["samples"] == 3
    assert outcome["within_range_pct"] == 33.3
    assert outcome["below_range_samples"] == 1
    assert outcome["above_range_samples"] == 1


def test_weather_matched_comparison_is_explicitly_observational():
    start = dt.datetime(2026, 7, 15, tzinfo=UTC)
    end = start + dt.timedelta(days=1)

    current = [(start + dt.timedelta(hours=hour), 0.5) for hour in range(1, 25)]
    first_match = [
        (start - dt.timedelta(days=2) + dt.timedelta(hours=hour), 1.0) for hour in range(1, 25)
    ]
    second_match = [
        (start - dt.timedelta(days=3) + dt.timedelta(hours=hour), 1.0) for hour in range(1, 25)
    ]
    outdoor = [(timestamp, 5.0) for timestamp, _ in [*current, *first_match, *second_match]]

    result = weather_matched_energy_comparison(
        current_intervals=current,
        all_intervals=[*current, *first_match, *second_match],
        outdoor_samples=outdoor,
        start=start,
        end=end,
    )

    assert result["status"] == "observational_comparison"
    assert result["candidate_windows"] == 2
    assert result["matched_average_energy_kwh"] == 24.0
    assert result["energy_delta_vs_matched_kwh"] == 12.0
    assert "not proof" in result["note"]

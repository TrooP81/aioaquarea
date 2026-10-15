"""S3 meter and tariff contracts, isolated from poller/device fixtures."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from packages.core.plan_outcome import (
    confirmed_energy_intervals,
    duration_allocated_cost,
    observed_rate_baseline,
)
from packages.ml.models_common import (
    iter_confirmed_zero_intervals,
    iter_counter_change_intervals,
)

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 12, 8, tzinfo=UTC)


def _row(timestamp, value, *, device="a", source_date=None, source_timezone="UTC"):
    return SimpleNamespace(
        device_id=device,
        ts=timestamp,
        heat_kwh=value,
        cool_kwh=None,
        tank_kwh=None,
        source_date=source_date or timestamp.date(),
        source_timezone=source_timezone,
    )


@pytest.mark.parametrize("confirmed", [False, True])
def test_R14_return_to_anchor_cancels_transient_correction(confirmed):
    rows = [
        _row(START + dt.timedelta(minutes=15 * index), value)
        for index, value in enumerate((5.0, 4.0, 5.0, 4.5, 5.5, 5.5))
    ]
    diagnostics = {}
    intervals = list(
        iter_counter_change_intervals(
            rows,
            "heat_kwh",
            ZoneInfo("UTC"),
            confirm_changes=confirmed,
            diagnostics=diagnostics,
        )
    )
    assert [item.energy_kwh for item in intervals] == pytest.approx([0.5])
    assert intervals[0].start_ts == START
    assert diagnostics.get("correction_reanchor", 0) == 0


@pytest.mark.parametrize("confirmed", [False, True])
def test_R14_two_consecutive_lower_readings_reanchor_without_inventing_energy(confirmed):
    rows = [
        _row(START + dt.timedelta(minutes=15 * index), value)
        for index, value in enumerate((None, 5.0, 4.0, 4.5, 5.0, 5.0))
    ]
    intervals = list(
        iter_counter_change_intervals(rows, "heat_kwh", ZoneInfo("UTC"), confirm_changes=confirmed)
    )
    assert [item.energy_kwh for item in intervals] == pytest.approx([0.5])
    assert intervals[0].start_ts == rows[3].ts


@pytest.mark.parametrize("day", [dt.date(2026, 3, 29), dt.date(2026, 10, 25)])
@pytest.mark.parametrize("value", [1.0, 2.0])
@pytest.mark.parametrize("confirmed", [False, True])
def test_R14_equal_or_greater_source_reset_uses_recorded_dst_boundary(day, value, confirmed):
    zone = ZoneInfo("Europe/Stockholm")
    boundary = dt.datetime.combine(day, dt.time.min, tzinfo=zone).astimezone(UTC)
    rows = [
        _row(
            boundary - dt.timedelta(minutes=10),
            1.0,
            source_date=day - dt.timedelta(days=1),
            source_timezone=zone.key,
        ),
        _row(boundary + dt.timedelta(minutes=10), value, source_date=day, source_timezone=zone.key),
        _row(boundary + dt.timedelta(minutes=20), value, source_date=day, source_timezone=zone.key),
    ]
    intervals = list(
        iter_counter_change_intervals(
            rows, "heat_kwh", ZoneInfo("America/New_York"), confirm_changes=confirmed
        )
    )
    assert len(intervals) == 1
    assert intervals[0].start_ts == boundary
    assert intervals[0].elapsed_hours == pytest.approx(1 / 6)
    assert intervals[0].energy_kwh == value


def test_R14_confirmed_channels_are_device_scoped_and_missing_is_not_zero():
    rows = [
        _row(START, 100.0, device="b"),
        _row(START, 1.0),
        _row(START + dt.timedelta(minutes=15), 102.0, device="b"),
        _row(START + dt.timedelta(minutes=15), 2.0),
        _row(START + dt.timedelta(minutes=30), 102.0, device="b"),
        _row(START + dt.timedelta(minutes=30), 2.0),
    ]
    intervals = confirmed_energy_intervals(reversed(rows))
    assert len(intervals) == 2
    assert sum(item.energy_kwh for item in intervals) == 3.0
    assert {item.device_id for item in intervals} == {"a", "b"}


@pytest.mark.parametrize("value", [-1.0, True, None, float("nan"), float("inf")])
def test_R14_invalid_equal_counters_cannot_certify_zero(value):
    rows = [_row(START, value), _row(START + dt.timedelta(minutes=15), value)]
    statuses = [
        SimpleNamespace(
            device_id="a", ts=START + dt.timedelta(minutes=5 * index), space_heating_active=False
        )
        for index in range(4)
    ]
    assert list(iter_confirmed_zero_intervals(rows, statuses, ZoneInfo("UTC"))) == []


@pytest.mark.parametrize(
    "status_device,active,expected",
    [
        ("a", False, 1),
        ("b", False, 0),
        ("a", True, 0),
    ],
)
def test_R14_zero_requires_same_device_fresh_inactive_continuity(status_device, active, expected):
    rows = [_row(START, 1.0), _row(START + dt.timedelta(minutes=15), 1.0)]
    statuses = [
        SimpleNamespace(
            device_id=status_device,
            ts=START + dt.timedelta(minutes=5 * index),
            space_heating_active=active,
        )
        for index in range(4)
    ]
    intervals = list(iter_confirmed_zero_intervals(rows, statuses, ZoneInfo("UTC")))
    assert len(intervals) == expected
    assert sum(item.energy_kwh for item in intervals) == 0.0
    assert list(iter_confirmed_zero_intervals(rows, [], ZoneInfo("UTC"))) == []


def test_R15_daily_counter_units_are_interval_energy_not_sample_average():
    rows = [
        _row(START + dt.timedelta(minutes=15 * index), value)
        for index, value in enumerate((0.0, 4.0, 8.0, 12.0, 12.0))
    ]
    prices = [(START + dt.timedelta(hours=index), 0.25) for index in range(2)]
    baseline = observed_rate_baseline(rows, prices, device_id="a")
    assert baseline["measured_interval_kwh"] == 12.0
    assert baseline["measured_interval_hours"] == 0.75
    assert baseline["unshifted_baseline_cost"] == 8.0
    assert baseline["strategy_cost"] is None
    assert baseline["strategy_cost_reason"] == "serialized_strategy_replay_unavailable"
    assert baseline["baseline_estimated"] is True


@pytest.mark.parametrize(
    "prices,priced,cost,coverage",
    [
        ({START: -0.1, START + dt.timedelta(hours=1): 0.3}, 2.0, 0.2, 100.0),
        ({START: -0.1}, 1.0, -0.1, 50.0),
        ({}, 0.0, None, 0.0),
        ({START: float("nan")}, 0.0, None, 0.0),
    ],
)
def test_R15_tariff_allocation_discloses_estimate_and_unpriced_energy(
    prices, priced, cost, coverage
):
    rows = [
        _row(START + dt.timedelta(minutes=30), 100.0),
        _row(START + dt.timedelta(minutes=90), 102.0),
        _row(START + dt.timedelta(minutes=100), 102.0),
    ]
    outcome = duration_allocated_cost(rows, prices, start=START, end=START + dt.timedelta(hours=2))
    assert outcome["measured_kwh"] == 2.0
    assert outcome["priced_kwh"] == priced
    assert outcome["unpriced_kwh"] == 2.0 - priced
    assert outcome["actual_cost"] == cost
    assert outcome["coverage_pct"] == coverage
    assert outcome["tariff_allocation_estimated"] is True
    assert outcome["command_attribution_supported"] is False
    assert outcome["allocation_basis"] == "uniform_duration_within_confirmed_counter_intervals"


def test_R15_window_edges_prorate_energy_and_unconfirmed_tail_stays_unknown():
    rows = [
        _row(START, 100.0),
        _row(START + dt.timedelta(hours=1), 104.0),
        _row(START + dt.timedelta(minutes=70), 104.0),
        _row(START + dt.timedelta(minutes=80), 110.0),
    ]
    outcome = duration_allocated_cost(
        rows,
        {START: 0.5},
        start=START + dt.timedelta(minutes=15),
        end=START + dt.timedelta(minutes=45),
    )
    assert outcome["measured_kwh"] == 2.0
    assert outcome["actual_cost"] == 1.0

    # Include the unconfirmed 104 -> 110 tail in a fully priced window.
    # Only 45 minutes of the confirmed 4-kWh interval may contribute.
    including_tail = duration_allocated_cost(
        rows,
        {START: 0.5, START + dt.timedelta(hours=1): 2.0},
        start=START + dt.timedelta(minutes=15),
        end=START + dt.timedelta(minutes=90),
    )
    assert including_tail["measured_kwh"] == 3.0
    assert including_tail["priced_kwh"] == 3.0
    assert including_tail["unpriced_kwh"] == 0.0
    assert including_tail["actual_cost"] == 1.5
    assert including_tail["coverage_pct"] == 100.0
    assert including_tail["allocation_status"] == "estimated"
    assert including_tail["tariff_allocation_estimated"] is True
    assert (
        including_tail["allocation_basis"] == "uniform_duration_within_confirmed_counter_intervals"
    )
    assert including_tail["command_attribution_supported"] is False


@pytest.mark.parametrize(
    "price_rows",
    [
        [],
        [(START, 0.1), (START, 0.2)],
        [(START, 0.1), (START + dt.timedelta(hours=2), 0.2)],
        [(START, float("inf"))],
        [(START, True)],
    ],
)
def test_R15_baseline_refuses_incomplete_or_invalid_tariff_horizon(price_rows):
    rows = [
        _row(START, 100.0),
        _row(START + dt.timedelta(minutes=30), 102.0),
        _row(START + dt.timedelta(minutes=45), 102.0),
    ]
    baseline = observed_rate_baseline(rows, price_rows, device_id="a")
    assert baseline["measured_interval_kwh"] == 2.0
    assert baseline["unshifted_baseline_cost"] is None
    assert baseline["baseline_unavailable_reason"] == "incomplete_or_invalid_price_horizon"
    assert baseline["strategy_cost"] is None

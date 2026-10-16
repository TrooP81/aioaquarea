"""Frozen reporting regressions; no database, device, or feed writes."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.plan_outcome import (
    duration_allocated_cost,
    measured_window_outcome,
    observed_rate_baseline,
)
from packages.core.pricing import PriceContext

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 12, 10, tzinfo=UTC)


def rows(*, device="A", all_channels=True):
    return [
        SimpleNamespace(
            ts=START + dt.timedelta(minutes=minute),
            device_id=device,
            heat_kwh=value,
            cool_kwh=value if all_channels else None,
            tank_kwh=value if all_channels else None,
            source_date=START.date(),
            source_timezone="UTC",
        )
        for minute, value in [(0, 10), (60, 12), (61, 12), (120, 14), (121, 14)]
    ]


class Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


@asynccontextmanager
async def session_context(session):
    yield session


@pytest.mark.parametrize("price", [0.0, -0.25, 0.25])
def test_P16a_A3_A5_confirmed_window_deltas_not_daily_maxima(price):
    records = rows() + rows(device="B")
    result = duration_allocated_cost(
        records,
        {START: price, START + dt.timedelta(hours=1): price},
        start=START,
        end=START + dt.timedelta(hours=2),
    )
    assert result["measured_kwh"] == 24
    assert result["total_cost"] == 24 * price
    assert result["complete"] is True
    assert result["telemetry_status"] == "complete"


def test_P16a_A4_adjacent_partial_windows_weight_baseline_and_exclude_end():
    records = rows()
    prices = {
        START - dt.timedelta(hours=1): 999,
        START: 0,
        START + dt.timedelta(hours=1): -0.3,
        START + dt.timedelta(hours=2): 999,
    }
    whole = duration_allocated_cost(records, prices, start=START, end=START + dt.timedelta(hours=2))
    first = duration_allocated_cost(
        records, prices, start=START + dt.timedelta(minutes=30), end=START + dt.timedelta(hours=2)
    )
    lead = duration_allocated_cost(
        records, prices, start=START, end=START + dt.timedelta(minutes=30)
    )
    assert whole["measured_kwh"] == first["measured_kwh"] + lead["measured_kwh"]
    assert whole["actual_cost"] == pytest.approx(first["actual_cost"] + lead["actual_cost"])
    assert first["flat_price_per_kwh"] == -0.2
    assert whole["priced_hours"] == 2
    assert first["complete"] is True


@pytest.mark.parametrize("kind", ["empty", "flat", "pending", "missing_channel", "missing_price"])
def test_P16a_A5_unconfirmed_or_incomplete_is_never_complete_zero(kind):
    records = rows()
    prices = {START: 0.0, START + dt.timedelta(hours=1): -0.1}
    if kind == "empty":
        records = []
    elif kind == "flat":
        for row in records:
            row.heat_kwh = row.cool_kwh = row.tank_kwh = 10
    elif kind == "pending":
        records = records[:2]
    elif kind == "missing_channel":
        records = rows(all_channels=False)
    elif kind == "missing_price":
        prices.pop(START)
    result = duration_allocated_cost(
        records, prices, start=START, end=START + dt.timedelta(hours=2)
    )
    assert result["complete"] is False
    assert result["total_cost"] is None
    if kind in ("empty", "flat", "pending"):
        assert result["coverage_pct"] is None
        assert result["telemetry_status"] == "unavailable"
    if kind == "missing_channel":
        assert result["telemetry_status"] == "incomplete"


@pytest.mark.parametrize("day", [dt.date(2026, 3, 29), dt.date(2026, 10, 25)])
def test_P16a_A3_source_reset_dst_boundary_is_not_a_daily_maximum(day):
    from zoneinfo import ZoneInfo

    boundary = dt.datetime.combine(
        day, dt.time.min, tzinfo=ZoneInfo("Europe/Stockholm")
    ).astimezone(UTC)
    records = [
        SimpleNamespace(
            ts=boundary + dt.timedelta(minutes=minute),
            device_id="A",
            heat_kwh=value,
            cool_kwh=value,
            tank_kwh=value,
            source_date=day - dt.timedelta(days=1) if minute < 0 else day,
            source_timezone="Europe/Stockholm",
        )
        for minute, value in [(-10, 100), (10, 2), (20, 2)]
    ]
    outcome = duration_allocated_cost(
        records,
        {boundary: -0.1},
        start=boundary,
        end=boundary + dt.timedelta(minutes=5),
        timezone_name="Europe/Stockholm",
    )
    assert outcome["measured_kwh"] == 3
    assert outcome["actual_cost"] == -0.3


async def test_P16a_A3_A4_A6_stats_measured_allocator_agree_and_queries_are_scoped(monkeypatch):
    from packages.api.routers import dashboard

    end = START + dt.timedelta(days=1)
    records = rows()
    prices = [(START, 0.0), (START + dt.timedelta(hours=1), -0.25)]

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return end

    monkeypatch.setattr(
        dashboard,
        "dt",
        SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
    )
    monkeypatch.setattr(
        dashboard,
        "get_active_price_context",
        AsyncMock(return_value=PriceContext("tibber", "SEK", "tibber")),
    )
    monkeypatch.setattr(dashboard, "get_user_tz", AsyncMock(return_value="UTC"))
    session = SimpleNamespace(execute=AsyncMock(side_effect=[Result(records), Result(prices)]))
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    stats = await dashboard.get_stats("day")
    expected = duration_allocated_cost(records, dict(prices), start=START, end=end)
    assert stats.total_kwh == expected["measured_kwh"] == 12
    assert stats.partial_priced_amount == expected["actual_cost"] == -1.5
    assert stats.total_cost_eur is None and stats.complete is False
    assert (stats.price_area, stats.price_source, stats.price_currency) == (
        "tibber",
        "tibber",
        "SEK",
    )
    query = session.execute.call_args_list[1].args[0]
    params = query.compile().params
    assert params == {
        "ts_1": START,
        "ts_2": end,
        "area_1": "tibber",
        "price_source_1": "tibber",
        "price_currency_1": "SEK",
    }
    assert "prices.ts <" in str(query) and "prices.ts <=" not in str(query)
    measured_session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[Result(records), Result([]), Result([]), Result([]), Result(prices)]
        )
    )
    measured = await measured_window_outcome(
        measured_session,
        start=START,
        end=end,
        price_area="tibber",
        price_currency="SEK",
        price_source="tibber",
        comfort_min_c=19,
        comfort_max_c=22,
    )
    assert measured["cost"] == expected
    meter_bounds = measured_session.execute.call_args_list[0].args[0].compile().params
    assert meter_bounds["ts_1"] < START and meter_bounds["ts_2"] > end


@pytest.mark.parametrize("layer", ["rules", "milp"])
async def test_P16a_A7_frozen_planner_prices_and_baseline_are_reporting_independent(
    monkeypatch, layer
):
    from packages.optimizer import data_access
    from packages.optimizer.rules_engine import RulesOptimizer
    from packages.optimizer.milp import MILPOptimizer
    from tests.test_planning_input_batch import _batch

    batch = _batch()
    session = SimpleNamespace(info={"planning_input_batch": batch})
    monkeypatch.setattr(
        data_access,
        "load_planning_input_batch",
        AsyncMock(side_effect=AssertionError("frozen inputs must not reload")),
    )
    optimizer = RulesOptimizer() if layer == "rules" else MILPOptimizer()
    expected = [(row.ts, row.value) for row in batch.prices]
    before = await optimizer._get_prices(session, batch.start, batch.end)
    records = rows(all_channels=False)
    original = deepcopy(records)
    baseline = observed_rate_baseline(records, expected, device_id="A")
    duration_allocated_cost(
        records,
        dict(expected),
        start=START + dt.timedelta(minutes=30),
        end=START + dt.timedelta(hours=1),
    )
    after = await optimizer._get_prices(session, batch.start, batch.end)
    assert before == after == expected
    assert records == original
    assert observed_rate_baseline(records, expected, device_id="A") == baseline
    assert baseline["measured_interval_kwh"] == 4
    assert baseline["measured_interval_hours"] == 2
    assert baseline["unshifted_baseline_cost"] == pytest.approx(-4.8)
    assert baseline["strategy_cost"] is None


async def test_P16a_A6_dashboard_uses_identical_confirmed_allocation(monkeypatch):
    from packages.api.routers import dashboard
    from packages.core.space_heating_gate import HeatingGateConfig

    end = START + dt.timedelta(hours=2)
    start = START.replace(hour=0)
    records = rows()
    prices = {START: 0.0, START + dt.timedelta(hours=1): -0.25}

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return end

    monkeypatch.setattr(
        dashboard,
        "dt",
        SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
    )
    monkeypatch.setattr(dashboard, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(dashboard, "get_price_area", AsyncMock(return_value="tibber"))
    monkeypatch.setattr(
        dashboard, "get_device_data_quality", AsyncMock(return_value={"threshold_seconds": 900})
    )
    monkeypatch.setattr(
        dashboard, "get_space_heating_gate_config", AsyncMock(return_value=HeatingGateConfig())
    )
    monkeypatch.setattr(dashboard, "get_controlling_override", AsyncMock(return_value=None))
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: None),
                SimpleNamespace(one_or_none=lambda: (-0.25, "SEK", "tibber")),
                Result([]),
                Result(records),
                Result(
                    [(timestamp, price, "SEK", "tibber") for timestamp, price in prices.items()]
                ),
                SimpleNamespace(scalar_one_or_none=lambda: None),
            ]
        )
    )
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    response = await dashboard.get_dashboard()
    expected = duration_allocated_cost(records, prices, start=start, end=end)
    assert response.today_kwh == expected["measured_kwh"] == 12
    assert response.today_cost_priced_amount == expected["partial_priced_amount"] == -1.5
    assert response.today_cost_priced_kwh == expected["priced_kwh"]
    assert response.today_cost_unpriced_kwh == expected["unpriced_kwh"]
    assert response.today_telemetry_status == "incomplete"
    assert response.today_cost_complete is False
    assert response.today_cost_eur is None
    assert response.today_cost_currency == "SEK"

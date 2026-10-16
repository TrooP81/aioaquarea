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
    assert result["incomplete_reason"] is None
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
    assert result["incomplete_reason"] == (
        "telemetry_unavailable"
        if kind in ("empty", "flat", "pending")
        else "tariff_incomplete"
        if kind == "missing_price"
        else "telemetry_incomplete"
    )
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
        "area_2": "tb:unavailable",
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
    monkeypatch.setattr(
        dashboard, "get_active_price_context", AsyncMock(return_value=PriceContext("tibber", "SEK", "tibber"))
    )
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


@pytest.mark.parametrize("route", ["dashboard", "stats"])
async def test_owned_pg_seeded_energy_can_be_fully_priced_but_telemetry_incomplete(
    monkeypatch, route
):
    """Mirror tests/e2e/conftest.py seeds at the CI failure's 21:33 UTC clock."""
    from packages.api.routers import dashboard
    from packages.core.models import ConsumptionRecord, PriceRecord
    from packages.core.space_heating_gate import HeatingGateConfig

    now = dt.datetime(2026, 10, 16, 21, 33, tzinfo=UTC)
    consumption = [
        ConsumptionRecord(
            ts=now - dt.timedelta(hours=h),
            device_id="test-device-001",
            heat_kwh=1.7 - h * 0.1,
            cool_kwh=0.0,
            tank_kwh=0.5,
            outdoor_temp=5.0 + h,
        )
        for h in range(6)
    ]
    hourly = [
        0.05,
        0.04,
        0.03,
        0.03,
        0.04,
        0.06,
        0.08,
        0.12,
        0.15,
        0.18,
        0.20,
        0.22,
        0.20,
        0.18,
        0.15,
        0.14,
        0.16,
        0.25,
        0.30,
        0.28,
        0.20,
        0.12,
        0.08,
        0.06,
    ]
    # SQLAlchemy INSERT defaults from the E2E PriceRecord seeds.
    prices = [
        PriceRecord(
            ts=now.replace(minute=0) - dt.timedelta(hours=12) + dt.timedelta(hours=h),
            area="10Y1001A1001A46L",
            price_eur_per_kwh=value,
            price_currency="EUR",
            price_source="legacy",
        )
        for h, value in enumerate(hourly)
    ]
    assert PriceRecord.__table__.c.price_currency.default.arg == "EUR"
    assert PriceRecord.__table__.c.price_source.default.arg == "legacy"
    historical = [row for row in prices if row.ts < now]

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(
        dashboard, "dt", SimpleNamespace(datetime=Clock, timezone=UTC, timedelta=dt.timedelta)
    )
    monkeypatch.setattr(dashboard, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(
        dashboard,
        "get_active_price_context",
        AsyncMock(return_value=PriceContext("10Y1001A1001A46L", "EUR", "legacy")),
    )
    if route == "stats":
        results = [
            Result(consumption),
            Result([(row.ts, row.price_eur_per_kwh) for row in historical]),
        ]
    else:
        monkeypatch.setattr(
            dashboard, "get_device_data_quality", AsyncMock(return_value={"threshold_seconds": 900})
        )
        monkeypatch.setattr(
            dashboard, "get_space_heating_gate_config", AsyncMock(return_value=HeatingGateConfig())
        )
        monkeypatch.setattr(dashboard, "get_controlling_override", AsyncMock(return_value=None))
        results = [
            SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(one_or_none=lambda: (0.20, "EUR", "legacy")),
            Result([]),
            Result(consumption),
            Result(
                [
                    (row.ts, row.price_eur_per_kwh, row.price_currency, row.price_source)
                    for row in historical
                ]
            ),
            SimpleNamespace(scalar_one_or_none=lambda: None),
        ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    response = (
        await dashboard.get_stats("day") if route == "stats" else await dashboard.get_dashboard()
    )
    data = response.model_dump()
    prefix = "" if route == "stats" else "today_cost_"
    assert data["total_kwh" if route == "stats" else "today_kwh"] == 0.4
    assert data[prefix + "priced_kwh"] == 0.4
    assert data[prefix + "unpriced_kwh"] == 0
    assert data[
        "partial_priced_amount" if route == "stats" else "today_cost_priced_amount"
    ] == pytest.approx(0.0705, abs=0.0005)
    assert (
        data["telemetry_status" if route == "stats" else "today_telemetry_status"] == "incomplete"
    )
    assert data["total_cost_eur" if route == "stats" else "today_cost_eur"] is None
    assert (
        data["incomplete_reason" if route == "stats" else "today_cost_incomplete_reason"]
        == "telemetry_incomplete"
    )
    if route == "stats":
        params = session.execute.call_args_list[1].args[0].compile().params
        assert params["area_1"] == "10Y1001A1001A46L"
        assert params["price_source_1"] == "legacy"
        assert params["price_currency_1"] == "EUR"

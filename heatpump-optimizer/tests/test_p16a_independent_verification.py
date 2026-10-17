"""Independent P1.6a acceptance edges, including reporting disagreement repros."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from packages.core.plan_outcome import duration_allocated_cost
from tests.test_p16a_reporting import START, UTC, Result, rows, session_context


@pytest.mark.parametrize("day", [dt.date(2026, 3, 29), dt.date(2026, 10, 25)])
def test_stockholm_clock_transition_keeps_elapsed_hours_and_adjacent_energy(day):
    zone = ZoneInfo("Europe/Stockholm")
    start = dt.datetime.combine(day, dt.time(0), tzinfo=UTC)
    end = start + dt.timedelta(hours=3)
    records = [
        SimpleNamespace(
            ts=start + dt.timedelta(minutes=minute),
            device_id="A",
            heat_kwh=value,
            cool_kwh=value,
            tank_kwh=value,
            source_date=day,
            source_timezone="Europe/Stockholm",
        )
        for minute, value in [
            (0, 10),
            (60, 12),
            (61, 12),
            (120, 14),
            (121, 14),
            (180, 16),
            (181, 16),
        ]
    ]
    prices = {start + dt.timedelta(hours=i): value for i, value in enumerate([0, -0.1, 0.2])}
    local_hours = [(start + dt.timedelta(hours=i)).astimezone(zone).hour for i in range(3)]
    assert local_hours == ([1, 3, 4] if day.month == 3 else [2, 2, 3])
    whole = duration_allocated_cost(records, prices, start=start, end=end, timezone_name=zone.key)
    split = start + dt.timedelta(minutes=90)
    pieces = [
        duration_allocated_cost(records, prices, start=left, end=right, timezone_name=zone.key)
        for left, right in [(start, split), (split, end)]
    ]
    assert whole["measured_kwh"] == 18
    assert whole["total_cost"] == pytest.approx(0.6)
    assert whole["complete"] is True
    assert sum(piece["measured_kwh"] for piece in pieces) == whole["measured_kwh"]
    assert sum(piece["total_cost"] for piece in pieces) == pytest.approx(whole["total_cost"])


@pytest.mark.parametrize("counter", [0.0, None])
def test_flat_zero_counters_and_missing_telemetry_do_not_certify_zero_use(counter):
    records = rows()
    for record in records:
        record.heat_kwh = record.cool_kwh = record.tank_kwh = counter
    result = duration_allocated_cost(
        records,
        {START: 0.0, START + dt.timedelta(hours=1): -0.25},
        start=START,
        end=START + dt.timedelta(hours=2),
    )
    assert result["measured_kwh"] == 0
    assert result["telemetry_status"] == "unavailable"
    assert result["complete"] is False
    assert result["total_cost"] is None
    assert result["partial_priced_amount"] is None
    assert result["coverage_pct"] is None


@pytest.mark.parametrize("invalid", [None, True, float("nan"), float("inf")])
def test_invalid_tariffs_are_unpriced_not_fallback_cost(invalid):
    result = duration_allocated_cost(
        rows(),
        {START: invalid, START + dt.timedelta(hours=1): -0.25},
        start=START,
        end=START + dt.timedelta(hours=2),
    )
    assert result["measured_kwh"] == 12
    assert result["priced_kwh"] == result["unpriced_kwh"] == 6
    assert result["partial_priced_amount"] == -1.5
    assert result["total_cost"] is None
    assert result["coverage_pct"] == 50


async def test_dashboard_retains_historical_tariffs_when_current_hour_is_missing(monkeypatch):
    """A missing latest tariff must not erase known earlier priced energy."""
    from packages.api.routers import dashboard
    from packages.core.pricing import PriceContext
    from packages.core.space_heating_gate import HeatingGateConfig

    end = START + dt.timedelta(hours=2)

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
        dashboard,
        "get_active_price_context",
        AsyncMock(return_value=PriceContext("tibber", "SEK", "tibber")),
    )
    monkeypatch.setattr(
        dashboard, "get_device_data_quality", AsyncMock(return_value={"threshold_seconds": 900})
    )
    monkeypatch.setattr(
        dashboard, "get_space_heating_gate_config", AsyncMock(return_value=HeatingGateConfig())
    )
    monkeypatch.setattr(dashboard, "get_controlling_override", AsyncMock(return_value=None))
    records = rows()
    prices = {START: 0.0, START + dt.timedelta(hours=1): -0.25}
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: None),
                SimpleNamespace(one_or_none=lambda: None),
                Result([]),
                Result(records),
                Result([(ts, price, "SEK", "tibber") for ts, price in prices.items()]),
                SimpleNamespace(scalar_one_or_none=lambda: None),
            ]
        )
    )
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    response = await dashboard.get_dashboard()
    expected = duration_allocated_cost(records, prices, start=START.replace(hour=0), end=end)
    assert response.current_price is None
    assert response.today_cost_priced_amount == expected["partial_priced_amount"] == -1.5
    assert response.today_cost_priced_kwh == expected["priced_kwh"] == 12
    assert response.today_cost_currency == "SEK"

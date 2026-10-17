"""Independent positive-path and realistic inactive-channel reporting checks."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.plan_outcome import duration_allocated_cost
from packages.core.pricing import PriceContext
from tests.test_p16a_reporting import Result, session_context

UTC = dt.timezone.utc
END = dt.datetime(2026, 10, 13, tzinfo=UTC)
START = END - dt.timedelta(days=1)
AREA = "10Y1001A1001A46L"


def complete_reporting_rows():
    """Synthetic fully confirmed source-day snapshot, including closing evidence.

    Keep the previous source day's closing snapshot at midnight and its confirming
    reread; do not accidentally replace it with a new-day zero reset. All three
    channels deliberately advance: this proves reachability, not normal operation.
    """
    return [
        SimpleNamespace(
            ts=START + dt.timedelta(minutes=minute),
            device_id="complete-reporting-device",
            heat_kwh=value,
            cool_kwh=value,
            tank_kwh=value,
            source_date=START.date(),
            source_timezone="UTC",
        )
        for hour in range(25)
        for minute, value in [(hour * 60, float(hour)), (hour * 60 + 1, float(hour))]
    ]


@pytest.mark.parametrize("inactive", [None, "cool_kwh", "tank_kwh"])
def test_fresh_whole_day_inactive_channel_cannot_certify_total_under_current_contract(inactive):
    # Same-day 23-hour window isolates the flat-channel rule from midnight resets,
    # partial-window gaps and unconfirmed tails; every positive delta is confirmed.
    end = END - dt.timedelta(hours=1)
    records = complete_reporting_rows()
    if inactive:
        for row in records:
            setattr(row, inactive, 0.0)
    prices = {START + dt.timedelta(hours=h): 0.1 for h in range(24)}
    result = duration_allocated_cost(records, prices, start=START, end=end)
    assert result["coverage_pct"] == 100
    assert result["unpriced_kwh"] == 0
    assert result["measured_kwh"] == (46 if inactive else 69)
    assert result["partial_priced_amount"] == (4.6 if inactive else 6.9)
    assert result["complete"] is (inactive is None)
    assert result["total_cost"] == (None if inactive else 6.9)
    assert result["incomplete_reason"] == ("telemetry_incomplete" if inactive else None)


@pytest.mark.parametrize("route", ["dashboard", "stats"])
async def test_complete_seed_has_positive_certified_cost_through_both_routes(monkeypatch, route):
    from packages.api.routers import dashboard
    from packages.core.space_heating_gate import HeatingGateConfig

    # Dashboard covers the just-finished source day until 23:00. Stats covers the
    # complete preceding 24h ending at midnight, with closing confirmation retained.
    now = END if route == "stats" else END - dt.timedelta(hours=1)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(
        dashboard,
        "dt",
        SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
    )
    monkeypatch.setattr(dashboard, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(
        dashboard,
        "get_active_price_context",
        AsyncMock(return_value=PriceContext(AREA, "EUR", "legacy")),
    )
    prices = [(START + dt.timedelta(hours=h), 0.1) for h in range(24)]
    if route == "stats":
        results = [Result(complete_reporting_rows()), Result(prices)]
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
            SimpleNamespace(one_or_none=lambda: (0.1, "EUR", "legacy")),
            Result([]),
            Result(complete_reporting_rows()),
            Result([(ts, price, "EUR", "legacy") for ts, price in prices]),
            SimpleNamespace(scalar_one_or_none=lambda: None),
        ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    response = (
        await dashboard.get_stats("day") if route == "stats" else await dashboard.get_dashboard()
    )
    data = response.model_dump()
    assert data["complete" if route == "stats" else "today_cost_complete"] is True
    assert data["total_cost_eur" if route == "stats" else "today_cost_eur"] == (
        7.2 if route == "stats" else 6.9
    )
    assert data["telemetry_status" if route == "stats" else "today_telemetry_status"] == "complete"
    assert data["incomplete_reason" if route == "stats" else "today_cost_incomplete_reason"] is None


def test_live_window_without_future_confirmation_stays_incomplete_even_all_channels_active():
    end = END - dt.timedelta(hours=1)
    records = [row for row in complete_reporting_rows() if row.ts <= end]
    prices = {START + dt.timedelta(hours=h): 0.1 for h in range(24)}
    result = duration_allocated_cost(records, prices, start=START, end=end)
    assert result["measured_kwh"] == 66
    assert result["coverage_pct"] == 100
    assert result["unpriced_kwh"] == 0
    assert result["partial_priced_amount"] == 6.6
    assert result["total_cost"] is None
    assert result["complete"] is False
    assert result["incomplete_reason"] == "telemetry_incomplete"

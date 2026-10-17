"""Positive certified-cost counterpart to the intentionally incomplete E2E seeds."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import ConsumptionRecord, PriceRecord
from tests.test_p16a_completeness_verification import AREA, END, START, complete_reporting_rows


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("route", ["dashboard", "stats"])
async def test_fully_confirmed_seed_retains_non_null_cost(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, route
):
    from packages.api.routers import dashboard

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
    # All channel evidence is positive and confirmed, including the final source
    # day's closing snapshot; the shared six-hour fixture is intentionally partial.
    for row in complete_reporting_rows():
        db_session.add(ConsumptionRecord(**vars(row)))
    for hour in range(24):
        db_session.add(
            PriceRecord(
                ts=START + dt.timedelta(hours=hour),
                area=AREA,
                price_eur_per_kwh=0.1,
                price_currency="EUR",
                price_source="legacy",
            )
        )
    await db_session.commit()
    response = await client.get("/api/stats?period=day" if route == "stats" else "/api/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert data["complete" if route == "stats" else "today_cost_complete"] is True
    assert data["total_cost_eur" if route == "stats" else "today_cost_eur"] == (
        7.2 if route == "stats" else 6.9
    )
    assert data["total_kwh" if route == "stats" else "today_kwh"] == (
        72 if route == "stats" else 69
    )
    assert data["telemetry_status" if route == "stats" else "today_telemetry_status"] == "complete"
    assert data["incomplete_reason" if route == "stats" else "today_cost_incomplete_reason"] is None

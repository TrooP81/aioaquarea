"""Explicit-only forecast repairs on a guarded, uniquely owned Compose PostgreSQL.

Transactions are committed on separate connections; no shared-connection savepoints.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.owned_p21_contract_postgresql import _owned_database_environment

CASES = itertools.count()
UTC = dt.timezone.utc


@pytest_asyncio.fixture(loop_scope="function")
async def owned(monkeypatch):
    _, url, address = _owned_database_environment()
    from packages.api.routers import models_router as routes
    from packages.core import database, settings_service
    from packages.core.models import DeviceStatusRecord, IndoorTempReading
    from packages.ml.thermal import ThermalModel

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT current_database(),current_user,host(inet_server_addr()),inet_server_port()"
                    )
                )
            ).one() == ("hpo_p21diag", "hpo_p21diag", address, 5432)
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "039"
        # Each case owns a distinct forecast horizon; immutable history is retained.
        actual_now = dt.datetime.now(UTC)
        now = actual_now + dt.timedelta(hours=next(CASES) * 4)

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        monkeypatch.setattr(
            routes,
            "dt",
            SimpleNamespace(datetime=Clock, timedelta=dt.timedelta, timezone=dt.timezone),
        )
        await settings_service.set_settings_bulk(
            {
                "smartthings_device_ids": "room-a,room-b",
                "comfort_reference_sensor_id": "room-a",
                "weather_provider": "smhi",
                "timezone": "UTC",
                "comfort_schedule": '{"weekday":[],"weekend":[]}',
                "comfort_temp_min": "18",
                "comfort_temp_target": "20",
                "comfort_temp_max": "25",
            }
        )
        async with database.get_session() as session:
            session.add(
                DeviceStatusRecord(ts=now, device_id="forecast-test", outdoor_temp=5, zone1_temp=35)
            )
            session.add_all(
                [
                    IndoorTempReading(
                        timestamp=actual_now, device_id=sensor, temperature=temp, is_stale=False
                    )
                    for sensor, temp in (("room-a", 20), ("room-b", 24))
                ]
            )
        model = ThermalModel()
        model.arefresh_if_changed = AsyncMock(return_value=False)
        model.dependency_context_is_current = AsyncMock(return_value=True)
        monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
        monkeypatch.setattr(
            routes,
            "_read_live_forecast_quality",
            AsyncMock(
                return_value={"control_allowed": False, "reason": "insufficient_scored_forecasts"}
            ),
        )
        monkeypatch.setattr(
            routes, "_saved_plan_model_dependencies_are_current", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(routes, "get_price_area", AsyncMock(return_value="SE3"))
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://owned.invalid"
        ) as client:
            yield database.get_session, client, now
    finally:
        await engine.dispose()


async def store_plan(transaction, now, forecast, *, corrupt=False):
    from packages.core.house_contract import ensure_current_contract, snapshot_from_revision
    from packages.core.models import PlanRecord

    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(hours=3)
    async with transaction() as session:
        revision = await ensure_current_contract(session)
        authority = await snapshot_from_revision(session, revision.id, start, end)
        if corrupt:
            authority["schedule_projection"][0]["target_c"] += 1
        plan = PlanRecord(
            created_at=now,
            horizon_start=start,
            horizon_end=end,
            optimizer_version="rules_v8",
            price_source="manual",
            price_currency="EUR",
            status="diagnostic",
            contract_state="current",
            contract_revision_id=revision.id,
            contract_hash=revision.contract_hash,
            input_provenance_json="{}",
            plan_json=json.dumps(
                {
                    "house_contract_snapshot": authority,
                    "forecast_snapshot": {**forecast, "version": "indoor_forecast_v5"},
                }
            ),
        )
        session.add(plan)


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_provider_conflicts_and_rejected_plan_use_only_selected_provider(
    owned, missing, reverse
):
    from packages.core.models import WeatherRecord

    transaction, client, now = owned
    start = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    selected = (
        [
            WeatherRecord(
                ts=start + dt.timedelta(hours=i),
                source="smhi",
                temperature=-10,
                wind_speed=2,
                irradiance=0,
                precipitation=0,
            )
            for i in range(2)
        ]
        if not missing
        else []
    )
    conflict = [
        WeatherRecord(
            ts=start + dt.timedelta(hours=i),
            source="open-meteo",
            temperature=40,
            wind_speed=2,
            irradiance=0,
            precipitation=0,
        )
        for i in range(2)
    ]
    async with transaction() as session:
        session.add_all(selected + conflict if not reverse else conflict + selected)
    baseline = (await client.get("/api/thermal/indoor-forecast?hours=2")).json()
    assert baseline["forecast_source"] == "live_estimate"
    assert baseline["weather_forecast"][0]["outdoor_temp"] == (5 if missing else -10)
    assert baseline["weather_forecast"][0]["input_status"] == ("imputed" if missing else "observed")
    assert len(baseline["forecast_no_heating"]) == 2
    await store_plan(transaction, now, baseline, corrupt=True)
    response = await client.get("/api/thermal/indoor-forecast?hours=2")
    assert response.status_code == 200
    fallback = response.json()
    assert fallback["forecast_source"] == "live_estimate"
    assert fallback["weather_forecast"] == baseline["weather_forecast"]
    assert fallback["forecast_no_heating"] == baseline["forecast_no_heating"]
    assert fallback["comfort_assessment"] == baseline["comfort_assessment"]
    assert fallback["ordinary_writes_allowed"] is False


@pytest.mark.parametrize("key", ["comfort_reference_sensor_id", "smartthings_device_ids"])
@pytest.mark.parametrize("change", ["sensor", "aba", "same"])
@pytest.mark.parametrize("saved", [False, True])
async def test_committed_sensor_change_fences_held_default_forecast(
    owned, monkeypatch, key, change, saved
):
    from packages.core import control_temperature, settings_service
    from packages.core.models import HouseInstallationRecord

    transaction, client, now = owned
    if saved:
        baseline = (await client.get("/api/thermal/indoor-forecast?hours=2")).json()
        await store_plan(transaction, now, baseline)
        assert (await client.get("/api/thermal/indoor-forecast?hours=2")).json()[
            "forecast_source"
        ] == "diagnostic_plan"
    async with transaction() as session:
        before = (await session.get(HouseInstallationRecord, 1)).indoor_dependency_generation
    held, release = asyncio.Event(), asyncio.Event()
    original = control_temperature.get_control_temperature

    async def read_then_hold(**kwargs):
        captured = await original(**kwargs)
        assert captured.value == 20
        held.set()
        await asyncio.wait_for(release.wait(), 10)
        return captured

    monkeypatch.setattr(control_temperature, "get_control_temperature", read_then_hold)
    task = asyncio.create_task(client.get("/api/thermal/indoor-forecast?hours=2"))
    try:
        await asyncio.wait_for(held.wait(), 10)
        old = "room-a" if key == "comfort_reference_sensor_id" else "room-a,room-b"
        await asyncio.wait_for(
            settings_service.set_setting(key, old if change == "same" else "room-b"), 10
        )
        if change == "aba":
            await asyncio.wait_for(settings_service.set_setting(key, old), 10)
        # This connection observes committed writes, not the held reader's identity map.
        async with transaction() as session:
            after = (await session.get(HouseInstallationRecord, 1)).indoor_dependency_generation
        assert after == before + {"sensor": 1, "aba": 2, "same": 0}[change]
        release.set()
        response = await asyncio.wait_for(task, 10)
        assert response.status_code == 200
        payload = response.json()
        if change == "same":
            assert payload["forecast_source"] == ("diagnostic_plan" if saved else "live_estimate")
        else:
            assert (
                payload["forecast_unavailable_reason"]
                == "indoor_dependency_changed_during_forecast"
            )
            assert payload["current_indoor"] is None
            assert payload["forecast"] == payload["weather_forecast"] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

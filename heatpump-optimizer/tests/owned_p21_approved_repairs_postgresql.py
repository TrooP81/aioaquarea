"""Explicit-only AC1/AC4 acceptance on a uniquely owned Compose database."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.owned_p21_contract_postgresql import _owned_database_environment


@pytest_asyncio.fixture(loop_scope="function")
async def fresh(monkeypatch):
    _, url, _ = _owned_database_environment()
    from packages.core import database

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        # Each case starts with the freshly migrated, empty schema. Savepoints
        # allow real API/service commits without deleting immutable history.
        async with engine.connect() as connection:
            outer = await connection.begin()
            try:
                assert await connection.scalar(text("SELECT current_database()")) == "hpo_p21diag"
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "039"
                )
                assert (
                    await connection.scalar(text("SELECT count(*) FROM house_installations")) == 0
                )
                factory = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                monkeypatch.setattr(database, "async_session_factory", factory)
                yield database.get_session
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("value", ["room-a", ""])
@pytest.mark.parametrize("entry", ["direct", "bulk", "api"])
@pytest.mark.parametrize("key", ["smartthings_device_ids", "comfort_reference_sensor_id"])
async def test_AC4_first_sensor_write_initializes_singleton_atomically(fresh, entry, key, value):
    from packages.api.routers import settings as routes
    from packages.core import settings_service
    from packages.core.house_contract import ensure_current_contract
    from packages.core.models import (
        HouseInstallationRecord,
        HouseContractRevisionRecord,
        SettingRecord,
    )

    if entry == "direct":
        await settings_service.set_setting(key, value)
    elif entry == "bulk":
        await settings_service.set_settings_bulk({key: value})
    else:
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://owned.invalid"
        ) as client:
            response = await client.put("/api/settings", json={"settings": {key: value}})
        assert response.status_code == 200, response.text
        assert int(response.headers["X-Contract-Revision"]) > 0
    async with fresh() as session:
        revision = await ensure_current_contract(session)
        installation = await session.get(HouseInstallationRecord, 1)
        assert installation.current_revision_id == revision.id
        assert installation.indoor_dependency_generation == 1 + bool(value)
        assert installation.demand_dependency_generation == 1
        assert (await session.get(SettingRecord, key)).value == value
        assert await session.scalar(select(func.count()).select_from(HouseInstallationRecord)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(HouseContractRevisionRecord)) == 1
        )
    await settings_service.set_setting(key, value)
    async with fresh() as session:
        assert (
            await session.get(HouseInstallationRecord, 1)
        ).indoor_dependency_generation == 1 + bool(value)


async def test_AC4_first_write_failure_rolls_back_initialization_and_setting(fresh, monkeypatch):
    from packages.core import settings_service
    from packages.core.models import (
        HouseInstallationRecord,
        HouseContractRevisionRecord,
        SettingRecord,
    )
    from sqlalchemy.dialects.postgresql import Insert

    with pytest.raises(RuntimeError, match="injected write failure"):
        async with fresh() as session:
            original = session.execute

            async def failing(statement, *args, **kwargs):
                if isinstance(statement, Insert) and statement.table.name == "settings":
                    # Initialization really occurred before the failing write.
                    assert await session.get(HouseInstallationRecord, 1) is not None
                    raise RuntimeError("injected write failure")
                return await original(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", failing)
            await settings_service.set_settings_bulk(
                {"smartthings_device_ids": "room-a"}, session=session
            )
    async with fresh() as session:
        assert await session.get(HouseInstallationRecord, 1) is None
        assert (
            await session.scalar(select(func.count()).select_from(HouseContractRevisionRecord)) == 0
        )
        assert await session.get(SettingRecord, "smartthings_device_ids") is None


async def test_AC1_provider_aba_fences_indoor_and_saved_forecasts_not_tank(fresh):
    from packages.api.routers.models_router import _saved_plan_model_dependencies_are_current
    from packages.core.models import HouseInstallationRecord
    from packages.core.settings_service import set_setting, set_settings_bulk
    from packages.ml.forecast_quality import native_scope
    from packages.ml.lifecycle import Lifecycle
    from tests.test_forecast_quality import TestProspectiveNativeEvidence

    await set_setting("smartthings_device_ids", "room-a")
    await Lifecycle().initialize()
    async with fresh() as session:
        installation = await session.get(HouseInstallationRecord, 1)
        indoor_before = installation.indoor_dependency_generation
        demand_before = installation.demand_dependency_generation
        installation_id = str(installation.installation_id)
    plan = SimpleNamespace(
        input_provenance_json=json.dumps(
            {
                "indoor_dependency_generation": indoor_before,
                "models": {
                    "native_dependencies": [
                        {"component": c, "artifact_digest": None} for c in ("comfort", "thermal")
                    ]
                },
            }
        )
    )
    assert await _saved_plan_model_dependencies_are_current(plan)
    base = TestProspectiveNativeEvidence._inputs()
    base.update(
        installation_id=installation_id,
        indoor_dependency_generation=indoor_before,
        capabilities=("indoor_heating",),
    )
    indoor = {**base, "schemas": {**base["schemas"], "artifact": "thermal_v5"}}
    comfort = {**base, "schemas": {**base["schemas"], "artifact": "comfort_v8"}}
    tank = {**indoor, "capabilities": ("tank_heating",)}
    await set_setting("weather_provider", "smhi")
    assert not await _saved_plan_model_dependencies_are_current(plan)
    await set_settings_bulk({"weather_provider": "open-meteo"})
    assert not await _saved_plan_model_dependencies_are_current(plan)
    async with fresh() as session:
        installation = await session.get(HouseInstallationRecord, 1)
        assert installation.indoor_dependency_generation == indoor_before + 2
        assert installation.demand_dependency_generation == demand_before + 2
        current_generation = installation.indoor_dependency_generation
    for affected in (indoor, comfort):
        assert native_scope(affected) != native_scope(
            {**affected, "indoor_dependency_generation": current_generation}
        )
    assert native_scope(tank) == native_scope(
        {**tank, "indoor_dependency_generation": current_generation}
    )
    await set_settings_bulk({"weather_provider": "open-meteo", "comfort_temp_target": "21"})
    async with fresh() as session:
        assert (
            await session.get(HouseInstallationRecord, 1)
        ).indoor_dependency_generation == current_generation


async def test_AC2_real_cache_context_tracks_contract_generations_and_assessment_settings(fresh):
    from packages.api.routers.dashboard import _comfort_assessment_context
    from packages.core.settings_service import set_setting, set_settings_bulk

    initial = await _comfort_assessment_context()
    await set_setting("weather_provider", "smhi")
    changed = await _comfort_assessment_context()
    assert changed[0:2] == initial[0:2]
    assert changed[2:4] == (initial[2] + 1, initial[3] + 1)
    await set_setting("weather_provider", "open-meteo")
    restored = await _comfort_assessment_context()
    assert restored != initial and restored[4] == initial[4]
    await set_settings_bulk({"comfort_temp_target": "21"})
    target = await _comfort_assessment_context()
    assert target[0:2] != restored[0:2]
    await set_setting("heat_curve_supply_cold_c", "50")
    assert await _comfort_assessment_context() != target

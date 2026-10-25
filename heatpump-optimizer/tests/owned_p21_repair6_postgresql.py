"""Explicit acceptance against the guarded, disposable P2.1 PostgreSQL only."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import pickle
import platform
import threading
from uuid import uuid4

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.owned_p21_contract_postgresql import _owned_database_environment

UTC = dt.timezone.utc


@pytest_asyncio.fixture
async def owned(monkeypatch):
    _, url, _ = _owned_database_environment()
    from packages.core import database
    from packages.core.house_contract import revise_contract
    from packages.core.models import SettingRecord
    from tests.planner_orm_support import planning_rows

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT current_database()")) == "hpo_p21diag"
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "039"
        await connection.execute(
            text(
                "TRUNCATE settings,plans,"
                "plan_actions,optimization_requests,device_status,space_heating_gate,"
                "indoor_temp_reading,prices,weather,ml_lifecycle_state,ml_artifacts,"
                "ml_forecast_issues,ml_forecast_outcomes,ml_maintenance_requests RESTART IDENTITY CASCADE"
            )
        )
    rows = planning_rows()
    values = {
        "comfort_temp_min": "20.05",
        "comfort_temp_target": "20.55",
        "comfort_temp_max": "25",
        "comfort_schedule": '{"weekday":[],"weekend":[]}',
    }
    present = {row.key for row in rows if isinstance(row, SettingRecord)}
    for row in rows:
        if isinstance(row, SettingRecord) and row.key in values:
            row.value = values[row.key]
    rows.extend(SettingRecord(key=k, value=v) for k, v in values.items() if k not in present)
    async with database.get_session() as session:
        session.add_all(rows)
        await session.flush()
        from packages.core.settings_service import get_all_settings

        await revise_contract(
            session,
            await get_all_settings(session=session),
            source="owned_repair6_fixture",
            changed_keys=sorted(values),
        )
    try:
        yield engine, database.get_session
    finally:
        await engine.dispose()


async def wait_for_database_lock(engine):
    async def observe():
        while True:
            async with engine.connect() as connection:
                if await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock' "
                        "AND pid<>pg_backend_pid()"
                    )
                ):
                    return

    await asyncio.wait_for(observe(), 10)


async def generation(transaction):
    from packages.core.models import HouseInstallationRecord

    async with transaction() as session:
        return (await session.get(HouseInstallationRecord, 1)).demand_dependency_generation


@pytest.mark.parametrize("first_writer", ["direct", "bulk"])
async def test_mixed_provider_writers_serialize_and_advance_aba_generation(
    owned, monkeypatch, first_writer
):
    from packages.core import house_contract, settings_service

    engine, transaction = owned
    original = house_contract._lock_contract
    held, release = asyncio.Event(), asyncio.Event()

    async def controlled(session):
        await original(session)
        if asyncio.current_task().get_name() == "repair6-first":
            held.set()
            await release.wait()

    monkeypatch.setattr(house_contract, "_lock_contract", controlled)
    writers = {
        "direct": lambda value: settings_service.set_setting("weather_provider", value),
        "bulk": lambda value: settings_service.set_settings_bulk({"weather_provider": value}),
    }
    before = await generation(transaction)
    first = asyncio.create_task(writers[first_writer]("smhi"), name="repair6-first")
    await asyncio.wait_for(held.wait(), 10)
    second = asyncio.create_task(
        writers["bulk" if first_writer == "direct" else "direct"]("open-meteo")
    )
    try:
        await wait_for_database_lock(engine)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 15)
    assert await generation(transaction) == before + 2
    assert await settings_service.get_setting("weather_provider") == "open-meteo"
    await settings_service.set_settings_bulk({"comfort_temp_target": "20.65"})
    await settings_service.set_setting("weather_provider", "open-meteo")
    assert await generation(transaction) == before + 2


async def demand_inputs():
    from packages.ml.forecast_quality import materialize_native_inputs
    from tests.planner_orm_support import DEVICE

    inputs = await materialize_native_inputs("demand", now=dt.datetime.now(UTC), device_id=DEVICE)
    assert inputs is not None
    return inputs


def fitted_artifact(inputs):
    from packages.ml.demand_model_core import build_demand_candidate
    from packages.ml.forecast_quality import check_exact_fit, native_scope
    from packages.ml.safe_persistence import publish_signed_artifact

    features = np.zeros((500, 10))
    features[:, 0] = np.tile(np.arange(-5.0, 20.0), 20)
    start = dt.datetime.now(UTC) - dt.timedelta(days=30)
    payload = build_demand_candidate(
        {
            "features": features,
            "targets": 3.0 - 0.1 * features[:, 0],
            "bounds": [
                (start + dt.timedelta(hours=i), start + dt.timedelta(hours=i + 1))
                for i in range(500)
            ],
        }
    )
    assert payload is not None
    check_exact_fit("demand", payload)
    artifact = publish_signed_artifact(
        payload,
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest={
            "fit_id": str(uuid4()),
            "data_cutoff": start.isoformat(),
            "input_schema": inputs["schemas"]["input"],
            "output_schema": "demand_lifecycle_v1",
            "runtime_versions": {"python": platform.python_version()},
            "evidence_hashes": [hashlib.sha256(pickle.dumps(payload, protocol=5)).hexdigest()],
            "scope": native_scope(inputs),
            "device_id": inputs["device_id"],
            "horizons": [60],
            "capabilities": ["demand"],
        },
        round_trip_check=lambda restored: check_exact_fit("demand", restored),
    )
    return artifact


async def authority():
    from packages.core.settings_service import set_setting
    from packages.ml.lifecycle import Lifecycle

    lifecycle = Lifecycle()
    await lifecycle.initialize()
    await set_setting("ml_maintenance_enabled", "true")
    lease = await lifecycle.acquire("repair6-owned-tests", dt.timedelta(minutes=5))
    assert lease is not None
    return lifecycle, lease


async def publish_incumbent(lifecycle, lease, artifact):
    from packages.ml.forecast_quality import QUALIFICATION_POLICY_SCHEMA
    from packages.ml.lifecycle import Qualification

    now = dt.datetime.now(UTC)
    result = await lifecycle.publish(
        lease,
        artifact,
        expected_revision=(await lifecycle.read()).revision,
        expected_incumbent=None,
        role="incumbent",
        qualification=Qualification(
            now,
            now,
            {
                **artifact.manifest["scope"],
                "qualification_policy": QUALIFICATION_POLICY_SCHEMA,
                "horizons": [60],
                "capabilities": ["demand"],
            },
        ),
    )
    assert result.committed, result


async def test_real_fit_provider_change_refuses_lifecycle_publication(owned):
    from packages.core.settings_service import set_setting, set_settings_bulk
    from packages.ml.lifecycle import Lifecycle
    from packages.core.models import MLArtifactRecord

    _, transaction = owned
    inputs = await demand_inputs()
    lifecycle, lease = await authority()
    entered, release = threading.Event(), threading.Event()

    def fit():
        entered.set()
        assert release.wait(15)
        return fitted_artifact(inputs)

    task = asyncio.create_task(asyncio.to_thread(fit))
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        await set_setting("weather_provider", "smhi")
        await set_settings_bulk({"weather_provider": "open-meteo"})
    finally:
        release.set()
    artifact = await asyncio.wait_for(task, 60)
    before = await lifecycle.read()
    result = await lifecycle.publish(
        lease,
        artifact,
        expected_revision=before.revision,
        expected_incumbent=None,
    )
    assert not result.committed
    assert (await Lifecycle().read()).references == ()
    async with transaction() as session:
        assert (await session.execute(select(MLArtifactRecord))).scalars().all() == []


async def test_real_signed_load_adoption_refuses_provider_aba_during_load(owned):
    from packages.core.settings_service import set_setting, set_settings_bulk
    from packages.ml.demand_model_core import DemandModel

    lifecycle, lease = await authority()
    artifact = await asyncio.to_thread(fitted_artifact, await demand_inputs())
    await publish_incumbent(lifecycle, lease, artifact)
    model = DemandModel()
    assert await model.arefresh_committed(lifecycle=lifecycle)
    assert model.is_trained
    original = lifecycle.load_committed
    entered, release = asyncio.Event(), asyncio.Event()

    async def load(*args, **kwargs):
        payload = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return payload

    lifecycle.load_committed = load
    task = asyncio.create_task(model.arefresh_committed(lifecycle=lifecycle))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        await set_setting("weather_provider", "smhi")
        await set_settings_bulk({"weather_provider": "open-meteo"})
    finally:
        release.set()
    assert await asyncio.wait_for(task, 15) is False
    assert model._model is None and not model.is_trained


async def test_publication_holds_dependency_lock_until_database_commit(owned, monkeypatch):
    from packages.core.settings_service import set_setting
    from packages.ml import safe_persistence
    from packages.ml.demand_model_core import DemandModel

    engine, _ = owned
    lifecycle, lease = await authority()
    artifact = await asyncio.to_thread(fitted_artifact, await demand_inputs())
    original = safe_persistence.certify_durable_artifact
    entered, release = threading.Event(), threading.Event()

    def certify(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(safe_persistence, "certify_durable_artifact", certify)
    publishing = asyncio.create_task(publish_incumbent(lifecycle, lease, artifact))
    assert await asyncio.to_thread(entered.wait, 10)
    writer = asyncio.create_task(set_setting("weather_provider", "smhi"))
    try:
        await wait_for_database_lock(engine)
        assert not writer.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(publishing, writer), 20)
    assert (await lifecycle.read()).reference("demand") is not None
    # Publication linearized first; after the writer commits it cannot be served.
    model = DemandModel()
    assert await model.arefresh_committed(lifecycle=lifecycle) is False
    assert not model.is_trained


@pytest.mark.parametrize("layer", ["rules_only", "milp_preferred", "milp_fallback"])
async def test_database_backed_true_planner_publication_one_frozen_contract(
    owned, monkeypatch, layer
):
    import json
    from packages.core.models import PlanRecord, PlanActionRecord
    from packages.core.house_contract import validate_plan_contract_snapshot
    from packages.core.settings_service import set_settings_bulk
    from packages.optimizer import main, SolverTimeoutError
    from tests.planner_orm_support import isolate_model_evidence
    from unittest.mock import AsyncMock

    _, transaction = owned
    isolate_model_evidence(monkeypatch)
    main._demand_model.reset()
    await set_settings_bulk(
        {"optimizer_layer": "milp_preferred" if layer != "rules_only" else layer}
    )
    if layer == "milp_fallback":
        monkeypatch.setattr(
            main.MILPOptimizer,
            "generate_plan",
            AsyncMock(side_effect=SolverTimeoutError("owned deterministic fallback fixture")),
        )
    plan_id = await main.run_optimization()
    assert plan_id is not None
    async with transaction() as session:
        plan = await session.get(PlanRecord, plan_id)
        saved = json.loads(plan.plan_json)
        assert validate_plan_contract_snapshot(plan, saved["house_contract_snapshot"])
        assert (
            saved["forecast_snapshot"]["house_contract_snapshot"]
            == saved["house_contract_snapshot"]
        )
        assert saved["forecast_snapshot"]["target_schedule"] == saved["target_schedule"]
        assert saved["actions"] == [] and plan.status == "diagnostic"
        assert saved["input_provenance"]["planning_dependency_generation"] == await generation(
            transaction
        )
        assert (await session.execute(select(PlanActionRecord))).scalars().all() == []
        assert plan.optimizer_version.startswith("milp" if layer == "milp_preferred" else "rules")


@pytest.mark.parametrize("boundary", ["after_refresh", "after_inputs", "after_prediction"])
async def test_provider_aba_between_planning_boundaries_refuses_database_publication(
    owned, monkeypatch, boundary
):
    from packages.core.models import PlanRecord
    from packages.core.settings_service import set_setting, set_settings_bulk
    from packages.optimizer import main, data_access
    from tests.planner_orm_support import isolate_model_evidence

    _, transaction = owned
    isolate_model_evidence(monkeypatch)
    main._demand_model.reset()
    entered, release = asyncio.Event(), asyncio.Event()
    fired = False

    async def pause():
        nonlocal fired
        if not fired:
            fired = True
            entered.set()
            await release.wait()

    if boundary == "after_refresh":
        original = main._select_optimizer

        async def select_optimizer(*args, **kwargs):
            result = await original(*args, **kwargs)
            await pause()
            return result

        monkeypatch.setattr(main, "_select_optimizer", select_optimizer)
    elif boundary == "after_inputs":
        original = data_access._input_batch

        async def input_batch(*args, **kwargs):
            result = await original(*args, **kwargs)
            await pause()
            return result

        monkeypatch.setattr(data_access, "_input_batch", input_batch)
    else:
        original = main.RulesOptimizer.generate_plan

        async def generate_plan(self):
            result = await original(self)
            assert result is not None
            await pause()
            return result

        monkeypatch.setattr(main.RulesOptimizer, "generate_plan", generate_plan)
    task = asyncio.create_task(main.run_optimization())
    await asyncio.wait_for(entered.wait(), 15)
    try:
        await set_setting("weather_provider", "smhi")
        await set_settings_bulk({"weather_provider": "open-meteo"})
    finally:
        release.set()
    assert await asyncio.wait_for(task, 60) is None
    async with transaction() as session:
        assert (await session.execute(select(PlanRecord))).scalars().all() == []


@pytest.mark.parametrize(
    "boundary", ["unchanged", "after_refresh", "before_prediction", "after_prediction"]
)
async def test_qualified_demand_actual_planning_context_through_admission(
    owned, monkeypatch, boundary
):
    import json
    from unittest.mock import Mock
    from packages.core.models import PlanRecord, DeviceStatusRecord
    from packages.core.settings_service import set_setting, set_settings_bulk
    from packages.ml import forecast_quality
    from packages.ml.demand_model_core import DemandModel
    from packages.optimizer import main
    from tests.planner_orm_support import isolate_model_evidence

    _, transaction = owned
    lifecycle, lease = await authority()
    artifact = await asyncio.to_thread(fitted_artifact, await demand_inputs())
    await publish_incumbent(lifecycle, lease, artifact)
    model = DemandModel()
    assert await model.arefresh_committed(lifecycle=lifecycle)
    # The represented strategy requires an explicit conditional native-load
    # assumption matching this fitted demand, not the unrelated degree-day
    # assumption used by the untrained producer fixtures.
    from copy import deepcopy

    async with transaction() as session:
        status = (await session.execute(select(DeviceStatusRecord))).scalar_one()
        provenance = deepcopy(status.native_policy_provenance)
        start = dt.datetime.fromisoformat(
            provenance["diagnostic_assumptions"]["native_sh_provenance"]["horizon_start"]
        )
        demands = model.predict_hourly(
            [
                {
                    "ts": start + dt.timedelta(hours=h),
                    "temperature": 5.0,
                    "wind_speed": 2.0,
                    "irradiance": 0.0,
                    "precipitation": 0.0,
                    "humidity": 60.0,
                    "cloud_cover": 0.5,
                }
                for h in range(24)
            ],
            24,
        )
        provenance["diagnostic_assumptions"]["native_sh_provenance"]["hourly_electrical_kw"] = (
            demands
        )
        status.native_policy_provenance = provenance
    isolate_model_evidence(monkeypatch)
    monkeypatch.setattr(main, "_demand_model", model)
    await set_settings_bulk(
        {
            "optimizer_layer": "milp_preferred",
            "comfort_temp_min": "10",
            "comfort_temp_target": "10",
        }
    )
    predictor = Mock(wraps=model.predict_hourly)
    monkeypatch.setattr(model, "predict_hourly", predictor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    if boundary == "after_refresh":
        refresh = model.arefresh_committed

        async def refreshed(*args, **kwargs):
            result = await refresh(*args, **kwargs)
            assert result
            await pause()
            return result

        monkeypatch.setattr(model, "arefresh_committed", refreshed)
    elif boundary == "before_prediction":
        scorecard = forecast_quality.get_forecast_scorecard

        async def get_scorecard(*args, **kwargs):
            result = await scorecard(*args, **kwargs)
            await pause()
            return result

        monkeypatch.setattr(forecast_quality, "get_forecast_scorecard", get_scorecard)
    elif boundary == "after_prediction":
        generate = main.MILPOptimizer.generate_plan

        async def generated(self):
            result = await generate(self)
            assert result is not None
            await pause()
            return result

        monkeypatch.setattr(main.MILPOptimizer, "generate_plan", generated)
    if boundary == "unchanged":
        plan_id = await main.run_optimization()
        assert plan_id is not None
        assert predictor.called
        async with transaction() as session:
            plan = await session.get(PlanRecord, plan_id)
            provenance = json.loads(plan.input_provenance_json)
            dependency = provenance["models"]["demand_serving_dependency"]
            assert dependency["used_for_plan"] is True
            assert dependency["artifact_digest"] == artifact.artifact_hash
            assert (
                dependency["dependency_generation"]
                == artifact.manifest["scope"]["component_dependency_generation"]
            )
    else:
        task = asyncio.create_task(main.run_optimization())
        await asyncio.wait_for(entered.wait(), 15)
        try:
            await set_setting("weather_provider", "smhi")
            await set_settings_bulk({"weather_provider": "open-meteo"})
        finally:
            release.set()
        assert await asyncio.wait_for(task, 60) is None
        if boundary == "after_refresh":
            predictor.assert_not_called()
        async with transaction() as session:
            assert (await session.execute(select(PlanRecord))).scalars().all() == []

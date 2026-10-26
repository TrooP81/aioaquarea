"""Offline ordering/failure contracts; real PostgreSQL coverage lives in E2E."""

from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.sql.dml import Delete, Insert

from tests.e2e import ordinary_cleanup as cleanup


@pytest.mark.parametrize("layer", ["rules_only", "milp_preferred"])
@pytest.mark.parametrize("gate", ["valid", "absent", "mismatch"])
def test_planning_rows_are_valid_recognized_settings(layer, gate):
    from packages.core.models import SettingRecord
    from packages.core.settings_service import SETTINGS_SCHEMA
    from tests.planner_orm_support import planning_rows

    rows = planning_rows(layer=layer, gate=gate)
    assert isinstance(rows, list)  # SQLite/P17 consumers retain the synchronous interface.
    values = {row.key: row.value for row in rows if isinstance(row, SettingRecord)}
    minimum, target, maximum = (
        float(values[key])
        for key in ("comfort_temp_min", "comfort_temp_target", "comfort_temp_max")
    )
    assert 10 <= minimum <= target <= maximum <= 30
    assert set(values) <= set(SETTINGS_SCHEMA)
    assert values["learning_mode_enabled"] == "false"
    status = next(row for row in rows if type(row).__name__ == "DeviceStatusRecord")
    assert status.zone1_temp == status.zone1_target_temp == 35
    assert status.tank_target_temp == 60


def _fixture_producer(filename, name, namespace):
    """Execute the real producer body without importing the owned E2E conftest."""
    module = ast.parse(Path(cleanup.__file__).with_name(filename).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    function.decorator_list = []
    isolated = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            function,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(isolated), filename, "exec"), namespace)
    return namespace[name]


async def _producer_session(*, planning=False):
    """SQL-boundary fake, not PostgreSQL proof. Real services/validators run above it."""
    import datetime as dt
    from uuid import UUID

    from packages.core.models import (
        HouseContractRevisionRecord,
        HouseInstallationRecord,
        MLLifecycleStateRecord,
        PlanRecord,
        SettingRecord,
    )
    from sqlalchemy.sql.elements import TextClause
    from tests.planner_orm_support import planning_rows

    rows = {row.key: row for row in planning_rows() if planning and isinstance(row, SettingRecord)}
    rows.pop("smartthings_device_ids", None)
    events, added = [], []
    installation = HouseInstallationRecord(
        singleton_id=1,
        installation_id=UUID(int=1),
        house_id=UUID(int=2),
        current_revision_id=17,
        indoor_dependency_generation=4,
        demand_dependency_generation=3,
    )
    state = MLLifecycleStateRecord(
        id=1,
        epoch="offline-epoch",
        revision=1,
        fence=1,
        maintenance_enabled=False,
        checkpoints={},
    )
    session = SimpleNamespace()

    class Scalars:
        def all(self):
            return list(rows.values())

        def __iter__(self):
            return iter(rows.values())

    async def execute(statement, *args):
        if isinstance(statement, TextClause):
            assert "pg_advisory_xact_lock" in str(statement)
            events.append("contract_lock")
            return SimpleNamespace()
        if isinstance(statement, Insert):
            if statement.table.name == "settings":
                values = statement.compile().params
                rows[values["key"]] = SettingRecord(key=values["key"], value=values["value"])
                events.append("setting")
            else:
                assert statement.table.name == "ml_lifecycle_state"
                assert "contract_lock" in events
                events.append("lifecycle")
            return SimpleNamespace()
        sql = str(statement)
        if "ml_lifecycle_state" in sql:
            return SimpleNamespace(all=lambda: [(state, None)])
        assert "settings" in sql, sql
        return SimpleNamespace(scalars=Scalars)

    session.execute = execute
    effective = await cleanup.settings_service.get_all_settings(session=session)
    projection = cleanup.house_contract._settings_projection(
        effective, installation.installation_id, installation.house_id
    )
    revision = HouseContractRevisionRecord(
        id=17,
        installation_id=installation.installation_id,
        schema_version=cleanup.house_contract.SCHEMA_VERSION,
        contract_json=projection,
        contract_hash=cleanup.house_contract._contract_hash(projection),
        effective_at=dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc),
        source="offline_fixture",
    )

    async def get(model, identifier):
        assert identifier in (1, 17)
        return {HouseInstallationRecord: installation, HouseContractRevisionRecord: revision}[model]

    def add(row):
        if isinstance(row, PlanRecord):
            assert events.index("contract_lock") < events.index("lifecycle")
            row.id = 123
        added.append(row)
        events.append("add")

    async def commit():
        events.append("commit")

    async def scalar(statement):
        assert "ml_lifecycle_state" in str(statement)
        return state

    session.get = get
    session.scalar = scalar
    session.add = add
    session.add_all = lambda evidence: [add(row) for row in evidence]
    session.flush = AsyncMock()
    session.commit = AsyncMock(side_effect=commit)
    return session, installation, revision, state, events, added


@pytest.mark.parametrize("saved_hours,plan_hours", [(4, 4), (14, 24)])
async def test_saved_forecast_producer_real_authority_lifecycle_and_coverage(
    monkeypatch, saved_hours, plan_hours
):
    import datetime as dt
    import json
    from contextlib import asynccontextmanager

    from packages.api.routers import models_router
    from packages.core.models import IndoorTempReading, PlanRecord

    session, installation, revision, state, events, added = await _producer_session()
    now = dt.datetime(2026, 10, 26, 10, 59, 59, tzinfo=dt.timezone.utc)
    producer = _fixture_producer(
        "test_api_indoor_forecast.py",
        "_seed_saved_forecast",
        {
            "dt": dt,
            "json": json,
            "asynccontextmanager": asynccontextmanager,
            "IndoorTempReading": IndoorTempReading,
            "PlanRecord": PlanRecord,
        },
    )
    plan = await producer(
        session,
        now,
        saved_hours=saved_hours,
        plan_hours=plan_hours,
        reference_sensor=saved_hours == 14,
    )
    assert events[-1] == "commit"
    session.commit.assert_awaited_once()
    assert [row for row in added if isinstance(row, PlanRecord)] == [plan]
    assert plan.horizon_start == dt.datetime(2026, 10, 26, 11, tzinfo=dt.timezone.utc)
    assert plan.horizon_end == plan.horizon_start + dt.timedelta(hours=plan_hours)
    payload = json.loads(plan.plan_json)
    authority, snapshot = payload["house_contract_snapshot"], payload["forecast_snapshot"]
    assert cleanup.house_contract.validate_plan_contract_snapshot(plan, authority)
    assert authority == await cleanup.house_contract.snapshot_from_revision(
        session, revision.id, plan.horizon_start, plan.horizon_end
    )
    assert (
        snapshot["target_schedule"]
        == cleanup.house_contract.canonical_target_schedule(authority)[:saved_hours]
    )
    provenance = json.loads(plan.input_provenance_json)
    assert provenance["indoor_dependency_generation"] == installation.indoor_dependency_generation
    assert installation.indoor_dependency_generation == (5 if saved_hours == 14 else 4)
    assert not state.maintenance_enabled

    @asynccontextmanager
    async def committed_session():
        assert "commit" in events  # Real cross-session checker runs only after owner commits.
        yield session

    monkeypatch.setattr(models_router, "get_session", committed_session)
    assert await models_router._saved_plan_model_dependencies_are_current(plan)
    assert models_router._qualified_forecast_indexes(
        plan, payload, snapshot, plan.horizon_start, saved_hours
    ) == list(range(saved_hours))
    window = models_router._plan_forecast_window(plan, plan.horizon_start, saved_hours)
    assert window is not None
    assert window["forecast_with_plan"][0]["predicted_indoor_temp"] == 22.0
    assert window["forecast_no_heating"][0]["predicted_indoor_temp"] == 19.5
    assert window["price_forecast"][0]["price_eur_per_kwh"] == 0.12
    assert (
        window["forecast_with_plan"][0]["ts"]
        == (plan.horizon_start + dt.timedelta(hours=1)).isoformat()
    )
    qualifier = _fixture_producer(
        "test_api_indoor_forecast.py", "_assert_saved_forecast_qualified", {"json": json}
    )
    serialized = await qualifier(plan, plan.horizon_start, saved_hours)
    assert serialized["forecast_with_plan"][0]["ts"] == (
        plan.horizon_start + dt.timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    assert serialized["target_schedule"][0]["contract_hash"] == plan.contract_hash
    if saved_hours == 14:
        assert models_router._plan_forecast_window(plan, plan.horizon_start, 24) is None
    # The genuine dependency guard must reject both generation drift and a saved lie.
    installation.indoor_dependency_generation += 1
    assert not await models_router._saved_plan_model_dependencies_are_current(plan)
    provenance["indoor_dependency_generation"] = installation.indoor_dependency_generation
    provenance["models"]["demand_trained"] = True
    plan.input_provenance_json = json.dumps(provenance)
    assert not await models_router._saved_plan_model_dependencies_are_current(plan)


def test_saved_forecast_clock_is_router_local_and_stable_at_hour_rollover(monkeypatch):
    import datetime as dt

    from packages.api.routers import models_router

    instant = dt.datetime(2026, 10, 26, 10, 59, 59, tzinfo=dt.timezone.utc)
    original_datetime = dt.datetime
    source = ast.parse(
        Path(cleanup.__file__).with_name("test_api_indoor_forecast.py").read_text(encoding="utf-8")
    )
    function = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "_freeze_forecast_clock"
    )
    namespace = {"dt": dt, "SimpleNamespace": SimpleNamespace}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "forecast_clock", "exec"), namespace)
    namespace["_freeze_forecast_clock"](monkeypatch, instant)
    assert dt.datetime is original_datetime
    assert models_router.dt.datetime.now(dt.timezone.utc) == instant
    assert models_router.dt.datetime.now(dt.timezone.utc) == instant


@pytest.mark.parametrize("changed", [False, True])
async def test_saved_forecast_storage_check_detects_provenance_changes(changed):
    plan = SimpleNamespace(
        plan_json="saved",
        input_provenance_json="provenance",
        contract_revision_id=17,
        contract_hash="a" * 64,
        contract_state="current",
    )
    before = (
        plan.plan_json,
        plan.input_provenance_json,
        plan.contract_revision_id,
        plan.contract_hash,
        plan.contract_state,
    )

    async def refresh(actual):
        assert actual is plan
        if changed:
            actual.input_provenance_json = "mutated"

    session = SimpleNamespace(refresh=refresh)
    checker = _fixture_producer(
        "test_api_indoor_forecast.py", "_assert_saved_forecast_unchanged", {}
    )
    if changed:
        with pytest.raises(AssertionError):
            await checker(session, plan, before)
    else:
        await checker(session, plan, before)


@pytest.mark.parametrize("failure", ["ensure", "bulk", "freeze", "lifecycle", "commit"])
async def test_saved_forecast_producer_failure_aborts_before_serving(monkeypatch, failure):
    import datetime as dt
    import json
    from contextlib import asynccontextmanager

    from packages.core.models import IndoorTempReading, PlanRecord
    from packages.ml.lifecycle import Lifecycle

    session, _, _, _, events, added = await _producer_session()
    rejected = AsyncMock(side_effect=ValueError(f"{failure} rejected"))
    if failure == "ensure":
        monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", rejected)
    elif failure == "bulk":
        monkeypatch.setattr(cleanup.settings_service, "set_settings_bulk", rejected)
    elif failure == "freeze":
        monkeypatch.setattr(cleanup.house_contract, "freeze_contract_snapshot", rejected)
    elif failure == "lifecycle":
        monkeypatch.setattr(Lifecycle, "initialize", rejected)
    else:
        session.commit = rejected
    producer = _fixture_producer(
        "test_api_indoor_forecast.py",
        "_seed_saved_forecast",
        {
            "dt": dt,
            "json": json,
            "asynccontextmanager": asynccontextmanager,
            "IndoorTempReading": IndoorTempReading,
            "PlanRecord": PlanRecord,
        },
    )
    serve = AsyncMock()
    with pytest.raises(ValueError, match=f"{failure} rejected"):
        await producer(
            session,
            dt.datetime.now(dt.timezone.utc),
            saved_hours=14,
            plan_hours=24,
            reference_sensor=True,
        )
        await serve()
    serve.assert_not_awaited()
    assert "commit" not in events
    if failure != "commit":
        assert not any(isinstance(row, PlanRecord) for row in added)
        session.commit.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "ensure", "bulk", "commit", "consistency"])
async def test_p17_producer_routes_settings_atomically_before_consistency(monkeypatch, failure):
    from packages.core.models import SettingRecord
    from tests.planner_orm_support import planning_rows

    session, installation, _, _, events, added = await _producer_session(planning=True)
    boundary = AsyncMock()

    async def consistent():
        assert events[-1] == "commit"
        await cleanup.assert_ordinary_contract_consistent(
            session, await cleanup.settings_service.get_all_settings(session=session)
        )
        if failure == "consistency":
            raise ValueError("consistency rejected")
        return "materialized boundary"

    boundary.side_effect = consistent
    rejected = AsyncMock(side_effect=ValueError(f"{failure} rejected"))
    if failure == "ensure":
        monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", rejected)
    elif failure == "bulk":
        monkeypatch.setattr(cleanup.settings_service, "set_settings_bulk", rejected)
    elif failure == "commit":
        session.commit = rejected
    producer = _fixture_producer(
        "test_planner_orm_postgresql.py",
        "_seed_planning_rows",
        {
            "planning_rows": planning_rows,
            "SettingRecord": SettingRecord,
            "_planning_boundary": boundary,
        },
    )
    plan = AsyncMock()
    if failure:
        with pytest.raises(ValueError, match=f"{failure} rejected"):
            await producer(session)
            await plan()
        plan.assert_not_awaited()
        if failure != "consistency":
            boundary.assert_not_awaited()
    else:
        assert await producer(session) == "materialized boundary"
        assert added and not any(isinstance(row, SettingRecord) for row in added)
        assert installation.indoor_dependency_generation == 5
        session.commit.assert_awaited_once()
        boundary.assert_awaited_once()


@pytest.mark.parametrize("component", [None, "comfort", "thermal", "demand"])
async def test_saved_forecast_producer_rejects_nonempty_or_maintained_lifecycle(component):
    import datetime as dt
    import json
    from contextlib import asynccontextmanager

    from packages.core.models import IndoorTempReading, MLArtifactRecord, PlanRecord
    from packages.ml.lifecycle import LIFECYCLE_SCHEMAS, PROTECTED_ROLES

    session, _, _, state, events, added = await _producer_session()
    if component is None:
        state.maintenance_enabled = True
    else:
        assert "incumbent" in PROTECTED_ROLES
        artifact = MLArtifactRecord(
            artifact_hash="a" * 64,
            epoch=state.epoch,
            component=component,
            schema_version=LIFECYCLE_SCHEMAS[component],
            fit_id="offline-existing-fit",
            manifest={},
            role="incumbent",
            committed_revision=1,
            qualification_scope={},
        )
        execute = session.execute

        async def with_existing_artifact(statement, *args):
            if not isinstance(statement, Insert) and "ml_lifecycle_state" in str(statement):
                return SimpleNamespace(all=lambda: [(state, artifact)])
            return await execute(statement, *args)

        session.execute = with_existing_artifact
    producer = _fixture_producer(
        "test_api_indoor_forecast.py",
        "_seed_saved_forecast",
        {
            "dt": dt,
            "json": json,
            "asynccontextmanager": asynccontextmanager,
            "IndoorTempReading": IndoorTempReading,
            "PlanRecord": PlanRecord,
        },
    )
    with pytest.raises(AssertionError):
        await producer(
            session,
            dt.datetime(2026, 10, 26, 10, 59, 59, tzinfo=dt.timezone.utc),
            saved_hours=4,
            plan_hours=4,
        )
    session.commit.assert_not_awaited()
    assert not any(isinstance(row, PlanRecord) for row in added)
    assert "commit" not in events


@pytest.mark.parametrize("producer_kind", ["forecast", "planning"])
async def test_ordinary_producer_cancellation_propagates_without_taking_session_ownership(
    monkeypatch, producer_kind
):
    import datetime as dt
    import json
    from contextlib import asynccontextmanager

    from packages.core.models import IndoorTempReading, PlanRecord, SettingRecord
    from packages.ml.lifecycle import Lifecycle
    from tests.planner_orm_support import planning_rows

    session, _, _, _, events, added = await _producer_session(planning=producer_kind == "planning")
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    boundary = AsyncMock()
    if producer_kind == "forecast":
        factories = []
        original_init = Lifecycle.__init__

        def capture_factory(self, session_factory):
            factories.append(session_factory)
            original_init(self, session_factory=session_factory)

        async def cancelled_read(self, actual_session):
            assert actual_session is session
            assert events[-1] == "lifecycle"
            raise asyncio.CancelledError()

        monkeypatch.setattr(Lifecycle, "__init__", capture_factory)
        monkeypatch.setattr(Lifecycle, "_read", cancelled_read)
        producer = _fixture_producer(
            "test_api_indoor_forecast.py",
            "_seed_saved_forecast",
            {
                "dt": dt,
                "json": json,
                "asynccontextmanager": asynccontextmanager,
                "IndoorTempReading": IndoorTempReading,
                "PlanRecord": PlanRecord,
            },
        )
        with pytest.raises(asyncio.CancelledError):
            await producer(
                session,
                dt.datetime(2026, 10, 26, 10, 59, 59, tzinfo=dt.timezone.utc),
                saved_hours=4,
                plan_hours=4,
            )
        assert len(factories) == 1
        # The injected context still exposes the caller's session after unwinding.
        async with factories[0]() as same_session:
            assert same_session is session
    else:
        monkeypatch.setattr(
            cleanup.settings_service,
            "set_settings_bulk",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        producer = _fixture_producer(
            "test_planner_orm_postgresql.py",
            "_seed_planning_rows",
            {
                "planning_rows": planning_rows,
                "SettingRecord": SettingRecord,
                "_planning_boundary": boundary,
            },
        )
        with pytest.raises(asyncio.CancelledError):
            await producer(session)
        boundary.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.close.assert_not_awaited()
    assert not added
    # Existing outer owner, rather than either producer, remains responsible for rollback.
    await session.rollback()
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("authority", ["valid", "settings_mismatch", "missing_revision"])
async def test_seed_plan_with_real_contract_helpers_and_model(authority):
    """A fabricated snapshot dict cannot prove the real producer is canonical."""
    import datetime as dt
    import json
    from uuid import UUID

    from packages.core.models import (
        HouseContractRevisionRecord,
        HouseInstallationRecord,
        PlanActionRecord,
        PlanRecord,
    )

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc)

    session = SimpleNamespace(
        execute=AsyncMock(),
        get=AsyncMock(),
        commit=AsyncMock(),
    )
    session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    effective = await cleanup.settings_service.get_all_settings(session=session)
    installation = HouseInstallationRecord(
        singleton_id=1,
        installation_id=UUID(int=1),
        house_id=UUID(int=2),
        current_revision_id=17,
    )
    projection = cleanup.house_contract._settings_projection(
        effective, installation.installation_id, installation.house_id
    )
    revision = HouseContractRevisionRecord(
        id=17,
        installation_id=installation.installation_id,
        schema_version=cleanup.house_contract.SCHEMA_VERSION,
        contract_json=projection,
        contract_hash=cleanup.house_contract._contract_hash(projection),
        effective_at=Clock.now(),
        source="effective_settings_import",
    )
    if authority == "settings_mismatch":
        revision.contract_hash = "0" * 64
    original_revision = deepcopy(revision.contract_json)
    reads, added = [], []

    async def get(model, identifier):
        reads.append((model, identifier))
        if model is HouseInstallationRecord:
            assert identifier == 1
            return installation
        assert model is HouseContractRevisionRecord and identifier == revision.id
        return None if authority == "missing_revision" else revision

    def add(row):
        # Real ensure + historical projection must finish before any new row.
        assert reads[-2:] == [
            (HouseContractRevisionRecord, 17),
            (HouseContractRevisionRecord, 17),
        ]
        if isinstance(row, PlanRecord):
            row.id = 123
        added.append(row)

    session.get.side_effect = get
    session.add = add
    producer = _fixture_producer(
        "conftest.py",
        "seed_plan",
        {
            "dt": SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
            "json": json,
            "PlanRecord": PlanRecord,
            "PlanActionRecord": PlanActionRecord,
        },
    )
    if authority != "valid":
        error = (
            cleanup.house_contract.ContractSettingsMismatch
            if authority == "settings_mismatch"
            else ValueError
        )
        with pytest.raises(error):
            await producer(session)
        assert added == []
        session.commit.assert_not_awaited()
    else:
        plan = await producer(session)
        snapshot = json.loads(plan.plan_json)["house_contract_snapshot"]
        assert cleanup.house_contract.validate_plan_contract_snapshot(plan, snapshot)
        assert plan.contract_revision_id == installation.current_revision_id == revision.id
        assert plan.contract_hash == revision.contract_hash
        assert plan.contract_state == "current"
        assert snapshot == await cleanup.house_contract.snapshot_from_revision(
            session, revision.id, plan.horizon_start, plan.horizon_end
        )
        assert plan.horizon_start == Clock.now() - dt.timedelta(hours=1)
        assert plan.horizon_end == Clock.now() + dt.timedelta(hours=23)
        assert [type(row) for row in added] == [PlanRecord] + [PlanActionRecord] * 3
        assert [row.status for row in added[1:]] == ["pending", "pending", "executed"]
        assert session.commit.await_count == 2
    assert installation.current_revision_id == 17
    assert revision.contract_json == original_revision


async def test_legacy_negative_producer_respects_existing_active_plan_unique_index():
    """Exercise actual negative-test writes against the real active uniqueness rule.

    SQLite is only a disposable constraint witness, not PostgreSQL acceptance.
    Copy the PG partial-index predicate explicitly: SQLite otherwise ignores it.
    """
    import datetime as dt
    import json

    from packages.core.models import PlanRecord
    from sqlalchemy import BigInteger, Column, MetaData, Table, create_engine, select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    metadata = MetaData()
    Table("house_contract_revisions", metadata, Column("id", BigInteger, primary_key=True))
    table = PlanRecord.__table__.to_metadata(metadata)
    index = next(index for index in table.indexes if index.name == "ux_plans_one_active")
    assert index.unique
    index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    try:
        with Session(engine, expire_on_commit=False) as sync:
            control = PlanRecord(
                created_at=now,
                horizon_start=now,
                horizon_end=now + dt.timedelta(hours=24),
                plan_json="{}",
                contract_state="current",
                contract_revision_id=17,
                contract_hash="a" * 64,
            )
            sync.add(control)
            sync.commit()
            # Keep a real rejection witness: sequential setup must not hide a
            # dropped/ineffective active-only index.
            sync.add(
                PlanRecord(
                    created_at=now,
                    horizon_start=now,
                    horizon_end=now + dt.timedelta(hours=24),
                    plan_json="{}",
                )
            )
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed: plans.status"):
                sync.commit()
            sync.rollback()
            assert sync.scalars(select(PlanRecord.id)).all() == [control.id]
            events = []

            class Facade:
                def add(self, row):
                    assert events == ["dashboard", "retire_commit", "dashboard"]
                    assert (
                        sync.scalar(select(PlanRecord.status).where(PlanRecord.id == control.id))
                        == "superseded"
                    )
                    events.append("legacy_add")
                    sync.add(row)

                async def commit(self):
                    sync.commit()
                    if len(events) == 1:
                        assert control.status == "superseded"
                        assert not sync.scalars(
                            select(PlanRecord.id).where(PlanRecord.status == "active")
                        ).all()
                        events.append("retire_commit")
                    else:
                        assert events[-1] == "legacy_add"
                        active = sync.scalars(
                            select(PlanRecord).where(PlanRecord.status == "active")
                        ).one()
                        assert active.id != control.id
                        assert active.contract_state == "legacy_unknown"
                        events.append("legacy_commit")

                async def refresh(self, row):
                    sync.refresh(row)

            class Client:
                async def get(self, path):
                    if path == "/api/dashboard":
                        # Read the real committed rows; never return the old
                        # positive control once its lifecycle is non-active.
                        eligible = sync.scalars(
                            select(PlanRecord).where(
                                PlanRecord.status == "active",
                                PlanRecord.contract_state == "current",
                                PlanRecord.contract_revision_id == control.contract_revision_id,
                                PlanRecord.contract_hash == control.contract_hash,
                            )
                        ).one_or_none()
                        data = {"active_plan": {"id": eligible.id} if eligible else None}
                        events.append("dashboard")
                    else:
                        legacy = sync.scalars(
                            select(PlanRecord).where(PlanRecord.id != control.id)
                        ).one()
                        assert path == f"/api/plans/{legacy.id}"
                        events.append("detail")
                        data = {
                            "id": legacy.id,
                            "contract_state": legacy.contract_state,
                            "contract_revision_id": legacy.contract_revision_id,
                            "contract_hash": legacy.contract_hash,
                            "house_contract_snapshot": {},
                            "contract_snapshot_state": "unknown",
                            "outcome": {"state": "unknown"},
                            "ordinary_writes_allowed": False,
                        }
                    return SimpleNamespace(status_code=200, json=lambda: data)

            producer = _fixture_producer(
                "test_api_dashboard.py",
                "test_dashboard_excludes_new_unbound_legacy_plan",
                {
                    "dt": SimpleNamespace(
                        datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta
                    ),
                    "json": json,
                    "PlanRecord": PlanRecord,
                },
            )
            await producer(None, Client(), Facade(), control)
            assert events == [
                "dashboard",
                "retire_commit",
                "dashboard",
                "legacy_add",
                "legacy_commit",
                "dashboard",
                "detail",
            ]
            retained = sync.scalars(select(PlanRecord).order_by(PlanRecord.id)).all()
            assert len(retained) == 2
            assert retained[0].id == control.id and retained[0].status == "superseded"
            assert retained[0].contract_revision_id == 17
            assert retained[0].contract_hash == "a" * 64
            assert retained[0].contract_state == "current"
            assert retained[0].plan_json == "{}"
            assert retained[1].status == "active"
            assert retained[1].contract_state == "legacy_unknown"
            assert retained[1].contract_revision_id is None
            assert retained[1].contract_hash is None
            assert retained[1].input_provenance_json is None
            assert json.loads(retained[1].plan_json) == {"version": "rules_v1", "actions": []}
    finally:
        engine.dispose()


@pytest.mark.parametrize("freeze_fails", [False, True])
async def test_seed_plan_freezes_same_session_and_exact_horizon_before_new_plan(
    monkeypatch, freeze_fails
):
    import datetime as dt
    import json

    events = []
    snapshot = {"revision_id": 17, "contract_hash": "a" * 64, "canonical": ["saved"]}
    session = SimpleNamespace()

    async def freeze(actual_session, start, end):
        assert actual_session is session
        assert end - start == dt.timedelta(hours=24)
        session.horizon = (start, end)
        events.append("freeze")
        if freeze_fails:
            raise ValueError("snapshot rejected")
        return snapshot

    def plan_record(**values):
        assert events == ["freeze"]
        assert (values["horizon_start"], values["horizon_end"]) == session.horizon
        assert values["contract_revision_id"] == snapshot["revision_id"]
        assert values["contract_hash"] == snapshot["contract_hash"]
        assert values["contract_state"] == "current"
        assert json.loads(values["plan_json"]) == {
            "version": "rules_v1",
            "actions": [],
            "house_contract_snapshot": snapshot,
        }
        assert values["optimizer_version"] == "rules_v1"
        assert values["cost_estimate_eur"] == 2.85
        events.append("construct")
        return SimpleNamespace(id=123, **values)

    def add(record):
        events.append("plan" if record.id == 123 else "action")

    async def commit():
        events.append("commit")

    session.add = add
    session.commit = commit
    monkeypatch.setattr(cleanup.house_contract, "freeze_contract_snapshot", freeze)
    producer = _fixture_producer(
        "conftest.py",
        "seed_plan",
        {
            "dt": dt,
            "json": json,
            "PlanRecord": plan_record,
            "PlanActionRecord": lambda **values: SimpleNamespace(id=None, **values),
        },
    )
    if freeze_fails:
        with pytest.raises(ValueError, match="snapshot rejected"):
            await producer(session)
        assert events == ["freeze"]
    else:
        result = await producer(session)
        assert result.id == 123
        assert events == [
            "freeze",
            "construct",
            "plan",
            "commit",
            "action",
            "action",
            "action",
            "commit",
        ]


@pytest.mark.parametrize("failure", [None, "ensure", "bulk"])
async def test_g03_producer_uses_versioned_service_same_transaction_and_propagates_failure(
    monkeypatch, failure
):
    events = []
    session = SimpleNamespace()
    current = SimpleNamespace(id=29)

    async def ensure(actual_session):
        assert actual_session is session
        events.append("ensure")
        if failure == "ensure":
            raise ValueError("ensure rejected")
        return current

    async def bulk(values, *, session: object, expected_revision_id, source):
        assert session is producer_session
        assert expected_revision_id == current.id
        assert source == "g03_read_only_fixture"
        assert values == {
            "aquarea_username": "g03-offline-test",
            "aquarea_password": "g03-offline-test",
            "device_status_max_age_minutes": "15",
            "poll_interval_seconds": "300",
            "smartthings_device_ids": "g03-room",
            "comfort_temp_target": "21.5",
            "outcome_experiments_enabled": "true",
            "outcome_experiment_max_curve_step_c": "0.5",
            "_heat_curve_verification_state": '{"status": "verified"}',
        }
        assert values.keys() <= cleanup.settings_service.SETTINGS_SCHEMA.keys()
        events.append("bulk")
        if failure == "bulk":
            raise ValueError("bulk rejected")

    async def commit():
        assert events == ["ensure", "bulk"]
        events.append("commit")

    producer_session = session
    session.commit = commit
    monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", ensure)
    monkeypatch.setattr(cleanup.settings_service, "set_settings_bulk", bulk)
    producer = _fixture_producer("test_api_dashboard.py", "recorded_settings", {})
    if failure:
        with pytest.raises(ValueError, match=f"{failure} rejected"):
            await producer(None, session)
        assert events == (["ensure"] if failure == "ensure" else ["ensure", "bulk"])
    else:
        await producer(None, session)
        assert events == ["ensure", "bulk", "commit"]


def test_immutable_empty_probe_explicitly_covers_the_model_fk_closure():
    from packages.core.models import Base

    targets = {"house_contract_revisions"}
    while True:
        expanded = targets | {
            table.name
            for table in Base.metadata.tables.values()
            if any(foreign_key.column.table.name in targets for foreign_key in table.foreign_keys)
        }
        if expanded == targets:
            break
        targets = expanded
    assert targets == {
        "house_contract_revisions",
        "house_installations",
        "plans",
        "optimization_requests",
    }
    # plan_actions.plan_id and optimization_requests.plan_id are not FKs;
    # do not add unrelated tables or silently switch this rejection probe to CASCADE.
    module = ast.parse(
        Path(cleanup.__file__).with_name("test_settings.py").read_text(encoding="utf-8")
    )
    probe = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name
        == "test_ordinary_first_initialization_retains_fallback_absence_and_immutable_empty_trigger"
    )
    sql = [
        node.value
        for node in ast.walk(probe)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("TRUNCATE ")
    ]
    assert sql == [
        "TRUNCATE public.house_contract_revisions, public.house_installations, "
        "public.plans, public.optimization_requests"
    ]
    assert {
        target.removeprefix("public.") for target in sql[0].removeprefix("TRUNCATE ").split(", ")
    } == targets


@pytest.mark.parametrize("orphan_history", [False, True])
async def test_first_capture_locks_validates_and_initializes_without_materializing_fallbacks(
    monkeypatch, orphan_history
):
    events = []
    rows = ({"key": "latitude", "value": "51.5", "updated_at": "original"},)
    effective = {"latitude": "51.5", "timezone": "fallback"}

    async def lock(session):
        events.append("lock")

    async def settings(*, session):
        events.append("effective")
        return effective

    async def ensure(session, settings, *, lock):
        assert lock is False and settings is effective
        events.append("initialize")

    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: (1,) if orphan_history else None)
        ),
    )
    monkeypatch.setattr(cleanup.house_contract, "_lock_contract", lock)
    monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", ensure)
    monkeypatch.setattr(cleanup.settings_service, "get_all_settings", settings)
    monkeypatch.setattr(cleanup, "_settings_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(cleanup, "_validate_effective", lambda *args: events.append("validate"))
    monkeypatch.setattr(cleanup, "assert_ordinary_contract_consistent", AsyncMock())
    monkeypatch.setattr(cleanup, "_house_boundary", AsyncMock(return_value=boundary()))
    if orphan_history:
        with pytest.raises(AssertionError, match="history without installation"):
            await cleanup.capture_ordinary_baseline(session)
        assert "initialize" not in events
    else:
        result = await cleanup.capture_ordinary_baseline(session)
        assert result.rows == rows and result.effective == effective
        assert "timezone" not in {row["key"] for row in result.rows}
        assert events == ["lock", "effective", "validate", "initialize"]


def test_generic_cleanup_excludes_all_migrated_authority_storage():
    source = Path(cleanup.__file__).with_name("conftest.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    expression = next(
        statement.value
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "LEGACY_TABLES"
            for target in statement.targets
        )
    )
    names = cleanup.PRESERVED_TABLES | {
        "manual_cop_observations",
        "price_scopes",
        "app_logs",
        "plans",
    }
    tables = [SimpleNamespace(name=name) for name in sorted(names)]
    actual = eval(
        compile(ast.Expression(expression), "<real legacy cleanup expression>", "eval"),
        {
            "Base": SimpleNamespace(metadata=SimpleNamespace(sorted_tables=tables)),
            "PRESERVED_TABLES": cleanup.PRESERVED_TABLES,
        },
    )
    assert {table.name for table in actual} == {"app_logs", "plans"}
    # The existing retention guards remain controlling, not replaced by exclusions.
    assert "manual_sql.guard_owned_database(connection)" in source
    assert "assert not dependencies" in source
    assert "assert await manual_sql.retained_storage_state(connection) == retained" in source
    assert "finish_owned_cleanup(" in source


@pytest.mark.parametrize("missing_pointer", [True, False])
async def test_existing_baseline_rejects_missing_pointer_or_mismatch(monkeypatch, missing_pointer):
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                current_revision_id=None if missing_pointer else 9,
                installation_id="installation",
                house_id="house",
            )
        )
    )
    monkeypatch.setattr(cleanup, "_validate_effective", lambda *args: None)
    ensure = AsyncMock(side_effect=cleanup.house_contract.ContractSettingsMismatch("mismatch"))
    monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", ensure)
    with pytest.raises(
        AssertionError if missing_pointer else cleanup.house_contract.ContractSettingsMismatch
    ):
        await cleanup.assert_ordinary_contract_consistent(session, {})
    assert ensure.await_count == (0 if missing_pointer else 1)


def boundary(*, revision=7, demand=3, indoor=5):
    return {
        "installation": {
            "singleton_id": 1,
            "installation_id": "installation",
            "house_id": "house",
            "current_revision_id": revision,
            "demand_dependency_generation": demand,
            "indoor_dependency_generation": indoor,
        },
        "history": ({"id": 7, "contract_hash": "A"},),
        "catalog": ("unchanged definitions",),
        "sequence": 7,
    }


@pytest.mark.parametrize("change", ["identity", "history", "catalog", "sequence", "generation"])
def test_boundary_rejects_retained_storage_corruption(change):
    before = boundary()
    after = deepcopy(before)
    if change == "identity":
        after["installation"]["house_id"] = "other"
    elif change == "history":
        after["history"][0]["contract_hash"] = "tampered"
    elif change == "catalog":
        after["catalog"] = ()
    elif change == "sequence":
        after["sequence"] = 6
    else:
        after["installation"]["demand_dependency_generation"] = 2
    with pytest.raises(AssertionError):
        cleanup._assert_conservation(before, after)


def test_boundary_accepts_forward_history_generations_and_sequence_gaps():
    before = boundary()
    after = boundary(revision=23, demand=8, indoor=10)
    after["history"] += ({"id": 23, "contract_hash": "A"},)
    after["sequence"] = 29  # rollback gaps are not corruption
    cleanup._assert_conservation(before, after)


@pytest.mark.parametrize(
    ("weather", "sensor", "timezone"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
        (True, True, True),
    ],
)
async def test_restore_orders_dependency_accounting_rows_publication_and_truncate(
    monkeypatch, weather, sensor, timezone
):
    events = []
    effective = {
        "weather_provider": "open-meteo",
        "smartthings_device_ids": "",
        "comfort_reference_sensor_id": "",
        **{key: "A" for key in cleanup.house_contract.CONTRACT_SETTING_KEYS},
    }
    pre = effective | {
        "weather_provider": "smhi" if weather else "open-meteo",
        "smartthings_device_ids": "sensor" if sensor else "",
        "timezone": "B" if timezone else "A",
    }
    before = boundary()
    after = boundary(
        revision=8 if timezone else 7,
        demand=3 + int(weather) + int(timezone),
        indoor=5 + int(weather or sensor),
    )
    if timezone:
        after["history"] += ({"id": 8, "contract_hash": "restored"},)
        after["sequence"] = 8
    baseline = cleanup.OrdinaryBaseline(
        ({"key": "retained", "value": "exact", "updated_at": "original"},),
        effective,
        before,
    )
    installation = SimpleNamespace(**before["installation"])
    final_installation = SimpleNamespace(**after["installation"])
    previous = SimpleNamespace(
        id=7, contract_hash="old", contract_json={"timezone": pre["timezone"]}
    )
    current = SimpleNamespace(
        id=after["installation"]["current_revision_id"],
        contract_hash="restored" if timezone else "old",
        contract_json={"timezone": "A"},
    )

    async def lock(session):
        events.append("lock")

    async def settings(*, session):
        events.append("settings")
        return pre if events.count("settings") == 1 else effective

    async def consistent(session, values):
        events.append("validate")
        return (installation, previous) if values is pre else (final_installation, current)

    async def bulk(updates, *, session, expected_revision_id, source):
        assert updates == {key: effective[key] for key in cleanup.DEPENDENCIES}
        assert expected_revision_id == 7 and source == cleanup.SOURCE
        assert "delete" not in events
        events.append("dependencies")

    async def execute(statement, params=None):
        if isinstance(statement, Delete):
            assert statement.table.name == "settings"
            events.append("delete")
        elif isinstance(statement, Insert):
            assert statement.table.name == "settings"
            assert params == list(baseline.rows)
            events.append("insert")
        else:
            pytest.fail(f"unexpected mutation {statement}")

    async def revise(session, values, *, expected_revision_id, source, changed_keys):
        assert values == effective and expected_revision_id == 7
        assert changed_keys == (["timezone"] if timezone else [])
        assert source == cleanup.SOURCE
        assert events[-1] == "settings"
        events.append("publish")
        return current

    async def truncate(connection):
        events.append("truncate")

    session = SimpleNamespace(
        execute=execute,
        flush=AsyncMock(),
        expire_all=lambda: events.append("expire"),
        connection=AsyncMock(return_value="outer connection"),
    )
    monkeypatch.setattr(cleanup.house_contract, "_lock_contract", lock)
    monkeypatch.setattr(cleanup.settings_service, "get_all_settings", settings)
    monkeypatch.setattr(cleanup.settings_service, "set_settings_bulk", bulk)
    monkeypatch.setattr(cleanup.house_contract, "revise_contract", revise)
    monkeypatch.setattr(cleanup, "assert_ordinary_contract_consistent", consistent)
    monkeypatch.setattr(cleanup, "_house_boundary", AsyncMock(side_effect=[before, after]))
    monkeypatch.setattr(cleanup, "_settings_rows", AsyncMock(return_value=baseline.rows))
    assert await cleanup.restore_ordinary_baseline(session, baseline, truncate) == after
    assert events == [
        "lock",
        "settings",
        "validate",
        "dependencies",
        "delete",
        "insert",
        "expire",
        "settings",
        "publish",
        "truncate",
        "expire",
        "settings",
        "validate",
    ]
    assert baseline.boundary is before, "uncommitted boundary must not advance"


@pytest.mark.parametrize(
    "failure", ["guard", "dependencies", "rows", "revision", "truncate", "commit", "cancel", None]
)
async def test_one_outer_transaction_poison_on_failure_and_advance_only_after_commit(
    monkeypatch, failure
):
    events = []
    baseline = cleanup.OrdinaryBaseline((), {}, boundary())
    original = baseline.boundary
    candidate = boundary(revision=9)

    class Transaction:
        async def __aenter__(self):
            events.append("begin")
            return "connection"

        async def __aexit__(self, kind, error, tb):
            assert baseline.boundary is original
            events.append("rollback" if kind else "commit")
            if failure == "commit":
                raise RuntimeError("commit")

    class Session:
        def __init__(self, **kwargs):
            assert kwargs == dict(
                bind="connection", expire_on_commit=False, join_transaction_mode="rollback_only"
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def guard(connection):
        events.append("guard")
        if failure == "guard":
            raise RuntimeError("guard")

    async def restore(session, baseline, truncate):
        for phase in ("dependencies", "rows", "revision", "truncate"):
            events.append(phase)
            if failure == phase:
                raise RuntimeError(phase)
        if failure == "cancel":
            raise asyncio.CancelledError
        return candidate

    monkeypatch.setattr(cleanup, "AsyncSession", Session)
    monkeypatch.setattr(cleanup, "restore_ordinary_baseline", restore)
    engine = SimpleNamespace(begin=Transaction)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert baseline.boundary is original and baseline.poisoned
        with pytest.raises(AssertionError, match="unusable"):
            await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert events.count("begin") == 1
    else:
        await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert baseline.boundary is candidate and not baseline.poisoned
        assert events[-1] == "commit"

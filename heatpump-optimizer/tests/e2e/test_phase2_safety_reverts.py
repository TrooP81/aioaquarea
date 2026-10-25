from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.config import settings
from packages.core.database import engine, get_session
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    ExecutorVerificationReadRecord,
    IndoorTempReading,
    PlanActionRecord,
    PlanRecord,
    ShowerEventRecord,
)
from packages.core.plan_lifecycle import activate_plan, supersede_active_plan
from packages.optimizer.executor_core import PlanExecutor
from packages.optimizer.shower_mode import ShowerDetector, reconcile_shower_expiry


ROOT = Path(__file__).parents[2]
MIGRATION_UNDER_TEST = "034"
CURRENT_HEAD = "039"
HISTORICAL_HEAD = "035"


class _FrozenDateTime(dt.datetime):
    current = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    settings.database_url = database_url
    return config


def _historical_ancestry() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [CURRENT_HEAD], "migration fixture requires the reviewed head"
    assert {
        revision: scripts.get_revision(revision).down_revision
        for revision in (MIGRATION_UNDER_TEST, HISTORICAL_HEAD, "036", "037", "038", CURRENT_HEAD)
    } == {
        MIGRATION_UNDER_TEST: "033",
        HISTORICAL_HEAD: MIGRATION_UNDER_TEST,
        "036": "035",
        "037": "036",
        "038": "037",
        CURRENT_HEAD: "038",
    }
    return HISTORICAL_HEAD


def _run_alembic(database_url: str, operation: str, revision: str) -> None:
    config = _alembic_config(database_url)
    getattr(command, operation)(config, revision)


async def _migration_version(database_engine, expected: str | None = None) -> str | None:
    async with database_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if expected is not None:
        assert version == expected
    return version


async def _migration_sql(database_engine, statement: str, parameters: dict | None = None):
    async with database_engine.begin() as connection:
        return await connection.execute(text(statement), parameters or {})


async def _clear_migration_data(database_engine) -> None:
    await _migration_sql(database_engine, "DELETE FROM shower_events")
    await _migration_sql(database_engine, "DELETE FROM plan_actions")
    await _migration_sql(database_engine, "DELETE FROM plans")
    await _migration_sql(database_engine, "DELETE FROM device_status")
    await _migration_sql(database_engine, "DELETE FROM settings")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def phase2_migration_database():
    public_url = settings.database_url
    database_name = f"phase2_e2e_{uuid.uuid4().hex[:12]}"
    public_engine = create_async_engine(public_url, poolclass=NullPool)
    database_url = (
        make_url(public_url).set(database=database_name).render_as_string(hide_password=False)
    )
    try:
        async with public_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        migration_engine = create_async_engine(database_url, poolclass=NullPool)
        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "028")
        yield migration_engine, database_url
    finally:
        if "migration_engine" in locals():
            await migration_engine.dispose()
        async with public_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await public_engine.dispose()
        settings.database_url = public_url


@pytest_asyncio.fixture(loop_scope="session")
async def migration_case(phase2_migration_database):
    migration_engine, database_url = phase2_migration_database
    version = await _migration_version(migration_engine)
    if version != "028":
        await _clear_migration_data(migration_engine)
        await asyncio.to_thread(_run_alembic, database_url, "downgrade", "028")
    await _clear_migration_data(migration_engine)
    await _migration_version(migration_engine, "028")
    yield migration_engine, database_url


@pytest_asyncio.fixture(loop_scope="session")
async def legacy_restore_migration_database():
    from tests.e2e import test_manual_cop_postgresql as owned_sql

    async with owned_sql.owned_migration_child("restore") as child:
        yield child


def _plan(now: dt.datetime, version: str, status: str = "active") -> PlanRecord:
    return PlanRecord(
        horizon_start=now,
        horizon_end=now + dt.timedelta(hours=2),
        plan_json="{}",
        optimizer_version=version,
        status=status,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def s1_owned_restore(db_session, monkeypatch):
    import redis.asyncio as redis

    from packages.core import safety_attempts
    from packages.core.services.aquarea import DistributedReadQuota
    from packages.optimizer.executor_core import LearningModeState
    from tests.test_safety_reverts import _S1Clock, _S1RestoreStore

    clock = _S1Clock(monkeypatch, utc_start=dt.datetime.now(dt.timezone.utc), track_real_time=True)
    store = _S1RestoreStore(clock=clock)
    now = dt.datetime.now(dt.timezone.utc)
    plan = _plan(now, "s1-owned-attempt", status="superseded")
    db_session.add(plan)
    await db_session.flush()
    store.source.id = None
    store.source.plan_id = plan.id
    db_session.add(store.source)
    await db_session.flush()
    store.restore.id = None
    store.restore.plan_id = plan.id
    store.restore.reverts_action_id = store.source.id
    db_session.add_all([store.restore, store.status])
    await db_session.commit()

    def executor():
        return PlanExecutor(
            store.wrapper,
            session_factory=get_session,
            connection_factory=engine.connect,
            sleep=clock.sleep,
            learning_check=AsyncMock(return_value=LearningModeState.INACTIVE),
            device_quality_check=AsyncMock(
                return_value={"ready": True, "reasons": [], "threshold_seconds": 900}
            ),
        )

    store.executor = executor
    store.expect_owner_blocked = False
    redis_client = redis.Redis.from_url(
        settings.redis_url, socket_timeout=2, socket_connect_timeout=2
    )
    store.wrapper._redis = redis_client
    store.wrapper._read_quota = DistributedReadQuota(redis_client)
    store.wrapper._account_key = store.wrapper._account_key_for_username(
        store.device_id + "@s1.invalid"
    )
    store.wrapper._distributed_quota_enabled = lambda: True
    try:
        assert await redis_client.ping()
        yield store
    finally:
        await redis_client.aclose()
        owner = safety_attempts._DEVICE_OWNERS.get(store.device_id)
        if store.expect_owner_blocked:
            assert owner is not None and owner._keep_blocked and owner._revoked
            assert all(task.done() for task in owner._tasks)
        else:
            assert owner is None


async def _s1_assert_database_fence(store, *, row_unlocked=True):
    from packages.core.safety_attempts import device_attempt_key

    async with engine.connect() as connection:
        key = device_attempt_key(store.device_id)
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            assert acquired is False
        finally:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.commit()
    async with get_session() as session:
        query = select(PlanActionRecord).where(PlanActionRecord.id == store.restore.id)
        if row_unlocked:
            query = query.with_for_update(nowait=True)
        current = (await session.execute(query)).scalar_one()
        assert current.status == "executing" and current.safety_claim_token is not None
        token = current.safety_claim_token
    assert (
        await store.executor()._claim_due_safety_action(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        )
        is None
    )
    return token


async def _s1_run_fenced_wait(store, db_session, entered, release):
    from packages.core.safety_attempts import SafetyAttempt

    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        token = await _s1_assert_database_fence(store)
        other = await SafetyAttempt.acquire(store.device_id + "-other", engine.connect)
        assert other is not None
        assert await other.release()
        await db_session.refresh(store.restore)
        assert store.restore.safety_claim_token == token
        release.set()
        await asyncio.wait_for(worker, 5)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    await db_session.refresh(store.restore)
    assert store.restore.status == "executed" and store.restore.safety_claim_token == token
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)


def _s1_repair2_clocked_status(store, now, defect, *, evidence_after):
    from packages.core.device_data_quality import _as_utc
    from packages.core.device_status_snapshot import build_device_status_record
    from tests.test_safety_reverts import _repair2_provenance

    provenance = _repair2_provenance(now, defect, evidence_after=evidence_after)
    timing = provenance["timing"]
    if defect in ("future_observation", "future_retrieval"):
        field = "observed_at" if defect == "future_observation" else "retrieved_at"
        timing[field] = (now + dt.timedelta(hours=1)).isoformat()
    zone = SimpleNamespace(
        temperature=35,
        heat_target_temperature=35,
        heat_min=20,
        heat_max=65,
        operation_status=SimpleNamespace(value=1),
    )
    device = SimpleNamespace(
        long_id=store.device_id,
        zones={1: zone},
        tank=None,
        mode="heat",
        current_direction=SimpleNamespace(name="HEATING"),
        current_action=SimpleNamespace(name="PUMP"),
        device_mode_status=SimpleNamespace(name="NORMAL"),
        operation_status=SimpleNamespace(value=1),
        pump_duty=None,
        quiet_mode=SimpleNamespace(value=0),
        powerful_time=SimpleNamespace(value=0),
        special_status=None,
        force_dhw=SimpleNamespace(value=0),
        force_heater=SimpleNamespace(value=0),
        holiday_timer=SimpleNamespace(value=0),
        status_observed_at=_as_utc(timing["observed_at"]),
        status_retrieved_at=_as_utc(timing["retrieved_at"]),
    )
    if defect in ("future_observation", "future_retrieval"):
        with pytest.raises(ValueError, match="provenance is unknown or future"):
            build_device_status_record(device, now=now)
        device.status_observed_at = device.status_retrieved_at = None
    record = build_device_status_record(device, now=now)
    if defect in ("future_observation", "future_retrieval"):
        record.native_policy_provenance["timing"] = timing
    assert record.native_policy_provenance["timing"] == timing
    assert record.native_policy_provenance["captured_at"] == now.isoformat()
    return record


def _s1_repair2_receipt(name, value):
    destination = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"]) / "repair2-cases"
    destination.mkdir(exist_ok=True)
    (destination / (name + ".json")).write_text(
        json.dumps(value, default=str, indent=2), encoding="utf-8"
    )


class TestS1OwnedAttempts:
    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("completion_path", ["ambiguous", "already_safe"])
    @pytest.mark.parametrize(
        "defect",
        [
            "old_observation",
            "old_retrieval",
            "both_old",
            "old_observation_missing_retrieval",
            "reversed",
            "future_observation",
            "future_retrieval",
            "prebound",
            "missing",
            "fresh",
        ],
    )
    async def test_repair2_persisted_clock_columns_cannot_forge_restore_completion(
        self, s1_owned_restore, db_session, completion_path, defect
    ):
        from packages.optimizer.actions import ActionType

        store = s1_owned_restore
        accepted = defect == "fresh"
        transitions = [store.restore.status]
        source_bound = store.clock.utcnow() - dt.timedelta(seconds=1)
        executor = store.executor()
        if completion_path == "already_safe":
            store.source.result_json = json.dumps(
                {
                    "dispatch_provenance": {
                        "schema": "source_dispatch_v1",
                        "device_id": store.device_id,
                        "drain": "confirmed",
                        "settled_at": source_bound.isoformat(),
                    }
                }
            )
            db_session.add(
                _s1_repair2_clocked_status(
                    store, store.clock.utcnow(), defect, evidence_after=source_bound
                )
            )
            await db_session.commit()
            if accepted:
                store.device.zones[1].heat_target_temperature = 35
            executor._last_device_quality[store.device_id] = {
                "ready": True,
                "threshold_seconds": 900,
            }
            assert (
                await executor._safety_already_safe(
                    store.restore,
                    ActionType.ZONE_TEMP_RESTORE,
                    json.loads(store.restore.payload_json),
                )
                is accepted
            )

        async def ambiguous_dispatch(temperature, *, zone_id):
            await store.clock.dispatch(2)
            if accepted:
                store.device.zones[zone_id].heat_target_temperature = temperature
            raise RuntimeError("AMBIGUOUS offline PostgreSQL write")

        async def sleep_and_ingest(seconds):
            await store.clock.sleep(seconds)
            async with get_session() as session:
                action = await session.get(PlanActionRecord, store.restore.id)
                transitions.append(action.status)
                if completion_path == "ambiguous" and action.status == "dispatched":
                    bound = dt.datetime.fromisoformat(
                        json.loads(action.result_json)["safety_attempt"]["dispatch_settled_at"]
                    )
                    session.add(
                        _s1_repair2_clocked_status(
                            store, store.clock.utcnow(), defect, evidence_after=bound
                        )
                    )

        store.device.set_temperature = AsyncMock(side_effect=ambiguous_dispatch)
        executor._sleep = sleep_and_ingest
        await executor.execute_due_actions()

        await db_session.refresh(store.restore)
        transitions.append(store.restore.status)
        assert store.restore.status == ("executed" if accepted else "pending")
        assert isinstance(store.restore.safety_claim_token, uuid.UUID)
        assert store.restore.reverts_action_id == store.source.id
        assert json.loads(store.restore.payload_json) == {"zone_id": 1, "temperature": 35}
        audits = (await db_session.execute(select(AuditLogRecord))).scalars().all()
        assert len(audits) == int(accepted)
        assert all(
            audit.result == "success" and audit.target_device == store.device_id for audit in audits
        )
        reads = (
            (
                await db_session.execute(
                    select(ExecutorVerificationReadRecord).order_by(
                        ExecutorVerificationReadRecord.id
                    )
                )
            )
            .scalars()
            .all()
        )
        preflight_deferred = completion_path == "already_safe" and defect not in (
            "prebound",
            "missing",
            "fresh",
        )
        no_io = completion_path == "already_safe" and (accepted or preflight_deferred)
        if no_io:
            assert reads == []
            store.device.refresh_data.assert_not_awaited()
            store.device.set_temperature.assert_not_awaited()
        else:
            store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
            assert [read.checkpoint_seconds for read in reads] == ([15] if accepted else [15, 60])
        assert store.device.zones[1].heat_target_temperature == (35 if accepted else 37)
        result = json.loads(store.restore.result_json)
        assert result["safety_attempt"]["drain"] == "confirmed"
        if not accepted:
            assert result["reason"] and result["original_reason"] == "original_obligation"
        columns = [
            dict(row._mapping)
            for row in (
                await db_session.execute(
                    text(
                        "SELECT ts, device_id, zone1_target_temp, "
                        "native_policy_provenance->>'captured_at' AS captured_at, "
                        "native_policy_provenance->'timing' AS timing "
                        "FROM device_status WHERE device_id=:device_id "
                        "AND native_policy_provenance IS NOT NULL ORDER BY ts"
                    ),
                    {"device_id": store.device_id},
                )
            ).all()
        ]
        assert columns and all(row["zone1_target_temp"] == 35 for row in columns)
        if completion_path == "ambiguous":
            assert "dispatched" in transitions
            assert result["safety_attempt"]["dispatch_outcome"] == "ambiguous"
        _s1_repair2_receipt(
            completion_path + "-" + defect,
            {
                "criteria": "AC-R01",
                "completion_path": completion_path,
                "defect": defect,
                "actual_device_target": store.device.zones[1].heat_target_temperature,
                "baseline": 35,
                "source_id": store.source.id,
                "restore_id": store.restore.id,
                "claim_token": store.restore.safety_claim_token,
                "transitions": transitions,
                "clock_columns_read_from_postgresql": columns,
                "result": result,
                "verification_checkpoints": [read.checkpoint_seconds for read in reads],
                "write_attempts": store.device.set_temperature.await_count,
                "success_audits": len(audits),
            },
        )

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("completion_path", ["already_safe", "ambiguous"])
    @pytest.mark.parametrize("remote_clock", ["observed_at", "retrieved_at"])
    @pytest.mark.parametrize("offset_microseconds", [-1, 0, 1])
    async def test_repair2_independent_single_clock_boundary_keeps_durable_obligation(
        self, s1_owned_restore, db_session, completion_path, remote_clock, offset_microseconds
    ):
        from packages.optimizer.actions import ActionType

        store = s1_owned_restore
        accepted = offset_microseconds > 0
        source_bound = store.clock.utcnow() - dt.timedelta(seconds=1)
        store.source.result_json = json.dumps(
            {
                "dispatch_provenance": {
                    "schema": "source_dispatch_v1",
                    "drain": "confirmed",
                    "device_id": store.device_id,
                    "settled_at": source_bound.isoformat(),
                }
            }
        )
        await db_session.commit()

        def clocked_status(bound):
            record = _s1_repair2_clocked_status(
                store, store.clock.utcnow(), "fresh", evidence_after=bound
            )
            record.native_policy_provenance["timing"] = {
                "schema": "local_snapshot_v1",
                "observed_at": None,
                "retrieved_at": None,
                remote_clock: (bound + dt.timedelta(microseconds=offset_microseconds)).isoformat(),
            }
            return record

        executor = store.executor()
        if completion_path == "already_safe":
            db_session.add(clocked_status(source_bound))
            await db_session.commit()
            if accepted:
                store.device.zones[1].heat_target_temperature = 35
            executor._last_device_quality[store.device_id] = {
                "ready": True,
                "threshold_seconds": 900,
            }
            assert (
                await executor._safety_already_safe(
                    store.restore,
                    ActionType.ZONE_TEMP_RESTORE,
                    json.loads(store.restore.payload_json),
                )
                is accepted
            )
            observation = await executor._load_persisted_observation(store.device_id, source_bound)
            assert (observation is not None) is accepted

        async def ambiguous_dispatch(temperature, *, zone_id):
            await store.clock.dispatch(2)
            if accepted:
                store.device.zones[zone_id].heat_target_temperature = temperature
            raise RuntimeError("AMBIGUOUS independent PostgreSQL boundary write")

        async def sleep_and_ingest(seconds):
            await store.clock.sleep(seconds)
            async with get_session() as session:
                current = await session.get(PlanActionRecord, store.restore.id)
                if current.status == "dispatched":
                    bound = dt.datetime.fromisoformat(
                        json.loads(current.result_json)["safety_attempt"]["dispatch_settled_at"]
                    )
                    session.add(clocked_status(bound))

        store.device.set_temperature = AsyncMock(side_effect=ambiguous_dispatch)
        executor._sleep = sleep_and_ingest

        await executor.execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == ("executed" if accepted else "pending")
        assert isinstance(store.restore.safety_claim_token, uuid.UUID)
        assert store.restore.reverts_action_id == store.source.id
        assert json.loads(store.restore.payload_json) == {"zone_id": 1, "temperature": 35}
        audits = (await db_session.execute(select(AuditLogRecord))).scalars().all()
        assert len(audits) == int(accepted)
        assert all(
            audit.result == "success" and audit.target_device == store.device_id for audit in audits
        )
        reads = (
            (
                await db_session.execute(
                    select(ExecutorVerificationReadRecord).order_by(
                        ExecutorVerificationReadRecord.id
                    )
                )
            )
            .scalars()
            .all()
        )
        if completion_path == "already_safe" and accepted:
            assert reads == []
            store.device.refresh_data.assert_not_awaited()
            store.device.set_temperature.assert_not_awaited()
        else:
            store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
            assert [read.checkpoint_seconds for read in reads] == ([15] if accepted else [15, 60])
        assert store.device.zones[1].heat_target_temperature == (35 if accepted else 37)
        result = json.loads(store.restore.result_json)
        assert result["safety_attempt"]["drain"] == "confirmed"
        assert result["safety_attempt"]["bounds_seconds"] == {
            "overall": 110.0,
            "work": 40.0,
            "verification": 60.0,
            "cancel_drain": 5.0,
        }
        if not accepted:
            assert result["reason"] and result["original_reason"] == "original_obligation"
            assert json.loads(store.restore.last_observed_json)["ok"] is False
        columns = [
            dict(row._mapping)
            for row in (
                await db_session.execute(
                    text(
                        "SELECT ts, zone1_target_temp, "
                        "native_policy_provenance->'timing' AS timing "
                        "FROM device_status WHERE device_id=:device_id "
                        "AND native_policy_provenance IS NOT NULL ORDER BY ts"
                    ),
                    {"device_id": store.device_id},
                )
            ).all()
        ]
        assert columns and all(row["zone1_target_temp"] == 35 for row in columns)
        assert all(
            row["timing"][remote_clock] is not None
            and row["timing"]["retrieved_at" if remote_clock == "observed_at" else "observed_at"]
            is None
            for row in columns
        )
        _s1_repair2_receipt(
            f"independent-boundary-{completion_path}-{remote_clock}-{offset_microseconds}",
            {
                "criteria": "AC-R01",
                "completion_path": completion_path,
                "remote_clock": remote_clock,
                "settlement_offset_microseconds": offset_microseconds,
                "actual_device_target": store.device.zones[1].heat_target_temperature,
                "baseline": 35,
                "restore_id": store.restore.id,
                "claim_token": store.restore.safety_claim_token,
                "persisted_columns": columns,
                "status": store.restore.status,
                "success_audits": len(audits),
                "write_attempts": store.device.set_temperature.await_count,
                "verification_checkpoints": [read.checkpoint_seconds for read in reads],
                "result": result,
            },
        )

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize(
        "failure",
        [
            "normal",
            "acquisition_query_failure",
            "acquired_lost_ack",
            "unlock_aborted",
            "invalidation_failure",
            "invalidation_and_sync_failure",
            "cleanup_cancel",
        ],
    )
    async def test_repair2_real_pool_cleanup_after_query_or_ack_failure(
        self, s1_owned_restore, monkeypatch, failure
    ):
        from packages.core import safety_attempts

        store = s1_owned_restore
        device_id = store.device_id + "-repair2-pool-" + failure
        pooled = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
            connect_args={"timeout": 2, "command_timeout": 5},
        )
        events, invalidations, closes = [], [], []
        owned = {}
        entered, release = asyncio.Event(), asyncio.Event()
        worker = None
        attempt = None

        def invalidated(connection, record, exception):
            invalidations.append(connection)
            events.append("physical_invalidate")

        def closed(connection, record):
            closes.append(connection)
            events.append("physical_close")

        def detached_closed(connection):
            closes.append(connection)
            events.append("physical_close_detached")

        event.listen(pooled.sync_engine, "invalidate", invalidated)
        event.listen(pooled.sync_engine, "close", closed)
        event.listen(pooled.sync_engine, "close_detached", detached_closed)
        try:
            async with pooled.connect() as connection:
                original_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
                connection_type = type(connection)
            original_execute = connection_type.execute
            original_invalidate = connection_type.invalidate
            original_close = connection_type.close
            original_run_sync = connection_type.run_sync

            async def execute(connection, statement, parameters=None, **keywords):
                sql = str(statement)
                if "pg_try_advisory_lock" in sql and parameters[
                    "key"
                ] == safety_attempts.device_attempt_key(device_id):
                    owned["connection"] = connection
                    owned["attempt"] = safety_attempts._DEVICE_OWNERS[device_id]
                    if failure == "acquisition_query_failure":
                        events.append("acquisition_query_failure")
                        return await original_execute(connection, text("SELECT 1/0"))
                    result = await original_execute(connection, statement, parameters, **keywords)
                    if failure == "acquired_lost_ack":
                        assert result.scalar_one() is True
                        events.append("acquired_lock_lost_ack")
                        raise TimeoutError("acquisition acknowledgement lost")
                    return result
                if connection is owned.get("connection") and "pg_advisory_unlock" in sql:
                    events.append("unlock_attempt")
                    if failure == "cleanup_cancel":
                        entered.set()
                        await release.wait()
                    if failure not in ("normal", "cleanup_cancel"):
                        try:
                            await original_execute(connection, text("SELECT 1/0"))
                        except DBAPIError:
                            pass
                        events.append("unlock_transaction_aborted")
                    return await original_execute(connection, statement, parameters, **keywords)
                return await original_execute(connection, statement, parameters, **keywords)

            async def invalidate(connection, *arguments, **keywords):
                if connection is owned.get("connection"):
                    events.append("invalidate_attempt")
                    if failure in ("invalidation_failure", "invalidation_and_sync_failure"):
                        raise RuntimeError("async invalidation failure")
                return await original_invalidate(connection, *arguments, **keywords)

            async def run_sync(connection, callback, *arguments, **keywords):
                if connection is owned.get("connection"):
                    if (
                        failure == "invalidation_and_sync_failure"
                        and "invalidate" in callback.__code__.co_names
                    ):
                        events.append("sync_invalidation_failure")
                        raise RuntimeError("sync invalidation failure")
                    if "detach" in callback.__code__.co_names:
                        events.append("detach_attempt")
                return await original_run_sync(connection, callback, *arguments, **keywords)

            async def close(connection):
                if connection is owned.get("connection"):
                    events.append("owned_close_attempt")
                await original_close(connection)

            with monkeypatch.context() as failed_queries:
                failed_queries.setattr(connection_type, "execute", execute)
                failed_queries.setattr(connection_type, "invalidate", invalidate)
                failed_queries.setattr(connection_type, "close", close)
                failed_queries.setattr(connection_type, "run_sync", run_sync)
                if failure in ("acquisition_query_failure", "acquired_lost_ack"):
                    exception = (
                        DBAPIError if failure == "acquisition_query_failure" else TimeoutError
                    )
                    with pytest.raises(exception):
                        await safety_attempts.SafetyAttempt.acquire(device_id, pooled.connect)
                    attempt = owned["attempt"]
                else:
                    attempt = await safety_attempts.SafetyAttempt.acquire(device_id, pooled.connect)
                    assert attempt is not None
                    if failure == "cleanup_cancel":
                        worker = asyncio.create_task(attempt.release())
                        await asyncio.wait_for(entered.wait(), 2)
                        assert attempt._lock.locked() and pooled.sync_engine.pool.checkedout() == 1
                        assert (
                            await safety_attempts.SafetyAttempt.acquire(device_id, pooled.connect)
                            is None
                        )
                        worker.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(worker, 2)
                    else:
                        assert await attempt.release()
                assert attempt.connection.closed and attempt._released and attempt._drained
                assert all(task.done() for task in attempt._tasks)
                assert (
                    device_id not in safety_attempts._DEVICE_OWNERS and not attempt._lock.locked()
                )
                assert pooled.sync_engine.pool.checkedout() == 0
                assert events.count("unlock_attempt") == events.count("owned_close_attempt") == 1
                if failure == "normal":
                    assert invalidations == closes == []
                elif failure == "invalidation_and_sync_failure":
                    assert invalidations == [] and len(closes) == 1
                    assert events.index("detach_attempt") < events.index("owned_close_attempt")
                    assert events.index("owned_close_attempt") < events.index(
                        "physical_close_detached"
                    )
                    assert attempt.metadata()["connection_pool_detached"] is True
                    assert attempt.metadata()["connection_release_proof"] == "detached_closed"
                else:
                    assert len(invalidations) == len(closes) == 1
                    assert invalidations[0] is closes[0]
                    assert events.index("physical_close") < events.index("owned_close_attempt")
                before_second_release = list(events)
                assert await attempt.release()
                assert events == before_second_release
            async with pooled.connect() as replacement:
                replacement_pid = await replacement.scalar(text("SELECT pg_backend_pid()"))
                assert (replacement_pid == original_pid) is (failure == "normal")
                assert await replacement.scalar(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid())"
                    )
                )
                assert await replacement.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": attempt.key}
                )
                assert await replacement.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": attempt.key}
                )
                await replacement.commit()
            _s1_repair2_receipt(
                "pool-" + failure,
                {
                    "criteria": "AC-R02",
                    "failure": failure,
                    "events": events,
                    "original_backend_pid": original_pid,
                    "replacement_backend_pid": replacement_pid,
                    "pool_checked_out": pooled.sync_engine.pool.checkedout(),
                    "connection_closed": attempt.connection.closed,
                    "owned_tasks_done": all(task.done() for task in attempt._tasks),
                    "process_owner_released": device_id not in safety_attempts._DEVICE_OWNERS,
                    "release_proof": attempt.metadata()["connection_release_proof"],
                },
            )
        finally:
            release.set()
            if worker is not None:
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if attempt is not None and not attempt._released:
                await attempt.release()
            event.remove(pooled.sync_engine, "invalidate", invalidated)
            event.remove(pooled.sync_engine, "close", closed)
            event.remove(pooled.sync_engine, "close_detached", detached_closed)
            await pooled.dispose()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_genuine_legacy_restore_dispatches_with_real_claim_and_verification(
        self, s1_owned_restore, db_session
    ):
        store = s1_owned_restore
        assert store.source.executed_at is None
        assert store.restore.safety_claim_token is None

        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "executed"
        assert isinstance(store.restore.safety_claim_token, uuid.UUID)
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        assert store.device.refresh_data.await_count == 2
        reads = (
            (
                await db_session.execute(
                    select(ExecutorVerificationReadRecord).order_by(
                        ExecutorVerificationReadRecord.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [read.checkpoint_seconds for read in reads] == [15, 60]
        audits = (await db_session.execute(select(AuditLogRecord))).scalars().all()
        assert len(audits) == 1
        assert audits[0].result == "success" and audits[0].target_device == store.device_id
        metadata = json.loads(store.restore.result_json)["safety_attempt"]
        assert metadata["drain"] == "confirmed"
        assert metadata["claim_token"] == str(store.restore.safety_claim_token)
        assert metadata["live_read"]["observed_at"] is None
        assert metadata["checkpoint_origin"] == "sdk_restore_call_entry"
        assert (
            metadata["live_read"]["started_monotonic"]
            >= metadata["dispatch_started_monotonic"] + 60
        )
        assert (
            metadata["live_read"]["finished_monotonic"]
            < metadata["verification_deadline_monotonic"]
        )
        assert (
            metadata["verification_deadline_monotonic"] - metadata["dispatch_settled_monotonic"]
            <= 60 + 1e-6
        )
        assert await store.wrapper._redis.dbsize() > 0

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_fence_quota_wait_blocks_reclaim(self, s1_owned_restore, db_session):
        store = s1_owned_restore
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait_for_quota():
            entered.set()
            await release.wait()

        store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_quota)
        await _s1_run_fenced_wait(store, db_session, entered, release)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_fence_auth_retry_blocks_reclaim(
        self, s1_owned_restore, db_session, monkeypatch
    ):
        from aioaquarea import AquareaEnvironment
        from aioaquarea.api_client import AquareaAPIClient
        from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes

        store = s1_owned_restore
        entered, release = asyncio.Event(), asyncio.Event()
        response = SimpleNamespace(
            content_type="application/json", status=200, json=AsyncMock(return_value={})
        )
        sdk_session = SimpleNamespace(request=AsyncMock(return_value=response))
        api = AquareaAPIClient(
            sdk_session, SimpleNamespace(), SimpleNamespace(), AquareaEnvironment.PRODUCTION
        )
        api.look_for_errors = AsyncMock(
            side_effect=[
                [AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED, "test expired")],
                [],
            ]
        )
        monkeypatch.setattr(
            "aioaquarea.api_client.PanasonicRequestHeader.get", AsyncMock(return_value={})
        )

        async def recover():
            entered.set()
            await release.wait()

        recovery = AsyncMock(side_effect=recover)
        api.set_refresh_authentication_callback(recovery)

        async def sdk_command(temperature, *, zone_id):
            await store.clock.dispatch(2)
            await api.request("POST", url="/offline-restore", json={"temperature": temperature})
            store.device.zones[zone_id].heat_target_temperature = temperature

        store.device.set_temperature = AsyncMock(side_effect=sdk_command)
        await _s1_run_fenced_wait(store, db_session, entered, release)
        recovery.assert_awaited_once()
        assert sdk_session.request.await_count == 2

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("transition", ["progress", "complete", "requeue", "reservation"])
    async def test_owned_old_worker_cannot_mutate_or_reserve(
        self, s1_owned_restore, db_session, transition
    ):
        from packages.optimizer.actions import VerifyResult

        store = s1_owned_restore
        executor = store.executor()
        claimed = await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
        assert claimed is not None
        attempt = executor._attempt_for(claimed)
        replacement_token = uuid.uuid4()
        async with get_session() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == claimed.id)
                .values(safety_claim_token=replacement_token)
            )
        try:
            if transition in {"complete", "requeue"}:
                assert await attempt.drain()
            with pytest.raises(PermissionError):
                if transition == "progress":
                    await executor._store_verification_progress(claimed, 1, VerifyResult(ok=True))
                elif transition == "complete":
                    await executor._mark_verified(claimed, 1, VerifyResult(ok=True))
                elif transition == "requeue":
                    await executor._requeue_safety_action(claimed, "stale_worker")
                else:
                    await executor._reserve_verification_read(claimed, "safety", "safety", 15)
            await db_session.refresh(store.restore)
            assert store.restore.safety_claim_token == replacement_token
            assert store.restore.status == "executing"
            assert store.restore.result_json == claimed.result_json
            assert not (await db_session.execute(select(ExecutorVerificationReadRecord))).all()
            assert not (await db_session.execute(select(AuditLogRecord))).all()
            store.device.refresh_data.assert_not_awaited()
            store.device.set_temperature.assert_not_awaited()
        finally:
            assert await attempt.release()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_verification_reservation_wait_is_fenced(
        self, s1_owned_restore, db_session
    ):
        store = s1_owned_restore
        ledger_entered, quota_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_reserve = store.wrapper._read_quota.reserve

        async def wait_for_quota(*arguments, **keywords):
            quota_entered.set()
            await release.wait()
            return await original_reserve(*arguments, **keywords)

        store.wrapper._read_quota.reserve = AsyncMock(side_effect=wait_for_quota)

        def observe_ledger(connection, cursor, statement, parameters, context, executemany):
            if statement == "SELECT pg_advisory_xact_lock(1163280966, 1)":
                ledger_entered.set()

        async with engine.connect() as ledger_connection:
            await ledger_connection.execute(text("SELECT pg_advisory_xact_lock(1163280966, 1)"))
            event.listen(engine.sync_engine, "before_cursor_execute", observe_ledger)
            worker = asyncio.create_task(store.executor().execute_due_actions())
            try:
                await asyncio.wait_for(ledger_entered.wait(), 5)
                token = await _s1_assert_database_fence(store, row_unlocked=False)
                assert not (await db_session.execute(select(ExecutorVerificationReadRecord))).all()
                await ledger_connection.commit()
                await asyncio.wait_for(quota_entered.wait(), 5)
                replacement_token = uuid.uuid4()
                async with get_session() as session:
                    await session.execute(
                        update(PlanActionRecord)
                        .where(PlanActionRecord.id == store.restore.id)
                        .values(safety_claim_token=replacement_token)
                    )
                release.set()
                await asyncio.wait_for(worker, 5)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", observe_ledger)
                await ledger_connection.rollback()
                release.set()
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        await db_session.refresh(store.restore)
        assert store.restore.status == "executing"
        assert store.restore.safety_claim_token == replacement_token
        assert json.loads(store.restore.result_json)["safety_attempt"]["claim_token"] == str(token)
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert len(reads) == 1 and reads[0].checkpoint_seconds == 15
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        store.device.refresh_data.assert_not_awaited()
        store.device.set_temperature.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_missing_source_clock_uses_postclaim_live_proof(
        self, s1_owned_restore, db_session
    ):
        store = s1_owned_restore
        store.device.zones[1].heat_target_temperature = 35

        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "executed" and store.source.executed_at is None
        store.device.set_temperature.assert_not_awaited()
        store.device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert len(reads) == 1 and reads[0].checkpoint_seconds == 15
        assert json.loads(store.restore.result_json)["safety_attempt"]["live_read"]["live"] is True

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("defect", ["cached", "observed_future", "retrieved_future"])
    async def test_future_or_cached_live_proof_cannot_complete_restore(
        self, s1_owned_restore, db_session, defect
    ):
        store = s1_owned_restore
        store.device.zones[1].heat_target_temperature = 35
        original = store.wrapper._refresh_restore_status

        async def invalid_proof(attempt):
            proof = await original(attempt)
            future = proof.read_finished_at + dt.timedelta(seconds=1)
            if defect == "cached":
                return replace(proof, live=False)
            if defect == "observed_future":
                return replace(proof, observed_at=future)
            return replace(proof, retrieved_at=future)

        store.wrapper._refresh_restore_status = invalid_proof
        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "pending"
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        store.device.set_temperature.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at", "both"])
    async def test_known_stale_clocks_never_commit_restore_success(
        self, s1_owned_restore, db_session, clock_name
    ):
        store = s1_owned_restore
        store.device.zones[1].heat_target_temperature = 35
        original = store.wrapper._refresh_restore_status

        async def stale_proof(attempt):
            proof = await original(attempt)
            stale = proof.read_finished_at - dt.timedelta(days=1)
            clocks = (
                {"observed_at": stale, "retrieved_at": stale}
                if clock_name == "both"
                else {clock_name: stale}
            )
            return replace(proof, **clocks)

        store.wrapper._refresh_restore_status = stale_proof
        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "pending" and store.restore.executed_at is None
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert [read.checkpoint_seconds for read in reads] == [15]
        metadata = json.loads(store.restore.result_json)["safety_attempt"]
        assert "live_read" not in metadata and metadata["drain"] == "confirmed"
        store.device.set_temperature.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at", "both"])
    async def test_postcall_stale_proof_preserves_obligation_without_success_audit(
        self, s1_owned_restore, db_session, clock_name
    ):
        store = s1_owned_restore
        original = store.wrapper._refresh_restore_status
        proof_count = 0

        async def postcall_stale_proof(attempt):
            nonlocal proof_count
            proof = await original(attempt)
            proof_count += 1
            if proof_count == 1:
                return proof
            stale = proof.read_finished_at - dt.timedelta(days=1)
            clocks = (
                {"observed_at": stale, "retrieved_at": stale}
                if clock_name == "both"
                else {clock_name: stale}
            )
            return replace(proof, **clocks)

        store.wrapper._refresh_restore_status = postcall_stale_proof
        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "pending" and store.restore.executed_at is None
        assert isinstance(store.restore.safety_claim_token, uuid.UUID)
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        assert proof_count == 2 and store.device.refresh_data.await_count == 2
        reads = (
            (
                await db_session.execute(
                    select(ExecutorVerificationReadRecord).order_by(
                        ExecutorVerificationReadRecord.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [read.checkpoint_seconds for read in reads] == [15, 60]
        assert all(read.action_id == store.restore.id and read.lane == "safety" for read in reads)
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        result = json.loads(store.restore.result_json)
        assert result["reason"] == "_VerificationEvidenceUnavailable"
        assert result["safety_attempt"]["drain"] == "confirmed"
        assert result["safety_attempt"].get("live_read", {}).get("checkpoint") != 60

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("past_deadline_seconds", [0.0, 0.001])
    async def test_checkpoint_wait_exhaustion_cannot_add_reservation_or_read(
        self, s1_owned_restore, db_session, past_deadline_seconds
    ):
        store = s1_owned_restore
        executor = store.executor()
        waits = 0
        original_read = store.wrapper._refresh_restore_status
        admitted_remaining = []
        owned_attempts = []

        async def read(attempt):
            remaining = attempt.remaining()
            assert remaining > 0
            admitted_remaining.append(remaining)
            owned_attempts.append(attempt)
            return await original_read(attempt)

        async def delayed_sleep(seconds):
            nonlocal waits
            waits += 1
            await store.clock.sleep(seconds)
            if waits == 4:
                attempt = owned_attempts[0]
                store.clock.advance(
                    max(0.0, attempt.verify_deadline - store.clock.monotonic())
                    + past_deadline_seconds
                )
                await asyncio.sleep(0)

        store.wrapper._refresh_restore_status = read
        executor._sleep = delayed_sleep
        await executor.execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "pending" and store.restore.executed_at is None
        assert isinstance(store.restore.safety_claim_token, uuid.UUID)
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        assert waits == 4 and len(admitted_remaining) == 1
        assert store.device.refresh_data.await_count == 1
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert [record.checkpoint_seconds for record in reads] == [15]
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        result = json.loads(store.restore.result_json)
        assert result["reason"] == "SafetyAttemptDeadline"
        metadata = result["safety_attempt"]
        assert store.clock.monotonic() >= metadata["verification_deadline_monotonic"]
        assert metadata["checkpoints"] == [15] and metadata["drain"] == "confirmed"

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("status", ["executing", "dispatched"])
    async def test_restore_null_token_requires_legacy_drain(
        self, s1_owned_restore, db_session, status
    ):
        store = s1_owned_restore
        store.restore.status = status
        store.restore.safety_claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        await db_session.commit()

        assert (
            await store.executor()._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
            is None
        )

        await db_session.refresh(store.restore)
        assert store.restore.status == status and store.restore.safety_claim_token is None
        assert not (await db_session.execute(select(ExecutorVerificationReadRecord))).all()
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        store.device.refresh_data.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_cancel_drains_before_reclaim(self, s1_owned_restore, db_session):
        store = s1_owned_restore
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def owned_sdk_call(temperature, *, zone_id):
            assert temperature == 35 and zone_id == 1
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()

        store.device.set_temperature = AsyncMock(side_effect=owned_sdk_call)
        worker = asyncio.create_task(store.executor().execute_due_actions())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            token = await _s1_assert_database_fence(store)
            worker.cancel()
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not worker.done()
            assert await _s1_assert_database_fence(store) == token
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 5)
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await db_session.refresh(store.restore)
        assert store.restore.status == "pending"
        assert json.loads(store.restore.result_json)["safety_attempt"]["drain"] == "confirmed"
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        async with get_session() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == store.restore.id)
                .values(safety_next_retry_at=None)
            )
        replacement = store.executor()
        claimed = await replacement._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
        assert claimed is not None and claimed.safety_claim_token != token
        attempt = replacement._attempt_for(claimed)
        try:
            assert await attempt.drain()
            await replacement._requeue_safety_action(claimed, "test_confirmed_drain_cleanup")
        finally:
            assert await attempt.release()

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("failure", ["failed_drain", "lock_loss"])
    async def test_restore_failed_drain_or_lock_loss_defers(
        self, s1_owned_restore, db_session, monkeypatch, failure
    ):
        from packages.core import safety_attempts

        store = s1_owned_restore
        store.expect_owner_blocked = True
        monkeypatch.setattr(safety_attempts, "DRAIN_SECONDS", 0.1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def owned_sdk_call(temperature, *, zone_id):
            assert temperature == 35 and zone_id == 1
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        store.device.set_temperature = AsyncMock(side_effect=owned_sdk_call)
        executor = store.executor()
        worker = asyncio.create_task(executor.execute_due_actions())
        attempt = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await db_session.refresh(store.restore)
            token = store.restore.safety_claim_token
            attempt = executor._attempt_for(store.restore)
            if failure == "failed_drain":
                worker.cancel()
            else:
                async with attempt._connection_lock:
                    await attempt.connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": attempt.key}
                    )
                    await attempt.connection.commit()
            result = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 5)
            if failure == "failed_drain":
                assert isinstance(result[0], asyncio.CancelledError)
            await db_session.refresh(store.restore)
            assert store.restore.status == "executing" and store.restore.safety_claim_token == token
            assert json.loads(store.restore.result_json)["safety_attempt"]["drain"] == "unproven"
            assert not (await db_session.execute(select(AuditLogRecord))).all()
            assert (
                await store.executor()._claim_due_safety_action(
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
                )
                is None
            )
            assert attempt.connection.closed or attempt.connection.invalidated
            assert safety_attempts._DEVICE_OWNERS[store.device_id] is attempt
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if attempt is not None:
                await asyncio.wait_for(asyncio.gather(*attempt._tasks, return_exceptions=True), 5)
                assert all(task.done() for task in attempt._tasks)
        with monkeypatch.context() as restarted_process:
            restarted_process.setattr(safety_attempts, "_DEVICE_OWNERS", {})
            restarted_process.setattr(safety_attempts, "_DEVICE_LOCKS", {})
            assert (
                await store.executor()._claim_due_safety_action(
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
                )
                is None
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_ambiguous_dispatch_verifies_before_duplicate(
        self, s1_owned_restore, db_session
    ):
        store = s1_owned_restore

        async def lost_acknowledgement(temperature, *, zone_id):
            await store.clock.dispatch(2)
            store.device.zones[zone_id].heat_target_temperature = temperature
            raise TimeoutError("test acknowledgement lost")

        store.device.set_temperature = AsyncMock(side_effect=lost_acknowledgement)
        await store.executor().execute_due_actions()

        await db_session.refresh(store.restore)
        assert store.restore.status == "executed"
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert [read.checkpoint_seconds for read in reads] == [15, 60]
        assert (
            json.loads(store.restore.result_json)["safety_attempt"]["dispatch_outcome"]
            == "ambiguous"
        )

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("wait_at", ["quota", "sdk_auth_retry"])
    async def test_restore_cancelled_wait_rejects_stale_sdk_permit(
        self, s1_owned_restore, db_session, monkeypatch, wait_at
    ):
        from aioaquarea import AquareaEnvironment
        from aioaquarea.api_client import AquareaAPIClient
        from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes

        store = s1_owned_restore
        entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        response = SimpleNamespace(
            content_type="application/json", status=200, json=AsyncMock(return_value={})
        )
        sdk_session = SimpleNamespace(request=AsyncMock(return_value=response))
        api = AquareaAPIClient(
            sdk_session, SimpleNamespace(), SimpleNamespace(), AquareaEnvironment.PRODUCTION
        )
        api.look_for_errors = AsyncMock(
            side_effect=[
                [AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED, "test expired")],
                [],
            ]
        )
        monkeypatch.setattr(
            "aioaquarea.api_client.PanasonicRequestHeader.get", AsyncMock(return_value={})
        )

        async def wait_for_admission():
            entered.set()
            try:
                await release.wait()
            finally:
                settled.set()

        api.set_refresh_authentication_callback(wait_for_admission)
        if wait_at == "quota":
            store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_admission)

        async def sdk_command(temperature, *, zone_id):
            await store.clock.dispatch(2)
            await api.request("POST", url="/offline-restore", json={"temperature": temperature})
            store.device.zones[zone_id].heat_target_temperature = temperature

        store.device.set_temperature = AsyncMock(side_effect=sdk_command)
        executor = store.executor()
        worker = asyncio.create_task(executor.execute_due_actions())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            token = await _s1_assert_database_fence(store)
            await db_session.refresh(store.restore)
            old_action = PlanActionRecord(
                **{
                    column.name: getattr(store.restore, column.name)
                    for column in PlanActionRecord.__table__.columns
                }
            )
            old_owner = executor._attempt_for(old_action)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 5)
            assert settled.is_set() and all(task.done() for task in old_owner._tasks)
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        await db_session.refresh(store.restore)
        assert store.restore.status == "pending" and store.restore.safety_claim_token == token
        assert json.loads(store.restore.result_json)["safety_attempt"]["drain"] == "confirmed"
        assert sdk_session.request.await_count == (0 if wait_at == "quota" else 1)
        assert store.device.set_temperature.await_count == (0 if wait_at == "quota" else 1)
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert len(reads) == 1 and reads[0].checkpoint_seconds == 15
        assert not (await db_session.execute(select(AuditLogRecord))).all()
        async with get_session() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == store.restore.id)
                .values(safety_next_retry_at=None)
            )
        replacement = store.executor()
        claimed = await replacement._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
        assert claimed is not None and claimed.safety_claim_token != token
        attempt = replacement._attempt_for(claimed)
        before = (
            store.wrapper._read_limiter.acquire.await_count,
            store.wrapper._write_limiter.acquire.await_count,
            store.device.refresh_data.await_count,
            sdk_session.request.await_count,
        )
        try:
            with pytest.raises(PermissionError):
                await store.wrapper._dispatch_linked_restore(
                    old_action, store.source, attempt=old_owner
                )
            assert before == (
                store.wrapper._read_limiter.acquire.await_count,
                store.wrapper._write_limiter.acquire.await_count,
                store.device.refresh_data.await_count,
                sdk_session.request.await_count,
            )
            assert await attempt.drain()
            await replacement._requeue_safety_action(claimed, "test_cancelled_wait_cleanup")
        finally:
            assert await attempt.release()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_stale_token_after_write_quota_cannot_dispatch(
        self, s1_owned_restore, db_session
    ):
        store = s1_owned_restore
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait_for_quota():
            entered.set()
            await release.wait()

        store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_quota)
        worker = asyncio.create_task(store.executor().execute_due_actions())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            token = await _s1_assert_database_fence(store)
            await db_session.refresh(store.restore)
            progress = store.restore.result_json
            replacement_token = uuid.uuid4()
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == store.restore.id)
                    .values(safety_claim_token=replacement_token)
                )
            release.set()
            await asyncio.wait_for(worker, 5)
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        await db_session.refresh(store.restore)
        assert store.restore.status == "executing"
        assert store.restore.safety_claim_token == replacement_token != token
        assert store.restore.result_json == progress
        store.device.set_temperature.assert_not_awaited()
        store.device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
        reads = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
        assert len(reads) == 1 and reads[0].checkpoint_seconds == 15
        assert not (await db_session.execute(select(AuditLogRecord))).all()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_restore_different_devices_can_dispatch_in_parallel(
        self, s1_owned_restore, db_session
    ):
        from packages.optimizer.executor_core import LearningModeState
        from tests.test_safety_reverts import _S1RestoreStore

        store = s1_owned_restore
        other = _S1RestoreStore(clock=store.clock)
        entered, release = asyncio.Event(), asyncio.Event()
        first_dispatched, other_dispatched, sdk_release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        verification_ready = asyncio.Event()
        verification_started = set()
        original_factory = store.executor
        original_first_command = store.device.set_temperature
        original_other_command = other.device.set_temperature

        def sleep_for(label):
            admission = True

            async def sleep(seconds):
                nonlocal admission
                if admission:
                    admission = False
                    await store.clock.sleep(seconds)
                    return
                target = store.clock.monotonic() + seconds
                verification_started.add(label)
                if len(verification_started) == 2:
                    verification_ready.set()
                await verification_ready.wait()
                await store.clock.sleep(max(0, target - store.clock.monotonic()))

            return sleep

        def executor():
            current = original_factory()
            current._sleep = sleep_for("first")
            return current

        async def first_command(temperature, *, zone_id):
            await original_first_command(temperature, zone_id=zone_id)
            first_dispatched.set()
            await sdk_release.wait()

        async def other_command(temperature, *, zone_id):
            await original_other_command(temperature, zone_id=zone_id)
            other_dispatched.set()
            await sdk_release.wait()

        store.executor = executor
        store.device.set_temperature = AsyncMock(side_effect=first_command)
        other.device.set_temperature = AsyncMock(side_effect=other_command)

        async def wait_for_quota():
            entered.set()
            await release.wait()

        store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_quota)
        worker = asyncio.create_task(store.executor().execute_due_actions())
        independent_worker = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            token = await _s1_assert_database_fence(store)
            other.source.id = None
            other.source.plan_id = store.source.plan_id
            db_session.add(other.source)
            await db_session.flush()
            other.restore.id = None
            other.restore.plan_id = store.source.plan_id
            other.restore.reverts_action_id = other.source.id
            db_session.add_all([other.restore, other.status])
            await db_session.commit()
            independent = PlanExecutor(
                other.wrapper,
                session_factory=get_session,
                connection_factory=engine.connect,
                sleep=sleep_for("other"),
                learning_check=AsyncMock(return_value=LearningModeState.INACTIVE),
                device_quality_check=AsyncMock(
                    return_value={"ready": True, "reasons": [], "threshold_seconds": 900}
                ),
            )
            independent_worker = asyncio.create_task(independent.execute_due_actions())
            await asyncio.wait_for(other_dispatched.wait(), 5)
            other.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
            store.device.set_temperature.assert_not_awaited()
            assert await _s1_assert_database_fence(store) == token
            release.set()
            await asyncio.wait_for(first_dispatched.wait(), 5)
            sdk_release.set()
            await asyncio.wait_for(asyncio.gather(worker, independent_worker), 5)
        finally:
            release.set()
            sdk_release.set()
            verification_ready.set()
            workers = [worker] + ([independent_worker] if independent_worker is not None else [])
            for owned_worker in workers:
                if not owned_worker.done():
                    owned_worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        await db_session.refresh(other.restore)
        assert other.restore.status == "executed"
        other.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        await db_session.refresh(store.restore)
        assert store.restore.status == "executed"
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        audits = (await db_session.execute(select(AuditLogRecord))).scalars().all()
        assert len(audits) == 2 and {audit.target_device for audit in audits} == {
            store.device_id,
            other.device_id,
        }

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("unlock_report", [True, False])
    async def test_restore_dedicated_connection_is_clean_before_pool_reuse(
        self, s1_owned_restore, monkeypatch, unlock_report
    ):
        from packages.core.safety_attempts import SafetyAttempt

        store = s1_owned_restore
        pooled = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
            connect_args={"timeout": 2, "command_timeout": 5},
        )
        attempt = None
        try:
            attempt = await SafetyAttempt.acquire(store.device_id + "-pool", pooled.connect)
            assert attempt is not None
            # The fixture installs _S1Clock into safety_attempts; deadlines share its epoch.
            deadline = store.clock.monotonic() + 2
            result = await attempt._query("SELECT pg_backend_pid()", {}, deadline)
            original_pid = result.scalar_one()
            connection_type = type(attempt.connection)
            original_execute = connection_type.execute

            async def execute(connection, statement, parameters=None, **keywords):
                if connection is attempt.connection and "pg_advisory_unlock" in str(statement):
                    return SimpleNamespace(scalar_one=lambda: False)
                return await original_execute(connection, statement, parameters, **keywords)

            with monkeypatch.context() as unlock_failure:
                if not unlock_report:
                    unlock_failure.setattr(connection_type, "execute", execute)
                assert await attempt.release()
            assert attempt.connection.closed
            async with pooled.connect() as reused:
                assert await reused.scalar(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype='advisory' AND pid=pg_backend_pid())"
                    )
                )
                current_pid = await reused.scalar(text("SELECT pg_backend_pid()"))
                if not unlock_report:
                    assert current_pid != original_pid
                acquired = await reused.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": attempt.key}
                )
                try:
                    assert acquired is True
                finally:
                    if acquired:
                        assert await reused.scalar(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": attempt.key}
                        )
                        await reused.commit()
        finally:
            if attempt is not None and not attempt._released:
                await attempt.release()
            await pooled.dispose()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_acquired_pool_connection_cancel_closes_before_replacement(
        self, s1_owned_restore, monkeypatch
    ):
        from packages.core import safety_attempts

        store = s1_owned_restore
        pooled = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
            connect_args={"timeout": 2, "command_timeout": 5},
        )
        acquired, release = asyncio.Event(), asyncio.Event()
        invalidations, closes = [], []
        bounded = safety_attempts.SafetyAttempt._bounded

        def invalidated(connection, record, exception):
            invalidations.append(connection)

        def closed(connection, record):
            closes.append(connection)

        async def pause_handoff(attempt, awaitable, deadline):
            result = await bounded(attempt, awaitable, deadline)
            if not acquired.is_set():
                acquired.set()
                await release.wait()
            return result

        event.listen(pooled.sync_engine, "invalidate", invalidated)
        event.listen(pooled.sync_engine, "close", closed)
        monkeypatch.setattr(safety_attempts.SafetyAttempt, "_bounded", pause_handoff)
        device_id = store.device_id + "-cancel-pool"
        worker = asyncio.create_task(
            safety_attempts.SafetyAttempt.acquire(device_id, pooled.connect)
        )
        try:
            await asyncio.wait_for(acquired.wait(), 5)
            attempt = safety_attempts._DEVICE_OWNERS[device_id]
            assert attempt.connection is not None and pooled.sync_engine.pool.checkedout() == 1
            assert attempt._lock.locked()
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 5)
            assert attempt.connection.closed and len(invalidations) == 1
            assert len(invalidations) == len(closes) == 1
            assert invalidations[0] is closes[0]
            assert pooled.sync_engine.pool.checkedout() == 0
            assert device_id not in safety_attempts._DEVICE_OWNERS
            assert not attempt._lock.locked() and all(task.done() for task in attempt._tasks)
            assert await attempt.release()
            assert len(invalidations) == len(closes) == 1
            async with pooled.connect() as replacement:
                assert await replacement.scalar(text("SELECT 1")) == 1
                assert await replacement.scalar(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid())"
                    )
                )
            assert store.wrapper._device is store.device
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            event.remove(pooled.sync_engine, "invalidate", invalidated)
            event.remove(pooled.sync_engine, "close", closed)
            await pooled.dispose()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_stored_future_sensor_rows_cannot_certify_diagnostics(
        self, s1_owned_restore, db_session
    ):
        from packages.core.sensor_diagnostics import summarize_sensor_diagnostics

        now = dt.datetime.now(dt.timezone.utc)
        readings = [
            IndoorTempReading(
                timestamp=now,
                device_id="s1-present-sensor",
                device_label="Present",
                room="Hall",
                temperature=20,
                is_stale=False,
            )
        ] + [
            IndoorTempReading(
                timestamp=now + dt.timedelta(seconds=1, microseconds=index),
                device_id="s1-future-sensor",
                device_label="Future",
                room="Living",
                temperature=28,
                is_stale=False,
            )
            for index in range(24)
        ]
        db_session.add_all(readings)
        await db_session.commit()
        stored = (await db_session.execute(select(IndoorTempReading))).scalars().all()
        assert len(stored) == 25

        result = summarize_sensor_diagnostics(stored, now=now)

        future = next(
            sensor for sensor in result["sensors"] if sensor["device_id"] == "s1-future-sensor"
        )
        assert future["fresh_samples"] == 0 and future["age_seconds"] is None
        assert future["state"] != "healthy" and future["median_temperature_c"] is None
        assert result["room_median_temperature_c"] == 20
        assert result["suggested_reference_sensor_id"] is None
        assert result["controls_unchanged"] is True

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("unresolved_status", ["pending", "executing", "dispatched"])
    async def test_migration_034_roundtrip_preserves_legacy_rows_and_sequences(
        self, legacy_restore_migration_database, unresolved_status
    ):
        # Every parameterization owns a new child database and never reaches forward-only 036.
        from tests.e2e.test_manual_cop_postgresql import GuardedChildEngine

        child = legacy_restore_migration_database
        # Every child mutation below, including _migration_sql, runs through the guard that
        # re-proves the original parent receipt/services and the child OID per connection.
        database_engine = child.engine
        assert isinstance(database_engine, GuardedChildEngine)
        head = _historical_ancestry()
        await child.alembic("upgrade", "033")
        await _migration_version(database_engine, "033")

        async def rows():
            async with database_engine.connect() as connection:
                return {
                    table: [
                        dict(row._mapping) for row in (await connection.execute(text(query))).all()
                    ]
                    for table, query in {
                        "plans": "SELECT * FROM plans ORDER BY id",
                        "actions": "SELECT * FROM plan_actions ORDER BY id",
                        "status": "SELECT * FROM device_status ORDER BY ts,device_id",
                    }.items()
                }

        async def sequences():
            async with database_engine.connect() as connection:
                names = (
                    (
                        await connection.execute(
                            text(
                                "SELECT sequencename FROM pg_sequences "
                                "WHERE schemaname='public' ORDER BY sequencename"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                snapshot = {}
                for name in names:
                    identifier = connection.dialect.identifier_preparer.quote(name)
                    snapshot[name] = tuple(
                        (
                            await connection.execute(
                                text(f"SELECT last_value,is_called FROM public.{identifier}")
                            )
                        ).one()
                    )
                return snapshot

        now = dt.datetime.now(dt.timezone.utc)
        device_id = "s1-migration-" + uuid.uuid4().hex
        plan_id = (
            await _migration_sql(
                database_engine,
                "INSERT INTO plans (horizon_start,horizon_end,plan_json,optimizer_version,status) "
                "VALUES (:now,:end,'{}','s1-migration-fixture','superseded') RETURNING id",
                {"now": now, "end": now + dt.timedelta(hours=1)},
            )
        ).scalar_one()
        source_id = (
            await _migration_sql(
                database_engine,
                "INSERT INTO plan_actions "
                "(plan_id,scheduled_ts,action_type,payload_json,device_id,status) "
                "VALUES (:plan,:now,'zone_temp_boost',:payload,:device,'executed') RETURNING id",
                {
                    "plan": plan_id,
                    "now": now,
                    "payload": '{"zone_id":1,"baseline_temperature":35,"temperature":37}',
                    "device": device_id,
                },
            )
        ).scalar_one()
        restore_id = (
            await _migration_sql(
                database_engine,
                "INSERT INTO plan_actions (plan_id,scheduled_ts,action_type,payload_json,"
                "device_id,status,reverts_action_id,safety_attempt_count) "
                "VALUES (:plan,:now,'zone_temp_restore',:payload,:device,:status,:source,3) "
                "RETURNING id",
                {
                    "plan": plan_id,
                    "now": now,
                    "payload": '{"zone_id":1,"temperature":35}',
                    "device": device_id,
                    "status": unresolved_status,
                    "source": source_id,
                },
            )
        ).scalar_one()
        await _migration_sql(
            database_engine,
            "INSERT INTO device_status (ts,device_id,zone1_target_temp) VALUES (:now,:device,35)",
            {"now": now, "device": device_id},
        )
        before_upgrade = await rows()
        sequence_before = await sequences()
        await child.alembic("upgrade", MIGRATION_UNDER_TEST)
        await _migration_version(database_engine, MIGRATION_UNDER_TEST)
        before = await rows()
        assert {
            **before,
            "actions": [
                {key: value for key, value in row.items() if key != "safety_claim_token"}
                for row in before["actions"]
            ],
        } == before_upgrade
        assert await sequences() == sequence_before
        assert len(before["actions"]) == 2 and len(before["plans"]) == 1
        assert all(row["safety_claim_token"] is None for row in before["actions"])
        assert before["actions"][1]["reverts_action_id"] == source_id
        with pytest.raises(RuntimeError, match="unresolved safety obligations"):
            await child.alembic("downgrade", "033")
        await _migration_version(database_engine, MIGRATION_UNDER_TEST)
        assert await rows() == before
        assert await sequences() == sequence_before
        await _migration_sql(
            database_engine,
            "UPDATE plan_actions SET status='executed' WHERE id=:id",
            {"id": restore_id},
        )
        before = await rows()
        legacy = {
            **before,
            "actions": [
                {key: value for key, value in row.items() if key != "safety_claim_token"}
                for row in before["actions"]
            ],
        }
        await child.alembic("downgrade", "033")
        await _migration_version(database_engine, "033")
        assert await rows() == legacy
        assert await sequences() == sequence_before
        for target in (MIGRATION_UNDER_TEST, head):
            await child.alembic("upgrade", target)
            await _migration_version(database_engine, target)
            assert await rows() == before
        assert await sequences() == sequence_before
        async with database_engine.connect() as connection:
            column = (
                await connection.execute(
                    text(
                        "SELECT udt_name,is_nullable,column_default "
                        "FROM information_schema.columns WHERE table_schema='public' "
                        "AND table_name='plan_actions' AND column_name='safety_claim_token'"
                    )
                )
            ).one()
            assert tuple(column) == ("uuid", "YES", None)
            provenance = (
                await connection.execute(
                    text(
                        "SELECT table_name,column_name,is_nullable "
                        "FROM information_schema.columns WHERE table_schema='public' "
                        "AND (table_name,column_name) IN (('consumption','source_timezone'),"
                        "('weather','units_schema'),('prices','interval_schema')) "
                        "ORDER BY table_name"
                    )
                )
            ).all()
            assert [tuple(row) for row in provenance] == [
                ("consumption", "source_timezone", "YES"),
                ("prices", "interval_schema", "YES"),
                ("weather", "units_schema", "YES"),
            ]
        await _migration_version(database_engine, HISTORICAL_HEAD)


class TestPhase2MigrationAcceptance:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_migration_030_up_down_up_creates_and_removes_verification_ledger(
        self, migration_case
    ):
        migration_engine, database_url = migration_case

        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "030")
        await _migration_version(migration_engine, "030")
        result = await _migration_sql(
            migration_engine, "SELECT to_regclass('public.executor_verification_reads')"
        )
        assert result.scalar_one() == "executor_verification_reads"

        await asyncio.to_thread(_run_alembic, database_url, "downgrade", "029")
        await _migration_version(migration_engine, "029")
        result = await _migration_sql(
            migration_engine, "SELECT to_regclass('public.executor_verification_reads')"
        )
        assert result.scalar_one() is None

        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "030")
        await _migration_version(migration_engine, "030")
        result = await _migration_sql(
            migration_engine, "SELECT to_regclass('public.executor_verification_reads')"
        )
        assert result.scalar_one() == "executor_verification_reads"

    async def _seed_legacy_event(
        self,
        migration_engine,
        *,
        setting: str | None = None,
        device_ids: tuple[str, ...] = ("device-a",),
    ) -> dt.datetime:
        started_at = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        if setting is not None:
            await _migration_sql(
                migration_engine,
                "INSERT INTO settings (key, value) VALUES ('shower_max_duration_minutes', :value)",
                {"value": setting},
            )
        await _migration_sql(
            migration_engine,
            "INSERT INTO shower_events (started_at, pre_shower_temp, status, peak_price_skipped) "
            "VALUES (:started_at, 55, 'active', false)",
            {"started_at": started_at},
        )
        for device_id in device_ids:
            await _migration_sql(
                migration_engine,
                "INSERT INTO device_status (ts, device_id) VALUES (:started_at, :device_id)",
                {"started_at": started_at, "device_id": device_id},
            )
        return started_at

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize(("setting", "expected_minutes"), [("45", 45), (None, 60), ("   ", 60)])
    async def test_P2_AC12_migration_backfills_expiry_and_requires_not_null(
        self, migration_case, setting, expected_minutes
    ):
        migration_engine, database_url = migration_case
        started_at = await self._seed_legacy_event(migration_engine, setting=setting)

        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")

        async with migration_engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT expires_at FROM shower_events WHERE started_at = :started_at"),
                    {"started_at": started_at},
                )
            ).one()
            nullable = await connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'shower_events' AND column_name = 'expires_at'"
                )
            )
        assert row.expires_at == started_at + dt.timedelta(minutes=expected_minutes)
        assert nullable == "NO"

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("setting", ["abc", "0", "-5"])
    async def test_P2_AC12_migration_rejects_invalid_duration_and_rolls_back(
        self, migration_case, setting
    ):
        migration_engine, database_url = migration_case
        await self._seed_legacy_event(migration_engine, setting=setting)

        with pytest.raises(RuntimeError, match="positive"):
            await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")

        await _migration_version(migration_engine, "028")
        async with migration_engine.connect() as connection:
            columns = (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'plan_actions' AND column_name = 'reverts_action_id'"
                    )
                )
            ).all()
        assert columns == []

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC12_lock_timeout_aborts_upgrade_and_keeps_revision_028(self, migration_case):
        migration_engine, database_url = migration_case
        now = dt.datetime.now(dt.timezone.utc)
        async with migration_engine.begin() as connection:
            plan_id = await connection.scalar(
                text(
                    "INSERT INTO plans (horizon_start, horizon_end, plan_json, optimizer_version) "
                    "VALUES (:start, :end, '{}', 'lock-timeout-test') RETURNING id"
                ),
                {"start": now, "end": now + dt.timedelta(hours=1)},
            )
            for action_type in ("force_dhw_on", "force_dhw_off"):
                await connection.execute(
                    text(
                        "INSERT INTO plan_actions "
                        "(plan_id, scheduled_ts, action_type, payload_json, status) "
                        "VALUES (:plan_id, :scheduled_ts, :action_type, '{}', 'pending')"
                    ),
                    {
                        "plan_id": plan_id,
                        "scheduled_ts": now,
                        "action_type": action_type,
                    },
                )

        lock_engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with lock_engine.connect() as lock_connection:
                async with lock_connection.begin():
                    await lock_connection.execute(
                        text("LOCK TABLE plan_actions IN ACCESS SHARE MODE")
                    )
                    started = time.monotonic()
                    with pytest.raises(DBAPIError) as error:
                        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")
                    elapsed = time.monotonic() - started

                    assert getattr(error.value.orig, "sqlstate", None) == "55P03"
                    assert 4 <= elapsed < 15
                    await _migration_version(migration_engine, "028")

                    async with migration_engine.connect() as connection:
                        columns = (
                            await connection.execute(
                                text(
                                    "SELECT table_name, column_name "
                                    "FROM information_schema.columns "
                                    "WHERE table_schema = 'public' AND "
                                    "((table_name = 'plan_actions' AND column_name IN "
                                    "('reverts_action_id', 'safety_attempt_count', "
                                    "'safety_next_retry_at', 'safety_claimed_at')) OR "
                                    "(table_name = 'shower_events' AND column_name IN "
                                    "('device_id', 'expires_at', 'activation_action_id')))"
                                )
                            )
                        ).all()
                        indexes = (
                            await connection.execute(
                                text(
                                    "SELECT indexname FROM pg_indexes "
                                    "WHERE schemaname = 'public' AND indexname IN "
                                    "('uq_plan_actions_reverts_action_id', "
                                    "'ix_plan_actions_safety_due', 'ix_shower_events_open_expiry')"
                                )
                            )
                        ).all()
                        constraints = (
                            await connection.execute(
                                text(
                                    "SELECT constraint_name FROM information_schema.table_constraints "
                                    "WHERE table_schema = 'public' AND constraint_name IN "
                                    "('fk_plan_actions_reverts_action_id', "
                                    "'fk_shower_events_activation_action_id')"
                                )
                            )
                        ).all()
                    assert columns == []
                    assert indexes == []
                    assert constraints == []
        finally:
            await lock_engine.dispose()

        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")
        await _migration_version(migration_engine, "029")

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC12_migration_synthesizes_legacy_shower_obligation(self, migration_case):
        migration_engine, database_url = migration_case
        started_at = await self._seed_legacy_event(migration_engine)

        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")

        async with migration_engine.connect() as connection:
            event = (
                await connection.execute(
                    text(
                        "SELECT id, device_id, activation_action_id, expires_at "
                        "FROM shower_events WHERE started_at = :started_at"
                    ),
                    {"started_at": started_at},
                )
            ).one()
            actions = (
                await connection.execute(
                    text(
                        "SELECT action_type, reverts_action_id, scheduled_ts, status "
                        "FROM plan_actions ORDER BY id"
                    )
                )
            ).all()
            plan_version = await connection.scalar(
                text(
                    "SELECT optimizer_version FROM plans WHERE id = (SELECT plan_id FROM plan_actions LIMIT 1)"
                )
            )
        assert event.device_id == "device-a"
        assert event.activation_action_id is not None
        assert actions[0].action_type == "force_dhw_on"
        assert actions[0].status == "executed"
        assert actions[1].action_type == "force_dhw_off"
        assert (
            actions[1].reverts_action_id == actions[0].reverts_action_id
            or actions[1].reverts_action_id == event.activation_action_id
        )
        assert actions[1].scheduled_ts == event.expires_at
        assert actions[1].status == "pending"
        assert plan_version == "migration_029_legacy_shower"

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("device_ids", [(), ("device-a", "device-b")])
    async def test_P2_AC12_migration_aborts_without_exactly_one_device_match(
        self, migration_case, device_ids
    ):
        migration_engine, database_url = migration_case
        await self._seed_legacy_event(migration_engine, device_ids=device_ids)

        with pytest.raises(RuntimeError, match="cannot infer unique device_id"):
            await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")

        await _migration_version(migration_engine, "028")

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC12_unique_restore_source_index_rejects_duplicate(self, migration_case):
        migration_engine, database_url = migration_case
        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")
        now = dt.datetime.now(dt.timezone.utc)
        async with migration_engine.begin() as connection:
            plan_id = await connection.scalar(
                text(
                    "INSERT INTO plans (horizon_start, horizon_end, plan_json, optimizer_version) "
                    "VALUES (:start, :end, '{}', 'test') RETURNING id"
                ),
                {"start": now, "end": now + dt.timedelta(hours=1)},
            )
            source_id = await connection.scalar(
                text(
                    "INSERT INTO plan_actions (plan_id, scheduled_ts, action_type, payload_json, status) "
                    "VALUES (:plan_id, :scheduled_ts, 'force_dhw_on', '{}', 'executed') RETURNING id"
                ),
                {"plan_id": plan_id, "scheduled_ts": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO plan_actions (plan_id, reverts_action_id, scheduled_ts, action_type, "
                    "payload_json, status) VALUES (:plan_id, :source_id, :scheduled_ts, "
                    "'force_dhw_off', '{}', 'pending')"
                ),
                {"plan_id": plan_id, "source_id": source_id, "scheduled_ts": now},
            )
        with pytest.raises(IntegrityError):
            async with migration_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO plan_actions (plan_id, reverts_action_id, scheduled_ts, action_type, "
                        "payload_json, status) VALUES (:plan_id, :source_id, :scheduled_ts, "
                        "'force_dhw_off', '{}', 'pending')"
                    ),
                    {"plan_id": plan_id, "source_id": source_id, "scheduled_ts": now},
                )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC12_downgrade_requires_resolved_tagged_action(self, migration_case):
        migration_engine, database_url = migration_case
        await asyncio.to_thread(_run_alembic, database_url, "upgrade", "029")
        now = dt.datetime.now(dt.timezone.utc)
        async with migration_engine.begin() as connection:
            plan_id = await connection.scalar(
                text(
                    "INSERT INTO plans (horizon_start, horizon_end, plan_json, optimizer_version) "
                    "VALUES (:start, :end, '{}', 'test') RETURNING id"
                ),
                {"start": now, "end": now + dt.timedelta(hours=1)},
            )
            source_id = await connection.scalar(
                text(
                    "INSERT INTO plan_actions (plan_id, scheduled_ts, action_type, payload_json, status) "
                    "VALUES (:plan_id, :scheduled_ts, 'force_dhw_on', '{}', 'executed') RETURNING id"
                ),
                {"plan_id": plan_id, "scheduled_ts": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO plan_actions (plan_id, reverts_action_id, scheduled_ts, action_type, "
                    "payload_json, status) VALUES (:plan_id, :source_id, :scheduled_ts, "
                    "'force_dhw_off', '{}', 'pending')"
                ),
                {"plan_id": plan_id, "source_id": source_id, "scheduled_ts": now},
            )

        with pytest.raises(RuntimeError, match="unresolved safety obligations"):
            await asyncio.to_thread(_run_alembic, database_url, "downgrade", "028")
        await _migration_version(migration_engine, "029")
        await _migration_sql(
            migration_engine,
            "UPDATE plan_actions SET status = 'executed' WHERE reverts_action_id IS NOT NULL",
        )
        await asyncio.to_thread(_run_alembic, database_url, "downgrade", "028")
        await _migration_version(migration_engine, "028")


class TestPhase2SafetyRuntimeAcceptance:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC2_real_cycle_reconciles_due_off_after_failed_polls_without_on_redispatch(
        self, db_session
    ):
        from packages.optimizer.executor_core import PlanExecutor as RealPlanExecutor
        from packages.optimizer.main import execute_pending_actions
        from packages.poller.main import poll_device_status

        now = _FrozenDateTime.current
        expires_at = now - dt.timedelta(seconds=60)
        source = PlanActionRecord(
            plan_id=91,
            scheduled_ts=now - dt.timedelta(minutes=30),
            action_type="force_dhw_on",
            payload_json=json.dumps({"trigger": "shower_mode"}),
            device_id="device-a",
            status="executed",
        )
        db_session.add(source)
        await db_session.flush()
        restore = PlanActionRecord(
            plan_id=91,
            reverts_action_id=source.id,
            scheduled_ts=now + dt.timedelta(minutes=10),
            action_type="force_dhw_off",
            payload_json=json.dumps({"trigger": "shower_mode", "reason": "expiry"}),
            device_id="device-a",
            status="pending",
        )
        event = ShowerEventRecord(
            started_at=now - dt.timedelta(minutes=60),
            pre_shower_temp=55,
            status="active",
            device_id="device-a",
            expires_at=expires_at,
            activation_action_id=source.id,
        )
        db_session.add_all([restore, event])
        await db_session.commit()

        poll_wrapper = SimpleNamespace(
            refresh_device=AsyncMock(side_effect=RuntimeError("poll down"))
        )
        await poll_device_status(poll_wrapper)
        await poll_device_status(poll_wrapper)

        cycle_wrapper = MagicMock()
        cycle_wrapper.get_selected_device_id = AsyncMock(return_value="device-a")
        cycle_wrapper.force_dhw = AsyncMock()
        quality = AsyncMock(
            return_value={
                "ready": False,
                "reasons": ["device_status_stale"],
                "threshold_seconds": 900,
            }
        )
        frozen_datetime_module = SimpleNamespace(
            datetime=_FrozenDateTime,
            timedelta=dt.timedelta,
            timezone=dt.timezone,
        )
        with (
            patch("packages.optimizer.main.dt", frozen_datetime_module),
            patch("packages.optimizer.executor_core.dt", frozen_datetime_module),
            patch("packages.optimizer.shower_mode.dt", frozen_datetime_module),
            patch(
                "packages.optimizer.main.PlanExecutor",
                new=lambda wrapper: RealPlanExecutor(
                    wrapper,
                    device_quality_check=quality,
                ),
            ),
        ):
            await execute_pending_actions(cycle_wrapper)

        await db_session.refresh(restore)
        await db_session.refresh(event)
        assert event.status == "timeout_pending"
        assert restore.status == "pending"
        assert restore.scheduled_ts == now
        assert restore.safety_attempt_count == 1
        cycle_wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize(
        ("stuck_status", "claimed_at"),
        [
            ("executing", dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)),
            ("dispatched", dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)),
            ("dispatched", None),
        ],
    )
    async def test_P2_AC7_stuck_tagged_safety_action_is_recovered_to_pending(
        self, db_session, stuck_status, claimed_at
    ):
        source = PlanActionRecord(
            plan_id=92,
            scheduled_ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
            action_type="force_dhw_on",
            payload_json="{}",
            device_id="device-a",
            status="executed",
        )
        db_session.add(source)
        await db_session.flush()
        restore = PlanActionRecord(
            plan_id=92,
            reverts_action_id=source.id,
            scheduled_ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=4),
            action_type="force_dhw_off",
            payload_json="{}",
            device_id="device-a",
            status="pending",
        )
        db_session.add(restore)
        await db_session.commit()

        executor = PlanExecutor(None, session_factory=lambda: get_session())
        claimed = await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
        assert claimed is not None and claimed.id == restore.id
        attempt = executor._attempt_for(claimed)
        try:
            assert await attempt.drain()
            await executor._requeue_safety_action(claimed, "test_prior_owner_drained")
        finally:
            assert await attempt.release()
            executor._safety_attempts.pop(claimed.safety_claim_token, None)
        await db_session.refresh(restore)
        old_token = restore.safety_claim_token
        restore.status = stuck_status
        restore.safety_claimed_at = claimed_at
        restore.safety_next_retry_at = None
        await db_session.commit()

        await executor.execute_due_actions()

        await db_session.refresh(restore)
        assert restore.status == "pending"
        assert restore.safety_claim_token is not None and restore.safety_claim_token != old_token
        assert json.loads(restore.result_json)["safety_attempt"]["drain"] == "confirmed"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC7_fresh_dispatched_safety_claim_is_not_recovered(self, db_session):
        now = dt.datetime.now(dt.timezone.utc)
        source = PlanActionRecord(
            plan_id=92,
            scheduled_ts=now - dt.timedelta(minutes=5),
            action_type="force_dhw_on",
            payload_json="{}",
            device_id="device-a",
            status="executed",
        )
        db_session.add(source)
        await db_session.flush()
        restore = PlanActionRecord(
            plan_id=92,
            reverts_action_id=source.id,
            scheduled_ts=now - dt.timedelta(minutes=4),
            action_type="force_dhw_off",
            payload_json="{}",
            device_id="device-a",
            status="dispatched",
            safety_claimed_at=now - dt.timedelta(seconds=30),
        )
        db_session.add(restore)
        await db_session.commit()

        executor = PlanExecutor(None, session_factory=lambda: get_session())
        await executor.execute_due_actions()

        await db_session.refresh(restore)
        assert restore.status == "dispatched"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC7_held_fresh_dispatched_claim_is_not_recovered_or_reclaimed(
        self, db_session
    ):
        now = dt.datetime.now(dt.timezone.utc)
        source = PlanActionRecord(
            plan_id=92,
            scheduled_ts=now - dt.timedelta(minutes=5),
            action_type="force_dhw_on",
            payload_json="{}",
            device_id="device-a",
            status="executed",
        )
        db_session.add(source)
        await db_session.flush()
        restore = PlanActionRecord(
            plan_id=92,
            reverts_action_id=source.id,
            scheduled_ts=now - dt.timedelta(minutes=4),
            action_type="force_dhw_off",
            payload_json="{}",
            device_id="device-a",
            status="pending",
        )
        db_session.add(restore)
        await db_session.commit()

        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "UPDATE plan_actions SET status = 'dispatched', safety_claimed_at = :now "
                        "WHERE id = :action_id"
                    ),
                    {"now": now, "action_id": restore.id},
                )
                executor = PlanExecutor(None, session_factory=lambda: get_session())
                claimed = await asyncio.wait_for(executor._claim_due_safety_action(now), timeout=1)
                assert claimed is None
            finally:
                await transaction.rollback()


class TestPhase2ApiAcceptance:
    async def _seed_revert(
        self,
        db_session,
        *,
        action_type="force_dhw_off",
        force_dhw=0,
        current_target=21,
        baseline_temperature=21,
        status_age=dt.timedelta(0),
    ):
        now = dt.datetime.now(dt.timezone.utc)
        source_type = "force_dhw_on" if action_type == "force_dhw_off" else "zone_temp_boost"
        source_payload = {"zone_id": 1}
        restore_payload = {"zone_id": 1}
        if action_type == "zone_temp_restore":
            if baseline_temperature is not None:
                source_payload["baseline_temperature"] = baseline_temperature
            restore_payload["temperature"] = current_target
        source = PlanActionRecord(
            plan_id=93,
            scheduled_ts=now - dt.timedelta(minutes=5),
            action_type=source_type,
            payload_json=json.dumps(source_payload),
            device_id="device-a",
            status="executed",
        )
        db_session.add(source)
        await db_session.flush()
        restore = PlanActionRecord(
            plan_id=93,
            reverts_action_id=source.id,
            scheduled_ts=now - dt.timedelta(minutes=4),
            action_type=action_type,
            payload_json=json.dumps(restore_payload),
            device_id="device-a",
            status="pending",
        )
        status = DeviceStatusRecord(
            ts=now - status_age,
            device_id="device-a",
            force_dhw=force_dhw,
            zone1_target_temp=current_target,
        )
        event = ShowerEventRecord(
            started_at=now - dt.timedelta(minutes=10),
            pre_shower_temp=55,
            status="active",
            device_id="device-a",
            expires_at=now + dt.timedelta(hours=1),
            activation_action_id=source.id,
        )
        db_session.add_all([restore, status, event])
        await db_session.commit()
        return restore.id, event.id

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_fresh_safe_resolution_closes_event_audits_and_does_not_write(
        self, client: AsyncClient, db_session
    ):
        action_id, event_id = await self._seed_revert(db_session)
        from packages.core.services.aquarea import AquareaWrapper

        with patch.object(AquareaWrapper, "force_dhw", new_callable=AsyncMock) as write:
            response = await client.post(
                f"/api/operations/safety-reverts/{action_id}/resolve",
                json={"reason": "Operator confirmed safe state"},
            )

        assert response.status_code == 200
        action = await db_session.get(PlanActionRecord, action_id)
        event = await db_session.get(ShowerEventRecord, event_id)
        audits = (
            (
                await db_session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.action == "manual_safety_revert_resolution"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert action.status == "executed"
        assert json.loads(action.result_json)["reason"] == "manually_resolved"
        assert event.status == "resolved"
        assert any(a.actor == "authenticated_api" for a in audits)
        write.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("reason", ["", "   "])
    async def test_P2_AC11_blank_resolution_reason_is_rejected(
        self, client: AsyncClient, db_session, reason
    ):
        action_id, _ = await self._seed_revert(db_session)
        response = await client.post(
            f"/api/operations/safety-reverts/{action_id}/resolve",
            json={"reason": reason},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_stale_status_is_rejected(self, client: AsyncClient, db_session):
        action_id, _ = await self._seed_revert(db_session, status_age=dt.timedelta(hours=2))
        response = await client.post(
            f"/api/operations/safety-reverts/{action_id}/resolve",
            json={"reason": "stale"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_zone_restore_resolution_uses_restore_temperature(
        self, client: AsyncClient, db_session
    ):
        action_id, _ = await self._seed_revert(
            db_session,
            action_type="zone_temp_restore",
            baseline_temperature=21,
            current_target=21,
        )
        with patch(
            "packages.core.services.aquarea.AquareaWrapper.set_zone_heat_temperature"
        ) as write:
            response = await client.post(
                f"/api/operations/safety-reverts/{action_id}/resolve",
                json={"reason": "zone check"},
            )
        assert response.status_code == 200
        assert write.call_count == 0
        audits = (
            (
                await db_session.execute(
                    select(AuditLogRecord).where(
                        AuditLogRecord.action == "manual_safety_revert_resolution"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(a.target_device == "device-a" for a in audits)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_zone_restore_baseline_disagreement_is_rejected(
        self, client: AsyncClient, db_session
    ):
        action_id, _ = await self._seed_revert(
            db_session,
            action_type="zone_temp_restore",
            baseline_temperature=22,
            current_target=21,
        )
        response = await client.post(
            f"/api/operations/safety-reverts/{action_id}/resolve",
            json={"reason": "zone check"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_untagged_or_missing_action_is_not_resolvable(
        self, client: AsyncClient, db_session
    ):
        action = PlanActionRecord(
            plan_id=94,
            scheduled_ts=dt.datetime.now(dt.timezone.utc),
            action_type="force_dhw_off",
            payload_json="{}",
            device_id="device-a",
            status="pending",
        )
        db_session.add(action)
        await db_session.commit()

        untagged = await client.post(
            f"/api/operations/safety-reverts/{action.id}/resolve",
            json={"reason": "untagged"},
        )
        missing = await client.post(
            "/api/operations/safety-reverts/999999/resolve",
            json={"reason": "missing"},
        )
        assert untagged.status_code == 404
        assert missing.status_code == 404

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_request_body_cannot_override_persisted_state(
        self, client: AsyncClient, db_session
    ):
        action_id, _ = await self._seed_revert(db_session, force_dhw=1)
        response = await client.post(
            f"/api/operations/safety-reverts/{action_id}/resolve",
            json={"reason": "fake safe state", "force_dhw": 0, "zone1_target_temp": 21},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC11_missing_and_wrong_bearer_tokens_follow_global_auth(
        self, client: AsyncClient
    ):
        missing = await client.post(
            "/api/operations/safety-reverts/1/resolve",
            headers={"Authorization": ""},
            json={"reason": "auth"},
        )
        wrong = await client.post(
            "/api/operations/safety-reverts/1/resolve",
            headers={"Authorization": "Bearer wrong-token"},
            json={"reason": "auth"},
        )
        assert missing.status_code == 401
        assert wrong.status_code == 403

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC6_concurrent_real_claim_selects_exactly_one_due_restore(self):
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            plan = _plan(now, "claim-test", status="superseded")
            session.add(plan)
            await session.flush()
            source = PlanActionRecord(
                plan_id=plan.id,
                scheduled_ts=now - dt.timedelta(minutes=1),
                action_type="force_dhw_on",
                payload_json="{}",
                device_id="device-a",
                status="executed",
            )
            session.add(source)
            await session.flush()
            session.add(
                PlanActionRecord(
                    plan_id=plan.id,
                    reverts_action_id=source.id,
                    scheduled_ts=now - dt.timedelta(seconds=1),
                    action_type="force_dhw_off",
                    payload_json="{}",
                    device_id="device-a",
                    status="pending",
                )
            )

        def session_factory():
            return get_session()

        executors = [PlanExecutor(None, session_factory=session_factory) for _ in range(2)]
        ready, release = asyncio.Queue(), asyncio.Event()

        async def claim_and_cleanup(executor):
            action = await executor._claim_due_safety_action(now)
            ready.put_nowait(action)
            try:
                await release.wait()
            finally:
                if action is not None:
                    attempt = executor._attempt_for(action)
                    try:
                        assert await attempt.drain()
                        await executor._requeue_safety_action(action, "test_claim_cleanup")
                    finally:
                        assert await attempt.release()

        workers = [asyncio.create_task(claim_and_cleanup(executor)) for executor in executors]
        try:
            claimed = await asyncio.wait_for(asyncio.gather(ready.get(), ready.get()), 5)
            claimed_ids = [action.id for action in claimed if action is not None]
            assert len(claimed_ids) == 1
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*workers), 5)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC3_AC4_real_supersession_cancels_untagged_and_preserves_restore(self):
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            first = _plan(now, "first")
            await activate_plan(session, first)
            session.add(
                PlanActionRecord(
                    plan_id=first.id,
                    scheduled_ts=now + dt.timedelta(hours=1),
                    action_type="quiet_mode_on",
                    payload_json="{}",
                    status="pending",
                )
            )
            source = PlanActionRecord(
                plan_id=first.id,
                scheduled_ts=now,
                action_type="force_dhw_on",
                payload_json="{}",
                status="executed",
            )
            session.add(source)
            await session.flush()
            restore = PlanActionRecord(
                plan_id=first.id,
                reverts_action_id=source.id,
                scheduled_ts=now + dt.timedelta(hours=1),
                action_type="force_dhw_off",
                payload_json="{}",
                status="pending",
            )
            session.add(restore)
            second = _plan(now, "second")
            await activate_plan(session, second)

        async with get_session() as session:
            actions = (
                await session.execute(
                    text(
                        "SELECT action_type, reverts_action_id, status FROM plan_actions "
                        "WHERE plan_id = :plan_id ORDER BY id"
                    ),
                    {"plan_id": first.id},
                )
            ).all()
            stored_first = await session.get(PlanRecord, first.id)
        assert stored_first.status == "superseded"
        assert actions[0].status == "cancelled"
        assert actions[1].reverts_action_id is None
        assert actions[1].status == "executed"
        assert actions[2].reverts_action_id is not None
        assert actions[2].status == "pending"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_P2_AC3_AC4_supersede_active_plan_returns_real_active_id(self):
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            plan = _plan(now, "active")
            await activate_plan(session, plan)
            active_ids = await supersede_active_plan(session, reason="e2e")
        assert active_ids == [plan.id]

    @pytest.mark.asyncio(loop_scope="session")
    async def test_OBS1_shower_denial_preserves_existing_plan_and_tagged_restore(self):
        now = dt.datetime.now(dt.timezone.utc)
        previous = DeviceStatusRecord(
            ts=now - dt.timedelta(minutes=5),
            device_id="device-a",
            tank_temp=55,
            force_dhw=0,
        )
        current = DeviceStatusRecord(
            ts=now,
            device_id="device-a",
            tank_temp=43,
            force_dhw=0,
        )
        async with get_session() as session:
            session.add_all([previous, current])
            first = _plan(now, "existing")
            await activate_plan(session, first)
            session.add(
                PlanActionRecord(
                    plan_id=first.id,
                    scheduled_ts=now + dt.timedelta(hours=1),
                    action_type="quiet_mode_on",
                    payload_json="{}",
                    status="pending",
                )
            )
            source = PlanActionRecord(
                plan_id=first.id,
                scheduled_ts=now,
                action_type="force_dhw_on",
                payload_json="{}",
                status="executed",
            )
            session.add(source)
            await session.flush()
            session.add(
                PlanActionRecord(
                    plan_id=first.id,
                    reverts_action_id=source.id,
                    scheduled_ts=now + dt.timedelta(hours=1),
                    action_type="force_dhw_off",
                    payload_json="{}",
                    device_id="device-b",
                    status="pending",
                )
            )
        detector = ShowerDetector()
        detector._is_peak_price = lambda *_args: asyncio.sleep(0, result=False)
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "packages.optimizer.shower_mode.get_setting",
                lambda key: asyncio.sleep(
                    0,
                    result={
                        "shower_drop_threshold": "10",
                        "shower_max_duration_minutes": "60",
                    }[key],
                ),
            )
            async with get_session() as session:
                await detector._check_for_drop(session, current)

        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT action_type, reverts_action_id, status FROM plan_actions "
                        "WHERE plan_id = :plan_id ORDER BY id"
                    ),
                    {"plan_id": first.id},
                )
            ).all()
        assert len(rows) == 3
        assert rows[0].action_type == "quiet_mode_on"
        assert rows[0].status == "pending"
        assert rows[1].reverts_action_id is None
        assert rows[1].status == "executed"
        assert rows[2].reverts_action_id is not None
        assert rows[2].status == "pending"
        async with get_session() as session:
            assert await session.scalar(text("SELECT count(*) FROM plans")) == 1
            shower_events = (await session.execute(select(ShowerEventRecord))).scalars().all()
            assert len(shower_events) == 1
            assert shower_events[0].status == "skipped_observe_only_build"
            assert shower_events[0].activation_action_id is None
            assert await detector._get_active_event(session, current.device_id) is None
            assert (
                await session.scalar(
                    text("SELECT status FROM plans WHERE id = :id"), {"id": first.id}
                )
                == "active"
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_A2_watchdog_due_query_ignores_skipped_and_closed_events(self):
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            session.add_all(
                [
                    ShowerEventRecord(
                        started_at=now - dt.timedelta(minutes=30),
                        pre_shower_temp=55,
                        status=status,
                        device_id=f"device-{status[:8]}",
                        expires_at=now - dt.timedelta(minutes=1),
                    )
                    for status in (
                        "active",
                        "recovery_pending",
                        "timeout_pending",
                        "skipped_unresolved_revert",
                        "recovered",
                    )
                ]
            )
            session.add(
                ShowerEventRecord(
                    started_at=now,
                    pre_shower_temp=55,
                    status="active",
                    device_id="future-device",
                    expires_at=now + dt.timedelta(minutes=30),
                )
            )
        await reconcile_shower_expiry(now)
        async with get_session() as session:
            rows = (
                await session.execute(
                    text("SELECT status, device_id FROM shower_events ORDER BY id")
                )
            ).all()
        assert [row.status for row in rows[:3]] == ["timeout_pending"] * 3
        assert rows[3].status == "skipped_unresolved_revert"
        assert rows[4].status == "recovered"
        assert rows[5].status == "active"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_OBS1_shower_refuses_before_any_on_or_restore_flush(self):
        now = dt.datetime.now(dt.timezone.utc)
        previous = DeviceStatusRecord(
            ts=now - dt.timedelta(minutes=5),
            device_id="device-a",
            tank_temp=55,
            force_dhw=0,
        )
        current = DeviceStatusRecord(
            ts=now,
            device_id="device-a",
            tank_temp=43,
            force_dhw=0,
        )
        async with get_session() as session:
            session.add_all([previous, current])
        await _migration_sql(
            engine,
            """
            CREATE OR REPLACE FUNCTION test_reject_safety_off() RETURNS trigger AS $$
            BEGIN
                IF NEW.action_type = 'force_dhw_off' AND NEW.reverts_action_id IS NOT NULL THEN
                    RAISE EXCEPTION 'injected linked OFF failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """,
        )
        await _migration_sql(
            engine,
            "CREATE TRIGGER test_reject_safety_off_trigger BEFORE INSERT ON plan_actions "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_safety_off()",
        )
        detector = ShowerDetector()
        detector._is_peak_price = lambda *_args: asyncio.sleep(0, result=False)
        try:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(
                    "packages.optimizer.shower_mode.get_setting",
                    lambda key: asyncio.sleep(
                        0,
                        result={
                            "shower_mode_enabled": "true",
                            "shower_drop_threshold": "10",
                            "shower_max_duration_minutes": "60",
                        }[key],
                    ),
                )
                await detector.check(current)
        finally:
            await _migration_sql(
                engine, "DROP TRIGGER test_reject_safety_off_trigger ON plan_actions"
            )
            await _migration_sql(engine, "DROP FUNCTION test_reject_safety_off()")

        async with get_session() as session:
            shower_events = (await session.execute(select(ShowerEventRecord))).scalars().all()
            assert len(shower_events) == 1
            assert shower_events[0].status == "skipped_observe_only_build"
            assert shower_events[0].activation_action_id is None
            assert await detector._get_active_event(session, current.device_id) is None
            assert await session.scalar(text("SELECT count(*) FROM plans")) == 0
            assert await session.scalar(text("SELECT count(*) FROM plan_actions")) == 0

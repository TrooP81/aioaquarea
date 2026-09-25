from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.config import settings
from packages.core.database import engine, get_session
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    PlanActionRecord,
    PlanRecord,
    ShowerEventRecord,
)
from packages.core.plan_lifecycle import activate_plan, supersede_active_plan
from packages.optimizer.executor_core import PlanExecutor
from packages.optimizer.shower_mode import ShowerDetector, reconcile_shower_expiry


ROOT = Path(__file__).parents[2]


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
    if version == "029":
        await _clear_migration_data(migration_engine)
        await asyncio.to_thread(_run_alembic, database_url, "downgrade", "028")
    await _clear_migration_data(migration_engine)
    await _migration_version(migration_engine, "028")
    yield migration_engine, database_url


def _plan(now: dt.datetime, version: str, status: str = "active") -> PlanRecord:
    return PlanRecord(
        horizon_start=now,
        horizon_end=now + dt.timedelta(hours=2),
        plan_json="{}",
        optimizer_version=version,
        status=status,
    )


class TestPhase2MigrationAcceptance:
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
            status=stuck_status,
            safety_claimed_at=claimed_at,
        )
        db_session.add(restore)
        await db_session.commit()

        executor = PlanExecutor(None, session_factory=lambda: get_session())
        await executor.execute_due_actions()

        await db_session.refresh(restore)
        assert restore.status == "pending"

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
        claimed = await asyncio.gather(
            *(executor._claim_due_safety_action(now) for executor in executors)
        )
        claimed_ids = [action.id for action in claimed if action is not None]
        assert len(claimed_ids) == 1

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
    async def test_P2_AC4_shower_activation_preserves_tagged_restore(self):
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
        assert rows[0].status == "cancelled"
        assert rows[1].reverts_action_id is None
        assert rows[2].reverts_action_id is not None
        assert rows[2].status == "pending"

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
    async def test_P2_AC16_real_failure_after_on_flush_rolls_back_everything(self):
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
                with pytest.raises(Exception, match="injected linked OFF failure"):
                    await detector.check(current)
        finally:
            await _migration_sql(
                engine, "DROP TRIGGER test_reject_safety_off_trigger ON plan_actions"
            )
            await _migration_sql(engine, "DROP FUNCTION test_reject_safety_off()")

        async with get_session() as session:
            assert await session.scalar(text("SELECT count(*) FROM shower_events")) == 0
            assert await session.scalar(text("SELECT count(*) FROM plans")) == 0
            assert await session.scalar(text("SELECT count(*) FROM plan_actions")) == 0

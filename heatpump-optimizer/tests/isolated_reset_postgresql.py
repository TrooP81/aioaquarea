"""Explicit-only Timescale qualification; NEVER use an existing test/runtime stack.

Not auto-collected: invoke this filename only with the isolated proof described in
docs/settings-preserving-reset.md. Missing ownership proof is a hard failure, not
a skip. This file creates no containers, databases, networks or volumes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core import data_reset as reset
from packages.core.models import (
    DeviceStatusRecord,
    ManualCOPObservationRecord,
    MLLifecycleStateRecord,
    PlanActionRecord,
    SettingRecord,
    ShowerEventRecord,
    SmartThingsToken,
)
from packages.core.safety_attempts import device_attempt_key
from packages.core.reset_operator import source_fingerprint

PINNED_IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)


def owned_environment():
    proof = json.loads(Path(os.environ["HPO_RESET_TEST_PROOF"]).read_text(encoding="utf-8"))
    namespace = proof["namespace"]
    assert re.fullmatch(r"[0-9a-f]{32}", namespace)
    assert proof["context"] == "desktop-linux"
    assert proof["project"] == f"reset-disposable-{namespace}"
    url = make_url(os.environ["HPO_RESET_TEST_DATABASE_URL"])
    assert os.environ["DATABASE_URL"] == os.environ["HPO_RESET_TEST_DATABASE_URL"]
    assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
    assert url.port is not None and url.port >= 20000
    assert url.database == f"reset_test_{namespace}" and url.username == "reset_test"
    assert re.fullmatch(r"[0-9a-f]{64}", proof["container"])
    return proof, url


def inspect_owned(proof):
    def inspect(kind, identity):
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", kind, "inspect", identity]
            if kind != "container"
            else ["docker", "--context", "desktop-linux", "inspect", identity],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, "isolated resource unavailable"
        return json.loads(result.stdout)[0]

    container = inspect("container", proof["container"])
    labels = container["Config"]["Labels"]
    assert container["Config"]["Image"] == PINNED_IMAGE
    assert labels["com.docker.compose.project"] == proof["project"]
    assert container["State"]["Running"] and not container["State"]["Restarting"]
    assert container["Mounts"], "disposable storage ownership must be explicit"
    for mount in container["Mounts"]:
        assert mount["Type"] == "volume", "no host/runtime bind mounts"
        volume = inspect("volume", mount["Name"])
        assert volume["Labels"]["com.docker.compose.project"] == proof["project"]
    for network in container["NetworkSettings"]["Networks"].values():
        owned = inspect("network", network["NetworkID"])
        assert owned["Labels"]["com.docker.compose.project"] == proof["project"]
    return container


@pytest.mark.asyncio
async def test_RESET2_RESET3_RESET4_REPAIR2_real_timescale_commit_and_rollback(
    monkeypatch, tmp_path
):
    proof, url = owned_environment()  # All ownership checks precede any DB connection.
    container = inspect_owned(proof)
    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            async with session.begin():
                yield session

    async def resources(scope):
        assert scope["project"] == proof["project"]
        inspect_owned(proof)

    try:
        async with sessions() as session:
            assert await session.scalar(text("SELECT current_database()")) == url.database
            assert (
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            ).all() == ["039"]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_schema='public'"
                    )
                )
                > 0
            )
            for name in reset.WIPE_TABLES:
                count = await session.scalar(text(f'SELECT count(*) FROM public."{name}"'))
                assert count == 0 or (name == "ml_lifecycle_state" and count == 1), (
                    "DB is not fresh/owned"
                )
            assert await session.scalar(text("SELECT count(*) FROM settings")) == 0
            assert await session.scalar(text("SELECT count(*) FROM smartthings_oauth_token")) == 0
            oid, address, port = (
                await session.execute(
                    text(
                        "SELECT (SELECT oid FROM pg_database WHERE datname=current_database()), "
                        "inet_server_addr()::text, inet_server_port()"
                    )
                )
            ).one()
            now = await session.scalar(text("SELECT clock_timestamp()"))
            state = await session.get(MLLifecycleStateRecord, 1)
            if state is None:
                state = MLLifecycleStateRecord(id=1, epoch=str(uuid4()))
                session.add(state)
            session.add_all(
                [
                    SettingRecord(
                        key="manual_cop_monitoring_enabled", value="true", updated_at=now
                    ),
                    SettingRecord(key="ml_maintenance_enabled", value="true", updated_at=now),
                    SettingRecord(key="learning_mode_since", value=now.isoformat(), updated_at=now),
                    SmartThingsToken(
                        id=1,
                        access_token="disposable-access",
                        refresh_token="disposable-refresh",
                        expires_at=now + dt.timedelta(hours=1),
                        updated_at=now,
                    ),
                    PlanActionRecord(
                        id=11762,
                        plan_id=1,
                        action_type="force_dhw_on",
                        device_id="isolated-device",
                        status="expired",
                        payload_json="{}",
                        scheduled_ts=now,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    PlanActionRecord(
                        id=11763,
                        plan_id=1,
                        reverts_action_id=11762,
                        action_type="force_dhw_off",
                        device_id="isolated-device",
                        status="pending",
                        payload_json="{}",
                        scheduled_ts=now,
                    ),
                    ShowerEventRecord(
                        id=26,
                        device_id="isolated-device",
                        activation_action_id=11762,
                        status="timeout_pending",
                        started_at=now,
                        expires_at=now,
                        pre_shower_temp=55,
                    ),
                    DeviceStatusRecord(ts=now, device_id="isolated-device", force_dhw=1),
                    ManualCOPObservationRecord(
                        device_id="isolated-device",
                        granularity="day",
                        period_date=now.date() - dt.timedelta(days=1),
                        timezone="UTC",
                        value=3.0,
                        # CURRENT_TIMESTAMP in the ledger constraint is transaction
                        # start, earlier than the clock_timestamp() snapshot above.
                        observed_at=dt.datetime.combine(now.date(), dt.time(), dt.timezone.utc),
                        completeness="closed",
                    ),
                ]
            )
            await session.flush()
            previous_tokens = state.revision, state.fence
            manual_token = await session.scalar(
                text("SELECT last_value FROM manual_cop_revision_seq")
            )
            retained = [
                [reset.row_values(row) for row in await reset._rows(session, model)]
                for model in (SettingRecord, SmartThingsToken)
            ]
        # Core-only qualification has no operator maintenance container. This synthetic
        # entry satisfies the wire shape, not the real CLI resource/ownership guard.
        maintenance_id = "f" * 64
        scope = {
            "context": "desktop-linux",
            "project": proof["project"],
            "compose_files": {
                proof["container"]: container["Config"]["Labels"][
                    "com.docker.compose.project.config_files"
                ].split(","),
                maintenance_id: ["/core-test-only/maintenance.yml"],
            },
            "database": url.database,
            "database_oid": oid,
            "server_address": address,
            "server_port": port,
            "old_model_volume": "not-used-by-core-test-old",
            "new_model_volume": "not-used-by-core-test-new",
            "source_revision": source_fingerprint(),
            "owners": [proof["container"]],
            "maintenance_container": maintenance_id,
        }
        manifest = await reset.plan(
            sessions,
            resources,
            scope=scope,
            operation_uuid=str(uuid4()),
            operator="isolated-test",
            statement="Exact disposable test tuple administratively invalid.",
            statement_at=now.isoformat(),
            device_id="isolated-device",
        )

        async def refuses_plan(candidate_scope=scope):
            with pytest.raises(reset.ResetRefused):
                await reset.plan(
                    sessions,
                    resources,
                    scope=candidate_scope,
                    operation_uuid=str(uuid4()),
                    operator="isolated-test",
                    statement="Only the disposable tuple is invalid.",
                    statement_at=now.isoformat(),
                    device_id="isolated-device",
                )

        # Genuine PostgreSQL negative paths, not patched guard decisions.
        await refuses_plan({**scope, "database_oid": oid + 1})
        async with engine.connect() as writer:
            await writer.execute(text("SELECT 1"))
            await refuses_plan()
            assert await writer.scalar(text("SELECT 1")) == 1  # Never killed.
        async with sessions() as session:
            await session.execute(text("CREATE TABLE public.unreviewed_reset_test (id integer)"))
        try:
            await refuses_plan()
        finally:
            async with sessions() as session:
                await session.execute(text("DROP TABLE public.unreviewed_reset_test"))
        async with sessions() as session:
            await session.execute(text("CREATE SCHEMA reset_dependency_test"))
            await session.execute(
                text(
                    "CREATE TABLE reset_dependency_test.external_ref "
                    "(action_id integer REFERENCES public.plan_actions(id))"
                )
            )
        try:
            await refuses_plan()
        finally:
            async with sessions() as session:
                await session.execute(text("DROP TABLE reset_dependency_test.external_ref"))
                await session.execute(text("DROP SCHEMA reset_dependency_test"))
        async with sessions() as session:
            state = await session.get(MLLifecycleStateRecord, 1)
            state.lease_owner = "disposable-inflight-owner"
            state.lease_expires_at = now + dt.timedelta(hours=1)
        try:
            await refuses_plan()
        finally:
            async with sessions() as session:
                state = await session.get(MLLifecycleStateRecord, 1)
                state.lease_owner = state.lease_expires_at = None
        # A held owner must refuse; no row mutation or forced session cleanup.
        async with engine.connect() as owner:
            await owner.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": device_attempt_key("isolated-device")},
            )
            with pytest.raises(reset.ResetRefused):
                await reset.plan(
                    sessions,
                    resources,
                    scope=scope,
                    operation_uuid=str(uuid4()),
                    operator="isolated-test",
                    statement="invalid",
                    statement_at=now.isoformat(),
                    device_id="isolated-device",
                )
            await owner.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": device_attempt_key("isolated-device")},
            )
        tmp_path.chmod(0o700)
        receipts = reset.ReceiptStore(tmp_path)
        with monkeypatch.context() as patch:

            def fail_audit(*args):
                raise RuntimeError("injected transactional audit failure")

            patch.setattr(reset, "_add_audit", fail_audit)
            with pytest.raises(reset.ResetStageFailure):
                await reset.apply(
                    sessions, resources, receipts, manifest, approved_digest=manifest["digest"]
                )
        async with sessions() as session:
            assert (await session.get(PlanActionRecord, 11763)).status == "pending"
            assert (await session.get(ShowerEventRecord, 26)).status == "timeout_pending"
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 0
        # New obligations after the approved snapshot must abort, never be retired.
        async with sessions() as session:
            session.add(
                PlanActionRecord(
                    id=11764,
                    plan_id=1,
                    action_type="force_dhw_on",
                    device_id="isolated-device",
                    status="expired",
                    payload_json="{}",
                    scheduled_ts=now,
                )
            )
            await session.flush()
            session.add(
                PlanActionRecord(
                    id=11765,
                    plan_id=1,
                    reverts_action_id=11764,
                    action_type="force_dhw_off",
                    device_id="isolated-device",
                    status="pending",
                    payload_json="{}",
                    scheduled_ts=now,
                )
            )
        with pytest.raises(reset.ResetStageFailure):
            await reset.apply(
                sessions, resources, receipts, manifest, approved_digest=manifest["digest"]
            )
        async with sessions() as session:
            assert (await session.get(PlanActionRecord, 11763)).status == "pending"
            assert (await session.get(PlanActionRecord, 11765)).status == "pending"
            await session.delete(await session.get(PlanActionRecord, 11765))
            await session.flush()
            await session.delete(await session.get(PlanActionRecord, 11764))

        class UnavailableRetiredReceipt(reset.ReceiptStore):
            def persist(self, operation, stage, value):
                if stage == "retired":
                    raise OSError("injected durable receipt failure after DB commit")
                return super().persist(operation, stage, value)

        with pytest.raises(reset.ResetStageFailure) as failure:
            await reset.apply(
                sessions,
                resources,
                UnavailableRetiredReceipt(tmp_path),
                manifest,
                approved_digest=manifest["digest"],
            )
        assert failure.value.stage == "retirement_committed_receipt_pending"
        async with sessions() as session:
            restore = await session.get(PlanActionRecord, 11763)
            event = await session.get(ShowerEventRecord, 26)
            assert restore.status == event.status == "operator_invalidated"
            assert restore.executed_at is None and event.recovered_at is None
            assert (await session.get(PlanActionRecord, 11762)).status == "expired"
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 1
            assert await session.scalar(text("SELECT count(*) FROM device_status")) == 1

        # A client arriving between committed retirement and wipe still blocks.
        late_writer = None
        checks = 0

        async def racing_resources(current_scope):
            nonlocal late_writer, checks
            await resources(current_scope)
            checks += 1
            if checks == 2:
                late_writer = await engine.connect()
                await late_writer.execute(text("SELECT 1"))

        try:
            with pytest.raises(reset.ResetStageFailure) as failure:
                await reset.apply(
                    sessions,
                    racing_resources,
                    receipts,
                    manifest,
                    approved_digest=manifest["digest"],
                )
            assert failure.value.stage == "retirement_receipt_durable"
            assert late_writer is not None
            assert await late_writer.scalar(text("SELECT 1")) == 1
        finally:
            if late_writer is not None:
                await late_writer.close()
        result = await reset.apply(
            sessions, resources, receipts, manifest, approved_digest=manifest["digest"]
        )
        assert (
            await reset.apply(
                sessions, resources, receipts, manifest, approved_digest=manifest["digest"]
            )
            == result
        )
        async with sessions() as session:
            after = [
                [reset.row_values(row) for row in await reset._rows(session, model)]
                for model in (SettingRecord, SmartThingsToken)
            ]
            assert retained == after
            for name in reset.WIPE_TABLES:
                assert await session.scalar(text(f'SELECT count(*) FROM public."{name}"')) == {
                    "audit_log": 2,
                    "ml_lifecycle_state": 1,
                }.get(name, 0)
            fresh = await session.get(MLLifecycleStateRecord, 1)
            assert fresh.epoch != manifest["previous_epoch"]
            assert min(fresh.revision, fresh.fence) > max(previous_tokens)
            assert fresh.maintenance_enabled and fresh.checkpoints == {}
            assert (
                await session.scalar(text("SELECT nextval('manual_cop_revision_seq')"))
                > manual_token
            )
            assert (
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            ).all() == ["039"]
            assert not (await session.scalars(select(PlanActionRecord))).all()
    finally:
        await engine.dispose()

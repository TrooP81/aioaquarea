"""Explicit-only real operator qualification. Never discover/use an existing stack.

The external harness owns every resource and sets a synthetic DSN. No guard is
patched. Normal pytest fixtures remain active; CLI calls run in fresh processes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core import data_reset as reset
from packages.core.models import (
    DeviceStatusRecord,
    MLLifecycleStateRecord,
    PlanActionRecord,
    SettingRecord,
    ShowerEventRecord,
    SmartThingsToken,
)
from packages.core.reset_operator import inspect_resources, source_fingerprint

PIN = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)


def docker(*args):
    result = subprocess.run(
        ["docker", "--context", "desktop-linux", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return result.stdout


def environment():
    proof = json.loads(Path(os.environ["HPO_OPERATOR_PROOF"]).read_text())
    assert re.fullmatch(r"[0-9a-f]{32}", proof["namespace"])
    assert proof["project"] == "resetop-disposable-" + proof["namespace"]
    for key in ("database_container", "maintenance_container"):
        assert re.fullmatch(r"[0-9a-f]{64}", proof[key])
    db = json.loads(docker("inspect", proof["database_container"]))[0]
    assert db["Config"]["Image"] == PIN
    assert db["Config"]["Labels"]["com.docker.compose.project"] == proof["project"]
    assert db["Mounts"] and all(m["Type"] == "volume" for m in db["Mounts"])
    for mount in db["Mounts"]:
        volume = json.loads(docker("volume", "inspect", mount["Name"]))[0]
        assert volume["Labels"]["com.docker.compose.project"] == proof["project"]
    for network in db["NetworkSettings"]["Networks"].values():
        net = json.loads(docker("network", "inspect", network["NetworkID"]))[0]
        assert net["Internal"]
        assert net["Labels"]["com.docker.compose.project"] == proof["project"]
    url = make_url(os.environ["LOCAL_RESET_DATABASE_URL"])
    assert os.environ["DATABASE_URL"] == os.environ["LOCAL_RESET_DATABASE_URL"]
    assert url.database == "reset_test_" + proof["namespace"]
    assert url.username == "reset_test" and url.port == 5432
    assert url.host in {n["IPAddress"] for n in db["NetworkSettings"]["Networks"].values()}
    assert Path("/receipts").stat().st_uid == os.getuid()
    assert Path("/receipts").stat().st_mode & 0o077 == 0
    return proof, url


def cli(command, input_path, *extra):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "packages.core.reset_operator",
            command,
            "--input",
            str(input_path),
            "--model-dir",
            "/fresh-models",
            "--receipts",
            "/receipts",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=100,
    )
    # The production CLI deliberately sanitizes failures and emits bounded JSON.
    print("CLI", command, "EXIT", result.returncode, result.stderr.strip(), flush=True)
    return result


async def test_RESET1_to_RESET6_real_operator_stages_replay_and_refusals(tmp_path):
    proof, url = environment()  # Ownership proof before any DB connection.
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            async with session.begin():
                yield session

    try:
        async with sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM settings")) == 0
            assert (
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            ).all() == ["037"]
            now = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
            oid = await session.scalar(
                text("SELECT oid FROM pg_database WHERE datname=current_database()")
            )
            state = await session.get(MLLifecycleStateRecord, 1)
            if state is None:
                state = MLLifecycleStateRecord(id=1, epoch=str(uuid4()))
                session.add(state)
            from packages.core.settings_service import SETTING_SPECS

            values = {k: spec.serialize_default() for k, spec in SETTING_SPECS.items()}
            values.update(
                manual_cop_monitoring_enabled="true",
                learning_mode_enabled="true",
                learning_mode_since="2025-10-16T06:00:00+00:00",
                ml_maintenance_enabled="true",
                aquarea_username="synthetic-preserved-user",
                aquarea_password="synthetic-preserved-password",
            )
            session.add_all(
                [SettingRecord(key=k, value=v, updated_at=now) for k, v in values.items()]
            )
            session.add(
                SmartThingsToken(
                    id=1,
                    access_token="synthetic-access",
                    refresh_token="synthetic-refresh",
                    expires_at=now + dt.timedelta(days=1),
                    updated_at=now,
                )
            )
            session.add(
                PlanActionRecord(
                    id=11762,
                    plan_id=1,
                    device_id="synthetic-device",
                    action_type="force_dhw_on",
                    status="expired",
                    payload_json="{}",
                    scheduled_ts=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    PlanActionRecord(
                        id=11763,
                        plan_id=1,
                        reverts_action_id=11762,
                        device_id="synthetic-device",
                        action_type="force_dhw_off",
                        status="pending",
                        payload_json="{}",
                        scheduled_ts=now,
                    ),
                    ShowerEventRecord(
                        id=26,
                        device_id="synthetic-device",
                        activation_action_id=11762,
                        status="timeout_pending",
                        started_at=now,
                        expires_at=now,
                        pre_shower_temp=55,
                    ),
                    DeviceStatusRecord(ts=now, device_id="synthetic-device", force_dhw=1),
                ]
            )
            await session.flush()
            old_epoch, old_tokens = state.epoch, (state.revision, state.fence)
            retained = [
                [reset.row_values(row) for row in await reset._rows(session, model)]
                for model in (SettingRecord, SmartThingsToken)
            ]
        scope = {
            "context": "desktop-linux",
            "project": proof["project"],
            "compose_files": {
                inspected["Id"]: inspected["Config"]["Labels"][
                    "com.docker.compose.project.config_files"
                ].split(",")
                for inspected in json.loads(
                    docker("inspect", *proof["owners"], proof["maintenance_container"])
                )
            },
            "database": url.database,
            "database_oid": oid,
            "server_address": url.host,
            "server_port": 5432,
            "old_model_volume": proof["old_model_volume"],
            "new_model_volume": proof["new_model_volume"],
            "source_revision": source_fingerprint(),
            "owners": proof["owners"],
            "maintenance_container": proof["maintenance_container"],
        }
        from tests.isolated_provenance_resources import verify_real_provenance

        await verify_real_provenance(scope, proof, tmp_path, docker)
        # Independently reproduce the old finding against actual full Docker IDs.
        inspect_resources(scope, Path("/fresh-models"))
        with pytest.raises(reset.ResetRefused, match="container"):
            inspect_resources(
                {**scope, "owners": [identity[:12] for identity in scope["owners"]]},
                Path("/fresh-models"),
            )
        print("REAL_FULL_ID_PREFLIGHT_PASS; SHORT_ID_REFUSAL_PASS", flush=True)
        scope_path = tmp_path / "scope.json"
        scope_path.write_text(json.dumps(scope))
        operation = str(uuid4())
        args = (
            "--operation-uuid",
            operation,
            "--operator",
            "disposable-test-operator",
            "--statement",
            "Only this synthetic exact historical tuple is invalid.",
            "--statement-at",
            now.isoformat(),
            "--device-id",
            "synthetic-device",
        )
        stale = Path("/fresh-models/stale.pkl")
        stale.write_bytes(b"not-a-model")
        assert cli("plan", scope_path, *args).returncode == 1
        stale.unlink()
        planned = cli("plan", scope_path, *args)
        if planned.returncode:
            # The CLI intentionally hides driver details. Re-run its unchanged
            # public implementation for a synthetic-only diagnostic traceback;
            # no ownership, session, inventory or receipt guard is bypassed.
            from packages.core.reset_operator import run

            await run(
                SimpleNamespace(
                    command="plan",
                    input=scope_path,
                    dsn_env="LOCAL_RESET_DATABASE_URL",
                    model_dir=Path("/fresh-models"),
                    receipts=Path("/receipts"),
                    operation_uuid=operation,
                    operator="disposable-test-operator",
                    statement=args[5],
                    statement_at=now.isoformat(),
                    device_id="synthetic-device",
                )
            )
        assert planned.returncode == 0
        manifest = json.loads(planned.stdout)
        from tests.isolated_provenance_resources import verify_manifest_refusals

        verify_manifest_refusals(manifest, tmp_path, cli)
        assert manifest["target"] == [11763, 11762, 26]
        assert manifest["previous_epoch"] == old_epoch
        manifest_path = tmp_path / "approved-synthetic-manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        apply_args = ("--approved-digest", manifest["digest"])
        assert cli("apply", manifest_path, "--approved-digest", "0" * 64).returncode == 1

        # A real DB audit failure must roll back restore + linked-event settlement.
        async with sessions() as session:
            await session.execute(
                text(
                    "CREATE FUNCTION reset_test_fail() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'injected isolated failure'; END $$"
                )
            )
            await session.execute(
                text(
                    "CREATE TRIGGER reset_test_audit BEFORE INSERT ON audit_log "
                    "FOR EACH ROW EXECUTE FUNCTION reset_test_fail()"
                )
            )
        assert cli("apply", manifest_path, *apply_args).returncode == 1
        async with sessions() as session:
            assert (await session.get(PlanActionRecord, 11763)).status == "pending"
            assert (await session.get(ShowerEventRecord, 26)).status == "timeout_pending"
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 0
            await session.execute(text("DROP TRIGGER reset_test_audit ON audit_log"))

        # A filesystem receipt conflict after retirement commit cannot authorize wipe.
        retired_path = Path("/receipts") / f"{operation}.retired.json"
        retired_path.symlink_to("/receipts/missing")
        # Race initial retirement too, not only replay after an already committed
        # wipe. Both contenders may fail closed on other-client/ownership checks;
        # the same-UUID retry below must still produce exactly one settlement.
        retirement_race = await asyncio.gather(
            asyncio.to_thread(cli, "apply", manifest_path, *apply_args),
            asyncio.to_thread(cli, "apply", manifest_path, *apply_args),
        )
        assert all(result.returncode == 1 for result in retirement_race)
        result = cli("apply", manifest_path, *apply_args)
        assert result.returncode == 1
        assert "retirement_committed_receipt_pending" in result.stderr
        async with sessions() as session:
            restore = await session.get(PlanActionRecord, 11763)
            event = await session.get(ShowerEventRecord, 26)
            assert restore.status == event.status == "operator_invalidated"
            assert restore.executed_at is None and event.recovered_at is None
            assert await session.scalar(text("SELECT count(*) FROM device_status")) == 1
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 1
        retired_path.unlink()

        # TRUNCATE dependency failure occurs after retirement and must roll back wipe.
        async with sessions() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER reset_test_truncate BEFORE TRUNCATE ON plans "
                    "FOR EACH STATEMENT EXECUTE FUNCTION reset_test_fail()"
                )
            )
        result = cli("apply", manifest_path, *apply_args)
        assert result.returncode == 1
        assert "retirement_receipt_durable" in result.stderr
        async with sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM device_status")) == 1
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 1
            await session.execute(text("DROP TRIGGER reset_test_truncate ON plans"))
            await session.execute(text("DROP FUNCTION reset_test_fail()"))

        # Final receipt failure must leave the committed wipe recoverable by UUID.
        reset_path = Path("/receipts") / f"{operation}.reset.json"
        reset_path.symlink_to("/receipts/missing")
        result = cli("apply", manifest_path, *apply_args)
        assert result.returncode == 1
        assert "reset_committed_receipt_pending" in result.stderr
        async with sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM device_status")) == 0
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 2
        reset_path.unlink()

        # Simultaneous same-UUID operators never duplicate settlement or wipe.
        results = await asyncio.gather(
            asyncio.to_thread(cli, "apply", manifest_path, *apply_args),
            asyncio.to_thread(cli, "apply", manifest_path, *apply_args),
        )
        assert all(result.returncode in (0, 1) for result in results)
        # A bounded retry uses the *same* approved snapshot, never a changed target.
        result = cli("apply", manifest_path, *apply_args)
        assert result.returncode == 0
        receipt = json.loads(result.stdout)
        assert receipt["physical_state_verified"] is receipt["device_write"] is False
        assert receipt["new_epoch"] != old_epoch
        assert json.loads(cli("apply", manifest_path, *apply_args).stdout) == receipt
        conflicting = {**manifest, "statement": "different synthetic attestation"}
        conflicting["digest"] = reset.manifest_digest(conflicting)
        manifest_path.write_text(json.dumps(conflicting))
        assert (
            cli("apply", manifest_path, "--approved-digest", conflicting["digest"]).returncode == 1
        )
        async with sessions() as session:
            for name in reset.WIPE_TABLES:
                assert await session.scalar(text(f'SELECT count(*) FROM public."{name}"')) == {
                    "audit_log": 2,
                    "ml_lifecycle_state": 1,
                }.get(name, 0)
            assert retained == [
                [reset.row_values(row) for row in await reset._rows(session, model)]
                for model in (SettingRecord, SmartThingsToken)
            ]
            fresh = await session.get(MLLifecycleStateRecord, 1)
            assert fresh.epoch == receipt["new_epoch"]
            assert min(fresh.revision, fresh.fence) > max(old_tokens)
            assert fresh.checkpoints == {} and not fresh.lease_owner
            assert fresh.lease_expires_at is None
            assert fresh.next_maintenance_at is None and fresh.last_demand_attempt_at is None
            assert (
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            ).all() == ["037"]
        from packages.ml.lifecycle import Lease, LeaseLost, Lifecycle

        with pytest.raises(LeaseLost):
            await Lifecycle(sessions).renew(
                Lease(
                    old_epoch,
                    "old-synthetic-owner",
                    old_tokens[1],
                    old_tokens[0],
                    now + dt.timedelta(days=1),
                )
            )
        for stage in ("prepared", "retired", "reset"):
            path = Path("/receipts") / f"{operation}.{stage}.json"
            assert path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0
            assert json.loads(path.read_text())
        assert not list(Path("/fresh-models").iterdir())
        Path("/receipts/result.json").write_text(json.dumps(receipt))
        Path("/receipts/result.json").chmod(0o600)
        print("REAL_OPERATOR_PLAN_APPLY_STAGE_FAILURES_CONCURRENCY_REPLAY_PASS", flush=True)
    finally:
        await engine.dispose()

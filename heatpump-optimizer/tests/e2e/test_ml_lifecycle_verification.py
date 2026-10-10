"""Independent OBS-5 checks restricted to receipt-owned disposable storage."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import sys
from urllib.parse import urlencode
from unittest.mock import AsyncMock

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.models import MLLifecycleStateRecord
from packages.ml.lifecycle import Lifecycle, LifecycleUnavailable, invalidate_lifecycle
from packages.ml.model_status import get_model_status
from tests.e2e.test_manual_cop_postgresql import (
    _inspect_owned_resources,
    guard_owned_database,
    owned_test_environment,
    owned_test_network,
    prepare_owned_test_profile,
    retained_storage_state,
    run_verification_phase,
    verification_preflight,
)
from tests.e2e.test_ml_lifecycle_postgresql import (
    _artifact,
    _owner,
    _run_migration,
    _source_receipt,
    lifecycle_database as lifecycle_database,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

TABLES = (
    "ml_lifecycle_state",
    "ml_artifacts",
    "ml_maintenance_requests",
    "ml_forecast_issues",
    "ml_forecast_outcomes",
)


async def _marker(engine):
    async with engine.connect() as connection:
        return (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()


async def _retained(engine):
    async with engine.connect() as connection:
        result = await retained_storage_state(connection)
    result.pop("marker")
    return result


class TestOBS5RealMigration:
    async def test_032_to_033_round_trip_check_and_populated_refusal(self, request):
        assert request.config.option.noconftest
        receipt = owned_test_environment()
        _source_receipt(receipt)
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def sessions():
            async with maker() as session:
                try:
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        with owned_test_network():
            try:
                async with engine.begin() as connection:
                    await guard_owned_database(connection, require_empty=True)
                await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "032")
                assert await _marker(engine) == "032"
                retained = await _retained(engine)
                await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "033")
                assert await _marker(engine) == "033"
                async with engine.connect() as connection:
                    kinds = (
                        await connection.execute(
                            text(
                                "SELECT relname,relkind::text FROM pg_class "
                                "WHERE relnamespace='public'::regnamespace "
                                "AND relname = ANY(:tables) ORDER BY relname"
                            ),
                            {"tables": list(TABLES)},
                        )
                    ).all()
                    assert kinds == [(name, "r") for name in sorted(TABLES)]
                    assert (
                        await connection.execute(
                            text("SELECT is_called FROM public.ml_lifecycle_token_seq")
                        )
                    ).scalar_one() is False
                await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "032")
                assert await _marker(engine) == "032"
                async with engine.connect() as connection:
                    for name in (*TABLES, "ml_lifecycle_token_seq"):
                        assert (
                            await connection.execute(
                                text("SELECT to_regclass(:name)"), {"name": "public." + name}
                            )
                        ).scalar_one() is None
                assert await _retained(engine) == retained
                await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "033")
                await asyncio.to_thread(command.check, Config("alembic.ini"))
                lifecycle = Lifecycle(sessions)
                before = await lifecycle.initialize()
                with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
                    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "032")
                assert await _marker(engine) == "033"
                assert await lifecycle.read() == before
                assert await _retained(engine) == retained
                await asyncio.to_thread(command.check, Config("alembic.ini"))
            finally:
                await engine.dispose()


async def test_real_linux_fsync_precedes_sql_cas(lifecycle_database, monkeypatch):
    database = lifecycle_database
    lease = await _owner(database)
    events = []
    real_fsync = os.fsync

    def fsync(descriptor):
        target = str(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        real_fsync(descriptor)
        events.append((kind, target))

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE ml_lifecycle_state SET revision="):
            events.append(("cas", statement))

    monkeypatch.setattr(os, "fsync", fsync)
    event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
    try:
        artifact = _artifact(database, "real-linux-order")
        publication_end = len(events)
        assert any(kind == "file" for kind, _ in events)
        assert ("directory", str(artifact.path.parent)) in events
        assert ("directory", str(database.model_dir)) in events
        result = await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=lease.revision,
            expected_incumbent=None,
        )
        assert result.committed
        cas_index = next(index for index, (kind, _) in enumerate(events) if kind == "cas")
        certification = events[publication_end:cas_index]
        assert ("file", str(artifact.path)) in certification
        assert ("directory", str(artifact.path.parent)) in certification
        assert ("directory", str(database.model_dir)) in certification
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)


async def test_real_cas_loser_never_installs(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database)
    artifacts = [_artifact(database, "install-first"), _artifact(database, "install-second")]
    installed = []
    results = await asyncio.gather(
        *(
            database.lifecycle.publish(
                lease,
                artifact,
                expected_revision=lease.revision,
                expected_incumbent=None,
                on_committed=lambda reference: installed.append(reference.artifact_hash),
            )
            for artifact in artifacts
        )
    )
    assert sum(result.committed for result in results) == 1
    assert len(installed) == 1
    winner = artifacts[next(index for index, result in enumerate(results) if result.committed)]
    assert installed == [winner.artifact_hash]
    assert (await database.lifecycle.load_committed("demand", role="candidate"))["fit_id"] == (
        winner.manifest["fit_id"]
    )


async def test_reset_and_status_never_initialize_empty_authority(lifecycle_database):
    database = lifecycle_database
    status = await get_model_status("demand", database.model_dir, lifecycle=database.lifecycle)
    assert status["status"] == "unavailable"
    async with database.sessions() as session:
        assert await invalidate_lifecycle(session) is None
        assert (
            await session.execute(select(func.count()).select_from(MLLifecycleStateRecord))
        ).scalar_one() == 0
        assert (
            await session.execute(text("SELECT is_called FROM ml_lifecycle_token_seq"))
        ).scalar_one() is False


async def test_issued_tokens_refuse_downgrade_even_after_rows_removed(lifecycle_database):
    database = lifecycle_database
    state = await database.lifecycle.initialize()
    async with database.engine.begin() as connection:
        await connection.execute(text("DELETE FROM ml_lifecycle_state"))
    with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda current: _run_migration(current, "downgrade"))
    async with database.engine.begin() as connection:
        assert (
            await connection.execute(text("SELECT is_called FROM ml_lifecycle_token_seq"))
        ).scalar_one() is True
        token = (
            await connection.execute(text("SELECT nextval('ml_lifecycle_token_seq')"))
        ).scalar_one()
        assert token > max(state.revision, state.fence)


@pytest.mark.parametrize("initialized", [False, True])
async def test_maintenance_off_admin_reset_only_revokes_existing_state(
    lifecycle_database, monkeypatch, initialized
):
    from packages.api.routers import admin
    from packages.ml import lifecycle as lifecycle_module
    from packages.ml.thermal import ThermalModel

    database = lifecycle_database
    artifact = None
    queued = None
    before = None
    if initialized:
        lease = await _owner(database)
        artifact = _artifact(database, "maintenance-off-reset")
        published = await database.lifecycle.publish(
            lease, artifact, expected_revision=lease.revision, expected_incumbent=None
        )
        assert published.committed
        await database.lifecycle.set_maintenance_enabled(False)
        queued = await database.lifecycle.enqueue("demand", "verification")
        before = await database.lifecycle.read()
    if before is not None:
        assert before.maintenance_enabled is False
    monkeypatch.setattr(admin, "get_session", database.sessions)
    monkeypatch.setattr(admin, "reset_ml_models", lambda: [])
    monkeypatch.setattr(lifecycle_module, "get_bool_setting", AsyncMock(return_value=False))
    writes = []

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if any(
            statement.startswith(prefix + name)
            for prefix in ("UPDATE ", "INSERT INTO ", "DELETE FROM ")
            for name in TABLES
        ):
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", before_execute)
    try:
        status = await get_model_status("demand", database.model_dir, lifecycle=database.lifecycle)
        assert status["status"] in {"unavailable", "unqualified", "candidate"}
        owner = lifecycle_module.MaintenanceRunner(database.lifecycle)
        assert (await owner.run())["reason"] == "maintenance_disabled"
        await owner.close()
        assert (
            await lifecycle_module.enqueue_maintenance_request(
                "demand", "startup", automatic=True, lifecycle=database.lifecycle
            )
            is None
        )
        assert await database.lifecycle.get_request("missing-request") is None
        assert await ThermalModel().arefresh_committed(lifecycle=database.lifecycle) is False
        assert writes == [], "maintenance-off API read mutated lifecycle storage"
        if before is not None:
            assert await database.lifecycle.read() == before
        result = await admin.reset_data(admin.ResetRequest(scopes=["energy"]))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_execute)
    assert result["status"] == "ok"
    if before is None:
        assert result["model_lifecycle"] is None
        assert writes == [], "reset created lifecycle authority where none existed"
        with pytest.raises(LifecycleUnavailable, match="lifecycle_not_initialized"):
            await database.lifecycle.read()
        async with database.sessions() as session:
            for name in TABLES:
                assert (
                    await session.execute(text(f"SELECT count(*) FROM {name}"))
                ).scalar_one() == 0
            assert (
                await session.execute(text("SELECT is_called FROM ml_lifecycle_token_seq"))
            ).scalar_one() is False
        return
    after = await database.lifecycle.read()
    assert after.epoch == before.epoch
    assert after.maintenance_enabled is False
    assert after.revision > max(before.revision, before.fence)
    assert after.fence > max(before.revision, before.fence)
    assert after.lease_owner is None and after.lease_expires_at is None
    assert after.references == ()
    assert artifact.path.exists()
    assert (await database.lifecycle.get_request(queued.id)).status == "cancelled"
    assert writes and all(statement.startswith("UPDATE ") for statement in writes)
    assert result["model_lifecycle"] == {
        "epoch": after.epoch,
        "revision": after.revision,
        "fence": after.fence,
    }


def _wait_owned_health(root, role, seconds):
    receipt = owned_test_environment()
    started = dt.datetime.now(dt.timezone.utc)
    deadline = started + dt.timedelta(seconds=seconds)
    service_id = receipt["services"][role]["id"]
    evidence = {"role": role, "id": service_id, "started_at": started.isoformat()}

    def healthy():
        actual = _inspect_owned_resources(
            [service_id], receipt["namespace"]["id"], root / "evidence"
        )
        state = actual["services"][0]["State"]
        return state["Running"] and state.get("Health", {}).get("Status") == "healthy"

    connection = http.client.HTTPConnection("localhost", timeout=seconds)
    try:
        with owned_test_network():
            if not healthy():
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(seconds)
                connection.sock.connect("/var/run/docker.sock")
                query = urlencode(
                    {
                        "since": int(started.timestamp()) - 60,
                        "until": int(deadline.timestamp()),
                        "filters": json.dumps(
                            {"container": [service_id], "event": ["health_status"]}
                        ),
                    }
                )
                connection.request("GET", "/events?" + query)
                response = connection.getresponse()
                assert response.status == 200
                while dt.datetime.now(dt.timezone.utc) < deadline:
                    line = response.readline()
                    if not line:
                        break
                    action = json.loads(line).get("Action")
                    if action == "health_status: healthy":
                        break
                    if action == "health_status: unhealthy":
                        raise AssertionError("owned service health check failed: " + role)
            assert healthy(), "owned service readiness deadline exceeded: " + role
        evidence["ready"] = True
    finally:
        connection.close()
        evidence["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (root / "evidence" / ("obs5-readiness-" + role + ".json")).write_text(
            json.dumps(evidence), encoding="utf-8"
        )


def _run_owned_verification():
    export = Path(os.environ["HPO_OBS5_EXPORT"])
    assert export.is_dir()
    root = Path(os.environ["MODEL_DIR"]).parent
    phases = []
    failed = False
    try:
        assert prepare_owned_test_profile() == root
        _wait_owned_health(root, "postgres", 90)
        _wait_owned_health(root, "redis", 60)
        assert importlib.metadata.version("tzdata") == "2026.4"
        verification_preflight(root)
        commands = (
            (
                "obs5-real-migration",
                [
                    "-m",
                    "pytest",
                    "--noconftest",
                    "tests/e2e/test_ml_lifecycle_verification.py::TestOBS5RealMigration",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-rA",
                ],
                1,
            ),
            (
                "obs5-pg-original",
                [
                    "-m",
                    "pytest",
                    "--noconftest",
                    "tests/e2e/test_ml_lifecycle_postgresql.py",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-rA",
                ],
                12,
            ),
            (
                "obs5-pg-supplement",
                [
                    "-m",
                    "pytest",
                    "--noconftest",
                    "tests/e2e/test_ml_lifecycle_verification.py",
                    "-k",
                    "not TestOBS5RealMigration",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-rA",
                ],
                5,
            ),
            ("obs5-alembic-check", ["-m", "alembic", "check"], None),
        )
        seen = set()
        for name, arguments, count in commands:
            print("OBS5_PHASE=" + name, flush=True)
            try:
                run_verification_phase(name, [sys.executable, *arguments])
            except Exception as error:
                failed = True
                print(f"OBS5_PHASE_FAILURE={name}:{type(error).__name__}:{error}", flush=True)
            phase = root / "evidence" / name
            process = json.loads((phase / "process.json").read_text(encoding="utf-8"))
            assert not process["alive"], "verification child still running"
            if count is not None:
                nodes = json.loads((phase / "nodes.json").read_text(encoding="utf-8"))
                selected = set(nodes["selected"])
                assert len(selected) == count and not seen.intersection(selected)
                seen.update(selected)
                reports = [
                    json.loads(line)
                    for line in (phase / "reports.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                successful = (
                    process["exit"] == nodes["exit"] == 0
                    and not process["timed_out"]
                    and all(
                        report["outcome"] == "passed" and not report["wasxfail"]
                        for report in reports
                    )
                    and {report["nodeid"] for report in reports if report["when"] == "call"}
                    == selected
                )
                failed |= not successful
            phases.append(process)
        _source_receipt(json.loads(os.environ["COP_MANUAL_TEST_RECEIPT"]))
        (root / "evidence" / "obs5-scope-seal.json").write_text(
            json.dumps(
                {
                    "scope": "OBS-5 only; not full backend release qualification",
                    "passed": not failed,
                    "revision": os.environ["COP_MANUAL_TEST_REVISION"],
                    "nodes": sorted(seen),
                    "phases": phases,
                }
            ),
            encoding="utf-8",
        )
        return int(failed)
    finally:
        if (root / "evidence").exists():
            shutil.copytree(root / "evidence", export / "evidence", dirs_exist_ok=True)
        if (root / "ownership.json").exists():
            shutil.copy2(root / "ownership.json", export / "ownership.json")


if __name__ == "__main__":
    raise SystemExit(_run_owned_verification())

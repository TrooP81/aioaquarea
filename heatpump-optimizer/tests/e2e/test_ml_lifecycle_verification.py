"""Independent OBS-5 checks restricted to receipt-owned disposable storage.

Supported entrypoints:

* The backend-e2e workflow phases ``obs5-real-migration``, ``obs5-primary-*``,
  ``obs5-pg-original``, ``obs5-pg-supplement`` and ``check-obs5`` on the profile
  prepared by ``test_manual_cop_postgresql.py prepare``.
* ``python tests/e2e/test_ml_lifecycle_verification.py --prepared`` on an already
  prepared owned profile; it consumes (never re-prepares) the canonical source
  manifest and seals OBS-5 scope only, not full backend qualification.

``run_obs5_verification.ps1`` is UNSUPPORTED: its fixed historical source fingerprint
and external manifest are not the canonical tested-commit source proof.
"""

from __future__ import annotations

import asyncio
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
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.models import MLLifecycleStateRecord
from packages.ml.lifecycle import Lifecycle, LifecycleUnavailable, invalidate_lifecycle
from packages.ml.model_status import get_model_status
from tests.e2e.test_manual_cop_postgresql import (
    _publish_lifecycle_json,
    atomic_lifecycle_seal,
    lifecycle_evidence_lock,
    CURRENT_HEAD_REVISION,
    OBS5_SCHEMA_PHASES,
    OBS5_TEST_PHASES,
    SOURCE_MANIFEST_DIGEST_ENV,
    SyntheticVerificationEvidence,
    _inspect_owned_resources,
    _parent_relations,
    _primary_marker,
    current_verification_source_proof,
    guard_owned_database,
    migration_targets,
    owned_migration_child,
    owned_test_environment,
    owned_test_network,
    reconcile_verification_partitions,
    retained_storage_state,
    run_primary_head_upgrade,
    run_verification_phase,
    validate_bound_verification_evidence,
)
from tests.e2e.test_ml_lifecycle_postgresql import (
    _artifact,
    _owner,
    _public_fixture_state,
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


async def test_owned_fingerprint_read_stability_reverted_write_and_sequence_rollback(
    lifecycle_database,
):
    """Exercise the diagnostic primitive without intentionally writing public storage."""
    database = lifecycle_database
    schema = database.schema
    quoted = database.engine.dialect.identifier_preparer.quote_identifier(schema)

    async def observe():
        components = {}
        async with database.engine.begin() as connection:
            state = await _public_fixture_state(connection, schema=schema, diagnostics=components)
        return state, components

    async with database.engine.begin() as connection:
        await connection.execute(
            text(f"INSERT INTO {quoted}.settings (key,value) VALUES (:key,:value)"),
            {"key": "fingerprint-diagnostic-only", "value": "synthetic-not-logged"},
        )
    before, baseline = await observe()
    repeated, repeat_components = await observe()
    assert repeated == before and repeat_components == baseline
    async with database.engine.begin() as connection:
        await connection.execute(
            text(f"UPDATE {quoted}.settings SET value=value WHERE key=:key"),
            {"key": "fingerprint-diagnostic-only"},
        )
    changed, after = await observe()
    relation = f"{schema}.settings"
    assert changed != before
    assert after[relation]["content"] == baseline[relation]["content"]
    assert after[relation]["versions"] != baseline[relation]["versions"]
    assert after[relation]["catalog"] == baseline[relation]["catalog"]
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text("SELECT nextval(CAST(:sequence AS regclass))"),
                {"sequence": f"{schema}.ml_lifecycle_token_seq"},
            )
        finally:
            await transaction.rollback()
    advanced, sequence_after = await observe()
    sequence = f"{schema}.ml_lifecycle_token_seq"
    assert advanced != changed
    assert sequence_after[sequence]["value_state"] != after[sequence]["value_state"]
    assert sequence_after[sequence]["configuration"] == after[sequence]["configuration"]
    assert sequence_after[sequence]["dependencies"] == after[sequence]["dependencies"]


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


def _retained_data(state):
    # Physical shape legitimately changes across 034-036; retained data must not.
    return {key: state[key] for key in ("rows", "sequence", "sequence_configuration")}


async def _primary_state():
    engine = create_async_engine(
        os.environ["DATABASE_URL"], poolclass=NullPool, connect_args={"command_timeout": 20}
    )
    try:
        async with engine.connect() as connection:
            await guard_owned_database(connection)
            table = (
                await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
            ).scalar_one()
            marker = (
                None
                if table is None
                else tuple(
                    (await connection.execute(text("SELECT version_num FROM alembic_version")))
                    .scalars()
                    .all()
                )
            )
            return marker, await _parent_relations(connection)
    finally:
        await engine.dispose()


class TestOBS5RealMigration:
    async def test_032_to_033_round_trip_check_and_populated_refusal(self, request):
        """Genuine 032->033->032->033 on a fresh receipt-bound owned child database."""
        assert request.config.option.noconftest
        receipt = owned_test_environment()
        _source_receipt(receipt)
        head, _ = migration_targets()
        assert head == CURRENT_HEAD_REVISION
        with owned_test_network():
            primary = await _primary_state()
            async with owned_migration_child("obsreal") as child:
                await child.alembic("upgrade", "032")
                assert await _marker(child.engine) == "032"
                retained = await _retained(child.engine)
                await child.alembic("upgrade", "033")
                assert await _marker(child.engine) == "033"
                async with child.engine.connect() as connection:
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
                await child.alembic("downgrade", "032")
                assert await _marker(child.engine) == "032"
                async with child.engine.connect() as connection:
                    for name in (*TABLES, "ml_lifecycle_token_seq"):
                        assert (
                            await connection.execute(
                                text("SELECT to_regclass(:name)"), {"name": "public." + name}
                            )
                        ).scalar_one() is None
                assert await _retained(child.engine) == retained
                await child.alembic("upgrade", "033")
                lifecycle = Lifecycle(child.session_context)
                before = await lifecycle.initialize()
                # Historical 033 populated-downgrade refusal, before any later revision.
                with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
                    await child.alembic("downgrade", "032")
                assert await _marker(child.engine) == "033"
                assert await lifecycle.read() == before
                assert await _retained(child.engine) == retained
                for target in ("034", "035", head):
                    await child.alembic("upgrade", target)
                    assert await _marker(child.engine) == target
                    assert await lifecycle.read() == before
                    assert _retained_data(await _retained(child.engine)) == _retained_data(retained)
                await child.alembic("check")
            assert await _primary_state() == primary, "real migration touched the primary"


async def test_real_linux_fsync_precedes_sql_cas(lifecycle_database, monkeypatch):
    database = lifecycle_database
    lease = await _owner(database, demand=True)
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
        artifact = await _artifact(database, "real-linux-order")
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
    lease = await _owner(database, demand=True)
    artifacts = [
        await _artifact(database, "install-first"),
        await _artifact(database, "install-second"),
    ]
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
        lease = await _owner(database, demand=True)
        artifact = await _artifact(database, "maintenance-off-reset")
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


UNSUPPORTED_ENTRYPOINT = (
    "unsupported OBS-5 entrypoint: use the backend-e2e workflow or --prepared on an "
    "owned profile prepared by test_manual_cop_postgresql.py; run_obs5_verification.ps1 "
    "is UNSUPPORTED"
)
OBS5_RUNNER_PHASES = (
    "obs5-inventory",
    "obs5-real-migration",
    "obs5-primary",
    "obs5-pg-original",
    "obs5-pg-supplement",
    "check-obs5",
)


@atomic_lifecycle_seal
def seal_obs5_scope(evidence, proof, current, completed, read_marker):
    """OBS-5-only seal; refuses unless every bound receipt validates (never full backend)."""
    evidence = Path(evidence)
    assert tuple(completed) == OBS5_RUNNER_PHASES, "OBS-5 phases did not all complete"
    assert (current["commit"], current["digest"], current["binding"]) == (
        proof["commit"],
        proof["digest"],
        proof["binding"],
    ), "verification source drift"
    collected, partitions = reconcile_verification_partitions(
        evidence, "obs5-inventory", OBS5_TEST_PHASES
    )
    outside = sorted(
        {node.split("::", 1)[0] for node in collected}
        - {path.removeprefix("heatpump-optimizer/") for path in proof["binding"]}
    )
    assert not outside, f"inventoried test source is outside the source manifest: {outside[0]}"
    head, processes = validate_bound_verification_evidence(
        evidence, proof, partitions, OBS5_SCHEMA_PHASES, "obs5-inventory"
    )
    assert read_marker() == CURRENT_HEAD_REVISION, "primary head was not proven"
    seal = {
        "scope": "OBS-5 only; not full backend release qualification",
        "passed": True,
        "commit": proof["commit"],
        "source_manifest_sha256": proof["digest"],
        "nodes": sorted(collected),
        "partitions": {name: sorted(nodes) for name, nodes in partitions.items()},
        "primary_head": head,
        "phases": list(completed),
        "processes": processes,
    }
    _publish_lifecycle_json(evidence / "obs5-scope-seal.json", seal)
    return seal


def _run_owned_verification(prepared=False):
    """Dedicated OBS-5 runner; consumes the canonical prepared source proof, fail-closed."""
    if not prepared:
        print(UNSUPPORTED_ENTRYPOINT, file=sys.stderr, flush=True)
        return 2
    receipt = owned_test_environment()
    proof = current_verification_source_proof()
    source = _source_receipt(receipt)
    root = Path(receipt["temp_root"])
    assert Path(os.environ["MODEL_DIR"]).parent == root, "model root is not the owned profile"
    evidence = root / "evidence"
    preflight = json.loads((evidence / "preflight.json").read_text(encoding="utf-8"))
    assert (preflight["commit"], preflight["source_manifest_sha256"]) == (
        proof["commit"],
        proof["digest"],
    ), "preflight source mismatch"
    export = os.environ.get("HPO_OBS5_EXPORT")
    export = Path(export) if export else None
    assert export is None or export.is_dir()
    pytest_options = ["-q", "-p", "no:cacheprovider", "-rA"]
    steps = (
        (
            "obs5-inventory",
            [
                "-m",
                "pytest",
                "--noconftest",
                "tests/e2e/test_ml_lifecycle_postgresql.py",
                "tests/e2e/test_ml_lifecycle_verification.py",
                "--collect-only",
                *pytest_options,
            ],
        ),
        (
            "obs5-real-migration",
            [
                "-m",
                "pytest",
                "--noconftest",
                "tests/e2e/test_ml_lifecycle_verification.py::TestOBS5RealMigration",
                *pytest_options,
            ],
        ),
        ("obs5-primary", None),
        (
            "obs5-pg-original",
            [
                "-m",
                "pytest",
                "--noconftest",
                "tests/e2e/test_ml_lifecycle_postgresql.py",
                *pytest_options,
            ],
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
                *pytest_options,
            ],
        ),
        ("check-obs5", ["-m", "alembic", "check"]),
    )
    completed = []
    failed = False
    try:
        _wait_owned_health(root, "postgres", 90)
        _wait_owned_health(root, "redis", 60)
        assert importlib.metadata.version("tzdata") == "2026.4"
        for name, arguments in steps:
            print("OBS5_PHASE=" + name, flush=True)
            try:
                if arguments is None:
                    run_primary_head_upgrade(name)
                else:
                    run_verification_phase(name, [sys.executable, *arguments])
            except Exception as error:
                failed = True
                print(f"OBS5_PHASE_FAILURE={name}:{type(error).__name__}:{error}", flush=True)
                break
            completed.append(name)
        if failed:
            with lifecycle_evidence_lock(evidence):
                assert not any(
                    (evidence / name).exists() for name in ("seal.json", "obs5-scope-seal.json")
                ), "lifecycle receipt store is already sealed"
                _publish_lifecycle_json(
                    evidence / "obs5-scope-seal.json",
                    {
                        "scope": "OBS-5 only; not full backend release qualification",
                        "passed": False,
                        **source,
                        "nodes": [],
                        "phases": completed,
                    },
                )
            return 1
        seal_obs5_scope(
            evidence,
            proof,
            current_verification_source_proof(),
            completed,
            lambda: asyncio.run(_primary_marker()),
        )
        return 0
    finally:
        if export is not None:
            if evidence.exists():
                shutil.copytree(evidence, export / "evidence", dirs_exist_ok=True)
            if (root / "ownership.json").exists():
                shutil.copy2(root / "ownership.json", export / "ownership.json")


async def test_supported_entrypoints_are_documented_and_unprepared_mode_refuses(capsys):
    assert "run_obs5_verification.ps1" in __doc__ and "UNSUPPORTED" in __doc__
    assert _run_owned_verification() == 2
    assert "UNSUPPORTED" in capsys.readouterr().err


@pytest.mark.parametrize("digest", [None, "0" * 64], ids=["missing", "stale"])
async def test_prepared_runner_refuses_stale_source_before_any_phase(digest, monkeypatch):
    receipt = owned_test_environment()
    current_verification_source_proof()
    phase = Mock()
    primary = Mock()
    monkeypatch.setitem(globals(), "run_verification_phase", phase)
    monkeypatch.setitem(globals(), "run_primary_head_upgrade", primary)
    if digest is None:
        monkeypatch.delenv(SOURCE_MANIFEST_DIGEST_ENV)
    else:
        monkeypatch.setenv(SOURCE_MANIFEST_DIGEST_ENV, digest)
    with pytest.raises((AssertionError, KeyError)):
        _run_owned_verification(prepared=True)
    phase.assert_not_called()
    primary.assert_not_called()
    assert not (Path(receipt["temp_root"]) / "evidence" / "obs5-scope-seal.json").exists()


LIFECYCLE_NODE_PREFIX = "tests/e2e/test_ml_lifecycle_postgresql.py::"


class TestOBS5ScopeSeal(SyntheticVerificationEvidence):
    """OBS-only seal over the shared synthetic unit fixture; no process or database."""

    def obs5(self, tmp_path):
        evidence, proof = self.evidence(tmp_path)
        return evidence, proof, lambda: CURRENT_HEAD_REVISION

    async def test_complete_bound_obs5_evidence_seals_obs_scope_only(self, tmp_path):
        evidence, proof, marker = self.obs5(tmp_path)
        seal = seal_obs5_scope(evidence, proof, dict(proof), OBS5_RUNNER_PHASES, marker)
        assert seal["passed"] and seal["scope"].startswith("OBS-5 only")
        assert seal["partitions"] == {name: sorted(self.NODES[name]) for name in OBS5_TEST_PHASES}
        assert seal["primary_head"]["after"] == CURRENT_HEAD_REVISION
        assert json.loads((evidence / "obs5-scope-seal.json").read_text(encoding="utf-8")) == seal
        assert not self.receipt_path(evidence, self.NODES["obs5-pg-supplement"][1]).exists()

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("process_digest", "verification source drift"),
            ("process_source", "verification source drift"),
            ("current_source", "verification source drift"),
            ("schema_check_failed", "did not succeed"),
            ("primary_check_missing", "did not succeed"),
            ("primary_not_head", "primary head was not proven"),
            ("primary_unexpected_before", "primary head was not proven"),
            ("primary_marker_actual", "primary head was not proven"),
            ("preflight_digest", "receipt source digest mismatch"),
            ("ownership_commit", "receipt source digest mismatch"),
            ("no_lifecycle_receipt", "lifecycle receipt"),
            ("missing_supplement_receipt", "lifecycle receipt"),
            ("lifecycle_cleanup", "lifecycle receipt"),
            ("lifecycle_public_drift", "lifecycle receipt"),
            ("lifecycle_digest", "lifecycle receipt"),
            ("lifecycle_foreign_test", "lifecycle receipt"),
            ("lifecycle_duplicate_test", "lifecycle receipt"),
            ("lifecycle_foreign_token", "lifecycle receipt"),
            ("lifecycle_truthy_flag", "lifecycle receipt"),
            ("missing_original_second_receipt", "lifecycle receipt coverage mismatch"),
            ("duplicate_receipt", "lifecycle receipt coverage mismatch"),
            ("extra_resource_free_receipt", "lifecycle receipt coverage mismatch"),
            ("inventory_obligations_missing", "lifecycle receipt inventory is missing or unbound"),
            ("inventory_unbound_digest", "lifecycle receipt inventory is missing or unbound"),
            ("inventory_drops_obligation", "lifecycle receipt inventory drifted"),
            ("obligation_outside_lifecycle", "ran outside a lifecycle phase"),
            ("phase_inventory_drift", "lifecycle receipt inventory drifted"),
            ("unrun_node", "not completely partitioned"),
            ("incomplete_phases", "did not all complete"),
            ("outside_manifest", "outside the source manifest"),
        ],
    )
    async def test_unbound_obs5_evidence_refuses_seal(self, tmp_path, case, message):
        evidence, proof, marker = self.obs5(tmp_path)
        current = dict(proof)
        completed = OBS5_RUNNER_PHASES
        if case == "current_source":
            current["digest"] = "d" * 64
        elif case == "primary_marker_actual":
            marker = lambda: "035"  # noqa: E731
        elif case == "unrun_node":
            nodes = [node for name in OBS5_TEST_PHASES for node in self.NODES[name]]
            self.phase(evidence, "obs5-inventory", [], nodes + [LIFECYCLE_NODE_PREFIX + "test_x"])
        elif case == "incomplete_phases":
            completed = OBS5_RUNNER_PHASES[:-1]
        elif case == "outside_manifest":
            proof["binding"] = {
                path: digest
                for path, digest in proof["binding"].items()
                if not path.endswith("test_ml_lifecycle_postgresql.py")
            }
            current = dict(proof)
        else:
            self.mutate(evidence, case)
        with pytest.raises(AssertionError, match=message):
            seal_obs5_scope(evidence, proof, current, completed, marker)
        assert not (evidence / "obs5-scope-seal.json").exists()


if __name__ == "__main__":
    raise SystemExit(_run_owned_verification(sys.argv[1:] == ["--prepared"]))

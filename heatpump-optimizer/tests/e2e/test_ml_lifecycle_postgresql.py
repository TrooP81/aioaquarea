"""OBS-5 races on receipt-proven disposable PostgreSQL, run with --noconftest.

The coordinator must provision/migrate its own services and pass the tested
source receipt. No service creation, adoption or Compose operation occurs here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from packages.core.config import settings
from packages.core.models import (
    MLArtifactRecord,
    MLForecastIssueRecord,
    MLForecastOutcomeRecord,
    MLLifecycleStateRecord,
    MLMaintenanceRequestRecord,
)
from packages.ml.lifecycle import (
    CASConflict,
    Lifecycle,
    LeaseLost,
    LifecycleUnavailable,
    invalidate_lifecycle,
)
from packages.ml.safe_persistence import publish_signed_artifact
from tests.e2e.test_manual_cop_postgresql import (
    guard_owned_database,
    migration_targets,
    owned_test_environment,
    owned_test_network,
    retained_storage_state,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _migration():
    path = Path(__file__).parents[2] / "migrations/versions/033_ml_observation_lifecycle.py"
    spec = importlib.util.spec_from_file_location("migration_033_pg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), direction)()


def _source_receipt(receipt):
    fingerprint = os.environ["HPO_OBS5_TESTED_REVISION"]
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint), "missing tested source fingerprint"
    assert receipt["revision"].endswith("+" + fingerprint), "ownership/source mismatch"
    report = json.loads(Path(os.environ["HPO_OBS5_SOURCE_RECEIPT"]).read_text(encoding="utf-8"))
    manifest = report["post_change_source_manifest"]
    canonical = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == fingerprint, "source manifest mismatch"
    checkout = Path(__file__).resolve().parents[3]
    for entry in manifest:
        path = checkout / entry["path"]
        assert path.resolve().is_relative_to(checkout), "unsafe source path"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], "source drift"
    return fingerprint


@pytest_asyncio.fixture
async def lifecycle_database(request, tmp_path, monkeypatch):
    assert request.config.option.noconftest, "coordinator-owned suite requires --noconftest"
    assert os.name == "posix", "owned harness requires Linux Unix-socket/process semantics"
    receipt = owned_test_environment()
    fingerprint = _source_receipt(receipt)
    root = Path(receipt["temp_root"])
    assert tmp_path.resolve().is_relative_to(root), "pytest basetemp must be coordinator-owned"
    model_dir = tmp_path / "artifact-fixtures"
    model_dir.mkdir()
    monkeypatch.setattr(settings, "model_dir", str(model_dir))
    schema = "obs5_" + uuid.uuid4().hex
    control = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema + ", public"}},
    )
    started = dt.datetime.now(dt.timezone.utc)
    created = False
    removed = False
    public_unchanged = False
    with owned_test_network():
        try:
            async with control.begin() as connection:
                await guard_owned_database(connection)
                head, _ = migration_targets()
                assert head == "033"
                assert (
                    await connection.execute(text("SELECT version_num FROM public.alembic_version"))
                ).scalar_one() == head
                retained = await retained_storage_state(connection)
                await connection.execute(CreateSchema(schema))
            created = True
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE device_status (ts TIMESTAMPTZ NOT NULL, device_id VARCHAR(128) NOT NULL, "
                        "PRIMARY KEY (ts, device_id))"
                    )
                )
                await connection.run_sync(lambda current: _run_migration(current, "upgrade"))
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

            yield SimpleNamespace(
                lifecycle=Lifecycle(sessions),
                sessions=sessions,
                maker=maker,
                engine=engine,
                model_dir=model_dir,
            )
        finally:
            try:
                await engine.dispose()
                if created:
                    async with control.begin() as connection:
                        await guard_owned_database(connection)
                        public_unchanged = await retained_storage_state(connection) == retained
                        await connection.execute(DropSchema(schema, cascade=True))
                    removed = True
                    assert public_unchanged, "public manual storage drift"
            finally:
                await control.dispose()
                (root / "evidence" / ("obs5-" + request.node.name + ".json")).write_text(
                    json.dumps(
                        {
                            "test": request.node.nodeid,
                            "source_revision": fingerprint,
                            "ownership_token": receipt["token"],
                            "schema": schema,
                            "started_at": started.isoformat(),
                            "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "schema_owned": created,
                            "schema_removed": removed,
                            "public_manual_storage_unchanged": public_unchanged,
                        }
                    ),
                    encoding="utf-8",
                )


def _artifact(database, fit_id, *, checkpoint=None):
    return publish_signed_artifact(
        {"fit_id": fit_id, "weights": [1.25, 3.5]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest={
            "fit_id": fit_id,
            "data_cutoff": dt.datetime.now(dt.timezone.utc).isoformat(),
            "input_schema": "native_inputs_v1",
            "output_schema": "demand_v1",
            "runtime_versions": {"python": "3.12"},
            "evidence_hashes": [],
        },
        round_trip_check=lambda restored: _exact_fit(restored, fit_id),
        checkpoint=checkpoint,
    )


def _exact_fit(restored, fit_id):
    assert restored == {"fit_id": fit_id, "weights": [1.25, 3.5]}


async def _owner(database):
    await database.lifecycle.initialize()
    await database.lifecycle.set_maintenance_enabled(True)
    lease = await database.lifecycle.acquire("worker-" + uuid.uuid4().hex)
    assert lease is not None
    return lease


async def _native_fixture(database, count):
    lease = await _owner(database)
    artifact = _artifact(database, "native-fixture")
    state = await database.lifecycle.read()
    assert (
        await database.lifecycle.publish(
            lease, artifact, expected_revision=state.revision, expected_incumbent=None
        )
    ).committed
    reference = (await database.lifecycle.read()).reference("demand", "candidate")
    issued = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    snapshot = {"scope": {"policy_schema": "native_unchanged_v1"}, "capability": "demand"}
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    rows = [
        {
            "epoch": lease.epoch,
            "device_id": "fixture-device",
            "component": "demand",
            "cohort_key": "cohort-" + str(index),
            "horizon_minutes": 60,
            "issue_kind": "prospective_native",
            "policy_schema": "native_unchanged_v1",
            "issued_at": issued,
            "target_at": issued + dt.timedelta(hours=1),
            "device_day": issued.date(),
            "candidate_hash": artifact.artifact_hash,
            "incumbent_hash": None,
            "snapshot_hash": snapshot_hash,
            "input_snapshot": snapshot,
            "predictions": {"candidate": 1.0, "incumbent": 2.0, "baseline": 2.0},
            "expected_horizons": [60],
        }
        for index in range(count)
    ]
    return lease, reference, rows


async def test_issue_outcome_uniqueness_and_immutable_snapshot_under_concurrency(
    lifecycle_database,
):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 20)
    await asyncio.gather(
        *(database.lifecycle.record_native_issues(lease, reference, rows, slot=0) for _ in range(6))
    )
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLForecastIssueRecord))
        ).scalar_one() == 20
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 20
        record = (await session.execute(select(MLForecastIssueRecord).limit(1))).scalar_one()
        issue_id = record.id
    with pytest.raises(DBAPIError, match="ml_forecast_issue_immutable"):
        async with database.sessions() as session:
            await session.execute(
                update(MLForecastIssueRecord)
                .where(MLForecastIssueRecord.id == issue_id)
                .values(predictions={"candidate": 99.0})
            )
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(MLForecastIssueRecord.predictions).where(
                    MLForecastIssueRecord.id == issue_id
                )
            )
        ).scalar_one() == rows[0]["predictions"]


async def test_jittered_cycle_admission_uses_fixed_utc_grid(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database)
    for _attempt in range(2):
        async with database.sessions() as session:
            await session.execute(
                update(MLLifecycleStateRecord).values(
                    next_maintenance_at=func.clock_timestamp() - dt.timedelta(seconds=3)
                )
            )
        assert await database.lifecycle.begin_cycle(lease)
        admitted = await database.lifecycle.read()
        assert admitted.next_maintenance_at.timestamp() % (6 * 3600) == 0
        async with database.sessions() as session:
            clock = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        assert dt.timedelta() < admitted.next_maintenance_at - clock <= dt.timedelta(hours=6)
        assert await database.lifecycle.begin_cycle(lease) is False


async def test_native_collection_recovers_after_gap_without_fabricated_slots(lifecycle_database):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 3)
    checkpoints = []
    for slot, row in zip((0, 2, 3), rows):
        values = dict(row, cohort_key=f"slot-{slot}")
        await database.lifecycle.record_native_issues(lease, reference, [values], slot=slot)
        checkpoints.append(
            (await database.lifecycle.read()).checkpoints["collection:" + reference.artifact_hash]
        )
    assert [row["complete"] for row in checkpoints] == [True, False, True]
    assert checkpoints[-1]["gaps"] == 1
    assert checkpoints[-1]["expected_slots"] == 4 and checkpoints[-1]["collected_slots"] == 3
    async with database.sessions() as session:
        cohorts = (await session.execute(select(MLForecastIssueRecord.cohort_key))).scalars().all()
        assert set(cohorts) == {"slot-0", "slot-2", "slot-3"}
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 3


async def test_damaged_candidate_invalidation_is_durable_and_fences_publication(lifecycle_database):
    from packages.ml.lifecycle import ArtifactIntegrityError

    database = lifecycle_database
    lease, reference, _rows = await _native_fixture(database, 1)
    before = await database.lifecycle.read()
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    path.write_bytes(b"damaged disposable candidate")
    with pytest.raises(ArtifactIntegrityError, match="Artifact hash mismatch"):
        await database.lifecycle.load_committed("demand", role="candidate")
    await database.lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")
    repaired_state = await Lifecycle(database.sessions).read()
    assert repaired_state.epoch == before.epoch and repaired_state.revision > before.revision
    assert repaired_state.reference("demand", "candidate") is None
    assert (
        repaired_state.checkpoints["invalidated:" + reference.artifact_hash]["reason"]
        == "artifact_integrity_failure"
    )
    replacement = _artifact(database, "repair-after-invalidation")
    stale = await database.lifecycle.publish(
        lease, replacement, expected_revision=before.revision, expected_incumbent=None
    )
    assert stale.committed is False and stale.reason == "publication_conflict"
    fresh = await database.lifecycle.publish(
        lease, replacement, expected_revision=repaired_state.revision, expected_incumbent=None
    )
    assert fresh.committed
    assert (await database.lifecycle.load_committed("demand", role="candidate"))[
        "fit_id"
    ] == "repair-after-invalidation"


async def test_artifact_outage_checkpoint_is_durable_and_recovery_keeps_candidate(
    lifecycle_database, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import Mock
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1)
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    before = await database.lifecycle.read()
    payload = await database.lifecycle.load_committed("demand", role="candidate")
    with monkeypatch.context() as outage:
        outage.setattr(
            module.safe_persistence,
            "verify_signed_artifact",
            Mock(side_effect=PermissionError("owned offline storage probe")),
        )
        with pytest.raises(module.ArtifactUnavailableError) as failure:
            await database.lifecycle.load_committed("demand", role="candidate")
        assert (
            await database.lifecycle.record_artifact_unavailability(lease, failure.value) is False
        )
    restarted = Lifecycle(database.sessions)
    unavailable = await restarted.read()
    key = "availability:" + reference.artifact_hash
    assert unavailable.checkpoints[key]["status"] == "unavailable"
    assert unavailable.checkpoints[key]["attempts"] == 1
    assert unavailable.references == before.references
    assert (unavailable.epoch, unavailable.revision, unavailable.fence) == (
        before.epoch,
        before.revision,
        before.fence,
    )
    assert (
        unavailable.checkpoints["collection:" + reference.artifact_hash]
        == before.checkpoints["collection:" + reference.artifact_hash]
    )
    assert await restarted.load_committed("demand", role="candidate") == payload
    await restarted.clear_artifact_unavailability(lease, reference)
    recovered = await restarted.read()
    assert recovered.checkpoints[key]["status"] == "available"
    assert recovered.references == before.references and recovered.revision == before.revision
    assert (
        recovered.checkpoints["collection:" + reference.artifact_hash]
        == before.checkpoints["collection:" + reference.artifact_hash]
    )
    with pytest.raises(LeaseLost):
        await restarted.record_artifact_unavailability(
            replace(lease, fence=lease.fence - 1), failure.value
        )
    assert (await restarted.read()).checkpoints == recovered.checkpoints


async def test_cycle_retention_is_bounded_and_preserves_protected_evidence(
    lifecycle_database, monkeypatch
):
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1)
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(days=100)
    protected = dict(
        rows[0],
        issued_at=old,
        target_at=old + dt.timedelta(hours=1),
        device_day=old.date(),
        created_at=old,
    )
    await database.lifecycle.record_native_issues(lease, reference, [protected], slot=0)
    unreferenced = _artifact(database, "expired-unreferenced-fit")
    async with database.sessions() as session:
        session.add(
            MLArtifactRecord(
                artifact_hash=unreferenced.artifact_hash,
                epoch=lease.epoch,
                component=unreferenced.component,
                schema_version=unreferenced.schema_version,
                fit_id=unreferenced.manifest["fit_id"],
                manifest=unreferenced.manifest,
                role="unreferenced",
                committed_revision=lease.revision,
                created_at=old,
            )
        )
        await session.flush()
        for index in range(6):
            stamp = now if index == 5 else old
            issue = MLForecastIssueRecord(
                **dict(
                    protected,
                    cohort_key=f"unprotected-{index}",
                    candidate_hash=unreferenced.artifact_hash,
                    issued_at=stamp,
                    target_at=stamp + dt.timedelta(hours=1),
                    device_day=stamp.date(),
                    created_at=stamp,
                )
            )
            session.add(issue)
            await session.flush()
            session.add(
                MLForecastOutcomeRecord(
                    issue_id=issue.id,
                    epoch=lease.epoch,
                    device_id=issue.device_id,
                    component=issue.component,
                    horizon_minutes=60,
                    canonical_key=issue.cohort_key,
                    status="excluded",
                    exclusion_reason="unknown_execution",
                    result={},
                )
            )
            session.add(
                MLMaintenanceRequestRecord(
                    id=str(uuid.uuid4()),
                    epoch=lease.epoch,
                    component="demand",
                    source="test",
                    dedup_key=f"retention-{index}",
                    status="queued" if index == 5 else "succeeded",
                    requested_at=old,
                    finished_at=old,
                    details={},
                )
            )
    monkeypatch.setattr(module, "RETENTION_BATCH_SIZE", 2)
    results = [await database.lifecycle.prune_evidence(lease, now=now) for _pass in range(3)]
    assert results == [
        {"outcomes": 2, "issues": 2, "requests": 2},
        {"outcomes": 2, "issues": 2, "requests": 2},
        {"outcomes": 1, "issues": 1, "requests": 1},
    ]
    async with database.sessions() as session:
        remaining = (
            (await session.execute(select(MLForecastIssueRecord.cohort_key))).scalars().all()
        )
        assert set(remaining) == {protected["cohort_key"], "unprotected-5"}
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 2
        assert (
            await session.execute(select(MLMaintenanceRequestRecord.status))
        ).scalar_one() == "queued"
    artifact = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    orphan = artifact.parent / ("1" * 64 + ".pkl")
    recent = artifact.parent / ("2" * 64 + ".pkl")
    orphan.write_bytes(b"expired orphan")
    recent.write_bytes(b"grace-period orphan")
    old_file = (now - dt.timedelta(hours=25)).timestamp()
    for path in (artifact, orphan):
        os.utime(path, (old_file, old_file))
    deleted = []
    for _pass in range(2):
        deleted.extend(await database.lifecycle.prune_artifacts(lease, now=now))
    assert deleted == [orphan.name] and artifact.exists() and recent.exists()
    await database.lifecycle.set_maintenance_enabled(False)
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_evidence(lease, now=now)
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_artifacts(lease, now=now)


async def test_pagination_concurrent_progress_late_targets_and_retry(lifecycle_database):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 251)
    for slot, offset in enumerate(range(0, len(rows), 100)):
        await database.lifecycle.record_native_issues(
            lease, reference, rows[offset : offset + 100], slot=slot
        )
    key = "fixture-scan"
    window_start = rows[0]["issued_at"] - dt.timedelta(hours=1)
    window_end = dt.datetime.now(dt.timezone.utc)
    checkpoint = await database.lifecycle.begin_evidence_scan(
        lease, reference, key=key, window_start=window_start, window_end=window_end
    )
    assert checkpoint["expected_rows"] == 251
    pages = await asyncio.gather(*(database.lifecycle.evidence_page(lease, key) for _ in range(2)))
    assert [entry["issue"]["id"] for entry in pages[0]["rows"]] == [
        entry["issue"]["id"] for entry in pages[1]["rows"]
    ]
    incomplete = {
        "status": "incomplete",
        "exclusion_reason": "missing_target",
        "observed_at": None,
        "result": {},
    }
    results = await asyncio.gather(
        *(
            database.lifecycle.commit_evidence_page(
                lease, key, page, [dict(incomplete) for _ in page["rows"]]
            )
            for page in pages
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, CASConflict) for result in results) == 1
    seen = {entry["issue"]["id"] for entry in pages[0]["rows"]}
    while True:
        page = await database.lifecycle.evidence_page(lease, key)
        if page["checkpoint"]["complete"]:
            break
        ids = {entry["issue"]["id"] for entry in page["rows"]}
        assert not seen & ids
        seen.update(ids)
        await database.lifecycle.commit_evidence_page(
            lease, key, page, [dict(incomplete) for _ in page["rows"]]
        )
    assert len(seen) == 251
    await database.lifecycle.begin_evidence_scan(
        lease, reference, key=key, window_start=window_start, window_end=window_end
    )
    while True:
        page = await database.lifecycle.evidence_page(lease, key)
        if page["checkpoint"]["complete"]:
            break
        outcomes = [
            {
                "status": "valid",
                "exclusion_reason": None,
                "observed_at": entry["issue"]["target_at"],
                "result": {"observed": 1.1, **entry["issue"]["predictions"]},
            }
            for entry in page["rows"]
        ]
        await database.lifecycle.commit_evidence_page(lease, key, page, outcomes)
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(MLForecastOutcomeRecord)
                .where(MLForecastOutcomeRecord.status == "valid")
            )
        ).scalar_one() == 251
        assert (
            await session.execute(select(func.count()).select_from(MLForecastIssueRecord))
        ).scalar_one() == 251


async def test_concurrent_init_keeps_one_epoch(lifecycle_database):
    database = lifecycle_database
    states = await asyncio.gather(*(database.lifecycle.initialize() for _ in range(8)))
    assert len({state.epoch for state in states}) == 1
    assert len({state.revision for state in states}) == 1
    assert all(not state.maintenance_enabled for state in states)
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLLifecycleStateRecord))
        ).scalar_one() == 1
        assert (
            await session.execute(
                text(
                    "SELECT cycle FROM pg_sequences WHERE schemaname=current_schema() AND sequencename='ml_lifecycle_token_seq'"
                )
            )
        ).scalar_one() is False


async def test_two_maintenance_owners_contend_for_one_lease(lifecycle_database):
    database = lifecycle_database
    await database.lifecycle.initialize()
    await database.lifecycle.set_maintenance_enabled(True)
    owners = ["poller-" + uuid.uuid4().hex for _ in range(2)]
    claims = await asyncio.gather(*(database.lifecycle.acquire(owner) for owner in owners))
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    loser = owners[claims.index(None)]
    assert (await database.lifecycle.read()).lease_owner == winner.owner
    assert await database.lifecycle.acquire(loser) is None
    await database.lifecycle.release(winner)
    successor = await database.lifecycle.acquire(loser)
    assert successor is not None and successor.epoch == winner.epoch
    assert successor.fence > winner.fence
    with pytest.raises(LeaseLost):
        await database.lifecycle.renew(winner)
    await database.lifecycle.release(successor)


async def test_concurrent_publication_has_one_CAS_winner(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database)
    artifacts = [_artifact(database, "fit-1"), _artifact(database, "fit-2")]
    results = await asyncio.gather(
        *(
            database.lifecycle.publish(
                lease, artifact, expected_revision=lease.revision, expected_incumbent=None
            )
            for artifact in artifacts
        )
    )
    assert sum(result.committed for result in results) == 1
    winner = artifacts[next(index for index, result in enumerate(results) if result.committed)]
    state = await database.lifecycle.read()
    assert state.reference("demand", "candidate").artifact_hash == winner.artifact_hash
    assert await database.lifecycle.load_committed("demand", role="candidate") == {
        "fit_id": winner.manifest["fit_id"],
        "weights": [1.25, 3.5],
    }
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLArtifactRecord))
        ).scalar_one() == 1
    assert all(artifact.path.exists() for artifact in artifacts)


async def test_lease_loss_fences_late_worker(lifecycle_database):
    database = lifecycle_database
    old = await _owner(database)
    async with database.sessions() as session:
        await session.execute(
            update(MLLifecycleStateRecord).values(
                lease_expires_at=func.clock_timestamp() - dt.timedelta(seconds=1),
            )
        )
    current = await database.lifecycle.acquire("replacement-worker")
    assert current is not None and current.fence > old.fence and current.revision > old.revision
    stale = await database.lifecycle.publish(
        old,
        _artifact(database, "late-fit"),
        expected_revision=old.revision,
        expected_incumbent=None,
    )
    assert not stale.committed and stale.reason == "lease_lost"
    with pytest.raises(LeaseLost):
        await database.lifecycle.renew(old)
    renewed = await database.lifecycle.renew(current)
    assert renewed.fence == current.fence
    result = await database.lifecycle.publish(
        renewed,
        _artifact(database, "current-fit"),
        expected_revision=current.revision,
        expected_incumbent=None,
    )
    assert result.committed


@pytest.mark.parametrize(
    "stage",
    [
        "before_file",
        "file_synced",
        "renamed",
        "directory_synced",
        "before_commit",
        "after_commit",
        "lost_ack",
    ],
)
async def test_crash_before_file_after_file_before_commit_after_commit_and_lost_ack(
    lifecycle_database, stage
):
    database = lifecycle_database
    lease = await _owner(database)
    installed = []
    if stage == "before_file":
        with pytest.raises(LifecycleUnavailable):
            await database.lifecycle.load_committed("demand", role="candidate")
        assert not list(database.model_dir.rglob("*.pkl"))
        return
    if stage in {"file_synced", "renamed", "directory_synced"}:

        def crash(checkpoint):
            if checkpoint == stage:
                raise OSError("injected publication interruption")

        with pytest.raises(OSError, match="injected publication interruption"):
            _artifact(database, "interrupted-fit", checkpoint=crash)
        with pytest.raises(LifecycleUnavailable):
            await database.lifecycle.load_committed("demand", role="candidate")
        assert len(list(database.model_dir.rglob("*.pkl"))) == int(stage != "file_synced")
        return
    artifact = _artifact(database, "commit-fit")
    fired = False

    @asynccontextmanager
    async def interrupted_sessions():
        nonlocal fired
        async with database.maker() as session:
            try:
                yield session
                if not fired:
                    fired = True
                    if stage == "before_commit":
                        await session.rollback()
                    else:
                        await session.commit()
                    raise OSError("injected commit acknowledgement loss")
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    if stage in {"before_commit", "lost_ack"}:
        lifecycle = Lifecycle(interrupted_sessions)
        result = await lifecycle.publish(
            lease,
            artifact,
            expected_revision=lease.revision,
            expected_incumbent=None,
            on_committed=installed.append,
        )
        assert result.readback
        assert result.committed is (stage == "lost_ack")
        assert len(installed) == int(stage == "lost_ack")
    else:

        def after_commit(reference):
            raise OSError("injected post-commit process interruption")

        with pytest.raises(OSError, match="post-commit"):
            await database.lifecycle.publish(
                lease,
                artifact,
                expected_revision=lease.revision,
                expected_incumbent=None,
                on_committed=after_commit,
            )
    restarted = Lifecycle(database.sessions)
    if stage == "before_commit":
        with pytest.raises(LifecycleUnavailable):
            await restarted.load_committed("demand", role="candidate")
    else:
        assert (await restarted.load_committed("demand", role="candidate"))[
            "fit_id"
        ] == "commit-fit"
    assert artifact.path.exists()


async def test_pruning_races_and_reset_never_recreate_epoch(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database)
    artifact = _artifact(database, "racing-fit")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).timestamp()
    os.utime(artifact.path, (old, old))
    enumerated = asyncio.Event()
    resume = asyncio.Event()

    class PausedPruner(Lifecycle):
        async def _lock(self, session, current_lease):
            enumerated.set()
            await resume.wait()
            await super()._lock(session, current_lease)

    task = asyncio.create_task(PausedPruner(database.sessions).prune_artifacts(lease))
    try:
        await asyncio.wait_for(enumerated.wait(), timeout=10)
        committed = await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=lease.revision,
            expected_incumbent=None,
        )
        assert committed.committed
    finally:
        resume.set()
        deleted = await asyncio.wait_for(task, timeout=10)
    assert deleted == [] and artifact.path.exists()
    await database.lifecycle.retain_artifact(
        lease,
        artifact.artifact_hash,
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
    )
    before = await database.lifecycle.read()
    async with database.sessions() as session:
        reset = await invalidate_lifecycle(session)
    after = await database.lifecycle.initialize()
    assert reset["epoch"] == after.epoch == before.epoch
    assert after.revision > before.revision and after.fence > before.fence
    assert not after.references and after.lease_owner is None and not after.maintenance_enabled
    with pytest.raises(LifecycleUnavailable):
        await database.lifecycle.load_committed("demand", role="candidate")
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_artifacts(lease)
    assert artifact.path.exists()
    await database.lifecycle.set_maintenance_enabled(True)
    current = await database.lifecycle.acquire("new-owner")
    assert current is not None
    assert await database.lifecycle.prune_artifacts(current) == []
    assert artifact.path.exists()


async def test_migration_033_empty_downgrade_and_populated_refusal(lifecycle_database):
    database = lifecycle_database
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda current: _run_migration(current, "downgrade"))
        assert (
            await connection.execute(
                text("SELECT to_regclass(current_schema() || '.ml_lifecycle_state')")
            )
        ).scalar_one() is None
        await connection.run_sync(lambda current: _run_migration(current, "upgrade"))
    state = await database.lifecycle.initialize()
    with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda current: _run_migration(current, "downgrade"))
    retained = await database.lifecycle.read()
    assert (
        retained.epoch == state.epoch
        and retained.revision == state.revision
        and retained.fence == state.fence
    )
    assert retained.maintenance_enabled is False


async def test_restart_and_rollback_preserve_restore_and_embargo(lifecycle_database):
    from unittest.mock import AsyncMock
    from packages.core.models import PlanActionRecord, PlanRecord
    from packages.core.plan_lifecycle import supersede_active_plan
    from packages.core.safety_reverts import dhw_embargoed, unresolved_revert_summary
    from packages.core.services.aquarea import AquareaWrapper

    database = lifecycle_database
    now = dt.datetime.now(dt.timezone.utc)
    before = await database.lifecycle.initialize()
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda current: PlanRecord.__table__.create(current))
        await connection.run_sync(lambda current: PlanActionRecord.__table__.create(current))
    async with database.sessions() as session:
        session.add(
            PlanRecord(
                id=1,
                horizon_start=now,
                horizon_end=now + dt.timedelta(days=1),
                plan_json='{"actions": []}',
                status="active",
            )
        )
        session.add(
            PlanActionRecord(
                id=11762,
                plan_id=1,
                device_id="fixture-device",
                action_type="force_dhw_on",
                payload_json="{}",
                status="executed",
                scheduled_ts=now - dt.timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            PlanActionRecord(
                id=11763,
                plan_id=1,
                device_id="fixture-device",
                action_type="force_dhw_off",
                payload_json="{}",
                status="pending",
                reverts_action_id=11762,
                scheduled_ts=now - dt.timedelta(minutes=30),
            )
        )
    async with database.sessions() as session:
        await supersede_active_plan(session, reason="guarded_rollback_fixture")
    for _phase in ("restart", "guarded_rollback"):
        state = await Lifecycle(database.sessions).read()
        assert state.epoch == before.epoch
        assert state.maintenance_enabled is False
        async with database.sessions() as session:
            restore = await session.get(PlanActionRecord, 11763)
            source = await session.get(PlanActionRecord, 11762)
            obligations = await unresolved_revert_summary(session, now=now)
        assert restore.status == "pending" and restore.reverts_action_id == source.id
        assert source.status == "executed"
        assert obligations["count"] == 1
        assert dhw_embargoed([restore], "fixture-device") is True
        wrapper = AquareaWrapper(read_only=False)
        wrapper._preflight_control_check = AsyncMock()
        with pytest.raises(PermissionError, match="observe-only build"):
            await wrapper.force_dhw(True)
        wrapper._preflight_control_check.assert_not_awaited()

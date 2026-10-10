"""Security and integrity tests for persisted ML artifacts."""

from __future__ import annotations

import hashlib
import os
import datetime as dt
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.config import settings
from packages.ml import safe_persistence
from packages.ml.safe_persistence import (
    _validate_path,
    artifact_path,
    load_signed_artifact,
    publish_signed_artifact,
    safe_dump,
    safe_load,
    safe_publish_temp,
    safe_write_temp,
)


@pytest.fixture
def model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "models"
    directory.mkdir()
    monkeypatch.setattr(settings, "model_dir", str(directory))
    monkeypatch.setattr(settings, "secret_key", "test-only-model-signing-key")
    return directory


def test_signed_model_round_trip(model_dir: Path) -> None:
    path = model_dir / "model.pkl"

    safe_dump({"version": 1, "weights": [1.0, 2.0]}, path)

    assert safe_load(path) == {"version": 1, "weights": [1.0, 2.0]}


def test_signed_temp_artifact_is_not_visible_until_published(model_dir: Path) -> None:
    path = model_dir / "model.pkl"
    temp_path = safe_write_temp({"version": 2}, path)

    assert temp_path.exists()
    assert not path.exists()

    safe_publish_temp(temp_path, path)

    assert safe_load(path) == {"version": 2}


def test_rejects_parent_traversal(model_dir: Path) -> None:
    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(model_dir / ".." / "escaped.pkl")


def test_rejects_sibling_with_matching_string_prefix(model_dir: Path) -> None:
    sibling = model_dir.parent / f"{model_dir.name}-backup" / "model.pkl"

    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(sibling)


def test_rejects_symlink_that_escapes_model_directory(model_dir: Path) -> None:
    outside = model_dir.parent / "outside"
    outside.mkdir()
    link = model_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(link / "model.pkl")


def test_rejects_tampered_model(model_dir: Path) -> None:
    path = model_dir / "model.pkl"
    safe_dump({"trusted": True}, path)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="HMAC mismatch"):
        safe_load(path)


@pytest.fixture
def artifact_manifest() -> dict:
    return {
        "fit_id": "fit-1",
        "data_cutoff": "2026-10-08T00:00:00+00:00",
        "input_schema": "native_inputs_v1",
        "output_schema": "demand_v1",
        "runtime_versions": {"python": "3.11"},
        "evidence_hashes": [],
    }


@pytest.fixture
def artifact_directory(model_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = model_dir / "lifecycle"
    directory.mkdir()
    if os.name == "nt":
        monkeypatch.setattr(safe_persistence, "_fsync_directory", lambda directory: None)
    return directory


def test_durable_publication_order_and_exact_fit_round_trip(
    artifact_directory: Path, artifact_manifest: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    real_sync = os.fsync
    real_rename = safe_persistence._rename_immutable

    def file_sync(descriptor):
        real_sync(descriptor)
        events.append("file_fsync")

    def rename(source, destination):
        real_rename(source, destination)
        events.append("atomic_rename")

    def round_trip(restored):
        assert restored == {"weights": [1.25, 3.5]}
        events.append("round_trip")

    monkeypatch.setattr(os, "fsync", file_sync)
    monkeypatch.setattr(safe_persistence, "_rename_immutable", rename)
    monkeypatch.setattr(
        safe_persistence,
        "_fsync_directory",
        lambda directory: events.append(
            "parent_directory_fsync"
            if directory == artifact_directory.parent
            else "directory_fsync"
        ),
    )
    artifact = publish_signed_artifact(
        {"weights": [1.25, 3.5]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
        round_trip_check=round_trip,
        checkpoint=events.append,
    )

    assert events == [
        "parent_directory_fsync",
        "file_fsync",
        "file_synced",
        "atomic_rename",
        "renamed",
        "directory_fsync",
        "directory_synced",
        "round_trip",
        "verified",
    ]
    assert artifact.path.name == f"{artifact.artifact_hash}.pkl"
    assert load_signed_artifact(
        artifact.artifact_hash, component="demand", schema_version="demand_lifecycle_v1"
    ) == {"weights": [1.25, 3.5]}
    assert not list(artifact_directory.glob(".artifact-*"))


@pytest.mark.parametrize("tampering", ["hash", "hmac", "schema"])
def test_hash_hmac_schema_tampering_refuses_load(
    artifact_directory: Path,
    artifact_manifest: dict,
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    artifact = publish_signed_artifact(
        {"weights": [1.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    expected_hash = artifact.artifact_hash
    schema = "demand_lifecycle_v1"
    raw = bytearray(artifact.path.read_bytes())
    if tampering == "hash":
        raw[-1] ^= 1
        artifact.path.write_bytes(raw)
    elif tampering == "hmac":
        raw[0] ^= 1
        expected_hash = hashlib.sha256(raw).hexdigest()
        artifact_path(expected_hash).write_bytes(raw)
    else:
        schema = "demand_lifecycle_v2"

    def forbidden_deserialization(payload):
        pytest.fail("Untrusted or incompatible bytes must not be deserialized")

    monkeypatch.setattr(safe_persistence.pickle, "loads", forbidden_deserialization)
    with pytest.raises(
        ValueError, match={"hash": "hash", "hmac": "HMAC", "schema": "schema"}[tampering]
    ):
        load_signed_artifact(expected_hash, component="demand", schema_version=schema)


def test_immutable_artifact_is_not_overwritten(
    artifact_directory: Path, artifact_manifest: dict
) -> None:
    artifact = publish_signed_artifact(
        {"weights": [1.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    artifact.path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="Immutable artifact"):
        publish_signed_artifact(
            {"weights": [1.0]},
            component="demand",
            schema_version="demand_lifecycle_v1",
            manifest=artifact_manifest,
        )
    assert artifact.path.read_bytes() == b"corrupt"
    assert not list(artifact_directory.glob(".artifact-*"))


def test_directory_sync_failure_never_returns_publication_reference(
    artifact_directory: Path, artifact_manifest: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_sync(directory):
        if directory == artifact_directory:
            raise OSError("directory sync unavailable")

    monkeypatch.setattr(safe_persistence, "_fsync_directory", failed_sync)
    with pytest.raises(OSError, match="directory sync unavailable"):
        publish_signed_artifact(
            {"weights": [1.0]},
            component="demand",
            schema_version="demand_lifecycle_v1",
            manifest=artifact_manifest,
        )
    assert len(list(artifact_directory.glob("*.pkl"))) == 1
    assert not list(artifact_directory.glob(".artifact-*"))


def test_preexisting_directory_must_still_sync_parent_before_publication(
    artifact_directory, artifact_manifest, monkeypatch
):
    synced = []

    def failed_parent_sync(directory):
        synced.append(directory)
        if directory == artifact_directory.parent:
            raise OSError("parent entry is not durable")

    monkeypatch.setattr(safe_persistence, "_fsync_directory", failed_parent_sync)
    with pytest.raises(OSError, match="parent entry"):
        publish_signed_artifact(
            {"weights": [1.0]},
            component="demand",
            schema_version="demand_lifecycle_v1",
            manifest=artifact_manifest,
        )
    assert synced == [artifact_directory.parent]
    assert list(artifact_directory.iterdir()) == []


@pytest.fixture
def lifecycle_state():
    from packages.ml.lifecycle import Lease, LifecycleSnapshot

    expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)
    lease = Lease("epoch-1", "worker-1", 5, 7, expiry)
    state = LifecycleSnapshot("epoch-1", 7, 5, True, "worker-1", expiry)
    return state, lease


@pytest.mark.parametrize(
    "changes,expected_incumbent",
    [
        ({"epoch": "other-epoch"}, None),
        ({"revision": 8}, None),
        ({"fence": 6}, None),
        ({"lease_owner": "worker-2"}, None),
        ({"lease_expires_at": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)}, None),
        ({"lease_expires_at": None}, None),
        ({"maintenance_enabled": False}, None),
        ({}, "f" * 64),
    ],
)
def test_cas_fencing_predicates_refuse_stale_publication(
    lifecycle_state, changes, expected_incumbent
):
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import _can_publish, _cas_statement

    state, lease = lifecycle_state
    assert not _can_publish(
        replace(state, **changes),
        lease,
        7,
        "demand",
        expected_incumbent,
        dt.datetime.now(dt.timezone.utc),
    )
    statement = _cas_statement(lease, 7, "demand", expected_incumbent)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    for predicate in (
        "ml_lifecycle_state.epoch =",
        "ml_lifecycle_state.revision =",
        "ml_lifecycle_state.fence =",
        "ml_lifecycle_state.lease_owner =",
        "ml_lifecycle_state.lease_expires_at > clock_timestamp()",
        "ml_lifecycle_state.maintenance_enabled IS true",
        "IS NOT DISTINCT FROM",
        "nextval('ml_lifecycle_token_seq')",
    ):
        assert predicate in compiled


def test_current_cas_predicates_allow_exact_expected_incumbent(lifecycle_state):
    from packages.ml.lifecycle import _can_publish

    state, lease = lifecycle_state
    assert _can_publish(state, lease, 7, "demand", None, dt.datetime.now(dt.timezone.utc))


class PublicationSessions:
    def __init__(self, *, commit_error=False):
        self.session = AsyncMock()
        self.session.execute.side_effect = self.execute
        self.statements = []
        self.commit_error = commit_error
        self.committed = False
        self.final_revision = 11

    async def execute(self, statement):
        from packages.core.models import MLLifecycleStateRecord

        self.statements.append(statement)
        result = Mock(rowcount=1)
        if (
            getattr(statement, "is_update", False)
            and statement.table.name == MLLifecycleStateRecord.__tablename__
        ):
            result.scalar_one_or_none.return_value = self.final_revision
        else:
            result.scalar_one_or_none.return_value = None
        return result

    @asynccontextmanager
    async def __call__(self):
        yield self.session
        self.committed = True
        if self.commit_error:
            raise OSError("lost commit acknowledgement")


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_memory_installation_requires_commit_confirmation_or_readback(
    artifact_directory, artifact_manifest, lifecycle_state, lost_ack
):
    from packages.ml.lifecycle import ArtifactReference, Lifecycle

    state, lease = lifecycle_state
    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    sessions = PublicationSessions(commit_error=lost_ack)
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=state)
    committed = ArtifactReference(
        artifact.artifact_hash,
        state.epoch,
        "demand",
        "demand_lifecycle_v1",
        artifact_manifest,
        "candidate",
        11,
    )
    lifecycle.read = AsyncMock(return_value=replace(state, revision=11, references=(committed,)))
    installed = []

    def install(reference):
        assert sessions.committed
        assert reference.artifact_hash == artifact.artifact_hash
        installed.append(reference)

    result = await lifecycle.publish(
        lease,
        artifact,
        expected_revision=7,
        expected_incumbent=None,
        on_committed=install,
    )

    assert result.committed is True
    assert result.revision == 11
    assert result.readback is lost_ack
    assert len(installed) == 1
    assert lifecycle.read.await_count == int(lost_ack)


async def test_ambiguous_uncommitted_or_unreadable_result_never_installs(
    artifact_directory, artifact_manifest, lifecycle_state
):
    from packages.ml.lifecycle import Lifecycle, LifecycleUnavailable

    state, lease = lifecycle_state
    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    lifecycle = Lifecycle(PublicationSessions(commit_error=True))
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=state)
    lifecycle.read = AsyncMock(return_value=state)
    install = Mock()

    result = await lifecycle.publish(
        lease,
        artifact,
        expected_revision=7,
        expected_incumbent=None,
        on_committed=install,
    )
    assert result.committed is False
    assert result.readback is True
    install.assert_not_called()

    lifecycle.read.side_effect = OSError("database unavailable")
    with pytest.raises(LifecycleUnavailable, match="readback_failed"):
        await lifecycle.publish(
            lease,
            artifact,
            expected_revision=7,
            expected_incumbent=None,
            on_committed=install,
        )
    install.assert_not_called()
    assert artifact.path.exists()


async def test_uncommitted_artifact_is_never_loaded(
    artifact_directory, artifact_manifest, lifecycle_state
):
    from packages.ml.lifecycle import Lifecycle, LifecycleUnavailable

    artifact = publish_signed_artifact(
        {"weights": [99.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    lifecycle = Lifecycle(PublicationSessions())
    lifecycle.read = AsyncMock(return_value=lifecycle_state[0])

    with pytest.raises(LifecycleUnavailable, match="no_committed_artifact"):
        await lifecycle.load_committed("demand")
    assert artifact.path.exists()


async def test_lease_renewal_is_short_and_never_changes_revision_or_fence(lifecycle_state):
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import Lifecycle, LeaseLost

    sessions = PublicationSessions()
    expiry = lifecycle_state[1].expires_at + dt.timedelta(seconds=30)
    sessions.session.execute.side_effect = None
    result = Mock()
    result.scalar_one_or_none.return_value = expiry
    sessions.session.execute.return_value = result
    lifecycle = Lifecycle(sessions)
    renewed = await lifecycle.renew(lifecycle_state[1])
    assert renewed.fence == lifecycle_state[1].fence
    assert renewed.revision == lifecycle_state[1].revision
    assert renewed.expires_at == expiry
    statement = sessions.session.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assignments = compiled.split(" WHERE ", 1)[0]
    assert "SET lease_expires_at=" in assignments
    assert "nextval" not in compiled
    assert "lease_expires_at > clock_timestamp()" in compiled

    result.scalar_one_or_none.return_value = None
    with pytest.raises(LeaseLost):
        await lifecycle.renew(lifecycle_state[1])


async def test_initialization_losers_reread_without_replacing_epoch(lifecycle_state):
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import Lifecycle

    sessions = PublicationSessions()
    lifecycle = Lifecycle(sessions)
    lifecycle._read = AsyncMock(return_value=lifecycle_state[0])

    first = await lifecycle.initialize()
    second = await lifecycle.initialize()

    assert first.epoch == second.epoch == "epoch-1"
    assert lifecycle._read.await_count == 2
    for statement in sessions.statements:
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (id) DO NOTHING" in compiled
        assert "DO UPDATE" not in compiled


async def test_reset_never_recreates_epoch_or_rewinds_tokens():
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import invalidate_lifecycle

    session = AsyncMock()
    exists = Mock()
    exists.scalar_one_or_none.return_value = 1
    changed = Mock()
    changed.mappings.return_value.one.return_value = {
        "epoch": "epoch-1",
        "revision": 19,
        "fence": 20,
    }
    session.execute.side_effect = [exists, changed, Mock(), Mock()]

    result = await invalidate_lifecycle(session)

    assert result == {"epoch": "epoch-1", "revision": 19, "fence": 20}
    statements = [
        str(call.args[0].compile(dialect=postgresql.dialect()))
        for call in session.execute.call_args_list
    ]
    assert "FOR UPDATE" in statements[0]
    assert statements[1].count("nextval('ml_lifecycle_token_seq')") == 2
    assert "epoch=" not in statements[1].split(" WHERE ", 1)[0]
    assert not any("INSERT" in sql or "DELETE" in sql or "setval" in sql for sql in statements)


@pytest.mark.parametrize(
    "reference_kind",
    [
        "incumbent",
        "candidate",
        "rollback",
        "retained",
        "retention_deadline",
        "forecast_issue",
    ],
)
async def test_pruning_protects_references_and_gives_orphans_24_hours(
    artifact_directory, lifecycle_state, monkeypatch, reference_kind
):
    from packages.ml.lifecycle import Lifecycle

    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(hours=25)).timestamp()
    protected = artifact_directory / ("a" * 64 + ".pkl")
    orphan = artifact_directory / ("b" * 64 + ".pkl")
    recent = artifact_directory / ("c" * 64 + ".pkl")
    for path in (protected, orphan, recent):
        path.write_bytes(b"local test orphan")
    for path in (protected, orphan):
        os.utime(path, (old, old))
    sessions = PublicationSessions()

    async def execute(statement):
        result = Mock(rowcount=1)
        if getattr(statement, "is_select", False):
            table = next(iter(statement.selected_columns)).table.name
            params = statement.compile().params
            if table == "ml_artifacts":
                is_protected = protected.stem in params.values()
                record = SimpleNamespace(
                    role=reference_kind
                    if reference_kind in {"incumbent", "candidate", "rollback", "retained"}
                    else "unreferenced",
                    created_at=now - dt.timedelta(days=2),
                    retained_until=now + dt.timedelta(days=7)
                    if reference_kind == "retention_deadline"
                    else None,
                )
                result.scalar_one_or_none.return_value = record if is_protected else None
            else:
                result.scalar_one_or_none.return_value = (
                    42
                    if (reference_kind == "forecast_issue" and protected.stem in params.values())
                    else None
                )
        return result

    sessions.session.execute.side_effect = execute
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=lifecycle_state[0])
    monkeypatch.setattr(safe_persistence, "_fsync_directory", lambda directory: None)

    removed = await lifecycle.prune_artifacts(lifecycle_state[1], now=now)

    assert removed == [orphan.name]
    assert protected.exists() and recent.exists()
    assert lifecycle._lock.await_count == 5


async def test_pruning_lease_loss_refuses_deletion(artifact_directory, lifecycle_state):
    from packages.ml.lifecycle import Lifecycle, LeaseLost

    path = artifact_directory / ("a" * 64 + ".pkl")
    path.write_bytes(b"local orphan")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).timestamp()
    os.utime(path, (old, old))
    lifecycle = Lifecycle(PublicationSessions())
    lifecycle._lock = AsyncMock(side_effect=LeaseLost("lease_lost"))

    with pytest.raises(LeaseLost):
        await lifecycle.prune_artifacts(lifecycle_state[1])
    assert path.exists()


async def test_pruning_rechecks_ownership_immediately_before_deletion(
    artifact_directory, lifecycle_state
):
    from packages.ml.lifecycle import Lifecycle, LeaseLost

    path = artifact_directory / ("a" * 64 + ".pkl")
    path.write_bytes(b"old uncommitted artifact")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).timestamp()
    os.utime(path, (old, old))
    lifecycle = Lifecycle(PublicationSessions())
    lifecycle._read = AsyncMock(return_value=lifecycle_state[0])
    lifecycle._lock = AsyncMock(side_effect=[None, None, LeaseLost("lease_lost")])

    with pytest.raises(LeaseLost):
        await lifecycle.prune_artifacts(lifecycle_state[1])
    assert path.read_bytes() == b"old uncommitted artifact"
    assert lifecycle._lock.await_count == 3


async def test_bounded_orphan_cursor_advances_past_protected_files(
    artifact_directory, lifecycle_state, monkeypatch
):
    from dataclasses import replace
    from packages.ml import lifecycle as module

    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(hours=25)).timestamp()
    paths = [artifact_directory / (f"{index:064x}.pkl") for index in range(4)]
    for path in paths:
        path.write_bytes(b"local test orphan")
        os.utime(path, (old, old))
    snapshot = lifecycle_state[0]
    sessions = PublicationSessions()

    async def execute(statement):
        nonlocal snapshot
        result = Mock(rowcount=1)
        params = statement.compile().params
        if getattr(statement, "is_select", False):
            table = next(iter(statement.selected_columns)).table.name
            result.scalar_one_or_none.return_value = (
                SimpleNamespace(role="retained", created_at=now, retained_until=None)
                if table == "ml_artifacts" and paths[-1].stem not in params.values()
                else None
            )
        if "checkpoints" in params:
            snapshot = replace(snapshot, checkpoints=params["checkpoints"])
        return result

    sessions.session.execute.side_effect = execute
    lifecycle = module.Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(side_effect=lambda session: snapshot)
    monkeypatch.setattr(module, "RETENTION_BATCH_SIZE", 2)
    monkeypatch.setattr(safe_persistence, "_fsync_directory", lambda directory: None)
    assert await lifecycle.prune_artifacts(lifecycle_state[1], now=now) == []
    assert snapshot.checkpoints["pruning:artifacts"]["cursor"] == paths[1].name
    assert await lifecycle.prune_artifacts(lifecycle_state[1], now=now) == [paths[-1].name]
    assert all(path.exists() for path in paths[:-1]) and not paths[-1].exists()


async def test_large_artifact_inventory_touches_only_one_bounded_page_per_cycle(
    artifact_directory, lifecycle_state, monkeypatch
):
    from packages.ml import lifecycle as module

    snapshot = lifecycle_state[0]
    sessions = PublicationSessions()
    counters = {"touched": 0, "live": 0, "peak": 0, "opened": 0}
    original_scandir = os.scandir
    original_iterdir = Path.iterdir

    class Entry:
        def __init__(self, index):
            self.name = f"{index:064x}.pkl"
            self.path = str(artifact_directory / self.name)
            counters["touched"] += 1
            counters["live"] += 1
            counters["peak"] = max(counters["peak"], counters["live"])

        def __del__(self):
            counters["live"] -= 1

    class Inventory:
        def __init__(self):
            self.entries = (Entry(index) for index in range(10_000))

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.entries)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.entries.close()

    def scandir(path):
        if Path(path) != artifact_directory:
            return original_scandir(path)
        counters["opened"] += 1
        return Inventory()

    def iterdir(path):
        if path != artifact_directory:
            yield from original_iterdir(path)
            return
        with scandir(path) as entries:
            for entry in entries:
                yield path / entry.name

    async def execute(statement):
        nonlocal snapshot
        params = statement.compile().params
        if "checkpoints" in params:
            snapshot = replace(snapshot, checkpoints=params["checkpoints"])
        return Mock(rowcount=1)

    sessions.session.execute.side_effect = execute
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    for cycle in range(40):
        lifecycle = module.Lifecycle(sessions)
        lifecycle._lock = AsyncMock()
        lifecycle._read = AsyncMock(side_effect=lambda session: snapshot)
        before = counters["touched"]
        assert await lifecycle.prune_artifacts(lifecycle_state[1]) == []
        assert counters["touched"] - before <= module.RETENTION_BATCH_SIZE, (
            "inventory enumerated the whole directory"
        )
        assert (
            snapshot.checkpoints["pruning:artifacts"]["cursor"]
            == f"{(cycle + 1) * 250 - 1:064x}.pkl"
        )
    assert counters["touched"] == 10_000
    assert counters["opened"] == 1, "inventory progress was lost between lifecycle owners"
    assert counters["peak"] <= module.RETENTION_BATCH_SIZE, "inventory retained unbounded entries"
    assert snapshot.checkpoints["pruning:artifacts"]["entries_seen"] == 10_000
    assert await lifecycle.prune_artifacts(lifecycle_state[1]) == []
    assert snapshot.checkpoints["pruning:artifacts"]["complete"] is True


@pytest.mark.parametrize("marker_exists", [False, True])
def test_artifact_inventory_restart_replays_only_bounded_pages(
    artifact_directory, lifecycle_state, monkeypatch, marker_exists
):
    from packages.ml import lifecycle as module

    names = [f"entry-{index}.txt" for index in range(8)]
    touched = 0
    original_scandir = os.scandir

    class Inventory:
        def __init__(self):
            self.entries = iter(names)

        def __next__(self):
            nonlocal touched
            name = next(self.entries)
            touched += 1
            return SimpleNamespace(name=name)

        def close(self):
            pass

    def scandir(directory):
        return Inventory() if Path(directory) == artifact_directory else original_scandir(directory)

    monkeypatch.setattr(module, "RETENTION_BATCH_SIZE", 2)
    monkeypatch.setattr(os, "scandir", scandir)
    try:
        page, progress = module._bounded_artifact_page(
            artifact_directory, lifecycle_state[0].epoch, {}
        )
        assert [path.name for path in page] == names[:2]
        with module._ARTIFACT_INVENTORY_LOCK:
            module._ARTIFACT_INVENTORY.entries.close()
            module._ARTIFACT_INVENTORY = None
        if not marker_exists:
            names.remove(progress["cursor"])
        recovered = []
        for _cycle in range(6):
            before = touched
            page, progress = module._bounded_artifact_page(
                artifact_directory, lifecycle_state[0].epoch, progress
            )
            assert touched - before <= 2
            recovered.extend(path.name for path in page)
            if progress["complete"]:
                break
        assert progress["complete"] is True
        if marker_exists:
            assert recovered == names[2:]
        else:
            assert recovered == []
            before = touched
            page, progress = module._bounded_artifact_page(
                artifact_directory, lifecycle_state[0].epoch, progress
            )
            assert touched - before == 2
            assert [path.name for path in page] == names[:2]
    finally:
        with module._ARTIFACT_INVENTORY_LOCK:
            if module._ARTIFACT_INVENTORY is not None:
                module._ARTIFACT_INVENTORY.entries.close()
                module._ARTIFACT_INVENTORY = None


@pytest.mark.parametrize(
    "mismatch",
    ["epoch", "revision", "fence", "owner", "expired", "maintenance", "incumbent", "candidate"],
)
async def test_publication_conflict_never_commits_or_installs(
    artifact_directory, artifact_manifest, lifecycle_state, mismatch
):
    from packages.ml.lifecycle import ArtifactReference, Lifecycle

    state, lease = lifecycle_state
    changes = {
        "epoch": {"epoch": "other-epoch"},
        "revision": {"revision": 8},
        "fence": {"fence": 6},
        "owner": {"lease_owner": "other-owner"},
        "expired": {"lease_expires_at": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)},
        "maintenance": {"maintenance_enabled": False},
        "incumbent": {},
        "candidate": {
            "references": (
                ArtifactReference(
                    "f" * 64,
                    "epoch-1",
                    "demand",
                    "demand_lifecycle_v1",
                    {},
                    "candidate",
                    6,
                ),
            )
        },
    }[mismatch]
    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    sessions = PublicationSessions()
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=replace(state, **changes))
    install = Mock()

    result = await lifecycle.publish(
        lease,
        artifact,
        expected_revision=7,
        expected_incumbent="f" * 64 if mismatch == "incumbent" else None,
        on_committed=install,
    )
    assert not result.committed and not sessions.committed
    assert sessions.statements == []
    install.assert_not_called()
    assert artifact.path.exists()


async def test_file_loss_before_cas_never_commits_or_installs(
    artifact_directory, artifact_manifest, lifecycle_state
):
    from packages.ml.lifecycle import Lifecycle

    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    artifact.path.unlink()
    sessions = PublicationSessions()
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=lifecycle_state[0])
    install = Mock()
    with pytest.raises(FileNotFoundError):
        await lifecycle.publish(
            lifecycle_state[1],
            artifact,
            expected_revision=7,
            expected_incumbent=None,
            on_committed=install,
        )
    assert not sessions.committed and sessions.statements == []
    install.assert_not_called()


async def test_cas_refuses_reference_when_directory_durability_cannot_be_confirmed(
    artifact_directory, artifact_manifest, lifecycle_state, monkeypatch
):
    from packages.ml.lifecycle import Lifecycle

    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=artifact_manifest,
    )
    sessions = PublicationSessions()
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=lifecycle_state[0])
    install = Mock()

    def failed_sync(directory):
        raise OSError("directory durability unavailable")

    monkeypatch.setattr(safe_persistence, "_fsync_directory", failed_sync)
    with pytest.raises(OSError, match="durability unavailable"):
        await lifecycle.publish(
            lifecycle_state[1],
            artifact,
            expected_revision=7,
            expected_incumbent=None,
            on_committed=install,
        )
    assert not sessions.committed and sessions.statements == []
    assert artifact.path.exists()
    install.assert_not_called()


async def test_lease_acquisition_uses_new_tokens_and_cannot_take_a_live_lease(lifecycle_state):
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import Lifecycle

    sessions = PublicationSessions()
    sessions.session.execute.side_effect = None
    result = Mock()
    result.mappings.return_value.one_or_none.return_value = {
        "epoch": "epoch-1",
        "revision": 9,
        "fence": 10,
        "lease_expires_at": lifecycle_state[1].expires_at,
    }
    sessions.session.execute.return_value = result
    lifecycle = Lifecycle(sessions)
    acquired = await lifecycle.acquire("new-owner")
    assert acquired.owner == "new-owner" and acquired.revision == 9 and acquired.fence == 10
    statement = sessions.session.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert compiled.count("nextval('ml_lifecycle_token_seq')") == 2
    assert "maintenance_enabled IS true" in compiled
    assert (
        "lease_owner IS NULL OR ml_lifecycle_state.lease_expires_at <= clock_timestamp()"
        in compiled
    )
    result.mappings.return_value.one_or_none.return_value = None
    assert await lifecycle.acquire("another-owner") is None
    with pytest.raises(ValueError):
        await lifecycle.acquire("bad-duration", dt.timedelta(minutes=6))


async def test_evidence_retention_is_90_days_and_protects_frozen_references(lifecycle_state):
    from sqlalchemy.dialects import postgresql
    from packages.ml.lifecycle import Lifecycle

    sessions = PublicationSessions()
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    now = dt.datetime.now(dt.timezone.utc)

    result = await lifecycle.prune_evidence(lifecycle_state[1], now=now)

    assert result == {"outcomes": 1, "issues": 1, "requests": 1}
    assert len(sessions.statements) == 3
    for statement in sessions.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert now - dt.timedelta(days=90) in compiled.params.values()
    issue_sql = str(sessions.statements[1].compile(dialect=postgresql.dialect()))
    assert "NOT IN (SELECT ml_artifacts.artifact_hash" in issue_sql


@pytest.fixture
def committed_state(artifact_directory, artifact_manifest, lifecycle_state):
    from packages.ml.lifecycle import ArtifactReference

    manifest = {**artifact_manifest, "metrics": {"samples": 23, "mae": 0.3}}
    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=manifest,
    )
    reference = ArtifactReference(
        artifact.artifact_hash,
        lifecycle_state[0].epoch,
        "demand",
        "demand_lifecycle_v1",
        manifest,
        "incumbent",
        7,
    )
    return replace(lifecycle_state[0], references=(reference,)), artifact


def test_cache_expiry_refuses_stale_state(committed_state, model_dir, monkeypatch):
    from packages.ml import model_status

    state, artifact = committed_state
    now = dt.datetime.now(dt.timezone.utc)
    expiry = now + dt.timedelta(seconds=30)
    reference = replace(
        state.references[0],
        qualified_at=now - dt.timedelta(hours=1),
        last_valid_outcome_at=expiry - dt.timedelta(hours=72),
        qualification_expires_at=expiry,
        qualification_scope={"policy": "native_unchanged_v1"},
    )
    state = replace(state, references=(reference,))
    model_status.clear_artifact_status_cache()
    monkeypatch.setattr(model_status, "_now", lambda: now)
    first = model_status._inspect_model_artifact("demand", model_dir, state)
    assert first["status"] == "qualified-for-reporting"
    assert first["committed_hash"] == artifact.artifact_hash
    assert first["control_ready"] is False

    monkeypatch.setattr(model_status, "_now", lambda: now + dt.timedelta(seconds=30))
    second = model_status._inspect_model_artifact("demand", model_dir, state)
    assert second["status"] == "expired"
    assert second["control_ready"] is False
    assert second["ordinary_writes_allowed"] is False


async def test_db_failure_refuses_cached_status(committed_state, model_dir):
    from packages.ml.model_status import clear_artifact_status_cache, get_model_status
    from packages.ml.lifecycle import Lifecycle

    lifecycle = Lifecycle(PublicationSessions())
    lifecycle.read = AsyncMock(return_value=committed_state[0])
    clear_artifact_status_cache()
    first = await get_model_status("demand", model_dir, lifecycle=lifecycle)
    assert first["trained"] is True
    lifecycle.read.side_effect = OSError("private database failure")
    failed = await get_model_status("demand", model_dir, lifecycle=lifecycle)
    assert failed["status"] == "unavailable"
    assert failed["trained"] is False
    assert failed["control_ready"] is False
    assert "private" not in str(failed)
    assert lifecycle.read.await_count == 2


@pytest.mark.parametrize("failure", ["hash", "hmac", "schema", "epoch", "manifest"])
def test_cached_status_detects_integrity_and_schema_failure_on_every_read(
    committed_state, model_dir, monkeypatch, failure
):
    from packages.ml import model_status

    state, artifact = committed_state
    model_status.clear_artifact_status_cache()
    first = model_status._inspect_model_artifact("demand", model_dir, state)
    assert first["trained"] is True
    if failure == "hash":
        artifact.path.write_bytes(b"corrupt")
    elif failure == "hmac":
        monkeypatch.setattr(settings, "secret_key", "new-key-invalidates-old-signatures")
    else:
        reference = replace(
            state.references[0],
            **{
                "schema": {"schema_version": "legacy"},
                "epoch": {"epoch": "other-epoch"},
                "manifest": {"manifest": {**artifact.manifest, "fit_id": "other-fit"}},
            }[failure],
        )
        state = replace(state, references=(reference,))
    failed = model_status._inspect_model_artifact("demand", model_dir, state)
    assert failed["status"] == "unavailable"
    assert failed["trained"] is False
    assert failed["control_ready"] is False
    assert "demand" not in model_status._artifact_status_cache


def test_parsed_status_cache_is_bounded_and_keyed_by_committed_revision(
    committed_state, model_dir, monkeypatch
):
    from packages.ml import model_status

    state, artifact = committed_state
    now = dt.datetime.now(dt.timezone.utc)
    parse = Mock(wraps=model_status._base_status)
    monkeypatch.setattr(model_status, "_base_status", parse)
    monkeypatch.setattr(model_status, "_now", lambda: now)
    monkeypatch.setattr(model_status, "_cache_time", lambda: 100.0)
    model_status.clear_artifact_status_cache()
    first = model_status._inspect_model_artifact("demand", model_dir, state)
    artifact_path("f" * 64).write_bytes(b"uncommitted newer file")
    second = model_status._inspect_model_artifact("demand", model_dir, state)
    assert first == second
    assert parse.call_count == 1
    monkeypatch.setattr(model_status, "_now", lambda: now + dt.timedelta(seconds=60))
    monkeypatch.setattr(model_status, "_cache_time", lambda: 160.0)
    assert model_status._inspect_model_artifact("demand", model_dir, state) == first
    assert parse.call_count == 2
    model_status._inspect_model_artifact("demand", model_dir, replace(state, revision=8))
    assert parse.call_count == 3
    assert first["committed_hash"] == artifact.artifact_hash

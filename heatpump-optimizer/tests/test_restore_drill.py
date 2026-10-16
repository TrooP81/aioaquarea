from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import datetime as dt
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_drill.py"
spec = importlib.util.spec_from_file_location("hpo_restore_drill", SCRIPT)
drill = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = drill
spec.loader.exec_module(drill)


def manifest(archive):
    return {
        "schema_version": 1,
        "eligible": True,
        "archive_id": "heatpump-" + "a" * 32,
        "sha256": drill.digest(archive),
        "source_id": "source",
        "schema_revision": "030",
        "postgres_major": 16,
        "timescale_version": "2.26.4",
        "database_size_bytes": 100,
    }


@pytest.fixture
def coordinator(tmp_path):
    # Windows chmod does not model POSIX ACLs. Fakes bypass only root permission
    # inspection, not Docker identity/ownership checks.
    root = tmp_path / "leases"
    root.mkdir(mode=0o700)
    original = Path.stat
    from unittest.mock import patch

    def private_stat(path, **kwargs):
        result = original(path, **kwargs)
        if path == root:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=0o40700)
        return result

    with patch.object(Path, "stat", private_stat):
        yield drill.Coordinator(root, {}, runner=Mock())


def test_deadline_reserves_cleanup():
    clock = Mock(return_value=100.0)
    budget = drill.Budget(seconds=600, reserve=60, clock=clock)
    assert budget.remaining(3600) == 540
    clock.return_value = 641
    with pytest.raises(drill.DrillError, match="lifecycle_timeout"):
        budget.remaining(1)
    assert budget.remaining(60, cleanup=True) == 59


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "bad"),
        ("eligible", False),
        ("postgres_major", 15),
        ("timescale_version", "2.0"),
        ("database_size_bytes", 0),
        ("archive_id", "../escape"),
        ("schema_revision", "bad"),
    ],
)
def test_manifest_refuses_corruption_and_version(tmp_path, field, value):
    archive = tmp_path / "archive.dump"
    archive.write_bytes(b"synthetic")
    metadata = manifest(archive)
    metadata[field] = value
    with pytest.raises(drill.DrillError, match="archive_binding_invalid"):
        drill.validate_manifest(metadata, archive)
    assert archive.read_bytes() == b"synthetic"


def test_unowned_source_refused_before_commands(coordinator):
    with pytest.raises(drill.DrillError, match="source_receipt_invalid"):
        coordinator.source_guard()
    coordinator.runner.assert_not_called()


def test_insufficient_capacity_refuses_startup(coordinator):
    coordinator.runner.return_value = json.dumps({"MemTotal": 1024})
    with pytest.raises(drill.DrillError, match="memory_capacity_refused"):
        coordinator.capacity(100, 100)
    assert coordinator.resources == []


@pytest.mark.parametrize("failure", ["startup", "restore", "cancel"])
def test_execution_failure_always_runs_cleanup_and_failed_report(coordinator, failure):
    coordinator.reporting_started = True
    coordinator.execute = Mock(side_effect=drill.DrillError(failure))
    coordinator.cleanup = Mock(return_value=True)
    coordinator.report = Mock()
    assert coordinator.run() is False
    coordinator.cleanup.assert_called_once()
    coordinator.report.assert_called_once_with(
        succeeded=False, reason="verification_resource_failed"
    )


def test_cleanup_failure_never_reports_verified(coordinator):
    coordinator.reporting_started = True
    coordinator.execute = Mock()
    coordinator.cleanup = Mock(return_value=False)
    coordinator.report = Mock()
    assert coordinator.run() is False
    coordinator.report.assert_called_once_with(succeeded=False, reason="cleanup_failed")


def test_reporting_failure_retains_incomplete_receipt(coordinator):
    coordinator.reporting_started = True
    coordinator.execute = Mock()
    coordinator.cleanup = Mock(return_value=True)
    coordinator.report = Mock(side_effect=drill.DrillError("timeout"))
    assert coordinator.run() is False
    assert (
        json.loads((coordinator.lease / "receipt.json").read_text())["status"]
        == "reporting_incomplete"
    )


def test_cancellation_stops_actual_work_before_removal(coordinator):
    identity = "a" * 64
    coordinator.resources = [("container", identity)]
    coordinator.inspect = Mock(
        side_effect=[
            {"Config": {"Labels": {drill.LABEL: coordinator.token}}, "State": {"Running": True}},
            {"State": {"Running": False}},
        ]
    )
    coordinator.docker = Mock(return_value="")
    assert coordinator.cleanup() is True
    commands = [call.args for call in coordinator.docker.call_args_list]
    assert commands[:3] == [("kill", identity), ("wait", identity), ("container", "rm", identity)]


def test_unknown_membership_is_never_broadly_cleaned(coordinator):
    coordinator.resources = [("network", "b" * 64)]
    coordinator.inspect = Mock(
        return_value={"Labels": {drill.LABEL: coordinator.token}, "Containers": {"unknown": {}}}
    )
    coordinator.docker = Mock()
    assert coordinator.cleanup() is False
    coordinator.docker.assert_not_called()
    assert coordinator.residual == coordinator.resources


def test_daemon_loss_is_unverified_cleanup(coordinator):
    coordinator.resources = [("container", "c" * 64)]
    coordinator.inspect = Mock(side_effect=drill.DrillError("daemon_lost"))
    assert coordinator.cleanup() is False
    assert coordinator.receipt["cleanup_verified"] is False


@pytest.fixture
def pipeline(coordinator):
    """Docker inspect semantics: created endpoints have NO runtime NetworkID."""
    c = coordinator
    c.created_after -= 10
    c.source = {"source_id": "source"}
    source = "f" * 64
    c.source_guard = Mock(return_value=source)
    archive = c.lease / "seed"
    archive.write_bytes(b"synthetic")
    metadata = manifest(archive)
    archive.unlink()
    calls, items = [], {}
    state = {
        "stage": False,
        "stage_failure": None,
        "cleanup_failure": False,
        "bad_runtime": False,
        "copy_failure": False,
        "restore_failure": False,
    }

    def inspect(kind, identity, **_):
        if kind == "image":
            return {"Id": "sha256:pinned"}
        item = items[(kind, identity)]
        if kind == "container":
            calls.append(
                (
                    "inspect",
                    identity,
                    item["State"]["Running"],
                    tuple(n["NetworkID"] for n in item["NetworkSettings"]["Networks"].values()),
                )
            )
        return item

    def create(kind, *args):
        identity = str(len(items) + 1).zfill(64)
        if kind == "network":
            item = {"Internal": True, "Labels": {drill.LABEL: c.token}, "Containers": {}}
        else:
            network = args[args.index("--network") + 1]
            mounts = [{"Type": "tmpfs", "Destination": "/var/lib/postgresql/data"}]
            if "--mount" in args:
                mounts.append(
                    {
                        "Type": "volume",
                        "Name": c.volume,
                        "Destination": "/backups",
                        "RW": "readonly" not in args[args.index("--mount") + 1],
                    }
                )
            # Both DB and restore containers must be idle at start.
            assert args[-2:] == ("-c", "while :; do sleep 60; done") or network == "none"
            item = {
                "Config": {"Image": drill.IMAGE, "Labels": {drill.LABEL: c.token}},
                "Image": "sha256:pinned",
                "HostConfig": {"NetworkMode": network},
                "NetworkSettings": {
                    "Networks": {} if network == "none" else {"endpoint": {"NetworkID": ""}}
                },
                "State": {"Running": False},
                "Mounts": mounts,
            }
        items[(kind, identity)] = item
        c.resources.append((kind, identity))
        calls.append(("create", kind, identity, args))
        return identity

    def docker(*args, **_):
        calls.append(args)
        if args[:2] == ("volume", "create"):
            name = args[-1]
            labels = dict(value.split("=", 1) for value in args if value.startswith(drill.LABEL))
            items[("volume", name)] = {
                "Name": name,
                "Labels": labels,
                "Driver": "local",
                "Options": None,
                # Docker metadata is an immutable whole-second timestamp in
                # this fake's known valid reservation window, not a second live
                # wall-clock sample racing the guard's clock.
                "CreatedAt": dt.datetime.fromtimestamp(
                    int(c.created_after) + 1, dt.timezone.utc
                ).isoformat(),
            }
            return name
        if args[0] == "start":
            item = items[("container", args[1])]
            item["State"]["Running"] = True
            network = item["HostConfig"]["NetworkMode"]
            item["NetworkSettings"]["Networks"]["endpoint"]["NetworkID"] = (
                "foreign" if state["bad_runtime"] else network
            )
        elif args[0] == "kill":
            assert args[1] != source, "must never stop source worker"
            items[("container", args[1])]["State"]["Running"] = False
        elif args[0] == "wait":
            return "0"
        elif len(args) > 1 and args[1] == "rm":
            del items[(args[0], args[2])]
        elif len(args) > 1 and args[1] == "ls":
            return "\n".join(identity for kind, identity in items if kind == args[0])
        elif args[0] == "cp" and args[1].startswith(source + ":"):
            path = Path(args[2])
            path.write_bytes(
                b"synthetic" if path.suffix == ".dump" else json.dumps(metadata).encode()
            )
            if state["copy_failure"]:
                raise drill.DrillError("copy_failed")
        elif args[0] == "exec" and "/scripts/stage_backup.sh" in args:
            mode = args[-1]
            if mode == "reserve":
                receipt = json.loads((c.lease / "receipt.json").read_text())
                assert receipt["source_stage"]["path"] == c.stage_path
                state["stage"] = True
            elif mode == "cleanup":
                if state["cleanup_failure"]:
                    raise drill.DrillError("unsettled")
                state["stage"] = False
                return "settled"
            else:
                assert mode == "run", "coordinator must not auto-reserve a cleaned stage"
                if state["stage_failure"]:
                    raise state["stage_failure"]
                return c.stage_path
        elif args[0] == "exec" and "psql" in args:
            if "-c" in args:
                return json.dumps(metadata)
            if "-Atc" in args:
                return args[args.index("-d") + 1] + "|restore_test"
        elif args[0] == "exec" and "/backups/restore_archive.sh" in args:
            if state["restore_failure"]:
                raise drill.DrillError("restore_failed")
        return ""

    def report(**kwargs):
        if kwargs.get("start"):
            c.reporting_started = True

    c.inspect = Mock(side_effect=inspect)
    c.create = Mock(side_effect=create)
    c.docker = Mock(side_effect=docker)
    c.capacity = Mock(return_value=2 * 1024**3)
    c.report = Mock(side_effect=report)
    return c, calls, state


def test_idle_start_then_runtime_inspection_precedes_restore_and_cleans_copies(pipeline):
    c, calls, state = pipeline
    assert c.run() is True
    assert not state["stage"]
    assert c.receipt["cleanup_verified"] is True, c.receipt
    assert {p.name for p in c.lease.iterdir()} == {"receipt.json"}
    assert any(call[0] == "exec" and "/backups/restore_archive.sh" in call for call in calls)
    for index, call in enumerate(calls):
        if call[0] == "start":
            identity = call[1]
            assert ("inspect", identity, False, ("",)) in calls[:index]
            after = calls[index + 1 :]
            assert after[0][:3] == ("inspect", identity, True)
            assert after[0][3] != ("",)
            assert after[1][0] == "exec"  # work cannot launch before runtime check


def test_runtime_network_mismatch_refuses_database_launch_and_restore(pipeline):
    c, calls, state = pipeline
    state["bad_runtime"] = True
    assert c.run() is False
    assert not any(
        "docker-entrypoint.sh" in call or "/backups/restore_archive.sh" in call for call in calls
    )


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("docker exec", 60), KeyboardInterrupt()]
)
def test_stage_timeout_or_cancellation_has_pre_recorded_identity_and_verified_cleanup(
    pipeline, failure
):
    c, calls, state = pipeline
    state["stage_failure"] = failure
    assert c.run() is False
    assert c.receipt["cleanup_verified"] is True
    assert not state["stage"]
    assert c.stage_path is None
    assert {p.name for p in c.lease.iterdir()} == {"receipt.json"}
    assert not any(call[0] == "kill" for call in calls)  # no target or source started


def test_unresolved_source_staging_forces_failed_cleanup_receipt(pipeline):
    c, _, state = pipeline
    state.update(stage_failure=KeyboardInterrupt(), cleanup_failure=True)
    assert c.run() is False
    assert c.receipt["cleanup_verified"] is False
    assert ("source_stage", c.stage_path) in c.receipt["residual_resources"]
    assert c.receipt["status"] == "cleanup_failed"


@pytest.mark.parametrize("phase", ["copy_failure", "restore_failure"])
def test_failed_drill_removes_local_archive_and_manifest_as_well_as_source_stage(pipeline, phase):
    c, _, state = pipeline
    state[phase] = True
    assert c.run() is False
    assert c.receipt["cleanup_verified"] is True, c.receipt
    assert not state["stage"]
    assert {p.name for p in c.lease.iterdir()} == {"receipt.json"}


def test_local_copy_removal_failure_is_an_explicit_cleanup_residual(coordinator, monkeypatch):
    path = coordinator.lease / "archive.dump"
    path.write_bytes(b"sensitive disposable bytes")
    original = Path.unlink

    def unlink(item, *args, **kwargs):
        if item == path:
            raise PermissionError("disposable failure")
        return original(item, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert coordinator.cleanup() is False
    assert ("local_archive", str(path)) in coordinator.receipt["residual_resources"]
    assert coordinator.receipt["cleanup_verified"] is False


@pytest.mark.parametrize(
    "damage", ["foreign_nonce", "driver", "options", "created", "future_created"]
)
def test_volume_race_never_mounts_or_deletes_unproven_volume(coordinator, damage):
    c = coordinator
    c.created_after -= 10
    foreign = {}

    def docker(*args, **_):
        assert args[:2] == ("volume", "create")
        name = args[-1]
        # Simulate a foreign creator winning immediately before Docker's create.
        foreign.update(
            Name=name,
            Driver="local",
            Options=None,
            CreatedAt=dt.datetime.now(dt.timezone.utc).isoformat(),
            Labels=dict(value.split("=", 1) for value in args if value.startswith(drill.LABEL)),
        )
        if damage == "foreign_nonce":
            foreign["Labels"][f"{drill.LABEL}.nonce"] = "foreign"
        elif damage == "driver":
            foreign["Driver"] = "foreign"
        elif damage == "options":
            foreign["Options"] = {"device": "/foreign"}
        elif damage == "created":
            foreign["CreatedAt"] = "2000-01-01T00:00:00Z"
        else:
            foreign["CreatedAt"] = "2099-01-01T00:00:00Z"
        return name

    c.docker = Mock(side_effect=docker)
    c.inspect = Mock(side_effect=lambda *_args, **_kwargs: foreign)
    with pytest.raises(drill.DrillError, match="volume_creation_unproven"):
        c.create_archive_volume()
    assert c.resources == []
    assert c.cleanup() is False
    assert foreign  # no remove, mount or write was attempted
    assert c.docker.call_count == 1
    expected = {
        "foreign_nonce": "nonce_label",
        "driver": "local_driver",
        "options": "no_options",
        "created": "creation_time",
        "future_created": "creation_time",
    }
    assert expected[damage] in c.receipt["volume_creation_rejected"]


def test_monthly_configuration_docs_have_no_obsolete_freshness_or_future_work():
    text = (SCRIPT.parents[1] / "docs/configuration-reference.md").read_text(encoding="utf-8")
    assert text.count("`BACKUP_MAX_AGE_SECONDS`") == 1
    assert "2764800" in text
    assert "26 h" not in text
    assert "remain Phase 2 work" not in text
    assert "verification advances only after verified cleanup" in text
    assert "require the Phase 2 launcher" not in text
    assert "Legacy verifier errors distinguish" in text
    assert "coordinator already performs metadata-based" in text


def test_capacity_uses_source_size_and_host_headroom(coordinator, monkeypatch):
    from types import SimpleNamespace

    coordinator.runner.return_value = json.dumps({"MemTotal": 32 * 1024**3})
    original = Path.read_text

    def memory(path, *args, **kwargs):
        if str(path).replace("\\", "/") == "/proc/meminfo":
            return f"MemAvailable: {24 * 1024**2} kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", memory)
    monkeypatch.setattr(drill.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert coordinator.capacity(1024**3, 100) == 5 * 1024**3 // 2
    assert coordinator.capacity(100, 100) == 2 * 1024**3


def test_staging_blocks_retention_until_validated_copy_survives_original_deletion(
    tmp_path, monkeypatch
):
    """Cross-platform fake flock checks ordering; CI must exercise the real flock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    mutex = threading.Lock()

    def flock(_fd, operation):
        if operation == 1:
            mutex.acquire()

    # stage_local closes the fd to release flock. Model that lifetime explicitly
    # via a file wrapper rather than silently releasing before copy/validation.
    original_open = Path.open

    class LockedFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self.file

        def __exit__(self, *_):
            self.file.close()
            mutex.release()

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return LockedFile(stream) if path.name == ".backup.lock" else stream

    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=1, flock=flock))
    monkeypatch.setattr(Path, "open", opened)
    root, lease = tmp_path / "backups", tmp_path / "private"
    root.mkdir()
    lease.mkdir()
    archive = root / ("heatpump-" + "a" * 32 + ".dump")
    archive.write_bytes(b"synthetic archive retained during staging")
    metadata = manifest(archive)
    entered, release, retention_waiting, deleted = [threading.Event() for _ in range(4)]

    def select():
        entered.set()
        assert release.wait(5)
        return archive, metadata

    def retention():
        retention_waiting.set()
        with mutex:
            archive.unlink()
            deleted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        staging = pool.submit(drill.stage_local, root, lease, select)
        assert entered.wait(5)
        deleting = pool.submit(retention)
        assert retention_waiting.wait(5)
        assert not deleted.is_set()
        release.set()
        staged, staged_metadata = staging.result(timeout=5)
        deleting.result(timeout=5)
    assert deleted.is_set()
    drill.validate_manifest(staged_metadata, staged)
    assert staged.read_bytes() == b"synthetic archive retained during staging"

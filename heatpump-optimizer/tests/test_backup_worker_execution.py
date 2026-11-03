"""Run the actual worker/stager with disposable command doubles, never live services."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import signal

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"
SHIM = Path(__file__).with_name("helpers") / "backup_command_shim.py"
NOW = 1800000000
INTERVAL = 2592000
# Replaces the product's fixed 2 s TERM->KILL grace inside real timers only.
TEST_GRACE = 0.5
DATE_SHIM = """#!/bin/sh
IFS= read -r now < "$HPO_BACKUP_FAKE_ROOT/clock.json" || [ -n "$now" ] || exit 1
for format; do :; done
[ "$format" != +%s ] || { printf '%s\\n' "$now"; exit 0; }
exec "$HPO_BACKUP_REAL_DATE" -u -d "@$now" "$format"
"""
# Execute the real runtime timer in place, preserving PID/process group and
# inherited lock descriptors. Only durations are shortened.
TIMEOUT_SHIM = """#!/bin/sh
[ "$1" = -k ] && [ "$2" = 2 ] || { echo "hard kill-after is mandatory" >&2; exit 125; }
limit=$3
shift 3
. "$HPO_BACKUP_FAKE_ROOT/timeout.env"
[ -z "$cap" ] || [ "$limit" -le "$cap" ] || limit=$cap
HPO_BACKUP_ACTIVE_TIMEOUT=$limit
export HPO_BACKUP_ACTIVE_TIMEOUT
if [ -n "$pid_only" ]; then
    # GNU foreground mode, like BusyBox, signals only the monitored PID.
    # This forces the stage's independent group watchdog to do real work.
    exec "$HPO_BACKUP_REAL_TIMEOUT" --foreground -k "$HPO_BACKUP_TEST_GRACE" "$limit" "$@"
fi
exec "$HPO_BACKUP_REAL_TIMEOUT" -k "$HPO_BACKUP_TEST_GRACE" "$limit" "$@"
"""


def working_bash() -> str | None:
    executable = shutil.which("bash")
    if executable:
        try:
            result = subprocess.run(
                [executable, "-c", "printf backup-shell-probe"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and result.stdout == b"backup-shell-probe":
                return executable
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def require_bash(executable):
    if executable:
        return executable
    if sys.platform.startswith("linux") and os.environ.get("CI", "").lower() == "true":
        pytest.fail("Linux CI must execute the backup shell matrix: working bash is required")
    pytest.skip("working bash is unavailable; executable shell matrix NOT_RUN")


def test_linux_ci_cannot_silently_skip_the_shell_matrix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="must execute"):
        require_bash(None)


def test_bash_is_available_in_linux_ci():
    if sys.platform.startswith("linux") and os.environ.get("CI", "").lower() == "true":
        assert working_bash(), "backend-unit cannot skip the executable backup matrix"


class Harness:
    def __init__(self, root, bash):
        self.root, self.bash = root, bash
        self.backups = root / "backups"
        self.backups.mkdir()
        (root / "replica").mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for command in (
            "psql",
            "pg_dump",
            "pg_restore",
            "sleep",
            "sha256sum",
            "mv",
            "openssl",
            "cp",
            "flock",
        ):
            # Same Python as pytest, with no app imports or dependency on python3 on PATH.
            # Isolated, site-free startup: the worker spawns these per command.
            self.install(
                bin_dir / command,
                f"#!{sys.executable} -IS\n" + SHIM.read_text(encoding="utf-8").split("\n", 1)[1],
            )
        # The clock and timer doubles wrap every bounded command, so they are
        # POSIX sh rather than an interpreter start per call.
        self.install(bin_dir / "date", DATE_SHIM)
        self.install(bin_dir / "timeout", TIMEOUT_SHIM)
        real_flock = shutil.which("flock")
        assert real_flock, "bash environment must provide real flock, not a simulated lock"
        self.env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "HPO_BACKUP_FAKE_ROOT": str(root),
            "HPO_BACKUP_REAL_FLOCK": real_flock,
            "HPO_BACKUP_REAL_TIMEOUT": shutil.which("timeout") or "",
            "HPO_BACKUP_REAL_SLEEP": shutil.which("sleep") or "",
            "HPO_BACKUP_REAL_DATE": shutil.which("date") or "",
            "HPO_BACKUP_TEST_GRACE": str(TEST_GRACE),
            "BACKUP_DIR": str(self.backups),
            "BACKUP_REPLICA_DIR": str(root / "replica"),
            "BACKUP_REPLICA_ENABLED": "false",
            "BACKUP_REPLICA_REQUIRED": "false",
            "BACKUP_REPLICA_ENCRYPTION_KEY": "disposable-test-key",
            "BACKUP_INTERVAL_SECONDS": str(INTERVAL),
            "BACKUP_RETENTION_DAYS": "100",
            "BACKUP_MAX_AGE_SECONDS": "2764800",
            "BACKUP_TIMEOUT_SECONDS": "3600",
            "BACKUP_EXPECTED_SOURCE_ID": "source",
            "PGHOST": "never-connected",
            "PGUSER": "fake",
            "PGDATABASE": "fake",
            "PGPASSWORD": "disposable",
        }
        self.write("clock.json", NOW)
        self.write("config.json", {})
        assert shutil.which("setsid"), "worker runtime must support isolated process groups"
        assert self.env["HPO_BACKUP_REAL_TIMEOUT"] and self.env["HPO_BACKUP_REAL_SLEEP"]
        assert self.env["HPO_BACKUP_REAL_DATE"]

    @staticmethod
    def install(destination, text):
        destination.write_text(text, encoding="utf-8", newline="\n")
        destination.chmod(0o700)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        if name == "config.json":
            # Sourced by the sh timeout double; sleep-time policy edits never touch these.
            cap = value.get("timeout_cap")
            (self.root / "timeout.env").write_text(
                f"cap={int(cap) if cap else ''}\n"
                f"pid_only={'1' if value.get('pid_only_timeout') else ''}\n",
                encoding="utf-8",
                newline="\n",
            )

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def config(self, **values):
        self.write("config.json", {**self.read("config.json"), **values})

    def events(self, command=None):
        path = self.root / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if command is None or row["command"] == command]

    def run(self, script="backup_db.sh", args=("--once",), *, check=True):
        self.write("identities.json", 0)
        result = subprocess.run(
            [self.bash, str(SCRIPTS / script), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def archive(self, number, snapshot):
        archive_id = f"heatpump-{number:032x}"
        archive = self.backups / (archive_id + ".dump")
        archive.write_bytes(f"synthetic archive {number}".encode())
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        when = dt.datetime.fromtimestamp(snapshot, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "schema_version": 1,
            "eligible": True,
            "archive_id": archive_id,
            "sha256": checksum,
            "source_id": "source",
            "schema_revision": "036",
            "snapshot_at": when,
            "completed_at": when,
            "postgres_major": 16,
            "timescale_version": "2.26.4",
            "database_size_bytes": 100,
        }
        Path(str(archive) + ".json").write_text(json.dumps(manifest), encoding="utf-8")
        Path(str(archive) + ".sha256").write_text(
            f"{checksum}  {archive_id}.dump\n",
            encoding="utf-8",
        )
        return manifest

    def seed(self, count=1, *, age=INTERVAL + 1):
        manifests = [self.archive(i + 1, NOW - age - (count - i) * 1000) for i in range(count)]
        success, verified = manifests[-1], manifests[0]
        state = {
            "schema_version": 1,
            "last_attempt": {
                "id": "f" * 32,
                "archive_id": success["archive_id"],
                "status": "succeeded",
                "started_at": success["snapshot_at"],
                "finished_at": success["completed_at"],
            },
            "last_success": {
                key: success[key]
                for key in (
                    "archive_id",
                    "sha256",
                    "source_id",
                    "schema_revision",
                    "snapshot_at",
                    "completed_at",
                )
            },
            "last_verified": {**verified, "verified_at": verified["completed_at"]},
            "last_verification": {
                "id": "e" * 32,
                "archive_id": verified["archive_id"],
                "sha256": verified["sha256"],
                "status": "succeeded",
                "started_at": verified["snapshot_at"],
                "finished_at": verified["completed_at"],
            },
        }
        self.write("state.json", state)
        return state

    def dumps(self):
        return set(self.backups.glob("*.dump"))


@pytest.fixture
def worker(tmp_path):
    return Harness(tmp_path, require_bash(working_bash()))


@pytest.mark.parametrize("shell_route", ["bash", "sh"])
def test_fake_clock_first_activation_and_suppression_after_persisted_success(worker, shell_route):
    if shell_route == "sh":
        worker.bash = shutil.which("sh")
        assert worker.bash, "Linux worker qualification requires /bin/sh"
    worker.run()
    first = worker.read("state.json")
    assert first["last_attempt"]["status"] == "succeeded"
    assert first["last_success"]["snapshot_at"] == "2027-01-15T08:00:00Z"
    assert len(worker.events("pg_dump")) == 1
    assert len(worker.dumps()) == 1
    assert not list(worker.backups.glob(".work-*"))
    worker.run()
    assert len(worker.events("pg_dump")) == 1
    assert worker.read("state.json") == first


@pytest.mark.parametrize(("offset", "attempts"), [(-1, 0), (0, 1), (7 * 86400, 1)])
def test_before_due_due_and_overdue_restarts(worker, offset, attempts):
    old = worker.seed()
    due = int(dt.datetime.fromisoformat(old["last_success"]["snapshot_at"]).timestamp()) + INTERVAL
    worker.write("clock.json", due + offset)
    worker.run()
    assert len(worker.events("pg_dump")) == attempts
    worker.run()
    assert len(worker.events("pg_dump")) == attempts  # no catch-up bursts


def test_failure_retry_spacing_survives_restarts_without_moving_success(worker):
    previous = worker.seed()["last_success"]
    worker.config(fail="dump")
    worker.run()
    failed = worker.read("state.json")
    assert failed["last_attempt"]["status"] == "failed"
    assert failed["last_success"] == previous
    worker.write("clock.json", NOW + 299)
    worker.run()
    assert len(worker.events("pg_dump")) == 1
    worker.write("clock.json", NOW + 300)
    worker.config(fail=None)
    worker.run()
    assert len(worker.events("pg_dump")) == 2
    assert worker.read("state.json")["last_success"] != previous


@pytest.mark.parametrize(
    "phase",
    [
        "identity_changed",
        "dump",
        "toc",
        "checksum",
        "publish_dump",
        "publish_checksum",
        "manifest",
        "publish_manifest",
    ],
)
def test_phase_failure_records_failed_attempt_and_preserves_success(worker, phase):
    previous = worker.seed()["last_success"]
    worker.config(fail=phase)
    worker.run()
    state = worker.read("state.json")
    assert state["last_attempt"]["status"] == "failed"
    assert state["last_attempt"]["reason_code"] == "backup_attempt_failed"
    assert state["last_success"] == previous
    assert not list(worker.backups.glob("*.partial"))


def test_identity_preflight_failure_cannot_create_an_attempt(worker):
    previous = worker.seed()
    worker.config(fail="identity_start")
    worker.run()
    assert worker.read("state.json") == previous
    assert not worker.events("status")
    assert not worker.events("pg_dump")


@pytest.mark.parametrize(
    "args",
    [
        ("--unknown",),
        ("--transition-legacy",),
        ("--once", "--once-preserve-archives"),
        ("--once", "--once"),
        ("--once-preserve-archives",),
    ],
)
def test_invalid_modes_fail_before_tools_or_scheduling(worker, args):
    assert worker.run(args=args, check=False).returncode == 2
    assert not worker.events()


@pytest.mark.parametrize("transition", [False, True])
@pytest.mark.parametrize("damage", [False, True])
def test_preservation_initial_and_retry_never_enter_retention(worker, transition, damage):
    initial = worker.seed(count=5, age=200 * 86400)
    if damage:
        (worker.backups / ("heatpump-" + "d" * 32 + ".dump.json")).write_text("{}")
    originals = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in worker.backups.iterdir()
    }
    worker.env["BACKUP_EXPECTED_SCHEMA_REVISION"] = "039"
    worker.config(revision="039")
    args = ["--once-preserve-archives"]
    if transition:
        raw = ' \n{"status":"ok"}\n'
        worker.config(legacy_raw=raw)
        worker.write("state.json", {"status": "ok"})
        worker.env.update(
            BACKUP_LEGACY_EXPECTED_SHA256=hashlib.sha256(raw.encode()).hexdigest(),
            BACKUP_LEGACY_EXPECTED_UPDATED_AT="2026-01-01T00:00:00Z",
        )
        args.append("--transition-legacy")
    worker.run(args=args)
    state = worker.read("state.json")
    assert state["last_attempt"]["retention_mode"] == "preserve"
    assert state["last_attempt"]["reason_code"] == ""
    assert state["last_success"]["archive_id"] != initial["last_success"]["archive_id"]
    assert not worker.events("retention")
    assert not [event for event in worker.events("status") if event["phase"] == "diagnostic"]
    assert not worker.events("sleep")
    for name, (data, modified) in originals.items():
        path = worker.backups / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (data, modified)


@pytest.mark.parametrize("phase", ["start", "finish", "diagnostic"])
def test_lost_status_ack_never_relabels_committed_success(worker, phase):
    worker.seed()
    if phase == "diagnostic":
        (worker.backups / ("heatpump-" + "d" * 32 + ".dump.json")).write_text("{}")
    worker.config(lost_ack=phase)
    worker.run()
    state = worker.read("state.json")
    assert state["last_attempt"]["status"] == "succeeded"
    assert not [
        event
        for event in worker.events("status")
        if event["values"]["status"] in {"failed", "interrupted"}
    ]
    if phase == "diagnostic":
        assert state["last_attempt"]["reason_code"] == "backup_retention_blocked"


@pytest.mark.parametrize(
    ("required", "status", "replica"),
    [
        ("true", "failed", "required_failed"),
        ("false", "succeeded", "optional_failed"),
    ],
)
def test_replica_failure_remains_required_failure_or_optional_warning(
    worker, required, status, replica
):
    previous = worker.seed()["last_success"]
    worker.env.update(BACKUP_REPLICA_ENABLED="true", BACKUP_REPLICA_REQUIRED=required)
    worker.config(fail="replica")
    worker.run()
    state = worker.read("state.json")
    assert state["last_attempt"]["status"] == status
    assert state["last_attempt"]["replica_status"] == replica
    assert (state["last_success"] == previous) is (required == "true")
    if required == "true":
        assert state["last_attempt"]["reason_code"] == "backup_replica_required_failed"


@pytest.mark.parametrize("phase", ["status_start", "status_finish"])
def test_status_write_failure_cannot_manufacture_success(worker, phase):
    previous = worker.seed()["last_success"]
    worker.config(fail=phase)
    worker.run()
    state = worker.read("state.json")
    assert state["last_success"] == previous
    if phase == "status_start":
        assert not worker.events("pg_dump")
    else:
        assert state["last_attempt"]["status"] == "running"  # final reporting unavailable


@pytest.mark.parametrize("count", [1, 2, 5])
def test_retention_keeps_two_and_protects_verified_and_latest_success(worker, count):
    initial = worker.seed(count=count, age=200 * 86400)
    worker.run()
    state = worker.read("state.json")
    protected = {
        worker.backups / (initial["last_verified"]["archive_id"] + ".dump"),
        worker.backups / (state["last_success"]["archive_id"] + ".dump"),
    }
    assert worker.dumps() == protected
    assert len(worker.dumps()) == 2
    for archive in worker.dumps():
        assert Path(str(archive) + ".json").is_file()
        assert Path(str(archive) + ".sha256").is_file()


@pytest.mark.parametrize("mode", ["normal", "preserve", "transition"])
@pytest.mark.parametrize("shell_route", ["bash", "sh"])
def test_aggregate_retention_budget_survives_terminal_reporting(worker, mode, shell_route):
    if shell_route == "sh":
        worker.bash = shutil.which("sh")
        assert worker.bash
    worker.seed(count=5, age=200 * 86400)
    originals = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in worker.backups.iterdir()
    }
    worker.config(dump_elapsed=3597, slow_archive_seconds=2, archive_clock_step=2)
    args = ["--once"]
    if mode != "normal":
        args = ["--once-preserve-archives"]
        worker.env["BACKUP_EXPECTED_SCHEMA_REVISION"] = "039"
        worker.config(revision="039")
        if mode == "transition":
            raw = '{"status":"ok"}'
            worker.write("state.json", {"status": "ok"})
            worker.env.update(
                BACKUP_LEGACY_EXPECTED_SHA256=hashlib.sha256(raw.encode()).hexdigest(),
                BACKUP_LEGACY_EXPECTED_UPDATED_AT="2026-01-01T00:00:00Z",
            )
            args.append("--transition-legacy")
    started = time.monotonic()
    worker.run(args=args)
    elapsed = time.monotonic() - started
    state = worker.read("state.json")
    assert state["last_attempt"]["status"] == "succeeded"
    assert state["last_success"]["completed_at"] == state["last_attempt"]["finished_at"]
    assert elapsed < 8, "inventory checks must share the original remaining three seconds"
    reports = worker.events("status")
    finish = [event for event in reports if event["phase"] == "finish"]
    assert len(finish) == 1 and finish[0]["timeout"] == 20
    if mode == "normal":
        checks = worker.events("slow_archive_check")
        assert [event["timeout"] for event in checks] == [3, 1]
        diagnostic = [event for event in reports if event["phase"] == "diagnostic"]
        assert len(diagnostic) == 1 and diagnostic[0]["timeout"] == 20
        assert state["last_attempt"]["reason_code"] == "backup_retention_blocked"
    else:
        assert not worker.events("retention") and not worker.events("slow_archive_check")
        assert not [event for event in reports if event["phase"] == "diagnostic"]
        assert state["last_attempt"]["reason_code"] == ""
    assert all(event["values"]["status"] not in {"failed", "interrupted"} for event in reports)
    for name, expected in originals.items():
        path = worker.backups / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected
    assert not worker.events("sleep")
    assert not list(worker.backups.glob(".work-*"))
    lock = subprocess.run(
        [worker.env["HPO_BACKUP_REAL_FLOCK"], "-n", str(worker.backups / ".backup.lock"), "true"],
        timeout=2,
        check=False,
    )
    assert lock.returncode == 0, "bounded exit must release the staging/destination lock"


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "missing_checksum",
        "malformed_manifest",
        "unsupported_manifest",
        "foreign_source",
        "orphan_manifest",
        "symlink",
    ],
)
def test_untrusted_inventory_blocks_all_deletion(worker, damage):
    initial = worker.seed(count=5, age=200 * 86400)
    originals = worker.dumps()
    archive = sorted(originals)[2]
    if damage == "checksum":
        archive.write_bytes(b"corruption")
    elif damage == "missing_checksum":
        Path(str(archive) + ".sha256").unlink()
    elif damage in {"malformed_manifest", "unsupported_manifest", "foreign_source"}:
        path = Path(str(archive) + ".json")
        metadata = json.loads(path.read_text())
        if damage == "malformed_manifest":
            path.write_text("{invalid", encoding="utf-8")
        else:
            metadata.update(
                {"schema_version": 2}
                if damage == "unsupported_manifest"
                else {"source_id": "foreign"}
            )
            path.write_text(json.dumps(metadata), encoding="utf-8")
    elif damage == "orphan_manifest":
        (worker.backups / ("heatpump-" + "d" * 32 + ".dump.json")).write_text("{}")
    else:
        target = worker.root / "unrelated.dump"
        archive.replace(target)
        archive.symlink_to(target)
    sibling = worker.root / "sibling"
    sibling.mkdir()
    (sibling / archive.name).write_bytes(b"unrelated")
    worker.run()
    state = worker.read("state.json")
    assert originals <= worker.dumps()
    assert state["last_attempt"]["reason_code"] == "backup_retention_blocked"
    assert state["last_success"]["archive_id"] != initial["last_success"]["archive_id"]
    assert (sibling / archive.name).read_bytes() == b"unrelated"


def test_policy_change_is_observed_on_next_bounded_wake(worker):
    worker.seed(age=7200)
    worker.config(sleep_policy="3600|100|2764800|3600", stop_after_sleeps=2)
    result = worker.run(args=(), check=False)
    assert result.returncode == 73  # fake sleep intentionally bounds the scheduler
    assert len(worker.events("pg_dump")) == 1
    assert all(row["args"] == ["60"] for row in worker.events("sleep"))
    assert len(worker.events("policy")) == 2


def test_staging_lock_blocks_worker_retention_and_staged_bytes_survive_deletion(worker):
    old = worker.seed(count=4, age=200 * 86400)["last_success"]
    original = worker.backups / (old["archive_id"] + ".dump")
    before = original.read_bytes()
    worker.config(pause_stage=True)
    token = "b" * 32
    process = subprocess.Popen(
        [worker.bash, str(SCRIPTS / "stage_backup.sh")],
        env={**worker.env, "BACKUP_STAGE_TOKEN": token},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (worker.root / "copy_entered").exists():
            assert process.poll() is None, "stager failed before reaching the locked copy"
            assert time.monotonic() < deadline, "staging did not reach copy barrier"
            time.sleep(0.01)
        worker.run()
        assert worker.events("lock_busy")
        assert not worker.events("pg_dump")
        assert not worker.events("retention")
        assert original.read_bytes() == before
        (worker.root / "copy_release").touch()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        staged = worker.backups / (".stage-" + token) / "archive.dump"
        assert stdout.strip() == str(staged.parent)
        assert staged.read_bytes() == before
        worker.run()
        assert worker.events("retention")
        assert not original.exists()
        assert staged.read_bytes() == before
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == old["sha256"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def process_alive(pid):
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] != "Z"
    except ProcessLookupError:
        return False


def await_file(path, process, seconds=8):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert process.poll() is None, "owned subprocess exited before test barrier"
        assert time.monotonic() < deadline, "owned subprocess did not reach test barrier"
        time.sleep(0.01)


def assert_group_dead(group):
    # A reparented zombie is dead and holds no lock; no LIVE member may remain.
    deadline = time.monotonic() + 2
    while True:
        members = []
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().split(") ", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    members.append(stat.parent.name)
            except FileNotFoundError:
                continue  # exited between directory enumeration and read
        if not members:
            return
        assert time.monotonic() < deadline, f"owned process group still live: {members}"
        time.sleep(0.01)


def exercise_resistant_tool(worker, cancel, pid_only, *, phase="dump", shell=None):
    original_state = worker.seed()
    previous = original_state["last_success"]
    original_archives = worker.dumps()
    grace = 8
    # Cancellation must beat the tool's OWN timer, rather than accidentally
    # passing when a one-second timeout naturally expires during shutdown.
    worker.config(
        resistant=True,
        resistant_phase=phase,
        timeout_cap=60 if cancel else 1,
        pid_only_timeout=pid_only,
    )
    process = subprocess.Popen(
        [shell or worker.bash, str(SCRIPTS / "backup_db.sh"), "--once"],
        env=worker.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    child = None
    try:
        await_file(worker.root / "resistant.json", process)
        child = worker.read("resistant.json")
        assert child["phase"] == phase
        if cancel:
            assert child["timeout"] > grace
        started = time.monotonic()
        if cancel:
            process.terminate()
        stdout, stderr = process.communicate(timeout=grace)
        assert time.monotonic() - started < grace
        assert process.returncode == (143 if cancel else 0), (stdout, stderr)
        assert not process_alive(child["pid"])
        assert_group_dead(child["group"])
        state = worker.read("state.json")
        if phase == "identity":
            assert state == original_state
            assert not worker.events("status")  # no owner before preflight
        else:
            assert state["last_attempt"]["status"] == ("interrupted" if cancel else "failed")
            assert state["last_attempt"]["reason_code"] == "backup_attempt_failed"
        assert state["last_success"] == previous
        assert worker.dumps() == original_archives
        assert not list(worker.backups.glob(".work-*"))
        result = subprocess.run(
            [
                worker.env["HPO_BACKUP_REAL_FLOCK"],
                "-n",
                str(worker.backups / ".backup.lock"),
                "true",
            ],
            timeout=2,
            check=False,
        )
        assert result.returncode == 0
        assert time.monotonic() - started < grace
    finally:
        if child:
            try:
                os.killpg(child["group"], signal.SIGKILL)
            except ProcessLookupError:
                pass  # already-settled exclusively owned test process group
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("pid_only", [False, True])
def test_term_resistant_dump_is_hard_bounded_dead_and_unlocks_with_preserved_success(
    worker, cancel, pid_only
):
    exercise_resistant_tool(worker, cancel, pid_only)


@pytest.mark.parametrize("phase", ["checksum", "identity"])
@pytest.mark.parametrize("pid_only", [False, True])
@pytest.mark.parametrize("shell_route", ["bash", "sh"])
def test_cancellation_during_captured_tool_is_owned_by_main_shell(
    worker, phase, pid_only, shell_route
):
    shell = worker.bash if shell_route == "bash" else shutil.which("sh")
    assert shell, "Linux shell qualification must provide /bin/sh as well as bash"
    exercise_resistant_tool(worker, True, pid_only, phase=phase, shell=shell)


@pytest.mark.parametrize("when", ["before", "due", "overdue"])
def test_abandoned_pid_and_uuid_workspaces_do_not_block_startup_or_due_policy(worker, when):
    previous = worker.seed(count=5, age=200 * 86400)
    due = int(dt.datetime.fromisoformat(previous["last_success"]["snapshot_at"]).timestamp())
    due += INTERVAL
    worker.write("clock.json", NOW if when == "overdue" else due - (when == "before"))
    abandoned = worker.backups / ".work-11111111-1111-4111-8111-111111111111"
    abandoned.mkdir(mode=0o700)
    (abandoned / "foreign").write_bytes(b"abandoned UUID workspace")
    program = (
        'set -eu; umask 077; work="$BACKUP_DIR/.work-$$"; mkdir "$work"; '
        'printf "abandoned PID workspace\\n" > "$work/foreign"; exec "$1" "$2" --once'
    )
    # exec preserves this exact PID: the abandoned legacy name therefore
    # collides with the real worker PID, not merely an unrelated test number.
    process = subprocess.Popen(
        [worker.bash, "-c", program, "workspace-test", worker.bash, str(SCRIPTS / "backup_db.sh")],
        env=worker.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=8)
    legacy = worker.backups / (".work-" + str(process.pid))

    def assert_abandoned_untouched():
        assert set(worker.backups.glob(".work-*")) == {legacy, abandoned}
        assert {p.name: p.read_bytes() for p in legacy.iterdir()} == {
            "foreign": b"abandoned PID workspace\n"
        }
        assert {p.name: p.read_bytes() for p in abandoned.iterdir()} == {
            "foreign": b"abandoned UUID workspace"
        }

    assert_abandoned_untouched()  # the fresh invocation's workspace is gone
    assert worker.events("policy")
    assert len(worker.events("pg_dump")) == (0 if when == "before" else 1)
    state = worker.read("state.json")
    if when == "before":
        assert state == previous
    else:
        assert state["last_attempt"]["status"] == "succeeded"
        assert state["last_attempt"]["reason_code"] != "backup_retention_blocked"
        assert len(worker.dumps()) == (2 if when == "overdue" else 6)
    worker.run()  # persisted success suppresses a duplicate on another restart
    assert len(worker.events("pg_dump")) == (0 if when == "before" else 1)
    assert_abandoned_untouched()

    # Staging's existing exact selection/copy/cleanup must also ignore both
    # abandoned workspaces. No changes to its ownership protocol are needed.
    worker.env["BACKUP_STAGE_TOKEN"] = "d" * 32
    result = worker.run("stage_backup.sh", args=())
    stage = Path(result.stdout.strip())
    assert (stage / "archive.dump").is_file()
    assert worker.run("stage_backup.sh", args=("cleanup",)).stdout.strip() == "settled"
    assert_abandoned_untouched()


def test_main_shell_capture_preserves_multiline_json_and_sidecar_trailing_newlines(worker):
    previous = worker.seed(count=3, age=200 * 86400)
    verified = worker.backups / (previous["last_verified"]["archive_id"] + ".dump")
    path = Path(str(verified) + ".json")
    path.write_text(json.dumps(json.loads(path.read_text()), indent=2) + "\n\n", encoding="utf-8")
    sidecar = Path(str(verified) + ".sha256")
    sidecar.write_text(sidecar.read_text() + "\n\n", encoding="utf-8")
    worker.run()
    state = worker.read("state.json")
    assert state["last_attempt"]["status"] == "succeeded"
    assert state["last_attempt"]["reason_code"] != "backup_retention_blocked"
    assert worker.dumps() == {
        verified,
        worker.backups / (state["last_success"]["archive_id"] + ".dump"),
    }
    assert not list(worker.backups.glob(".work-*"))


@pytest.mark.parametrize("cancel", [False, "term", "kill"])
def test_source_stage_timeout_or_cancel_settles_copy_releases_lock_without_stopping_worker(
    worker, cancel
):
    worker.seed(age=0)
    worker.config(pause_stage=True, real_sleep=True, pid_only_timeout=True)
    worker.env["BACKUP_STAGE_TIMEOUT_SECONDS"] = "1"
    source = subprocess.Popen(
        [worker.bash, str(SCRIPTS / "backup_db.sh")],
        env=worker.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stage = None
    try:
        deadline = time.monotonic() + 8
        while not worker.events("sleep"):
            assert source.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        token = "c" * 32
        env = {**worker.env, "BACKUP_STAGE_TOKEN": token}
        reserved = subprocess.run(
            [worker.bash, str(SCRIPTS / "stage_backup.sh"), "reserve"],
            env=env,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert reserved.returncode == 0, reserved.stderr
        stage = subprocess.Popen(
            [worker.bash, str(SCRIPTS / "stage_backup.sh"), "run"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        await_file(worker.root / "copy_entered", stage)
        started = time.monotonic()
        if cancel == "term":
            stage.terminate()
        elif cancel == "kill":
            stage.kill()  # even a dead supervisor must not orphan the copy group
        cleaned = subprocess.run(
            [worker.bash, str(SCRIPTS / "stage_backup.sh"), "cleanup"],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        assert cleaned.returncode == 0, cleaned.stderr
        assert cleaned.stdout.strip() == "settled"
        assert time.monotonic() - started < 8
        assert not process_alive(worker.read("stage_child.json")["pid"])
        assert not (worker.backups / (".stage-" + token)).exists()
        assert source.poll() is None, "staging cleanup must not stop source worker"
        result = subprocess.run(
            [
                worker.env["HPO_BACKUP_REAL_FLOCK"],
                "-n",
                str(worker.backups / ".backup.lock"),
                "true",
            ],
            timeout=2,
            check=False,
        )
        assert result.returncode == 0
        late = subprocess.run(
            [worker.bash, str(SCRIPTS / "stage_backup.sh"), "run"],
            env=env,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert late.returncode != 0, "a queued exec must not recreate a cleaned stage"
        assert not (worker.backups / (".stage-" + token)).exists()
        stage.communicate(timeout=5)
        assert stage.returncode != 0  # timeout or cancellation, never a successful copy
    finally:
        if stage and stage.poll() is None:
            stage.kill()
        if stage:
            stage.communicate(timeout=8)
        if source.poll() is None:
            source.terminate()
        source.communicate(timeout=8)

"""Real SQL/worker qualification in a creation-owned, networkless disposable PG16.

The canonical owned runner requires this explicit --noconftest matrix and its
separate complete inventory before the full seal. Ordinary unit/E2E discovery is
unchanged. No configured database, credentials, network or archive is accepted.
The immutable image must already exist (no pull).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import uuid

import pytest

IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


class Database:
    def command(self, *args, input=None, check=True):
        result = subprocess.run(
            ["docker", "exec", "-i", self.id, *args],
            input=input.encode() if isinstance(input, str) else input,
            capture_output=True,
            timeout=45,
        )
        result.stdout = result.stdout.decode()
        result.stderr = result.stderr.decode()
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def sql(self, query):
        return self.command("psql", "-XqAt", "-v", "ON_ERROR_STOP=1", input=query).stdout.strip()

    def row(self):
        return self.sql(
            "SELECT json_build_array(updated_at,details_json) FROM service_heartbeats WHERE service='backup'"
        )

    def state(self):
        return json.loads(json.loads(self.row())[1])

    def operation(self, *, check=True, omit=(), application_name=None, **overrides):
        values = {
            "backup_start": "false",
            "backup_finish": "false",
            "verification_start": "false",
            "verification_finish": "false",
            "succeeded": "false",
            "attempt": "a" * 32,
            "archive": "heatpump-" + "a" * 32,
            "started": self.started,
            "finished": self.finished,
            "status": "running",
            "reason": "",
            "replica": "pending",
            "checksum": "b" * 64,
            "source": self.source,
            "revision": "039",
            "retention_mode": "preserve",
            "expected_state": self.fingerprint(),
            "expected_source": self.source,
            "expected_revision": "039",
            "interval": "3600",
            "timeout": "60",
        }
        values.update(overrides)
        args = ["psql", "-XqAt", "-v", "ON_ERROR_STOP=1"]
        if application_name:
            args = ["env", f"PGAPPNAME={application_name}", *args]
        for key, value in values.items():
            if key not in omit:
                args.extend(["-v", f"{key}={value}"])
        return self.command(*args, "-f", "/scripts/backup_status.sql", check=check)

    def fingerprint(self):
        return self.sql(
            "SELECT COALESCE((SELECT encode(sha256(convert_to(updated_at::text||'/'||details_json,'UTF8')),'hex') FROM service_heartbeats WHERE service='backup'),'absent')"
        )

    def legacy(self, raw=' \n{ "status" : "ok" }\n\n'):
        # Parameter-free fixture literal, not caller-supplied SQL.
        assert "$raw$" not in raw
        self.sql(
            "INSERT INTO service_heartbeats VALUES ('backup',now()-interval '1 day',$raw$"
            + raw
            + "$raw$)"
        )
        self.original = json.loads(self.row())
        return {
            "transition_legacy": "true",
            "legacy_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "legacy_updated_at": self.original[0],
        }

    def start(self, **kw):
        return self.operation(backup_start="true", **kw)

    def finish(self, **kw):
        return self.operation(
            **{
                **dict(
                    backup_finish="true", status="succeeded", succeeded="true", replica="disabled"
                ),
                **kw,
            }
        )

    def install(self, name, content):
        self.command("mkdir", "-p", "/tmp/bin")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            data = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o700
            archive.addfile(member, io.BytesIO(data))
        self.command("tar", "-x", "-C", "/tmp/bin", input=stream.getvalue())

    def worker_args(self, fences=None):
        args = [
            "env",
            "BACKUP_DIR=/tmp/backups",
            f"BACKUP_EXPECTED_SOURCE_ID={self.source}",
            "BACKUP_EXPECTED_SCHEMA_REVISION=039",
        ]
        if fences:
            args.extend(
                [
                    f"BACKUP_LEGACY_EXPECTED_SHA256={fences['legacy_sha256']}",
                    f"BACKUP_LEGACY_EXPECTED_UPDATED_AT={fences['legacy_updated_at']}",
                ]
            )
        return args


@pytest.fixture
def db():
    assert not os.environ.get("DATABASE_URL"), "No configured database is accepted"
    token = uuid.uuid4().hex
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec",
            "--user",
            "postgres",
            "--label",
            f"org.heatpump.legacy-test={token}",
            "-v",
            f"{SCRIPTS}:/scripts:ro",
            "-e",
            "PGHOST=/tmp",
            "-e",
            "PGUSER=postgres",
            "-e",
            "PGDATABASE=postgres",
            "--entrypoint",
            "sh",
            IMAGE,
            "-ceu",
            "initdb -D /tmp/db -A trust >/tmp/init.log; "
            "pg_ctl -D /tmp/db -l /tmp/pg.log -o \"-k /tmp -c listen_addresses=''\" -w start; "
            "touch /tmp/ready; sleep 600",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    database = Database()
    database.id = result.stdout.strip()
    assert len(database.id) == 64
    try:
        database.command(
            "sh",
            "-c",
            "for n in $(seq 1 100); do test ! -f /tmp/ready || exit 0; sleep .1; done; exit 1",
        )
        database.sql("""
          CREATE TABLE service_heartbeats(service text PRIMARY KEY, updated_at timestamptz NOT NULL, details_json text);
          CREATE TABLE alembic_version(version_num text);
          INSERT INTO alembic_version VALUES ('039');
          CREATE TABLE settings(key text PRIMARY KEY,value text);
          CREATE EXTENSION timescaledb;
        """)
        database.source = database.sql(
            "SELECT md5(system_identifier::text||'/'||current_database()) FROM pg_control_system()"
        )
        database.started = database.sql(
            "SELECT to_char(now()-interval '2 seconds','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
        )
        database.finished = database.sql('SELECT to_char(now(),\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')')
        yield database
    finally:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.heatpump.legacy-test"}}',
                database.id,
            ],
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert inspected.returncode == 0 and inspected.stdout.strip() == token
        subprocess.run(
            ["docker", "stop", "-t", "1", database.id], check=True, capture_output=True, timeout=20
        )


def test_transition_exact_original_and_terminal_replay(db):
    fences = db.legacy()
    db.start(**fences)
    state = db.state()
    original = state["legacy_transition"]["original"]
    assert original["details_json"] == db.original[1]
    assert original["details_sha256"] == fences["legacy_sha256"]
    assert original["service"] == "backup"
    assert state["last_attempt"]["status"] == "running"
    db.finish()
    row = db.row()
    db.finish()
    assert db.row() == row
    assert db.state()["legacy_transition"]["first_attempt"] == db.state()["last_attempt"]
    assert (
        db.operation(
            backup_finish="true",
            status="failed",
            reason="backup_attempt_failed",
            replica="disabled",
            check=False,
        ).returncode
        != 0
    )
    assert db.row() == row


@pytest.mark.parametrize(
    "raw",
    [
        '{"status":"ok","status":"ok"}',
        '{"status":"ok","extra":1}',
        "null",
        "{}",
        '{"status":"bad"}',
    ],
)
def test_legacy_shape_rejected_without_write(db, raw):
    fences = db.legacy(raw)
    row = db.row()
    assert db.start(check=False, **fences).returncode != 0
    assert db.row() == row


def test_transition_race_one_owner(db):
    fences = db.legacy()
    fingerprint = db.fingerprint()

    def start(letter):
        return db.start(
            check=False,
            attempt=letter * 32,
            archive="heatpump-" + letter * 32,
            expected_state=fingerprint,
            **fences,
        ).returncode

    with concurrent.futures.ThreadPoolExecutor() as pool:
        codes = list(pool.map(start, ["a", "c"]))
    assert sorted(codes)[0] == 0 and sum(code == 0 for code in codes) == 1
    assert db.state()["legacy_transition"]["first_attempt_id"] == db.state()["last_attempt"]["id"]


def test_normal_diagnostic_and_replays(db):
    db.start(retention_mode="normal")
    db.finish(retention_mode="normal")
    before = db.state()
    db.operation(
        backup_retention_diagnostic="true",
        status="succeeded",
        succeeded="true",
        reason="backup_retention_blocked",
        replica="disabled",
        retention_mode="normal",
    )
    after = db.state()
    expected = json.loads(json.dumps(before))
    expected["last_attempt"]["reason_code"] = "backup_retention_blocked"
    assert after == expected
    row = db.row()
    db.finish(retention_mode="normal")
    db.operation(
        backup_retention_diagnostic="true",
        status="succeeded",
        succeeded="true",
        reason="backup_retention_blocked",
        replica="disabled",
        retention_mode="normal",
    )
    assert db.row() == row


def test_preserve_rejects_diagnostic(db):
    db.start(**db.legacy())
    db.finish()
    row = db.row()
    assert (
        db.operation(
            backup_retention_diagnostic="true",
            status="succeeded",
            succeeded="true",
            reason="backup_retention_blocked",
            replica="disabled",
            check=False,
        ).returncode
        != 0
    )
    assert db.row() == row


def test_cas_and_identity_failures_leave_exact_legacy_row(db):
    fences = db.legacy()
    row = db.row()
    for change in [
        {"legacy_sha256": "0" * 64},
        {"legacy_updated_at": "2020-01-01T00:00:00Z"},
        {"expected_state": "0" * 64},
        {"expected_source": "other"},
        {"expected_revision": "038"},
        {"source": "other"},
        {"revision": "038"},
        {"backup_finish": "true"},
        {"verification_start": "true"},
    ]:
        assert db.start(check=False, **{**fences, **change}).returncode != 0
        assert db.row() == row


def test_terminal_and_diagnostic_conflicts_are_no_write(db):
    db.start(retention_mode="normal")
    db.finish(retention_mode="normal")
    row = db.row()
    for change in [
        {"checksum": "c" * 64},
        {"source": "changed"},
        {"revision": "038"},
        {"replica": "optional_failed"},
        {"started": "2020-01-01T00:00:00Z"},
        {"finished": "2020-01-01T00:00:00Z"},
        {"reason": "backup_retention_blocked"},
        {"attempt": "c" * 32, "archive": "heatpump-" + "c" * 32},
        {"retention_mode": "preserve"},
    ]:
        assert db.finish(check=False, **{**{"retention_mode": "normal"}, **change}).returncode != 0
        assert db.row() == row

    diagnostic = dict(
        backup_retention_diagnostic="true",
        status="succeeded",
        succeeded="true",
        replica="disabled",
        reason="backup_retention_blocked",
        retention_mode="normal",
    )
    for change in [
        {"reason": ""},
        {"checksum": "c" * 64},
        {"backup_finish": "true"},
        {"replica": "optional_failed"},
        {"source": "changed"},
    ]:
        assert db.operation(check=False, **{**diagnostic, **change}).returncode != 0
        assert db.row() == row
    # Pre-change readable histories cannot be retroactively annotated as normal.
    db.sql(
        "UPDATE service_heartbeats SET details_json="
        "(details_json::jsonb #- '{last_attempt,retention_mode}')::text WHERE service='backup'"
    )
    row = db.row()
    assert db.operation(check=False, **diagnostic).returncode != 0
    assert db.row() == row


def test_verifier_defaults_and_concurrent_merge_with_replay_and_diagnostic(db):
    db.start(retention_mode="normal")
    db.finish(retention_mode="normal")

    def verification():
        optional = (
            "retention_mode",
            "expected_state",
            "expected_source",
            "expected_revision",
            "interval",
            "timeout",
            "replica",
        )
        db.operation(verification_start="true", omit=optional)
        db.operation(
            verification_finish="true", status="succeeded", succeeded="true", omit=optional
        )

    def diagnostic():
        db.operation(
            backup_retention_diagnostic="true",
            status="succeeded",
            succeeded="true",
            replica="disabled",
            reason="backup_retention_blocked",
            retention_mode="normal",
        )

    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = [
            pool.submit(fn)
            for fn in (verification, diagnostic, lambda: db.finish(retention_mode="normal"))
        ]
        for future in futures:
            future.result()
    state = db.state()
    assert state["last_attempt"]["reason_code"] == "backup_retention_blocked"
    assert state["last_verification"]["status"] == "succeeded"
    assert state["last_verified"]["sha256"] == "b" * 64


@pytest.mark.parametrize("first", ["diagnostic", "start"])
def test_retention_diagnostic_racing_newer_backup_start_is_fenced(db, first):
    db.started = db.sql("SELECT to_char(now()-interval '2 hours','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")
    db.finished = db.sql(
        "SELECT to_char(now()-interval '119 minutes','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    )
    db.start(retention_mode="normal")
    db.finish(retention_mode="normal")
    success = db.state()["last_success"]
    diagnostic = dict(
        backup_retention_diagnostic="true",
        status="succeeded",
        succeeded="true",
        replica="disabled",
        reason="backup_retention_blocked",
        retention_mode="normal",
    )
    newer = dict(
        backup_start="true",
        attempt="c" * 32,
        archive="heatpump-" + "c" * 32,
        started=db.sql('SELECT to_char(now(),\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')'),
        retention_mode="normal",
        expected_state=db.fingerprint(),
    )

    def wait_for(application, wait_event):
        deadline = time.monotonic() + 10
        while (
            db.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name="
                f"'{application}' AND wait_event='{wait_event}'"
            )
            != "1"
        ):
            assert time.monotonic() < deadline, f"{application} did not reach {wait_event}"
            time.sleep(0.02)

    # Hold the actual SQL advisory lock, then enqueue both genuine transactions
    # in each serialization order. Neither operation is replaced by a fake.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        holder = pool.submit(
            db.command,
            "env",
            "PGAPPNAME=diagnostic-race-holder",
            "psql",
            "-XqAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "BEGIN; SELECT pg_advisory_xact_lock(hashtextextended('backup:status',0)); "
            "SELECT pg_sleep(30); COMMIT",
            check=False,
        )
        futures = {}
        try:
            wait_for("diagnostic-race-holder", "PgSleep")
            for operation in (first, "start" if first == "diagnostic" else "diagnostic"):
                application = "diagnostic-race-" + operation
                futures[operation] = pool.submit(
                    db.operation,
                    check=False,
                    application_name=application,
                    **(newer if operation == "start" else diagnostic),
                )
                wait_for(application, "advisory")
        finally:
            db.sql(
                "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                "WHERE application_name='diagnostic-race-holder'"
            )
        assert holder.result().returncode != 0  # canceled test-owned lock holder
        results = {name: future.result() for name, future in futures.items()}
    assert results[first].returncode == 0, results[first].stderr
    loser = "start" if first == "diagnostic" else "diagnostic"
    assert results[loser].returncode != 0
    if first == "diagnostic":
        assert db.state()["last_attempt"]["reason_code"] == "backup_retention_blocked"
        assert db.state()["last_success"] == success
        # Diagnostic changed the observed CAS: retry is a fresh genuine start.
        db.start(
            **{
                key: value
                for key, value in newer.items()
                if key not in {"backup_start", "expected_state"}
            }
        )
    state = db.state()
    assert state["last_attempt"]["id"] == "c" * 32
    assert state["last_attempt"]["status"] == "running"
    assert "reason_code" not in state["last_attempt"]
    assert state["last_success"] == success
    row = db.row()
    assert db.operation(check=False, **diagnostic).returncode != 0
    assert db.row() == row  # stale diagnostic cannot annotate the newer attempt


def test_failed_first_attempt_retry_is_distinct_and_fenced(db):
    db.started = db.sql(
        "SELECT to_char(now()-interval '20 minutes','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    )
    db.finished = db.sql(
        "SELECT to_char(now()-interval '19 minutes','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    )
    db.start(**db.legacy())
    db.operation(
        backup_finish="true", status="failed", reason="backup_attempt_failed", replica="disabled"
    )
    original = db.state()["legacy_transition"]
    db.command("mkdir", "/tmp/backups")
    db.command("sh", "-c", "printf preserved > /tmp/backups/old.dump")
    db.command(*db.worker_args(), "sh", "/scripts/backup_db.sh", "--once-preserve-archives")
    state = db.state()
    assert state["legacy_transition"] == original
    assert original["first_attempt"]["status"] == "failed"
    assert state["last_attempt"]["id"] != original["first_attempt_id"]
    assert state["last_attempt"]["status"] == "succeeded"
    assert state["last_attempt"]["reason_code"] == ""
    assert db.command("cat", "/tmp/backups/old.dump").stdout == "preserved"
    row = db.row()
    assert db.finish(check=False).returncode != 0  # stale first finisher
    assert db.row() == row
    assert (
        db.command(
            *db.worker_args(),
            "sh",
            "/scripts/backup_db.sh",
            "--once-preserve-archives",
            check=False,
        ).returncode
        != 0
    )
    assert db.row() == row


def test_stale_running_retry_requires_source_settlement_and_preserves_incomplete_first(db):
    db.started = db.sql(
        "SELECT to_char(now()-interval '20 minutes','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    )
    db.start(**db.legacy())
    original = db.state()["legacy_transition"]
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            db.id,
            "env",
            "PGAPPNAME=backup:" + "a" * 32,
            "psql",
            "-XqAt",
            "-c",
            "SELECT pg_sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            db.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name='backup:"
                + "a" * 32
                + "'"
            )
            != "1"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        new = dict(attempt="c" * 32, archive="heatpump-" + "c" * 32)
        row = db.row()
        assert db.start(check=False, **new).returncode != 0
        assert db.row() == row
        db.sql(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='backup:"
            + "a" * 32
            + "'"
        )
        process.communicate(timeout=15)
        db.start(**new)
        assert db.state()["legacy_transition"] == original
        assert original["first_attempt"]["status"] == "running"
        db.finish(**new)
        assert db.state()["legacy_transition"] == original
    finally:
        if process.poll() is None:
            db.sql(
                "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='backup:"
                + "a" * 32
                + "'"
            )
            process.communicate(timeout=15)


def test_normal_diagnostic_preserves_legacy_and_first_attempt(db):
    db.started = db.sql("SELECT to_char(now()-interval '2 hours','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")
    db.finished = db.sql(
        "SELECT to_char(now()-interval '119 minutes','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    )
    db.start(**db.legacy())
    db.finish()
    original = db.state()["legacy_transition"]
    new = dict(attempt="c" * 32, archive="heatpump-" + "c" * 32, retention_mode="normal")
    db.start(**new)
    db.finish(**new)
    db.operation(
        backup_retention_diagnostic="true",
        status="succeeded",
        succeeded="true",
        replica="disabled",
        reason="backup_retention_blocked",
        **new,
    )
    assert db.state()["legacy_transition"] == original
    assert db.state()["last_attempt"]["reason_code"] == "backup_retention_blocked"


@pytest.mark.parametrize("publish_race", [False, True])
def test_worker_collision_cannot_overwrite_or_clean_existing_archive(db, publish_race):
    fences = db.legacy()
    db.command("mkdir", "/tmp/backups")
    fixed = "a" * 32
    db.install("tr", f"#!/bin/sh\nprintf '%s\\n' {fixed}\n")
    archive = "/tmp/backups/heatpump-" + fixed + ".dump"
    if publish_race:
        real_dump = db.command("sh", "-c", "command -v pg_dump").stdout.strip()
        db.install("pg_dump", f'#!/bin/sh\nprintf preserved > {archive}\nexec {real_dump} "$@"\n')
    else:
        db.command("sh", "-c", "printf preserved > " + archive + ".partial")
    row = db.row()
    result = db.command(
        *db.worker_args(fences),
        "PATH=/tmp/bin:/usr/local/bin:/usr/bin:/bin",
        "sh",
        "/scripts/backup_db.sh",
        "--once-preserve-archives",
        "--transition-legacy",
        check=False,
    )
    assert result.returncode != 0
    path = archive if publish_race else archive + ".partial"
    assert db.command("cat", path).stdout == "preserved"
    if publish_race:
        assert db.state()["last_attempt"]["status"] == "failed"
        assert "last_success" not in db.state()
    else:
        assert db.row() == row


@pytest.mark.parametrize("suffix", [".sha256.partial", ".json.partial"])
def test_worker_never_cleans_metadata_scratch_it_did_not_create(db, suffix):
    fences = db.legacy()
    db.command("mkdir", "/tmp/backups")
    fixed = "a" * 32
    db.install("tr", f"#!/bin/sh\nprintf '%s\\n' {fixed}\n")
    archive = "/tmp/backups/heatpump-" + fixed + ".dump"
    real_dump = db.command("sh", "-c", "command -v pg_dump").stdout.strip()
    db.install(
        "pg_dump", f'#!/bin/sh\nprintf preserved > {archive}{suffix}\nexec {real_dump} "$@"\n'
    )
    result = db.command(
        *db.worker_args(fences),
        "PATH=/tmp/bin:/usr/local/bin:/usr/bin:/bin",
        "sh",
        "/scripts/backup_db.sh",
        "--once-preserve-archives",
        "--transition-legacy",
        check=False,
    )
    assert result.returncode != 0
    assert db.command("cat", archive + suffix).stdout == "preserved"
    assert db.state()["last_attempt"]["status"] == "failed"


@pytest.mark.parametrize(
    "phase", ["start", "finish", "diagnostic", "finish_unavailable", "diagnostic_unavailable"]
)
def test_real_sql_worker_lost_ack_and_unavailable_reporting(db, phase):
    normal = phase.startswith("diagnostic")
    fences = None if normal else db.legacy()
    real = db.command("sh", "-c", "command -v psql").stdout.strip()
    db.install(
        "psql",
        f"""#!/bin/sh
phase=other
for arg do
 case $arg in backup_start=true) phase=start ;; backup_finish=true) phase=finish ;;
 backup_retention_diagnostic=true) phase=diagnostic ;; esac
done
[ "$phase" = other ] || printf '%s\\n' "$phase" >> /tmp/report-phases
[ "$HPO_ACK" != finish_unavailable ] || [ "$phase" != finish ] || exit 1
[ "$HPO_ACK" != diagnostic_unavailable ] || [ "$phase" != diagnostic ] || exit 1
{real} "$@" || exit $?
if [ "$phase" = "$HPO_ACK" ] && [ ! -e "/tmp/ack-$phase" ]; then
 touch "/tmp/ack-$phase"; exit 1
fi
""",
    )
    db.command("mkdir", "/tmp/backups")
    args = db.worker_args(fences)
    args.extend(["PATH=/tmp/bin:/usr/local/bin:/usr/bin:/bin", f"HPO_ACK={phase}"])
    mode = ["--once"] if normal else ["--once-preserve-archives", "--transition-legacy"]
    result = db.command(*args, "sh", "/scripts/backup_db.sh", *mode, check=False)
    state = db.state()
    if phase == "finish_unavailable":
        assert result.returncode != 0
        assert state["last_attempt"]["status"] == "running"
        assert "last_success" not in state
    else:
        assert result.returncode == 0, result.stderr
        assert state["last_attempt"]["status"] == "succeeded"
        assert state["last_attempt"]["reason_code"] == (
            "backup_retention_blocked" if phase == "diagnostic" else ""
        )
        if phase == "diagnostic_unavailable":
            assert "backup_reporting_incomplete" in result.stderr
            assert db.command("cat", "/tmp/report-phases").stdout.splitlines() == [
                "start",
                "finish",
                "diagnostic",
                "diagnostic",
            ]
            attempt, success = state["last_attempt"], state["last_success"]
            assert attempt["archive_id"] == success["archive_id"]
            assert attempt["sha256"] == success["sha256"]
            assert attempt["started_at"] == success["snapshot_at"]
            assert attempt["finished_at"] == success["completed_at"]


@pytest.mark.parametrize(("signal", "exit_code"), [("TERM", 143), ("INT", 130), ("HUP", 129)])
def test_real_sql_cancellation_reports_only_owned_interruption(db, signal, exit_code):
    fences = db.legacy()
    db.command("mkdir", "/tmp/backups")
    db.install("pg_dump", "#!/bin/sh\nexec psql -XqAt -c 'SELECT pg_sleep(60)'\n")
    args = db.worker_args(fences) + [
        "PATH=/tmp/bin:/usr/local/bin:/usr/bin:/bin",
        "sh",
        "/scripts/backup_db.sh",
        "--once-preserve-archives",
        "--transition-legacy",
    ]
    # This shell becomes the actual worker PID, not a daemon or host process.
    process = subprocess.Popen(
        ["docker", "exec", "-i", db.id, "sh", "-c", 'echo $$; exec "$@"', "sh", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = process.stdout.readline().strip()
    assert pid.isdecimal()
    try:
        deadline = time.monotonic() + 15
        while (
            db.sql("SELECT count(*) FROM pg_stat_activity WHERE query='SELECT pg_sleep(60)'") != "1"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        db.command("kill", "-" + signal, pid)
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == exit_code, stderr
        state = db.state()
        assert state["last_attempt"]["status"] == "interrupted"
        assert state["legacy_transition"]["first_attempt"] == state["last_attempt"]
        assert "last_success" not in state
        assert (
            db.sql("SELECT count(*) FROM pg_stat_activity WHERE query='SELECT pg_sleep(60)'") == "0"
        )
    finally:
        if process.poll() is None:
            db.command("kill", "-TERM", pid, check=False)
            process.communicate(timeout=15)


def test_real_worker_synthetic039_preservation_roundtrip(db):
    fences = db.legacy()
    db.command("mkdir", "/tmp/backups")
    db.command("sh", "-c", "printf preserved > /tmp/backups/old.dump")
    result = db.command(
        "env",
        "BACKUP_DIR=/tmp/backups",
        f"BACKUP_EXPECTED_SOURCE_ID={db.source}",
        "BACKUP_EXPECTED_SCHEMA_REVISION=039",
        f"BACKUP_LEGACY_EXPECTED_SHA256={fences['legacy_sha256']}",
        f"BACKUP_LEGACY_EXPECTED_UPDATED_AT={fences['legacy_updated_at']}",
        "sh",
        "/scripts/backup_db.sh",
        "--once-preserve-archives",
        "--transition-legacy",
    )
    assert result.returncode == 0
    state = db.state()
    assert state["last_attempt"]["status"] == "succeeded"
    assert state["last_attempt"]["retention_mode"] == "preserve"
    assert state["last_attempt"]["reason_code"] == ""
    assert state["legacy_transition"]["original"]["details_json"] == db.original[1]
    binding = state["last_success"]
    assert binding["schema_revision"] == "039"
    archive = "/tmp/backups/" + binding["archive_id"] + ".dump"
    assert db.command("sha256sum", archive).stdout.split()[0] == binding["sha256"]
    assert db.command("cat", "/tmp/backups/old.dump").stdout == "preserved"
    db.command("createdb", "isolated_restore")
    db.command("pg_restore", "--exit-on-error", "--no-owner", "-d", "isolated_restore", archive)
    restored = json.loads(
        db.command(
            "psql",
            "-XqAt",
            "-d",
            "isolated_restore",
            "-c",
            "SELECT details_json FROM service_heartbeats WHERE service='backup'",
        ).stdout
    )
    assert restored["last_attempt"]["status"] == "running"
    assert restored["legacy_transition"]["original"] == state["legacy_transition"]["original"]
    assert (
        db.command(
            "psql",
            "-XqAt",
            "-d",
            "isolated_restore",
            "-c",
            "SELECT version_num FROM alembic_version",
        ).stdout.strip()
        == "039"
    )
    # Authentic test-owned verification, only after inspecting this exact restore.
    db.operation(
        verification_start="true",
        attempt="d" * 32,
        archive=binding["archive_id"],
        checksum=binding["sha256"],
        started=binding["completed_at"],
    )
    db.operation(
        verification_finish="true",
        status="succeeded",
        succeeded="true",
        attempt="d" * 32,
        archive=binding["archive_id"],
        checksum=binding["sha256"],
        started=binding["completed_at"],
        finished=db.sql('SELECT to_char(now(),\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')'),
    )
    assert db.state()["last_verified"]["sha256"] == binding["sha256"]

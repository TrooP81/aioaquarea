"""Offline contract checks; executable shell coverage is in test_backup_worker_execution.

These checks intentionally do not pretend to execute PostgreSQL or a shell.
"""

from __future__ import annotations

from pathlib import Path
import re

from packages.core.backup_health import BACKUP_BOUNDS, BACKUP_DEFAULTS

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
WORKER = (SCRIPTS / "backup_db.sh").read_text(encoding="utf-8")
STATUS = (SCRIPTS / "backup_status.sql").read_text(encoding="utf-8")


def test_policy_defaults_and_sql_bounds_match_api():
    for key, value in BACKUP_DEFAULTS.items():
        assert f"${{{key.upper()}:={value}}}" in WORKER
    for letter, key in [
        ("i", "backup_interval_seconds"),
        ("r", "backup_retention_days"),
        ("f", "backup_max_age_seconds"),
        ("t", "backup_timeout_seconds"),
    ]:
        low, high = BACKUP_BOUNDS[key]
        assert f"{letter} BETWEEN {low} AND {high}" in WORKER
    assert "f >= i+t+120 AND r*86400 > i AND r*86400 >= f" in WORKER
    assert "COALESCE(NULLIF(s.value, ''), d.value)" in WORKER
    assert not re.search(r"\beval\b", WORKER)


def test_scheduler_rechecks_durable_success_under_lock_with_bounded_wake():
    assert WORKER.index("if flock -n 9") < WORKER.index(
        'if read_policy && capture due && [ "$captured" = t ]'
    )
    assert "make_interval(secs => :interval)" in WORKER
    assert "s->'last_success'->>'snapshot_at'" in WORKER
    assert "interval '5 minutes'" in WORKER
    assert "sleep 60" in WORKER
    assert 'sleep "$interval"' not in WORKER
    assert "s->'last_attempt'->>'finished_at'" in WORKER
    assert "--once" in WORKER


def test_retention_is_nonrecursive_and_count_protected_before_deletion():
    body = WORKER.split("retain() {", 1)[1].split("attempt_backup() {", 1)[0]
    deletion = body.index('rm "$BACKUP_DIR/$managed_id.dump"')
    for protection in (
        '[ "$count" -gt 2 ]',
        '"$success_id"|"$verified_id"|"$newest_id"',
        'if [ "$success_found" != true ] || [ "$verified_found" != true ]',
        '[ "$actual_sha" = "$managed_sha" ]',
        '[ ! -L "$candidate" ]',
        '[ ! -L "$candidate.sha256" ]',
    ):
        assert body.index(protection) < deletion
    assert "find " not in body
    assert "rm -r" not in body


def test_status_histories_fenced_and_source_parameters_not_interpolated_sql():
    assert "state->'last_attempt'->>'id' = :'attempt'" in STATUS
    assert "state->'last_verification'->>'id' = :'attempt'" in STATUS
    assert "state->'last_verification'->>'sha256' = :'checksum'" in STATUS
    assert "\\if :succeeded\nUPDATE backup_state" in STATUS
    assert "'last_success', jsonb_build_object" in STATUS
    assert "'last_verified', jsonb_build_object" in STATUS
    assert "'source_id', :'source'" in STATUS
    assert "updated_at = EXCLUDED.updated_at" in STATUS


def test_staging_selects_and_validates_before_unlock():
    stage = (SCRIPTS / "stage_backup.sh").read_text(encoding="utf-8")
    assert stage.index("timeout -k 2 30 flock 9") < stage.index("selection=$(")
    assert stage.index('[ "$actual" = "$checksum" ]') < stage.index('cp "$archive"')
    assert stage.index('cp "$archive"') < stage.index('[ "$staged" = "$checksum" ]')
    assert "flock -u" not in stage  # fd held through validation and process exit


def test_runtime_requires_kill_after_and_group_settlement_without_new_dependencies():
    dockerfile = (SCRIPTS.parent / "Dockerfile.backup").read_text(encoding="utf-8")
    assert "Alpine" in dockerfile and "TIMESCALE_IMAGE" in dockerfile
    assert "timeout --help 2>&1 | grep -q -- '-k' && command -v setsid" in dockerfile
    assert 'setsid timeout -k 2 "$owned_limit"' in WORKER
    assert 'kill -s TERM -- "-$child"' in WORKER
    assert 'kill -s KILL -- "-$child"' in WORKER
    assert "sleep 2 || true" in WORKER
    assert "setsid sleep 60 &" in WORKER
    stage = (SCRIPTS / "stage_backup.sh").read_text(encoding="utf-8")
    assert 'exec timeout -k 2 "$2" sh "$0" copy' in stage
    assert 'setsid sh "$0" bounded-copy "$seconds"' in stage
    assert 'kill -s KILL -- "-$group"' in stage
    assert "timeout -k 2 50 flock 8" in stage
    assert "trap '' TERM" in stage


def test_worker_cannot_hide_owned_tools_in_any_command_substitution():
    # Also excludes indirect SQL calls, backticks and nested captures; only
    # arithmetic expansion is allowed. Captured output is read by builtins.
    forbidden = re.compile(r"\$\((?!\()|`")
    assert forbidden.search(WORKER) is None
    assert "capture bounded" in WORKER
    assert 'work="$BACKUP_DIR/.work-$workspace_uuid"' in WORKER
    assert "IFS= read -r workspace_uuid < /proc/sys/kernel/random/uuid" in WORKER
    assert 'if bounded 5 mkdir "$work"; then' in WORKER
    assert 'work="$BACKUP_DIR/.work-$$"' not in WORKER
    assert "umask 077" in WORKER
    assert 'while [ "$workspace_tries" -lt 10 ]' in WORKER
    for regression in (
        "checksum=$(bounded 60 sha256sum archive)",
        "identity=$(source_identity)",
        "policy=`sql`",
        'value=$(printf "%s" "$(bounded 20 psql)")',
    ):
        assert forbidden.search(regression), regression
    assert forbidden.search("remaining=$((deadline_epoch-clock_epoch))") is None


def test_backup_shell_scripts_have_lf_bytes_and_explicit_git_attributes():
    import subprocess

    scripts = [
        SCRIPTS / name
        for name in (
            "backup_db.sh",
            "stage_backup.sh",
            "restore_db.sh",
            "restore_archive.sh",
            "verify_backup.sh",
            "verify_backup_isolated.sh",
        )
    ]
    for script in scripts:
        data = script.read_bytes()
        assert b"\r" not in data, f"CRLF/CR in executable shell script: {script.name}"
        assert data.startswith(b"#!/bin/sh\n")
    checkout = SCRIPTS.parents[1]
    result = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "check-attr",
            "text",
            "eol",
            "--",
            *(str(script.relative_to(checkout)).replace("\\", "/") for script in scripts),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 2 * len(scripts)
    assert all(
        line.endswith(": text: set") or line.endswith(": eol: lf")
        for line in result.stdout.splitlines()
    )

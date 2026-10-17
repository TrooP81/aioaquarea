"""Fail-closed backup verification target coverage."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_backup.sh"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell is required")
class TestVerifyBackupGuard:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"PGHOST": "db"},
            {"PGDATABASE": "heatpump"},
            {"BACKUP_VERIFY_DATABASE": "heatpump"},
            {"BACKUP_VERIFY_ISOLATED": "false"},
            {"BACKUP_VERIFY_ISOLATED": ""},
            {"BACKUP_LIVE_DATABASE": "custom_live", "PGDATABASE": "custom_live"},
            {"BACKUP_LIVE_DATABASE": "custom_live", "BACKUP_VERIFY_DATABASE": "custom_live"},
        ],
    )
    def test_BK_11A_refuses_live_targets_before_any_archive_or_database_command(
        self, tmp_path: Path, overrides: dict[str, str]
    ) -> None:
        command_log = tmp_path / "commands.log"
        for command in ("find", "openssl", "pg_restore", "dropdb", "createdb", "psql"):
            executable = tmp_path / command
            executable.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "{command}" >> "{command_log.as_posix()}"\nexit 99\n',
                encoding="utf-8",
            )
            executable.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            "PGHOST": "isolated-verifier",
            "PGDATABASE": "postgres",
            "BACKUP_LIVE_DATABASE": "heatpump",
            "BACKUP_VERIFY_DATABASE": "restore_verify_guard",
            "BACKUP_VERIFY_ISOLATED": "true",
            **overrides,
        }

        result = subprocess.run(
            ["sh", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        assert result.returncode == 2, result.stderr
        assert "live database verification is forbidden" in result.stderr
        assert not command_log.exists()

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        ("exit_code", "message", "status", "reason"),
        [
            (137, "", "verification_resource_failed", "container_oom"),
            (124, "", "verification_resource_failed", "timeout"),
            (143, "", "verification_resource_failed", "timeout"),
            (1, "No space left on device", "verification_resource_failed", "tmpfs_exhausted"),
            (1, "ERROR: out of memory", "verification_resource_failed", "postgres_memory"),
            (1, "pg_restore: error: invalid archive", "verification_failed", "restore_or_content"),
        ],
    )
    def test_BK_c_resource_failure_contract_retains_archive(
        self, tmp_path: Path, exit_code: int, message: str, status: str, reason: str
    ) -> None:
        backup_work_dir = tmp_path / "backup_guard"
        backup_work_dir.mkdir()
        archive = backup_work_dir / "heatpump-20261003T000000Z.dump"
        archive.write_bytes(b"unchanged archive")
        pg_restore = backup_work_dir / "pg_restore"
        pg_restore.write_text(
            f"""#!/bin/sh
case "$*" in
    *--list*) printf '; Dumped from database version: 16.13\\n1; 1 1 TABLE public plans heatpump\\n' ;;
    *--data-only*) for argument do case $argument in --file=*) : > "${{argument#--file=}}" ;; esac; done ;;
    *) printf '%s\\n' '{message}' >&2; exit {exit_code} ;;
esac
""",
            encoding="utf-8",
        )
        pg_restore.chmod(0o755)
        for command in ("psql", "createdb", "dropdb"):
            executable = backup_work_dir / command
            executable.write_text('#!/bin/sh\nprintf "t\\n"\n', encoding="utf-8")
            executable.chmod(0o755)
        result = subprocess.run(
            ["sh", str(SCRIPT), archive.as_posix()],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{backup_work_dir}{os.pathsep}{os.environ['PATH']}",
                "TMPDIR": backup_work_dir.as_posix(),
                "PGHOST": "restore-target",
                "PGDATABASE": "restore_production_test_unit",
                "PGUSER": "restore_test",
                "BACKUP_VERIFY_DATABASE": "isolated_verify",
                "BACKUP_VERIFY_ISOLATED": "true",
                "BACKUP_LIVE_DATABASE": "heatpump",
                "RESTORE_ISOLATED_PROJECT": "restore-db-production-test-unit",
                "RESTORE_SOURCE_IMAGE": "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57",
                "RESTORE_SOURCE_TIMESCALE_VERSION": "2.26.4",
                "RESTORE_EXPECTED_REVISION": "030",
            },
        )
        assert result.returncode == 1
        assert f"STATUS={status} REASON={reason}" in result.stderr
        assert archive.read_bytes() == b"unchanged archive"
        assert {path.name for path in backup_work_dir.iterdir()} == {
            archive.name,
            "pg_restore",
            "psql",
            "createdb",
            "dropdb",
        }

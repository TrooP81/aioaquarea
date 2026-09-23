"""Opt-in disposable Docker verification for the restore test mode."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_RESTORE_TESTS") != "1"
    or shutil.which("docker") is None
    or shutil.which("sh") is None,
    reason="set RUN_DOCKER_RESTORE_TESTS=1 with Docker and a POSIX shell to run restore integration",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = PROJECT_ROOT / "docker-compose.test.yml"
PRODUCTION_COMPOSE = PROJECT_ROOT / "docker-compose.restore-production.test.yml"
RESTORE_SCRIPT = PROJECT_ROOT / "scripts" / "restore_db.sh"


def _compose(project: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE), *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=check,
        text=True,
    )


def test_test_mode_restores_seeded_dump_without_touching_source(tmp_path: Path) -> None:
    project = f"restore-db-test-source-{uuid.uuid4().hex[:8]}"
    source_database = f"restore_test_source_{uuid.uuid4().hex[:8]}"
    restored_database = f"restore_test_restored_{uuid.uuid4().hex[:8]}"
    archive = tmp_path / "heatpump-20260918T000000Z.dump"
    try:
        _compose(project, "up", "-d", "--wait", "test-db")
        container = _compose(project, "ps", "-q", "test-db").stdout.strip()
        assert container
        seed = (
            "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL); "
            "INSERT INTO alembic_version VALUES ('006'); "
            "CREATE TABLE restore_sentinel (value text); "
            "INSERT INTO restore_sentinel VALUES ('seed-ok');"
        )
        result = _compose(
            project,
            "exec",
            "-T",
            "-e",
            "PGPASSWORD=heatpump_test",
            "test-db",
            "sh",
            "-ceu",
            f"createdb -U heatpump --maintenance-db=postgres {source_database}; "
            f'psql -U heatpump -d {source_database} -c "{seed}"; '
            f"pg_dump -U heatpump -d {source_database} --format=custom --file=/tmp/restore.dump",
        )
        assert result.returncode == 0, result.stderr
        subprocess.run(["docker", "cp", f"{container}:/tmp/restore.dump", str(archive)], check=True)

        result = subprocess.run(
            ["sh", str(RESTORE_SCRIPT), "--test", str(archive)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "RESTORE_TEST_PROJECT": project,
                "RESTORE_TEST_DATABASE": restored_database,
                "RESTORE_TEST_KEEP_STACK": "true",
            },
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Restored isolated test database" in result.stdout
        result = _compose(
            project,
            "exec",
            "-T",
            "-e",
            "PGPASSWORD=heatpump_test",
            "test-db",
            "psql",
            "-U",
            "heatpump",
            "-d",
            restored_database,
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT (SELECT value FROM restore_sentinel) || ':' || (SELECT version_num FROM alembic_version)",
        )
        assert result.stdout.strip() == "seed-ok:006"
        result = _compose(
            project,
            "exec",
            "-T",
            "-e",
            "PGPASSWORD=heatpump_test",
            "test-db",
            "psql",
            "-U",
            "heatpump",
            "-d",
            source_database,
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT value FROM restore_sentinel",
        )
        assert result.stdout.strip() == "seed-ok"
    finally:
        _compose(project, "down", "-v", check=False)


def test_test_mode_failure_stops_its_disposable_stack(tmp_path: Path) -> None:
    project = f"restore-db-test-failure-{uuid.uuid4().hex[:8]}"
    invalid_archive = tmp_path / "heatpump-20260918T000000Z.dump"
    invalid_archive.write_bytes(b"not a PostgreSQL archive")
    try:
        result = subprocess.run(
            ["sh", str(RESTORE_SCRIPT), "--test", str(invalid_archive)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            env={**os.environ, "RESTORE_TEST_PROJECT": project},
            text=True,
        )

        assert result.returncode != 0
        assert "heatpump_test" not in result.stdout + result.stderr
        assert (
            _compose(project, "ps", "--status", "running", "--format", "{{.Name}}").stdout.strip()
            == ""
        )
    finally:
        _compose(project, "down", "-v", check=False)


def test_production_mode_restores_only_to_disposable_compose_target(tmp_path: Path) -> None:
    project = f"restore-db-production-test-{uuid.uuid4().hex[:8]}"
    database = f"restore_production_test_{uuid.uuid4().hex[:8]}"
    archive_name = "heatpump-20260918T000000Z.dump"
    archive = tmp_path / archive_name

    def production_compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-p", project, "-f", str(PRODUCTION_COMPOSE), *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=check,
            text=True,
            env={
                **os.environ,
                "POSTGRES_DB": database,
                "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(tmp_path),
            },
        )

    environment = {
        **os.environ,
        "POSTGRES_DB": database,
        "POSTGRES_USER": "restore_test",
        "RESTORE_PRODUCTION_TEST_PROJECT": project,
        "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": PRODUCTION_COMPOSE.as_posix(),
        "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(tmp_path),
    }
    try:
        production_compose("up", "-d", "--wait", "db")
        seed = (
            "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL); "
            "INSERT INTO alembic_version VALUES ('027'); "
            "CREATE TABLE device_status (id integer); CREATE TABLE plans (id integer); "
            "CREATE TABLE restore_sentinel (value text); INSERT INTO restore_sentinel VALUES ('production-ok');"
        )
        result = production_compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-ceu",
            f'psql -U restore_test -d {database} -c "{seed}"; '
            f"pg_dump -U restore_test -d {database} --format=custom --file=/tmp/restore.dump",
        )
        assert result.returncode == 0, result.stderr
        container = production_compose("ps", "-q", "db").stdout.strip()
        assert container
        subprocess.run(["docker", "cp", f"{container}:/tmp/restore.dump", str(archive)], check=True)

        result = subprocess.run(
            ["sh", str(RESTORE_SCRIPT), "--production", archive_name],
            cwd=PROJECT_ROOT,
            input=f"RESTORE {archive_name}\n",
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Restore completed" in result.stdout
        result = production_compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "restore_test",
            "-d",
            database,
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT (SELECT value FROM restore_sentinel) || ':' || (SELECT version_num FROM alembic_version)",
        )
        assert result.stdout.strip() == "production-ok:027"
    finally:
        production_compose("down", "-v", check=False)

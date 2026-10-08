"""Pinned Timescale restore drills. No host ports, live network, or persistent data."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_RESTORE_TESTS") != "1"
    or shutil.which("docker") is None
    or shutil.which("sh") is None,
    reason="set RUN_DOCKER_RESTORE_TESTS=1 with Docker and a POSIX shell to run restore integration",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = PROJECT_ROOT / "docker-compose.restore-production.test.yml"
RESTORE_SCRIPT = PROJECT_ROOT / "scripts" / "restore_db.sh"
PINNED_IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)
GIB = 1024**3


class RestoreStack:
    def __init__(self, backup_dir: Path) -> None:
        self.project = f"restore-db-production-test-{uuid.uuid4().hex[:10]}"
        self.database = f"restore_production_test_{uuid.uuid4().hex[:8]}"
        self.backup_dir = backup_dir
        self.tmpfs_bytes = int(os.environ.get("RESTORE_VERIFY_TMPFS_BYTES", str(2 * GIB)))
        self.environment = {
            **os.environ,
            "POSTGRES_DB": self.database,
            "POSTGRES_USER": "restore_test",
            "RESTORE_PRODUCTION_TEST_PROJECT": self.project,
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": COMPOSE.as_posix(),
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": backup_dir.as_posix(),
            "RESTORE_SOURCE_IMAGE": PINNED_IMAGE,
            "RESTORE_SOURCE_TIMESCALE_VERSION": "2.26.4",
            "RESTORE_EXPECTED_REVISION": "030",
            "RESTORE_REQUIRED_ROLES": "heatpump",
            "RESTORE_DISPOSABLE": "true",
            "RESTORE_ISOLATED_PROJECT": self.project,
            "RESTORE_VERIFY_TMPFS_BYTES": str(self.tmpfs_bytes),
            "RESTORE_VERIFY_MEMORY_BYTES": str(self.tmpfs_bytes + 2 * GIB),
            "MSYS2_ARG_CONV_EXCL": "/scripts/;/backups/",
        }

    def compose(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(COMPOSE), *arguments],
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=3600,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def sql(self, database: str, statement: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-U",
            "restore_test",
            "-d",
            database,
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            statement,
        ).stdout.strip()

    def helper(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "RESTORE_SOURCE_IMAGE",
            "-e",
            "RESTORE_SOURCE_TIMESCALE_VERSION",
            "-e",
            "RESTORE_EXPECTED_REVISION",
            "-e",
            "RESTORE_REQUIRED_ROLES",
            "-e",
            "RESTORE_ISOLATED_PROJECT",
            "restore-tool",
            "sh",
            "/scripts/restore_archive.sh",
            *arguments,
            check=check,
        )

    def entry(
        self, mode: str, archive: Path, *flags: str, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "sh",
                str(RESTORE_SCRIPT),
                mode,
                *flags,
                archive.name if mode == "--production" else archive.as_posix(),
            ],
            cwd=PROJECT_ROOT,
            env={**self.environment, **overrides},
            input=f"RESTORE {archive.name}\n",
            capture_output=True,
            check=False,
            text=True,
            timeout=3600,
        )


@contextmanager
def _isolated_stack(backup_dir: Path) -> Iterator[RestoreStack]:
    stack = RestoreStack(backup_dir)
    capacity = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(capacity.stdout.strip()) >= stack.tmpfs_bytes + 3 * GIB, (
        "STATUS=verification_resource_failed reason=host_memory_guard"
    )
    try:
        config = json.loads(stack.compose("config", "--format", "json").stdout)
        database_config = config["services"]["db"]
        assert int(database_config["mem_limit"]) == stack.tmpfs_bytes + 2 * GIB
        assert str(stack.tmpfs_bytes) in database_config["tmpfs"][0]
        assert database_config.get("ports", []) == []
        assert config["networks"]["default"]["internal"] is True
        assert all(volume["read_only"] for volume in config["services"]["restore-tool"]["volumes"])
        stack.compose("up", "-d", "--wait", "db")
        assert stack.sql("postgres", "SHOW server_version") == "16.13"
        assert (
            stack.sql(
                stack.database, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
            )
            == "2.26.4"
        )
        assert stack.sql(stack.database, "SHOW shared_buffers") == "256MB"
        assert stack.sql(stack.database, "SHOW maintenance_work_mem") == "512MB"
        assert stack.sql(stack.database, "SHOW work_mem") == "16MB"
        assert stack.sql(stack.database, "SHOW max_connections") == "20"
        yield stack
    finally:
        stack.compose("down", "--volumes", "--remove-orphans")
        remaining = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={stack.project}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert remaining.stdout.strip() == "", "isolated project containers were not cleaned up"
        volumes = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={stack.project}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert volumes.stdout.strip() == "", "isolated project volumes were not cleaned up"


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[RestoreStack]:
    with _isolated_stack(tmp_path) as disposable:
        yield disposable


def _seed(stack: RestoreStack, database: str, sentinel: str) -> None:
    stack.sql(
        "postgres",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heatpump') THEN CREATE ROLE heatpump LOGIN; END IF; END $$",
    )
    stack.sql(database, "CREATE EXTENSION IF NOT EXISTS timescaledb VERSION '2.26.4'")
    stack.sql(
        database,
        f"""
        SET ROLE heatpump;
        CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('030');
        CREATE TABLE plans (id integer PRIMARY KEY);
        INSERT INTO plans VALUES (1);
        CREATE TABLE restore_sentinel (value text);
        INSERT INTO restore_sentinel VALUES ('{sentinel}');
        CREATE TABLE restore_probe (observed_at timestamptz NOT NULL, database_name text NOT NULL);
        CREATE PROCEDURE restore_probe_job(job_id integer, config jsonb)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $probe$
        BEGIN
            INSERT INTO public.restore_probe VALUES (clock_timestamp(), current_database());
        END;
        $probe$;
    """,
    )
    for table in ("device_status", "consumption", "prices", "weather"):
        stack.sql(
            database,
            f"""
            SET ROLE heatpump;
            CREATE TABLE {table} (time timestamptz PRIMARY KEY, value double precision);
            SELECT create_hypertable('{table}', 'time', chunk_time_interval => INTERVAL '1 day');
            INSERT INTO {table} VALUES (clock_timestamp(), 1);
        """,
        )
    stack.sql(
        database,
        "SET ROLE heatpump; SELECT add_retention_policy('device_status', INTERVAL '30 days'); RESET ROLE; SELECT add_job('restore_probe_job', INTERVAL '2 seconds', initial_start => clock_timestamp() + INTERVAL '2 seconds')",
    )


@pytest.fixture
def synthetic_archive(stack: RestoreStack) -> Path:
    stack.sql("postgres", "CREATE DATABASE restore_source OWNER restore_test")
    stack.sql("postgres", "CREATE ROLE heatpump LOGIN")
    stack.sql("postgres", "ALTER DATABASE restore_source OWNER TO heatpump")
    _seed(stack, "restore_source", "archive")
    stack.compose(
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "restore_test",
        "-d",
        "restore_source",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file=/tmp/synthetic.dump",
    )
    archive = stack.backup_dir / "heatpump-20261003T000000Z.dump"
    container = stack.compose("ps", "-q", "db").stdout.strip()
    subprocess.run(
        ["docker", "cp", f"{container}:/tmp/synthetic.dump", str(archive)],
        check=True,
        capture_output=True,
    )
    stack.sql("postgres", "DROP DATABASE restore_source WITH (FORCE)")
    stack.sql("postgres", "DROP ROLE heatpump")
    return archive


def _assert_restored(stack: RestoreStack, database: str) -> None:
    assert stack.sql(database, "SELECT version_num FROM alembic_version") == "030"
    assert stack.sql(database, "SELECT value FROM restore_sentinel") == "archive"
    assert stack.sql(database, "SHOW timescaledb.restoring") == "off"
    assert stack.sql(database, "SELECT count(*) FROM timescaledb_information.hypertables") == "4"
    assert (
        stack.sql(
            database,
            "SELECT hypertable_name || ':' || count(*) FROM timescaledb_information.chunks GROUP BY hypertable_name ORDER BY hypertable_name",
        )
        == "consumption:1\ndevice_status:1\nprices:1\nweather:1"
    )
    assert (
        stack.sql(
            database,
            "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname IN ('device_status', 'consumption', 'prices', 'weather') AND contype = 'p' AND convalidated",
        )
        == "4"
    )
    assert (
        stack.sql(
            database,
            "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention' AND scheduled AND owner::regrole::text = 'heatpump'",
        )
        == "1"
    )
    assert (
        stack.sql(
            database,
            "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = 'restore_probe_job' AND scheduled AND owner::regrole::text = 'restore_test'",
        )
        == "1"
    )
    assert (
        stack.sql("postgres", "SELECT NOT rolcanlogin FROM pg_roles WHERE rolname = 'heatpump'")
        == "t"
    )


def test_BK_4_identical_timescale_archive_fails_without_role_and_passes_with_nologin(
    stack: RestoreStack, synthetic_archive: Path
) -> None:
    stack.sql("postgres", "CREATE DATABASE missing_role")
    negative = stack.compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "restore-tool",
        "sh",
        "-ceu",
        "psql -X -v ON_ERROR_STOP=1 -d missing_role -c \"CREATE EXTENSION IF NOT EXISTS timescaledb VERSION '2.26.4'\"; "
        'psql -X -v ON_ERROR_STOP=1 -d missing_role -c "SELECT timescaledb_pre_restore()"; '
        'trap \'status=$?; psql -X -v ON_ERROR_STOP=1 -d missing_role -c "SELECT timescaledb_post_restore()"; exit "$status"\' EXIT; '
        f"pg_restore --exit-on-error --no-owner --no-privileges --dbname=missing_role /backups/{synthetic_archive.name}",
        check=False,
    )
    assert negative.returncode != 0
    assert "bgw_job" in negative.stderr
    assert 'role "heatpump" does not exist' in negative.stderr
    assert (
        stack.sql("postgres", "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heatpump')")
        == "f"
    )
    positive = stack.helper("restore", f"/backups/{synthetic_archive.name}", "restored_candidate")
    assert "ERROR:" not in positive.stderr
    assert "pg_restore: error:" not in positive.stderr
    _assert_restored(stack, "restored_candidate")
    print(
        "BK-3/BK-4/BK-5: same unfiltered archive failed on bgw_job without heatpump and passed with heatpump NOLOGIN"
    )


def test_BK_7_failed_candidate_leaves_original_unchanged(
    stack: RestoreStack, synthetic_archive: Path
) -> None:
    stack.sql(
        stack.database,
        "CREATE TABLE restore_sentinel (value text); INSERT INTO restore_sentinel VALUES ('original')",
    )
    result = stack.entry("--production", synthetic_archive, RESTORE_EXPECTED_REVISION="999")
    assert result.returncode != 0
    assert "revision does not match" in result.stderr
    assert stack.sql(stack.database, "SELECT value FROM restore_sentinel") == "original"
    assert (
        stack.sql(
            "postgres",
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'heatpump_restore_%' OR datname LIKE 'heatpump_old_%'",
        )
        == "0"
    )
    assert stack.sql(stack.database, "SHOW timescaledb.restoring") == "off"


def test_BK_7A_BK_7B_jobs_execute_after_rename_and_old_database_stays_suppressed(
    stack: RestoreStack, synthetic_archive: Path
) -> None:
    stack.sql("postgres", "CREATE ROLE heatpump LOGIN")
    stack.sql(stack.database, "GRANT CREATE ON SCHEMA public TO heatpump")
    _seed(stack, stack.database, "original")
    assert (
        stack.sql(
            stack.database,
            "SELECT to_regprocedure('_timescaledb_functions.stop_background_workers()') IS NOT NULL AND to_regprocedure('_timescaledb_functions.start_background_workers()') IS NOT NULL",
        )
        == "t"
    )
    deadline = time.monotonic() + 60
    while stack.sql(stack.database, "SELECT count(*) > 0 FROM restore_probe") != "t":
        assert time.monotonic() < deadline, "original scheduled probe job did not execute"
        time.sleep(0.25)
    result = stack.entry("--production", synthetic_archive, "--allow-live-restore")
    assert result.returncode == 0, result.stdout + result.stderr
    cutover_time = next(
        line.removeprefix("CUTOVER_TIME=")
        for line in result.stdout.splitlines()
        if line.startswith("CUTOVER_TIME=")
    )
    old_database = next(
        line.removeprefix("RETAINED_DATABASE=")
        for line in result.stdout.splitlines()
        if line.startswith("RETAINED_DATABASE=")
    )
    assert stack.sql(stack.database, "SELECT value FROM restore_sentinel") == "archive"
    assert stack.sql(old_database, "SELECT value FROM restore_sentinel") == "original"
    assert stack.sql(stack.database, "SHOW timescaledb.restoring") == "off"
    assert stack.sql(old_database, "SHOW timescaledb.restoring") == "on"
    stats_query = "SELECT row_to_json(stats) FROM (SELECT s.last_successful_finish, s.total_runs, (SELECT count(*) FROM restore_probe) AS probes FROM timescaledb_information.job_stats s JOIN timescaledb_information.jobs j USING (job_id) WHERE j.proc_name = 'restore_probe_job') stats"
    stopped_stats = stack.sql(old_database, stats_query)
    assert json.loads(stopped_stats)["probes"] > 0
    deadline = time.monotonic() + 60
    success_query = f"SELECT s.last_run_status = 'Success' AND s.last_successful_finish > '{cutover_time}'::timestamptz AND EXISTS (SELECT 1 FROM restore_probe WHERE observed_at > '{cutover_time}'::timestamptz AND database_name = current_database()) FROM timescaledb_information.job_stats s JOIN timescaledb_information.jobs j USING (job_id) WHERE j.proc_name = 'restore_probe_job'"
    while stack.sql(stack.database, success_query) != "t":
        assert time.monotonic() < deadline, (
            "BK-7A: renamed live job did not successfully execute after cutover"
        )
        time.sleep(0.25)
    live_stats = stack.sql(stack.database, stats_query)
    observation_start = time.monotonic()
    while time.monotonic() - observation_start < 4.1:
        assert stack.sql(old_database, stats_query) == stopped_stats, (
            "BK-7B: retained old job advanced"
        )
        assert stack.sql(old_database, "SHOW timescaledb.restoring") == "on"
        assert (
            stack.sql(
                "postgres",
                f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{old_database}' AND backend_type ILIKE '%worker%'",
            )
            == "0"
        )
        time.sleep(0.25)
    print(f"BK-7A PASS cutover={cutover_time} new_live={live_stats}")
    print(
        f"BK-7B PASS observed_seconds={time.monotonic() - observation_start:.2f} retained_old={stopped_stats}"
    )
    rollback = stack.helper("rollback", stack.database, old_database, "restored_failed")
    assert rollback.returncode == 0
    assert stack.sql(stack.database, "SELECT value FROM restore_sentinel") == "original"
    assert stack.sql(stack.database, "SHOW timescaledb.restoring") == "off"
    assert stack.sql("restored_failed", "SHOW timescaledb.restoring") == "on"


def test_test_mode_restores_synthetic_archive_and_preserves_source(
    stack: RestoreStack, synthetic_archive: Path
) -> None:
    stack.sql(
        stack.database,
        "CREATE TABLE restore_sentinel (value text); INSERT INTO restore_sentinel VALUES ('untouched')",
    )
    result = stack.entry(
        "--test",
        synthetic_archive,
        RESTORE_TEST_PROJECT=stack.project,
        RESTORE_TEST_DATABASE="restore_production_test_restored",
        RESTORE_TEST_KEEP_STACK="true",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_restored(stack, "restore_production_test_restored")
    assert stack.sql(stack.database, "SELECT value FROM restore_sentinel") == "untouched"


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_BACKUP_RESTORE_TESTS") != "1",
    reason="real backups run only on an explicitly authorized trusted host",
)
def test_BK_13_latest_real_backup_restores_without_filters_or_errors() -> None:
    backup_dir = Path(
        os.environ.get("RESTORE_REAL_BACKUP_DIR", str(PROJECT_ROOT / "backups"))
    ).resolve()
    archives = sorted(
        path
        for path in backup_dir.glob("heatpump-*.dump")
        if path.is_file() and not path.is_symlink()
    )
    assert archives, "no completed real backup archive"
    archive = archives[-1]
    original_stat = archive.stat()
    with _isolated_stack(backup_dir) as disposable:
        disposable.environment["RESTORE_EXPECTED_REVISION"] = os.environ.get(
            "RESTORE_REAL_EXPECTED_REVISION", "030"
        )
        result = disposable.helper("restore", f"/backups/{archive.name}", "real_backup_candidate")
        assert "ERROR:" not in result.stderr
        assert "pg_restore: error:" not in result.stderr
        assert disposable.sql("real_backup_candidate", "SHOW timescaledb.restoring") == "off"
        assert (
            disposable.sql(
                "real_backup_candidate", "SELECT count(*) FROM timescaledb_information.hypertables"
            )
            == "4"
        )
        assert archive.stat().st_size == original_stat.st_size
        assert archive.stat().st_mtime_ns == original_stat.st_mtime_ns
        print(
            f"BK-13 PASS archive={archive.name} bytes={original_stat.st_size} PostgreSQL=16.13 TimescaleDB=2.26.4 zero_restore_errors"
        )
        print(result.stdout)

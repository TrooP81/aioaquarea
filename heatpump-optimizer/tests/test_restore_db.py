"""Unit coverage for the guarded database restore entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_db.sh"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell is required")
class TestRestoreScript:
    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_rejects_unknown_mode_without_calling_docker(self) -> None:
        result = subprocess.run(
            ["sh", str(SCRIPT), "--unsafe", "anything.dump"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_requires_completed_dump_name_before_docker(self, tmp_path: Path) -> None:
        partial = tmp_path / "heatpump-20260918T000000Z.dump.partial"
        partial.write_bytes(b"not a dump")
        result = subprocess.run(
            ["sh", str(SCRIPT), "--test", str(partial)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "completed heatpump-*.dump" in result.stderr

    def test_production_rejects_linked_archive_before_docker(self, tmp_path: Path) -> None:
        backups = SCRIPT.parents[1] / "backups"
        backups.mkdir(exist_ok=True)
        archive_name = "heatpump-20990101T000000Z.dump"
        archive = backups / archive_name
        outside_archive = tmp_path / "outside.dump"
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        outside_archive.write_bytes(b"not a dump")
        try:
            archive.symlink_to(outside_archive)
        except OSError as error:
            pytest.skip(f"symbolic links are unavailable: {error}")
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
        docker.chmod(0o755)
        try:
            result = subprocess.run(
                ["sh", str(SCRIPT), "--production", archive_name],
                cwd=SCRIPT.parents[1],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
            )
        finally:
            archive.unlink(missing_ok=True)

        assert result.returncode == 2
        assert "must not be a symbolic link" in result.stderr
        assert not docker_log.exists()

    @pytest.mark.parametrize(
        "archive_name",
        [
            "../heatpump-20990101T000000Z.dump",
            "nested/heatpump-20990101T000000Z.dump",
            "nested/../heatpump-20990101T000000Z.dump",
        ],
    )
    def test_production_rejects_archive_path_traversal_before_docker(
        self, tmp_path: Path, archive_name: str
    ) -> None:
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
        docker.chmod(0o755)

        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", archive_name],
            cwd=SCRIPT.parents[1],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        )

        assert result.returncode == 2
        assert "must not contain a path separator" in result.stderr
        assert not docker_log.exists()

    @pytest.mark.parametrize("database", ["", "postgres", "template0", "template1", "bad-name"])
    def test_production_rejects_unsafe_database_before_destructive_commands(
        self, tmp_path: Path, database: str
    ) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        production_body = script.split("run_production_restore() {", 1)[1].split(
            "run_test_restore() {", 1
        )[0]
        restore_command = production_body.rsplit("sh -ceu '", 1)[1].split("\n        '", 1)[0]
        commands_log = tmp_path / "commands.log"
        command_dir = tmp_path / "bin"
        command_dir.mkdir()
        for command in ("psql", "dropdb", "createdb", "pg_restore"):
            executable = command_dir / command
            executable.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "{command}" >> "{commands_log}"\n', encoding="utf-8"
            )
            executable.chmod(0o755)

        result = subprocess.run(
            ["sh", "-ceu", restore_command],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{command_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PGDATABASE": database,
            },
        )

        assert result.returncode == 2
        assert "Refusing unsafe restore database name" in result.stderr
        assert not commands_log.exists()

    def test_production_mode_requires_confirmation_and_blocks_active_writers(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        production_body = script.split("run_production_restore() {", 1)[1].split(
            "run_test_restore() {", 1
        )[0]

        assert (
            "printf 'Type RESTORE %s to replace the local database: ' \"$archive_name\""
            in production_body
        )
        assert '[ "$confirmation" = "RESTORE $archive_name" ]' in production_body
        assert "running_writers=$(production_compose" in production_body
        assert "Stop writers before restoring" in production_body
        assert '[ ! -L "$archive" ]' in script
        assert '""|postgres|template0|template1)' in production_body
        assert "[0-9]*|*[!A-Za-z0-9_]*" in production_body
        assert "PGPASSWORD" not in production_body

    def test_production_mode_waits_for_database_before_backup_container_commands(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        production_body = script.split("run_production_restore() {", 1)[1].split(
            "run_test_restore() {", 1
        )[0]

        assert "production_compose up -d --wait db" in production_body
        assert (
            'production_compose exec -T db pg_isready -U "${POSTGRES_USER:-heatpump}"'
            in production_body
        )
        assert '-d "${POSTGRES_DB:-heatpump}"' in production_body
        assert '[ "$readiness_attempt" -lt 20 ]' in production_body
        assert "Timed out waiting for database readiness before restore" in production_body
        assert production_body.index("up -d --wait db") < production_body.index("backup \\")

    @pytest.mark.parametrize(
        ("variable", "value", "message"),
        [
            (
                "RESTORE_PRODUCTION_TEST_PROJECT",
                "default",
                "must start with restore-db-production-test-",
            ),
            (
                "RESTORE_PRODUCTION_TEST_COMPOSE_FILE",
                "docker-compose.yml",
                "must name the canonical disposable compose file",
            ),
            ("POSTGRES_DB", "heatpump", "must start with restore_production_test_"),
            (
                "RESTORE_PRODUCTION_TEST_BACKUP_DIR",
                "",
                "must be an external disposable directory",
            ),
        ],
    )
    def test_production_test_mode_rejects_default_targets_before_docker(
        self, tmp_path: Path, variable: str, value: str, message: str
    ) -> None:
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
        docker.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-guards",
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": str(
                SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
            ),
            "POSTGRES_DB": "restore_production_test_guards",
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(tmp_path),
        }
        environment[variable] = value

        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", "heatpump-20260918T000000Z.dump"],
            cwd=SCRIPT.parents[1],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        assert result.returncode == 2
        assert message in result.stderr
        assert not docker_log.exists()

    @pytest.mark.parametrize("alias_kind", ["dot", "parent", "child", "symlink"])
    def test_production_test_mode_rejects_canonical_backups_aliases_before_docker(
        self, tmp_path: Path, alias_kind: str
    ) -> None:
        backups = SCRIPT.parents[1] / "backups"
        backups.mkdir(exist_ok=True)
        child = backups / "restore-db-canonical-alias-test"
        alias = child
        if alias_kind == "dot":
            alias = backups / "."
        elif alias_kind == "parent":
            alias = backups / ".." / "backups"
        elif alias_kind == "child":
            child.mkdir(exist_ok=True)
        else:
            alias = tmp_path / "backups-alias"
            try:
                alias.symlink_to(backups, target_is_directory=True)
            except OSError as error:
                pytest.skip(f"symbolic links are unavailable: {error}")

        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
        docker.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-alias",
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": str(
                SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
            ),
            "POSTGRES_DB": "restore_production_test_alias",
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(alias),
        }
        try:
            result = subprocess.run(
                ["sh", str(SCRIPT), "--production", "heatpump-20260918T000000Z.dump"],
                cwd=SCRIPT.parents[1],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        finally:
            if alias_kind == "child":
                child.rmdir()
            elif alias_kind == "symlink":
                alias.unlink(missing_ok=True)

        assert result.returncode == 2
        assert "must be an external disposable directory" in result.stderr
        assert not docker_log.exists()

    def test_production_test_mode_uses_canonical_external_backup_directory(
        self, tmp_path: Path
    ) -> None:
        external = tmp_path / "external"
        external.mkdir()
        archive_name = "heatpump-20260918T000000Z.dump"
        (external / archive_name).write_bytes(b"archive")
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" >> "{docker_log}"\n',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-canonical",
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": str(
                SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
            ),
            "POSTGRES_DB": "restore_production_test_canonical",
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(external / "."),
        }

        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", archive_name],
            cwd=SCRIPT.parents[1],
            input="WRONG\n",
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        assert result.returncode == 1
        assert "Confirmation did not match" in result.stderr
        canonical_external = subprocess.run(
            ["sh", "-c", 'CDPATH= cd -- "$1" && pwd -P', "sh", str(external)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        assert docker_log.read_text(encoding="utf-8").splitlines() == [canonical_external]

    def test_production_wrong_confirmation_stops_before_writer_check(self, tmp_path: Path) -> None:
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
        docker.chmod(0o755)
        archive = tmp_path / "heatpump-20260918T000000Z.dump"
        archive.write_bytes(b"archive")
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-confirm",
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": str(
                SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
            ),
            "POSTGRES_DB": "restore_production_test_confirm",
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(tmp_path),
        }

        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", archive.name],
            cwd=SCRIPT.parents[1],
            input="WRONG\n",
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        assert result.returncode == 1
        assert "Confirmation did not match" in result.stderr
        calls = docker_log.read_text(encoding="utf-8").splitlines()
        assert len(calls) == 1
        assert "backup-verify" in calls[0]
        assert all(" ps " not in call and " up " not in call for call in calls)

    def test_production_writer_check_runs_before_database_start(self, tmp_path: Path) -> None:
        docker_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(
            f'''#!/bin/sh
printf "%s\\n" "$*" >> "{docker_log}"
case " $* " in
    *" ps --services --status running "*) printf 'api\\n' ;;
esac
''',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        archive = tmp_path / "heatpump-20260918T000000Z.dump"
        archive.write_bytes(b"archive")
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-writer",
            "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": str(
                SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
            ),
            "POSTGRES_DB": "restore_production_test_writer",
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": str(tmp_path),
        }

        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", archive.name],
            cwd=SCRIPT.parents[1],
            input=f"RESTORE {archive.name}\n",
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        assert result.returncode == 1
        assert "Stop writers before restoring: api" in result.stderr
        calls = docker_log.read_text(encoding="utf-8").splitlines()
        assert any("backup-verify" in call for call in calls)
        assert any("ps --services --status running" in call for call in calls)
        assert any(" stop " in f" {call} " for call in calls)
        assert not any("up -d --wait db" in call for call in calls)

    def test_production_restore_command_order_is_destructive_only_after_guards(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        production_body = script.split("run_production_restore() {", 1)[1].split(
            "run_test_restore() {", 1
        )[0]

        assert production_body.index("backup-verify") < production_body.index(
            "IFS= read -r confirmation"
        )
        assert production_body.index("RESTORE $archive_name") < production_body.index(
            "running_writers=$(production_compose"
        )
        destructive_start = production_body.index("dropdb")
        assert production_body.index("up -d --wait db") < destructive_start
        assert production_body.index("dropdb") < production_body.index(
            "createdb", destructive_start
        )
        assert production_body.index("createdb", destructive_start) < production_body.index(
            "pg_restore", destructive_start
        )
        assert production_body.index("pg_restore", destructive_start) < production_body.index(
            "alembic current"
        )

    def test_production_test_mode_can_only_target_disposable_resources(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")

        assert (
            'PRODUCTION_TEST_COMPOSE_FILE="$PROJECT_DIR/docker-compose.restore-production.test.yml"'
            in script
        )
        assert "restore-db-production-test-*" in script
        assert "restore_production_test_*" in script
        assert 'project_backups_dir=$(CDPATH= cd -- "$PROJECT_DIR/backups" && pwd -P)' in script
        assert (
            'production_test_backup_dir=$(CDPATH= cd -- "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" && pwd -P)'
            in script
        )
        assert "RESTORE_PRODUCTION_TEST_BACKUP_DIR=$production_test_backup_dir" in script
        assert (
            'production_compose run --rm --no-deps -e RESTORE_ARCHIVE="$archive_name" backup'
            in script
        )

    def test_test_mode_isolated_restore_contract_is_explicit(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        test_body = script.split("run_test_restore() {", 1)[1]

        assert '"$TEST_COMPOSE_FILE"' in test_body
        assert "test_project=${RESTORE_TEST_PROJECT:-restore-db-test-$$}" in test_body
        assert "test_database=${RESTORE_TEST_DATABASE:-restore_test_$$}" in test_body
        assert "up -d --wait test-db" in test_body
        assert 'docker cp "$archive" "$container:/tmp/restore.dump"' in test_body
        assert "-e PGPASSWORD=heatpump_test" in test_body
        assert "pg_isready -U heatpump -d heatpump_test" in test_body
        assert '[ "$readiness_attempt" -lt 20 ]' in test_body
        assert "Timed out waiting for isolated test database readiness" in test_body
        assert "-U heatpump" in test_body
        assert "--maintenance-db=postgres" in test_body
        assert '"$COMPOSE_FILE"' not in test_body
        assert "set -x" not in script

        database_option = test_body.index('-e RESTORE_TEST_DATABASE="$test_database"')
        service_name = test_body.index("test-db sh")
        assert database_option < service_name
        password_option = test_body.rindex("-e PGPASSWORD=heatpump_test")
        assert password_option < service_name

"""Unit coverage for the guarded database restore entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_db.sh"
ARCHIVE_SCRIPT = SCRIPT.with_name("restore_archive.sh")
PINNED_IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)


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
        archive_name = "heatpump-20990101T000000Z.dump"
        archive = tmp_path / archive_name
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
                env={
                    **os.environ,
                    "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                    "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-links",
                    "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": (
                        SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
                    ).as_posix(),
                    "RESTORE_PRODUCTION_TEST_BACKUP_DIR": tmp_path.as_posix(),
                    "POSTGRES_DB": "restore_production_test_links",
                },
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
            ["sh", str(SCRIPT), "--production", "--allow-live-restore", archive_name],
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
            ["sh", str(ARCHIVE_SCRIPT), "restore", "missing.dump", database],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{command_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PGDATABASE": database,
                "PGHOST": "db",
                "PGUSER": "heatpump",
                "RESTORE_ALLOW_LIVE_RESTORE": "true",
                "RESTORE_SOURCE_IMAGE": PINNED_IMAGE,
                "RESTORE_SOURCE_TIMESCALE_VERSION": "2.26.4",
            },
        )

        assert result.returncode == 2
        assert "unsafe" in result.stderr
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
        assert "writer_services=$(production_compose" in production_body
        assert "Stop writers before restoring" in production_body
        assert '[ ! -L "$archive" ]' in script
        assert '""|postgres|template0|template1|[0-9]*|*[!A-Za-z0-9_]*' in script
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
        assert '-d "$live_database"' in production_body
        assert production_body.index("up -d --wait db") < production_body.index(
            "restore_tool restore"
        )

    def test_AC10_1_restore_requires_panasonic_credentials_without_printing_values(self) -> None:
        script = ARCHIVE_SCRIPT.read_text(encoding="utf-8")

        assert "aquarea_username" in script
        assert "aquarea_password" in script
        assert "Restored database has no configured Panasonic credentials" in script

    def test_AC10_1_missing_credentials_stop_services_without_printing_values(
        self, tmp_path: Path
    ) -> None:
        command_log = tmp_path / "commands.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql = bin_dir / "psql"
        psql.write_text(
            f'''#!/bin/sh
printf '%s\\n' "$*" >> "{command_log.as_posix()}"
case "$*" in
    *aquarea_username*) printf 'f\\n' ;;
    *) printf 't\\n' ;;
esac
''',
            encoding="utf-8",
        )
        psql.chmod(0o755)
        secret = "never-print-this-panasonic-secret"
        result = subprocess.run(
            ["sh", str(ARCHIVE_SCRIPT), "verify", "candidate"],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PGHOST": "db",
                "PGUSER": "heatpump",
                "RESTORE_ALLOW_LIVE_RESTORE": "true",
                "RESTORE_DISPOSABLE": "false",
                "RESTORE_SOURCE_IMAGE": PINNED_IMAGE,
                "RESTORE_SOURCE_TIMESCALE_VERSION": "2.26.4",
                "RESTORE_EXPECTED_REVISION": "030",
                "AQUAREA_USERNAME": secret,
                "AQUAREA_PASSWORD": secret,
            },
        )

        assert result.returncode != 0
        logged_commands = command_log.read_text(encoding="utf-8")
        assert "Restored database has no configured Panasonic credentials" in result.stderr
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in logged_commands

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
        child = backups / "restore-db-canonical-alias-test"
        alias = child
        if alias_kind == "dot":
            alias = backups / "."
        elif alias_kind == "parent":
            alias = backups / ".." / "backups"
        elif alias_kind == "symlink":
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
            "RESTORE_PRODUCTION_TEST_BACKUP_DIR": alias.as_posix(),
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
            if alias_kind == "symlink":
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
        assert "restore-tool" in calls[0]
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
        assert any("restore-tool" in call for call in calls)
        assert any("ps --services --status running" in call for call in calls)
        assert any(" stop " in f" {call} " for call in calls)
        assert not any("up -d --wait db" in call for call in calls)

    def test_production_candidate_and_cutover_order_is_guarded(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        production_body = script.split("run_production_restore() {", 1)[1].split(
            "run_test_restore() {", 1
        )[0]

        assert production_body.index("restore-tool") < production_body.index(
            "IFS= read -r confirmation"
        )
        assert production_body.index("RESTORE $archive_name") < production_body.index(
            "writer_services=$(production_compose"
        )
        assert production_body.index("up -d --wait db") < production_body.index(
            "restore_tool restore"
        )
        assert (
            production_body.index("restore_tool restore")
            < production_body.index('["alembic", "upgrade", "head"]')
            < production_body.index("restore_tool cutover")
        )
        assert 'dropdb --if-exists "$PGDATABASE"' not in production_body
        assert '.set(database=os.environ["RESTORE_CANDIDATE"])' in production_body

    def test_production_test_mode_can_only_target_disposable_resources(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")

        assert (
            'PRODUCTION_TEST_COMPOSE_FILE="$PROJECT_DIR/docker-compose.restore-production.test.yml"'
            in script
        )
        assert "restore-db-production-test-*" in script
        assert "restore_production_test_*" in script
        assert "project_backups_dir=$(CDPATH='' cd -- \"$PROJECT_DIR/backups\" && pwd -P)" in script
        assert (
            "production_test_backup_dir=$(CDPATH='' cd -- \"$RESTORE_PRODUCTION_TEST_BACKUP_DIR\" && pwd -P)"
            in script
        )
        assert "RESTORE_PRODUCTION_TEST_BACKUP_DIR=$production_test_backup_dir" in script
        assert 'restore_tool restore "/backups/$archive_name" "$candidate_database"' in script

    def test_test_mode_isolated_restore_contract_is_explicit(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        test_body = script.split("run_test_restore() {", 1)[1]

        assert 'exec python3 "$SCRIPT_DIR/restore_drill.py"' in test_body
        assert "${RESTORE_SOURCE_RECEIPT:?" in test_body
        assert "${RESTORE_LEASE_ROOT:?" in test_body
        assert 'require_completed_archive "$archive"' in test_body
        assert '--archive-id "$(basename -- "$archive" .dump)"' in test_body
        assert "keeping resources is forbidden" in test_body
        assert "test_compose down" not in test_body
        assert "up -d" not in test_body
        assert '"$COMPOSE_FILE"' not in test_body
        assert "set -x" not in script

        assert '"$COMPOSE_FILE"' not in test_body

    @pytest.mark.parametrize("flag", [[], ["--allow-live-restore"]])
    def test_BK_12_redirected_production_remains_isolated(
        self, tmp_path: Path, flag: list[str]
    ) -> None:
        archive = tmp_path / "heatpump-20261003T000000Z.dump"
        archive.write_bytes(b"stub archive")
        command_log = tmp_path / "docker.log"
        docker = tmp_path / "docker"
        docker.write_text(
            f'''#!/bin/sh
printf '%s\\n' "$*" >> "{command_log.as_posix()}"
case "$*" in
    *"config --images db"*) printf '{PINNED_IMAGE}\\n' ;;
    *"inspect --format"*) printf 'pinned-image\\n' ;;
    *"ps -q db"*) printf 'isolated-db\\n' ;;
esac
''',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", *flag, archive.name],
            capture_output=True,
            text=True,
            check=False,
            input=f"RESTORE {archive.name}\n",
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-flags",
                "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": (
                    SCRIPT.parents[1] / "docker-compose.restore-production.test.yml"
                ).as_posix(),
                "RESTORE_PRODUCTION_TEST_BACKUP_DIR": tmp_path.as_posix(),
                "POSTGRES_DB": "restore_production_test_flags",
            },
        )
        assert result.returncode == 0, result.stderr
        commands = command_log.read_text(encoding="utf-8").splitlines()
        assert all(
            "-p restore-db-production-test-flags" in command
            for command in commands
            if command.startswith("compose ")
        )
        assert any("restore_archive.sh restore" in command for command in commands)
        assert any("restore_archive.sh cutover" in command for command in commands)
        assert not any("backup-verify" in command for command in commands)
        assert not any("stop db" in command for command in commands)

    def test_BK_6_production_requires_live_flag_even_with_disposable_environment_marker(
        self, tmp_path: Path
    ) -> None:
        result = subprocess.run(
            ["sh", str(SCRIPT), "--production", "heatpump-x.dump"],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "RESTORE_DISPOSABLE": "true",
                "RESTORE_PRODUCTION_TEST_PROJECT": "",
                "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": "",
                "RESTORE_PRODUCTION_TEST_BACKUP_DIR": "",
            },
        )
        assert result.returncode == 2
        assert "requires --allow-live-restore" in result.stderr

    def test_BK_12_test_rejects_live_flag(self) -> None:
        result = subprocess.run(
            ["sh", str(SCRIPT), "--test", "--allow-live-restore", "heatpump-x.dump"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "--test rejects --allow-live-restore" in result.stderr

    def test_BK_c_test_memory_is_always_tmpfs_plus_two_gib(self, tmp_path: Path) -> None:
        archive = tmp_path / "heatpump-20261003T000000Z.dump"
        archive.write_bytes(b"stub")
        command_log = tmp_path / "resources.log"
        docker = tmp_path / "docker"
        docker.write_text(
            f'#!/bin/sh\nprintf "%s:%s\\n" "$RESTORE_VERIFY_TMPFS_BYTES" "$RESTORE_VERIFY_MEMORY_BYTES" >> "{command_log.as_posix()}"\n',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        result = subprocess.run(
            ["sh", str(SCRIPT), "--test", archive.as_posix()],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "POSTGRES_DB": "restore_production_test_memory",
                "RESTORE_VERIFY_TMPFS_BYTES": str(3 * 1024**3),
                "RESTORE_VERIFY_MEMORY_BYTES": "1",
            },
        )
        # Memory sizing moved to the ownership coordinator (covered by
        # test_restore_drill). A prefix and memory override alone must not start Docker.
        assert result.returncode != 0
        assert "RESTORE_SOURCE_RECEIPT" in result.stderr
        assert not command_log.exists()

    def test_disposable_redirection_works_without_project_backups_directory(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "checkout"
        scripts = project / "scripts"
        scripts.mkdir(parents=True)
        entry = scripts / "restore_db.sh"
        shutil.copyfile(SCRIPT, entry)
        compose = project / "docker-compose.restore-production.test.yml"
        compose.write_text("services: {}\n", encoding="utf-8")
        external = tmp_path / "archives"
        external.mkdir()
        archive = external / "heatpump-20261003T000000Z.dump"
        archive.write_bytes(b"stub")
        docker = tmp_path / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o755)
        result = subprocess.run(
            ["sh", str(entry), "--production", archive.name],
            input="WRONG\n",
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "RESTORE_PRODUCTION_TEST_COMPOSE_FILE": compose.as_posix(),
                "RESTORE_PRODUCTION_TEST_PROJECT": "restore-db-production-test-clean",
                "RESTORE_PRODUCTION_TEST_BACKUP_DIR": external.as_posix(),
                "POSTGRES_DB": "restore_production_test_clean",
            },
        )
        assert result.returncode == 1, result.stderr
        assert "Confirmation did not match" in result.stderr
        assert not (project / "backups").exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell is required")
class TestRestoreArchive:
    def _run_restore(
        self,
        tmp_path: Path,
        *,
        disposable: bool = True,
        restore_status: int = 0,
        post_status: int = 0,
        version: str = "2.26.4",
        candidate_exists: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        archive = tmp_path / "heatpump-20261003T000000Z.dump"
        archive.write_bytes(b"stub archive")
        command_log = tmp_path / "commands.log"
        environment = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": tmp_path.as_posix(),
            "PGHOST": "restore-target" if disposable else "db",
            "PGUSER": "restore_test",
            "PGDATABASE": "restore_production_test_unit",
            "RESTORE_DISPOSABLE": str(disposable).lower(),
            "RESTORE_ISOLATED_PROJECT": "restore-db-production-test-unit",
            "RESTORE_ALLOW_LIVE_RESTORE": "true",
            "RESTORE_SOURCE_IMAGE": PINNED_IMAGE,
            "RESTORE_SOURCE_TIMESCALE_VERSION": version,
            "RESTORE_EXPECTED_REVISION": "030",
        }
        pg_restore = tmp_path / "pg_restore"
        pg_restore.write_text(
            f'''#!/bin/sh
printf 'pg_restore %s\\n' "$*" >> "{command_log.as_posix()}"
case "$*" in
    *--list*) printf '; Dumped from database version: 16.13\\n1; 1 1 TABLE public plans heatpump\\n' ;;
    *--data-only*) for argument do case $argument in --file=*) : > "${{argument#--file=}}" ;; esac; done ;;
    *) exit {restore_status} ;;
esac
''',
            encoding="utf-8",
        )
        psql = tmp_path / "psql"
        psql.write_text(
            f'''#!/bin/sh
printf 'psql %s\\n' "$*" >> "{command_log.as_posix()}"
case "$*" in
    *"FROM pg_database"*) printf '{"f" if candidate_exists else "t"}\\n' ;;
    *"FROM pg_roles WHERE"*) printf 'f\\n' ;;
    *timescaledb_post_restore*) exit {post_status} ;;
    *) printf 't\\n' ;;
esac
''',
            encoding="utf-8",
        )
        for command in ("createdb", "dropdb"):
            (tmp_path / command).write_text(
                f'#!/bin/sh\nprintf "{command} %s\\n" "$*" >> "{command_log.as_posix()}"\n',
                encoding="utf-8",
            )
        for command in ("pg_restore", "psql", "createdb", "dropdb"):
            (tmp_path / command).chmod(0o755)

        result = subprocess.run(
            ["sh", str(ARCHIVE_SCRIPT), "restore", archive.as_posix(), "restore_candidate"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, command_log.read_text(encoding="utf-8") if command_log.exists() else ""

    def test_BK_3_protocol_creates_nologin_role_and_uses_all_restore_flags(
        self, tmp_path: Path
    ) -> None:
        result, commands = self._run_restore(tmp_path)
        assert result.returncode == 0, result.stderr
        assert 'CREATE ROLE "heatpump" NOLOGIN' in commands
        assert "VERSION '2.26.4'" in commands
        assert commands.index("timescaledb_pre_restore()") < commands.index("--exit-on-error")
        assert "--exit-on-error --no-owner --no-privileges --dbname=restore_candidate" in commands
        assert commands.index("--exit-on-error") < commands.index("timescaledb_post_restore()")
        assert "dropdb" not in commands

    def test_BK_6_production_missing_role_fails_before_candidate_creation(
        self, tmp_path: Path
    ) -> None:
        result, commands = self._run_restore(tmp_path, disposable=False)
        assert result.returncode == 2
        assert "production never creates roles" in result.stderr
        assert "CREATE ROLE" not in commands
        assert "createdb" not in commands

    @pytest.mark.parametrize("post_status", [0, 17])
    def test_BK_3_BK_7_restore_failure_runs_post_hook_and_drops_only_candidate(
        self, tmp_path: Path, post_status: int
    ) -> None:
        result, commands = self._run_restore(tmp_path, restore_status=42, post_status=post_status)
        assert result.returncode == 42, result.stderr
        assert "timescaledb_post_restore()" in commands
        assert "dropdb --maintenance-db=postgres --if-exists --force restore_candidate" in commands
        assert "DROP ROLE" in commands

    @pytest.mark.parametrize("version", ["", "2.26.3", "2.27.0"])
    def test_BK_f_exact_timescale_match_has_no_override(self, tmp_path: Path, version: str) -> None:
        result, commands = self._run_restore(tmp_path, version=version)
        assert result.returncode == 2
        assert "must be exactly 2.26.4" in result.stderr
        assert commands == ""

    def test_post_restore_failure_after_success_is_not_hidden(self, tmp_path: Path) -> None:
        result, commands = self._run_restore(tmp_path, post_status=17)
        assert result.returncode == 17
        assert "dropdb --maintenance-db=postgres --if-exists --force restore_candidate" in commands

    def test_BK_7_existing_candidate_is_never_replaced_or_dropped(self, tmp_path: Path) -> None:
        result, commands = self._run_restore(tmp_path, candidate_exists=True)
        assert result.returncode == 2
        assert "Candidate database already exists" in result.stderr
        assert "createdb" not in commands
        assert "dropdb" not in commands
        assert "CREATE ROLE" not in commands

    def test_archive_helper_shell_syntax(self) -> None:
        result = subprocess.run(
            ["sh", "-n", str(ARCHIVE_SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("action", ["cutover", "rollback"])
    @pytest.mark.parametrize("missing_functions", [False, True])
    def test_BK_7_worker_controls_and_rename_order(
        self, tmp_path: Path, action: str, missing_functions: bool
    ) -> None:
        command_log = tmp_path / "commands.log"
        psql = tmp_path / "psql"
        psql.write_text(
            f'''#!/bin/sh
printf "%s\\n" "$*" >> "{command_log.as_posix()}"
case "$*" in
    *to_regprocedure*) printf '{"f" if missing_functions else "t"}\\n' ;;
    *) printf 't\\n' ;;
esac
''',
            encoding="utf-8",
        )
        psql.chmod(0o755)
        result = subprocess.run(
            ["sh", str(ARCHIVE_SCRIPT), action, "live", "candidate", "retained"],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "PGHOST": "restore-target",
                "PGUSER": "restore_test",
                "PGDATABASE": "restore_production_test_unit",
                "RESTORE_DISPOSABLE": "true",
                "RESTORE_ISOLATED_PROJECT": "restore-db-production-test-unit",
                "RESTORE_SOURCE_IMAGE": PINNED_IMAGE,
                "RESTORE_SOURCE_TIMESCALE_VERSION": "2.26.4",
            },
        )
        commands = command_log.read_text(encoding="utf-8")
        if missing_functions:
            assert result.returncode == 2
            assert "worker-control functions are absent" in result.stderr
            assert "ALTER DATABASE" not in commands
            assert "pg_terminate_backend" not in commands
            return
        assert result.returncode == 0, result.stderr
        assert commands.count("to_regprocedure(") == 4
        assert commands.index("to_regprocedure(") < commands.index("SET timescaledb.restoring")
        assert commands.index('ALTER DATABASE "live" SET') < commands.index(
            "SELECT _timescaledb_functions.stop_background_workers()"
        )
        first_rename = commands.index('ALTER DATABASE "live" RENAME TO "retained"')
        second_rename = commands.index('ALTER DATABASE "candidate" RENAME TO "live"')
        assert commands.index("pg_terminate_backend") < first_rename < second_rename
        assert second_rename < commands.index('ALTER DATABASE "live" RESET')
        assert commands.index('ALTER DATABASE "live" RESET') < commands.index(
            "SELECT _timescaledb_functions.start_background_workers()"
        )
        assert 'ALTER DATABASE "retained" RESET' not in commands
        assert "_timescaledb_internal" not in commands

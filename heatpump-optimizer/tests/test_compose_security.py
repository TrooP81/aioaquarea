"""Security invariants for the local development compose stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.config import Settings
from scripts import render_compose_evidence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = tuple(sorted(PROJECT_ROOT.glob("docker-compose*.yml")))
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"
README_FILE = PROJECT_ROOT / "README.md"
CONFIG_FILE = PROJECT_ROOT / "packages" / "core" / "config.py"
SENTINEL_SECRETS = {
    "AQUAREA_USERNAME": "synthetic-aquarea-username",
    "AQUAREA_PASSWORD": "synthetic-aquarea-password",
    "ENTSOE_API_TOKEN": "synthetic-entsoe-token",
    "SECRET_KEY": "synthetic-secret-key",
    "POSTGRES_PASSWORD": "synthetic-postgres-password",
    "AQUAREA_LOCATION": "synthetic-aquarea-location",
}


def _safe_compose_config() -> dict[str, object]:
    application_environment = {
        "APP_ENVIRONMENT": "development",
        "API_TOKEN": "disabled",
        "CORS_ORIGINS": "http://localhost:4444",
    }
    return {
        "services": {
            "db": {
                "environment": {"POSTGRES_PASSWORD": "changeme_in_production"},
                "ports": [{"host_ip": "127.0.0.1", "published": "5434"}],
            },
            "api": {
                "environment": {
                    **application_environment,
                    "DATABASE_URL": render_compose_evidence.SAFE_LOCAL_DATABASE_URL,
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "8500"}],
            },
            "web": {
                "environment": {"INTERNAL_API_URL": "http://api:8500"},
                "ports": [{"host_ip": "127.0.0.1", "published": "4444"}],
            },
            "migrate": {"environment": application_environment.copy()},
            "poller": {"environment": application_environment.copy()},
            "optimizer": {"environment": application_environment.copy()},
        }
    }


def _render_compose_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    output_path = tmp_path / "compose-evidence.json"
    invocation: dict[str, object] = {}

    def fake_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        invocation["command"] = command
        invocation["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(config))

    monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")
    render_compose_evidence.render_evidence(
        output_path,
        project_directory=tmp_path,
        compose_file=compose_file,
        runner=fake_runner,
    )
    return output_path, invocation


class TestComposeSecurity:
    def test_all_compose_env_files_default_to_dotenv_when_not_overridden(self) -> None:
        env_file_entries = []
        for compose_file in COMPOSE_FILES:
            env_file_entries.extend(
                (compose_file.name, line.strip())
                for line in compose_file.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("env_file:")
            )

        assert env_file_entries
        assert all(
            entry == "env_file: ${COMPOSE_ENV_FILE:-.env}" for _, entry in env_file_entries
        ), env_file_entries

    def test_runtime_and_evidence_sources_are_explicitly_documented(self) -> None:
        config_source = CONFIG_FILE.read_text(encoding="utf-8")
        readme = README_FILE.read_text(encoding="utf-8")

        assert '"env_file": ".env"' in config_source
        assert "python scripts/render_compose_evidence.py --output compose-evidence.json" in readme
        assert "COMPOSE_ENV_FILE=<empty>" not in readme
        assert "docker compose --env-file" not in readme

    def test_documented_local_contract_matches_environment_example(self) -> None:
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in ENV_EXAMPLE_FILE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        readme = README_FILE.read_text(encoding="utf-8")

        assert values["APP_ENVIRONMENT"] == "development"
        assert values["API_TOKEN"] == "disabled"
        assert values["CORS_ORIGINS"] == "http://localhost:4444"
        assert values["DB_PORT"] == "5434"
        assert values["API_PORT"] == "8500"
        assert values["WEB_PORT"] == "4444"
        assert "http://localhost:3500" not in readme
        assert "web :3500" not in readme

    def test_application_defaults_match_the_local_only_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("APP_ENVIRONMENT", "API_TOKEN", "CORS_ORIGINS"):
            monkeypatch.delenv(name, raising=False)

        settings = Settings(_env_file=None)

        assert settings.app_environment == "development"
        assert settings.api_token == "disabled"
        assert settings.cors_origins == "http://localhost:4444"

    def test_wrapper_output_is_clean_and_uses_both_env_selectors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AQUAREA_USERNAME", SENTINEL_SECRETS["AQUAREA_USERNAME"])
        monkeypatch.setenv("AQUAREA_PASSWORD", SENTINEL_SECRETS["AQUAREA_PASSWORD"])
        config = _safe_compose_config()
        output_path, invocation = _render_compose_evidence(tmp_path, monkeypatch, config)
        rendered = __import__("json").loads(output_path.read_text(encoding="utf-8"))

        for service_name in ("db", "api", "web"):
            ports = rendered["services"][service_name]["ports"]
            assert all(port["host_ip"] == "127.0.0.1" for port in ports)

        assert rendered["services"]["db"]["ports"][0]["published"] == "5434"
        assert rendered["services"]["api"]["ports"][0]["published"] == "8500"
        assert rendered["services"]["web"]["ports"][0]["published"] == "4444"

        for service_name in ("migrate", "poller", "optimizer", "api"):
            environment = rendered["services"][service_name]["environment"]
            assert environment["APP_ENVIRONMENT"] == "development"
            assert "API_TOKEN" not in environment
            assert environment["CORS_ORIGINS"] == "http://localhost:4444"

        assert all(
            not key.startswith("NEXT_PUBLIC") or "TOKEN" not in key
            for key in rendered["services"]["web"]["environment"]
        )
        assert "POSTGRES_PASSWORD" not in __import__("json").dumps(rendered)
        assert "DATABASE_URL" not in __import__("json").dumps(rendered)
        assert all(
            sentinel not in __import__("json").dumps(rendered)
            for sentinel in SENTINEL_SECRETS.values()
        )

        command = invocation["command"]
        environment = invocation["environment"]
        env_file_index = command.index("--env-file") + 1
        assert environment["COMPOSE_ENV_FILE"] == command[env_file_index]
        assert "AQUAREA_USERNAME" not in environment
        assert "AQUAREA_PASSWORD" not in environment

    def test_wrapper_replaces_case_insensitive_inherited_environment_with_safe_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inherited = {
            "pAtH": "synthetic-path",
            "cOmPoSe_EnV_fIlE": "C:\\live.env",
            "pOsTgReS_pAsSwOrD": SENTINEL_SECRETS["POSTGRES_PASSWORD"],
            "AqUaReA_PaSsWoRd": SENTINEL_SECRETS["AQUAREA_PASSWORD"],
        }
        monkeypatch.setattr(render_compose_evidence.os, "environ", inherited)
        _, invocation = _render_compose_evidence(tmp_path, monkeypatch, _safe_compose_config())

        environment = invocation["environment"]
        assert environment["pAtH"] == "synthetic-path"
        assert (
            environment["COMPOSE_ENV_FILE"]
            == invocation["command"][invocation["command"].index("--env-file") + 1]
        )
        assert environment["POSTGRES_PASSWORD"] == "changeme_in_production"
        assert "cOmPoSe_EnV_fIlE" not in environment
        assert "pOsTgReS_pAsSwOrD" not in environment
        assert "AqUaReA_PaSsWoRd" not in environment
        assert SENTINEL_SECRETS["POSTGRES_PASSWORD"] not in environment.values()
        assert SENTINEL_SECRETS["AQUAREA_PASSWORD"] not in environment.values()

    def test_wrapper_uses_a_temporary_env_file_for_both_selectors_and_removes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed: dict[str, object] = {}
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")

        def fake_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            env_file = Path(command[command.index("--env-file") + 1])
            observed["exists_during_run"] = env_file.is_file()
            observed["compose_env_file"] = kwargs["env"]["COMPOSE_ENV_FILE"]
            observed["command_env_file"] = str(env_file)
            return SimpleNamespace(returncode=0, stdout=json.dumps(_safe_compose_config()))

        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")
        render_compose_evidence.render_evidence(
            tmp_path / "compose-evidence.json",
            project_directory=tmp_path,
            compose_file=compose_file,
            runner=fake_runner,
        )

        assert observed["exists_during_run"] is True
        assert observed["compose_env_file"] == observed["command_env_file"]
        assert not Path(observed["command_env_file"]).exists()

    def test_wrapper_does_not_accept_an_env_selector_override(self) -> None:
        with pytest.raises(SystemExit):
            render_compose_evidence.main(["--output", "evidence.json", "--env-file", "unsafe.env"])

    def test_wrapper_requires_a_named_local_artifact(self, tmp_path: Path) -> None:
        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(tmp_path / "evidence.json")

    def test_wrapper_rejects_a_compose_file_outside_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_directory = tmp_path / "project"
        project_directory.mkdir()
        compose_file = tmp_path / "outside-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")

        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(
                project_directory / "compose-evidence.json",
                project_directory=project_directory,
                compose_file=compose_file,
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

    def test_wrapper_rejects_a_noncanonical_compose_path_before_running_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")

        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(
                tmp_path / "compose-evidence.json",
                project_directory=tmp_path,
                compose_file=tmp_path / "docker-compose.test.yml",
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

    def test_wrapper_rejects_compose_path_traversal_before_running_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_directory = tmp_path / "project"
        project_directory.mkdir()
        (project_directory / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")

        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(
                project_directory / "compose-evidence.json",
                project_directory=project_directory,
                compose_file=project_directory / ".." / "outside-compose.yml",
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

    def test_wrapper_rejects_output_path_traversal_before_running_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")

        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(
                tmp_path / ".." / "compose-evidence.json",
                project_directory=tmp_path,
                compose_file=compose_file,
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

    @pytest.mark.skipif(os.name == "nt", reason="symbolic links require elevated Windows support")
    def test_wrapper_rejects_a_symlinked_compose_source_before_running_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "source.yml"
        source.write_text("services: {}\n", encoding="utf-8")
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.symlink_to(source)
        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")

        with pytest.raises(render_compose_evidence.EvidenceError):
            render_compose_evidence.render_evidence(
                tmp_path / "compose-evidence.json",
                project_directory=tmp_path,
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

    def test_wrapper_has_a_runtime_symlink_guard_for_compose_and_output(self) -> None:
        source = Path(render_compose_evidence.__file__).read_text(encoding="utf-8")

        assert "candidate.is_symlink()" in source

    def test_wrapper_rejects_an_output_symlink_even_when_target_is_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "target.json"
        target.write_text("previous\n", encoding="utf-8")
        output_path = tmp_path / "compose-evidence.json"
        try:
            output_path.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are unavailable in this environment")

        with pytest.raises(render_compose_evidence.EvidenceError):
            _render_compose_evidence(tmp_path, monkeypatch, _safe_compose_config())

        assert target.read_text(encoding="utf-8") == "previous\n"

    def test_wrapper_does_not_publish_partial_write_over_an_existing_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output_path = tmp_path / "compose-evidence.json"
        output_path.write_text('{"previous": true}\n', encoding="utf-8")

        def fail_after_temporary_write(_file_descriptor: int) -> None:
            raise OSError("synthetic temporary-write failure")

        monkeypatch.setattr(render_compose_evidence.os, "fsync", fail_after_temporary_write)
        with pytest.raises((OSError, render_compose_evidence.EvidenceError)):
            _render_compose_evidence(tmp_path, monkeypatch, _safe_compose_config())

        assert output_path.read_text(encoding="utf-8") == '{"previous": true}\n'

    @pytest.mark.parametrize("nonfinite_number", ["NaN", "Infinity", "-Infinity"])
    def test_wrapper_rejects_non_json_numeric_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nonfinite_number: str
    ) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        output_path = tmp_path / "compose-evidence.json"

        def fake_runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout=('{"services":{"api":{"environment":{"VALUE":' + nonfinite_number + "}}}}"),
            )

        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")
        with pytest.raises(render_compose_evidence.EvidenceError) as error:
            render_compose_evidence.render_evidence(
                output_path,
                project_directory=tmp_path,
                compose_file=compose_file,
                runner=fake_runner,
            )

        assert nonfinite_number not in str(error.value)
        assert not output_path.exists()

    def test_failed_partial_compose_step_keeps_json_and_secrets_out_of_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")
        output_path = tmp_path / "compose-evidence.json"
        secret = SENTINEL_SECRETS["AQUAREA_PASSWORD"]
        partial_json = json.dumps({"services": {"api": {"environment": {"PASSWORD": secret}}}})

        def fake_runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout=partial_json, stderr=secret)

        monkeypatch.setattr(render_compose_evidence.shutil, "which", lambda _: "docker")
        with pytest.raises(render_compose_evidence.EvidenceError) as error:
            render_compose_evidence.render_evidence(
                output_path,
                project_directory=tmp_path,
                compose_file=compose_file,
                runner=fake_runner,
            )

        assert secret not in str(error.value)
        assert not output_path.exists()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("AQUAREA_USERNAME", SENTINEL_SECRETS["AQUAREA_USERNAME"]),
            ("AQUAREA_PASSWORD", SENTINEL_SECRETS["AQUAREA_PASSWORD"]),
            ("SECRET_KEY", SENTINEL_SECRETS["SECRET_KEY"]),
            ("ENTSOE_API_TOKEN", SENTINEL_SECRETS["ENTSOE_API_TOKEN"]),
            ("POSTGRES_PASSWORD", SENTINEL_SECRETS["POSTGRES_PASSWORD"]),
            ("LATITUDE", "52.37"),
            ("LONGITUDE", "4.89"),
            ("DATABASE_URL", "postgresql://user:synthetic-password@db/heatpump"),
        ],
    )
    def test_wrapper_fails_closed_for_synthetic_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
    ) -> None:
        config = _safe_compose_config()
        config["services"]["api"]["environment"][field] = value
        with pytest.raises(render_compose_evidence.EvidenceError) as error:
            _render_compose_evidence(tmp_path, monkeypatch, config)

        assert value not in str(error.value)
        assert not (tmp_path / "compose-evidence.json").exists()

    def test_artifact_matches_the_validated_projection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _safe_compose_config()
        config["services"]["api"]["environment"]["BACKUP_REPLICA_ENCRYPTION_KEY"] = ""
        output_path, _ = _render_compose_evidence(tmp_path, monkeypatch, config)

        artifact_bytes = output_path.read_bytes()
        artifact = __import__("json").loads(artifact_bytes)
        assert "BACKUP_REPLICA_ENCRYPTION_KEY" not in __import__("json").dumps(artifact)
        assert artifact == render_compose_evidence._project_evidence(config)
        assert artifact_bytes == (
            json.dumps(
                render_compose_evidence._project_evidence(config),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

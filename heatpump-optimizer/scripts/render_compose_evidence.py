"""Render a shareable, secret-free Docker Compose evidence artifact."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_ENVIRONMENT_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "DOCKER_CONFIG",
    "HOME",
)
COMPOSE_DEFAULTS = {
    "POSTGRES_USER": "heatpump",
    "POSTGRES_PASSWORD": "changeme_in_production",
    "POSTGRES_DB": "heatpump",
    "APP_ENVIRONMENT": "development",
    "API_TOKEN": "disabled",
    "CORS_ORIGINS": "http://localhost:4444",
    "DB_PORT": "5434",
    "API_PORT": "8500",
    "WEB_PORT": "4444",
}
SECRET_KEY_PARTS = (
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "KEY",
    "USERNAME",
    "LATITUDE",
    "LONGITUDE",
)
SAFE_LOCAL_VALUES = frozenset(COMPOSE_DEFAULTS.values())
SAFE_LOCAL_DATABASE_URL = "postgresql+asyncpg://heatpump:changeme_in_production@db:5432/heatpump"


class EvidenceError(RuntimeError):
    """Raised when Compose evidence cannot be safely produced."""


def _reject_nonfinite_number(_: str) -> None:
    raise ValueError("Non-finite JSON numbers are not supported.")


def _validated_compose_file(project_directory: Path, compose_file: Path | None) -> Path:
    canonical_file = project_directory / "docker-compose.yml"
    candidate = compose_file or canonical_file
    if ".." in candidate.parts:
        raise EvidenceError("Compose file must be the project's docker-compose.yml.")
    candidate = Path(os.path.abspath(candidate))
    if candidate != canonical_file or candidate.is_symlink() or not candidate.is_file():
        raise EvidenceError("Compose file must be the project's docker-compose.yml.")
    return candidate


def _validated_output_path(project_directory: Path, output_path: Path) -> Path:
    if ".." in output_path.parts:
        raise EvidenceError("Evidence artifact must be the project compose-evidence.json.")
    candidate = Path(os.path.abspath(output_path))
    expected_path = project_directory / "compose-evidence.json"
    if candidate != expected_path or candidate.is_symlink():
        raise EvidenceError("Evidence artifact must be the project compose-evidence.json.")
    return candidate


def _sanitized_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for requested_key in SAFE_ENVIRONMENT_KEYS:
        actual_key = next((key for key in os.environ if key.lower() == requested_key.lower()), None)
        if actual_key is not None:
            environment[actual_key] = os.environ[actual_key]
    environment.update(COMPOSE_DEFAULTS)
    return environment


def _is_secret_key(key: str) -> bool:
    normalized = key.upper()
    return normalized == "DATABASE_URL" or any(part in normalized for part in SECRET_KEY_PARTS)


def _has_url_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.scheme and (parsed.username is not None or parsed.password is not None))


def _validate_schema(config: object) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        raise EvidenceError("Compose returned an unsupported configuration schema.")
    return config


def _project_evidence(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise EvidenceError("Compose returned an unsupported configuration schema.")
            if _is_secret_key(key):
                if key.upper() == "DATABASE_URL" and child == SAFE_LOCAL_DATABASE_URL:
                    continue
                if child == "":
                    continue
                if isinstance(child, str) and child in SAFE_LOCAL_VALUES:
                    continue
                raise EvidenceError("Compose output contains a secret-bearing field.")
            projected[key] = _project_evidence(child, (*path, key))
        return projected
    if isinstance(value, list):
        return [_project_evidence(item, path) for item in value]
    if isinstance(value, str):
        if _has_url_credentials(value):
            raise EvidenceError("Compose output contains a credential-bearing URL.")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise EvidenceError("Compose returned an unsupported configuration schema.")


def render_evidence(
    output_path: Path,
    *,
    project_directory: Path = PROJECT_ROOT,
    compose_file: Path | None = None,
    runner: Any = subprocess.run,
) -> Path:
    """Render, validate, and write a redacted Compose evidence projection."""
    if shutil.which("docker") is None:
        raise EvidenceError("Docker with the Compose plugin is required.")

    project_directory = project_directory.resolve()
    compose_file = _validated_compose_file(project_directory, compose_file)
    output_path = _validated_output_path(project_directory, output_path)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".env", delete=False
    ) as handle:
        empty_env = Path(handle.name)
    try:
        environment = _sanitized_environment()
        environment["COMPOSE_ENV_FILE"] = str(empty_env)
        result = runner(
            [
                "docker",
                "compose",
                "--env-file",
                str(empty_env),
                "--project-directory",
                str(project_directory),
                "-f",
                str(compose_file),
                "config",
                "--format",
                "json",
            ],
            cwd=project_directory,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        if result.returncode:
            raise EvidenceError("Docker Compose config rendering failed or is unsupported.")
        try:
            config = json.loads(result.stdout, parse_constant=_reject_nonfinite_number)
        except (json.JSONDecodeError, ValueError) as error:
            raise EvidenceError("Docker Compose did not return JSON configuration.") from error
        projection = _project_evidence(_validate_schema(config))
        artifact = (
            json.dumps(projection, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=output_path.parent, prefix=".compose-evidence-", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(artifact)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, output_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return output_path
    finally:
        empty_env.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Evidence JSON artifact to create"
    )
    parser.add_argument("--project-directory", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--compose-file", type=Path)
    arguments = parser.parse_args(argv)
    try:
        output = render_evidence(
            arguments.output,
            project_directory=arguments.project_directory.resolve(),
            compose_file=arguments.compose_file,
        )
    except EvidenceError as error:
        print(f"Compose evidence failed: {error}", file=sys.stderr)
        return 1
    print(f"Compose evidence PASS: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

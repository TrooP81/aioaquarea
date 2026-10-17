"""Narrow local operator plan/apply interface; not an API or device-resolution lane.

Run in a dedicated Linux maintenance container, using an explicit DSN environment
variable (never the application's implicit .env). Docker is queried read-only.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.data_reset import (
    ReceiptStore,
    ResetRefused,
    ResetStageFailure,
    apply,
    canonical,
    canonical_server_address,
    plan,
    validate_manifest,
    validate_scope,
)


def source_fingerprint() -> str:
    """Bind every application Python source, including loaders and API mutation paths."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def docker_json(context: str, *arguments: str):
    result = subprocess.run(
        ["docker", "--context", context, *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise ResetRefused("read-only Docker resource inspection failed")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ResetRefused("Docker resource inspection unavailable") from exc


def inspect_resources(scope: dict, model_dir: Path) -> None:
    """Bind all project containers, stopped owners, volume labels and actual mount."""
    validate_scope(scope)
    if scope["context"] != "desktop-linux" or scope["source_revision"] != source_fingerprint():
        raise ResetRefused("context/build fingerprint mismatch")
    contexts = docker_json(scope["context"], "context", "inspect", "desktop-linux")
    if (
        len(contexts) != 1
        or contexts[0]["Endpoints"]["docker"]["Host"] != "unix:///var/run/docker.sock"
        or contexts[0]["Endpoints"]["docker"].get("SkipTLSVerify", False)
    ):
        raise ResetRefused("only the explicitly local Desktop daemon is permitted")
    project = scope["project"]
    if not project or scope["old_model_volume"] == scope["new_model_volume"]:
        raise ResetRefused("project/fresh storage required")
    # No shell invocation and no Compose up/down/run/stop/kill/prune/volume removal.
    listed = subprocess.run(
        [
            "docker",
            "--context",
            scope["context"],
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if listed.returncode:
        raise ResetRefused("container inventory unavailable")
    ids = listed.stdout.split()
    if not ids or set(ids) != set(scope["owners"]) | {scope["maintenance_container"]}:
        raise ResetRefused("new/missing/unapproved project container")
    containers = docker_json(scope["context"], "inspect", *ids)
    maintenance = None
    databases = []
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            raise ResetRefused("container project mismatch")
        files = labels.get("com.docker.compose.project.config_files")
        if not isinstance(files, str) or files.split(",") != scope["compose_files"].get(
            container["Id"]
        ):
            raise ResetRefused("Compose identity drift")
        state = container["State"]
        if state.get("Restarting") or state.get("Paused"):
            raise ResetRefused("container cleanup not settled")
        if container["Id"] == scope["maintenance_container"]:
            maintenance = container
            if not state["Running"]:
                raise ResetRefused("maintenance owner unavailable")
        elif labels.get("com.docker.compose.service") == "db":
            databases.append(container)
            if not state["Running"]:
                raise ResetRefused("database unavailable")
        elif state["Running"] or state["Status"] != "exited":
            raise ResetRefused("application/backup/one-shot owner not quiesced")
    if len(databases) != 1 or maintenance is None:
        raise ResetRefused("ambiguous database/maintenance identity")
    addresses = {
        canonical_server_address(network[key])
        for network in databases[0]["NetworkSettings"].get("Networks", {}).values()
        for key in ("IPAddress", "GlobalIPv6Address")
        if network.get(key)
    }
    if (
        canonical_server_address(scope["server_address"]) not in addresses
        or scope["server_port"] != 5432
    ):
        raise ResetRefused("database endpoint is not the inspected project database")
    # Must execute *inside* the approved maintenance container, not merely point
    # at an arbitrary empty local path on the operator's desktop.
    hostname = os.environ.get("HOSTNAME", "")
    if not hostname or not maintenance["Id"].startswith(hostname):
        raise ResetRefused("not running in approved maintenance container")
    mounts = maintenance["Mounts"]
    resolved = model_dir.resolve(strict=True)
    if not any(
        mount["Type"] == "volume"
        and mount["Name"] == scope["new_model_volume"]
        and Path(mount["Destination"]) == resolved
        for mount in mounts
    ):
        raise ResetRefused("new storage is not the inspected model mount")
    if resolved.is_symlink() or not resolved.is_dir() or any(resolved.iterdir()):
        raise ResetRefused("new model storage must be empty")
    for name in (scope["old_model_volume"], scope["new_model_volume"]):
        volumes = docker_json(scope["context"], "volume", "inspect", name)
        if len(volumes) != 1 or volumes[0]["Name"] != name:
            raise ResetRefused("volume identity mismatch")
        labels = volumes[0].get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            raise ResetRefused("model volume is not explicitly project-owned")
        # Detect cross-project consumers too. Old storage stays detached; only
        # this maintenance container may have the new volume mounted/running.
        users = subprocess.run(
            ["docker", "--context", scope["context"], "ps", "-q", "--filter", f"volume={name}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        allowed = {maintenance["Id"][:12]} if name == scope["new_model_volume"] else set()
        if users.returncode or any(user[:12] not in allowed for user in users.stdout.split()):
            raise ResetRefused("model storage has an unapproved active consumer")


async def run(args) -> dict:
    manifest_or_scope = json.loads(args.input.read_text(encoding="utf-8"))
    # Reject passive artifacts and incompatible manifests before DSN/resource/DB access.
    if args.command == "plan":
        validate_scope(manifest_or_scope)
    else:
        validate_manifest(manifest_or_scope)
    scope = manifest_or_scope if args.command == "plan" else manifest_or_scope["scope"]
    # Reject implicit runtime DB settings and non-PostgreSQL targets.
    url = make_url(os.environ[args.dsn_env])
    if url.drivername != "postgresql+asyncpg" or url.database != scope["database"]:
        raise ResetRefused("explicit PostgreSQL database identity required")
    inspect_resources(scope, args.model_dir)
    receipts = ReceiptStore(args.receipts)
    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            async with session.begin():
                yield session

    async def resource_guard(current):
        inspect_resources(current, args.model_dir)

    try:
        if args.command == "plan":
            return await plan(
                sessions,
                resource_guard,
                scope=scope,
                operation_uuid=args.operation_uuid,
                operator=args.operator,
                statement=args.statement,
                statement_at=args.statement_at,
                device_id=args.device_id,
            )
        return await apply(
            sessions,
            resource_guard,
            receipts,
            manifest_or_scope,
            approved_digest=args.approved_digest,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fingerprint", "plan", "apply"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--dsn-env", default="LOCAL_RESET_DATABASE_URL")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--operation-uuid")
    parser.add_argument("--operator")
    parser.add_argument("--statement")
    parser.add_argument("--statement-at")
    parser.add_argument("--device-id")
    parser.add_argument("--approved-digest")
    args = parser.parse_args()
    if args.command == "fingerprint":
        print(source_fingerprint())
        return 0
    required = ["input", "model_dir", "receipts"]
    required += (
        ["operation_uuid", "operator", "statement", "statement_at", "device_id"]
        if args.command == "plan"
        else ["approved_digest"]
    )
    if any(getattr(args, name) is None for name in required):
        parser.error("all operation-specific arguments are required")
    try:
        print(canonical(asyncio.run(run(args))))
        return 0
    except ResetStageFailure as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # Never print DSNs, ORM rows, subprocess stderr or driver SQL parameters.
        print(
            "reset refused before completion; leave workers stopped; inspect by operation UUID",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

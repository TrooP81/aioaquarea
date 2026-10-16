"""Supplementary Linux owned-source qualification; never an ordinary E2E test.

No household archive/configuration is accepted. All source identities come from
the existing prepared job.services proof, checked again through SQL and Docker.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scripts.restore_drill import IMAGE, LABEL
from tests.helpers.backup_qualification_command import command_json, command_result
from tests.e2e.test_manual_cop_postgresql import (
    current_verification_source_proof,
    guard_owned_database,
    owned_test_environment,
    owned_test_network,
    retained_storage_state,
    write_backup_receipt,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_owned_worker_and_coordinator_image_roundtrip(request):
    assert request.config.option.noconftest
    assert os.name == "posix"
    ownership = owned_test_environment()
    proof = current_verification_source_proof()
    assert (ownership["commit"], ownership["source_manifest_sha256"]) == (
        proof["commit"],
        proof["digest"],
    )
    base = os.environ.get("RESTORE_COORDINATOR_BASE_IMAGE", "")
    assert re.fullmatch(r"[^ ]+@sha256:[0-9a-f]{64}", base), (
        "the pinned immutable Docker CLI coordinator base is required"
    )
    token = uuid.uuid4().hex
    key = "backup_roundtrip_" + token
    root = Path(ownership["temp_root"]) / ("backup-" + token)
    root.mkdir(mode=0o700)
    leases = root / "leases"
    leases.mkdir(mode=0o700)
    created: list[str] = []
    started = time.monotonic()
    last_stderr = ""

    def docker(*args, limit=30):
        nonlocal last_stderr
        remaining = 540 - (time.monotonic() - started)
        assert remaining > 0, "supplementary phase deadline expired"
        result = command_result(["docker", *args], timeout=min(limit, remaining))
        last_stderr = result.stderr
        if last_stderr:
            print(f"docker {args[0]} stderr: {last_stderr}")
        return result.stdout.strip()

    def docker_json(*args, limit=30):
        output = docker(*args, limit=limit)
        return command_json(output, command=f"docker {args[0]}", stderr=last_stderr)

    def inspect(identity):
        rows = docker_json("container", "inspect", identity)
        assert len(rows) == 1 and rows[0]["Id"] == identity
        return rows[0]

    def create(*args):
        identity = docker("container", "create", "--label", f"{LABEL}={token}", *args)
        assert re.fullmatch(r"[0-9a-f]{64}", identity)
        created.append(identity)
        item = inspect(identity)
        assert item["Config"]["Labels"][LABEL] == token
        return identity

    source_state = None
    cleanup_verified = False
    seeded = False
    coordinator_verified = False
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        poolclass=NullPool,
        connect_args={"command_timeout": 20, "timeout": 20},
    )
    try:
        with owned_test_network():
            async with engine.begin() as connection:
                await guard_owned_database(connection)
                source_state = await retained_storage_state(connection)
                assert not (
                    await connection.execute(
                        text("SELECT 1 FROM service_heartbeats WHERE service='backup'")
                    )
                ).all(), "qualification cannot overwrite an existing backup history"
                await connection.execute(
                    text("INSERT INTO settings(key,value) VALUES (:key,:value)"),
                    {"key": key, "value": key},
                )
                seeded = True
        platform = inspect(ownership["services"]["postgres"]["id"])
        assert platform["Config"]["Image"] == IMAGE
        network = ownership["namespace"]
        endpoint = platform["NetworkSettings"]["Networks"][network["name"]]
        assert endpoint["NetworkID"] == network["id"]
        worker = create(
            "--network",
            network["id"],
            "--tmpfs",
            "/backups:rw,size=268435456",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=16777216",
            "-e",
            f"PGHOST={endpoint['IPAddress']}",
            "-e",
            "PGUSER=heatpump",
            "-e",
            "PGPASSWORD=heatpump_test",
            "-e",
            "PGDATABASE=heatpump_test",
            "-e",
            "BACKUP_REPLICA_ENABLED=false",
            "-e",
            "BACKUP_REPLICA_REQUIRED=false",
            "--entrypoint",
            "sh",
            IMAGE,
            "-ceu",
            "mkdir -p /scripts /backups; sleep 590",
        )
        docker("start", worker)
        scripts = Path(__file__).parents[2] / "scripts"
        for name in ("backup_db.sh", "backup_status.sql", "stage_backup.sh"):
            docker("cp", str(scripts / name), f"{worker}:/scripts/{name}")
        docker("exec", worker, "sh", "/scripts/backup_db.sh", "--once", limit=120)
        binding = docker_json(
            "exec",
            worker,
            "psql",
            "-XqAt",
            "-c",
            "SELECT details_json::jsonb->'last_success' FROM service_heartbeats "
            "WHERE service='backup'",
        )
        assert binding["archive_id"].startswith("heatpump-")
        source = inspect(worker)
        receipt = {
            "container_id": worker,
            "image_id": source["Image"],
            "created": source["Created"],
            "network_id": network["id"],
            "prepared_at": time.time(),
            "expires_at": time.time() + 540,
            "source_id": binding["source_id"],
        }
        receipt_path = root / "source.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        image = "hpo-restore-coordinator:" + token
        docker(
            "build",
            "--build-arg",
            f"RESTORE_COORDINATOR_BASE_IMAGE={base}",
            "-f",
            "Dockerfile.restore-coordinator",
            "-t",
            image,
            ".",
            limit=120,
        )
        coordinator = create(
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=bind,source={root},target={root}",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            image,
            "--lease-root",
            str(leases),
            "--source-receipt",
            str(receipt_path),
            "--archive-id",
            binding["archive_id"],
            "--probe-key",
            key,
            "--overall-seconds",
            "360",
            "--cleanup-reserve",
            "60",
        )
        item = inspect(coordinator)
        assert item["HostConfig"]["NetworkMode"] == "none"
        assert not item["HostConfig"].get("PortBindings")
        docker("start", coordinator)
        exit_code = docker("wait", coordinator, limit=420)
        assert exit_code == "0", docker("logs", coordinator)
        records = list(leases.glob("*/receipt.json"))
        assert len(records) == 1
        drill = json.loads(records[0].read_text(encoding="utf-8"))
        assert drill["status"] == "verified"
        assert drill["content_verified"] is True and drill["cleanup_verified"] is True
        assert drill["residual_resources"] == []
        coordinator_verified = True
        with owned_test_network():
            async with engine.connect() as connection:
                await guard_owned_database(connection)
                assert (
                    await connection.execute(
                        text("SELECT value FROM settings WHERE key=:key"), {"key": key}
                    )
                ).scalar_one() == key
                assert await retained_storage_state(connection) == source_state
                history = json.loads(
                    (
                        await connection.execute(
                            text(
                                "SELECT details_json FROM service_heartbeats WHERE service='backup'"
                            )
                        )
                    ).scalar_one()
                )
                assert history["last_verified"]["sha256"] == binding["sha256"]
                assert history["last_success"] == binding
    finally:
        errors = []
        for identity in reversed(created):
            try:
                item = inspect(identity)
                assert item["Config"]["Labels"][LABEL] == token
                if item["State"]["Running"]:
                    # TERM lets the coordinator settle its actual restore containers;
                    # killing the client/container alone is never proof of teardown.
                    docker("stop", "--time", "65", identity, limit=75)
                    docker("wait", identity)
                docker("container", "rm", identity)
                assert (
                    identity
                    not in docker(
                        "container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"
                    ).splitlines()
                )
            except Exception as error:
                errors.append(type(error).__name__)
        for path in leases.glob("*/receipt.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("cleanup_verified") is not True or record.get("residual_resources"):
                errors.append("unverified_coordinator_teardown")
        try:
            if seeded:
                with owned_test_network():
                    async with engine.begin() as connection:
                        await guard_owned_database(connection)
                        await connection.execute(
                            text("DELETE FROM settings WHERE key=:key AND value=:key"), {"key": key}
                        )
                        await connection.execute(
                            text(
                                "DELETE FROM service_heartbeats WHERE service='backup' "
                                "AND details_json::jsonb->'last_success'->>'archive_id'=:archive"
                            ),
                            {"archive": binding["archive_id"] if "binding" in locals() else ""},
                        )
                        assert await retained_storage_state(connection) == source_state
        except Exception as error:
            errors.append(type(error).__name__)
        await engine.dispose()
        cleanup_verified = not errors
        write_backup_receipt(
            Path(ownership["temp_root"]) / "evidence",
            {
                "test": request.node.nodeid,
                "ownership_token": ownership["token"],
                "commit": proof["commit"],
                "source_manifest_sha256": proof["digest"],
                "created_resources": created,
                "cleanup_verified": cleanup_verified,
                "source_preserved": cleanup_verified and source_state is not None,
                "coordinator_verified": coordinator_verified,
                "worker_script_sha256": hashlib.sha256(
                    (Path(__file__).parents[2] / "scripts" / "backup_db.sh").read_bytes()
                ).hexdigest(),
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        assert cleanup_verified, f"synthetic backup cleanup failed: {errors}"

"""Explicit, bounded Linux-only disposable restore lifecycle (no application imports).

The source receipt must be produced by the owning qualification/maintenance gate,
not inferred from a prefix, open port, labels alone, or this program's arguments.
Never adopt resources. Only returned creation IDs enter the cleanup ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)
LABEL = "org.heatpump.restore-lease"


class DrillError(RuntimeError):
    """Fixed, sanitized failure codes only."""


@dataclass
class Budget:
    seconds: float = 4500
    reserve: float = 120
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)

    def __post_init__(self):
        self.started = self.clock()
        if self.seconds <= self.reserve or self.reserve < 60:
            raise DrillError("invalid_deadline")

    def remaining(self, cap: float, *, cleanup: bool = False) -> float:
        available = self.seconds - (self.clock() - self.started)
        if not cleanup:
            available -= self.reserve
        if available <= 0:
            raise DrillError("lifecycle_timeout")
        return min(cap, available)


def run_command(args: list[str], *, timeout: float) -> str:
    """Bound CLI and reap its process group; cleanup separately settles containers."""
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        out, _ = process.communicate(timeout=timeout)
    except BaseException:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    if process.returncode:
        raise DrillError("command_failed")
    return out.decode("utf-8").strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_manifest(manifest: dict, archive: Path) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("eligible") is not True
        or not re.fullmatch(r"heatpump-[0-9a-f]{32}", str(manifest.get("archive_id", "")))
        or manifest.get("sha256") != digest(archive)
        or not manifest.get("source_id")
        or not re.fullmatch(r"[0-9]+", str(manifest.get("schema_revision", "")))
        or manifest.get("postgres_major") != 16
        or manifest.get("timescale_version") != "2.26.4"
        or type(manifest.get("database_size_bytes")) is not int
        or manifest["database_size_bytes"] <= 0
    ):
        raise DrillError("archive_binding_invalid")


def stage_local(
    root: Path, lease: Path, select: Callable[[], tuple[Path, dict]]
) -> tuple[Path, dict]:
    """Test/maintenance primitive: select under the same flock as retention."""
    import fcntl

    if root.is_symlink() or not root.is_dir() or (root / ".backup.lock").is_symlink():
        raise DrillError("stage_ownership_invalid")
    with (root / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive, manifest = select()
        if archive.parent != root or archive.is_symlink() or not archive.is_file():
            raise DrillError("archive_ownership_invalid")
        validate_manifest(manifest, archive)
        stage = lease / "archive.dump"
        with stage.open("xb") as output, archive.open("rb") as source:
            shutil.copyfileobj(source, output)
        validate_manifest(manifest, stage)
        (lease / "archive.json").write_text(json.dumps(manifest), encoding="utf-8")
        return stage, manifest


class Coordinator:
    def __init__(
        self,
        root: Path,
        source_receipt: dict,
        *,
        budget: Budget | None = None,
        runner=run_command,
        archive_id: str | None = None,
        probe_key: str | None = None,
    ):
        if runner is run_command:
            if os.name != "posix":
                raise DrillError("linux_route_required")
            if os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock") != (
                "unix:///var/run/docker.sock"
            ):
                raise DrillError("local_daemon_required")
        if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
            raise DrillError("private_lease_root_required")
        self.budget = budget or Budget()
        self.created_after = time.time()
        self.runner = runner
        self.token = uuid.uuid4().hex
        self.lease = root / self.token
        self.lease.mkdir(mode=0o700)  # exclusive, no adoption or cleanup on collision
        self.source = source_receipt
        self.archive_id = archive_id
        if probe_key is not None and not re.fullmatch(r"backup_roundtrip_[0-9a-f]{32}", probe_key):
            raise DrillError("qualification_probe_invalid")
        self.probe_key = probe_key
        self.resources: list[tuple[str, str]] = []
        self.residual: list[tuple[str, str]] = []
        self.stage_path: str | None = None
        self.manifest: dict = {}
        self.reporting_started = False
        self.report_deadline: float | None = None
        self.receipt = {
            "lease": self.token,
            "deadline_seconds": self.budget.seconds,
            "scripts": {
                name: digest(Path(__file__).with_name(name))
                for name in ("restore_drill.py", "restore_archive.sh", "backup_status.sql")
            },
            "planned_names": [f"restore-{self.token}-{role}" for role in ("net", "db", "work")],
            "resources": [],
            "status": "reserved",
            "source_receipt_sha256": hashlib.sha256(
                json.dumps(source_receipt, sort_keys=True).encode()
            ).hexdigest(),
            "target_image": IMAGE,
        }
        self.save()

    def save(self):
        temporary = self.lease / "receipt.tmp"
        temporary.write_text(json.dumps(self.receipt, sort_keys=True), encoding="utf-8")
        temporary.replace(self.lease / "receipt.json")

    def docker(self, *args: str, cap: float = 30, cleanup: bool = False) -> str:
        if self.report_deadline is not None:
            cap = min(cap, self.report_deadline - time.monotonic())
            if cap <= 0:
                raise DrillError("report_timeout")
        return self.runner(["docker", *args], timeout=self.budget.remaining(cap, cleanup=cleanup))

    def inspect(self, kind: str, identity: str, *, cleanup: bool = False) -> dict:
        values = json.loads(self.docker(kind, "inspect", identity, cleanup=cleanup))
        if len(values) != 1:
            raise DrillError("ownership_inspection_failed")
        return values[0]

    def source_guard(self, *, cleanup=False):
        # Full immutable identities plus fresh preparation receipt, not a project prefix.
        if set(self.source) != {
            "container_id",
            "image_id",
            "created",
            "network_id",
            "prepared_at",
            "expires_at",
            "source_id",
        }:
            raise DrillError("source_receipt_invalid")
        now = time.time()
        if not self.source["prepared_at"] <= now < self.source["expires_at"]:
            raise DrillError("source_receipt_expired")
        container = self.inspect("container", self.source["container_id"], cleanup=cleanup)
        networks = container["NetworkSettings"]["Networks"].values()
        if (
            container["Id"] != self.source["container_id"]
            or container["Image"] != self.source["image_id"]
            or container["Created"] != self.source["created"]
            or not container["State"]["Running"]
            or self.source["network_id"] not in {n["NetworkID"] for n in networks}
        ):
            raise DrillError("source_identity_changed")
        return container["Id"]

    def create(self, kind: str, *args: str) -> str:
        output = self.docker(kind, "create", "--label", f"{LABEL}={self.token}", *args)
        if not re.fullmatch(r"[0-9a-f]{64}", output):
            # Volumes return their exclusively reserved name rather than an ID.
            raise DrillError("creation_receipt_invalid")
        self.resources.append((kind, output))
        self.receipt["resources"] = list(self.resources)
        self.save()
        inspected = self.inspect(kind, output)
        labels = inspected.get("Labels", inspected.get("Config", {}).get("Labels", {})) or {}
        if labels.get(LABEL) != self.token:
            raise DrillError("ownership_inspection_failed")
        created = inspected.get("Created")
        if not isinstance(created, str):
            raise DrillError("creation_time_unproven")
        try:
            created_at = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_at.tzinfo is None or not (
                self.created_after <= created_at.timestamp() <= time.time()
            ):
                raise ValueError
        except ValueError:
            raise DrillError("creation_time_unproven") from None
        return output

    def capacity(self, size: int, archive_size: int) -> int:
        info = json.loads(self.docker("info", "--format", "{{json .}}"))
        quantum = 268435456
        required = (size * 3 + 1) // 2 + 1073741824
        target = max(2147483648, ((required + quantum - 1) // quantum) * quantum)
        # tmpfs plus PostgreSQL/process allowance and a separate 1 GiB host reserve.
        if info.get("MemTotal", 0) < target + 3758096384:
            raise DrillError("memory_capacity_refused")
        try:
            available = next(
                int(line.split()[1]) * 1024
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        except (OSError, StopIteration, ValueError):
            raise DrillError("memory_capacity_unverifiable") from None
        if available < target + 3758096384:
            raise DrillError("memory_headroom_refused")
        if shutil.disk_usage(self.lease).free < archive_size * 2 + 1073741824:
            raise DrillError("scratch_capacity_refused")
        return target

    def report(self, *, start=False, succeeded=False, reason=""):
        self.report_deadline = time.monotonic() + 20
        try:
            self._report(start=start, succeeded=succeeded, reason=reason)
        finally:
            self.report_deadline = None

    def _report(self, *, start=False, succeeded=False, reason=""):
        source = self.source_guard(cleanup=not start)
        m = self.manifest
        # One CLI budget bounds inspection+SQL final reporting together.
        started = self.receipt["started_at"]
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        args = [
            "exec",
            "-e",
            "PGCONNECT_TIMEOUT=5",
            "-e",
            "PGOPTIONS=-c statement_timeout=10000 -c lock_timeout=5000",
            source,
            "psql",
            "-Xq",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "backup_start=false",
            "-v",
            "backup_finish=false",
            "-v",
            f"verification_start={'true' if start else 'false'}",
            "-v",
            f"verification_finish={'false' if start else 'true'}",
            "-v",
            f"succeeded={'true' if succeeded else 'false'}",
            "-v",
            f"attempt={self.token}",
            "-v",
            f"archive={m['archive_id']}",
            "-v",
            f"checksum={m['sha256']}",
            "-v",
            f"source={m['source_id']}",
            "-v",
            f"revision={m['schema_revision']}",
            "-v",
            f"started={started}",
            "-v",
            f"finished={finished}",
            "-v",
            f"reason={reason}",
            "-v",
            f"status={'running' if start else 'succeeded' if succeeded else 'failed'}",
            "-f",
            "/scripts/backup_status.sql",
        ]
        self.docker(*args, cap=20, cleanup=not start)
        if start:
            self.reporting_started = True

    def container_guard(
        self, identity: str, network: str, *, archive: bool, running=False, writable=False
    ):
        """Before start inspect configuration; after start require runtime membership."""
        item = self.inspect("container", identity)
        endpoints = item["NetworkSettings"]["Networks"].values()
        if (
            item["Config"]["Image"] != IMAGE
            or item["Image"] != self.image_id
            or item["Config"].get("Labels", {}).get(LABEL) != self.token
            or item["HostConfig"].get("PortBindings")
            or item["HostConfig"].get("Privileged")
            or item["HostConfig"].get("NetworkMode") != network
            or item["State"]["Running"] is not running
        ):
            raise DrillError("target_isolation_failed")
        if network != "none" and (
            len(endpoints) != 1
            or any(
                n.get("NetworkID") not in ({network} if running else {"", network})
                for n in endpoints
            )
        ):
            raise DrillError("target_network_untrusted")
        for mount in item.get("Mounts", []):
            if mount["Type"] != "tmpfs" and not (
                archive
                and mount["Destination"] == "/backups"
                and mount["Type"] == "volume"
                and mount["Name"] == self.volume
                and mount["RW"] is writable
            ):
                raise DrillError("target_mount_untrusted")

    def create_archive_volume(self) -> str:
        """A returned volume name alone is NOT creation proof (create may adopt)."""
        name = f"restore-{self.token}-{uuid.uuid4().hex}-archive"
        nonce = uuid.uuid4().hex
        self.receipt["planned_volume"] = name
        self.receipt["volume_nonce"] = nonce
        self.receipt["volume_creation_rejected"] = ["command_or_inspection"]
        self.save()
        try:
            # Docker volume CreatedAt may have only whole-second precision.
            # Cross that boundary before creation, rather than relaxing the proof.
            while int(time.time()) <= self.created_after:
                self.budget.remaining(1)
                time.sleep(0.05)
            result = self.docker(
                "volume",
                "create",
                "--label",
                f"{LABEL}={self.token}",
                "--label",
                f"{LABEL}.nonce={nonce}",
                name,
            )
            item = self.inspect("volume", name)
            created = dt.datetime.fromisoformat(item["CreatedAt"].replace("Z", "+00:00"))
            checks = {
                "returned_name": result == name,
                "inspected_name": item["Name"] == name,
                "local_driver": item.get("Driver") == "local",
                "no_options": not item.get("Options"),
                "lease_label": item.get("Labels", {}).get(LABEL) == self.token,
                "nonce_label": item.get("Labels", {}).get(f"{LABEL}.nonce") == nonce,
                "creation_time": created.tzinfo is not None
                and self.created_after <= created.timestamp() <= time.time(),
            }
            self.receipt["volume_creation_rejected"] = [
                key for key, passed in checks.items() if not passed
            ]
            if self.receipt["volume_creation_rejected"]:
                raise DrillError("volume_creation_unproven")
        except Exception:
            # Never mount, write or remove a possibly adopted foreign volume.
            self.residual.append(("unverified_volume", name))
            self.save()
            raise DrillError("volume_creation_unproven") from None
        self.resources.append(("volume", name))
        self.receipt["resources"] = list(self.resources)
        self.save()
        self.volume = name
        return name

    def execute(self):
        self.receipt["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        source = self.source_guard()
        self.image_id = self.inspect("image", IMAGE)["Id"]
        # Persist the attempt before staging so a corrupt/copy-failed drill cannot
        # leave an older qualification looking healthy.
        raw = self.docker(
            "exec",
            source,
            "psql",
            "-XqAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SELECT details_json::jsonb->'last_success' FROM service_heartbeats "
            "WHERE service='backup'",
        )
        self.manifest = json.loads(raw)
        if not isinstance(self.manifest, dict) or not all(
            self.manifest.get(key)
            for key in ("archive_id", "sha256", "source_id", "schema_revision")
        ):
            raise DrillError("source_success_missing")
        selected = dict(self.manifest)
        if self.archive_id is not None and selected["archive_id"] != self.archive_id:
            raise DrillError("selected_archive_mismatch")
        self.report(start=True)
        # Record the exact identity BEFORE either exec: a timed-out client is
        # not evidence that the source-side operation stopped or never created.
        self.stage_path = f"/backups/.stage-{self.token}"
        self.receipt["source_stage"] = {"path": self.stage_path, "token": self.token}
        self.save()
        self.docker(
            "exec",
            "-e",
            f"BACKUP_STAGE_TOKEN={self.token}",
            source,
            "sh",
            "/scripts/stage_backup.sh",
            "reserve",
            cap=10,
        )
        stage = self.docker(
            "exec",
            "-e",
            f"BACKUP_STAGE_TOKEN={self.token}",
            source,
            "sh",
            "/scripts/stage_backup.sh",
            "run",
            cap=60,
        )
        expected = f"/backups/.stage-{self.token}"
        if stage != expected:
            raise DrillError("source_stage_invalid")
        for name in ("archive.dump", "archive.json"):
            self.docker("cp", f"{source}:{stage}/{name}", str(self.lease / name), cap=60)
        archive = self.lease / "archive.dump"
        self.manifest = json.loads((self.lease / "archive.json").read_text(encoding="utf-8"))
        validate_manifest(self.manifest, archive)
        if any(
            self.manifest[key] != selected[key]
            for key in ("archive_id", "sha256", "source_id", "schema_revision")
        ):
            raise DrillError("selected_archive_changed")
        if self.manifest["source_id"] != self.source["source_id"]:
            raise DrillError("source_archive_mismatch")
        self.receipt["archive_sha256"] = self.manifest["sha256"]
        self.save()
        target_size = self.capacity(self.manifest["database_size_bytes"], archive.stat().st_size)
        net = self.create("network", "--internal", f"restore-{self.token}-net")
        if not self.inspect("network", net).get("Internal"):
            raise DrillError("target_network_not_internal")
        volume = self.create_archive_volume()
        stage_container = self.create(
            "container",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume},target=/backups",
            "--entrypoint",
            "sh",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=16777216",
            IMAGE,
            "-c",
            "exit 0",
        )
        self.container_guard(stage_container, "none", archive=True, writable=True)
        self.docker("cp", str(archive), f"{stage_container}:/backups/archive.dump", cap=60)
        self.docker(
            "cp",
            str(Path(__file__).with_name("restore_archive.sh")),
            f"{stage_container}:/backups/restore_archive.sh",
        )
        password = uuid.uuid4().hex
        database = "restore_production_test_" + self.token
        env = [
            "-e",
            "PGHOST=restore-target",
            "-e",
            "PGUSER=restore_test",
            "-e",
            f"PGPASSWORD={password}",
            "-e",
            f"PGDATABASE={database}",
        ]
        db = self.create(
            "container",
            "--name",
            f"restore-{self.token}-db",
            "--network",
            net,
            "--network-alias",
            "restore-target",
            "--memory",
            str(target_size + 2147483648),
            "--cpus",
            "2",
            "--tmpfs",
            f"/var/lib/postgresql/data:rw,size={target_size}",
            "-e",
            "POSTGRES_USER=restore_test",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={database}",
            "-e",
            "TIMESCALEDB_TELEMETRY=off",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "while :; do sleep 60; done",
        )
        self.container_guard(db, net, archive=False)
        self.docker("start", db)
        self.container_guard(db, net, archive=False, running=True)
        # The idle entrypoint cannot initialize a database before runtime checks.
        self.docker(
            "exec",
            "-d",
            db,
            "docker-entrypoint.sh",
            "postgres",
            "-c",
            "shared_buffers=256MB",
            "-c",
            "maintenance_work_mem=512MB",
            "-c",
            "work_mem=16MB",
            "-c",
            "max_connections=20",
            "-c",
            "timescaledb.telemetry_level=off",
        )
        startup = time.monotonic() + self.budget.remaining(90)
        while True:
            try:
                self.docker("exec", db, "pg_isready", "-U", "restore_test", "-d", database, cap=2)
                break
            except DrillError:
                if time.monotonic() >= startup:
                    raise DrillError("startup_timeout") from None
                time.sleep(1)
        identity = self.docker(
            "exec",
            db,
            "psql",
            "-U",
            "restore_test",
            "-d",
            database,
            "-Atc",
            "SELECT current_database() || '|' || current_user",
        )
        if identity != f"{database}|restore_test":
            raise DrillError("target_database_identity_failed")
        work = self.create(
            "container",
            "--name",
            f"restore-{self.token}-work",
            "--network",
            net,
            "--memory",
            "536870912",
            "--cpus",
            "2",
            "--mount",
            f"type=volume,source={volume},target=/backups,readonly",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=16777216",
            *env,
            "-e",
            "RESTORE_DISPOSABLE=true",
            "-e",
            f"RESTORE_ISOLATED_PROJECT=restore-db-production-test-{self.token}",
            "-e",
            f"RESTORE_SOURCE_IMAGE={IMAGE}",
            "-e",
            "RESTORE_SOURCE_TIMESCALE_VERSION=2.26.4",
            "-e",
            f"RESTORE_EXPECTED_REVISION={self.manifest['schema_revision']}",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "while :; do sleep 60; done",
        )
        self.container_guard(work, net, archive=True)
        self.docker("start", work)
        self.container_guard(work, net, archive=True, running=True)
        self.docker(
            "exec",
            work,
            "sh",
            "/backups/restore_archive.sh",
            "restore",
            "/backups/archive.dump",
            "restored_" + self.token,
            cap=3600,
        )
        if self.probe_key:
            seeded = self.docker(
                "exec",
                db,
                "psql",
                "-U",
                "restore_test",
                "-d",
                "restored_" + self.token,
                "-Atc",
                f"SELECT value FROM settings WHERE key='{self.probe_key}'",
            )
            if seeded != self.probe_key:
                raise DrillError("seeded_content_mismatch")
        self.receipt["content_verified"] = True
        self.save()

    def cleanup(self) -> bool:
        """Only exact creation-proven IDs; verify membership and absence."""
        okay = not self.residual
        for kind, identity in reversed(self.resources):
            try:
                item = self.inspect(kind, identity, cleanup=True)
                labels = item.get("Labels", item.get("Config", {}).get("Labels", {})) or {}
                if labels.get(LABEL) != self.token:
                    raise DrillError("cleanup_ownership_lost")
                if kind == "volume" and labels.get(f"{LABEL}.nonce") != self.receipt.get(
                    "volume_nonce"
                ):
                    raise DrillError("cleanup_volume_ownership_lost")
                if kind == "network" and item.get("Containers"):
                    raise DrillError("cleanup_unexpected_membership")
                if kind == "container" and item["State"]["Running"]:
                    self.docker("kill", identity, cleanup=True, cap=10)
                    self.docker("wait", identity, cleanup=True, cap=10)
                    if self.inspect(kind, identity, cleanup=True)["State"]["Running"]:
                        raise DrillError("cleanup_work_running")
                self.docker(kind, "rm", identity, cleanup=True, cap=10)
                # Successful list with no exact ID/name proves absence; failed inspect alone cannot.
                listing = self.docker(
                    kind,
                    "ls",
                    *(
                        ["-a", "--no-trunc"]
                        if kind == "container"
                        else ["--no-trunc"]
                        if kind == "network"
                        else []
                    ),
                    "--format",
                    "{{.ID}}" if kind != "volume" else "{{.Name}}",
                    cleanup=True,
                ).splitlines()
                if identity in listing:
                    raise DrillError("cleanup_residual")
            except Exception:
                okay = False
                self.residual.append((kind, identity))
        if self.stage_path:
            try:
                source = self.source_guard(cleanup=True)
                # Source cleanup waits on the copy's inherited control lock,
                # proves its reserved token, and removes only exact stage files.
                settled = self.docker(
                    "exec",
                    "-e",
                    f"BACKUP_STAGE_TOKEN={self.token}",
                    source,
                    "sh",
                    "/scripts/stage_backup.sh",
                    "cleanup",
                    cleanup=True,
                    cap=60,
                )
                if settled != "settled":
                    raise DrillError("stage_cleanup_unverified")
                self.stage_path = None
            except Exception:
                okay = False
                self.residual.append(("source_stage", self.stage_path))
        for name in ("archive.dump", "archive.json"):
            try:
                (self.lease / name).unlink(missing_ok=True)
            except OSError:
                okay = False
                self.residual.append(("local_archive", str(self.lease / name)))
        self.receipt["residual_resources"] = self.residual
        self.receipt["cleanup_verified"] = okay
        self.save()
        return okay

    def run(self) -> bool:
        failure = ""
        try:
            self.execute()
        except BaseException:
            failure = "verification_resource_failed"
        cleaned = self.cleanup()
        if not cleaned:
            failure = "cleanup_failed"
        try:
            if self.reporting_started:
                self.report(succeeded=not failure, reason=failure)
            else:
                failure = failure or "reporting_incomplete"
        except Exception:
            failure = "reporting_incomplete"
        self.receipt["status"] = failure or "verified"
        self.save()
        return not failure


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-root", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--archive-id")
    parser.add_argument("--probe-key")
    parser.add_argument("--overall-seconds", type=int, default=4500)
    parser.add_argument("--cleanup-reserve", type=int, default=120)
    args = parser.parse_args()
    source = json.loads(args.source_receipt.read_text(encoding="utf-8"))
    if not 120 <= args.overall_seconds <= 4500:
        parser.error("overall deadline must be between 120 and 4500 seconds")
    coordinator = Coordinator(
        args.lease_root,
        source,
        archive_id=args.archive_id,
        probe_key=args.probe_key,
        budget=Budget(seconds=args.overall_seconds, reserve=args.cleanup_reserve),
    )

    def cancel(_signum, _frame):
        raise DrillError("cancelled")

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    return 0 if coordinator.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Reset-bound administrative invalidation. No device client, Redis or settings writes.

The operator entry point supplies fresh, independently checked resource/quiescence
evidence at *each* stage. Transactions never stop services or terminate sessions.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import hashlib
from ipaddress import ip_interface
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from packages.core.models import (
    AuditLogRecord,
    Base,
    MLLifecycleStateRecord,
    PlanActionRecord,
    SettingRecord,
    PriceScopeRecord,
    ShowerEventRecord,
    SmartThingsToken,
)
from packages.core.safety_attempts import device_attempt_key

WIPE_TABLES = (
    "device_status",
    "consumption",
    "prices",
    "weather",
    "indoor_temp_reading",
    "faults",
    "manual_cop_observations",
    "cop_history",
    "plans",
    "plan_actions",
    "executor_verification_reads",
    "optimization_requests",
    "overrides",
    "shower_events",
    "space_heating_gate",
    "service_heartbeats",
    "ml_forecast_outcomes",
    "ml_forecast_issues",
    "ml_maintenance_requests",
    "ml_artifacts",
    "ml_lifecycle_state",
    "app_logs",
    "audit_log",
)
RETAIN_TABLES = ("settings", "smartthings_oauth_token", "price_scopes", "alembic_version")
AUDIT_ACTIONS = ("reset_operator_retirement", "settings_preserving_reset")
TARGET = (11763, 11762, 26)
MAINTENANCE_KEY = 521769314703255406
OPEN_EVENTS = ("active", "recovery_pending", "timeout_pending")
UNRESOLVED = ("pending", "executing", "dispatched")
MANIFEST_VERSION = 2


class ResetRefused(RuntimeError):
    """A guard failed. The operator must leave application workers stopped."""


def canonical_server_address(value: str) -> str:
    """Canonicalize one host, bare or full-width mask; never authorize a network."""
    if not isinstance(value, str) or "%" in value:
        raise ResetRefused("database endpoint must be an unscoped IP literal")
    try:
        address = ip_interface(value)
        if address.network.prefixlen != address.max_prefixlen:
            raise ValueError("not a single host")
        return address.ip.compressed
    except ValueError:
        raise ResetRefused("database endpoint must be an unscoped IP literal") from None


class ResetStageFailure(RuntimeError):
    def __init__(self, stage: str):
        super().__init__(f"reset stopped at stage: {stage}; leave workers stopped; read back UUID")
        self.stage = stage


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def row_values(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def manifest_digest(manifest: dict) -> str:
    return fingerprint({key: value for key, value in manifest.items() if key != "digest"})


def validate_scope(scope: dict) -> None:
    """Require exact full-ID provenance; never infer, merge or normalize Compose paths."""
    if not isinstance(scope, dict) or set(scope) != {
        "context",
        "project",
        "compose_files",
        "database",
        "database_oid",
        "server_address",
        "server_port",
        "old_model_volume",
        "new_model_volume",
        "source_revision",
        "owners",
        "maintenance_container",
    }:
        raise ResetRefused("incomplete resource scope")
    if scope["context"] != "desktop-linux" or not scope["project"]:
        raise ResetRefused("wrong context/project")
    if not scope["old_model_volume"] or scope["old_model_volume"] == scope["new_model_volume"]:
        raise ResetRefused("fresh model storage required")
    owners = scope["owners"]
    maintenance = scope["maintenance_container"]
    if (
        not isinstance(owners, list)
        or not owners
        or not all(
            isinstance(identity, str)
            and len(identity) == 64
            and all(char in "0123456789abcdef" for char in identity)
            for identity in [*owners, maintenance]
        )
        or len(set(owners)) != len(owners)
        or maintenance in owners
    ):
        raise ResetRefused("invalid full container inventory")
    files = scope["compose_files"]
    if (
        not isinstance(files, dict)
        or set(files) != set(owners) | {maintenance}
        or not all(
            isinstance(paths, list)
            and paths
            and all(isinstance(path, str) and path for path in paths)
            for paths in files.values()
        )
    ):
        raise ResetRefused("Compose provenance must cover the exact container inventory")


def validate_manifest(manifest: dict) -> None:
    if (
        set(manifest)
        != {
            "version",
            "operation_uuid",
            "scope",
            "operator",
            "statement",
            "statement_at",
            "device_id",
            "target",
            "previous_epoch",
            "fingerprint",
            "previous_statuses",
            "digest",
        }
        or len(canonical(manifest)) > 16384
    ):
        raise ResetRefused("manifest must be bounded and contain only reviewed fields")
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("digest") != manifest_digest(
        manifest
    ):
        raise ResetRefused("invalid manifest")
    UUID(manifest["operation_uuid"])
    UUID(manifest["previous_epoch"])
    if len(manifest["fingerprint"]) != 64 or any(
        char not in "0123456789abcdef" for char in manifest["fingerprint"]
    ):
        raise ResetRefused("invalid approved fingerprint")
    if tuple(manifest.get("target", ())) != TARGET:
        raise ResetRefused("only reviewed exact historical tuple is supported")
    for name in ("operator", "statement", "statement_at", "device_id", "previous_epoch"):
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            raise ResetRefused(f"missing {name}")
    if dt.datetime.fromisoformat(manifest["statement_at"]).tzinfo is None:
        raise ResetRefused("operator statement requires timezone")
    validate_scope(manifest["scope"])


class ReceiptStore:
    """Exclusive append-only stage files in an operator-owned private directory.

    Directory ownership/ACL is a precondition. Linux directory fsync is required;
    unsupported platforms refuse before retirement rather than promise durability.
    Files contain only the minimal manifest/receipt, never ORM payloads or secrets.
    """

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if os.name != "posix":
            raise ResetRefused("durable receipt custody requires a private Linux directory")
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ResetRefused("receipt directory must be owned by operator, mode 0700")

    def persist(self, operation_uuid: str, stage: str, value: dict) -> None:
        operation_uuid = str(UUID(operation_uuid))
        if stage not in ("prepared", "retired", "reset"):
            raise ResetRefused("invalid receipt stage")
        path = self.root / f"{operation_uuid}.{stage}.json"
        encoded = canonical(value).encode()
        if path.exists():
            if path.is_symlink() or path.read_bytes() != encoded:
                raise ResetRefused("receipt replay conflict")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
            if stream.read() != encoded:
                raise ResetRefused("receipt readback failed")
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


async def _rows(session, model):
    return list((await session.scalars(select(model).order_by(*model.__table__.primary_key))).all())


@asynccontextmanager
async def _transaction(session_factory):
    async with asyncio.timeout(45):
        async with session_factory() as session:
            yield session


async def _no_other_sessions(session):
    if await session.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND backend_type='client backend'"
        )
    ):
        raise ResetRefused("unexpected database sessions")


def retained_maintenance_enabled(value: str) -> bool:
    """Same accepted bool vocabulary as SettingSpec, without loading runtime .env."""
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off", ""):
        return False
    raise ResetRefused("unrecognized retained maintenance preference")


async def reset_history_summary(session) -> dict:
    """Read-only, secret-free distinction between retirement and physical recovery."""
    row = (
        await session.scalars(
            select(AuditLogRecord)
            .where(AuditLogRecord.action.in_(AUDIT_ACTIONS))
            .order_by(AuditLogRecord.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {"available": True, "data_reset_at": None, "historical_retirement": None}
    try:
        value = json.loads(row.payload_json or "{}")
        if (
            value["disposition"] != "operator_invalidated"
            or value["reason"] != "reset_superseded"
            or value["physical_state_verified"] is not False
            or value["device_write"] is not False
        ):
            raise ValueError("invalid administrative receipt")
        reset_at = value.get("data_reset_at") if row.action == AUDIT_ACTIONS[1] else None
        for key in (
            ("data_reset_at", "retired_at") if row.action == AUDIT_ACTIONS[1] else ("retired_at",)
        ):
            if dt.datetime.fromisoformat(value[key]).tzinfo is None:
                raise ValueError("unavailable administrative time")
        return {
            "available": True,
            "data_reset_at": reset_at,
            "historical_retirement": {
                "disposition": "operator_invalidated",
                "reason": "reset_superseded",
                "physical_state_verified": False,
                "device_write": False,
                "retired_at": value["retired_at"],
            },
        }
    except (KeyError, TypeError, ValueError):
        return {"available": False, "data_reset_at": None, "historical_retirement": None}


async def _guard(session, scope: dict, device_id: str) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    await session.execute(text("SET LOCAL statement_timeout = '30s'"))
    for key in (MAINTENANCE_KEY, device_attempt_key(device_id)):
        if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
            raise ResetRefused("maintenance/device owner not settled")
    identity = (
        await session.execute(
            text(
                "SELECT current_database(), (SELECT oid FROM pg_database WHERE datname=current_database()), "
                "host(inet_server_addr()), inet_server_port()"
            )
        )
    ).one()
    database, database_oid, server_address, server_port = identity
    if [database, database_oid, canonical_server_address(server_address), server_port] != [
        scope["database"],
        scope["database_oid"],
        canonical_server_address(scope["server_address"]),
        scope["server_port"],
    ]:
        raise ResetRefused("database identity drift")
    # No pool, idle client, one-shot worker or foreign application may remain.
    await _no_other_sessions(session)
    tables = set(
        (
            await session.scalars(
                text(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind IN ('r','p')"
                )
            )
        ).all()
    )
    expected = set(WIPE_TABLES) | set(RETAIN_TABLES)
    if tables != expected or set(Base.metadata.tables) != expected - {"alembic_version"}:
        raise ResetRefused("unknown/missing application tables")
    # All application rows, new inserts and settings/credential writers fenced.
    await session.execute(
        text(
            "LOCK TABLE "
            + ", ".join(f'public."{name}"' for name in sorted(expected))
            + " IN ACCESS EXCLUSIVE MODE NOWAIT"
        )
    )
    if (await session.scalars(text("SELECT version_num FROM public.alembic_version"))).all() != [
        "037"
    ]:
        raise ResetRefused("schema must remain at 037")
    dependencies = (
        await session.execute(
            text(
                "SELECT ns.nspname, c.relname FROM pg_constraint fk "
                "JOIN pg_class c ON c.oid=fk.conrelid JOIN pg_namespace ns ON ns.oid=c.relnamespace "
                "JOIN pg_class target ON target.oid=fk.confrelid "
                "JOIN pg_namespace tn ON tn.oid=target.relnamespace "
                "WHERE fk.contype='f' AND tn.nspname='public' AND target.relname = ANY(:tables)"
            ),
            {"tables": list(WIPE_TABLES)},
        )
    ).all()
    if any(schema != "public" or table not in WIPE_TABLES for schema, table in dependencies):
        raise ResetRefused("unapproved dependency")
    lifecycle = await session.get(MLLifecycleStateRecord, 1)
    if lifecycle is None or lifecycle.lease_owner or lifecycle.lease_expires_at:
        raise ResetRefused("lifecycle owner not settled")
    if await session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM ml_maintenance_requests WHERE status='running') "
            "OR EXISTS(SELECT 1 FROM optimization_requests WHERE status='running')"
        )
    ):
        raise ResetRefused("inflight application work")


async def _snapshot(session, device_id: str, *, retired: bool = False) -> dict:
    actions = list(
        (
            await session.scalars(
                select(PlanActionRecord)
                .order_by(PlanActionRecord.id)
                .limit(10001)
                .with_for_update()
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(ShowerEventRecord)
                .order_by(ShowerEventRecord.id)
                .limit(10001)
                .with_for_update()
            )
        ).all()
    )
    if len(actions) > 10000 or len(events) > 10000:
        raise ResetRefused("inventory exceeds review bound")
    restores = [
        row for row in actions if row.reverts_action_id is not None and row.status in UNRESOLVED
    ]
    open_events = [row for row in events if row.status in OPEN_EVENTS]
    expected_restores = [] if retired else [TARGET[0]]
    expected_events = [] if retired else [TARGET[2]]
    if [row.id for row in restores] != expected_restores or [
        row.id for row in open_events
    ] != expected_events:
        raise ResetRefused("new, missing or unapproved safety obligations")
    if any(
        row.status in ("executing", "dispatched") or row.safety_claim_token or row.safety_claimed_at
        for row in actions
        if row.status in UNRESOLVED
    ):
        raise ResetRefused("active claim or inflight action")
    by_id = {row.id: row for row in actions}
    restore, source = by_id.get(TARGET[0]), by_id.get(TARGET[1])
    event = next((row for row in events if row.id == TARGET[2]), None)
    if restore is None or source is None or event is None:
        raise ResetRefused("approved tuple missing")
    if (
        restore.reverts_action_id != source.id
        or event.activation_action_id != source.id
        or {restore.device_id, source.device_id, event.device_id} != {device_id}
        or restore.action_type != "force_dhw_off"
        or source.action_type != "force_dhw_on"
        or source.status != "expired"
        or restore.status != ("operator_invalidated" if retired else "pending")
        or (retired and event.status != "operator_invalidated")
        or restore.safety_claim_token
        or restore.safety_claimed_at
    ):
        raise ResetRefused("approved linkage/state changed")
    # An executed activation without a terminal or inventoried restore is also unsafe.
    for row in actions:
        if row.action_type in ("force_dhw_on", "zone_temp_boost") and row.status in (
            "executed",
            "executed_unverified",
            "expired",
            "failed",
        ):
            linked = [item for item in actions if item.reverts_action_id == row.id]
            if not linked or any(
                item.status in UNRESOLVED and item.id != TARGET[0] for item in linked
            ):
                raise ResetRefused("unapproved activation obligation")
    state = await session.get(MLLifecycleStateRecord, 1)
    return {
        "previous_epoch": state.epoch,
        "fingerprint": fingerprint([row_values(source), row_values(restore), row_values(event)]),
        "previous_statuses": [restore.status, source.status, event.status],
    }


async def plan(
    session_factory,
    resource_guard,
    *,
    scope: dict,
    operation_uuid: str,
    operator: str,
    statement: str,
    statement_at: str,
    device_id: str,
) -> dict:
    """Locked preparation after authorized quiescence, NOT passive discovery."""
    validate_scope(scope)
    await resource_guard(scope)
    async with _transaction(session_factory) as session:
        await _guard(session, scope, device_id)
        snapshot = await _snapshot(session, device_id)
    manifest = {
        "version": MANIFEST_VERSION,
        "operation_uuid": str(UUID(operation_uuid)),
        "scope": scope,
        "operator": operator,
        "statement": statement,
        "statement_at": statement_at,
        "device_id": device_id,
        "target": list(TARGET),
        **snapshot,
    }
    manifest["digest"] = manifest_digest(manifest)
    validate_manifest(manifest)
    return manifest


async def _audit(session, operation_uuid: str, action: str):
    rows = (
        await session.scalars(
            select(AuditLogRecord).where(AuditLogRecord.action == action).limit(1001)
        )
    ).all()
    if len(rows) > 1000:
        raise ResetRefused("operation audit inventory exceeds review bound")
    matches = []
    for row in rows:
        try:
            value = json.loads(row.payload_json or "{}")
        except ValueError as exc:
            raise ResetRefused("unreadable reset audit") from exc
        if value.get("operation_uuid") == operation_uuid:
            matches.append(value)
    if len(matches) > 1:
        raise ResetRefused("ambiguous operation audit")
    return matches[0] if matches else None


def _check_replay(record: dict, manifest: dict) -> None:
    if record["manifest_digest"] != manifest["digest"]:
        raise ResetRefused("operation UUID reused with altered manifest")


def _add_audit(session, action: str, receipt: dict):
    session.add(
        AuditLogRecord(
            actor="local_reset_operator",
            action=action,
            target_device=receipt["device_id"],
            payload_json=canonical(receipt),
            result="administrative",
        )
    )


async def apply(
    session_factory, resource_guard, receipts: ReceiptStore, manifest: dict, *, approved_digest: str
) -> dict:
    """Retire, read back, persist receipt, then wipe. Never auto-retry drift."""
    validate_manifest(manifest)
    if approved_digest != manifest["digest"]:
        raise ResetRefused("explicit exact-manifest approval required")
    operation = manifest["operation_uuid"]
    stage = "preparation"
    try:
        await resource_guard(manifest["scope"])
        receipts.persist(
            operation, "prepared", {"stage": "approved_intention", "manifest": manifest}
        )
        async with _transaction(session_factory) as session:
            await _guard(session, manifest["scope"], manifest["device_id"])
            completed = await _audit(session, operation, AUDIT_ACTIONS[1])
            if completed:
                _check_replay(completed, manifest)
                receipt = completed
            else:
                receipt = await _audit(session, operation, AUDIT_ACTIONS[0])
                if receipt:
                    _check_replay(receipt, manifest)
                    current = await _snapshot(session, manifest["device_id"], retired=True)
                    if (
                        current["fingerprint"] != receipt["settled_fingerprint"]
                        or current["previous_epoch"] != manifest["previous_epoch"]
                    ):
                        raise ResetRefused("settled target drift")
                else:
                    current = await _snapshot(session, manifest["device_id"])
                    if any(current[key] != manifest[key] for key in current):
                        raise ResetRefused("approved fingerprint/epoch drift")
                    restore = await session.get(PlanActionRecord, TARGET[0])
                    event = await session.get(ShowerEventRecord, TARGET[2])
                    receipt = {
                        key: manifest[key]
                        for key in (
                            "operation_uuid",
                            "device_id",
                            "previous_epoch",
                            "target",
                            "operator",
                            "statement",
                            "statement_at",
                            "previous_statuses",
                        )
                    }
                    receipt.update(
                        manifest_digest=manifest["digest"],
                        approved_fingerprint=manifest["fingerprint"],
                        disposition="operator_invalidated",
                        reason="reset_superseded",
                        device_write=False,
                        physical_state_verified=False,
                        retired_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    )
                    try:
                        previous = json.loads(restore.result_json or "{}")
                    except ValueError:
                        previous = restore.result_json
                    restore.result_json = canonical(
                        {"previous_result": previous, "retirement": receipt}
                    )
                    restore.status = event.status = "operator_invalidated"
                    await session.flush()
                    settled = await _snapshot(session, manifest["device_id"], retired=True)
                    receipt["settled_fingerprint"] = settled["fingerprint"]
                    _add_audit(session, AUDIT_ACTIONS[0], receipt)
                    await session.flush()
                    await _no_other_sessions(session)
                    stage = "retirement_commit_pending_readback"
        stage = "retirement_committed_receipt_pending"
        # Read *committed* audit back before authorizing irreversible history removal.
        async with _transaction(session_factory) as session:
            readback = await _audit(
                session, operation, AUDIT_ACTIONS[1] if completed else AUDIT_ACTIONS[0]
            )
            if readback != receipt:
                raise ResetRefused("committed retirement readback failed")
        if completed:
            receipts.persist(operation, "reset", receipt)
            return receipt
        receipts.persist(operation, "retired", receipt)
        stage = "retirement_receipt_durable"
        await resource_guard(manifest["scope"])
        async with _transaction(session_factory) as session:
            await _guard(session, manifest["scope"], manifest["device_id"])
            current = await _snapshot(session, manifest["device_id"], retired=True)
            if (
                current["fingerprint"] != receipt["settled_fingerprint"]
                or current["previous_epoch"] != manifest["previous_epoch"]
            ):
                raise ResetRefused("settled target drift before wipe")
            retained = [
                [row_values(row) for row in await _rows(session, model)]
                for model in (SettingRecord, SmartThingsToken, PriceScopeRecord)
            ]
            # This is the only deletion statement; identifiers are compile-time constants.
            await session.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(f'public."{name}"' for name in WIPE_TABLES)
                    + " CONTINUE IDENTITY RESTRICT"
                )
            )
            maintenance = next(
                (row["value"] for row in retained[0] if row["key"] == "ml_maintenance_enabled"),
                "false",
            )
            maintenance_enabled = retained_maintenance_enabled(maintenance)
            epoch = str(uuid4())
            session.add(
                MLLifecycleStateRecord(
                    id=1,
                    epoch=epoch,
                    maintenance_enabled=maintenance_enabled,
                )
            )
            receipt = {
                **receipt,
                "new_epoch": epoch,
                "data_reset_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "model_storage": "fresh_empty_restart_required",
            }
            _add_audit(
                session,
                AUDIT_ACTIONS[0],
                {
                    key: value
                    for key, value in receipt.items()
                    if key not in ("new_epoch", "data_reset_at", "model_storage")
                },
            )
            _add_audit(session, AUDIT_ACTIONS[1], receipt)
            await session.flush()
            after = [
                [row_values(row) for row in await _rows(session, model)]
                for model in (SettingRecord, SmartThingsToken, PriceScopeRecord)
            ]
            if retained != after:
                raise ResetRefused("retained configuration equality failed")
            for name in WIPE_TABLES:
                count = await session.scalar(text(f'SELECT count(*) FROM public."{name}"'))
                if count != {"audit_log": 2, "ml_lifecycle_state": 1}.get(name, 0):
                    raise ResetRefused("reset bootstrap/emptiness verification failed")
            await _no_other_sessions(session)
            stage = "reset_commit_pending_readback"
        stage = "reset_committed_receipt_pending"
        async with _transaction(session_factory) as session:
            if await _audit(session, operation, AUDIT_ACTIONS[1]) != receipt:
                raise ResetRefused("reset readback failed")
        receipts.persist(operation, "reset", receipt)
        return receipt
    except Exception as exc:
        # Deliberately do not log SQL, credential rows, parameters or driver errors.
        raise ResetStageFailure(stage) from exc

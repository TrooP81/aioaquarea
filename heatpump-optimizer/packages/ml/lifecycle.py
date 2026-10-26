"""Durable observe-only authority and poller-owned maintenance orchestration."""

from __future__ import annotations

import datetime as dt
import asyncio
import copy
import hashlib
import os
import stat
import pickle
import platform
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import Interval, String, and_, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    ML_LIFECYCLE_TOKEN_SEQUENCE,
    MLArtifactRecord,
    MLForecastIssueRecord,
    MLForecastOutcomeRecord,
    MLLifecycleStateRecord,
    MLMaintenanceRequestRecord,
    SettingRecord,
)
from packages.core.settings_service import get_bool_setting, get_setting_spec
from packages.ml import safe_persistence
from packages.ml.safe_persistence import SignedArtifact

logger = structlog.get_logger()

LIFECYCLE_SCHEMAS = {
    "demand": "demand_lifecycle_v1",
    "comfort": "comfort_v8",
    "thermal": "thermal_v5",
}
ORPHAN_GRACE = dt.timedelta(hours=24)
EVIDENCE_RETENTION = dt.timedelta(days=90)
QUALIFICATION_MAX_AGE = dt.timedelta(hours=72)
PROTECTED_ROLES = frozenset({"incumbent", "candidate", "rollback", "retained"})
MAINTENANCE_CADENCE = dt.timedelta(hours=6)
DEMAND_CADENCE = dt.timedelta(hours=24)
RETENTION_BATCH_SIZE = 250
ARTIFACT_UNAVAILABLE_ATTEMPTS = 3
ARTIFACT_RETRY_MAX = dt.timedelta(hours=24)


class LifecycleUnavailable(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class CASConflict(RuntimeError):
    pass


class ArtifactUnavailableError(LifecycleUnavailable):
    def __init__(self, reference: ArtifactReference, error: OSError) -> None:
        super().__init__("artifact_unavailable")
        self.reference = reference
        self.missing = isinstance(error, FileNotFoundError)
        self.error_type = type(error).__name__


class ArtifactIntegrityError(ValueError):
    def __init__(self, reference: ArtifactReference, error: Exception) -> None:
        super().__init__(str(error))
        self.reference = reference


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _maintenance_slot(when: dt.datetime) -> int:
    return int(when.timestamp() // MAINTENANCE_CADENCE.total_seconds())


@dataclass(frozen=True)
class ArtifactReference:
    artifact_hash: str
    epoch: str
    component: str
    schema_version: str
    manifest: dict[str, Any]
    role: str
    committed_revision: int
    qualified_at: dt.datetime | None = None
    qualification_expires_at: dt.datetime | None = None
    last_valid_outcome_at: dt.datetime | None = None
    qualification_scope: dict[str, Any] | None = None
    retained_until: dt.datetime | None = None


@dataclass(frozen=True)
class LifecycleSnapshot:
    epoch: str
    revision: int
    fence: int
    maintenance_enabled: bool
    lease_owner: str | None
    lease_expires_at: dt.datetime | None
    references: tuple[ArtifactReference, ...] = ()
    checkpoints: dict[str, Any] | None = None
    next_maintenance_at: dt.datetime | None = None
    last_demand_attempt_at: dt.datetime | None = None

    def reference(self, component: str, role: str = "incumbent") -> ArtifactReference | None:
        matches = [
            ref for ref in self.references if ref.component == component and ref.role == role
        ]
        if len(matches) > 1:
            raise LifecycleUnavailable("duplicate_component_reference")
        return matches[0] if matches else None


@dataclass
class _ArtifactInventory:
    directory: Path
    epoch: str
    entries: Any
    progress: dict[str, Any]


_ARTIFACT_INVENTORY: _ArtifactInventory | None = None
_ARTIFACT_INVENTORY_LOCK = Lock()


@dataclass(frozen=True)
class Lease:
    epoch: str
    owner: str
    fence: int
    revision: int
    expires_at: dt.datetime


@dataclass(frozen=True)
class Qualification:
    qualified_at: dt.datetime
    last_valid_outcome_at: dt.datetime
    scope: dict[str, Any]

    @property
    def expires_at(self) -> dt.datetime:
        return self.last_valid_outcome_at + QUALIFICATION_MAX_AGE


@dataclass(frozen=True)
class PublicationResult:
    committed: bool
    revision: int | None = None
    readback: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class MaintenanceRequest:
    id: str
    epoch: str
    component: str
    source: str
    status: str
    requested_at: dt.datetime
    attempted_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    details: dict[str, Any] | None = None
    dedup_key: str | None = None

    def response(self, enabled: bool) -> dict[str, Any]:
        reason = (self.details or {}).get("reason")
        if not enabled and self.status in {"queued", "running"}:
            reason = "maintenance_disabled"
        return {
            "request_id": self.id,
            "component": self.component,
            "source": self.source,
            "status": self.status,
            "requested_at": self.requested_at.isoformat(),
            "attempted_at": self.attempted_at.isoformat() if self.attempted_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "maintenance_enabled": enabled,
            "reason": reason,
            "message": "maintenance disabled" if reason == "maintenance_disabled" else reason,
            "details": self.details or {},
            "control_ready": False,
            "ordinary_writes_allowed": False,
        }


@dataclass(frozen=True)
class ComponentHooks:
    """Materialize closed-session inputs; fit returns data, never publishes it."""

    materialize: Callable[[MaintenanceRequest], Awaitable[Any]]
    fit: Callable[[Any], Any]
    commit_candidate: Callable[[Lease, Any, LifecycleSnapshot], Awaitable[PublicationResult]]
    review_candidate: Callable[[Lease, ArtifactReference], Awaitable[None]] | None = None


def _request(row: MLMaintenanceRequestRecord) -> MaintenanceRequest:
    return MaintenanceRequest(
        row.id,
        row.epoch,
        row.component,
        row.source,
        row.status,
        row.requested_at,
        row.attempted_at,
        row.finished_at,
        dict(row.details),
        row.dedup_key,
    )


def _thermal_request_family(details: dict[str, Any], dedup_key: str | None) -> str | None:
    family = details.get("family")
    if "family" in details and (not isinstance(family, str) or family not in {"tank_", "indoor_"}):
        raise ValueError("Invalid thermal maintenance family")
    legacy_family = None
    if dedup_key is not None and not isinstance(dedup_key, str):
        raise ValueError("Invalid thermal maintenance family identity")
    if dedup_key and dedup_key.startswith("thermal-family:"):
        parts = dedup_key.split(":", 2)
        if len(parts) != 3 or not parts[2] or parts[1] not in {"tank_", "indoor_"}:
            raise ValueError("Invalid legacy thermal maintenance family")
        legacy_family = parts[1]
    if family is not None and legacy_family is not None and family != legacy_family:
        raise ValueError("Conflicting thermal maintenance family")
    return family or legacy_family


def _reference(row: MLArtifactRecord) -> ArtifactReference:
    return ArtifactReference(
        artifact_hash=row.artifact_hash,
        epoch=row.epoch,
        component=row.component,
        schema_version=row.schema_version,
        manifest=copy.deepcopy(row.manifest),
        role=row.role,
        committed_revision=row.committed_revision,
        qualified_at=row.qualified_at,
        qualification_expires_at=row.qualification_expires_at,
        last_valid_outcome_at=row.last_valid_outcome_at,
        qualification_scope=copy.deepcopy(row.qualification_scope),
        retained_until=row.retained_until,
    )


def _lease_conditions(lease: Lease):
    state = MLLifecycleStateRecord
    return (
        state.id == 1,
        state.epoch == lease.epoch,
        state.lease_owner == lease.owner,
        state.fence == lease.fence,
        state.lease_expires_at > func.clock_timestamp(),
        state.maintenance_enabled.is_(True),
    )


def _cas_statement(lease: Lease, revision: int, component: str, incumbent_hash: str | None):
    incumbent = (
        select(MLArtifactRecord.artifact_hash)
        .where(
            MLArtifactRecord.epoch == lease.epoch,
            MLArtifactRecord.component == component,
            MLArtifactRecord.role == "incumbent",
        )
        .scalar_subquery()
    )
    return (
        update(MLLifecycleStateRecord)
        .where(
            *_lease_conditions(lease),
            MLLifecycleStateRecord.revision == revision,
            incumbent.is_not_distinct_from(incumbent_hash),
        )
        .values(revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value())
        .returning(MLLifecycleStateRecord.revision)
    )


def _can_publish(
    state: LifecycleSnapshot,
    lease: Lease,
    revision: int,
    component: str,
    incumbent_hash: str | None,
    now: dt.datetime,
) -> bool:
    incumbent = state.reference(component)
    return (
        state.epoch == lease.epoch
        and state.revision == revision
        and state.lease_owner == lease.owner
        and state.fence == lease.fence
        and state.lease_expires_at is not None
        and state.lease_expires_at > now
        and state.maintenance_enabled
        and (incumbent.artifact_hash if incumbent else None) == incumbent_hash
    )


def _artifact_entry_identity(reference: ArtifactReference) -> tuple[int, ...] | None:
    try:
        metadata = safe_persistence.artifact_path(
            reference.artifact_hash, resolve_entry=False
        ).lstat()
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    except (OSError, ValueError):
        return None


def verify_reference(reference: ArtifactReference) -> tuple[SignedArtifact, bytes]:
    entry_before = _artifact_entry_identity(reference)
    try:
        schema = LIFECYCLE_SCHEMAS.get(reference.component)
        if schema is None or schema != reference.schema_version:
            raise ValueError("Artifact schema mismatch")
        if entry_before is not None and entry_before[2] not in {stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError("Artifact entry is not a regular file")
        artifact, payload = safe_persistence.verify_signed_artifact(
            reference.artifact_hash, component=reference.component, schema_version=schema
        )
        if artifact.manifest != reference.manifest:
            raise ValueError("Committed artifact manifest mismatch")
        return artifact, payload
    except OSError as error:
        entry_after = _artifact_entry_identity(reference)
        if (
            entry_before is not None
            and entry_before == entry_after
            and entry_before[2] != stat.S_IFREG
        ):
            raise ArtifactIntegrityError(
                reference, ValueError("Confirmed non-file artifact entry")
            ) from error
        raise ArtifactUnavailableError(reference, error) from error
    except ValueError as error:
        if entry_before is not None and entry_before != _artifact_entry_identity(reference):
            raise ArtifactUnavailableError(
                reference, OSError("Artifact entry changed during verification")
            ) from error
        raise ArtifactIntegrityError(reference, error) from error


def _load_reference(reference: ArtifactReference) -> Any:
    verify_reference(reference)
    try:
        return safe_persistence.load_signed_artifact(
            reference.artifact_hash,
            component=reference.component,
            schema_version=reference.schema_version,
        )
    except OSError as error:
        raise ArtifactUnavailableError(reference, error) from error
    except (ValueError, pickle.UnpicklingError, EOFError) as error:
        raise ArtifactIntegrityError(reference, error) from error


def _artifact_retry_due(state: LifecycleSnapshot, reference: ArtifactReference) -> bool:
    checkpoint = (state.checkpoints or {}).get("availability:" + reference.artifact_hash, {})
    return checkpoint.get("status") != "unavailable" or _now() >= dt.datetime.fromisoformat(
        checkpoint["next_retry_at"]
    )


def _confirm_artifact_missing(reference: ArtifactReference) -> bool:
    try:
        path = safe_persistence.artifact_path(reference.artifact_hash)
        with os.scandir(path.parent) as entries:
            next(entries, None)
        try:
            path.stat()
        except FileNotFoundError:
            return True
    except (OSError, ValueError):
        return False
    return False


def _bounded_artifact_page(
    directory: Path, epoch: str, checkpoint: dict[str, Any]
) -> tuple[list[Path], dict[str, Any]]:
    global _ARTIFACT_INVENTORY
    previous = {key: value for key, value in checkpoint.items() if key != "at"}
    with _ARTIFACT_INVENTORY_LOCK:
        inventory = _ARTIFACT_INVENTORY
        if inventory is not None and (
            inventory.directory != directory
            or inventory.epoch != epoch
            or inventory.progress != previous
        ):
            inventory.entries.close()
            inventory = _ARTIFACT_INVENTORY = None
        if inventory is None:
            resume = not previous.get("complete", False)
            inventory = _ArtifactInventory(
                directory,
                epoch,
                os.scandir(directory),
                {
                    "scan_id": previous.get("scan_id", str(uuid.uuid4()))
                    if resume
                    else str(uuid.uuid4()),
                    "cursor": previous.get("cursor", "") if resume else "",
                    "entries_seen": previous.get("entries_seen", 0) if resume else 0,
                    "replay_to": (previous.get("replay_to") or previous.get("cursor", ""))
                    if resume
                    else "",
                    "complete": False,
                },
            )
            _ARTIFACT_INVENTORY = inventory
        progress = dict(inventory.progress)
        paths = []
        try:
            for _entry_number in range(RETENTION_BATCH_SIZE):
                try:
                    entry = next(inventory.entries)
                except StopIteration:
                    progress["complete"] = True
                    break
                progress["entries_seen"] += 1
                progress["cursor"] = entry.name
                if progress["replay_to"]:
                    if entry.name == progress["replay_to"]:
                        progress["replay_to"] = ""
                    continue
                paths.append(directory / entry.name)
        except OSError:
            inventory.entries.close()
            _ARTIFACT_INVENTORY = None
            raise
        inventory.progress = progress
        if progress["complete"]:
            inventory.entries.close()
            _ARTIFACT_INVENTORY = None
        return paths, progress


def reference_is_qualified(
    reference: ArtifactReference | None,
    *,
    now: dt.datetime | None = None,
    scope: dict[str, Any] | None = None,
) -> bool:
    from packages.ml.forecast_quality import NATIVE_POLICY_SCHEMA, QUALIFICATION_POLICY_SCHEMA

    now = now or _now()
    if reference is None:
        return False
    expected_scope = reference.manifest.get("scope")
    qualification = reference.qualification_scope or {}
    return bool(
        reference.role == "incumbent"
        and reference.schema_version == LIFECYCLE_SCHEMAS.get(reference.component)
        and reference.qualified_at is not None
        and reference.qualified_at <= now
        and reference.last_valid_outcome_at is not None
        and reference.qualification_expires_at is not None
        and now
        < reference.qualification_expires_at
        <= reference.last_valid_outcome_at + QUALIFICATION_MAX_AGE
        and qualification.get("policy_schema") == NATIVE_POLICY_SCHEMA
        and qualification.get("qualification_policy") == QUALIFICATION_POLICY_SCHEMA
        and isinstance(expected_scope, dict)
        and all(qualification.get(key) == value for key, value in expected_scope.items())
        and (scope is None or scope == expected_scope)
        and qualification.get("horizons")
        and qualification.get("capabilities")
    )


class Lifecycle:
    def __init__(self, session_factory: Callable = get_session) -> None:
        self._sessions = session_factory

    async def _read(self, session: AsyncSession) -> LifecycleSnapshot:
        rows = (
            await session.execute(
                select(MLLifecycleStateRecord, MLArtifactRecord)
                .outerjoin(
                    MLArtifactRecord,
                    and_(
                        MLArtifactRecord.epoch == MLLifecycleStateRecord.epoch,
                        MLArtifactRecord.role.in_(PROTECTED_ROLES),
                    ),
                )
                .where(MLLifecycleStateRecord.id == 1)
            )
        ).all()
        if not rows:
            raise LifecycleUnavailable("lifecycle_not_initialized")
        state = rows[0][0]
        return LifecycleSnapshot(
            state.epoch,
            state.revision,
            state.fence,
            state.maintenance_enabled,
            state.lease_owner,
            state.lease_expires_at,
            tuple(_reference(artifact) for _, artifact in rows if artifact is not None),
            copy.deepcopy(state.checkpoints),
            state.next_maintenance_at,
            state.last_demand_attempt_at,
        )

    async def read(self, *, session=None) -> LifecycleSnapshot:
        async with self._sessions() if session is None else nullcontext(session) as session:
            return await self._read(session)

    async def initialize(self) -> LifecycleSnapshot:
        async with self._sessions() as session:
            await session.execute(
                insert(MLLifecycleStateRecord)
                .values(
                    id=1,
                    epoch=str(uuid.uuid4()),
                )
                .on_conflict_do_nothing(index_elements=[MLLifecycleStateRecord.id])
            )
            state = await self._read(session)
        return state

    async def set_maintenance_enabled(self, enabled: bool) -> int:
        async with self._sessions() as session:
            revision = (
                await session.execute(
                    update(MLLifecycleStateRecord)
                    .where(
                        MLLifecycleStateRecord.id == 1,
                    )
                    .values(
                        maintenance_enabled=enabled,
                        lease_owner=None,
                        lease_expires_at=None,
                        revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                        fence=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                    )
                    .returning(MLLifecycleStateRecord.revision)
                )
            ).scalar_one_or_none()
            if revision is None:
                raise LifecycleUnavailable("lifecycle_not_initialized")
        return revision

    async def prepare_maintenance(self) -> LifecycleSnapshot | None:
        async with self._sessions() as session:
            setting = (
                await session.execute(
                    select(SettingRecord)
                    .where(SettingRecord.key == "ml_maintenance_enabled")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if setting is None or not get_setting_spec("ml_maintenance_enabled").parse(
                setting.value
            ):
                return None
            await session.execute(
                insert(MLLifecycleStateRecord)
                .values(id=1, epoch=str(uuid.uuid4()))
                .on_conflict_do_nothing(index_elements=[MLLifecycleStateRecord.id])
            )
            await update_maintenance_gate(session, True)
            return await self._read(session)

    async def enqueue(
        self,
        component: str,
        source: str,
        *,
        dedup_key: str | None = None,
        family: str | None = None,
        lease: Lease | None = None,
        automatic: bool = False,
    ) -> MaintenanceRequest | None:
        if component not in LIFECYCLE_SCHEMAS:
            raise ValueError("Unknown maintenance component")
        if not source or len(source) > 32:
            raise ValueError("Maintenance source must be 1..32 characters")
        request_id = str(uuid.uuid4())
        dedup_key = dedup_key or request_id
        if len(dedup_key) > 128:
            raise ValueError("Maintenance dedup key must be at most 128 characters")
        if family is not None and component != "thermal":
            raise ValueError("Maintenance family is only supported for thermal requests")
        if component == "thermal":
            family = _thermal_request_family(
                {"family": family} if family is not None else {}, dedup_key
            )
        async with self._sessions() as session:
            if automatic:
                setting = (
                    await session.execute(
                        select(SettingRecord)
                        .where(SettingRecord.key == "ml_maintenance_enabled")
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if setting is None or not get_setting_spec("ml_maintenance_enabled").parse(
                    setting.value
                ):
                    return None
            if lease is None:
                await session.execute(
                    insert(MLLifecycleStateRecord)
                    .values(id=1, epoch=str(uuid.uuid4()))
                    .on_conflict_do_nothing(index_elements=[MLLifecycleStateRecord.id])
                )
            else:
                await self._lock(session, lease)
            epoch = (
                await session.execute(
                    select(MLLifecycleStateRecord.epoch)
                    .where(MLLifecycleStateRecord.id == 1)
                    .with_for_update()
                )
            ).scalar_one()
            row = (
                await session.execute(
                    insert(MLMaintenanceRequestRecord)
                    .values(
                        id=request_id,
                        epoch=epoch,
                        component=component,
                        source=source,
                        dedup_key=dedup_key,
                        status="queued",
                        details={"family": family} if family is not None else {},
                    )
                    .on_conflict_do_nothing(index_elements=["epoch", "component", "dedup_key"])
                    .returning(MLMaintenanceRequestRecord)
                )
            ).scalar_one_or_none()
            if row is None:
                row = (
                    await session.execute(
                        select(MLMaintenanceRequestRecord).where(
                            MLMaintenanceRequestRecord.epoch == epoch,
                            MLMaintenanceRequestRecord.component == component,
                            MLMaintenanceRequestRecord.dedup_key == dedup_key,
                        )
                    )
                ).scalar_one()
            if component == "thermal" and family is not None:
                if _thermal_request_family(row.details, row.dedup_key) != family:
                    raise ValueError("Maintenance family conflicts with existing request")
            return _request(row)

    async def get_request(self, request_id: str) -> MaintenanceRequest | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(MLMaintenanceRequestRecord).where(
                        MLMaintenanceRequestRecord.id == request_id
                    )
                )
            ).scalar_one_or_none()
            return _request(row) if row is not None else None

    async def begin_cycle(self, lease: Lease) -> bool:
        state = MLLifecycleStateRecord
        async with self._sessions() as session:
            await self._lock(session, lease)
            due = (
                await session.execute(
                    update(state)
                    .where(
                        *_lease_conditions(lease),
                        or_(
                            state.next_maintenance_at.is_(None),
                            state.next_maintenance_at <= func.clock_timestamp(),
                        ),
                    )
                    .values(
                        next_maintenance_at=func.date_bin(
                            literal(MAINTENANCE_CADENCE, type_=Interval()),
                            func.clock_timestamp(),
                            literal(dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)),
                        )
                        + literal(MAINTENANCE_CADENCE, type_=Interval())
                    )
                    .returning(state.id)
                )
            ).scalar_one_or_none()
            if due is None:
                return False
            await session.execute(
                update(MLMaintenanceRequestRecord)
                .where(
                    MLMaintenanceRequestRecord.epoch == lease.epoch,
                    MLMaintenanceRequestRecord.status == "running",
                    MLMaintenanceRequestRecord.fence.is_distinct_from(lease.fence),
                )
                .values(
                    status="queued",
                    fence=None,
                    details=MLMaintenanceRequestRecord.details.op("||")(
                        {"reason": "owner_restarted"}
                    ),
                )
            )
        return True

    async def claim_request(self, lease: Lease, component: str) -> MaintenanceRequest | None:
        async with self._sessions() as session:
            await self._lock(session, lease)
            snapshot = await self._read(session)
            if snapshot.reference(component, "candidate") is not None:
                return None
            row = (
                await session.execute(
                    select(MLMaintenanceRequestRecord)
                    .where(
                        MLMaintenanceRequestRecord.epoch == lease.epoch,
                        MLMaintenanceRequestRecord.component == component,
                        MLMaintenanceRequestRecord.status == "queued",
                    )
                    .order_by(
                        MLMaintenanceRequestRecord.requested_at, MLMaintenanceRequestRecord.id
                    )
                    .with_for_update()
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if component == "demand":
                allowed = (
                    await session.execute(
                        update(MLLifecycleStateRecord)
                        .where(
                            *_lease_conditions(lease),
                            or_(
                                MLLifecycleStateRecord.last_demand_attempt_at.is_(None),
                                MLLifecycleStateRecord.last_demand_attempt_at
                                <= func.clock_timestamp()
                                - literal(DEMAND_CADENCE, type_=Interval()),
                            ),
                        )
                        .values(last_demand_attempt_at=func.clock_timestamp())
                        .returning(MLLifecycleStateRecord.id)
                    )
                ).scalar_one_or_none()
                if allowed is None:
                    return None
            claimed = (
                await session.execute(
                    update(MLMaintenanceRequestRecord)
                    .where(MLMaintenanceRequestRecord.id == row.id)
                    .values(
                        status="running",
                        fence=lease.fence,
                        attempted_at=func.clock_timestamp(),
                        finished_at=None,
                        details={"family": row.details["family"]}
                        if "family" in row.details
                        else {},
                    )
                    .returning(MLMaintenanceRequestRecord)
                )
            ).scalar_one()
        return _request(claimed)

    async def defer_component(self, lease: Lease, component: str, reason: str) -> None:
        async with self._sessions() as session:
            await self._lock(session, lease)
            await session.execute(
                update(MLMaintenanceRequestRecord)
                .where(
                    MLMaintenanceRequestRecord.epoch == lease.epoch,
                    MLMaintenanceRequestRecord.component == component,
                    MLMaintenanceRequestRecord.status == "queued",
                )
                .values(details=MLMaintenanceRequestRecord.details.op("||")({"reason": reason}))
            )

    async def finish_request(
        self, lease: Lease, request_id: str, status: str, details: dict[str, Any]
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "queued"}:
            raise ValueError("Invalid maintenance completion status")
        if "family" in details:
            raise ValueError("Maintenance completion cannot change family identity")
        async with self._sessions() as session:
            await self._lock(session, lease)
            result = await session.execute(
                update(MLMaintenanceRequestRecord)
                .where(
                    MLMaintenanceRequestRecord.id == request_id,
                    MLMaintenanceRequestRecord.epoch == lease.epoch,
                    MLMaintenanceRequestRecord.fence == lease.fence,
                    MLMaintenanceRequestRecord.status == "running",
                )
                .values(
                    status=status,
                    details=MLMaintenanceRequestRecord.details.op("||")(details),
                    finished_at=func.clock_timestamp() if status != "queued" else None,
                    fence=lease.fence if status != "queued" else None,
                )
            )
            if result.rowcount != 1:
                raise LeaseLost("maintenance_request_lost")

    async def release(self, lease: Lease) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(
                    MLLifecycleStateRecord.id == 1,
                    MLLifecycleStateRecord.epoch == lease.epoch,
                    MLLifecycleStateRecord.lease_owner == lease.owner,
                    MLLifecycleStateRecord.fence == lease.fence,
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                    fence=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                )
            )

    async def invalidate_candidate(
        self, lease: Lease, component: str, artifact_hash: str, *, reason: str
    ) -> None:
        if reason not in {"integrity", "schema", "domain"}:
            raise ValueError("Candidate invalidation requires integrity, schema or domain evidence")
        async with self._sessions() as session:
            await self._lock(session, lease)
            result = await session.execute(
                update(MLArtifactRecord)
                .where(
                    MLArtifactRecord.epoch == lease.epoch,
                    MLArtifactRecord.component == component,
                    MLArtifactRecord.artifact_hash == artifact_hash,
                    MLArtifactRecord.role == "candidate",
                )
                .values(role="unreferenced")
            )
            if result.rowcount != 1:
                raise CASConflict("candidate_reference_changed")
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value())
            )
        logger.info("ml_candidate_invalidated", component=component, reason=reason)

    @staticmethod
    def _ttl(ttl: dt.timedelta) -> dt.timedelta:
        if not dt.timedelta() < ttl <= dt.timedelta(minutes=5):
            raise ValueError("Lease duration must be positive and at most five minutes")
        return ttl

    async def acquire(
        self, owner: str, ttl: dt.timedelta = dt.timedelta(seconds=90)
    ) -> Lease | None:
        if not owner or len(owner) > 128:
            raise ValueError("Lease owner is required")
        state = MLLifecycleStateRecord
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        update(state)
                        .where(
                            state.id == 1,
                            state.maintenance_enabled.is_(True),
                            or_(
                                state.lease_owner.is_(None),
                                state.lease_expires_at <= func.clock_timestamp(),
                            ),
                        )
                        .values(
                            lease_owner=owner,
                            lease_expires_at=func.clock_timestamp()
                            + literal(self._ttl(ttl), type_=Interval()),
                            revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                            fence=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                        )
                        .returning(state.epoch, state.revision, state.fence, state.lease_expires_at)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return Lease(row["epoch"], owner, row["fence"], row["revision"], row["lease_expires_at"])

    async def renew(self, lease: Lease, ttl: dt.timedelta = dt.timedelta(seconds=90)) -> Lease:
        async with self._sessions() as session:
            expiry = (
                await session.execute(
                    update(MLLifecycleStateRecord)
                    .where(
                        *_lease_conditions(lease),
                    )
                    .values(
                        lease_expires_at=func.clock_timestamp()
                        + literal(self._ttl(ttl), type_=Interval()),
                    )
                    .returning(MLLifecycleStateRecord.lease_expires_at)
                )
            ).scalar_one_or_none()
            if expiry is None:
                raise LeaseLost("lease_lost")
        return Lease(lease.epoch, lease.owner, lease.fence, lease.revision, expiry)

    async def _lock(self, session: AsyncSession, lease: Lease) -> None:
        row = (
            await session.execute(
                select(MLLifecycleStateRecord.id)
                .where(
                    *_lease_conditions(lease),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LeaseLost("lease_lost")

    async def publish(
        self,
        lease: Lease,
        artifact: SignedArtifact,
        *,
        expected_revision: int,
        expected_incumbent: str | None,
        role: str = "candidate",
        qualification: Qualification | None = None,
        on_committed: Callable[[ArtifactReference], None] | None = None,
    ) -> PublicationResult:
        safe_persistence._require_publication_owner()
        if role not in {"candidate", "incumbent"}:
            raise ValueError("Publication role must be candidate or incumbent")
        if LIFECYCLE_SCHEMAS.get(artifact.component) != artifact.schema_version:
            raise ValueError("Artifact schema mismatch")
        if qualification is not None and (
            role != "incumbent"
            or not qualification.scope
            or not qualification.last_valid_outcome_at
            <= qualification.qualified_at
            < qualification.expires_at
            or qualification.qualified_at > _now()
        ):
            raise ValueError("Invalid artifact qualification")
        prepared = False
        committed_revision = None
        reference = None
        try:
            async with self._sessions() as session:
                if artifact.component == "demand":
                    from packages.ml.forecast_quality import locked_demand_scope

                    async with locked_demand_scope(
                        artifact.manifest["device_id"], session=session
                    ) as (current_scope, _):
                        if current_scope != artifact.manifest.get("scope"):
                            raise CASConflict("demand_scope_mismatch")
                elif artifact.component == "comfort" or (
                    artifact.component == "thermal"
                    and any(
                        str(capability).startswith("indoor_")
                        for capability in artifact.manifest.get("capabilities", ())
                    )
                ):
                    from packages.core.house_contract import _lock_contract
                    from packages.ml.forecast_quality import materialize_native_inputs, native_scope

                    await _lock_contract(session)
                    inputs = await materialize_native_inputs(
                        artifact.component,
                        now=_now(),
                        device_id=artifact.manifest["device_id"],
                        capabilities=tuple(artifact.manifest.get("capabilities", ())),
                        session=session,
                    )
                    if inputs is None:
                        raise CASConflict("indoor_scope_unavailable")
                    current_scope = native_scope(inputs)
                    artifact_scope = artifact.manifest.get("scope")
                    dependency_keys = (
                        "component_dependency_schema",
                        "component_dependency_generation",
                        "component_dependency_fingerprint",
                    )
                    if not isinstance(artifact_scope, dict) or any(
                        current_scope.get(key) != artifact_scope.get(key) for key in dependency_keys
                    ):
                        raise CASConflict("indoor_dependency_scope_mismatch")
                await self._lock(session, lease)
                state = await self._read(session)
                if not _can_publish(
                    state, lease, expected_revision, artifact.component, expected_incumbent, _now()
                ):
                    raise CASConflict("publication_conflict")
                candidate = state.reference(artifact.component, "candidate")
                if (
                    candidate is not None
                    and candidate.artifact_hash != artifact.artifact_hash
                    and not (role == "incumbent" and artifact.artifact_hash == expected_incumbent)
                ):
                    raise CASConflict("candidate_is_frozen")
                verified, _ = await asyncio.to_thread(
                    safe_persistence.certify_durable_artifact,
                    artifact.artifact_hash,
                    component=artifact.component,
                    schema_version=artifact.schema_version,
                )
                if verified.manifest != artifact.manifest:
                    raise ValueError("Artifact manifest mismatch")
                existing = (
                    await session.execute(
                        select(MLArtifactRecord).where(
                            MLArtifactRecord.artifact_hash == artifact.artifact_hash,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None and (
                    existing.epoch != lease.epoch
                    or existing.component != artifact.component
                    or existing.schema_version != artifact.schema_version
                    or existing.manifest != artifact.manifest
                ):
                    raise CASConflict("artifact_identity_conflict")
                committed_revision = (
                    await session.execute(
                        _cas_statement(
                            lease, expected_revision, artifact.component, expected_incumbent
                        )
                    )
                ).scalar_one_or_none()
                if committed_revision is None:
                    raise CASConflict("publication_conflict")
                if role == "incumbent" and expected_incumbent != artifact.artifact_hash:
                    await session.execute(
                        update(MLArtifactRecord)
                        .where(
                            MLArtifactRecord.epoch == lease.epoch,
                            MLArtifactRecord.component == artifact.component,
                            MLArtifactRecord.role == "incumbent",
                        )
                        .values(role="rollback")
                    )
                values = {
                    "role": role,
                    "committed_revision": committed_revision,
                    "qualified_at": qualification.qualified_at if qualification else None,
                    "qualification_expires_at": qualification.expires_at if qualification else None,
                    "last_valid_outcome_at": qualification.last_valid_outcome_at
                    if qualification
                    else None,
                    "qualification_scope": qualification.scope if qualification else {},
                }
                if existing is None:
                    await session.execute(
                        insert(MLArtifactRecord).values(
                            artifact_hash=artifact.artifact_hash,
                            epoch=lease.epoch,
                            component=artifact.component,
                            schema_version=artifact.schema_version,
                            fit_id=artifact.manifest["fit_id"],
                            manifest=artifact.manifest,
                            **values,
                        )
                    )
                else:
                    await session.execute(
                        update(MLArtifactRecord)
                        .where(
                            MLArtifactRecord.artifact_hash == artifact.artifact_hash,
                        )
                        .values(**values)
                    )
                reference = ArtifactReference(
                    artifact.artifact_hash,
                    lease.epoch,
                    artifact.component,
                    artifact.schema_version,
                    artifact.manifest,
                    role,
                    committed_revision,
                    values["qualified_at"],
                    values["qualification_expires_at"],
                    values["last_valid_outcome_at"],
                    values["qualification_scope"],
                )
                prepared = True
        except (CASConflict, LeaseLost) as error:
            return PublicationResult(False, reason=str(error))
        except Exception:
            if not prepared:
                raise
            try:
                recovered = await self.read()
                current = recovered.reference(artifact.component, role)
            except Exception as read_error:
                raise LifecycleUnavailable("ambiguous_commit_readback_failed") from read_error
            if (
                recovered.epoch != lease.epoch
                or recovered.revision != committed_revision
                or current is None
                or current.artifact_hash != artifact.artifact_hash
                or current.committed_revision != committed_revision
            ):
                logger.warning("ml_publication_not_confirmed", component=artifact.component)
                return PublicationResult(False, readback=True, reason="commit_not_confirmed")
            await asyncio.to_thread(verify_reference, current)
            if on_committed:
                on_committed(current)
            logger.info("ml_publication_readback_confirmed", component=artifact.component)
            return PublicationResult(True, committed_revision, readback=True)
        if on_committed:
            on_committed(reference)
        return PublicationResult(True, committed_revision)

    async def load_committed(
        self, component: str, *, role: str = "incumbent", expected_hash: str | None = None
    ) -> Any:
        if role not in {"incumbent", "candidate"}:
            raise ValueError("Only current committed references may be loaded")
        state = await self.read()
        reference = state.reference(component, role)
        if reference is None or (
            expected_hash is not None and reference.artifact_hash != expected_hash
        ):
            raise LifecycleUnavailable("no_committed_artifact")
        return await asyncio.to_thread(_load_reference, reference)

    async def load_frozen_comparator(self, component: str, artifact_hash: str) -> Any:
        state = await self.read()
        reference = next(
            (
                ref
                for ref in state.references
                if ref.component == component and ref.artifact_hash == artifact_hash
            ),
            None,
        )
        if reference is None:
            raise LifecycleUnavailable("frozen_comparator_unavailable")
        return await asyncio.to_thread(_load_reference, reference)

    async def record_native_issues(
        self,
        lease: Lease,
        reference: ArtifactReference,
        issues: list[dict[str, Any]],
        *,
        slot: int,
    ) -> None:
        safe_persistence._require_publication_owner()
        if not 0 <= slot <= 10_000_000 or not issues or len(issues) > 100:
            raise ValueError("Invalid native collection batch")
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            if not any(ref.artifact_hash == reference.artifact_hash for ref in state.references):
                raise CASConflict("collection_reference_changed")
            checkpoints = dict(state.checkpoints or {})
            key = "collection:" + reference.artifact_hash
            previous = checkpoints.get(key, {})
            last_slot = previous.get("last_slot", -1)
            if slot < last_slot:
                raise CASConflict("collection_slot_rewound")
            for values in issues:
                if (
                    values["epoch"] != lease.epoch
                    or values["component"] != reference.component
                    or values["candidate_hash"] != reference.artifact_hash
                ):
                    raise ValueError("Issue scope does not match the committed fit")
                issue_id = (
                    await session.execute(
                        insert(MLForecastIssueRecord)
                        .values(**values)
                        .on_conflict_do_nothing(constraint="uq_ml_issue_cohort")
                        .returning(MLForecastIssueRecord.id)
                    )
                ).scalar_one_or_none()
                if issue_id is None:
                    existing = (
                        await session.execute(
                            select(MLForecastIssueRecord).where(
                                MLForecastIssueRecord.epoch == lease.epoch,
                                MLForecastIssueRecord.device_id == values["device_id"],
                                MLForecastIssueRecord.component == values["component"],
                                MLForecastIssueRecord.cohort_key == values["cohort_key"],
                                MLForecastIssueRecord.horizon_minutes == values["horizon_minutes"],
                            )
                        )
                    ).scalar_one()
                    if (
                        existing.snapshot_hash != values["snapshot_hash"]
                        or existing.predictions != values["predictions"]
                        or existing.candidate_hash != values["candidate_hash"]
                        or existing.incumbent_hash != values["incumbent_hash"]
                    ):
                        raise CASConflict("immutable_issue_conflict")
                    issue_id = existing.id
                await session.execute(
                    insert(MLForecastOutcomeRecord)
                    .values(
                        issue_id=issue_id,
                        epoch=lease.epoch,
                        device_id=values["device_id"],
                        component=values["component"],
                        horizon_minutes=values["horizon_minutes"],
                        canonical_key=values["cohort_key"],
                        status="pending",
                        result={},
                    )
                    .on_conflict_do_nothing(index_elements=[MLForecastOutcomeRecord.issue_id])
                )
            checkpoints[key] = {
                "last_slot": slot,
                "last_issue_at": max(row["issued_at"] for row in issues).isoformat(),
                "gaps": previous.get("gaps", 0) + max(0, slot - last_slot - 1),
                "collected_slots": previous.get(
                    "collected_slots", last_slot + 1 - previous.get("gaps", 0)
                )
                + int(slot > last_slot),
                "expected_slots": slot + 1,
                "complete": slot <= last_slot + 1,
            }
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )

    async def begin_evidence_scan(
        self,
        lease: Lease,
        reference: ArtifactReference,
        *,
        key: str,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> dict[str, Any]:
        safe_persistence._require_publication_owner()
        if window_start.tzinfo is None or window_end.tzinfo is None or window_end < window_start:
            raise ValueError("Invalid complete evidence window")
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoints = dict(state.checkpoints or {})
            previous = checkpoints.get(key)
            if previous is not None and not previous["complete"]:
                return dict(previous)
            issue = MLForecastIssueRecord
            high_water, total = (
                await session.execute(
                    select(func.max(issue.id), func.count(issue.id)).where(
                        issue.epoch == lease.epoch,
                        issue.component == reference.component,
                        issue.candidate_hash == reference.artifact_hash,
                        issue.issued_at >= window_start,
                        issue.issued_at <= window_end,
                    )
                )
            ).one()
            checkpoint = {
                "candidate_hash": reference.artifact_hash,
                "component": reference.component,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "high_water": int(high_water or 0),
                "cursor": 0,
                "expected_rows": int(total),
                "processed_rows": 0,
                "complete": not total,
            }
            checkpoints[key] = checkpoint
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )
            return checkpoint

    async def evidence_page(
        self, lease: Lease, key: str, *, page_size: int = 100
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ValueError("Evidence pages must contain at most 100 issues")
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoint = dict((state.checkpoints or {}).get(key, {}))
            if not checkpoint:
                raise LifecycleUnavailable("evidence_scan_not_started")
            issue = MLForecastIssueRecord
            rows = []
            if not checkpoint["complete"]:
                materialized = (
                    await session.execute(
                        select(issue, MLForecastOutcomeRecord)
                        .outerjoin(
                            MLForecastOutcomeRecord, MLForecastOutcomeRecord.issue_id == issue.id
                        )
                        .where(
                            issue.epoch == lease.epoch,
                            issue.component == checkpoint["component"],
                            issue.candidate_hash == checkpoint["candidate_hash"],
                            issue.issued_at
                            >= dt.datetime.fromisoformat(checkpoint["window_start"]),
                            issue.issued_at <= dt.datetime.fromisoformat(checkpoint["window_end"]),
                            issue.id > checkpoint["cursor"],
                            issue.id <= checkpoint["high_water"],
                        )
                        .order_by(issue.id)
                        .limit(page_size)
                    )
                ).all()
                for record, outcome in materialized:
                    rows.append(
                        {
                            "issue": {
                                column.name: getattr(record, column.name)
                                for column in issue.__table__.columns
                            },
                            "outcome": {
                                column.name: getattr(outcome, column.name)
                                for column in MLForecastOutcomeRecord.__table__.columns
                            }
                            if outcome
                            else None,
                        }
                    )
            complete = checkpoint["complete"] or len(rows) < page_size
            return {
                "rows": rows,
                "checkpoint": checkpoint,
                "complete": complete,
                "cursor": checkpoint["high_water"] if complete else rows[-1]["issue"]["id"],
            }

    async def commit_evidence_page(
        self,
        lease: Lease,
        key: str,
        page: dict[str, Any],
        outcomes: list[dict[str, Any]],
    ) -> None:
        safe_persistence._require_publication_owner()
        if len(outcomes) != len(page["rows"]) or len(outcomes) > 100:
            raise ValueError("Every page issue needs one outcome")
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoints = dict(state.checkpoints or {})
            if checkpoints.get(key) != page["checkpoint"]:
                raise CASConflict("evidence_page_progress_conflict")
            for entry, outcome in zip(page["rows"], outcomes):
                issue = entry["issue"]
                values = {
                    "issue_id": issue["id"],
                    "epoch": lease.epoch,
                    "device_id": issue["device_id"],
                    "component": issue["component"],
                    "horizon_minutes": issue["horizon_minutes"],
                    "canonical_key": issue["cohort_key"],
                    **outcome,
                    "updated_at": func.clock_timestamp(),
                }
                if (
                    issue["epoch"] != lease.epoch
                    or issue["candidate_hash"] != page["checkpoint"]["candidate_hash"]
                ):
                    raise ValueError("Evidence page scope mismatch")
                table = MLForecastOutcomeRecord
                await session.execute(
                    insert(table)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[table.issue_id],
                        set_={
                            name: values[name]
                            for name in (
                                "status",
                                "exclusion_reason",
                                "observed_at",
                                "result",
                                "updated_at",
                            )
                        },
                        where=or_(
                            table.status.in_(("pending", "incomplete")),
                            and_(
                                table.status == "valid",
                                outcome["status"] in ("excluded", "incomplete"),
                            ),
                        ),
                    )
                )
            checkpoint = {
                **page["checkpoint"],
                "cursor": page["cursor"],
                "processed_rows": page["checkpoint"]["processed_rows"] + len(page["rows"]),
                "complete": page["complete"],
            }
            if (
                checkpoint["complete"]
                and checkpoint["processed_rows"] != checkpoint["expected_rows"]
            ):
                raise CASConflict("evidence_window_incomplete")
            checkpoints[key] = checkpoint
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )

    async def claim_decision(
        self, lease: Lease, reference: ArtifactReference, checkpoint: int
    ) -> bool:
        safe_persistence._require_publication_owner()
        from packages.ml.forecast_quality import DECISION_OFFSETS_HOURS

        if not 0 <= checkpoint < len(DECISION_OFFSETS_HOURS):
            raise ValueError("Unknown predeclared decision checkpoint")
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoints = dict(state.checkpoints or {})
            key = f"decision:{reference.artifact_hash}:{checkpoint}"
            if key in checkpoints:
                return False
            frozen_at = dt.datetime.fromisoformat(reference.manifest["frozen_at"])
            if _now() < frozen_at + dt.timedelta(hours=DECISION_OFFSETS_HOURS[checkpoint]):
                return False
            checkpoints[key] = {
                "status": "consumed",
                "fence": lease.fence,
                "at": _now().isoformat(),
            }
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )
            return True

    async def finish_decision(
        self,
        lease: Lease,
        reference: ArtifactReference,
        checkpoint: int,
        result: dict[str, Any],
    ) -> None:
        safe_persistence._require_publication_owner()
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoints = dict(state.checkpoints or {})
            key = f"decision:{reference.artifact_hash}:{checkpoint}"
            if checkpoints.get(key, {}).get("fence") != lease.fence:
                raise CASConflict("decision_ownership_changed")
            checkpoints[key] = {**checkpoints[key], "status": "evaluated", "result": result}
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )

    async def record_artifact_unavailability(
        self, lease: Lease, error: ArtifactUnavailableError
    ) -> bool:
        safe_persistence._require_publication_owner()
        reference = error.reference
        now = _now()
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            if reference not in state.references:
                raise CASConflict("availability_reference_changed")
            checkpoints = dict(state.checkpoints or {})
            key = "availability:" + reference.artifact_hash
            previous = checkpoints.get(key, {})
            if previous.get("status") == "unavailable" and not _artifact_retry_due(
                state, reference
            ):
                return False
            attempts = min(previous.get("attempts", 0) + 1, ARTIFACT_UNAVAILABLE_ATTEMPTS)
            confirmed = bool(
                error.missing
                and attempts == ARTIFACT_UNAVAILABLE_ATTEMPTS
                and await asyncio.to_thread(_confirm_artifact_missing, reference)
            )
            delay = min(MAINTENANCE_CADENCE * 2 ** (attempts - 1), ARTIFACT_RETRY_MAX)
            checkpoint = {
                "status": "confirmed_missing" if confirmed else "unavailable",
                "component": reference.component,
                "role": reference.role,
                "attempts": attempts,
                "first_unavailable_at": previous.get("first_unavailable_at", now.isoformat()),
                "last_attempt_at": now.isoformat(),
                "next_retry_at": (now + delay).isoformat(),
                "error_type": error.error_type,
                "fence": lease.fence,
            }
            checkpoints[key] = checkpoint
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )
        logger.warning(
            "ml_artifact_unavailable",
            component=reference.component,
            artifact_hash=reference.artifact_hash,
            role=reference.role,
            attempts=attempts,
            next_retry_at=checkpoint["next_retry_at"],
            confirmed_missing=confirmed,
            error_type=error.error_type,
            fence=lease.fence,
        )
        return confirmed

    async def clear_artifact_unavailability(
        self, lease: Lease, reference: ArtifactReference
    ) -> None:
        safe_persistence._require_publication_owner()
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            if reference.epoch != state.epoch or not any(
                current.artifact_hash == reference.artifact_hash
                and current.component == reference.component
                for current in state.references
            ):
                raise CASConflict("availability_reference_changed")
            checkpoints = dict(state.checkpoints or {})
            key = "availability:" + reference.artifact_hash
            previous = checkpoints.get(key, {})
            if previous.get("status") != "unavailable":
                return
            checkpoints[key] = {
                "status": "available",
                "attempts": 0,
                "recovered_at": _now().isoformat(),
            }
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )
        logger.info(
            "ml_artifact_recovered",
            component=reference.component,
            artifact_hash=reference.artifact_hash,
        )

    async def invalidate_component(
        self, lease: Lease, reference: ArtifactReference, reason: str
    ) -> None:
        safe_persistence._require_publication_owner()
        integrity_failure = reason == "artifact_integrity_failure"
        damage_confirmed = integrity_failure or reason == "artifact_missing_confirmed"
        entry_before = _artifact_entry_identity(reference) if damage_confirmed else None
        retain_history = bool(
            reference.role == "incumbent"
            and reference.qualified_at is not None
            and reference.qualification_expires_at is not None
            and reference.last_valid_outcome_at is not None
        )
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            if reference not in state.references:
                raise CASConflict("invalidation_reference_changed")
            if damage_confirmed:
                if entry_before != _artifact_entry_identity(reference):
                    raise CASConflict("invalidation_artifact_entry_changed")
                if integrity_failure:
                    try:
                        _artifact, payload = await asyncio.to_thread(verify_reference, reference)
                        await asyncio.to_thread(pickle.loads, payload)
                    except (ValueError, pickle.UnpicklingError, EOFError):
                        pass
                    else:
                        raise CASConflict("invalidation_artifact_integrity_recovered")
                elif not await asyncio.to_thread(_confirm_artifact_missing, reference):
                    raise ArtifactUnavailableError(
                        reference, OSError("Artifact missing confirmation is no longer valid")
                    )
                if entry_before != _artifact_entry_identity(reference):
                    raise CASConflict("invalidation_artifact_entry_changed")
            checkpoints = dict(state.checkpoints or {})
            checkpoints["invalidated:" + reference.artifact_hash] = {
                "component": reference.component,
                "role": reference.role,
                "reason": reason,
                "at": _now().isoformat(),
                "fence": lease.fence,
            }
            revision = (
                await session.execute(
                    update(MLLifecycleStateRecord)
                    .where(*_lease_conditions(lease))
                    .values(
                        revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                        checkpoints=checkpoints,
                    )
                    .returning(MLLifecycleStateRecord.revision)
                )
            ).scalar_one()
            invalidated = await session.execute(
                update(MLArtifactRecord)
                .where(
                    MLArtifactRecord.epoch == lease.epoch,
                    MLArtifactRecord.artifact_hash == reference.artifact_hash,
                    MLArtifactRecord.role == reference.role,
                    MLArtifactRecord.committed_revision == reference.committed_revision,
                )
                .values(
                    role="retained" if reference.role == "candidate" else reference.role,
                    qualified_at=reference.qualified_at if retain_history else None,
                    qualification_expires_at=min(reference.qualification_expires_at, _now())
                    if retain_history and reference.qualification_expires_at is not None
                    else None,
                    last_valid_outcome_at=reference.last_valid_outcome_at
                    if retain_history
                    else None,
                    qualification_scope=reference.qualification_scope if retain_history else {},
                    committed_revision=revision,
                )
            )
            if invalidated.rowcount != 1:
                raise CASConflict("invalidation_reference_changed")
        logger.warning(
            "ml_component_invalidated",
            component=reference.component,
            artifact_hash=reference.artifact_hash,
            role=reference.role,
            fence=lease.fence,
            revision=revision,
            reason=reason,
        )

    async def decision_evidence(
        self, lease: Lease, reference: ArtifactReference, *, window_end: dt.datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        from packages.ml.forecast_quality import MAX_QUALIFICATION_PAIRS

        state = await self.read()
        collection = (state.checkpoints or {}).get("collection:" + reference.artifact_hash, {})
        scan = (state.checkpoints or {}).get("scan:" + reference.artifact_hash, {})
        complete = bool(
            collection.get("complete")
            and scan.get("complete")
            and dt.datetime.fromisoformat(scan["window_end"]) >= window_end
        )
        if not complete:
            return [], False
        frozen_at = dt.datetime.fromisoformat(reference.manifest["frozen_at"])
        high_water, cursor = scan["high_water"], 0
        pairs, expected, actual = [], set(), set()
        issue = MLForecastIssueRecord
        while cursor < high_water:
            async with self._sessions() as session:
                await self._lock(session, lease)
                rows = (
                    await session.execute(
                        select(issue, MLForecastOutcomeRecord)
                        .outerjoin(
                            MLForecastOutcomeRecord,
                            MLForecastOutcomeRecord.issue_id == issue.id,
                        )
                        .where(
                            issue.epoch == lease.epoch,
                            issue.candidate_hash == reference.artifact_hash,
                            issue.component == reference.component,
                            issue.issued_at >= frozen_at,
                            issue.issued_at <= window_end,
                            issue.id > cursor,
                            issue.id <= high_water,
                        )
                        .order_by(issue.id)
                        .limit(100)
                    )
                ).all()
                for record, outcome in rows:
                    cursor = record.id
                    actual.add((record.cohort_key, record.horizon_minutes))
                    expected.update(
                        (record.cohort_key, horizon) for horizon in record.expected_horizons
                    )
                    snapshot = record.input_snapshot
                    outdoor = next(
                        (
                            row["temperature"]
                            for row in snapshot.get("weather", [])
                            if isinstance(row.get("temperature"), (int, float))
                        ),
                        None,
                    )
                    pair = {
                        "issued_at": record.issued_at.isoformat(),
                        "target_at": record.target_at.isoformat(),
                        "device_id": record.device_id,
                        "cohort_key": record.cohort_key,
                        "canonical_key": record.cohort_key,
                        "horizon_minutes": record.horizon_minutes,
                        "candidate_hash": record.candidate_hash,
                        "incumbent_hash": record.incumbent_hash,
                        "issue_kind": record.issue_kind,
                        "policy_schema": record.policy_schema,
                        "scope": snapshot["scope"],
                        "capability": snapshot["capability"],
                        "outdoor_bin": int(outdoor // 2) * 2 if outdoor is not None else "unknown",
                        "status": outcome.status if outcome else "incomplete",
                        "exclusion_reason": outcome.exclusion_reason
                        if outcome
                        else "missing_outcome",
                        "observed_at": outcome.observed_at.isoformat()
                        if outcome and outcome.observed_at
                        else None,
                        **record.predictions,
                    }
                    if outcome and outcome.status == "valid":
                        if (
                            outcome.result.get("candidate_hash") != record.candidate_hash
                            or outcome.result.get("incumbent_hash") != record.incumbent_hash
                            or any(
                                outcome.result.get(key) != value
                                for key, value in record.predictions.items()
                            )
                            or record.incumbent_hash != reference.manifest["frozen_incumbent_hash"]
                        ):
                            complete = False
                        pair.update(outcome.result)
                    pairs.append(pair)
            if len(pairs) > MAX_QUALIFICATION_PAIRS:
                return [], False
            if len(rows) < 100:
                break
        return pairs, complete and actual == expected

    async def reporting_evidence(
        self, reference: ArtifactReference, *, now: dt.datetime, session=None
    ) -> dict[str, Any]:
        from packages.ml.forecast_quality import NATIVE_POLICY_SCHEMA

        issue, outcome = MLForecastIssueRecord, MLForecastOutcomeRecord
        frozen_at = dt.datetime.fromisoformat(reference.manifest["frozen_at"])
        valid = func.coalesce(
            and_(
                outcome.status == "valid",
                outcome.exclusion_reason.is_(None),
                outcome.observed_at <= now,
                issue.target_at <= now,
                func.abs(func.extract("epoch", outcome.observed_at - issue.target_at)) <= 600,
                outcome.result["candidate_hash"].as_string() == issue.candidate_hash,
                outcome.result["incumbent_hash"]
                .as_string()
                .is_not_distinct_from(issue.incumbent_hash),
                *(
                    outcome.result[key].as_float() == issue.predictions[key].as_float()
                    for key in ("candidate", "incumbent", "baseline")
                ),
            ),
            False,
        )
        evidence = (
            select(
                issue.device_id,
                issue.issued_at,
                issue.cohort_key,
                issue.horizon_minutes,
                outcome.status,
                outcome.exclusion_reason,
                outcome.observed_at,
                outcome.result["outdoor_bin"].as_string().label("outdoor_bin"),
                valid.label("valid"),
            )
            .outerjoin(outcome, outcome.issue_id == issue.id)
            .where(
                issue.epoch == reference.epoch,
                issue.component == reference.component,
                issue.device_id == reference.manifest["device_id"],
                issue.candidate_hash == reference.artifact_hash,
                issue.issue_kind == "prospective_native",
                issue.policy_schema == NATIVE_POLICY_SCHEMA,
                issue.issued_at >= frozen_at,
                issue.issued_at <= now,
                issue.input_snapshot["scope"] == reference.manifest["scope"],
                issue.horizon_minutes.in_(reference.manifest["horizons"]),
                issue.input_snapshot["capability"]
                .as_string()
                .in_(reference.manifest["capabilities"]),
            )
            .cte("reporting_evidence")
        )

        def distribution(column, condition):
            grouped = (
                select(column.cast(String).label("bucket"), func.count().label("total"))
                .select_from(evidence)
                .where(condition, column.is_not(None))
                .group_by(column)
                .subquery()
            )
            return select(
                func.jsonb_object_agg(grouped.c.bucket, grouped.c.total)
            ).scalar_subquery()

        statement = select(
            func.count().filter(evidence.c.valid).label("pairs"),
            func.count(
                func.distinct(func.row(evidence.c.device_id, func.date(evidence.c.issued_at)))
            )
            .filter(evidence.c.valid)
            .label("device_day_clusters"),
            func.count(func.distinct(func.row(evidence.c.device_id, evidence.c.cohort_key)))
            .filter(evidence.c.valid)
            .label("issue_cohorts"),
            func.max(evidence.c.observed_at)
            .filter(evidence.c.valid)
            .label("last_valid_outcome_at"),
            func.count()
            .filter(
                or_(evidence.c.status.is_(None), evidence.c.status.in_(("pending", "incomplete")))
            )
            .label("pending_outcomes"),
            distribution(evidence.c.horizon_minutes, evidence.c.valid).label("horizons"),
            distribution(evidence.c.outdoor_bin, evidence.c.valid).label("outdoor_bins"),
            distribution(
                func.coalesce(evidence.c.exclusion_reason, "invalid_pair"),
                or_(
                    evidence.c.status == "excluded",
                    and_(evidence.c.status == "valid", ~evidence.c.valid),
                ),
            ).label("exclusions"),
        ).select_from(evidence)
        async with self._sessions() if session is None else nullcontext(session) as session:
            result = (await session.execute(statement)).mappings().one()
        latest = result["last_valid_outcome_at"]
        return {
            "counts": {
                key: result[key] for key in ("pairs", "device_day_clusters", "issue_cohorts")
            }
            | {key: dict(result[key] or {}) for key in ("horizons", "outdoor_bins")},
            "exclusions": dict(result["exclusions"] or {}),
            "pending_outcomes": result["pending_outcomes"],
            "last_valid_outcome_at": latest.isoformat() if latest else None,
            "as_of": now.isoformat(),
        }

    async def retain_artifact(self, lease: Lease, artifact_hash: str, until: dt.datetime) -> None:
        if until.tzinfo is None or until <= _now():
            raise ValueError("Retention requires a future timezone-aware deadline")
        async with self._sessions() as session:
            await self._lock(session, lease)
            result = await session.execute(
                update(MLArtifactRecord)
                .where(
                    MLArtifactRecord.epoch == lease.epoch,
                    MLArtifactRecord.artifact_hash == artifact_hash,
                )
                .values(retained_until=func.greatest(MLArtifactRecord.retained_until, until))
            )
            if result.rowcount != 1:
                raise LifecycleUnavailable("artifact_not_found")

    async def prune_artifacts(self, lease: Lease, *, now: dt.datetime | None = None) -> list[str]:
        safe_persistence._require_publication_owner()
        now = now or _now()
        directory = safe_persistence._validate_path(Path(settings.model_dir) / "lifecycle")
        if not directory.exists():
            return []
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
        paths, inventory_progress = await asyncio.to_thread(
            _bounded_artifact_page,
            directory,
            state.epoch,
            (state.checkpoints or {}).get("pruning:artifacts", {}),
        )
        cutoff = now - ORPHAN_GRACE
        deleted_files = []
        for path in paths:
            is_artifact = (
                path.suffix == ".pkl"
                and len(path.stem) == 64
                and all(char in "0123456789abcdef" for char in path.stem)
            )
            if not is_artifact and not path.name.startswith(".artifact-"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            initial = path.stat()
            if dt.datetime.fromtimestamp(initial.st_mtime, dt.timezone.utc) >= cutoff:
                continue
            async with self._sessions() as session:
                await self._lock(session, lease)
                if is_artifact:
                    record = (
                        await session.execute(
                            select(MLArtifactRecord).where(
                                MLArtifactRecord.artifact_hash == path.stem,
                            )
                        )
                    ).scalar_one_or_none()
                    if record is not None and (
                        record.role in PROTECTED_ROLES
                        or record.created_at >= cutoff
                        or (record.retained_until is not None and record.retained_until > now)
                    ):
                        continue
                    referenced = (
                        await session.execute(
                            select(MLForecastIssueRecord.id)
                            .where(
                                or_(
                                    MLForecastIssueRecord.incumbent_hash == path.stem,
                                    MLForecastIssueRecord.candidate_hash == path.stem,
                                ),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if referenced is not None:
                        continue
                if not path.exists():
                    continue
                current = path.stat()
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                    initial.st_ino,
                    initial.st_size,
                    initial.st_mtime_ns,
                ):
                    continue
                await self._lock(session, lease)
                path.unlink()
                safe_persistence._fsync_directory(directory)
                if is_artifact:
                    await session.execute(
                        delete(MLArtifactRecord).where(
                            MLArtifactRecord.artifact_hash == path.stem,
                        )
                    )
            deleted_files.append(path.name)
        async with self._sessions() as session:
            await self._lock(session, lease)
            state = await self._read(session)
            checkpoints = dict(state.checkpoints or {})
            checkpoints["pruning:artifacts"] = {
                **inventory_progress,
                "at": now.isoformat(),
            }
            await session.execute(
                update(MLLifecycleStateRecord)
                .where(*_lease_conditions(lease))
                .values(checkpoints=checkpoints)
            )
        return deleted_files

    async def prune_evidence(
        self, lease: Lease, *, now: dt.datetime | None = None
    ) -> dict[str, int]:
        safe_persistence._require_publication_owner()
        cutoff = (now or _now()) - EVIDENCE_RETENTION
        async with self._sessions() as session:
            await self._lock(session, lease)
            protected = select(MLArtifactRecord.artifact_hash).where(
                MLArtifactRecord.epoch == lease.epoch,
                or_(
                    MLArtifactRecord.role.in_(PROTECTED_ROLES),
                    MLArtifactRecord.retained_until > (now or _now()),
                ),
            )
            expired_issues = (
                select(MLForecastIssueRecord.id)
                .where(
                    MLForecastIssueRecord.created_at < cutoff,
                    MLForecastIssueRecord.target_at < cutoff,
                    or_(
                        MLForecastIssueRecord.incumbent_hash.is_(None),
                        MLForecastIssueRecord.incumbent_hash.not_in(protected),
                    ),
                    or_(
                        MLForecastIssueRecord.candidate_hash.is_(None),
                        MLForecastIssueRecord.candidate_hash.not_in(protected),
                    ),
                )
                .order_by(MLForecastIssueRecord.id)
                .limit(RETENTION_BATCH_SIZE)
            )
            outcomes = await session.execute(
                delete(MLForecastOutcomeRecord).where(
                    MLForecastOutcomeRecord.issue_id.in_(expired_issues),
                )
            )
            issues = await session.execute(
                delete(MLForecastIssueRecord).where(
                    MLForecastIssueRecord.id.in_(expired_issues),
                )
            )
            requests = await session.execute(
                delete(MLMaintenanceRequestRecord).where(
                    MLMaintenanceRequestRecord.id.in_(
                        select(MLMaintenanceRequestRecord.id)
                        .where(
                            MLMaintenanceRequestRecord.status.in_(
                                ("succeeded", "failed", "cancelled")
                            ),
                            MLMaintenanceRequestRecord.finished_at < cutoff,
                        )
                        .order_by(
                            MLMaintenanceRequestRecord.finished_at, MLMaintenanceRequestRecord.id
                        )
                        .limit(RETENTION_BATCH_SIZE)
                    ),
                )
            )
        return {
            "outcomes": outcomes.rowcount,
            "issues": issues.rowcount,
            "requests": requests.rowcount,
        }


class MaintenanceRunner:
    def __init__(
        self,
        lifecycle: Lifecycle | None = None,
        *,
        hooks: Mapping[str, ComponentHooks] | None = None,
        enabled_reader: Callable[[str], Awaitable[bool]] | None = None,
        renewal_seconds: float = 30,
        timer_reader: Any | None = None,
    ) -> None:
        if not 0 < renewal_seconds <= 30:
            raise ValueError("Lease renewal must occur within thirty seconds")
        self.lifecycle = lifecycle if lifecycle is not None else Lifecycle()
        self.hooks = (
            dict(hooks) if hooks is not None else native_component_hooks(self, timer_reader)
        )
        if self.hooks.keys() - LIFECYCLE_SCHEMAS.keys():
            raise ValueError("Unknown maintenance component")
        self._enabled_reader = enabled_reader
        self._renewal_seconds = renewal_seconds
        self._owner = "poller-" + str(uuid.uuid4())
        self._executor: ThreadPoolExecutor | None = None
        self._active: asyncio.Task | None = None
        self._closed = False

    async def _enabled(self) -> bool:
        return await (self._enabled_reader or get_bool_setting)("ml_maintenance_enabled")

    async def _renew(self, lease: Lease, lost: asyncio.Event) -> None:
        try:
            while True:
                await asyncio.sleep(self._renewal_seconds)
                if not await self._enabled():
                    lost.set()
                    return
                await self.lifecycle.renew(lease)
        except Exception as error:
            lost.set()
            logger.warning("ml_maintenance_lease_lost", error_type=type(error).__name__)

    async def _guard(self, lease: Lease, lost: asyncio.Event) -> None:
        if lost.is_set() or not await self._enabled():
            raise LeaseLost("maintenance_ownership_lost")
        await self.lifecycle.renew(lease)

    async def _fit(self, fit: Callable[[Any], Any], inputs: Any) -> Any:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml-maintenance")

        def fit_materialized():
            with safe_persistence.fitting_worker_scope():
                return fit(inputs)

        future = asyncio.get_running_loop().run_in_executor(self._executor, fit_materialized)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if future.done() and not future.cancelled():
                with suppress(Exception):
                    future.result()
            raise

    async def run(self) -> dict[str, Any]:
        if self._closed:
            return {"status": "stopped"}
        if self._active is not None:
            return {"status": "busy"}
        self._active = asyncio.current_task()
        lease = None
        renewal = None
        request = None
        lost = asyncio.Event()
        results: dict[str, str] = {}
        try:
            if not await self._enabled():
                return {"status": "disabled", "reason": "maintenance_disabled"}
            if await self.lifecycle.prepare_maintenance() is None:
                return {"status": "disabled", "reason": "maintenance_disabled"}
            lease = await self.lifecycle.acquire(self._owner)
            if lease is None:
                return {"status": "contended"}
            if not await self.lifecycle.begin_cycle(lease):
                return {"status": "waiting", "reason": "six_hour_cadence"}
            renewal = asyncio.create_task(self._renew(lease, lost))
            slot = _maintenance_slot(_now())
            for component in LIFECYCLE_SCHEMAS:
                await self._guard(lease, lost)
                request = None
                candidate = None
                try:
                    await self.lifecycle.enqueue(
                        component, "scheduled", dedup_key=f"scheduled:{slot}", lease=lease
                    )
                    snapshot = await self.lifecycle.read()
                    candidate = snapshot.reference(component, "candidate")
                    hooks = self.hooks.get(component)
                    incumbent = snapshot.reference(component)
                    if any(
                        not _artifact_retry_due(snapshot, reference)
                        for reference in (incumbent, candidate)
                        if reference is not None
                    ):
                        await self.lifecycle.defer_component(
                            lease, component, "artifact_unavailable"
                        )
                        results[component] = "unavailable"
                        continue
                    if (
                        incumbent is not None
                        and hooks is not None
                        and hooks.review_candidate is not None
                    ):
                        await hooks.review_candidate(lease, incumbent)
                        if (snapshot.checkpoints or {}).get(
                            "availability:" + incumbent.artifact_hash, {}
                        ).get("status") == "unavailable":
                            await self.lifecycle.clear_artifact_unavailability(lease, incumbent)
                        await self._guard(lease, lost)
                        snapshot = await self.lifecycle.read()
                        candidate = snapshot.reference(component, "candidate")
                    if candidate is not None:
                        await self.lifecycle.defer_component(
                            lease, component, "prospective_evidence_pending"
                        )
                        if hooks is not None and hooks.review_candidate is not None:
                            await hooks.review_candidate(lease, candidate)
                            if (snapshot.checkpoints or {}).get(
                                "availability:" + candidate.artifact_hash, {}
                            ).get("status") == "unavailable":
                                await self.lifecycle.clear_artifact_unavailability(lease, candidate)
                        results[component] = "candidate_frozen"
                        continue
                    if hooks is None:
                        await self.lifecycle.defer_component(
                            lease, component, "model_lifecycle_pending"
                        )
                        results[component] = "queued"
                        continue
                    request = await self.lifecycle.claim_request(lease, component)
                    if request is None:
                        await self.lifecycle.defer_component(
                            lease,
                            component,
                            "daily_demand_cadence"
                            if component == "demand"
                            else "no_pending_request",
                        )
                        results[component] = "waiting"
                        continue
                    inputs = await hooks.materialize(request)
                    fitted = await self._fit(hooks.fit, inputs) if inputs is not None else None
                    await self._guard(lease, lost)
                    if fitted is None:
                        diagnostics = (
                            inputs.get("diagnostics") if isinstance(inputs, dict) else None
                        )
                        details = {
                            "reason": (diagnostics or {}).get("reason")
                            or "insufficient_training_data"
                        }
                        if diagnostics:
                            details["readiness_rejection"] = diagnostics
                        await self.lifecycle.finish_request(lease, request.id, "queued", details)
                        results[component] = "queued"
                    else:
                        snapshot = await self.lifecycle.read()
                        publication = await hooks.commit_candidate(lease, fitted, snapshot)
                        if publication.committed and hooks.review_candidate is not None:
                            current = await self.lifecycle.read()
                            published = current.reference(component, "candidate")
                            if published is not None:
                                await hooks.review_candidate(lease, published)
                        status = "succeeded" if publication.committed else "failed"
                        await self.lifecycle.finish_request(
                            lease,
                            request.id,
                            status,
                            {"reason": publication.reason, "revision": publication.revision},
                        )
                        results[component] = status
                except LeaseLost:
                    raise
                except ArtifactUnavailableError as error:
                    await self._guard(lease, lost)
                    confirmed = await self.lifecycle.record_artifact_unavailability(lease, error)
                    if confirmed:
                        await self.lifecycle.invalidate_component(
                            lease, error.reference, "artifact_missing_confirmed"
                        )
                        if (
                            candidate is not None
                            and candidate.artifact_hash != error.reference.artifact_hash
                        ):
                            await self.lifecycle.invalidate_component(
                                lease, candidate, "comparator_missing_confirmed"
                            )
                    reason = "artifact_missing_confirmed" if confirmed else "artifact_unavailable"
                    if request is not None:
                        await self.lifecycle.finish_request(
                            lease,
                            request.id,
                            "failed" if confirmed else "queued",
                            {"reason": reason},
                        )
                    else:
                        await self.lifecycle.defer_component(lease, component, reason)
                    results[component] = "failed" if confirmed else "unavailable"
                except (LifecycleUnavailable, DBAPIError, SQLAlchemyTimeoutError, OSError) as error:
                    logger.warning(
                        "ml_maintenance_unavailable",
                        component=component,
                        error_type=type(error).__name__,
                    )
                    await self._guard(lease, lost)
                    if request is not None:
                        await self.lifecycle.finish_request(
                            lease, request.id, "queued", {"reason": "storage_unavailable"}
                        )
                    else:
                        await self.lifecycle.defer_component(
                            lease, component, "storage_unavailable"
                        )
                    results[component] = "unavailable"
                except Exception as error:
                    await self._guard(lease, lost)
                    if isinstance(error, ArtifactIntegrityError):
                        await self.lifecycle.invalidate_component(
                            lease, error.reference, "artifact_integrity_failure"
                        )
                        if (
                            candidate is not None
                            and candidate.artifact_hash != error.reference.artifact_hash
                        ):
                            await self.lifecycle.invalidate_component(
                                lease, candidate, "comparator_integrity_failure"
                            )
                    if request is not None:
                        await self.lifecycle.finish_request(
                            lease,
                            request.id,
                            "failed",
                            {"reason": "maintenance_failed", "error_type": type(error).__name__},
                        )
                    else:
                        await self.lifecycle.defer_component(lease, component, "review_failed")
                    results[component] = "failed"
                    logger.warning(
                        "ml_maintenance_failed",
                        component=component,
                        error_type=type(error).__name__,
                    )
                request = None
            await self._guard(lease, lost)
            retention = await self.lifecycle.prune_evidence(lease)
            await self._guard(lease, lost)
            artifacts = await self.lifecycle.prune_artifacts(lease)
            logger.info("ml_retention_completed", **retention, artifacts=len(artifacts))
            return {
                "status": "reviewed",
                "components": results,
                "retention": {**retention, "artifacts": len(artifacts)},
            }
        except asyncio.CancelledError:
            if lease is not None and request is not None and not lost.is_set():
                with suppress(Exception):
                    if await self._enabled():
                        await self.lifecycle.finish_request(
                            lease, request.id, "cancelled", {"reason": "worker_cancelled"}
                        )
            raise
        except LeaseLost:
            return {"status": "lease_lost", "reason": "maintenance_ownership_lost"}
        except (LifecycleUnavailable, DBAPIError, SQLAlchemyTimeoutError, OSError) as error:
            logger.warning("ml_maintenance_unavailable", error_type=type(error).__name__)
            return {"status": "unavailable", "reason": "storage_unavailable", "components": results}
        finally:
            if renewal is not None:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
            if lease is not None:
                with suppress(Exception):
                    if await self._enabled():
                        await self.lifecycle.release(lease)
            self._active = None

    async def close(self) -> None:
        self._closed = True
        if self._active is not None and self._active is not asyncio.current_task():
            self._active.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(self._active)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)


async def update_maintenance_gate(session: AsyncSession, enabled: bool) -> None:
    state = MLLifecycleStateRecord
    changed = (
        await session.execute(
            update(state)
            .where(state.id == 1, state.maintenance_enabled.is_distinct_from(enabled))
            .values(
                maintenance_enabled=enabled,
                lease_owner=None,
                lease_expires_at=None,
                revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                fence=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
            )
            .returning(state.id)
        )
    ).scalar_one_or_none()
    if changed is not None and not enabled:
        await session.execute(
            update(MLMaintenanceRequestRecord)
            .where(MLMaintenanceRequestRecord.status == "running")
            .values(
                status="queued",
                fence=None,
                details=MLMaintenanceRequestRecord.details.op("||")(
                    {"reason": "maintenance_disabled"}
                ),
            )
        )


async def enqueue_maintenance_request(
    component: str,
    source: str,
    *,
    dedup_key: str | None = None,
    automatic: bool = False,
    lifecycle: Lifecycle | None = None,
) -> dict[str, Any] | None:
    enabled = await get_bool_setting("ml_maintenance_enabled")
    lifecycle = lifecycle if lifecycle is not None else Lifecycle()
    if automatic and not enabled:
        return None
    request = await lifecycle.enqueue(component, source, dedup_key=dedup_key, automatic=automatic)
    return request.response(enabled) if request is not None else None


async def invalidate_lifecycle(session: AsyncSession) -> dict[str, Any] | None:
    """Revoke authority in the caller's reset transaction without recreating its epoch."""
    state = MLLifecycleStateRecord
    exists = (
        await session.execute(select(state.id).where(state.id == 1).with_for_update())
    ).scalar_one_or_none()
    if exists is None:
        return None
    row = (
        (
            await session.execute(
                update(state)
                .where(state.id == 1)
                .values(
                    revision=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                    fence=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(),
                    lease_owner=None,
                    lease_expires_at=None,
                    maintenance_enabled=False,
                    checkpoints={},
                    next_maintenance_at=None,
                )
                .returning(state.epoch, state.revision, state.fence)
            )
        )
        .mappings()
        .one()
    )
    await session.execute(
        update(MLArtifactRecord)
        .where(
            MLArtifactRecord.epoch == row["epoch"],
        )
        .values(
            role="unreferenced",
            qualified_at=None,
            qualification_expires_at=None,
            last_valid_outcome_at=None,
            qualification_scope={},
        )
    )
    await session.execute(
        update(MLMaintenanceRequestRecord)
        .where(
            MLMaintenanceRequestRecord.epoch == row["epoch"],
            MLMaintenanceRequestRecord.status.in_(("queued", "running")),
        )
        .values(status="cancelled", finished_at=func.clock_timestamp())
    )
    return dict(row)


def native_component_hooks(
    runner: MaintenanceRunner, timer_reader: Any | None
) -> dict[str, ComponentHooks]:
    from packages.ml import forecast_quality as quality
    from packages.ml.comfort_model import (
        ComfortModel,
        WindowDataset,
        DIRECT_FORECAST_HORIZONS_MINUTES,
        build_candidate,
        build_window_dataset,
    )
    from packages.ml.demand_model_core import DemandModel, build_demand_candidate
    from packages.ml.thermal import build_thermal_candidate, materialize_thermal_inputs

    lifecycle = runner.lifecycle

    async def guard(lease):
        if not await runner._enabled():
            raise LeaseLost("maintenance_disabled")
        await lifecycle.renew(lease)

    def component_hooks(component):
        async def materialize(_request):
            requested_family = None
            if component == "thermal" and _request is not None:
                try:
                    requested_family = _thermal_request_family(
                        _request.details or {}, _request.dedup_key
                    )
                except ValueError:
                    return None
            cutoff = _now()
            inputs = await quality.materialize_native_inputs(
                component, now=cutoff, timer_reader=timer_reader
            )
            input_families: dict[str, dict[str, Any]] = {}
            if component == "demand":
                if inputs is None:
                    return None
                data = await DemandModel().materialize_candidate(
                    cutoff=cutoff,
                    device_id=inputs["device_id"],
                    weather_provider=inputs["weather_provider"],
                    worker=runner._fit,
                )
            elif component == "comfort":
                if inputs is None:
                    return None
                data = await ComfortModel()._materialize_window_dataset(
                    data_cutoff=cutoff, device_id=inputs["device_id"], raw=True
                )
            else:
                for family in ("tank_", "indoor_"):
                    current_family = (
                        "tank_"
                        if inputs and inputs["sensor_basis"]["kind"] == "heatpump_temperature"
                        else "indoor_"
                    )
                    scoped = (
                        inputs
                        if inputs is not None and current_family == family
                        else await quality.materialize_native_inputs(
                            component,
                            now=cutoff,
                            device_id=inputs["device_id"] if inputs else None,
                            timer_reader=timer_reader,
                            capabilities=(family + "heating", family + "cooling"),
                        )
                    )
                    if scoped is not None:
                        input_families[family] = scoped
                        inputs = inputs or scoped
                if not input_families or inputs is None:
                    return None
                if requested_family is not None:
                    inputs = input_families.get(requested_family)
                    if inputs is None:
                        return None
                data = await materialize_thermal_inputs(
                    inputs["device_id"],
                    inputs["reference_sensor_id"],
                    inputs["weather_provider"],
                    cutoff,
                    selected_ids=inputs["configured_sensor_ids"],
                )
            if data is None:
                return None
            return {
                "data": data,
                "inputs": inputs,
                "data_cutoff": cutoff,
                "input_families": input_families,
                "requested_family": requested_family,
            }

        def fit(materialized):
            fitted_inputs = materialized["inputs"]
            remaining_families = ()
            if component == "demand":
                payload = build_demand_candidate(materialized["data"])
                horizons, capabilities = (60, 180, 360, 720, 1440), ("demand",)
            elif component == "comfort":
                data = materialized["data"]
                if not isinstance(data, WindowDataset):
                    dataset = build_window_dataset(
                        data["readings"],
                        data["statuses"],
                        data["weather"],
                        DIRECT_FORECAST_HORIZONS_MINUTES,
                    )
                    data = WindowDataset(
                        dataset.controlled,
                        dataset.passive,
                        {
                            **dataset.evidence,
                            "sensor_strategy": data["sensor_strategy"],
                            "source_sensor_count": data["source_sensor_count"],
                        },
                        dataset.controlled_times,
                        dataset.passive_times,
                    )
                candidate = build_candidate(data, None)
                payload = (
                    ComfortModel()._candidate_artifact(candidate) if candidate is not None else None
                )
                horizons = tuple(sorted(payload["direct_models"])) if payload else ()
                capabilities = ("indoor",)
            else:
                diagnostics = materialized.setdefault("diagnostics", {})
                payload = build_thermal_candidate(materialized["data"], diagnostics=diagnostics)
                diagnostics["requested_family"] = materialized.get("requested_family")
                if payload is None:
                    return None
                quality.check_exact_fit(component, payload)
                default_family = (
                    "tank_"
                    if materialized["inputs"]["sensor_basis"]["kind"] == "heatpump_temperature"
                    else "indoor_"
                )
                input_families = materialized.get("input_families") or {
                    default_family: materialized["inputs"]
                }
                supported = {
                    family: tuple(
                        name for name in payload["declared_components"] if name.startswith(family)
                    )
                    for family in input_families
                    if any(name.startswith(family) for name in payload["declared_components"])
                }
                requested = materialized.get("requested_family")
                family = requested or next(iter(supported), None)
                if family is None or family not in supported:
                    diagnostics["reason"] = "requested_family_unvalidated"
                    return None
                fitted_inputs = input_families[family]
                capabilities = supported[family]
                if requested is None:
                    remaining_families = tuple(name for name in supported if name != family)
                horizons = (15, 30, 60)
            if payload is None or not horizons or not capabilities:
                return None
            if component != "thermal":
                quality.check_exact_fit(component, payload)
            return {
                **materialized,
                "inputs": fitted_inputs,
                "payload": payload,
                "horizons": horizons,
                "capabilities": capabilities,
                "remaining_families": remaining_families,
            }

        async def commit(lease, fitted, state):
            await guard(lease)
            incumbent = state.reference(component)
            scope = quality.native_scope(fitted["inputs"])
            frozen_incumbent = (
                incumbent.artifact_hash if reference_is_qualified(incumbent, scope=scope) else None
            )
            manifest = {
                "fit_id": str(uuid.uuid4()),
                "data_cutoff": fitted["data_cutoff"].isoformat(),
                "frozen_at": _now().isoformat(),
                "input_schema": fitted["inputs"]["schemas"]["input"],
                "output_schema": LIFECYCLE_SCHEMAS[component],
                "runtime_versions": {
                    "python": platform.python_version(),
                    "scikit-learn": version("scikit-learn"),
                },
                "evidence_hashes": [
                    hashlib.sha256(pickle.dumps(fitted["payload"], protocol=5)).hexdigest()
                ],
                "scope": scope,
                "sensor_basis": fitted["inputs"]["sensor_basis"],
                "device_id": fitted["inputs"]["device_id"],
                "horizons": list(fitted["horizons"]),
                "capabilities": list(fitted["capabilities"]),
                "frozen_incumbent_hash": frozen_incumbent,
                "qualification_policy": quality.QUALIFICATION_POLICY_SCHEMA,
                "decision_offsets_hours": list(quality.DECISION_OFFSETS_HOURS),
            }
            payload = fitted["payload"]
            metrics = payload.get("metrics")
            if metrics is None:
                metrics = {
                    name: {
                        key: value
                        for key, value in details.items()
                        if key not in {"holdout_features", "holdout_targets", "holdout_predictions"}
                    }
                    for name, details in payload.get("validation", {}).items()
                }
            manifest["metrics"] = metrics
            manifest["samples"] = payload.get(
                "samples", metrics.get("samples", payload.get("params", {}).get("sample_count", 0))
            )
            artifact = await asyncio.to_thread(
                safe_persistence.publish_signed_artifact,
                fitted["payload"],
                component=component,
                schema_version=LIFECYCLE_SCHEMAS[component],
                manifest=manifest,
                round_trip_check=lambda restored: quality.check_exact_fit(component, restored),
            )
            await guard(lease)
            publication = await lifecycle.publish(
                lease,
                artifact,
                expected_revision=state.revision,
                expected_incumbent=incumbent.artifact_hash if incumbent else None,
            )
            if publication.committed:
                for family in fitted.get("remaining_families", ()):
                    await lifecycle.enqueue(
                        component,
                        "scheduled",
                        lease=lease,
                        dedup_key=f"thermal-family:{family}:{artifact.artifact_hash}",
                        family=family,
                    )
            return publication

        async def review(lease, reference):
            now, manifest = _now(), reference.manifest
            if manifest.get("qualification_policy") != quality.QUALIFICATION_POLICY_SCHEMA:
                await lifecycle.invalidate_component(lease, reference, "incompatible_schema")
                return
            frozen_at = dt.datetime.fromisoformat(manifest["frozen_at"])
            if now < frozen_at:
                raise LifecycleUnavailable("future_frozen_candidate")
            slot = _maintenance_slot(now) - _maintenance_slot(frozen_at)
            if now - frozen_at > EVIDENCE_RETENTION and reference.role == "candidate":
                await lifecycle.invalidate_component(lease, reference, "collection_window_expired")
                return
            if (
                reference.role == "incumbent"
                and reference.qualification_expires_at is not None
                and reference.qualification_expires_at <= now
            ):
                await lifecycle.invalidate_component(lease, reference, "outcomes_expired")
                return
            state = await lifecycle.read()
            collection = (state.checkpoints or {}).get("collection:" + reference.artifact_hash, {})
            inputs = await quality.materialize_native_inputs(
                component,
                now=now,
                device_id=manifest["device_id"],
                timer_reader=timer_reader,
                capabilities=tuple(manifest["capabilities"]),
            )
            if inputs is not None and quality.native_scope(inputs) != manifest["scope"]:
                await lifecycle.invalidate_component(
                    lease, reference, "incompatible_sensor_policy_scope"
                )
                return
            if collection.get("last_slot") != slot:
                payload = await lifecycle.load_committed(
                    component, role=reference.role, expected_hash=reference.artifact_hash
                )
                frozen_hash, horizons = (
                    manifest["frozen_incumbent_hash"],
                    tuple(manifest["horizons"]),
                )
                comparator = (
                    await lifecycle.load_frozen_comparator(component, frozen_hash)
                    if frozen_hash
                    else None
                )

                def issue_pair(_unused):
                    if inputs is None:
                        return None
                    return quality.freeze_native_pair(
                        inputs,
                        epoch=lease.epoch,
                        component=component,
                        candidate_hash=reference.artifact_hash,
                        incumbent_hash=frozen_hash,
                        horizons=horizons,
                        candidate_predictor=lambda snapshot, leads: quality.predict_native(
                            component, payload, snapshot, leads
                        ),
                        incumbent_predictor=lambda snapshot, leads: quality.predict_native(
                            component, comparator, snapshot, leads
                        ),
                        baseline_predictor=lambda snapshot, leads: quality.predict_native(
                            component, payload, snapshot, leads, baseline=True
                        ),
                    )

                issue = await runner._fit(issue_pair, None)
                await guard(lease)
                if issue is not None:
                    rows = issue.rows()
                else:
                    snapshot = {
                        "unavailable_reason": "unconstructable_native_policy",
                        "scope": manifest["scope"],
                        "sensor_basis": manifest["sensor_basis"],
                        "weather": inputs["weather"] if inputs else [],
                        "capability": inputs["capability"]
                        if inputs
                        else manifest["capabilities"][0],
                        "source_availability": {"native_inputs": inputs is not None},
                    }
                    cohort = quality.evidence_hash({"hash": reference.artifact_hash, "slot": slot})
                    rows = [
                        {
                            "epoch": lease.epoch,
                            "device_id": manifest["device_id"],
                            "component": component,
                            "cohort_key": cohort,
                            "horizon_minutes": horizon,
                            "issue_kind": "prospective_native",
                            "policy_schema": quality.NATIVE_POLICY_SCHEMA,
                            "issued_at": now,
                            "target_at": now + dt.timedelta(minutes=horizon),
                            "device_day": now.date(),
                            "candidate_hash": reference.artifact_hash,
                            "incumbent_hash": frozen_hash,
                            "snapshot_hash": quality.evidence_hash(snapshot),
                            "input_snapshot": snapshot,
                            "predictions": {},
                            "expected_horizons": list(horizons),
                        }
                        for horizon in horizons
                    ]
                await lifecycle.record_native_issues(lease, reference, rows, slot=slot)
            key = "scan:" + reference.artifact_hash
            await lifecycle.begin_evidence_scan(
                lease, reference, key=key, window_start=frozen_at, window_end=now
            )
            for _page_number in range(501):
                page = await lifecycle.evidence_page(lease, key)
                if page["checkpoint"]["complete"]:
                    break
                evidence = await quality.materialize_execution_page(page, now=now)
                outcomes = await runner._fit(
                    lambda materialized: quality.reconcile_materialized_page(
                        page, materialized, now=now, scope=manifest["scope"]
                    ),
                    evidence,
                )
                await guard(lease)
                await lifecycle.commit_evidence_page(lease, key, page, outcomes)
                if page["complete"]:
                    break
            state = await lifecycle.read()
            completed = tuple(
                index
                for index in range(len(quality.DECISION_OFFSETS_HOURS))
                if f"decision:{reference.artifact_hash}:{index}" in (state.checkpoints or {})
            )
            checkpoint = quality.decision_checkpoint(frozen_at, now, completed)
            if checkpoint is None or not await lifecycle.claim_decision(
                lease, reference, checkpoint
            ):
                return
            window_end = frozen_at + dt.timedelta(
                hours=quality.DECISION_OFFSETS_HOURS[checkpoint] - 24
            )
            pairs, complete = await lifecycle.decision_evidence(
                lease, reference, window_end=window_end
            )
            result = await runner._fit(
                lambda materialized: quality.qualify_prospective(
                    materialized,
                    component=component,
                    candidate_hash=reference.artifact_hash,
                    frozen_at=frozen_at,
                    data_cutoff=dt.datetime.fromisoformat(manifest["data_cutoff"]),
                    now=now,
                    horizons=tuple(manifest["horizons"]),
                    scope=manifest["scope"],
                    capabilities=tuple(manifest["capabilities"]),
                    coverage_complete=complete,
                    checkpoint=checkpoint,
                ),
                pairs,
            )
            await guard(lease)
            await lifecycle.finish_decision(lease, reference, checkpoint, result)
            if result["qualified"]:
                state = await lifecycle.read()
                incumbent = state.reference(component)
                artifact, _ = await asyncio.to_thread(verify_reference, reference)
                publication = await lifecycle.publish(
                    lease,
                    artifact,
                    expected_revision=state.revision,
                    expected_incumbent=incumbent.artifact_hash if incumbent else None,
                    role="incumbent",
                    qualification=Qualification(
                        now,
                        dt.datetime.fromisoformat(result["last_valid_outcome_at"]),
                        result["scope"],
                    ),
                )
                if publication.committed and component == "demand":
                    from packages.ml.demand_model_core import DEMAND_MAE_BASELINE
                    from packages.ml.models_common import write_mae_baseline

                    await asyncio.to_thread(
                        write_mae_baseline,
                        DEMAND_MAE_BASELINE,
                        result["evidence"]["overall"]["baseline"]["candidate_mae"],
                    )
            elif (
                result["reason"] == "incompatible_scope"
                or checkpoint == len(quality.DECISION_OFFSETS_HOURS) - 1
            ):
                await lifecycle.invalidate_component(lease, reference, result["reason"])

        return ComponentHooks(materialize, fit, commit, review)

    return {component: component_hooks(component) for component in LIFECYCLE_SCHEMAS}

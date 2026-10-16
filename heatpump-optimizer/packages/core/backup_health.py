"""Versioned backup evidence and the shared, fail-closed health projection.

Heartbeat update time is deliberately not an input: it is not a recovery point.
Internal archive bindings never cross the public projection boundary.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

UTC = dt.timezone.utc
BACKUP_DEFAULTS = {
    "backup_interval_seconds": 2592000,
    "backup_retention_days": 100,
    "backup_max_age_seconds": 2764800,
    "backup_timeout_seconds": 3600,
}
BACKUP_BOUNDS = {
    "backup_interval_seconds": (3600, 7776000),
    "backup_retention_days": (2, 365),
    "backup_max_age_seconds": (3600, 10368000),
    "backup_timeout_seconds": (60, 21600),
}
REASON_CODES = frozenset(
    {
        "backup_state_unknown",
        "backup_success_missing",
        "backup_success_stale",
        "backup_attempt_failed",
        "backup_attempt_overdue",
        "backup_replica_required_failed",
        "backup_replica_optional_failed",
        "backup_configuration_invalid",
        "backup_restore_unqualified",
        "backup_verification_failed",
        "backup_verification_overdue",
        "backup_cleanup_failed",
        "backup_reporting_incomplete",
        "backup_retention_blocked",
    }
)


@dataclass(frozen=True)
class BackupPolicy:
    interval_seconds: int = 2592000
    retention_days: int = 100
    max_age_seconds: int = 2764800
    timeout_seconds: int = 3600

    @classmethod
    def from_settings(
        cls, values: Mapping[str, Any], *, fallbacks: Mapping[str, Any] | None = None
    ) -> BackupPolicy:
        parsed = {}
        for key, default in BACKUP_DEFAULTS.items():
            raw = values.get(key)
            if raw is None or raw == "":
                raw = (fallbacks or {}).get(key, default)
            if isinstance(raw, bool) or not re.fullmatch(r"[+-]?\d+", str(raw).strip()):
                raise ValueError(f"{key} must be an integer")
            value = int(raw)
            low, high = BACKUP_BOUNDS[key]
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            parsed[key.removeprefix("backup_")] = value
        policy = cls(**parsed)
        if policy.max_age_seconds < policy.interval_seconds + policy.timeout_seconds + 120:
            raise ValueError("backup freshness must cover interval, timeout and 120 seconds")
        if policy.retention_days * 86400 <= policy.interval_seconds:
            raise ValueError("backup retention must exceed interval")
        if policy.retention_days * 86400 < policy.max_age_seconds:
            raise ValueError("backup retention must cover freshness")
        return policy


def decode_state(raw: object) -> dict[str, Any] | None:
    try:
        state = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int:
        return None
    if state["schema_version"] != 1:
        return None
    if any(
        key in state and not isinstance(state[key], dict)
        for key in ("last_attempt", "last_success", "last_verification", "last_verified")
    ):
        return None
    return state


def timestamp(value: object, *, now: dt.datetime) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(UTC)
        return parsed if parsed <= now else None
    except (ValueError, OverflowError):
        return None


def _binding(event: dict) -> bool:
    return all(
        isinstance(event.get(key), str) and bool(event[key])
        for key in ("archive_id", "source_id", "schema_revision")
    ) and bool(re.fullmatch(r"[0-9a-f]{64}", str(event.get("sha256", ""))))


def project_backup_health(raw: object, policy: BackupPolicy, *, now: dt.datetime) -> dict[str, Any]:
    """Return public values only; no raw messages, identifiers or paths."""
    now = now.astimezone(UTC)
    state = decode_state(raw)
    reasons: list[str] = []
    if state is None:
        reasons.append("backup_state_unknown")
    state = state or {}
    success = state.get("last_success", {})
    attempt = state.get("last_attempt", {})
    verification = state.get("last_verification", {})
    verified = state.get("last_verified", {})
    snapshot = timestamp(success.get("snapshot_at"), now=now)
    completed = timestamp(success.get("completed_at"), now=now)
    if not (_binding(success) and snapshot and completed and snapshot <= completed):
        snapshot = completed = None
        reasons.append("backup_success_missing")
    age = (now - snapshot).total_seconds() if snapshot else None
    if age is not None and age >= policy.max_age_seconds:
        reasons.append("backup_success_stale")
    started = timestamp(attempt.get("started_at"), now=now)
    finished = timestamp(attempt.get("finished_at"), now=now)
    attempt_status = attempt.get("status")
    if not isinstance(attempt_status, str):
        attempt_status = None
    if attempt:
        if not started or not isinstance(attempt.get("id"), str) or not attempt["id"]:
            reasons.append("backup_state_unknown")
        elif attempt_status == "running":
            if (now - started).total_seconds() > policy.timeout_seconds + 120:
                reasons.append("backup_attempt_overdue")
        elif attempt_status != "succeeded" or not finished or finished < started:
            reasons.append("backup_attempt_failed")
        if attempt.get("replica_status") == "required_failed":
            reasons.append("backup_replica_required_failed")
        elif attempt.get("replica_status") == "optional_failed":
            reasons.append("backup_replica_optional_failed")
        if attempt.get("reason_code") == "backup_retention_blocked":
            reasons.append("backup_retention_blocked")
    elif snapshot:
        reasons.append("backup_state_unknown")
    verified_at = timestamp(verified.get("verified_at"), now=now)
    qualified = bool(
        snapshot
        and _binding(verified)
        and verified_at
        and verified["source_id"] == success["source_id"]
        and verified["schema_revision"] == success["schema_revision"]
    )
    if not qualified:
        reasons.append("backup_restore_unqualified")
    verification_started = timestamp(verification.get("started_at"), now=now)
    verification_finished = timestamp(verification.get("finished_at"), now=now)
    verification_status = verification.get("status")
    if not isinstance(verification_status, str):
        verification_status = None
    if verification:
        if (
            not verification_started
            or not isinstance(verification.get("id"), str)
            or not verification["id"]
        ):
            reasons.append("backup_state_unknown")
        if verification_status not in {"running", "succeeded"}:
            reasons.append("backup_verification_failed")
        if verification.get("reason_code") == "cleanup_failed":
            reasons.append("backup_cleanup_failed")
        if verification_status == "running":
            reasons.append("backup_reporting_incomplete")
            if not verification_started or (now - verification_started).total_seconds() > 4500:
                reasons.append("backup_verification_overdue")
        if verification_status == "succeeded" and not (
            verification_finished
            and verification_started
            and verification_started <= verification_finished
            and verified_at == verification_finished
            and verification.get("archive_id") == verified.get("archive_id")
            and verification.get("sha256") == verified.get("sha256")
        ):
            reasons.append("backup_reporting_incomplete")
    elif qualified:
        reasons.append("backup_reporting_incomplete")
    reasons = list(dict.fromkeys(reasons))
    ready = not any(reason != "backup_replica_optional_failed" for reason in reasons)
    return {
        "ready": ready,
        "status": "warning" if ready and reasons else "healthy" if ready else "degraded",
        "reason_codes": reasons,
        # Compatibility alias, now derived exclusively from completed success.
        "last_success": snapshot.isoformat() if snapshot else None,
        "last_attempt": {
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "status": attempt_status
            if attempt_status in {"running", "succeeded", "failed", "interrupted"}
            else "unknown",
        },
        "last_verification": {
            "started_at": verification_started.isoformat() if verification_started else None,
            "finished_at": verification_finished.isoformat() if verification_finished else None,
            "status": verification_status
            if verification_status in {"running", "succeeded", "failed"}
            else "unknown",
        },
        "last_verified": verified_at.isoformat() if qualified and verified_at else None,
        "success_age_seconds": age,
        "stale_after_seconds": policy.max_age_seconds,
        "next_due_at": (snapshot + dt.timedelta(seconds=policy.interval_seconds)).isoformat()
        if snapshot
        else None,
        "restore_qualified": qualified,
    }


async def load_backup_health(*, now: dt.datetime) -> dict[str, Any]:
    """Materialize values inside sessions; all failures produce unknown health."""
    import structlog
    from sqlalchemy import select

    from packages.core.config import settings
    from packages.core.database import get_session
    from packages.core.models import ServiceHeartbeatRecord
    from packages.core.settings_service import get_all_settings

    try:
        values = await get_all_settings()
        policy = BackupPolicy.from_settings(
            values, fallbacks={key: getattr(settings, key) for key in BACKUP_DEFAULTS}
        )
    except Exception:
        structlog.get_logger().warning("backup_policy_unavailable")
        result = project_backup_health(None, BackupPolicy(), now=now)
        result["reason_codes"].append("backup_configuration_invalid")
        return result
    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ServiceHeartbeatRecord.details_json).where(
                        ServiceHeartbeatRecord.service == "backup"
                    )
                )
            ).scalar_one_or_none()
        return project_backup_health(row, policy, now=now)
    except Exception:
        structlog.get_logger().warning("backup_state_unavailable")
        return project_backup_health(None, policy, now=now)

"""Shared safety-revert linkage, pairing, and embargo semantics."""

from __future__ import annotations

import math
import json
from collections.abc import Iterable
import datetime as dt
from typing import Any

from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.orm import aliased

from packages.core.models import PlanActionRecord
from packages.optimizer.actions import ActionType
from packages.optimizer.executor_gate import is_room_heating_increase

UNRESOLVED_STATUSES = ("pending", "executing", "dispatched")
MAX_UNRESOLVED_REVERT_ACTION_TYPES = 20
MAX_UNRESOLVED_REVERT_DEVICE_IDS = 500
MAX_HISTORICAL_REVERT_OVERLAPS = 500


def is_restorative_action(action: Any) -> bool:
    """Return whether an action is explicitly linked to a source action."""

    return _action_value(action, "reverts_action_id") is not None


def unresolved_revert_predicate():
    """Return the canonical SQL predicate for unresolved restores."""

    return and_(
        PlanActionRecord.reverts_action_id.is_not(None),
        PlanActionRecord.status.in_(UNRESOLVED_STATUSES),
    )


def due_revert_predicate():
    """Return the canonical SQL predicate for due pending restores."""

    return and_(
        PlanActionRecord.reverts_action_id.is_not(None),
        PlanActionRecord.status == "pending",
    )


async def unresolved_revert_summary(
    session: Any,
    *,
    now: dt.datetime,
) -> dict[str, object]:
    """Return a bounded operational summary of unresolved restore obligations."""

    count, oldest_scheduled_at = (
        await session.execute(
            select(
                func.count(PlanActionRecord.id),
                func.min(PlanActionRecord.scheduled_ts),
            ).where(unresolved_revert_predicate())
        )
    ).one()
    action_types = (
        (
            await session.execute(
                select(distinct(PlanActionRecord.action_type))
                .where(unresolved_revert_predicate())
                .order_by(PlanActionRecord.action_type)
                .limit(MAX_UNRESOLVED_REVERT_ACTION_TYPES)
            )
        )
        .scalars()
        .all()
    )
    oldest_age_seconds = (
        max(0, int((now - oldest_scheduled_at).total_seconds()))
        if oldest_scheduled_at is not None
        else None
    )
    return {
        "count": int(count or 0),
        "oldest_scheduled_at": oldest_scheduled_at,
        "oldest_age_seconds": oldest_age_seconds,
        "action_types": [str(action_type) for action_type in action_types],
    }


def serialize_unresolved_revert_summary(summary: dict[str, object]) -> dict[str, object]:
    """Convert the unresolved-revert summary into a JSON-safe API payload."""

    serialized = dict(summary)
    oldest_scheduled_at = serialized.get("oldest_scheduled_at")
    if isinstance(oldest_scheduled_at, dt.datetime):
        if oldest_scheduled_at.tzinfo is None:
            oldest_scheduled_at = oldest_scheduled_at.replace(tzinfo=dt.timezone.utc)
        serialized["oldest_scheduled_at"] = oldest_scheduled_at.astimezone(
            dt.timezone.utc
        ).isoformat()
    return serialized


async def unresolved_revert_device_ids(
    session: Any,
    *,
    limit: int = MAX_UNRESOLVED_REVERT_DEVICE_IDS,
) -> tuple[set[str], bool]:
    """Return unresolved device IDs and whether the bounded lookup overflowed."""

    rows = (
        (
            await session.execute(
                select(distinct(PlanActionRecord.device_id))
                .where(unresolved_revert_predicate(), PlanActionRecord.device_id.is_not(None))
                .order_by(PlanActionRecord.device_id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    overflowed = len(rows) > limit
    return {str(device_id) for device_id in rows[:limit]}, overflowed


async def historical_revert_overlap_rows(
    session: Any,
    *,
    horizon_start: dt.datetime,
    horizon_end: dt.datetime,
    drift_margin: dt.timedelta,
    limit: int = MAX_HISTORICAL_REVERT_OVERLAPS,
) -> tuple[list[tuple[str, dt.datetime, str, dt.datetime | None]], bool]:
    """Return bounded source/restore lifecycle rows that could overlap a horizon."""

    restore = aliased(PlanActionRecord)
    rows = (
        await session.execute(
            select(
                PlanActionRecord.device_id,
                PlanActionRecord.executed_at,
                restore.status,
                restore.executed_at,
            )
            .join(restore, restore.reverts_action_id == PlanActionRecord.id)
            .where(PlanActionRecord.device_id.is_not(None))
            .where(PlanActionRecord.executed_at.is_not(None))
            .where(PlanActionRecord.executed_at <= horizon_end + drift_margin)
            .where(
                or_(
                    PlanActionRecord.executed_at >= horizon_start - dt.timedelta(hours=24),
                    restore.status.in_(UNRESOLVED_STATUSES),
                    restore.executed_at >= horizon_start,
                )
            )
            .order_by(
                case(
                    (restore.status.in_(UNRESOLVED_STATUSES), 0),
                    (restore.executed_at >= horizon_start, 1),
                    else_=2,
                ),
                PlanActionRecord.executed_at.desc(),
            )
            .limit(limit + 1)
        )
    ).all()
    overflowed = len(rows) > limit
    return list(rows[:limit]), overflowed


def normalize_zone_id(zone_id: Any) -> int:
    """Normalize legacy/missing primary-zone values to zone 1."""

    if zone_id is None or isinstance(zone_id, bool) or zone_id in (0, 1):
        return 1
    if isinstance(zone_id, (int, float)) and math.isfinite(float(zone_id)):
        return int(zone_id)
    return 1


def _action_value(action: Any, name: str, default: Any = None) -> Any:
    if isinstance(action, dict):
        return action.get(name, default)
    return getattr(action, name, default)


def _payload(action: Any) -> dict[str, Any]:
    payload = _action_value(action, "payload", None)
    if isinstance(payload, dict):
        return payload
    payload_json = _action_value(action, "payload_json", None)
    if not isinstance(payload_json, str):
        return {}
    try:
        parsed = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def restore_baseline_target(restore_action: Any) -> float | None:
    """Return the finite zone target carried by a restore action itself."""

    target = _payload(restore_action).get("temperature")
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return None
    target = float(target)
    return target if math.isfinite(target) else None


def validate_action_pair(source: Any, restore: Any) -> None:
    """Require a compatible explicit source/restore pair."""

    source_type = str(_action_value(source, "type", _action_value(source, "action_type")))
    restore_type = str(_action_value(restore, "type", _action_value(restore, "action_type")))
    valid_types = {
        ("force_dhw_on", "force_dhw_off"),
        ("zone_temp_boost", "zone_temp_restore"),
    }
    if (source_type, restore_type) not in valid_types:
        raise ValueError(f"invalid safety action pair: {source_type} -> {restore_type}")
    if _action_value(source, "device_id") != _action_value(restore, "device_id"):
        raise ValueError("safety action pair must use the same device")
    source_zone = normalize_zone_id(_payload(source).get("zone_id"))
    restore_zone = normalize_zone_id(_payload(restore).get("zone_id"))
    if source_zone != restore_zone:
        raise ValueError("safety action pair must use the same zone")


def zone_matches_baseline(baseline: Any, current_target: Any) -> bool:
    """Return true only for known, finite, exact baseline equality."""

    if (
        isinstance(baseline, bool)
        or isinstance(current_target, bool)
        or not isinstance(baseline, (int, float))
        or not isinstance(current_target, (int, float))
    ):
        return False
    return (
        math.isfinite(float(baseline))
        and math.isfinite(float(current_target))
        and baseline == current_target
    )


def dhw_embargoed(actions: Iterable[Any], device_id: str | None) -> bool:
    """Return whether an unresolved DHW restore blocks a device's new DHW ON."""

    return any(
        is_restorative_action(action)
        and _action_value(action, "device_id") == device_id
        and str(_action_value(action, "action_type")) == "force_dhw_off"
        and _action_value(action, "status") in UNRESOLVED_STATUSES
        for action in actions
    )


def zone_embargoed(actions: Iterable[Any], action: Any, latest_status: Any) -> bool:
    """Return whether a matching unresolved zone restore blocks an increase."""

    action_type = _action_value(action, "type", _action_value(action, "action_type"))
    try:
        action_type = ActionType(action_type)
    except ValueError:
        return True
    if not is_room_heating_increase(action_type, _payload(action), latest_status):
        return False
    device_id = _action_value(action, "device_id")
    zone_id = normalize_zone_id(_payload(action).get("zone_id"))
    return any(
        is_restorative_action(candidate)
        and _action_value(candidate, "device_id") == device_id
        and str(_action_value(candidate, "action_type")) == "zone_temp_restore"
        and _action_value(candidate, "status") in UNRESOLVED_STATUSES
        and normalize_zone_id(_payload(candidate).get("zone_id")) == zone_id
        for candidate in actions
    )

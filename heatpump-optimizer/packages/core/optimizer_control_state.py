"""Read-only resolution of the optimizer control state."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping

import structlog
from sqlalchemy import and_, desc, select

from packages.core.database import get_session
from packages.core.device_data_quality import get_device_data_quality
from packages.core.learning_state import get_learning_state
from packages.core.models import DeviceStatusRecord, OverrideRecord, PlanActionRecord, PlanRecord
from packages.core.plan_lifecycle import ACTIVE_PLAN_STATUS
from packages.core.planning_data_quality import get_planning_data_quality
from packages.core.safety_reverts import (
    dhw_embargoed,
    is_restorative_action,
    unresolved_revert_predicate,
    zone_embargoed,
)
from packages.optimizer.actions import ActionType
from packages.optimizer.executor_gate import is_room_heating_increase

ControlStateName = Literal["paused_by_user", "observing", "holding", "comfort_at_risk", "automatic"]
logger = structlog.get_logger()


class ControlStateOverrideUnavailableError(RuntimeError):
    """Raised when active override state cannot be confirmed."""


@dataclass(frozen=True)
class ControlStateNotice:
    code: str
    severity: Literal["info", "warning", "danger"]
    detail: str


@dataclass(frozen=True)
class ControlStateAction:
    kind: Literal["link", "request"]
    label: str
    href: str | None = None
    endpoint: str | None = None
    method: str | None = None


@dataclass(frozen=True)
class ControlStateSnapshot:
    state: ControlStateName
    headline: str
    detail: str
    reason_code: str
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    override_id: int | None = None
    active_override_count: int = 0
    primary_action: ControlStateAction | None = None
    notices: list[ControlStateNotice] = field(default_factory=list)
    resolved_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    model_lifecycle: dict[str, dict[str, object]] = field(default_factory=dict)
    build_policy: Literal["observe_only"] = "observe_only"
    control_ready: bool = False
    ordinary_writes_allowed: bool = False
    optimization_requires_new_build: bool = True


def active_override_query(now: dt.datetime):
    """Select the controlling active override by its newest persistent identity."""

    return (
        select(OverrideRecord)
        .where(
            and_(
                OverrideRecord.active,
                OverrideRecord.ts_from <= now,
                OverrideRecord.ts_to >= now,
            )
        )
        .order_by(desc(OverrideRecord.id))
        .limit(1)
    )


async def get_controlling_override(session: Any, *, now: dt.datetime) -> OverrideRecord | None:
    """Return the one active override shared by dashboard and control-state reads."""

    return (await session.execute(active_override_query(now))).scalar_one_or_none()


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=dt.timezone.utc)
        if value.tzinfo is None
        else value.astimezone(dt.timezone.utc)
    )


def _next_action_is_embargoed(
    action: PlanActionRecord, unresolved_actions: list[PlanActionRecord], status: Any
) -> bool:
    """Match the executor's unresolved-revert applicability predicate."""

    if is_restorative_action(action):
        return False
    try:
        action_type = ActionType(action.action_type)
    except ValueError:
        return False
    payload = action.payload_json
    try:
        import json

        parsed_payload = json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        parsed_payload = {}
    if not isinstance(parsed_payload, dict):
        parsed_payload = {}
    heating_increase = is_room_heating_increase(action_type, parsed_payload, status)
    if action_type is not ActionType.FORCE_DHW_ON and not heating_increase:
        return False
    embargo_action = {
        "action_type": action_type,
        "device_id": action.device_id,
        "payload": parsed_payload,
    }
    return (
        action_type is ActionType.FORCE_DHW_ON
        and dhw_embargoed(unresolved_actions, action.device_id)
    ) or zone_embargoed(unresolved_actions, embargo_action, status)


async def _next_pending_action_embargoed(session: Any) -> bool:
    next_action = (
        await session.execute(
            select(PlanActionRecord)
            .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
            .where(
                PlanActionRecord.status == "pending",
                PlanActionRecord.reverts_action_id.is_(None),
                PlanRecord.status == ACTIVE_PLAN_STATUS,
            )
            .order_by(PlanActionRecord.scheduled_ts, PlanActionRecord.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if next_action is None or next_action.device_id is None:
        return False
    status = (
        await session.execute(
            select(DeviceStatusRecord)
            .where(DeviceStatusRecord.device_id == next_action.device_id)
            .order_by(DeviceStatusRecord.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if status is None:
        return False
    unresolved_actions = (
        (await session.execute(select(PlanActionRecord).where(unresolved_revert_predicate())))
        .scalars()
        .all()
    )
    return _next_action_is_embargoed(next_action, unresolved_actions, status)


async def resolve_control_state(
    *,
    now: dt.datetime | None = None,
    comfort_assessment: Mapping[str, object] | None = None,
) -> ControlStateSnapshot:
    from packages.ml.model_status import get_model_reporting_statuses

    snapshot = await _resolve_control_state(now=now, comfort_assessment=comfort_assessment)
    return replace(snapshot, model_lifecycle=await get_model_reporting_statuses())


async def _resolve_control_state(
    *,
    now: dt.datetime | None = None,
    comfort_assessment: Mapping[str, object] | None = None,
) -> ControlStateSnapshot:
    """Resolve persisted control gates without contacting Panasonic."""

    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    try:
        async with get_session() as session:
            try:
                override = await get_controlling_override(session, now=now)
                active_override_count = len(
                    (
                        await session.execute(
                            select(OverrideRecord.id).where(
                                and_(
                                    OverrideRecord.active,
                                    OverrideRecord.ts_from <= now,
                                    OverrideRecord.ts_to >= now,
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            except Exception as exc:  # noqa: BLE001 - active overrides are a dispatch gate
                logger.warning(
                    "control_state_dependency_failed",
                    dependency="active_override",
                    error_type=type(exc).__name__,
                )
                raise ControlStateOverrideUnavailableError() from exc
            if override is not None:
                learning = await get_learning_state()
                notices = (
                    [
                        ControlStateNotice(
                            code="observing_suppresses_safety_reverts",
                            severity="info",
                            detail="Observation mode is active, so safety reverts are also suppressed.",
                        )
                    ]
                    if learning.reliable and learning.active
                    else []
                )
                return ControlStateSnapshot(
                    state="paused_by_user",
                    headline="User override is active",
                    detail="Ordinary writes are prohibited in this build; existing linked restores may still run outside positive observation.",
                    reason_code="active_override",
                    since=_as_utc(override.ts_from),
                    until=_as_utc(override.ts_to),
                    override_id=override.id,
                    active_override_count=active_override_count,
                    primary_action=ControlStateAction(
                        kind="request",
                        label="Clear override",
                        endpoint=f"/api/overrides/{override.id}",
                        method="DELETE",
                    ),
                    notices=notices,
                    resolved_at=now,
                )

            learning = await get_learning_state()
            if learning.reliable and learning.active:
                return ControlStateSnapshot(
                    state="observing",
                    headline="Observation mode is active",
                    detail="No actions are dispatched, including linked restores. Optimization requires a new reviewed, approved build.",
                    reason_code="learning_mode_active",
                    primary_action=ControlStateAction(
                        kind="link", label="View plan", href="/?view=timeline"
                    ),
                    resolved_at=now,
                )
            if not learning.reliable:
                return _holding("learning_state_unavailable", now)

            quality = await get_device_data_quality(now=now)
            if not quality["ready"]:
                return _holding(str(quality["reasons"][0]), now)
            if await _next_pending_action_embargoed(session):
                return _holding("unresolved_safety_revert", now)

            if comfort_assessment and comfort_assessment.get("state") == "at_risk":
                return ControlStateSnapshot(
                    state="comfort_at_risk",
                    headline="Comfort may need attention",
                    detail="The forecast indicates comfort risk; this build does not dispatch ordinary writes.",
                    reason_code="comfort_at_risk",
                    primary_action=ControlStateAction(
                        kind="link", label="View plan", href="/?view=timeline"
                    ),
                    resolved_at=now,
                )

            planning_quality = await get_planning_data_quality(now=now)
            notices = (
                [
                    ControlStateNotice(
                        code="new_plans_paused",
                        severity="warning",
                        detail=(
                            "Diagnostic planning inputs are unavailable; "
                            "ordinary writes remain prohibited in this build."
                        ),
                    )
                ]
                if not planning_quality["control_allowed"]
                else []
            )
            unresolved_revert_notice = await session.execute(
                select(PlanActionRecord.id).where(unresolved_revert_predicate()).limit(1)
            )
            if unresolved_revert_notice.scalar_one_or_none() is not None:
                notices.append(
                    ControlStateNotice(
                        code="safety_restore_pending",
                        severity="info",
                        detail="An existing linked safety restore remains pending.",
                    )
                )
            return ControlStateSnapshot(
                state="observing",
                headline="Observe-only build",
                detail="Ordinary writes are prohibited in this build. Optimization requires a new reviewed, approved build; only existing linked restores may run outside positive observation.",
                reason_code="observe_only_build",
                primary_action=ControlStateAction(
                    kind="link", label="View plan", href="/?view=timeline"
                ),
                notices=notices,
                resolved_at=now,
            )
    except ControlStateOverrideUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - incomplete dependencies must hold control
        logger.warning(
            "control_state_dependency_failed",
            dependency="control_state_resolution",
            error_type=type(exc).__name__,
        )
        return _holding("control_state_unavailable", now)


def _holding(reason_code: str, now: dt.datetime) -> ControlStateSnapshot:
    return ControlStateSnapshot(
        state="holding",
        headline="Control evidence is unavailable",
        detail="Ordinary writes are prohibited in this build; linked restores still require their existing checks.",
        reason_code=reason_code,
        primary_action=ControlStateAction(kind="link", label="View plan", href="/?view=timeline"),
        resolved_at=now,
    )

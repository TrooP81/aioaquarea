"""Plan executor: dispatches actions and verifies the device applied them.

Verification consumes Panasonic API read budget. The executor therefore keeps batches small
(`MAX_ACTIONS_PER_CYCLE`) and polls at a bounded cadence (`VERIFY_POLL_INTERVAL_S`) so a single
cycle stays within the wrapper's rate limiter while still marking mismatches as failures instead
of silently succeeding.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import replace
from enum import StrEnum

import structlog
from sqlalchemy import and_, case, func, select, text, update

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.device_data_quality import get_device_data_quality
from packages.core.learning_state import get_learning_state_details
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    ExecutorVerificationReadRecord,
    SpaceHeatingGateRecord,
)
from packages.core.plan_lifecycle import ACTIVE_PLAN_STATUS
from packages.core.safety_reverts import (
    dhw_embargoed,
    due_revert_predicate,
    is_restorative_action,
    normalize_zone_id,
    restore_baseline_target,
    unresolved_revert_predicate,
    zone_embargoed,
    zone_matches_baseline,
)
from packages.core.services import AquareaWrapper
from packages.core.settings_service import get_space_heating_gate_config
from packages.core.space_heating_gate import SpaceHeatingGateState, resolve_effective_gate
from packages.optimizer.actions import (
    ActionType,
    VerificationObservation,
    VerifyResult,
    get_action_handler,
)
from packages.optimizer.executor_gate import is_room_heating_increase

logger = structlog.get_logger()

MAX_ACTIONS_PER_CYCLE = 3
EXECUTOR_VERIFY_READ_LIMIT = 5
INITIAL_VERIFY_CHECKPOINTS_S = (15, 30, 60)
INITIAL_LIVE_CHECKPOINTS_S = frozenset({15, 60})
REDISPATCH_VERIFY_CHECKPOINTS_S = (15, 30, 60)
REDISPATCH_LIVE_CHECKPOINTS_S = frozenset({60})
SAFETY_VERIFY_CHECKPOINTS_S = (15, 30, 60)
SAFETY_LIVE_CHECKPOINTS_S = frozenset({15, 60})
VERIFY_POLL_INTERVAL_S = 10
VERIFY_TIMEOUT_S = 60
VERIFY_REDISPATCH_ATTEMPTS = 1
SHUTDOWN_CANCEL_REASON = "shutdown_cancelled"
SAFETY_DISPATCH_MARGIN_S = 30
SAFETY_ACTION_STUCK_AFTER = dt.timedelta(minutes=2)
assert SAFETY_ACTION_STUCK_AFTER > dt.timedelta(
    seconds=max(SAFETY_VERIFY_CHECKPOINTS_S) + SAFETY_DISPATCH_MARGIN_S
)


class _VerificationEvidenceUnavailable(Exception):
    pass


class _VerificationLedgerUnavailable(Exception):
    pass


class _VerificationQuotaExhausted(Exception):
    pass


class LearningModeState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


async def resolve_learning_mode_state() -> LearningModeState:
    """Resolve observe-only learning mode without treating lookup errors as inactive.

    A lookup failure leaves ordinary control unsafe, but a pending restorative action
    remains eligible for the executor's separate safety lane.
    """
    state, seasonal = await get_learning_state_details()
    if not state.reliable:
        return LearningModeState.UNKNOWN
    if state.seasonal_active:
        logger.info("executor_seasonal_calibration_active", **seasonal)
    return LearningModeState.ACTIVE if state.active else LearningModeState.INACTIVE


async def is_learning_mode_active() -> bool:
    """Compatibility wrapper for callers that only need positive learning evidence."""
    return await resolve_learning_mode_state() is LearningModeState.ACTIVE


class PlanExecutor:
    """Executes pending plan actions respecting overrides and rate limits."""

    def __init__(
        self,
        wrapper: AquareaWrapper,
        *,
        session_factory=get_session,
        sleep=asyncio.sleep,
        learning_check=resolve_learning_mode_state,
        device_quality_check=get_device_data_quality,
    ):
        self._wrapper = wrapper
        self._session_factory = session_factory
        self._sleep = sleep
        self._learning_check = learning_check
        self._device_quality_check = device_quality_check

    async def execute_due_actions(self) -> None:
        """Find and execute all actions whose scheduled time has passed."""
        actions: list[PlanActionRecord] = []
        now = dt.datetime.now(dt.timezone.utc)

        try:
            learning_state = await self._learning_check()
        except Exception as exc:  # noqa: BLE001 - injected checks must not admit ordinary work
            logger.error(
                "learning_mode_state_unknown",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            learning_state = LearningModeState.UNKNOWN
        if learning_state is True:
            learning_state = LearningModeState.ACTIVE
        elif learning_state is False:
            learning_state = LearningModeState.INACTIVE

        # Positive observe-only learning leaves every action untouched, including
        # restores. Unknown state permits only the existing single safety lane.
        if learning_state is LearningModeState.ACTIVE:
            logger.info(
                "executor_learning_mode_active",
                reason="observe-only training mode",
                skipping=0,
            )
            return

        safety_action = await self._claim_due_safety_action(now)
        if safety_action is not None:
            try:
                await self._execute_safety_action(safety_action)
            except asyncio.CancelledError:
                await self._requeue_safety_action(safety_action, "shutdown_cancelled")
                raise
            return

        if learning_state is LearningModeState.UNKNOWN:
            logger.info("executor_learning_mode_unknown_skipping_ordinary_actions")
            return

        async with self._session_factory() as session:
            override_result = await session.execute(
                select(OverrideRecord).where(
                    and_(
                        OverrideRecord.active,
                        OverrideRecord.ts_from <= now,
                        OverrideRecord.ts_to >= now,
                    )
                )
            )
            active_overrides = override_result.scalars().all()

            result = await session.execute(
                select(PlanActionRecord)
                .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
                .where(
                    and_(
                        PlanActionRecord.status == "pending",
                        PlanActionRecord.scheduled_ts <= now,
                        ~unresolved_revert_predicate(),
                        PlanRecord.status == ACTIVE_PLAN_STATUS,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(MAX_ACTIONS_PER_CYCLE)
                # Lock both the action and its active plan so replacement plans
                # cannot race a due command in another executor cycle.
                .with_for_update()
            )
            actions = result.scalars().all()

            if actions:
                await session.execute(
                    update(PlanActionRecord)
                    .where(
                        and_(
                            PlanActionRecord.id.in_([action.id for action in actions]),
                            PlanActionRecord.status == "pending",
                        )
                    )
                    .values(status="executing")
                )

            if active_overrides and actions:
                override_reason = active_overrides[0].reason or "manual override"
                logger.info(
                    "executor_overrides_active",
                    count=len(active_overrides),
                    reason=override_reason,
                    skipping=len(actions),
                )
                for action in actions:
                    await session.execute(
                        update(PlanActionRecord)
                        .where(PlanActionRecord.id == action.id)
                        .values(
                            status="skipped",
                            executed_at=now,
                            result_json=json.dumps(
                                {"reason": "override_active", "override": override_reason}
                            ),
                        )
                    )
                return

            if active_overrides:
                logger.info(
                    "executor_overrides_active",
                    count=len(active_overrides),
                    reason=active_overrides[0].reason,
                )
                return

        try:
            for action in actions:
                await self._execute_action(action)
        except asyncio.CancelledError:
            await self._reconcile_claimed_batch_cancelled(actions)
            raise

    async def _claim_due_safety_action(self, now: dt.datetime) -> PlanActionRecord | None:
        """Claim one oldest linked restore without depending on its parent plan."""

        async with self._session_factory() as session:
            attempts = func.coalesce(PlanActionRecord.safety_attempt_count, 0) + 1
            await session.execute(
                update(PlanActionRecord)
                .where(
                    unresolved_revert_predicate(),
                    PlanActionRecord.status.in_(["executing", "dispatched"]),
                    (PlanActionRecord.safety_claimed_at.is_(None))
                    | (PlanActionRecord.safety_claimed_at <= now - SAFETY_ACTION_STUCK_AFTER),
                )
                .values(
                    status="pending",
                    safety_claimed_at=None,
                    safety_attempt_count=attempts,
                    safety_next_retry_at=case(
                        (attempts <= 3, now + dt.timedelta(minutes=1)),
                        else_=now + dt.timedelta(minutes=15),
                    ),
                    result_json=json.dumps({"reason": "stuck_safety_recovery"}),
                )
            )
            action = (
                await session.execute(
                    select(PlanActionRecord)
                    .where(
                        due_revert_predicate(),
                        PlanActionRecord.scheduled_ts <= now,
                        (PlanActionRecord.safety_next_retry_at.is_(None))
                        | (PlanActionRecord.safety_next_retry_at <= now),
                    )
                    .order_by(PlanActionRecord.scheduled_ts, PlanActionRecord.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if action is not None:
                action.status = "executing"
                action.safety_claimed_at = now
            return action

    async def _execute_safety_action(self, action: PlanActionRecord) -> None:
        """Dispatch one safety restore with priority capacity and no redispatch."""

        try:
            payload = json.loads(action.payload_json) if action.payload_json else {}
            action_type = ActionType(action.action_type)
            precondition = await self._dispatch_precondition(action, action_type, payload)
            if precondition is not None:
                await self._requeue_safety_action(
                    action, str(precondition.get("reason", "precondition"))
                )
                return
            already_safe = await self._safety_already_safe(action, action_type, payload)
            if already_safe:
                await self._mark_verified(
                    action,
                    0,
                    VerifyResult(
                        ok=True,
                        observed_value="already_safe",
                        expected_value="already_safe",
                        reason="already_safe",
                    ),
                )
                return
            handler = get_action_handler(action_type)
            with self._wrapper.safety_write():
                expected_state = await handler.dispatch(self._wrapper, payload) or {}
            if expected_state.get("skip"):
                await self._requeue_safety_action(
                    action, str(expected_state.get("reason", "live_precondition"))
                )
                return
            now = dt.datetime.now(dt.timezone.utc)
            async with self._session_factory() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="dispatched",
                        executed_at=now,
                        expected_state_json=json.dumps(expected_state),
                    )
                )
            result, attempts = await self._poll_until_verified(
                action=action,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=0,
                lane="safety",
                phase="safety",
                checkpoints=SAFETY_VERIFY_CHECKPOINTS_S,
                live_checkpoints=SAFETY_LIVE_CHECKPOINTS_S,
                evidence_after=now,
            )
            if result.ok:
                await self._mark_verified(action, attempts, result)
            else:
                await self._requeue_safety_action(action, result.reason)
        except asyncio.CancelledError:
            raise
        except (
            _VerificationEvidenceUnavailable,
            _VerificationLedgerUnavailable,
            _VerificationQuotaExhausted,
        ) as exc:
            await self._requeue_safety_action(action, type(exc).__name__)
        except Exception as exc:  # safety failures are retried, never terminal
            await self._requeue_safety_action(action, type(exc).__name__)

    async def _safety_already_safe(
        self, action: PlanActionRecord, action_type: ActionType, payload: dict
    ) -> bool:
        """Use fresh persisted evidence to finish a linked revert without a write."""

        async with self._session_factory() as session:
            status = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(DeviceStatusRecord.device_id == action.device_id)
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            source_action_id = getattr(action, "reverts_action_id", None)
            source = (
                await session.get(PlanActionRecord, source_action_id)
                if source_action_id is not None
                else None
            )
        if status is None:
            return False
        if action_type is ActionType.FORCE_DHW_OFF:
            return status.force_dhw == 0
        if action_type is not ActionType.ZONE_TEMP_RESTORE:
            return False
        baseline = restore_baseline_target(action)
        source_payload = json.loads(source.payload_json) if source and source.payload_json else {}
        source_baseline = source_payload.get("baseline_temperature")
        if source_baseline is not None and not zone_matches_baseline(source_baseline, baseline):
            logger.error(
                "safety_restore_baseline_mismatch",
                action_id=action.id,
                source_action_id=source_action_id,
                source_baseline=source_baseline,
                restore_baseline=baseline,
            )
            raise ValueError("safety_restore_baseline_mismatch")
        zone_id = normalize_zone_id(payload.get("zone_id"))
        current_target = getattr(status, f"zone{zone_id}_target_temp", None)
        return zone_matches_baseline(baseline, current_target)

    async def _requeue_safety_action(self, action: PlanActionRecord, reason: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        attempts = int(action.safety_attempt_count or 0) + 1
        delay = dt.timedelta(minutes=1 if attempts <= 3 else 15)
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="pending",
                    safety_attempt_count=attempts,
                    safety_next_retry_at=now + delay,
                    result_json=json.dumps({"reason": reason}),
                )
            )
        logger.warning(
            "safety_revert_requeued", action_id=action.id, attempts=attempts, reason=reason
        )

    async def _reconcile_claimed_batch_cancelled(
        self, claimed_actions: list[PlanActionRecord]
    ) -> None:
        """Cancel any still-executing actions from a claimed batch after interruption."""
        if not claimed_actions:
            return

        claimed_ids = [action.id for action in claimed_actions]
        action_by_id = {action.id: action for action in claimed_actions}
        now = dt.datetime.now(dt.timezone.utc)

        async with self._session_factory() as session:
            executing_result = await session.execute(
                select(PlanActionRecord.id).where(
                    and_(
                        PlanActionRecord.id.in_(claimed_ids),
                        PlanActionRecord.status == "executing",
                    )
                )
            )
            executing_ids = list(executing_result.scalars().all())
            if not executing_ids:
                return

            await session.execute(
                update(PlanActionRecord)
                .where(
                    and_(
                        PlanActionRecord.id.in_(executing_ids),
                        PlanActionRecord.status == "executing",
                    )
                )
                .values(
                    status="cancelled",
                    executed_at=now,
                    result_json=json.dumps(
                        {
                            "reason": SHUTDOWN_CANCEL_REASON,
                            "detail": "Executor shutdown interrupted action verification",
                        }
                    ),
                )
            )

            for action_id in executing_ids:
                action = action_by_id.get(action_id)
                if action is None:
                    continue
                session.add(
                    AuditLogRecord(
                        actor="optimizer",
                        action=action.action_type,
                        payload_json=action.payload_json,
                        result="cancelled",
                    )
                )

    async def _execute_action(self, action: PlanActionRecord) -> None:
        """Execute a single action, then synchronously verify it."""
        try:
            payload = json.loads(action.payload_json) if action.payload_json else {}
            action_type = ActionType(action.action_type)
            precondition = await self._dispatch_precondition(action, action_type, payload)
            if precondition is not None:
                if precondition["reason"] in {
                    "credentials_missing",
                    "device_status_missing",
                    "device_status_stale",
                    "quality_check_failed",
                }:
                    await self._defer_action(action, precondition)
                    return
                await self._skip_action(action, precondition)
                return
            handler = get_action_handler(action_type)

            expected_state = await handler.dispatch(self._wrapper, payload) or {}
            now = dt.datetime.now(dt.timezone.utc)
            if expected_state.get("skip"):
                result = {
                    "reason": expected_state.get("reason", "action_precondition_not_met"),
                    "detail": "Automatic command was not sent after its final live-device safety check",
                    "observed": {
                        key: value
                        for key, value in expected_state.items()
                        if key not in {"skip", "reason"}
                    },
                }
                async with self._session_factory() as session:
                    await session.execute(
                        update(PlanActionRecord)
                        .where(PlanActionRecord.id == action.id)
                        .values(
                            status="skipped",
                            executed_at=now,
                            expected_state_json=json.dumps(expected_state),
                            result_json=json.dumps(result),
                        )
                    )
                logger.info(
                    "action_skipped_live_precondition",
                    action_type=action.action_type,
                    action_id=action.id,
                    reason=result["reason"],
                )
                return

            async with self._session_factory() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="dispatched",
                        executed_at=now,
                        expected_state_json=json.dumps(expected_state),
                        verify_attempts=0,
                        last_observed_json=None,
                        result_json=json.dumps({"dispatched": True}),
                    )
                )

            logger.info(
                "action_dispatched",
                action_type=action.action_type,
                action_id=action.id,
                expected_state=expected_state,
            )

            await self._verify_with_retry(action, payload, expected_state, now)

        except asyncio.CancelledError:
            logger.warning(
                "action_cancelled",
                action_type=action.action_type,
                action_id=action.id,
            )
            await self._mark_cancelled(action)
            raise
        except ValueError:
            logger.warning("executor_unknown_action", action_type=action.action_type)
        except Exception as exc:
            logger.error(
                "action_failed", action_type=action.action_type, action_id=action.id, error=str(exc)
            )
            async with self._session_factory() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="failed",
                        executed_at=dt.datetime.now(dt.timezone.utc),
                        result_json=json.dumps({"error": str(exc)}),
                    )
                )

    async def _skip_action(self, action: PlanActionRecord, result: dict[str, object]) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="skipped",
                    executed_at=dt.datetime.now(dt.timezone.utc),
                    result_json=json.dumps(result),
                )
            )

    async def _defer_action(self, action: PlanActionRecord, result: dict[str, object]) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .where(PlanActionRecord.status == "executing")
                .values(status="pending", result_json=json.dumps(result))
            )
        logger.warning(
            "action_deferred_device_data_quality",
            action_id=action.id,
            action_type=action.action_type,
            reason=result["reason"],
        )

    async def _dispatch_precondition(
        self, action, action_type, payload
    ) -> dict[str, object] | None:
        try:
            selected_device_id = await self._wrapper.get_selected_device_id()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"reason": "action_device_unresolvable", "identity_error": type(exc).__name__}
        if action.device_id and action.device_id != selected_device_id:
            return {"reason": "action_device_unresolvable", "device_id": action.device_id}
        try:
            quality = await self._device_quality_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - readiness failures must block control
            logger.error(
                "action_device_data_quality_check_failed",
                action_id=action.id,
                action_type=action_type.value,
                reason="quality_check_failed",
                error_type=type(exc).__name__,
            )
            return {"reason": "quality_check_failed", "device_id": selected_device_id}
        if not quality["ready"]:
            return {"reason": quality["reasons"][0], "device_id": selected_device_id}
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=quality["threshold_seconds"]
        )
        async with self._session_factory() as session:
            status = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == selected_device_id,
                        DeviceStatusRecord.ts >= cutoff,
                    )
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if status is None:
                return {"reason": "device_status_stale", "device_id": selected_device_id}
            if action.device_id is None:
                fresh_ids = (
                    (
                        await session.execute(
                            select(DeviceStatusRecord.device_id)
                            .where(DeviceStatusRecord.ts >= cutoff)
                            .distinct()
                        )
                    )
                    .scalars()
                    .all()
                )
                if set(fresh_ids) != {selected_device_id}:
                    return {"reason": "action_device_unresolvable", "device_id": selected_device_id}
            heating_increase = is_room_heating_increase(action_type, payload, status)
            if not is_restorative_action(action) and (
                action_type is ActionType.FORCE_DHW_ON or heating_increase
            ):
                unresolved_actions = (
                    (
                        await session.execute(
                            select(PlanActionRecord).where(unresolved_revert_predicate())
                        )
                    )
                    .scalars()
                    .all()
                )
                embargo_action = {
                    "action_type": action_type,
                    "device_id": selected_device_id,
                    "payload": payload,
                }
                if (
                    action_type is ActionType.FORCE_DHW_ON
                    and dhw_embargoed(unresolved_actions, selected_device_id)
                ) or zone_embargoed(unresolved_actions, embargo_action, status):
                    logger.warning(
                        "action_blocked_by_unresolved_revert",
                        action_id=action.id,
                        action_type=action.action_type,
                        device_id=selected_device_id,
                    )
                    return {
                        "reason": "blocked_by_unresolved_revert",
                        "device_id": selected_device_id,
                    }
            if not heating_increase:
                return None
            gate_row = (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == selected_device_id
                    )
                )
            ).scalar_one_or_none()
        evidence = resolve_effective_gate(gate_row, await get_space_heating_gate_config())
        if evidence.state is not SpaceHeatingGateState.ALLOWED:
            return {
                "reason": "space_heating_gate_blocked"
                if evidence.state is SpaceHeatingGateState.BLOCKED
                else "space_heating_gate_unknown",
                "device_id": selected_device_id,
                "profile_id": evidence.profile_id,
                "gate_reason": evidence.reason_code,
            }
        return None

    async def _mark_cancelled(self, action: PlanActionRecord) -> None:
        """Reconcile in-flight cancellation so dispatched actions do not get stranded."""
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="cancelled",
        )
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="cancelled",
                    executed_at=now,
                    result_json=json.dumps(
                        {
                            "reason": SHUTDOWN_CANCEL_REASON,
                            "detail": "Executor shutdown interrupted action verification",
                        }
                    ),
                )
            )
            session.add(audit_record)

    async def _verify_with_retry(
        self,
        action: PlanActionRecord,
        payload: dict,
        expected_state: dict[str, object],
        dispatched_at: dt.datetime | None = None,
    ) -> None:
        action_type = ActionType(action.action_type)
        handler = get_action_handler(action_type)
        dispatched_at = dispatched_at or dt.datetime.now(dt.timezone.utc)
        if not handler.verification_supported:
            await self._mark_executed_unverified(action, "verification_not_supported")
            return
        try:
            initial, attempts = await self._poll_until_verified(
                action=action,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=0,
                lane="ordinary",
                phase="initial",
                checkpoints=INITIAL_VERIFY_CHECKPOINTS_S,
                live_checkpoints=INITIAL_LIVE_CHECKPOINTS_S,
                evidence_after=dispatched_at,
            )
        except asyncio.CancelledError:
            raise
        except _VerificationQuotaExhausted:
            await self._mark_executed_unverified(action, "verification_quota_exhausted")
            return
        except _VerificationLedgerUnavailable:
            await self._mark_executed_unverified(action, "verification_ledger_unavailable")
            return
        except _VerificationEvidenceUnavailable:
            await self._mark_executed_unverified(action, "verification_evidence_unavailable")
            return
        if initial.ok:
            await self._mark_verified(action, attempts, initial)
            return
        expected_state = (
            await handler.redispatch_expected(self._wrapper, payload, expected_state)
            or expected_state
        )
        redispatched_at = dt.datetime.now(dt.timezone.utc)
        try:
            final, attempts = await self._poll_until_verified(
                action=action,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=attempts,
                lane="ordinary",
                phase="redispatch",
                checkpoints=REDISPATCH_VERIFY_CHECKPOINTS_S,
                live_checkpoints=REDISPATCH_LIVE_CHECKPOINTS_S,
                evidence_after=redispatched_at,
            )
        except asyncio.CancelledError:
            raise
        except _VerificationQuotaExhausted:
            await self._mark_executed_unverified(action, "verification_quota_exhausted")
            return
        except _VerificationLedgerUnavailable:
            await self._mark_executed_unverified(action, "verification_ledger_unavailable")
            return
        except _VerificationEvidenceUnavailable:
            await self._mark_executed_unverified(action, "verification_evidence_unavailable")
            return
        if final.ok:
            await self._mark_verified(action, attempts, final)
        else:
            await self._mark_failed(
                action, attempts, replace(final, reason="verification_mismatch_after_redispatch")
            )

    async def _poll_until_verified(
        self,
        *,
        action: PlanActionRecord,
        handler,
        payload: dict,
        expected_state: dict[str, object],
        attempts: int,
        lane: str,
        phase: str,
        checkpoints: tuple[int, ...],
        live_checkpoints: frozenset[int],
        evidence_after: dt.datetime,
    ) -> tuple[VerifyResult, int]:
        result = VerifyResult(ok=False, expected_value=expected_state, reason="not_verified")
        previous_checkpoint = 0
        for checkpoint in checkpoints:
            await self._sleep(checkpoint - previous_checkpoint)
            previous_checkpoint = checkpoint
            try:
                persisted = await self._load_persisted_observation(action.device_id, evidence_after)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "verification_ledger_unavailable",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="persisted",
                    error_type=type(exc).__name__,
                )
                raise _VerificationLedgerUnavailable() from exc
            if persisted is not None:
                result = handler.verify(persisted, payload, expected_state)
                await self._store_verification_progress(action.id, attempts, result)
                logger.info(
                    "verification_evidence",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="persisted",
                    admission_result="not_required",
                    reason=result.reason,
                )
                if result.ok:
                    return result, attempts
            if checkpoint not in live_checkpoints:
                continue
            try:
                admitted = await self._reserve_verification_read(action, lane, phase, checkpoint)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "verification_ledger_unavailable",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="live",
                    error_type=type(exc).__name__,
                )
                raise _VerificationLedgerUnavailable() from exc
            if not admitted:
                logger.info(
                    "verification_reservation",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="live",
                    admission_result="denied",
                    reason="verification_quota_exhausted",
                )
                raise _VerificationQuotaExhausted()
            try:
                device = await self._wrapper.refresh_device()
                result = handler.verify(
                    self._normalize_live_observation(device), payload, expected_state
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _VerificationEvidenceUnavailable() from exc
            attempts += 1
            await self._store_verification_progress(action.id, attempts, result)
            logger.info(
                "verification_evidence",
                lane=lane,
                action_id=action.id,
                action_type=action.action_type,
                checkpoint=checkpoint,
                evidence_source="live",
                admission_result="admitted",
                reason=result.reason,
            )
            if result.ok:
                return result, attempts
        return result, attempts

    async def _reserve_verification_read(
        self, action, lane: str, phase: str, checkpoint: int
    ) -> bool:
        async with self._session_factory() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(text("SELECT pg_advisory_xact_lock(1163280966, 1)"))
            db_now = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
            await session.execute(
                text("DELETE FROM executor_verification_reads WHERE reserved_at < :cutoff"),
                {"cutoff": db_now - dt.timedelta(days=7)},
            )
            total = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM executor_verification_reads WHERE reserved_at > :cutoff"
                    ),
                    {"cutoff": db_now - dt.timedelta(hours=1)},
                )
            ).scalar_one()
            ordinary = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM executor_verification_reads WHERE lane = 'ordinary' AND reserved_at > :cutoff"
                    ),
                    {"cutoff": db_now - dt.timedelta(hours=1)},
                )
            ).scalar_one()
            reserve = settings.executor_safety_read_reserve
            admitted = total < EXECUTOR_VERIFY_READ_LIMIT and (
                lane != "ordinary" or ordinary < EXECUTOR_VERIFY_READ_LIMIT - reserve
            )
            if admitted:
                session.add(
                    ExecutorVerificationReadRecord(
                        reserved_at=db_now,
                        device_id=action.device_id,
                        lane=lane,
                        action_id=action.id,
                        phase=phase,
                        checkpoint_seconds=checkpoint,
                    )
                )
            return admitted

    async def _load_persisted_observation(self, device_id: str | None, evidence_after: dt.datetime):
        if not device_id:
            return None
        async with self._session_factory() as session:
            record = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == device_id,
                        DeviceStatusRecord.ts > evidence_after,
                    )
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if record is None:
            return None
        try:
            from aioaquarea.data import SpecialStatus

            special_status = (
                SpecialStatus(record.special_status).name
                if record.special_status is not None
                else None
            )
        except (TypeError, ValueError):
            return None
        return VerificationObservation(
            record.force_dhw,
            record.quiet_mode,
            special_status,
            record.tank_temp,
            record.tank_target_temp,
            {0: record.zone1_target_temp, 1: record.zone1_target_temp, 2: record.zone2_target_temp},
        )

    @staticmethod
    def _normalize_live_observation(device) -> VerificationObservation:
        tank = getattr(device, "tank", None)
        zones = getattr(device, "zones", {}) or {}
        return VerificationObservation(
            getattr(getattr(device, "force_dhw", None), "value", None),
            getattr(getattr(device, "quiet_mode", None), "value", None),
            getattr(getattr(device, "special_status", None), "name", None),
            getattr(tank, "temperature", None),
            getattr(tank, "target_temperature", None),
            {
                int(zone_id): getattr(zone, "heat_target_temperature", None)
                for zone_id, zone in zones.items()
            },
        )

    async def _store_verification_progress(
        self, action_id: int, attempts: int, result: VerifyResult
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action_id)
                .values(
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                )
            )

    async def _mark_verified(
        self, action: PlanActionRecord, attempts: int, result: VerifyResult
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="success",
        )
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="executed",
                    executed_at=now,
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                    result_json=json.dumps({"success": True, "verified": True}),
                )
            )
            session.add(audit_record)
        logger.info(
            "action_verified",
            action_type=action.action_type,
            action_id=action.id,
            verify_attempts=attempts,
        )

    async def _mark_executed_unverified(self, action: PlanActionRecord, reason: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="executed_unverified",
                    executed_at=dt.datetime.now(dt.timezone.utc),
                    result_json=json.dumps({"success": True, "verified": False, "reason": reason}),
                )
            )
        logger.warning(
            "action_executed_unverified",
            action_id=action.id,
            action_type=action.action_type,
            reason=reason,
        )

    async def _mark_failed(
        self, action: PlanActionRecord, attempts: int, result: VerifyResult
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="failed",
        )
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="failed",
                    executed_at=now,
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                    result_json=json.dumps(
                        {
                            "success": False,
                            "verified": False,
                            "reason": result.reason,
                            "observed": result.observed_value,
                            "expected": result.expected_value,
                        }
                    ),
                )
            )
            session.add(audit_record)
        logger.error(
            "action_verification_failed",
            action_type=action.action_type,
            action_id=action.id,
            verify_attempts=attempts,
            observed=result.observed_value,
            expected=result.expected_value,
            reason=result.reason,
        )

    async def expire_stale_actions(self) -> None:
        """Mark stale pending actions as expired with a diagnostic reason.

        Runs periodically to catch actions that the executor never picked up
        (e.g. scheduled during an override window, or from a superseded plan).
        Only expires actions older than 2 minutes to avoid racing with
        execute_due_actions.
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - SAFETY_ACTION_STUCK_AFTER
        device_quality = await self._device_quality_check(now=now)

        async with self._session_factory() as session:
            result = await session.execute(
                select(PlanActionRecord)
                .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
                .where(
                    and_(
                        PlanActionRecord.status.in_(["pending", "executing"]),
                        PlanActionRecord.scheduled_ts <= cutoff,
                        ~unresolved_revert_predicate(),
                        PlanRecord.status == ACTIVE_PLAN_STATUS,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(20)
            )
            stale = result.scalars().all()

            if not stale:
                return

            latest_plan_result = await session.execute(
                select(PlanRecord.id).order_by(PlanRecord.created_at.desc()).limit(1)
            )
            latest_plan_id = latest_plan_result.scalar_one_or_none()

            for action in stale:
                reason = await self._diagnose_missed(session, action, latest_plan_id, now)
                if not device_quality["ready"] and is_restorative_action(action):
                    logger.info(
                        "action_expiry_deferred_device_data_quality",
                        action_id=action.id,
                        reasons=device_quality["reasons"],
                    )
                    continue
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .where(PlanActionRecord.status.in_(["pending", "executing"]))
                    .values(
                        status="expired",
                        executed_at=now,
                        result_json=json.dumps(reason),
                    )
                )
                logger.info(
                    "action_expired",
                    action_id=action.id,
                    action_type=action.action_type,
                    diagnosis=reason.get("reason"),
                )

    @staticmethod
    async def _diagnose_missed(
        session, action: PlanActionRecord, latest_plan_id: int | None, now
    ) -> dict:
        """Determine why a pending action was never executed."""
        scheduled = action.scheduled_ts
        gap_minutes = round((now - scheduled).total_seconds() / 60, 1)

        if latest_plan_id and action.plan_id != latest_plan_id:
            return {
                "reason": "superseded",
                "gap_minutes": gap_minutes,
                "detail": f"Replaced by plan #{latest_plan_id}",
            }

        window_end = scheduled + dt.timedelta(minutes=2)
        override_result = await session.execute(
            select(OverrideRecord)
            .where(
                and_(
                    OverrideRecord.ts_from <= window_end,
                    OverrideRecord.ts_to >= scheduled,
                )
            )
            .limit(1)
        )
        blocking_override = override_result.scalar_one_or_none()
        if blocking_override:
            return {
                "reason": "override_active",
                "override": blocking_override.reason or "manual override",
                "gap_minutes": gap_minutes,
                "detail": f"Override '{blocking_override.reason}' was active at scheduled time",
            }

        plan_window_start = scheduled - dt.timedelta(minutes=1)
        plan_window_end = scheduled + dt.timedelta(minutes=3)
        plan_result = await session.execute(
            select(PlanRecord.id, PlanRecord.created_at)
            .where(
                and_(
                    PlanRecord.created_at >= plan_window_start,
                    PlanRecord.created_at <= plan_window_end,
                )
            )
            .order_by(PlanRecord.created_at.desc())
            .limit(1)
        )
        concurrent_plan = plan_result.one_or_none()
        if concurrent_plan:
            return {
                "reason": "optimization_overlap",
                "concurrent_plan_id": concurrent_plan[0],
                "gap_minutes": gap_minutes,
                "detail": (
                    f"Plan #{concurrent_plan[0]} was being generated at "
                    f"{concurrent_plan[1].strftime('%H:%M:%S')} - may have blocked the executor"
                ),
            }

        if gap_minutes > 10:
            return {
                "reason": "executor_gap",
                "gap_minutes": gap_minutes,
                "detail": f"Executor did not run for ~{round(gap_minutes)} min after scheduled time",
            }

        return {
            "reason": "timing",
            "gap_minutes": gap_minutes,
            "detail": (
                f"Action was due {gap_minutes} min ago but was never picked up "
                f"- possible event-loop delay or transient DB error"
            ),
        }

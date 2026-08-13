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

import structlog
from sqlalchemy import and_, select, update

from packages.core.database import get_session
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
)
from packages.core.plan_lifecycle import ACTIVE_PLAN_STATUS
from packages.core.services import AquareaWrapper
from packages.optimizer.actions import ActionType, VerifyResult, get_action_handler

logger = structlog.get_logger()

MAX_ACTIONS_PER_CYCLE = 3
VERIFY_POLL_INTERVAL_S = 10
VERIFY_TIMEOUT_S = 60
VERIFY_REDISPATCH_ATTEMPTS = 1


def _device_status_freshness_cutoff(now: dt.datetime) -> dt.datetime:
    """Allow normal poll jitter, but never dispatch against stale pump state."""
    from packages.core.config import settings

    max_age_seconds = max(int(settings.poll_interval_seconds) * 3, 15 * 60)
    return now - dt.timedelta(seconds=max_age_seconds)


async def is_learning_mode_active() -> bool:
    """Return True when observe-only learning mode is enabled.

    In learning mode the executor dispatches no device commands so the heat pump
    runs on its own native schedule, letting the poller collect clean, natural-
    behaviour data for ML training over a long period. Defensive: any lookup error
    is treated as "not active" so a transient settings failure never blocks control.
    """
    from packages.core.settings_service import get_bool_setting

    try:
        if await get_bool_setting("learning_mode_enabled"):
            return True
        from packages.ml.seasonal_learning import get_seasonal_calibration_status

        seasonal = await get_seasonal_calibration_status()
        if seasonal["observe_only_active"]:
            logger.info("executor_seasonal_calibration_active", **seasonal)
            return True
        return False
    except Exception as exc:  # noqa: BLE001 - never let a settings error pause control
        logger.warning("learning_mode_check_failed", error=str(exc))
        return False


class PlanExecutor:
    """Executes pending plan actions respecting overrides and rate limits."""

    def __init__(self, wrapper: AquareaWrapper):
        self._wrapper = wrapper

    async def execute_due_actions(self) -> None:
        """Find and execute all actions whose scheduled time has passed."""
        now = dt.datetime.now(dt.timezone.utc)

        async with get_session() as session:
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
                        PlanRecord.status == ACTIVE_PLAN_STATUS,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(MAX_ACTIONS_PER_CYCLE)
                # Lock both the action and its active plan.  A replacement
                # plan waits until these actions are either claimed or left
                # pending, so it cannot race a command sent to the pump.
                .with_for_update()
            )
            actions = result.scalars().all()

            if actions:
                latest_status_ts = (
                    await session.execute(
                        select(DeviceStatusRecord.ts)
                        .order_by(DeviceStatusRecord.ts.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                freshness_cutoff = _device_status_freshness_cutoff(now)
                if latest_status_ts is None or latest_status_ts < freshness_cutoff:
                    age_seconds = (
                        None
                        if latest_status_ts is None
                        else round((now - latest_status_ts).total_seconds())
                    )
                    logger.warning(
                        "executor_device_status_stale",
                        action_count=len(actions),
                        latest_status=latest_status_ts.isoformat() if latest_status_ts else None,
                        age_seconds=age_seconds,
                    )
                    for action in actions:
                        await session.execute(
                            update(PlanActionRecord)
                            .where(PlanActionRecord.id == action.id)
                            .where(PlanActionRecord.status == "pending")
                            .values(
                                status="skipped",
                                executed_at=now,
                                result_json=json.dumps(
                                    {
                                        "reason": "device_status_stale",
                                        "detail": "Automatic command not sent because live pump status was stale",
                                        "latest_status": latest_status_ts.isoformat()
                                        if latest_status_ts
                                        else None,
                                    }
                                ),
                            )
                        )
                    return

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

            if await is_learning_mode_active():
                logger.info(
                    "executor_learning_mode_active",
                    reason="observe-only training mode",
                    skipping=len(actions),
                )
                for action in actions:
                    await session.execute(
                        update(PlanActionRecord)
                        .where(PlanActionRecord.id == action.id)
                        .values(
                            status="skipped",
                            executed_at=now,
                            result_json=json.dumps({"reason": "learning_mode"}),
                        )
                    )
                return

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

        for action in actions:
            await self._execute_action(action)

    async def _execute_action(self, action: PlanActionRecord) -> None:
        """Execute a single action, then synchronously verify it."""
        try:
            payload = json.loads(action.payload_json) if action.payload_json else {}
            action_type = ActionType(action.action_type)
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
                async with get_session() as session:
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

            async with get_session() as session:
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

            await self._verify_with_retry(action, payload, expected_state)

        except ValueError:
            logger.warning("executor_unknown_action", action_type=action.action_type)
        except Exception as exc:
            logger.error(
                "action_failed", action_type=action.action_type, action_id=action.id, error=str(exc)
            )
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="failed",
                        executed_at=dt.datetime.now(dt.timezone.utc),
                        result_json=json.dumps({"error": str(exc)}),
                    )
                )

    async def _verify_with_retry(
        self,
        action: PlanActionRecord,
        payload: dict,
        expected_state: dict[str, object],
    ) -> None:
        action_type = ActionType(action.action_type)
        handler = get_action_handler(action_type)
        attempts = 0
        last_result = VerifyResult(ok=False, expected_value=expected_state, reason="not_verified")

        for dispatch_attempt in range(VERIFY_REDISPATCH_ATTEMPTS + 1):
            last_result, attempts = await self._poll_until_verified(
                action_id=action.id,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=attempts,
            )
            if last_result.ok:
                await self._mark_verified(action, attempts, last_result)
                return

            if dispatch_attempt < VERIFY_REDISPATCH_ATTEMPTS:
                logger.warning(
                    "action_verification_retrying",
                    action_id=action.id,
                    action_type=action.action_type,
                    observed=last_result.observed_value,
                    expected=last_result.expected_value,
                    reason=last_result.reason,
                )
                expected_state = await handler.dispatch(self._wrapper, payload) or expected_state

        await self._mark_failed(action, attempts, last_result)

    async def _poll_until_verified(
        self,
        *,
        action_id: int,
        handler,
        payload: dict,
        expected_state: dict[str, object],
        attempts: int,
    ) -> tuple[VerifyResult, int]:
        deadline = asyncio.get_running_loop().time() + VERIFY_TIMEOUT_S
        while True:
            device = await self._wrapper.refresh_device()
            result = handler.verify(device, payload, expected_state)
            attempts += 1
            await self._store_verification_progress(action_id, attempts, result)
            if result.ok:
                return result, attempts
            if asyncio.get_running_loop().time() >= deadline:
                return result, attempts
            await asyncio.sleep(VERIFY_POLL_INTERVAL_S)

    async def _store_verification_progress(
        self, action_id: int, attempts: int, result: VerifyResult
    ) -> None:
        async with get_session() as session:
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
        async with get_session() as session:
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
            add_result = session.add(audit_record)
            if asyncio.iscoroutine(add_result):
                await add_result
        logger.info(
            "action_verified",
            action_type=action.action_type,
            action_id=action.id,
            verify_attempts=attempts,
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
        async with get_session() as session:
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
            add_result = session.add(audit_record)
            if asyncio.iscoroutine(add_result):
                await add_result
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
        """Mark stale claimed or pending actions as expired with a diagnostic reason.

        Runs periodically to catch actions that the executor never picked up
        (e.g. scheduled during an override window, or from a superseded plan).
        Only expires actions older than 2 minutes to avoid racing with
        execute_due_actions.
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(minutes=2)

        async with get_session() as session:
            result = await session.execute(
                select(PlanActionRecord)
                .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
                .where(
                    and_(
                        PlanActionRecord.status.in_(("pending", "executing")),
                        PlanActionRecord.scheduled_ts <= cutoff,
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
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
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

"""Plan executor: reconciles planned actions against the heat pump."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import structlog
from sqlalchemy import select, and_, update

from packages.core.database import get_session
from packages.core.models import PlanActionRecord, PlanRecord, OverrideRecord, AuditLogRecord
from packages.core.services import AquareaWrapper

logger = structlog.get_logger()

# Delay before verifying action took effect (seconds)
VERIFICATION_DELAY = 30


class PlanExecutor:
    """Executes pending plan actions respecting overrides and rate limits."""

    def __init__(self, wrapper: AquareaWrapper):
        self._wrapper = wrapper

    async def execute_due_actions(self) -> None:
        """Find and execute all actions whose scheduled time has passed."""
        now = dt.datetime.now(dt.timezone.utc)

        async with get_session() as session:
            # Check for active overrides
            override_result = await session.execute(
                select(OverrideRecord).where(
                    and_(
                        OverrideRecord.active == True,
                        OverrideRecord.ts_from <= now,
                        OverrideRecord.ts_to >= now,
                    )
                )
            )
            active_overrides = override_result.scalars().all()

            # Get pending actions that are due
            result = await session.execute(
                select(PlanActionRecord)
                .where(
                    and_(
                        PlanActionRecord.status == "pending",
                        PlanActionRecord.scheduled_ts <= now,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(5)  # Max 5 actions per cycle to respect rate limits
            )
            actions = result.scalars().all()

            if active_overrides and actions:
                override_reason = active_overrides[0].reason or "manual override"
                logger.info(
                    "executor_overrides_active",
                    count=len(active_overrides),
                    reason=override_reason,
                    skipping=len(actions),
                )
                # Mark blocked actions as skipped with a clear reason
                for action in actions:
                    await session.execute(
                        update(PlanActionRecord)
                        .where(PlanActionRecord.id == action.id)
                        .values(
                            status="skipped",
                            executed_at=now,
                            result_json=json.dumps({
                                "reason": "override_active",
                                "override": override_reason,
                            }),
                        )
                    )
                return

            if active_overrides:
                logger.info(
                    "executor_overrides_active",
                    count=len(active_overrides),
                    reason=active_overrides[0].reason,
                )
                return  # Override active but no due actions

        for action in actions:
            await self._execute_action(action)

    async def _execute_action(self, action: PlanActionRecord) -> None:
        """Execute a single action, verify state change, and update status."""
        try:
            payload = json.loads(action.payload_json) if action.payload_json else {}

            match action.action_type:
                case "force_dhw_on":
                    from aioaquarea import ForceDHW
                    await self._wrapper.force_dhw(ForceDHW.ON)

                case "force_dhw_off":
                    from aioaquarea import ForceDHW
                    await self._wrapper.force_dhw(ForceDHW.OFF)

                case "quiet_mode_on":
                    from aioaquarea import QuietMode
                    await self._wrapper.set_quiet_mode(QuietMode.LEVEL1)

                case "quiet_mode_off":
                    from aioaquarea import QuietMode
                    await self._wrapper.set_quiet_mode(QuietMode.OFF)

                case "zone_temp_boost":
                    offset = payload.get("offset", 2)
                    device = await self._wrapper.get_device()
                    if hasattr(device.status, "zones") and device.status.zones:
                        zone = device.status.zones[0]
                        new_temp = (zone.heat_set or 20) + offset
                        await self._wrapper.set_zone_heat_temperature(0, new_temp)

                case "zone_temp_restore":
                    # Restore to comfort setpoint
                    from packages.core.config import settings
                    target = int(settings.comfort_temp_min + settings.comfort_temp_max) // 2
                    await self._wrapper.set_zone_heat_temperature(0, target)

                case "set_tank_temp":
                    temp = payload.get("temperature", 50)
                    await self._wrapper.set_tank_temperature(temp)

                case "eco_mode_on":
                    await self._wrapper.set_special_status("ECO")

                case "eco_mode_off":
                    await self._wrapper.clear_special_status()

                case "comfort_mode_on":
                    await self._wrapper.set_special_status("COMFORT")

                case _:
                    logger.warning("executor_unknown_action", action_type=action.action_type)
                    return

            # Mark as dispatched immediately — verify task may upgrade to
            # "executed" later, but we must not leave the action as "pending"
            # in case the async verification fails.
            now = dt.datetime.now(dt.timezone.utc)
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="executed_unverified",
                        executed_at=now,
                        result_json=json.dumps({"dispatched": True}),
                    )
                )

            # --- Action Verification (non-blocking) ---
            asyncio.create_task(self._deferred_verify(action))

            logger.info(
                "action_dispatched",
                action_type=action.action_type,
                action_id=action.id,
            )

        except Exception as e:
            logger.error(
                "action_failed", action_type=action.action_type, action_id=action.id, error=str(e)
            )
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="failed",
                        executed_at=dt.datetime.now(dt.timezone.utc),
                        result_json=json.dumps({"error": str(e)}),
                    )
                )

    async def _verify_action(self, action_type: str) -> bool:
        """
        Verify that an action took effect by polling device state.

        Waits VERIFICATION_DELAY seconds then checks if the device state
        matches what we expect after the action.
        """
        try:
            await asyncio.sleep(VERIFICATION_DELAY)
            device = await self._wrapper.refresh_device()

            match action_type:
                case "force_dhw_on":
                    return device.force_dhw.value == 1
                case "force_dhw_off":
                    return device.force_dhw.value == 0
                case "quiet_mode_on":
                    return device.quiet_mode.value >= 1
                case "quiet_mode_off":
                    return device.quiet_mode.value == 0
                case "eco_mode_on":
                    return device.special_status is not None and device.special_status.name == "ECO"
                case "comfort_mode_on":
                    return device.special_status is not None and device.special_status.name == "COMFORT"
                case "eco_mode_off":
                    return device.special_status is None
                case _:
                    # For actions we can't easily verify, assume success
                    return True

        except Exception as e:
            logger.warning("verification_failed", action_type=action_type, error=str(e))
            return False

    async def _deferred_verify(self, action: PlanActionRecord) -> None:
        """Background task: wait, verify, then update action status and audit log."""
        try:
            verified = await self._verify_action(action.action_type)
            status = "executed" if verified else "executed_unverified"
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status=status,
                        executed_at=dt.datetime.now(dt.timezone.utc),
                        result_json=json.dumps({"success": True, "verified": verified}),
                    )
                )
                session.add(
                    AuditLogRecord(
                        actor="optimizer",
                        action=action.action_type,
                        payload_json=action.payload_json,
                        result="success" if verified else "unverified",
                    )
                )
            logger.info(
                "action_verified",
                action_type=action.action_type,
                action_id=action.id,
                verified=verified,
            )
        except Exception as e:
            logger.error("deferred_verify_failed", action_id=action.id, error=str(e))
            # Status already set to executed_unverified by _execute_action,
            # so the action won't be stuck as "pending".

    async def expire_stale_actions(self) -> None:
        """Mark stale pending actions as expired with a diagnostic reason.

        Runs periodically to catch actions that the executor never picked up
        (e.g. scheduled during an override window, or from a superseded plan).
        Only expires actions older than 2 minutes to avoid racing with
        execute_due_actions.
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(minutes=2)

        async with get_session() as session:
            # Find stale pending actions
            result = await session.execute(
                select(PlanActionRecord)
                .where(
                    and_(
                        PlanActionRecord.status == "pending",
                        PlanActionRecord.scheduled_ts <= cutoff,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(20)
            )
            stale = result.scalars().all()

            if not stale:
                return

            # Find the latest plan id
            latest_plan_result = await session.execute(
                select(PlanRecord.id).order_by(PlanRecord.created_at.desc()).limit(1)
            )
            latest_plan_id = latest_plan_result.scalar_one_or_none()

            for action in stale:
                reason = await self._diagnose_missed(
                    session, action, latest_plan_id, now,
                )
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

        # 1. Superseded by a newer plan?
        if latest_plan_id and action.plan_id != latest_plan_id:
            return {
                "reason": "superseded",
                "gap_minutes": gap_minutes,
                "detail": f"Replaced by plan #{latest_plan_id}",
            }

        # 2. Was an override active in the execution window?
        #    The executor checks every 60s, so look for overrides covering
        #    a window from scheduled_ts to scheduled_ts + 2 min.
        window_end = scheduled + dt.timedelta(minutes=2)
        override_result = await session.execute(
            select(OverrideRecord).where(
                and_(
                    OverrideRecord.ts_from <= window_end,
                    OverrideRecord.ts_to >= scheduled,
                )
            ).limit(1)
        )
        blocking_override = override_result.scalar_one_or_none()
        if blocking_override:
            return {
                "reason": "override_active",
                "override": blocking_override.reason or "manual override",
                "gap_minutes": gap_minutes,
                "detail": f"Override '{blocking_override.reason}' was active at scheduled time",
            }

        # 3. Was a new plan generated around the action's scheduled time?
        #    Re-optimization can delay the executor in the same event loop.
        plan_window_start = scheduled - dt.timedelta(minutes=1)
        plan_window_end = scheduled + dt.timedelta(minutes=3)
        plan_result = await session.execute(
            select(PlanRecord.id, PlanRecord.created_at).where(
                and_(
                    PlanRecord.created_at >= plan_window_start,
                    PlanRecord.created_at <= plan_window_end,
                )
            ).order_by(PlanRecord.created_at.desc()).limit(1)
        )
        concurrent_plan = plan_result.one_or_none()
        if concurrent_plan:
            return {
                "reason": "optimization_overlap",
                "concurrent_plan_id": concurrent_plan[0],
                "gap_minutes": gap_minutes,
                "detail": (
                    f"Plan #{concurrent_plan[0]} was being generated at "
                    f"{concurrent_plan[1].strftime('%H:%M:%S')} "
                    f"— may have blocked the executor"
                ),
            }

        # 4. Large gap since scheduled time → executor likely wasn't running
        if gap_minutes > 10:
            return {
                "reason": "executor_gap",
                "gap_minutes": gap_minutes,
                "detail": f"Executor did not run for ~{round(gap_minutes)} min after scheduled time",
            }

        # 5. Fallback
        return {
            "reason": "timing",
            "gap_minutes": gap_minutes,
            "detail": (
                f"Action was due {gap_minutes} min ago but was never picked up "
                f"— possible event-loop delay or transient DB error"
            ),
        }

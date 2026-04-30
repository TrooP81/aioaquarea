"""Plan executor: reconciles planned actions against the heat pump."""

from __future__ import annotations

import datetime as dt
import json
import logging

import structlog
from sqlalchemy import select, and_, update

from packages.core.database import get_session
from packages.core.models import PlanActionRecord, OverrideRecord, AuditLogRecord
from packages.core.services import AquareaWrapper

logger = structlog.get_logger()


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

            if active_overrides:
                logger.info(
                    "executor_overrides_active",
                    count=len(active_overrides),
                    reason=active_overrides[0].reason,
                )
                return  # Manual override wins

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

        for action in actions:
            await self._execute_action(action)

    async def _execute_action(self, action: PlanActionRecord) -> None:
        """Execute a single action and update its status."""
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
                    await self._wrapper.set_quiet_mode(QuietMode.LEVEL_1)

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

                case _:
                    logger.warning("executor_unknown_action", action_type=action.action_type)
                    return

            # Mark as executed
            async with get_session() as session:
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .values(
                        status="executed",
                        executed_at=dt.datetime.now(dt.timezone.utc),
                        result_json=json.dumps({"success": True}),
                    )
                )
                # Audit log
                session.add(
                    AuditLogRecord(
                        actor="optimizer",
                        action=action.action_type,
                        payload_json=action.payload_json,
                        result="success",
                    )
                )

            logger.info("action_executed", action_type=action.action_type, action_id=action.id)

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

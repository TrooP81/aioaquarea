"""Shower mode: reactive DHW boost on rapid tank temperature drops."""

from __future__ import annotations

import datetime as dt
import json

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    PlanActionRecord,
    PlanRecord,
    ShowerEventRecord,
)
from packages.core.settings_service import get_setting
from packages.optimizer.data_access import get_prices
from packages.optimizer.actions import ActionType

logger = structlog.get_logger()


class ShowerDetector:
    """Detects shower events from rapid tank temperature drops and injects DHW boost actions."""

    async def check(self, record: DeviceStatusRecord) -> None:
        """Main entry point — called after each device status poll."""
        enabled = await get_setting("shower_mode_enabled")
        if enabled != "true":
            return

        async with get_session() as session:
            # Check for an already-active shower event
            active_event = await self._get_active_event(session)

            if active_event:
                await self._check_recovery(session, active_event, record)
            else:
                await self._check_for_drop(session, record)

    async def _get_active_event(
        self, session: AsyncSession
    ) -> ShowerEventRecord | None:
        result = await session.execute(
            select(ShowerEventRecord)
            .where(ShowerEventRecord.status == "active")
            .order_by(ShowerEventRecord.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_for_drop(
        self, session: AsyncSession, current: DeviceStatusRecord
    ) -> None:
        """Detect a rapid tank temp drop between this poll and the previous one."""
        if current.tank_temp is None:
            return

        # Get the previous device status record
        result = await session.execute(
            select(DeviceStatusRecord)
            .where(DeviceStatusRecord.ts < current.ts)
            .order_by(DeviceStatusRecord.ts.desc())
            .limit(1)
        )
        prev = result.scalar_one_or_none()
        if prev is None or prev.tank_temp is None:
            return

        drop = prev.tank_temp - current.tank_temp
        elapsed_minutes = (current.ts - prev.ts).total_seconds() / 60

        # Only trigger if polls are close together (≤10 min) to avoid stale data
        if elapsed_minutes > 10:
            return

        threshold = int(await get_setting("shower_drop_threshold"))
        if drop < threshold:
            return

        # Check if DHW is already being forced (something else is handling it)
        if current.force_dhw == 1:
            logger.info(
                "shower_drop_detected_but_dhw_active",
                drop=drop,
                elapsed_min=elapsed_minutes,
            )
            return

        # Peak price check — skip boost during top 5% most expensive hours
        if await self._is_peak_price(session, current.ts):
            event = ShowerEventRecord(
                started_at=current.ts,
                pre_shower_temp=prev.tank_temp,
                status="skipped_peak",
                peak_price_skipped=True,
            )
            session.add(event)
            logger.info(
                "shower_detected_but_peak_price",
                drop=drop,
                pre_temp=prev.tank_temp,
            )
            return

        # Create active shower event
        event = ShowerEventRecord(
            started_at=current.ts,
            pre_shower_temp=prev.tank_temp,
            status="active",
        )
        session.add(event)

        # Inject immediate force_dhw_on action
        now = dt.datetime.now(dt.timezone.utc)
        plan = PlanRecord(
            horizon_start=now,
            horizon_end=now + dt.timedelta(hours=1),
            plan_json="[]",
            optimizer_version="shower_reactive",
        )
        session.add(plan)
        await session.flush()  # Get plan.id

        action = PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=now,
            action_type=str(ActionType.FORCE_DHW_ON),
            payload_json=json.dumps(
                {
                    "trigger": "shower_mode",
                    "pre_shower_temp": prev.tank_temp,
                    "drop_detected": drop,
                    "reason": f"tank_drop_{drop:.1f}C_in_{elapsed_minutes:.0f}min",
                }
            ),
            status="pending",
        )
        session.add(action)

        # Audit log
        session.add(
            AuditLogRecord(
                actor="shower_detector",
                action="shower_mode_activated",
                payload_json=json.dumps(
                    {
                        "pre_shower_temp": prev.tank_temp,
                        "current_temp": current.tank_temp,
                        "drop": drop,
                    }
                ),
                result="force_dhw_on_scheduled",
            )
        )

        logger.info(
            "shower_mode_activated",
            drop=drop,
            pre_temp=prev.tank_temp,
            current_temp=current.tank_temp,
        )

    async def _check_recovery(
        self,
        session: AsyncSession,
        event: ShowerEventRecord,
        current: DeviceStatusRecord,
    ) -> None:
        """Check if the tank has recovered to pre-shower temperature, or timed out."""
        max_duration = int(await get_setting("shower_max_duration_minutes"))

        # Use record timestamp for elapsed calculation (testable, consistent)
        elapsed = (current.ts - event.started_at).total_seconds() / 60
        if elapsed >= max_duration:
            event.status = "timeout"
            event.recovered_at = current.ts
            await self._inject_dhw_off(session, "timeout")
            logger.warning(
                "shower_mode_timeout",
                elapsed_min=elapsed,
                max_min=max_duration,
            )
            return

        # Recovery check
        if current.tank_temp is not None and current.tank_temp >= event.pre_shower_temp:
            event.status = "recovered"
            event.recovered_at = current.ts
            await self._inject_dhw_off(session, "recovered")
            logger.info(
                "shower_mode_recovered",
                tank_temp=current.tank_temp,
                target=event.pre_shower_temp,
            )

    async def _inject_dhw_off(self, session: AsyncSession, reason: str) -> None:
        """Inject a force_dhw_off action to end the shower boost."""
        now = dt.datetime.now(dt.timezone.utc)
        plan = PlanRecord(
            horizon_start=now,
            horizon_end=now + dt.timedelta(hours=1),
            plan_json="[]",
            optimizer_version="shower_reactive",
        )
        session.add(plan)
        await session.flush()

        action = PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=now,
            action_type=str(ActionType.FORCE_DHW_OFF),
            payload_json=json.dumps(
                {"trigger": "shower_mode", "reason": reason}
            ),
            status="pending",
        )
        session.add(action)

    async def _is_peak_price(self, session: AsyncSession, ts: dt.datetime) -> bool:
        """Check if the current hour's price is in the top 5% of today's prices."""
        start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + dt.timedelta(days=1)

        prices = await get_prices(session, start_of_day, end_of_day)
        if not prices:
            return False  # Fail open - allow boost if no price data

        price_values = sorted(p for _, p in prices)

        # If all prices are equal (flat tariff), never consider it peak
        if len(set(price_values)) <= 1:
            return False

        # 95th percentile threshold (top 5%)
        idx = min(len(price_values) - 1, int(len(price_values) * 0.95))
        p95 = price_values[idx]

        # Find current hour's price
        current_hour_start = ts.replace(minute=0, second=0, microsecond=0)
        current_price = None
        for price_ts, price in prices:
            if price_ts == current_hour_start:
                current_price = price
                break

        if current_price is None:
            return False  # No price for this hour - fail open

        return current_price >= p95

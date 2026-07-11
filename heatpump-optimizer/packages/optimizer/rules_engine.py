"""Core rules optimizer orchestration and data access helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, IndoorTempReading, ShowerEventRecord
from packages.core.settings_service import (
    get_effective_schedule,
    get_float_setting,
    get_int_setting,
    get_user_tz,
)
from packages.ml.comfort_model import comfort_model
from packages.ml.thermal import thermal_model

from .rule_mixins import DHWRulesMixin, GuardrailRulesMixin, ModeRulesMixin, PreheatRulesMixin


class RulesOptimizer(DHWRulesMixin, PreheatRulesMixin, GuardrailRulesMixin, ModeRulesMixin):
    """
    Simple rule-based optimizer that:
    1. Shifts DHW heating to cheapest hours
    2. Pre-heats zones before cold spells during cheap hours
    3. Reduces power during expensive peak hours
    4. Schedules quiet mode at night
    5. Toggles eco/comfort mode based on price + occupancy
    6. Detects holiday mode and suspends actions
    """

    VERSION = "rules_v3"

    async def generate_plan(self) -> dict[str, Any] | None:
        now = dt.datetime.now(dt.timezone.utc)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)
        horizon_end = horizon_start + dt.timedelta(hours=24)

        async with get_session() as session:
            prices = await self._get_prices(session, horizon_start, horizon_end)
            weather = await self._get_weather(session, horizon_start, horizon_end)
            last_status = await self._get_last_status(session)

        if not prices:
            return None

        if last_status and getattr(last_status, "holiday_mode", None) == 1:
            return {
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "actions": [],
                "version": self.VERSION,
                "cost_estimate": 0.0,
                "note": "holiday_mode_active_optimization_suspended",
            }

        if (
            thermal_model.params.last_calibrated is None
            or (now - thermal_model.params.last_calibrated).total_seconds() > 6 * 3600
        ):
            await thermal_model.calibrate()

        current_tank_temp = (
            last_status.tank_temp if last_status and last_status.tank_temp is not None else 48.0
        )
        current_outdoor_temp = (
            last_status.outdoor_temp
            if last_status and last_status.outdoor_temp is not None
            else 7.0
        )
        current_water_temp = (
            last_status.zone1_temp if last_status and last_status.zone1_temp is not None else 35.0
        )
        tank_target = (
            last_status.tank_target_temp if last_status and last_status.tank_target_temp else 52
        )

        latest_indoor_temp: float | None = None
        async with get_session() as session:
            row = (
                await session.execute(
                    select(IndoorTempReading.temperature)
                    .order_by(IndoorTempReading.timestamp.desc())
                    .limit(1)
                )
            ).scalar()
            if row is not None:
                latest_indoor_temp = float(row)

        if comfort_model.is_trained:
            predicted_indoor = comfort_model.predict_indoor_temp(
                zone_water_temp=current_water_temp,
                outdoor_temp=current_outdoor_temp,
                hour=now.hour,
                indoor_temp=latest_indoor_temp,
            )
            current_indoor_temp = (
                predicted_indoor if predicted_indoor is not None else (latest_indoor_temp or 20.0)
            )
        else:
            current_indoor_temp = latest_indoor_temp or 20.0

        actions: list[dict[str, Any]] = []

        learned_threshold = await get_float_setting("learned_schedule_threshold")
        comfort_schedule = await get_effective_schedule(learned_threshold=learned_threshold)
        tz_name = await get_user_tz()

        async with get_session() as session:
            shower_row = await session.execute(
                select(ShowerEventRecord).where(ShowerEventRecord.status == "active").limit(1)
            )
            shower_active = shower_row.scalar_one_or_none() is not None

        actions.extend(
            self._plan_dhw(
                prices,
                weather,
                horizon_start,
                current_tank_temp,
                tank_target,
                current_outdoor_temp,
                comfort_schedule,
                suppress_dhw_off=shower_active,
                tz_name=tz_name,
            )
        )
        actions.extend(
            self._plan_preheat(
                prices,
                weather,
                horizon_start,
                current_indoor_temp,
                current_outdoor_temp,
                current_water_temp,
            )
        )
        actions.extend(self._plan_peak_avoidance(prices, weather, horizon_start))

        quiet_start = await get_int_setting("quiet_mode_start")
        quiet_end = await get_int_setting("quiet_mode_end")
        actions.extend(self._plan_quiet_mode(horizon_start, quiet_start, quiet_end, tz_name=tz_name))

        comfort_override_pct = await get_int_setting("price_comfort_override_pct")
        eco_upgrade_pct = await get_int_setting("price_eco_upgrade_pct")
        actions.extend(
            self._plan_eco_comfort(
                prices,
                weather,
                horizon_start,
                comfort_schedule,
                comfort_override_pct,
                eco_upgrade_pct,
                tz_name=tz_name,
            )
        )

        comfort_temp_target = await get_float_setting("comfort_temp_target")
        comfort_temp_min = await get_float_setting("comfort_temp_min")
        actions.extend(
            self._plan_indoor_guardrails(
                prices,
                weather,
                horizon_start,
                current_indoor_temp,
                current_outdoor_temp,
                current_water_temp,
                comfort_schedule,
                comfort_temp_target,
                comfort_temp_min,
                tz_name=tz_name,
            )
        )

        if not actions:
            return None

        actions.sort(key=lambda a: a["ts"])
        cost_estimate = await self._estimate_cost(actions, prices)

        return {
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
            "actions": actions,
            "version": self.VERSION,
            "cost_estimate": cost_estimate,
        }

    async def _estimate_cost(self, actions: list[dict], prices: list[tuple[dt.datetime, float]]) -> float:
        if not prices:
            return 0.0
        avg_price = sum(p for _, p in prices) / len(prices)

        estimated_kwh_per_day = 15.0
        try:
            from sqlalchemy import func as sa_func

            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
            heat = sa_func.coalesce(ConsumptionRecord.heat_kwh, 0)
            cool = sa_func.coalesce(ConsumptionRecord.cool_kwh, 0)
            tank = sa_func.coalesce(ConsumptionRecord.tank_kwh, 0)
            async with get_session() as session:
                result = await session.execute(
                    select(sa_func.avg(heat + cool + tank)).where(ConsumptionRecord.ts >= since)
                )
                avg_kwh = result.scalar()
                if avg_kwh and avg_kwh > 0:
                    estimated_kwh_per_day = float(avg_kwh)
        except Exception:
            pass

        return avg_price * estimated_kwh_per_day

    async def _get_prices(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        from packages.optimizer.data_access import get_prices

        return await get_prices(session, start, end)

    async def _get_weather(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        from packages.optimizer.data_access import get_weather

        return await get_weather(session, start, end)

    async def _get_last_status(self, session: AsyncSession):
        from packages.optimizer.data_access import get_last_status

        return await get_last_status(session)

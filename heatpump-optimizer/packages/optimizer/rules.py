"""Rules-based optimizer: DHW shifting, pre-heating, peak avoidance."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import PriceRecord, WeatherRecord, DeviceStatusRecord


class RulesOptimizer:
    """
    Simple rule-based optimizer that:
    1. Shifts DHW heating to cheapest hours
    2. Pre-heats zones before cold spells during cheap hours
    3. Reduces power during expensive peak hours
    """

    VERSION = "rules_v1"

    async def generate_plan(self) -> dict[str, Any] | None:
        """Generate a 24h plan based on current prices and weather forecast."""
        now = dt.datetime.now(dt.timezone.utc)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)
        horizon_end = horizon_start + dt.timedelta(hours=24)

        async with get_session() as session:
            prices = await self._get_prices(session, horizon_start, horizon_end)
            weather = await self._get_weather(session, horizon_start, horizon_end)
            last_status = await self._get_last_status(session)

        if not prices:
            return None

        actions = []

        # --- Rule 1: DHW shifting ---
        dhw_actions = self._plan_dhw(prices, horizon_start)
        actions.extend(dhw_actions)

        # --- Rule 2: Pre-heat during cheap hours before cold ---
        preheat_actions = self._plan_preheat(prices, weather, horizon_start)
        actions.extend(preheat_actions)

        # --- Rule 3: Peak avoidance ---
        peak_actions = self._plan_peak_avoidance(prices, weather, horizon_start)
        actions.extend(peak_actions)

        if not actions:
            return None

        # Sort by timestamp
        actions.sort(key=lambda a: a["ts"])

        # Estimate cost
        cost_estimate = self._estimate_cost(actions, prices)

        return {
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
            "actions": actions,
            "version": self.VERSION,
            "cost_estimate": cost_estimate,
        }

    def _plan_dhw(
        self, prices: list[tuple[dt.datetime, float]], horizon_start: dt.datetime
    ) -> list[dict]:
        """Schedule DHW heating during the N cheapest hours before each deadline."""
        actions = []
        dhw_hours_needed = 2  # Typically 2h of DHW heating fills the tank

        for ready_hour in settings.dhw_ready_hours:
            # Find the cheapest hours in the window before the deadline
            deadline = horizon_start.replace(hour=ready_hour, minute=0)
            if deadline <= horizon_start:
                deadline += dt.timedelta(days=1)

            window_start = deadline - dt.timedelta(hours=8)
            window_prices = [
                (ts, p) for ts, p in prices if window_start <= ts < deadline
            ]

            if not window_prices:
                continue

            # Sort by price, pick cheapest N hours
            window_prices.sort(key=lambda x: x[1])
            cheap_hours = window_prices[:dhw_hours_needed]

            for ts, price in cheap_hours:
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": "force_dhw_on",
                        "payload": {"reason": f"cheap_hour_before_{ready_hour}:00"},
                    }
                )
                # Turn off after 1 hour
                actions.append(
                    {
                        "ts": (ts + dt.timedelta(hours=1)).isoformat(),
                        "type": "force_dhw_off",
                        "payload": {"reason": "dhw_slot_end"},
                    }
                )

        return actions

    def _plan_preheat(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
    ) -> list[dict]:
        """Pre-heat zones during cheap hours before forecast cold spells."""
        actions = []

        if not weather:
            return actions

        # Find cold spells (below 0°C) in next 24h
        cold_hours = [(ts, t) for ts, t in weather if t is not None and t < 2.0]

        if not cold_hours:
            return actions

        # For each cold block, find cheap hours in the 4h before
        first_cold = cold_hours[0][0]
        preheat_window_start = first_cold - dt.timedelta(hours=4)
        preheat_window_end = first_cold

        window_prices = [
            (ts, p) for ts, p in prices if preheat_window_start <= ts < preheat_window_end
        ]

        if not window_prices:
            return actions

        # Pick cheapest 2 hours for pre-heating
        window_prices.sort(key=lambda x: x[1])
        for ts, price in window_prices[:2]:
            actions.append(
                {
                    "ts": ts.isoformat(),
                    "type": "zone_temp_boost",
                    "payload": {
                        "offset": +2,
                        "reason": "preheat_before_cold",
                    },
                }
            )
            actions.append(
                {
                    "ts": (ts + dt.timedelta(hours=1)).isoformat(),
                    "type": "zone_temp_restore",
                    "payload": {"reason": "preheat_slot_end"},
                }
            )

        return actions

    def _plan_peak_avoidance(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
    ) -> list[dict]:
        """Activate quiet mode during the most expensive hours if outdoor temp permits."""
        actions = []

        if not prices:
            return actions

        # Find the top 5% most expensive hours
        sorted_prices = sorted(prices, key=lambda x: x[1], reverse=True)
        n_expensive = max(1, len(sorted_prices) // 20)  # top 5%
        expensive_hours = sorted_prices[:n_expensive]

        # Only avoid peak if outdoor temp is above freezing (building has thermal mass)
        weather_dict = {ts: t for ts, t in weather} if weather else {}

        for ts, price in expensive_hours:
            outdoor_t = weather_dict.get(ts)
            if outdoor_t is not None and outdoor_t < 0:
                continue  # Don't throttle when it's freezing

            actions.append(
                {
                    "ts": ts.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"reason": f"peak_price_{price:.4f}_eur_kwh"},
                }
            )
            actions.append(
                {
                    "ts": (ts + dt.timedelta(hours=1)).isoformat(),
                    "type": "quiet_mode_off",
                    "payload": {"reason": "peak_avoidance_end"},
                }
            )

        return actions

    def _estimate_cost(
        self, actions: list[dict], prices: list[tuple[dt.datetime, float]]
    ) -> float:
        """Rough cost estimate (simplified)."""
        # Average price * estimated kWh (very rough)
        if not prices:
            return 0.0
        avg_price = sum(p for _, p in prices) / len(prices)
        estimated_kwh_per_day = 15.0  # Typical for a mid-size heat pump
        return avg_price * estimated_kwh_per_day

    async def _get_prices(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        """Fetch prices from DB."""
        result = await session.execute(
            select(PriceRecord.ts, PriceRecord.price_eur_per_kwh)
            .where(and_(PriceRecord.ts >= start, PriceRecord.ts < end))
            .order_by(PriceRecord.ts)
        )
        return [(row.ts, row.price_eur_per_kwh) for row in result.all()]

    async def _get_weather(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        """Fetch weather forecast from DB."""
        result = await session.execute(
            select(WeatherRecord.ts, WeatherRecord.temperature)
            .where(and_(WeatherRecord.ts >= start, WeatherRecord.ts < end))
            .order_by(WeatherRecord.ts)
        )
        return [(row.ts, row.temperature) for row in result.all()]

    async def _get_last_status(self, session: AsyncSession):
        """Get latest device status."""
        result = await session.execute(
            select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
        )
        return result.scalar_one_or_none()

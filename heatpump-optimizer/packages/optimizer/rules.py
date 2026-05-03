"""Rules-based optimizer: DHW shifting, pre-heating, peak avoidance."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import PriceRecord, WeatherRecord, DeviceStatusRecord, IndoorTempReading, ShowerEventRecord
from packages.core.settings_service import get_effective_schedule, get_setting, is_comfort_hour, dhw_deadlines_from_schedule, get_comfort_schedule
from packages.ml.thermal import thermal_model
from packages.ml.comfort_model import comfort_model


class RulesOptimizer:
    """
    Simple rule-based optimizer that:
    1. Shifts DHW heating to cheapest hours (using learned tank heating time)
    2. Pre-heats zones before cold spells during cheap hours
    3. Reduces power during expensive peak hours
    4. Schedules quiet mode at night
    5. Toggles eco/comfort mode based on price + occupancy
    6. Detects holiday mode and suspends actions
    """

    VERSION = "rules_v3"

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

        # If device is in holiday mode, suspend optimization
        if last_status and getattr(last_status, 'holiday_mode', None) == 1:
            return {
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "actions": [],
                "version": self.VERSION,
                "cost_estimate": 0.0,
                "note": "holiday_mode_active_optimization_suspended",
            }

        # Calibrate thermal model if stale (>6h)
        if (
            thermal_model.params.last_calibrated is None
            or (now - thermal_model.params.last_calibrated).total_seconds() > 6 * 3600
        ):
            await thermal_model.calibrate()

        # Extract current state for thermal predictions
        current_tank_temp = last_status.tank_temp if last_status and last_status.tank_temp else 48.0
        current_outdoor_temp = last_status.outdoor_temp if last_status and last_status.outdoor_temp else 7.0
        current_water_temp = last_status.zone1_temp if last_status and last_status.zone1_temp else 35.0
        tank_target = last_status.tank_target_temp if last_status and last_status.tank_target_temp else 52

        # Estimate current indoor air temp via comfort model (SmartThings-trained)
        # Falls back to a simple heuristic when model is not trained
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
            current_indoor_temp = predicted_indoor if predicted_indoor is not None else (latest_indoor_temp or 20.0)
        else:
            current_indoor_temp = latest_indoor_temp or 20.0  # prefer real reading over default

        actions = []

        # Fetch comfort schedule early — used by DHW shifting and eco/comfort rules
        learned_threshold = float(await get_setting("learned_schedule_threshold") or 0.3)
        comfort_schedule = await get_effective_schedule(learned_threshold=learned_threshold)

        # Check for active shower event (skip force_dhw_off to avoid conflict)
        shower_active = False
        async with get_session() as session:
            shower_row = await session.execute(
                select(ShowerEventRecord).where(ShowerEventRecord.status == "active").limit(1)
            )
            shower_active = shower_row.scalar_one_or_none() is not None

        # --- Rule 1: DHW shifting (thermal-model-aware) ---
        dhw_actions = self._plan_dhw(
            prices, weather, horizon_start,
            current_tank_temp, tank_target, current_outdoor_temp,
            comfort_schedule,
            suppress_dhw_off=shower_active,
        )
        actions.extend(dhw_actions)

        # --- Rule 2: Pre-heat during cheap hours before cold ---
        preheat_actions = self._plan_preheat(
            prices, weather, horizon_start,
            current_indoor_temp, current_outdoor_temp, current_water_temp,
        )
        actions.extend(preheat_actions)

        # --- Rule 3: Peak avoidance ---
        peak_actions = self._plan_peak_avoidance(prices, weather, horizon_start)
        actions.extend(peak_actions)

        # --- Rule 4: Quiet mode scheduling (night hours) ---
        quiet_start = int(await get_setting("quiet_mode_start") or 22)
        quiet_end = int(await get_setting("quiet_mode_end") or 6)
        quiet_actions = self._plan_quiet_mode(horizon_start, quiet_start, quiet_end)
        actions.extend(quiet_actions)

        # --- Rule 5: Eco/Comfort mode based on schedule + price ---
        comfort_override_pct = int(await get_setting("price_comfort_override_pct") or 90)
        eco_upgrade_pct = int(await get_setting("price_eco_upgrade_pct") or 25)
        eco_actions = self._plan_eco_comfort(
            prices, horizon_start, comfort_schedule,
            comfort_override_pct, eco_upgrade_pct,
        )
        actions.extend(eco_actions)

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
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        current_tank_temp: float,
        tank_target: int,
        current_outdoor_temp: float,
        comfort_schedule: dict[str, list[int]],
        suppress_dhw_off: bool = False,
    ) -> list[dict]:
        """
        Schedule DHW heating using thermal model predictions.

        Deadlines are derived from the comfort schedule: the tank must be
        at temperature by the start of each contiguous comfort block.

        When suppress_dhw_off is True (active shower event), force_dhw_off
        actions are omitted to let shower mode control DHW termination.
        """
        actions = []

        # Predict heating time needed
        prediction = thermal_model.predict_tank_heating_time(
            current_temp=current_tank_temp,
            target_temp=float(tank_target),
            outdoor_temp=current_outdoor_temp,
        )
        # Round up to whole hours (actions are hourly)
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))

        # Also predict standby loss: how fast does the tank cool?
        cooling_pred = thermal_model.predict_tank_cooling_time(
            current_temp=float(tank_target),
            min_temp=current_tank_temp,
            outdoor_temp=current_outdoor_temp,
        )

        # Derive deadlines from the comfort schedule
        ready_hours = dhw_deadlines_from_schedule(comfort_schedule, horizon_start)

        for ready_hour in ready_hours:
            deadline = horizon_start.replace(hour=ready_hour, minute=0)
            if deadline <= horizon_start:
                deadline += dt.timedelta(days=1)

            # Use thermal model to determine optimal start window
            # The window should account for: heating time + buffer
            latest_start = thermal_model.optimal_start_time(
                current_temp=current_tank_temp,
                target_temp=float(tank_target),
                deadline=deadline,
                outdoor_temp=self._get_outdoor_at(weather, deadline, current_outdoor_temp),
                is_tank=True,
            )

            window_start = max(latest_start - dt.timedelta(hours=4), horizon_start)
            window_prices = [
                (ts, p) for ts, p in prices if window_start <= ts < deadline
            ]

            if not window_prices:
                continue

            # Find cheapest contiguous block of `hours_needed` hours
            best_slot = self._find_cheapest_slot(window_prices, hours_needed)

            if best_slot:
                slot_start = best_slot[0][0]
                actions.append(
                    {
                        "ts": slot_start.isoformat(),
                        "type": "force_dhw_on",
                        "payload": {
                            "reason": f"thermal_optimized_before_{ready_hour}:00",
                            "predicted_minutes": round(prediction.estimated_minutes),
                            "heating_rate": round(prediction.heating_rate_per_hour, 2),
                            "confidence": prediction.confidence,
                        },
                    }
                )
                # Turn off after predicted heating duration (rounded to hours)
                off_time = slot_start + dt.timedelta(hours=hours_needed)
                if not suppress_dhw_off:
                    actions.append(
                        {
                            "ts": off_time.isoformat(),
                            "type": "force_dhw_off",
                            "payload": {"reason": "dhw_target_reached"},
                        }
                    )

        return actions

    def _plan_preheat(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        current_indoor_temp: float,
        current_outdoor_temp: float,
        current_water_temp: float,
    ) -> list[dict]:
        """
        Pre-heat zones using thermal model to determine exact timing.

        Uses the comfort model (when trained) to translate a target indoor
        air temperature into the required water supply temperature.  Falls
        back to a simple +2 °C water temp boost otherwise.
        """
        actions = []

        if not weather:
            return actions

        # Find cold spells (below 2°C) in next 24h
        cold_hours = [(ts, t) for ts, t in weather if t is not None and t < 2.0]

        if not cold_hours:
            return actions

        first_cold = cold_hours[0][0]

        # Determine the target: raise indoor temp by 2 °C before the cold spell
        target_indoor = current_indoor_temp + 2.0

        # Compute required water supply temperature
        if comfort_model.is_trained:
            outdoor_at_cold = cold_hours[0][1] if cold_hours[0][1] is not None else 0.0
            required_water = comfort_model.required_zone_temp(
                target_indoor=target_indoor,
                outdoor_temp=outdoor_at_cold,
                hour=first_cold.hour,
                indoor_temp=current_indoor_temp,
            )
            if required_water is None:
                required_water = current_water_temp + 2.0
            target_zone_boost = required_water
        else:
            # Fallback: boost water temp by 2 °C
            target_zone_boost = current_water_temp + 2.0

        # Use thermal model to find optimal start time for zone boost
        outdoor_at_cold = cold_hours[0][1] if cold_hours[0][1] is not None else 0.0
        prediction = thermal_model.predict_zone_heating_time(
            current_temp=current_water_temp,
            target_temp=target_zone_boost,
            outdoor_temp=outdoor_at_cold,
        )

        hours_needed = max(1, int(prediction.estimated_hours + 0.9))

        # Search window: from now until cold spell
        preheat_window_start = max(
            first_cold - dt.timedelta(hours=hours_needed + 4),
            horizon_start,
        )
        preheat_window_end = first_cold

        window_prices = [
            (ts, p)
            for ts, p in prices
            if preheat_window_start <= ts < preheat_window_end
        ]

        if not window_prices:
            return actions

        # Pick cheapest slot for pre-heating
        best_slot = self._find_cheapest_slot(window_prices, hours_needed)

        if best_slot:
            slot_start = best_slot[0][0]
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": "zone_temp_boost",
                    "payload": {
                        "offset": +2,
                        "reason": "thermal_preheat_before_cold",
                        "predicted_minutes": round(prediction.estimated_minutes),
                        "heating_rate": round(prediction.heating_rate_per_hour, 2),
                    },
                }
            )
            actions.append(
                {
                    "ts": (slot_start + dt.timedelta(hours=hours_needed)).isoformat(),
                    "type": "zone_temp_restore",
                    "payload": {"reason": "preheat_complete"},
                }
            )

        return actions

    @staticmethod
    def _find_cheapest_slot(
        prices: list[tuple[dt.datetime, float]], hours_needed: int
    ) -> list[tuple[dt.datetime, float]] | None:
        """Find the cheapest contiguous slot of N hours within a price list."""
        if len(prices) < hours_needed:
            return prices if prices else None

        # Sort by time to find contiguous blocks
        sorted_prices = sorted(prices, key=lambda x: x[0])

        best_cost = float("inf")
        best_slot = None

        for i in range(len(sorted_prices) - hours_needed + 1):
            slot = sorted_prices[i : i + hours_needed]
            # Check contiguity (each hour follows previous)
            contiguous = all(
                (slot[j + 1][0] - slot[j][0]).total_seconds() == 3600
                for j in range(len(slot) - 1)
            )
            if not contiguous:
                continue

            cost = sum(p for _, p in slot)
            if cost < best_cost:
                best_cost = cost
                best_slot = slot

        # Fallback: if no contiguous slot found, just pick cheapest individual hours
        if best_slot is None:
            sorted_by_price = sorted(prices, key=lambda x: x[1])
            best_slot = sorted_by_price[:hours_needed]

        return best_slot

    @staticmethod
    def _get_outdoor_at(
        weather: list[tuple[dt.datetime, float]],
        target_time: dt.datetime,
        default: float,
    ) -> float:
        """Get forecast outdoor temp closest to a target time."""
        if not weather:
            return default
        closest = min(weather, key=lambda w: abs((w[0] - target_time).total_seconds()))
        return closest[1] if closest[1] is not None else default

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

        # When prices are flat (e.g. manual provider), there are no peaks to avoid
        unique_prices = set(p for _, p in prices)
        if len(unique_prices) <= 1:
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

    def _plan_quiet_mode(
        self, horizon_start: dt.datetime,
        start_hour: int = 22, end_hour: int = 6,
    ) -> list[dict]:
        """
        Schedule quiet mode during night hours.

        Quiet mode reduces compressor noise by limiting speed.
        No significant efficiency loss but improves neighbor relations.
        """
        actions = []

        for offset_hours in range(24):
            ts = horizon_start + dt.timedelta(hours=offset_hours)
            hour = ts.hour

            if hour == start_hour:
                actions.append({
                    "ts": ts.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"reason": "night_quiet_schedule", "level": 2},
                })
            elif hour == end_hour:
                actions.append({
                    "ts": ts.isoformat(),
                    "type": "quiet_mode_off",
                    "payload": {"reason": "night_quiet_end"},
                })

        return actions

    def _plan_eco_comfort(
        self,
        prices: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        comfort_schedule: dict[str, list[int]],
        comfort_override_pct: int = 90,
        eco_upgrade_pct: int = 25,
    ) -> list[dict]:
        """
        Toggle eco/comfort mode based on the user's comfort schedule
        with price as a secondary modifier.

        Strategy:
        - Hours marked as comfort in schedule → COMFORT mode
          (unless price is above comfort_override_pct — then stay normal)
        - Hours NOT in comfort schedule → ECO mode
          (unless price is below eco_upgrade_pct — then upgrade to normal)
        - Transitions only emitted on mode change to avoid command spam.
        """
        actions = []

        if not prices:
            return actions

        price_values = sorted(p for _, p in prices)
        p_eco = price_values[max(0, len(price_values) * eco_upgrade_pct // 100 - 1)]
        p_comfort = price_values[min(len(price_values) - 1, len(price_values) * comfort_override_pct // 100)]

        # When prices are flat (e.g. manual provider), price-based overrides
        # are meaningless — just follow the comfort schedule directly.
        flat_price = len(set(price_values)) <= 1

        current_mode = None  # Track mode to avoid redundant switches

        for ts, price in prices:
            scheduled_comfort = is_comfort_hour(comfort_schedule, ts)

            if scheduled_comfort:
                # Comfort hour — but override to normal if price is extreme
                # (skip override when prices are flat)
                if not flat_price and price >= p_comfort:
                    target_mode = "normal"
                    reason = f"comfort_hour_but_peak_price_{price:.4f}"
                else:
                    target_mode = "comfort"
                    reason = "comfort_schedule"
            else:
                # Non-comfort hour — eco unless price is very cheap
                # (skip upgrade when prices are flat)
                if not flat_price and price <= p_eco:
                    target_mode = "normal"
                    reason = f"eco_hour_but_cheap_price_{price:.4f}"
                else:
                    target_mode = "eco"
                    reason = "outside_comfort_schedule"

            if target_mode == current_mode:
                continue

            if target_mode == "comfort":
                actions.append({
                    "ts": ts.isoformat(),
                    "type": "comfort_mode_on",
                    "payload": {"reason": reason},
                })
            elif target_mode == "eco":
                actions.append({
                    "ts": ts.isoformat(),
                    "type": "eco_mode_on",
                    "payload": {"reason": reason},
                })
            else:  # normal
                actions.append({
                    "ts": ts.isoformat(),
                    "type": "eco_mode_off",
                    "payload": {"reason": reason},
                })

            current_mode = target_mode

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
        """Fetch prices from DB for the active provider's area."""
        from packages.optimizer.data_access import get_prices
        return await get_prices(session, start, end)

    async def _get_weather(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        """Fetch weather forecast from DB."""
        from packages.optimizer.data_access import get_weather
        return await get_weather(session, start, end)

    async def _get_last_status(self, session: AsyncSession):
        """Get latest device status."""
        from packages.optimizer.data_access import get_last_status
        return await get_last_status(session)

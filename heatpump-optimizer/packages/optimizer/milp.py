"""MILP-based cost optimizer — uses PuLP/CBC with learned thermal and ML models."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import numpy as np
from sqlalchemy import select, and_

try:
    import pulp
except ImportError:
    pulp = None

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import PriceRecord, WeatherRecord, DeviceStatusRecord, IndoorTempReading
from packages.core.settings_service import get_setting, dhw_deadlines_from_schedule, get_comfort_schedule
from packages.ml.thermal import thermal_model
from packages.ml.comfort_model import comfort_model
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError

import structlog

logger = structlog.get_logger()


class MILPOptimizer:
    """
    Mixed-Integer Linear Programming optimizer.

    Minimizes electricity cost over a 24-48h horizon subject to:
    - Tank temperature comfort constraints (tank >= min by deadline hours)
    - Zone temperature bounds
    - Heat pump COP as a function of outdoor temp (linearized)
    - Max number of mode changes per day (API rate limit)

    Requires `pulp` package.
    """

    VERSION = "milp_v1"
    SOLVER_TIMEOUT_SECONDS = 30

    def __init__(self, cop_model=None, demand_model=None):
        """
        Args:
            cop_model: object with .is_trained and .predict_cop(outdoor, tank_target, hour)
            demand_model: object with .is_trained and .predict_hourly(weather, hours)
        """
        if pulp is None:
            raise ImportError("PuLP is required for MILP optimizer: pip install pulp")
        self._cop_model = cop_model
        self._demand_model = demand_model

    async def generate_plan(self) -> dict[str, Any] | None:
        """Fetch data from DB and solve the MILP, returning a standard plan dict."""
        now = dt.datetime.now(dt.timezone.utc)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)
        horizon_end = horizon_start + dt.timedelta(hours=24)

        async with get_session() as session:
            prices = await self._get_prices(session, horizon_start, horizon_end)
            weather = await self._get_weather(session, horizon_start, horizon_end)
            last_status = await self._get_last_status(session)

            # Latest indoor temp from SmartThings (if available)
            latest_indoor_temp: float | None = (
                await session.execute(
                    select(IndoorTempReading.temperature)
                    .order_by(IndoorTempReading.timestamp.desc())
                    .limit(1)
                )
            ).scalar()

        if not prices:
            raise DataIncompleteError("No price data available for the planning horizon")

        if not weather:
            raise DataIncompleteError("No weather data available for the planning horizon")

        # Calibrate thermal model if stale
        if (
            thermal_model.params.last_calibrated is None
            or (now - thermal_model.params.last_calibrated).total_seconds() > 6 * 3600
        ):
            await thermal_model.calibrate()

        current_tank_temp = (
            last_status.tank_temp if last_status and last_status.tank_temp else 48.0
        )

        # Build COP function: prefer ML model, fall back to default curve
        cop_fn = self._build_cop_function(last_status)

        # Build demand estimates: prefer ML model, fall back to constant
        demand_per_hour = self._build_demand_estimates(weather)

        # Get comfort schedule to derive DHW deadlines
        comfort_schedule = await get_comfort_schedule()
        dhw_deadlines = dhw_deadlines_from_schedule(comfort_schedule, horizon_start)

        # Solve synchronously in a thread to avoid blocking the event loop
        plan = await asyncio.to_thread(
            self._solve,
            prices,
            weather,
            cop_fn,
            demand_per_hour,
            current_tank_temp,
            latest_indoor_temp,
            dhw_deadlines,
        )
        return plan

    def _build_cop_function(self, last_status):
        """Return a callable(outdoor_temp, hour) -> COP."""
        tank_target = (
            last_status.tank_target_temp
            if last_status and last_status.tank_target_temp
            else 50
        )
        if self._cop_model and self._cop_model.is_trained:
            logger.info("milp_using_ml_cop_model")

            def _ml_cop(outdoor_temp: float, hour: int = 12) -> float:
                return self._cop_model.predict_cop(outdoor_temp, tank_target, hour)

            return _ml_cop

        logger.info("milp_using_default_cop_curve")
        return lambda outdoor_temp, hour=12: self._default_cop_curve(outdoor_temp)

    def _build_demand_estimates(self, weather) -> list[float]:
        """Return hourly demand estimates (kW) for the horizon."""
        if self._demand_model and self._demand_model.is_trained:
            logger.info("milp_using_ml_demand_model")
            weather_dicts = [
                {"temperature": t, "wind_speed": 3.0, "irradiance": 0.0}
                for _, t in weather
            ]
            return self._demand_model.predict_hourly(weather_dicts, len(weather))

        # Fallback: constant estimate
        return [3.0] * len(weather)

    @staticmethod
    def _default_cop_curve(outdoor_temp: float) -> float:
        """Simple linear COP approximation for air-to-water heat pump."""
        cop = 3.5 + 0.1 * outdoor_temp
        return max(1.5, min(6.0, cop))

    def _solve(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        cop_fn,
        demand_per_hour: list[float],
        current_tank_temp: float,
        current_indoor_temp: float | None = None,
        dhw_deadlines: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        Solve the optimization problem (runs in a thread).
        Raises InfeasibleError or SolverTimeoutError on failure.
        """
        H = len(prices)

        price_vals = [p for _, p in prices]
        temps = []
        for i in range(H):
            if i < len(weather):
                temps.append(weather[i][1] if weather[i][1] is not None else 5.0)
            else:
                temps.append(5.0)

        hours = [(prices[h][0]).hour for h in range(H)]
        cops = [cop_fn(temps[h], hours[h]) for h in range(H)]

        # --- Use thermal model parameters if calibrated ---
        if thermal_model.params.last_calibrated:
            # Convert thermal model rates (°C/h) to thermal kWh using tank capacity
            # Typical 200L tank ≈ 0.23 kWh/°C (water specific heat × mass)
            kwh_per_degree = 0.23
            tank_loss_kwh_per_h = abs(thermal_model.params.tank_standby_loss) * kwh_per_degree
            # DHW power derived from heating rate and COP
            avg_cop = sum(cops) / len(cops)
            dhw_thermal_kw = thermal_model.params.tank_heating_rate * kwh_per_degree
            dhw_power_kw = dhw_thermal_kw / avg_cop if avg_cop > 0 else 2.0
        else:
            dhw_power_kw = 2.0
            tank_loss_kwh_per_h = 0.3

        sh_max_power_kw = 3.0  # Max electrical input for space heating

        # Tank state (thermal kWh stored)
        tank_min = settings.tank_min_temp * 0.15
        tank_max = settings.tank_max_temp * 0.15
        # Use actual current tank temp to initialize
        tank_init = max(tank_min, min(tank_max, current_tank_temp * 0.15))

        # --- Problem setup ---
        prob = pulp.LpProblem("HeatPumpCostMin", pulp.LpMinimize)

        x_dhw = [pulp.LpVariable(f"x_dhw_{h}", cat="Binary") for h in range(H)]
        x_sh = [pulp.LpVariable(f"x_sh_{h}", 0, 1, cat="Continuous") for h in range(H)]

        # --- Objective: minimize cost ---
        prob += pulp.lpSum(
            [
                price_vals[h] * (x_dhw[h] * dhw_power_kw + x_sh[h] * sh_max_power_kw)
                for h in range(H)
            ]
        )

        # --- Constraints ---

        # Tank state evolution
        tank_state = [
            pulp.LpVariable(f"tank_{h}", tank_min, tank_max) for h in range(H + 1)
        ]
        prob += tank_state[0] == tank_init

        for h in range(H):
            heat_added = x_dhw[h] * dhw_power_kw * cops[h]
            prob += tank_state[h + 1] == tank_state[h] + heat_added - tank_loss_kwh_per_h

        # Tank must be above minimum at comfort-schedule deadline hours
        for ready_hour in (dhw_deadlines or []):
            if ready_hour < H:
                prob += tank_state[ready_hour] >= tank_min * 1.2

        # Limit DHW activations (API rate limit proxy)
        max_changes = 20
        prob += pulp.lpSum(x_dhw) <= max_changes

        # Space heating: ensure minimum comfort when freezing
        # When comfort model is trained, derive a minimum SH fraction from
        # the required water supply temperature to maintain the target indoor temp.
        comfort_target = float(settings.comfort_temp_min) if hasattr(settings, 'comfort_temp_min') else 20.0
        use_comfort = comfort_model.is_trained

        for h in range(H):
            if use_comfort:
                # Use actual indoor temp for hour 0, None for future hours
                indoor_now = current_indoor_temp if h == 0 else None
                required_water = comfort_model.required_zone_temp(
                    target_indoor=comfort_target,
                    outdoor_temp=temps[h],
                    hour=hours[h],
                    indoor_temp=indoor_now,
                )
                if required_water is not None and required_water > 25:
                    # Higher required water temp → higher SH fraction needed
                    # Scale linearly: water=25 → 0.0, water=55 → 1.0
                    min_sh = max(0.0, min(1.0, (required_water - 25.0) / 30.0))
                    prob += x_sh[h] >= min_sh
                elif temps[h] < 0:
                    prob += x_sh[h] >= 0.5
            else:
                if temps[h] < 0:
                    prob += x_sh[h] >= 0.5

        # --- Solve ---
        solver = pulp.PULP_CBC_CMD(
            msg=0, timeLimit=self.SOLVER_TIMEOUT_SECONDS
        )
        prob.solve(solver)

        if prob.status == pulp.constants.LpStatusNotSolved:
            raise SolverTimeoutError(
                f"CBC solver did not converge within {self.SOLVER_TIMEOUT_SECONDS}s"
            )

        if prob.status != pulp.constants.LpStatusOptimal:
            raise InfeasibleError(
                f"MILP infeasible or unbounded (status={prob.status})"
            )

        # --- Extract plan ---
        actions = []
        start_ts = prices[0][0]

        for h in range(H):
            ts = start_ts + dt.timedelta(hours=h)

            if x_dhw[h].varValue and x_dhw[h].varValue > 0.5:
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": "force_dhw_on",
                        "payload": {
                            "reason": "milp_optimal",
                            "price": price_vals[h],
                            "cop": cops[h],
                        },
                    }
                )
                actions.append(
                    {
                        "ts": (ts + dt.timedelta(minutes=55)).isoformat(),
                        "type": "force_dhw_off",
                        "payload": {"reason": "milp_slot_end"},
                    }
                )

            sh_val = x_sh[h].varValue or 0
            if sh_val < 0.3 and temps[h] > 5:
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": "quiet_mode_on",
                        "payload": {
                            "reason": "milp_low_demand",
                            "sh_fraction": sh_val,
                        },
                    }
                )

        total_cost = pulp.value(prob.objective)

        version = self.VERSION
        if self._cop_model and self._cop_model.is_trained:
            version += "+ml"

        return {
            "horizon_start": start_ts,
            "horizon_end": start_ts + dt.timedelta(hours=H),
            "actions": actions,
            "version": version,
            "cost_estimate": total_cost,
        }

    # --- Data fetching (same pattern as RulesOptimizer) ---

    async def _get_prices(
        self, session, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        provider = await get_setting("price_provider")
        if provider == "entsoe":
            area = (await get_setting("entsoe_area")) or "10YNL----------L"
        elif provider == "manual":
            area = "manual"
        else:
            area = "tibber"

        result = await session.execute(
            select(PriceRecord.ts, PriceRecord.price_eur_per_kwh)
            .where(
                and_(
                    PriceRecord.ts >= start,
                    PriceRecord.ts < end,
                    PriceRecord.area == area,
                )
            )
            .order_by(PriceRecord.ts)
        )
        return [(row.ts, row.price_eur_per_kwh) for row in result.all()]

    async def _get_weather(
        self, session, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        result = await session.execute(
            select(WeatherRecord.ts, WeatherRecord.temperature)
            .where(and_(WeatherRecord.ts >= start, WeatherRecord.ts < end))
            .order_by(WeatherRecord.ts)
        )
        return [(row.ts, row.temperature) for row in result.all()]

    async def _get_last_status(self, session):
        result = await session.execute(
            select(DeviceStatusRecord)
            .order_by(DeviceStatusRecord.ts.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

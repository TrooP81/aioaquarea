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
from packages.core.settings_service import get_setting, get_user_tz, dhw_deadlines_from_schedule, get_comfort_schedule, is_comfort_hour
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
        tz_name = await get_user_tz()
        dhw_deadlines = dhw_deadlines_from_schedule(comfort_schedule, horizon_start, tz_name=tz_name)

        # Resolve per-hour indoor comfort targets from schedule
        comfort_temp_target = float(await get_setting("comfort_temp_target") or 20.5)
        comfort_temp_min = float(
            await get_setting("comfort_temp_min") or getattr(settings, "comfort_temp_min", 18.0)
        )
        indoor_targets = []
        for h in range(len(prices)):
            hour_ts = horizon_start + dt.timedelta(hours=h)
            if is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
                indoor_targets.append(comfort_temp_target)
            else:
                indoor_targets.append(comfort_temp_min)

        # Tank temperature bounds (DB-first, env fallback)
        tank_min_temp = int(await get_setting("tank_min_temp") or settings.tank_min_temp)
        tank_min_temp_offpeak = int(
            await get_setting("tank_min_temp_offpeak") or settings.tank_min_temp_offpeak
        )
        tank_max_temp = int(await get_setting("tank_max_temp") or settings.tank_max_temp)

        # Per-hour tank floor: lower minimum during off-peak (sleep/away) hours
        tank_min_per_hour = []
        for h in range(len(prices)):
            hour_ts = horizon_start + dt.timedelta(hours=h)
            if is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
                tank_min_per_hour.append(tank_min_temp)
            else:
                tank_min_per_hour.append(tank_min_temp_offpeak)

        # Current heat curve baseline water temp (what the pump targets in NORMAL mode)
        heat_curve_water_temp = (
            last_status.zone1_target_temp if last_status and last_status.zone1_target_temp else 35.0
        )

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
            indoor_targets,
            heat_curve_water_temp,
            tank_min_per_hour,
            tank_max_temp,
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
        indoor_targets: list[float] | None = None,
        heat_curve_water_temp: float = 35.0,
        tank_min_per_hour: list[int] | None = None,
        tank_max_temp_setting: int | None = None,
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

        # Consistent conversion factor: 200L tank ≈ 0.23 kWh per °C
        # (water specific heat × mass: 200 kg × 4186 J/(kg·°C) / 3.6e6)
        kwh_per_degree = 0.23
        avg_cop = sum(cops) / len(cops) if cops else 3.5

        # --- Use thermal model's outdoor-adjusted rates per hour ---
        # The thermal model fits: rate = base + factor * outdoor_temp
        # Using the raw base (intercept at outdoor=0) can vastly understate
        # actual rates.  Call the model's methods with the per-hour outdoor
        # temperature so every hour uses a physically accurate rate.
        if thermal_model.params.last_calibrated:
            tank_heat_rates = [
                thermal_model._tank_heating_rate(temps[h]) for h in range(H)
            ]
            tank_loss_rates = [
                thermal_model._tank_loss_rate(temps[h]) for h in range(H)
            ]
        else:
            tank_heat_rates = [5.0] * H   # default 5 °C/h
            tank_loss_rates = [-0.5] * H  # default -0.5 °C/h

        # Convert per-hour °C rates to kWh quantities
        dhw_thermal_kw_per_h = [r * kwh_per_degree for r in tank_heat_rates]
        dhw_power_kw_per_h = [
            (dhw_thermal_kw_per_h[h] / cops[h]) if cops[h] > 0 else 0.27
            for h in range(H)
        ]
        tank_loss_kwh_per_h = [abs(r) * kwh_per_degree for r in tank_loss_rates]

        sh_max_power_kw = 3.0  # Max electrical input for space heating

        # Tank state (thermal kWh stored, using same kwh_per_degree factor)
        # Per-hour tank floor: lower bound during off-peak hours
        _tank_max = (tank_max_temp_setting or settings.tank_max_temp) * kwh_per_degree
        if tank_min_per_hour:
            tank_min_floors = [t * kwh_per_degree for t in tank_min_per_hour]
            tank_min_abs = min(tank_min_floors)  # lowest possible floor (for LP bounds)
            tank_min_comfort = max(tank_min_floors)  # comfort-hour floor (for deadlines)
        else:
            tank_min_abs = settings.tank_min_temp * kwh_per_degree
            tank_min_comfort = tank_min_abs
            tank_min_floors = [tank_min_abs] * H
        tank_init = max(tank_min_abs, min(_tank_max, current_tank_temp * kwh_per_degree))

        # Cap DHW thermal gain so it can't exceed the tank range in one hour.
        # Fast-heating tanks (e.g. 20°C/h) reach target in minutes; the solver
        # needs the ability to run DHW for only a fraction of the hour.
        tank_range = _tank_max - tank_min_abs
        max_thermal_per_h = [dhw_power_kw_per_h[h] * cops[h] for h in range(H)]
        # If any hour's thermal gain exceeds the tank range, use continuous DHW
        needs_continuous_dhw = any(g > tank_range * 1.1 for g in max_thermal_per_h)

        # Representative values for logging
        avg_dhw_power = sum(dhw_power_kw_per_h) / H
        avg_tank_loss = sum(tank_loss_kwh_per_h) / H

        logger.info(
            "milp_tank_parameters",
            tank_min_abs_kwh=round(tank_min_abs, 2),
            tank_min_comfort_kwh=round(tank_min_comfort, 2),
            tank_max_kwh=round(_tank_max, 2),
            tank_range_kwh=round(tank_range, 2),
            tank_init_kwh=round(tank_init, 2),
            avg_dhw_power_kw=round(avg_dhw_power, 3),
            avg_tank_loss_kwh_per_h=round(avg_tank_loss, 3),
            avg_tank_heat_rate=round(sum(tank_heat_rates) / H, 2),
            max_thermal_gain=round(max(max_thermal_per_h), 2),
            continuous_dhw=needs_continuous_dhw,
        )

        # --- Problem setup ---
        prob = pulp.LpProblem("HeatPumpCostMin", pulp.LpMinimize)

        # DHW decision: continuous [0,1] fraction of the hour when the thermal
        # gain per hour would exceed the tank range (fast-heating tanks),
        # binary otherwise (traditional slower tanks).
        if needs_continuous_dhw:
            x_dhw = [pulp.LpVariable(f"x_dhw_{h}", 0, 1, cat="Continuous") for h in range(H)]
        else:
            x_dhw = [pulp.LpVariable(f"x_dhw_{h}", cat="Binary") for h in range(H)]
        x_sh = [pulp.LpVariable(f"x_sh_{h}", 0, 1, cat="Continuous") for h in range(H)]

        # --- Objective: minimize cost ---
        prob += pulp.lpSum(
            [
                price_vals[h] * (x_dhw[h] * dhw_power_kw_per_h[h] + x_sh[h] * sh_max_power_kw)
                for h in range(H)
            ]
        )

        # --- Constraints ---

        # Tank state evolution — LP variable bounds use the absolute (offpeak)
        # minimum so the solver has full range; per-hour floors are added as
        # explicit constraints below.
        tank_state = [
            pulp.LpVariable(f"tank_{h}", tank_min_abs, _tank_max) for h in range(H + 1)
        ]
        prob += tank_state[0] == tank_init

        for h in range(H):
            heat_added = x_dhw[h] * dhw_power_kw_per_h[h] * cops[h]
            prob += tank_state[h + 1] == tank_state[h] + heat_added - tank_loss_kwh_per_h[h]

        # Per-hour tank floor: comfort hours use normal min, off-peak uses lower min
        for h in range(H + 1):
            floor = tank_min_floors[min(h, H - 1)]
            prob += tank_state[h] >= floor

        # Tank must be above comfort minimum at comfort-schedule deadline hours
        for ready_hour in (dhw_deadlines or []):
            if ready_hour < H:
                prob += tank_state[ready_hour] >= tank_min_comfort * 1.2

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

        # --- Indoor temperature state variable ---
        # Track predicted indoor air temperature through the horizon.
        # Uses learned indoor heating/cooling rates from the thermal model.
        indoor_init = current_indoor_temp if current_indoor_temp is not None else 20.0
        t_indoor = [
            pulp.LpVariable(f"T_indoor_{h}", 10.0, 35.0) for h in range(H + 1)
        ]
        prob += t_indoor[0] == indoor_init

        for h in range(H):
            gain = thermal_model._indoor_heating_rate(temps[h])
            loss = thermal_model._indoor_cooling_rate(temps[h])
            # Linear evolution: T[h+1] = T[h] + loss + x_sh[h] * (gain - loss)
            # When x_sh=0 → pure cooling; when x_sh=1 → full heating
            prob += t_indoor[h + 1] == t_indoor[h] + loss + x_sh[h] * (gain - loss)

        # Comfort floor: indoor temp must stay at/above the per-hour target
        if indoor_targets:
            for h in range(H):
                if h < len(indoor_targets):
                    prob += t_indoor[h] >= indoor_targets[h]

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

            dhw_frac = x_dhw[h].varValue or 0
            if dhw_frac > 0.05:
                # Duration proportional to fraction (minimum 5 minutes)
                dhw_minutes = max(5, int(round(dhw_frac * 60)))
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": "force_dhw_on",
                        "payload": {
                            "reason": "milp_optimal",
                            "price": price_vals[h],
                            "cop": cops[h],
                            "dhw_fraction": round(dhw_frac, 2),
                            "dhw_minutes": dhw_minutes,
                        },
                    }
                )
                actions.append(
                    {
                        "ts": (ts + dt.timedelta(minutes=dhw_minutes)).isoformat(),
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

        # --- Eco/Normal/Comfort mode selection based on solved indoor curve ---
        # Use the comfort model (if trained) to find the required water supply
        # temperature for the MILP's target indoor temp each hour, then compare
        # to the heat curve baseline to pick the best mode offset:
        #   ECO     = curve − 5 °C
        #   NORMAL  = curve ± 0 °C
        #   COMFORT = curve + 5 °C
        current_mode = None
        if comfort_model.is_trained:
            for h in range(H):
                ts = start_ts + dt.timedelta(hours=h)
                target_indoor = indoor_targets[h] if indoor_targets and h < len(indoor_targets) else 20.0
                indoor_now = current_indoor_temp if h == 0 else None

                required_water = comfort_model.required_zone_temp(
                    target_indoor=target_indoor,
                    outdoor_temp=temps[h],
                    hour=hours[h],
                    indoor_temp=indoor_now,
                )
                if required_water is None:
                    continue

                # How much offset from the heat curve baseline do we need?
                offset_needed = required_water - heat_curve_water_temp

                # Pick the mode whose offset is closest to what's needed
                # ECO = −5, NORMAL = 0, COMFORT = +5
                eco_dist = abs(offset_needed - (-5.0))
                normal_dist = abs(offset_needed - 0.0)
                comfort_dist = abs(offset_needed - 5.0)

                best = min(eco_dist, normal_dist, comfort_dist)
                if best == eco_dist:
                    target_mode = "eco"
                elif best == comfort_dist:
                    target_mode = "comfort"
                else:
                    target_mode = "normal"

                if target_mode == current_mode:
                    continue

                if target_mode == "eco":
                    action_type = "eco_mode_on"
                elif target_mode == "comfort":
                    action_type = "comfort_mode_on"
                else:
                    action_type = "normal_mode_on"

                actions.append({
                    "ts": ts.isoformat(),
                    "type": action_type,
                    "payload": {
                        "reason": f"milp_water_offset_{offset_needed:+.1f}C",
                        "required_water_temp": round(required_water, 1),
                        "heat_curve_baseline": round(heat_curve_water_temp, 1),
                        "outdoor_temp": round(temps[h], 1),
                        "target_indoor": target_indoor,
                    },
                })
                current_mode = target_mode

        total_cost = pulp.value(prob.objective)

        # Extract indoor temperature forecast from LP solution
        indoor_forecast = []
        for h in range(H):
            t_val = t_indoor[h].varValue
            target_val = indoor_targets[h] if indoor_targets and h < len(indoor_targets) else None
            indoor_forecast.append({
                "hour": h,
                "predicted_indoor_temp": round(t_val, 1) if t_val is not None else None,
                "target": target_val,
            })

        version = self.VERSION
        if self._cop_model and self._cop_model.is_trained:
            version += "+ml"

        return {
            "horizon_start": start_ts,
            "horizon_end": start_ts + dt.timedelta(hours=H),
            "actions": actions,
            "version": version,
            "cost_estimate": total_cost,
            "indoor_forecast": indoor_forecast,
        }

    # --- Data fetching (delegates to shared data_access module) ---

    async def _get_prices(
        self, session, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        from packages.optimizer.data_access import get_prices
        return await get_prices(session, start, end)

    async def _get_weather(
        self, session, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, float]]:
        from packages.optimizer.data_access import get_weather
        return await get_weather(session, start, end)

    async def _get_last_status(self, session):
        from packages.optimizer.data_access import get_last_status
        return await get_last_status(session)

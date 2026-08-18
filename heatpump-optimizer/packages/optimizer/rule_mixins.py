"""Strategy mixins for the rules optimizer."""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from packages.core.heat_curve import HeatCurveConfig
from packages.core.settings_service import dhw_deadlines_from_schedule, is_comfort_hour
from packages.ml.cop_model_core import COPModel
from packages.ml.comfort_model import comfort_model
from packages.ml.models import cop_model as trained_cop_model
from packages.ml.thermal import thermal_model
from packages.optimizer.actions import ActionType


class SharedRuleHelpersMixin:
    COMFORT_SATISFIED_MARGIN_C = 0.3

    @staticmethod
    def _zone_boost_targets(
        current_zone_target_temp: float | None,
        current_zone_heat_min: int | None,
        current_zone_heat_max: int | None,
        offset: int = 2,
    ) -> tuple[int, int] | None:
        """Freeze a positive target pair inside Panasonic's observed range."""
        values = (current_zone_target_temp, current_zone_heat_min, current_zone_heat_max)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            return None

        baseline = int(current_zone_target_temp)
        minimum = int(current_zone_heat_min)
        maximum = int(current_zone_heat_max)
        if (
            current_zone_target_temp != baseline
            or current_zone_heat_min != minimum
            or current_zone_heat_max != maximum
            or minimum > maximum
            or not minimum <= baseline <= maximum
        ):
            return None
        if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
            return None
        boost = min(baseline + offset, maximum)
        if boost <= baseline:
            return None
        return baseline, boost

    @staticmethod
    def _find_cheapest_slot(
        prices: list[tuple[dt.datetime, float]], hours_needed: int
    ) -> list[tuple[dt.datetime, float]] | None:
        if len(prices) < hours_needed:
            return prices if prices else None

        sorted_prices = sorted(prices, key=lambda x: x[0])
        best_cost = float("inf")
        best_slot = None

        for i in range(len(sorted_prices) - hours_needed + 1):
            slot = sorted_prices[i : i + hours_needed]
            contiguous = all(
                (slot[j + 1][0] - slot[j][0]).total_seconds() == 3600 for j in range(len(slot) - 1)
            )
            if not contiguous:
                continue

            cost = sum(p for _, p in slot)
            if cost < best_cost:
                best_cost = cost
                best_slot = slot

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
        if not weather:
            return default
        closest = min(weather, key=lambda w: abs((w[0] - target_time).total_seconds()))
        return closest[1] if closest[1] is not None else default

    def _find_lowest_heat_energy_cost_slot(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        hours_needed: int,
        fallback_outdoor_temp: float,
        tank_target: int | None = None,
    ) -> list[tuple[dt.datetime, float]] | None:
        """Choose heat-pump hours by electricity cost per delivered thermal kWh.

        When the ML COP model is trained and a tank target is known, it is
        preferred over the default linear curve. The ML model captures COP
        response to tank target and time-of-day, which the default curve
        cannot, so DHW slot picks reflect the real cost per kWh rather than
        an outdoor-only proxy.
        """
        use_ml = tank_target is not None and trained_cop_model.is_trained

        effective_prices = []
        original_prices = {ts: price for ts, price in prices}
        for ts, price in prices:
            outdoor_temp = self._get_outdoor_at(weather, ts, fallback_outdoor_temp)
            if use_ml:
                cop = trained_cop_model.predict_cop(outdoor_temp, int(tank_target), ts.hour)
            else:
                cop = COPModel._default_cop_curve(outdoor_temp)
            effective_prices.append((ts, price / cop))

        selected = self._find_cheapest_slot(effective_prices, hours_needed)
        if not selected:
            return None
        return [(ts, original_prices[ts]) for ts, _ in selected]

    @staticmethod
    def _weather_conditions_at(
        timestamp: dt.datetime,
        weather_full: list[dict] | None,
        outdoor_temp: float,
    ) -> dict:
        """Use the same weather dimensions for rule decisions and charts."""
        closest: dict | None = None
        if weather_full:
            dated = [row for row in weather_full if isinstance(row.get("ts"), dt.datetime)]
            if dated:
                closest = min(dated, key=lambda row: abs((row["ts"] - timestamp).total_seconds()))

        def value(name: str, default: float, *, non_negative: bool = False) -> float:
            raw = closest.get(name) if closest else None
            try:
                number = float(raw) if raw is not None else default
            except (TypeError, ValueError):
                number = default
            return max(0.0, number) if non_negative else number

        return {
            "outdoor_temp": value("temperature", outdoor_temp),
            "wind_speed": value("wind_speed", 3.0, non_negative=True),
            "irradiance": value("irradiance", 0.0, non_negative=True),
            "precipitation": value("precipitation", 0.0, non_negative=True),
            "hour": timestamp.hour,
        }

    # Wind and rain increase envelope heat loss beyond what an outdoor-only
    # rate predicts. Scale hours_needed so the preheat still reaches target
    # before the cold hour instead of finishing short. Values chosen from
    # ASHRAE-style infiltration multipliers, bounded so a stormy day cannot
    # double the runtime and hog the horizon.
    _WIND_LOSS_SCALE = 0.04  # per (m/s above 3 m/s)
    _RAIN_LOSS_SCALE = 0.10  # per (mm/h above 0)
    _WEATHER_LOSS_MAX_FACTOR = 1.35

    def _weather_heat_loss_factor(
        self,
        timestamp: dt.datetime,
        weather_full: list[dict] | None,
    ) -> float:
        """Return a >=1.0 multiplier for expected heat loss at ``timestamp``."""
        if not weather_full:
            return 1.0
        conditions = self._weather_conditions_at(timestamp, weather_full, 0.0)
        wind_over = max(0.0, conditions["wind_speed"] - 3.0)
        rain = max(0.0, conditions["precipitation"])
        factor = 1.0 + wind_over * self._WIND_LOSS_SCALE + rain * self._RAIN_LOSS_SCALE
        return max(1.0, min(self._WEATHER_LOSS_MAX_FACTOR, factor))

    def _passive_indoor_forecast(
        self,
        timestamps: list[dt.datetime],
        weather: list[tuple[dt.datetime, float]],
        current_indoor_temp: float,
        current_outdoor_temp: float,
        current_water_temp: float,
        heat_curve: HeatCurveConfig | None = None,
        weather_full: list[dict] | None = None,
    ) -> dict[dt.datetime, float]:
        """Predict indoor temperature with no planned space heating.

        Mode selection and pre-heating are both control decisions.  They must
        therefore use the same passive trajectory as the plan forecast instead
        of treating a comfort-schedule label or a cold outdoor reading as a
        sufficient reason to request heat.
        """
        if not timestamps:
            return {}

        weather_forecast = []
        zone_water_temps = []
        for ts in timestamps:
            outdoor_temp = self._get_outdoor_at(weather, ts, current_outdoor_temp)
            weather_forecast.append(self._weather_conditions_at(ts, weather_full, outdoor_temp))
            zone_water_temps.append(
                heat_curve.planned_supply_temperature(outdoor_temp)
                if heat_curve is not None
                else current_water_temp
            )

        curve = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor_temp,
            zone_water_temps=zone_water_temps,
            heating_fractions=[0.0] * len(weather_forecast),
            weather_forecast=weather_forecast,
            hours=len(weather_forecast),
        )
        return {ts: float(row["predicted_indoor_temp"]) for ts, row in zip(timestamps, curve)}


class DHWRulesMixin(SharedRuleHelpersMixin):
    # A forced DHW cycle has API and compressor cost. Very short calculated
    # top-ups are better left to the heat pump's own thermostat than turned
    # into a one-hour force-on/force-off pair.
    MIN_FORCE_DHW_MINUTES = 10
    # Opportunistic tank banking during exceptionally cheap slots. Requires a
    # material tank headroom so the arbitrage does not just wall-clock trip
    # the thermostat, and a price meaningfully below the horizon median so a
    # gently varying day cannot force an extra compressor cycle.
    OPPORTUNISTIC_HEADROOM_C = 3
    OPPORTUNISTIC_PRICE_FRACTION = 0.6

    def _find_lowest_dhw_energy_cost_slot(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        hours_needed: int,
        fallback_outdoor_temp: float,
        tank_target: int | None = None,
    ) -> list[tuple[dt.datetime, float]] | None:
        """Choose a DHW slot by electricity cost per unit of delivered heat.

        A heat pump produces more heat per kWh of electricity when outdoor air
        is warmer. Comparing raw spot prices alone can therefore select a cold
        hour that costs more and consumes more electricity for the same tank
        recharge. ``price / COP`` is the effective price per thermal kWh.
        When available, the trained COP model provides a tank-target-aware
        estimate rather than the default outdoor-only curve.
        """
        return self._find_lowest_heat_energy_cost_slot(
            prices,
            weather,
            hours_needed,
            fallback_outdoor_temp,
            tank_target=tank_target,
        )

    @staticmethod
    def _local_dhw_deadlines_in_horizon(
        comfort_schedule: dict[str, list[int]],
        horizon_start: dt.datetime,
        tz_name: str | None,
    ) -> list[tuple[dt.datetime, int]]:
        """Return future DHW deadlines as UTC instants with their local hour.

        The comfort schedule stores local wall-clock hours.  Building a
        deadline with ``horizon_start.replace(hour=...)`` accidentally treats
        those local hours as UTC, shifting an Amsterdam 08:00 deadline to
        10:00 in summer.  Enumerating local calendar days also means a plan
        crossing midnight uses tomorrow's weekday/weekend schedule correctly.
        """
        timezone = ZoneInfo(tz_name or "Europe/Amsterdam")
        if horizon_start.tzinfo is None:
            horizon_start = horizon_start.replace(tzinfo=dt.timezone.utc)
        horizon_end = horizon_start + dt.timedelta(hours=24)
        local_start = horizon_start.astimezone(timezone)
        local_end = horizon_end.astimezone(timezone)
        current_date = local_start.date()
        deadlines: list[tuple[dt.datetime, int]] = []

        while current_date <= local_end.date():
            local_noon = dt.datetime.combine(current_date, dt.time(12), tzinfo=timezone)
            for ready_hour in dhw_deadlines_from_schedule(
                comfort_schedule, local_noon, tz_name=tz_name
            ):
                local_deadline = dt.datetime.combine(
                    current_date, dt.time(ready_hour), tzinfo=timezone
                )
                deadline = local_deadline.astimezone(dt.timezone.utc)
                if horizon_start < deadline < horizon_end:
                    deadlines.append((deadline, ready_hour))
            current_date += dt.timedelta(days=1)

        return sorted(deadlines, key=lambda item: item[0])

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
        tz_name: str | None = None,
    ) -> list[dict]:
        actions = []
        prediction = thermal_model.predict_tank_heating_time(
            current_temp=current_tank_temp,
            target_temp=float(tank_target),
            outdoor_temp=current_outdoor_temp,
        )
        if prediction.estimated_minutes <= self.MIN_FORCE_DHW_MINUTES:
            return actions
        base_hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        for deadline, ready_hour in self._local_dhw_deadlines_in_horizon(
            comfort_schedule, horizon_start, tz_name
        ):
            latest_start = thermal_model.optimal_start_time(
                current_temp=current_tank_temp,
                target_temp=float(tank_target),
                deadline=deadline,
                outdoor_temp=self._get_outdoor_at(weather, deadline, current_outdoor_temp),
                is_tank=True,
            )
            # Widen the search window enough to capture overnight off-peak
            # troughs. The old 4h look-back missed the cheapest hours of the
            # night when the deadline was in the morning shortly after
            # off-peak ends.
            window_start = max(latest_start - dt.timedelta(hours=8), horizon_start)
            window_prices = [(ts, p) for ts, p in prices if window_start <= ts < deadline]
            if not window_prices:
                continue

            best_slot = self._find_lowest_dhw_energy_cost_slot(
                window_prices,
                weather,
                base_hours_needed,
                current_outdoor_temp,
                tank_target=tank_target,
            )
            if not best_slot:
                continue

            slot_start = best_slot[0][0]
            # Standby loss between now and slot start means the tank will be
            # cooler at slot start than it is now, so more heating time is
            # needed. Size the top-up against the projected temperature.
            delay_hours = max(
                0.0,
                (slot_start - horizon_start).total_seconds() / 3600.0,
            )
            projected_tank_temp = current_tank_temp
            if delay_hours > 0.0:
                loss_outdoor = self._get_outdoor_at(weather, slot_start, current_outdoor_temp)
                loss_rate_per_h = thermal_model._tank_loss_rate(loss_outdoor)
                projected_tank_temp = max(0.0, current_tank_temp + loss_rate_per_h * delay_hours)
            slot_outdoor = self._get_outdoor_at(weather, slot_start, current_outdoor_temp)
            projected_prediction = thermal_model.predict_tank_heating_time(
                current_temp=projected_tank_temp,
                target_temp=float(tank_target),
                outdoor_temp=slot_outdoor,
            )
            hours_needed = max(
                base_hours_needed,
                int(projected_prediction.estimated_hours + 0.9),
            )
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.FORCE_DHW_ON),
                    "payload": {
                        "reason": f"thermal_optimized_before_{ready_hour}:00",
                        "predicted_minutes": round(projected_prediction.estimated_minutes),
                        "heating_rate": round(projected_prediction.heating_rate_per_hour, 2),
                        "confidence": projected_prediction.confidence,
                    },
                }
            )
            off_time = slot_start + dt.timedelta(hours=hours_needed)
            if not suppress_dhw_off:
                actions.append(
                    {
                        "ts": off_time.isoformat(),
                        "type": str(ActionType.FORCE_DHW_OFF),
                        "payload": {"reason": "dhw_target_reached"},
                    }
                )

        # Opportunistic banking is only useful when no deadline cycle already
        # heats to the same target. Adding another forced cycle cannot store
        # energy beyond that target and only creates compressor/API churn.
        opportunistic = self._plan_dhw_opportunistic_top_up(
            prices,
            weather,
            horizon_start,
            current_tank_temp,
            tank_target,
            current_outdoor_temp,
            actions,
            suppress_dhw_off=suppress_dhw_off,
        )
        actions.extend(opportunistic)

        return actions

    def _plan_dhw_opportunistic_top_up(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        current_tank_temp: float,
        tank_target: int,
        current_outdoor_temp: float,
        existing_actions: list[dict],
        suppress_dhw_off: bool,
    ) -> list[dict]:
        """Schedule a bonus DHW cycle in an ultra-cheap slot, when it pays off.

        Skipped when the tank is already near target (banking has no room),
        when the horizon prices are near-flat (no meaningful arbitrage), or
        when a deadline top-up is already scheduled to reach the same target.
        """
        if not prices:
            return []
        if tank_target - current_tank_temp < self.OPPORTUNISTIC_HEADROOM_C:
            return []

        if any(action.get("type") == str(ActionType.FORCE_DHW_ON) for action in existing_actions):
            return []

        price_values = sorted(p for _, p in prices)
        if len(set(price_values)) <= 1:
            return []
        median_price = price_values[len(price_values) // 2]
        threshold = median_price * self.OPPORTUNISTIC_PRICE_FRACTION
        candidates = [(ts, p) for ts, p in prices if p < threshold]
        if not candidates:
            return []

        slot_start, slot_price = min(candidates, key=lambda item: item[1])
        delay_hours = max(0.0, (slot_start - horizon_start).total_seconds() / 3600.0)
        projected_tank_temp = current_tank_temp
        if delay_hours > 0.0:
            loss_outdoor = self._get_outdoor_at(weather, slot_start, current_outdoor_temp)
            loss_rate_per_h = thermal_model._tank_loss_rate(loss_outdoor)
            projected_tank_temp = max(0.0, current_tank_temp + loss_rate_per_h * delay_hours)
        slot_outdoor = self._get_outdoor_at(weather, slot_start, current_outdoor_temp)
        prediction = thermal_model.predict_tank_heating_time(
            current_temp=projected_tank_temp,
            target_temp=float(tank_target),
            outdoor_temp=slot_outdoor,
        )
        if prediction.estimated_minutes <= self.MIN_FORCE_DHW_MINUTES:
            return []

        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        actions: list[dict] = [
            {
                "ts": slot_start.isoformat(),
                "type": str(ActionType.FORCE_DHW_ON),
                "payload": {
                    "reason": (f"opportunistic_cheap_slot_{slot_price:.4f}_eur"),
                    "predicted_minutes": round(prediction.estimated_minutes),
                    "heating_rate": round(prediction.heating_rate_per_hour, 2),
                    "confidence": prediction.confidence,
                    "median_price": round(median_price, 4),
                },
            }
        ]
        if not suppress_dhw_off:
            actions.append(
                {
                    "ts": (slot_start + dt.timedelta(hours=hours_needed)).isoformat(),
                    "type": str(ActionType.FORCE_DHW_OFF),
                    "payload": {"reason": "opportunistic_top_up_complete"},
                }
            )
        return actions


class PreheatRulesMixin(SharedRuleHelpersMixin):
    def _plan_preheat(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        current_indoor_temp: float,
        current_outdoor_temp: float,
        current_water_temp: float,
        heat_curve: HeatCurveConfig | None = None,
        comfort_schedule: dict[str, list[int]] | None = None,
        comfort_temp_target: float = 20.5,
        comfort_temp_min: float = 18.0,
        tz_name: str | None = None,
        weather_full: list[dict] | None = None,
        current_zone_target_temp: float | None = None,
        current_zone_heat_min: int | None = None,
        current_zone_heat_max: int | None = None,
    ) -> list[dict]:
        actions = []
        if not weather:
            return actions

        passive_indoor = self._passive_indoor_forecast(
            [ts for ts, _ in weather],
            weather,
            current_indoor_temp,
            current_outdoor_temp,
            current_water_temp,
            heat_curve,
            weather_full,
        )
        cold_risk: tuple[dt.datetime, float, float] | None = None
        for ts, outdoor_temp in weather:
            if outdoor_temp is None or outdoor_temp >= 2.0:
                continue
            if heat_curve is not None and outdoor_temp >= heat_curve.heating_off_outdoor_c:
                continue
            target_indoor = (
                comfort_temp_target
                if comfort_schedule is None
                or is_comfort_hour(comfort_schedule, ts, tz_name=tz_name)
                else comfort_temp_min
            )
            predicted_indoor = passive_indoor.get(ts, current_indoor_temp)
            if predicted_indoor < target_indoor - self.COMFORT_SATISFIED_MARGIN_C:
                cold_risk = (ts, outdoor_temp, target_indoor)
                break

        if cold_risk is None:
            return actions

        first_cold, outdoor_at_cold, target_indoor = cold_risk
        baseline_supply = (
            heat_curve.supply_temperature(outdoor_at_cold)
            if heat_curve is not None
            else current_water_temp
        )
        if comfort_model.is_ready_for_control:
            required_water = comfort_model.required_zone_temp(
                target_indoor=target_indoor,
                outdoor_temp=outdoor_at_cold,
                hour=first_cold.hour,
                indoor_temp=current_indoor_temp,
            )
            if required_water is None:
                required_water = baseline_supply + 2.0
            target_zone_boost = required_water
        else:
            target_zone_boost = baseline_supply + 2.0

        if target_zone_boost <= current_water_temp:
            return actions

        water_deficit = target_zone_boost - current_water_temp
        requested_offset = 1 if water_deficit <= 1.0 else 2
        boost_targets = self._zone_boost_targets(
            current_zone_target_temp,
            current_zone_heat_min,
            current_zone_heat_max,
            offset=requested_offset,
        )
        if boost_targets is None:
            return actions

        prediction = thermal_model.predict_zone_heating_time(
            current_temp=current_water_temp,
            target_temp=target_zone_boost,
            outdoor_temp=outdoor_at_cold,
        )
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        # Wind-driven infiltration and evaporative cooling from rain hurt the
        # heat pump's effective output. Widen the reserved runtime so the
        # preheat still arrives at the target boost before the cold hour.
        weather_penalty = self._weather_heat_loss_factor(first_cold, weather_full)
        if weather_penalty > 1.0:
            hours_needed = max(hours_needed, math.ceil(hours_needed * weather_penalty))
        # Widen the look-back so the picker can reach overnight off-peak
        # troughs before an early-morning cold hour, matching the DHW rule.
        preheat_window_start = max(first_cold - dt.timedelta(hours=hours_needed + 8), horizon_start)
        weather_by_ts = {ts: temp for ts, temp in weather}

        def controller_can_heat(ts: dt.datetime) -> bool:
            if heat_curve is None:
                return True
            outdoor_temp = weather_by_ts.get(ts)
            if outdoor_temp is None:
                outdoor_temp = current_outdoor_temp
            return outdoor_temp < heat_curve.heating_off_outdoor_c

        window_prices = [
            (ts, p)
            for ts, p in prices
            if preheat_window_start <= ts < first_cold and controller_can_heat(ts)
        ]
        if not window_prices:
            return actions

        best_slot = self._find_lowest_heat_energy_cost_slot(
            window_prices,
            weather,
            hours_needed,
            current_outdoor_temp,
            tank_target=int(round(target_zone_boost)),
        )
        if best_slot:
            slot_start = best_slot[0][0]
            baseline_temperature, boost_temperature = boost_targets
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
                    "payload": {
                        "offset": boost_temperature - baseline_temperature,
                        "baseline_temperature": baseline_temperature,
                        "temperature": boost_temperature,
                        "reason": "thermal_preheat_before_cold",
                        "predicted_minutes": round(prediction.estimated_minutes),
                        "heating_rate": round(prediction.heating_rate_per_hour, 2),
                    },
                }
            )
            actions.append(
                {
                    "ts": (slot_start + dt.timedelta(hours=hours_needed)).isoformat(),
                    "type": str(ActionType.ZONE_TEMP_RESTORE),
                    "payload": {
                        "temperature": baseline_temperature,
                        "boost_temperature": boost_temperature,
                        "reason": "preheat_complete",
                    },
                }
            )

        return actions


class GuardrailRulesMixin(SharedRuleHelpersMixin):
    def _plan_indoor_guardrails(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        current_indoor_temp: float,
        current_outdoor_temp: float,
        current_water_temp: float,
        comfort_schedule: dict[str, list[int]],
        comfort_temp_target: float,
        comfort_temp_min: float,
        heat_curve: HeatCurveConfig | None = None,
        tz_name: str | None = None,
        weather_full: list[dict] | None = None,
        current_zone_target_temp: float | None = None,
        current_zone_heat_min: int | None = None,
        current_zone_heat_max: int | None = None,
    ) -> list[dict]:
        actions: list[dict] = []
        if not weather:
            return actions

        hours = min(24, len(weather))
        weather_forecast = []
        for h in range(hours):
            _, w_temp = weather[h] if h < len(weather) else (None, current_outdoor_temp)
            outdoor = w_temp if w_temp is not None else current_outdoor_temp
            weather_forecast.append(
                self._weather_conditions_at(
                    horizon_start + dt.timedelta(hours=h), weather_full, outdoor
                )
            )

        curve_supply_temps = [
            heat_curve.planned_supply_temperature(row["outdoor_temp"])
            if heat_curve is not None
            else current_water_temp
            for row in weather_forecast
        ]
        # Use the same explicit no-space-heat baseline as the saved plan
        # snapshot.  ``predict_indoor_curve`` treats a warm zone-water target
        # as active heating, which could hide a future comfort miss here while
        # the chart correctly showed the home coasting.
        curve = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor_temp,
            zone_water_temps=curve_supply_temps,
            heating_fractions=[0.0] * hours,
            weather_forecast=weather_forecast,
            hours=hours,
        )

        for h in range(hours):
            hour_ts = horizon_start + dt.timedelta(hours=h)
            if not is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
                continue
            if (
                heat_curve is not None
                and weather_forecast[h]["outdoor_temp"] >= heat_curve.heating_off_outdoor_c
            ):
                # The controller's own Värme AV threshold prevents space heat,
                # so do not create an action the pump cannot execute.
                continue

            predicted_indoor = (
                curve[h]["predicted_indoor_temp"] if h < len(curve) else current_indoor_temp
            )
            target = max(comfort_temp_target, comfort_temp_min)
            if predicted_indoor >= target or target - predicted_indoor < 0.3:
                continue

            heating_pred = thermal_model.predict_indoor_heating_time(
                current_temp=predicted_indoor,
                target_temp=target,
                outdoor_temp=weather_forecast[h]["outdoor_temp"],
            )
            hours_needed = max(1, int(heating_pred.estimated_hours + 0.9))
            weather_penalty = self._weather_heat_loss_factor(hour_ts, weather_full)
            if weather_penalty > 1.0:
                hours_needed = max(hours_needed, math.ceil(hours_needed * weather_penalty))
            # Widen the look-back so the picker can catch overnight off-peak
            # troughs before the guardrail hour, matching DHW and preheat.
            window_start = max(horizon_start, hour_ts - dt.timedelta(hours=hours_needed + 6))
            weather_by_ts = {ts: temp for ts, temp in weather}

            def controller_can_heat(ts: dt.datetime) -> bool:
                if heat_curve is None:
                    return True
                outdoor_temp = weather_by_ts.get(ts)
                if outdoor_temp is None:
                    outdoor_temp = current_outdoor_temp
                return outdoor_temp < heat_curve.heating_off_outdoor_c

            window_prices = [
                (ts, p)
                for ts, p in prices
                if window_start <= ts <= hour_ts and controller_can_heat(ts)
            ]
            # A boost target is a supply water temperature; give the trained
            # COP model that number so cost/kWh reflects the actual load
            # instead of the outdoor-only default curve.
            boost_supply_temp = (
                int(round(curve_supply_temps[h])) if h < len(curve_supply_temps) else None
            )
            best_slot = (
                self._find_lowest_heat_energy_cost_slot(
                    window_prices,
                    weather,
                    hours_needed,
                    current_outdoor_temp,
                    tank_target=boost_supply_temp,
                )
                if window_prices
                else None
            )
            slot_start = best_slot[0][0] if best_slot else hour_ts
            indoor_deficit = target - predicted_indoor
            requested_offset = 1 if indoor_deficit <= 0.5 else 2
            boost_targets = self._zone_boost_targets(
                current_zone_target_temp,
                current_zone_heat_min,
                current_zone_heat_max,
                offset=requested_offset,
            )
            if boost_targets is None:
                return actions
            baseline_temperature, boost_temperature = boost_targets

            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
                    "payload": {
                        "offset": boost_temperature - baseline_temperature,
                        "baseline_temperature": baseline_temperature,
                        "temperature": boost_temperature,
                        "reason": "indoor_guardrail",
                        "predicted_indoor": round(predicted_indoor, 1),
                        "target": target,
                        "predicted_minutes": round(heating_pred.estimated_minutes),
                    },
                }
            )
            actions.append(
                {
                    "ts": (slot_start + dt.timedelta(hours=hours_needed)).isoformat(),
                    "type": str(ActionType.ZONE_TEMP_RESTORE),
                    "payload": {
                        "temperature": baseline_temperature,
                        "boost_temperature": boost_temperature,
                        "reason": "indoor_guardrail_complete",
                    },
                }
            )
            break

        current_is_comfort = is_comfort_hour(comfort_schedule, horizon_start, tz_name=tz_name)
        if (
            current_is_comfort
            # Never start an emergency boost while the measured home is at or
            # above its comfort target. The old +1°C allowance could heat an
            # already warm home merely because the simple cooling estimate was
            # pessimistic.
            and current_indoor_temp < comfort_temp_target - 0.3
            and not actions
            and (heat_curve is None or current_outdoor_temp < heat_curve.heating_off_outdoor_c)
        ):
            cooling_pred = thermal_model.predict_indoor_cooling_time(
                current_temp=current_indoor_temp,
                min_temp=comfort_temp_target - 1.0,
                outdoor_temp=current_outdoor_temp,
            )
            if 0 < cooling_pred.estimated_minutes < 120:
                boost_targets = self._zone_boost_targets(
                    current_zone_target_temp,
                    current_zone_heat_min,
                    current_zone_heat_max,
                    offset=2,
                )
                if boost_targets is None:
                    return actions
                baseline_temperature, boost_temperature = boost_targets
                actions.append(
                    {
                        "ts": horizon_start.isoformat(),
                        "type": str(ActionType.ZONE_TEMP_BOOST),
                        "payload": {
                            "offset": boost_temperature - baseline_temperature,
                            "baseline_temperature": baseline_temperature,
                            "temperature": boost_temperature,
                            "reason": "indoor_cooling_imminent",
                            "minutes_until_cold": round(cooling_pred.estimated_minutes),
                            "current_indoor": round(current_indoor_temp, 1),
                        },
                    }
                )
                boost_hours = max(1, int(cooling_pred.estimated_minutes / 60))
                actions.append(
                    {
                        "ts": (horizon_start + dt.timedelta(hours=boost_hours)).isoformat(),
                        "type": str(ActionType.ZONE_TEMP_RESTORE),
                        "payload": {
                            "temperature": baseline_temperature,
                            "boost_temperature": boost_temperature,
                            "reason": "indoor_cooling_boost_complete",
                        },
                    }
                )

        return actions


class ModeRulesMixin(SharedRuleHelpersMixin):
    # Only treat an hour as a peak if it is materially above the horizon
    # median; a flat curve with a marginal high hour should not force quiet
    # mode. Bounded to a small fraction of the horizon so a genuine
    # multi-peak day still leaves the compressor useful hours to catch up.
    PEAK_MEDIAN_MULTIPLIER = 1.30
    PEAK_MAX_FRACTION = 6  # at most 1/6 (~4h out of 24)

    def _plan_peak_avoidance(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
    ) -> list[dict]:
        actions = []
        if not prices:
            return actions

        unique_prices = set(p for _, p in prices)
        if len(unique_prices) <= 1:
            return actions

        price_values = sorted(p for _, p in prices)
        median_price = price_values[len(price_values) // 2]
        threshold = median_price * self.PEAK_MEDIAN_MULTIPLIER
        max_hours = max(1, len(prices) // self.PEAK_MAX_FRACTION)
        sorted_prices = sorted(prices, key=lambda x: x[1], reverse=True)
        expensive_hours = [
            (ts, price) for ts, price in sorted_prices[:max_hours] if price >= threshold
        ]
        weather_dict = {ts: t for ts, t in weather} if weather else {}

        for ts, price in expensive_hours:
            outdoor_t = weather_dict.get(ts)
            if outdoor_t is not None and outdoor_t < 0:
                continue
            actions.append(
                {
                    "ts": ts.isoformat(),
                    "type": str(ActionType.QUIET_MODE_ON),
                    "payload": {
                        "reason": f"peak_price_{price:.4f}_eur_kwh",
                        "level": 1,
                    },
                }
            )
            actions.append(
                {
                    "ts": (ts + dt.timedelta(hours=1)).isoformat(),
                    "type": str(ActionType.QUIET_MODE_OFF),
                    "payload": {"reason": "peak_avoidance_end"},
                }
            )

        return actions

    def _plan_quiet_mode(
        self,
        horizon_start: dt.datetime,
        start_hour: int = 22,
        end_hour: int = 6,
        tz_name: str | None = None,
    ) -> list[dict]:
        from packages.core.settings_service import _to_local

        actions = []
        for offset_hours in range(24):
            ts = horizon_start + dt.timedelta(hours=offset_hours)
            local_hour = _to_local(ts, tz_name).hour
            if local_hour == start_hour:
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": str(ActionType.QUIET_MODE_ON),
                        "payload": {"reason": "night_quiet_schedule", "level": 2},
                    }
                )
            elif local_hour == end_hour:
                actions.append(
                    {
                        "ts": ts.isoformat(),
                        "type": str(ActionType.QUIET_MODE_OFF),
                        "payload": {"reason": "night_quiet_end"},
                    }
                )
        return actions

    def _plan_eco_comfort(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        horizon_start: dt.datetime,
        comfort_schedule: dict[str, list[int]],
        comfort_override_pct: int = 90,
        eco_upgrade_pct: int = 25,
        tz_name: str | None = None,
        current_indoor_temp: float | None = None,
        current_outdoor_temp: float = 5.0,
        current_water_temp: float = 35.0,
        heat_curve: HeatCurveConfig | None = None,
        comfort_temp_target: float | None = None,
        comfort_temp_min: float | None = None,
        weather_full: list[dict] | None = None,
        special_status_supported: bool = False,
        current_special_status: int | None = None,
        zone_control_windows: list[tuple[dt.datetime, dt.datetime]] | None = None,
    ) -> list[dict]:
        actions = []
        if not prices or special_status_supported is not True:
            return actions

        if current_special_status is None:
            current_mode = "normal"
        elif current_special_status == 1:
            current_mode = "eco"
        elif current_special_status == 2:
            current_mode = "comfort"
        else:
            return actions

        price_values = sorted(p for _, p in prices)
        p_eco = price_values[max(0, len(price_values) * eco_upgrade_pct // 100 - 1)]
        p_comfort = price_values[
            min(len(price_values) - 1, len(price_values) * comfort_override_pct // 100)
        ]
        flat_price = len(set(price_values)) <= 1
        temp_by_ts = {w_ts.isoformat(): w_temp for w_ts, w_temp in weather}
        passive_indoor = (
            self._passive_indoor_forecast(
                [ts for ts, _ in prices],
                weather,
                current_indoor_temp,
                current_outdoor_temp,
                current_water_temp,
                heat_curve,
                weather_full,
            )
            if current_indoor_temp is not None and comfort_temp_target is not None
            else {}
        )
        mild_outdoor_threshold = 5.0
        for ts, price in prices:
            comparable_ts = (
                ts.replace(tzinfo=dt.timezone.utc)
                if ts.tzinfo is None
                else ts.astimezone(dt.timezone.utc)
            )
            if any(start <= comparable_ts <= end for start, end in (zone_control_windows or [])):
                continue

            scheduled_comfort = is_comfort_hour(comfort_schedule, ts, tz_name=tz_name)
            outdoor_temp = temp_by_ts.get(ts.isoformat())
            predicted_indoor = passive_indoor.get(ts)

            if scheduled_comfort:
                if (
                    predicted_indoor is not None
                    and comfort_temp_target is not None
                    and predicted_indoor >= comfort_temp_target + self.COMFORT_SATISFIED_MARGIN_C
                ):
                    # A comfort window is a temperature objective, not an
                    # instruction to clear ECO or restore Normal mode.  Keep
                    # the lower-demand mode while stored heat already covers
                    # the target, then reconsider when the forecast cools.
                    target_mode = "eco"
                    reason = (
                        "comfort_satisfied_forecast_"
                        f"{predicted_indoor:.1f}_target_{comfort_temp_target:.1f}"
                    )
                elif not flat_price and price >= p_comfort:
                    target_mode = "normal"
                    reason = f"comfort_hour_but_peak_price_{price:.4f}"
                elif outdoor_temp is not None and outdoor_temp >= mild_outdoor_threshold:
                    target_mode = "normal"
                    reason = f"comfort_hour_but_mild_outdoor_{outdoor_temp:.1f}C"
                else:
                    target_mode = "comfort"
                    reason = "comfort_schedule"
            else:
                if (
                    predicted_indoor is not None
                    and comfort_temp_min is not None
                    and predicted_indoor >= comfort_temp_min + self.COMFORT_SATISFIED_MARGIN_C
                ):
                    # Cheap electricity is not a reason to restore Normal
                    # mode when the lower setback target is already covered
                    # by stored heat.
                    target_mode = "eco"
                    reason = (
                        "setback_satisfied_forecast_"
                        f"{predicted_indoor:.1f}_target_{comfort_temp_min:.1f}"
                    )
                elif not flat_price and price <= p_eco:
                    target_mode = "normal"
                    reason = f"eco_hour_but_cheap_price_{price:.4f}"
                else:
                    target_mode = "eco"
                    reason = "outside_comfort_schedule"

            if (
                heat_curve is not None
                and outdoor_temp is not None
                and outdoor_temp >= heat_curve.heating_off_outdoor_c
                and target_mode in {"normal", "comfort"}
            ):
                # Normal/Comfort only changes the heating curve. Above the
                # controller's own Värme AV threshold it cannot request room
                # heat, so exposing it as a comfort action is misleading.
                continue

            if target_mode == current_mode:
                continue

            action_type = {
                "comfort": ActionType.COMFORT_MODE_ON,
                "eco": ActionType.ECO_MODE_ON,
                "normal": ActionType.NORMAL_MODE_ON,
            }[target_mode]
            actions.append(
                {"ts": ts.isoformat(), "type": str(action_type), "payload": {"reason": reason}}
            )
            current_mode = target_mode

        return actions

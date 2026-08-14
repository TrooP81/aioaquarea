"""Strategy mixins for the rules optimizer."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from packages.core.heat_curve import HeatCurveConfig
from packages.core.settings_service import dhw_deadlines_from_schedule, is_comfort_hour
from packages.ml.cop_model_core import COPModel
from packages.ml.comfort_model import comfort_model
from packages.ml.thermal import thermal_model
from packages.optimizer.actions import ActionType


class SharedRuleHelpersMixin:
    COMFORT_SATISFIED_MARGIN_C = 0.3
    ZONE_WATER_TARGET_MIN_C = 20
    ZONE_WATER_TARGET_MAX_C = 65

    @classmethod
    def _zone_boost_targets(
        cls, current_zone_target_temp: float | None, offset: int = 2
    ) -> tuple[int, int] | None:
        """Freeze a safe Panasonic water target for a paired boost and restore."""
        if (
            not isinstance(current_zone_target_temp, (int, float))
            or isinstance(current_zone_target_temp, bool)
            or not cls.ZONE_WATER_TARGET_MIN_C
            <= current_zone_target_temp
            <= cls.ZONE_WATER_TARGET_MAX_C
        ):
            return None

        baseline = int(round(current_zone_target_temp))
        boost = baseline + offset
        if not cls.ZONE_WATER_TARGET_MIN_C <= boost <= cls.ZONE_WATER_TARGET_MAX_C:
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
    ) -> list[tuple[dt.datetime, float]] | None:
        """Choose heat-pump hours by electricity cost per delivered thermal kWh."""
        effective_prices = []
        original_prices = {ts: price for ts, price in prices}
        for ts, price in prices:
            outdoor_temp = self._get_outdoor_at(weather, ts, fallback_outdoor_temp)
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

    def _find_lowest_dhw_energy_cost_slot(
        self,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        hours_needed: int,
        fallback_outdoor_temp: float,
    ) -> list[tuple[dt.datetime, float]] | None:
        """Choose a DHW slot by electricity cost per unit of delivered heat.

        A heat pump produces more heat per kWh of electricity when outdoor air
        is warmer. Comparing raw spot prices alone can therefore select a cold
        hour that costs more and consumes more electricity for the same tank
        recharge. ``price / COP`` is the effective price per thermal kWh.
        """
        return self._find_lowest_heat_energy_cost_slot(
            prices,
            weather,
            hours_needed,
            fallback_outdoor_temp,
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
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        thermal_model.predict_tank_cooling_time(
            current_temp=float(tank_target),
            min_temp=current_tank_temp,
            outdoor_temp=current_outdoor_temp,
        )
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
            window_start = max(latest_start - dt.timedelta(hours=4), horizon_start)
            window_prices = [(ts, p) for ts, p in prices if window_start <= ts < deadline]
            if not window_prices:
                continue

            best_slot = self._find_lowest_dhw_energy_cost_slot(
                window_prices,
                weather,
                hours_needed,
                current_outdoor_temp,
            )
            if not best_slot:
                continue

            slot_start = best_slot[0][0]
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.FORCE_DHW_ON),
                    "payload": {
                        "reason": f"thermal_optimized_before_{ready_hour}:00",
                        "predicted_minutes": round(prediction.estimated_minutes),
                        "heating_rate": round(prediction.heating_rate_per_hour, 2),
                        "confidence": prediction.confidence,
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
    ) -> list[dict]:
        actions = []
        boost_targets = self._zone_boost_targets(current_zone_target_temp)
        if not weather or boost_targets is None:
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

        prediction = thermal_model.predict_zone_heating_time(
            current_temp=current_water_temp,
            target_temp=target_zone_boost,
            outdoor_temp=outdoor_at_cold,
        )
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        preheat_window_start = max(first_cold - dt.timedelta(hours=hours_needed + 4), horizon_start)
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
        )
        if best_slot:
            slot_start = best_slot[0][0]
            baseline_temperature, boost_temperature = boost_targets
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
                    "payload": {
                        "offset": +2,
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
    ) -> list[dict]:
        actions: list[dict] = []
        boost_targets = self._zone_boost_targets(current_zone_target_temp)
        if not weather or boost_targets is None:
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
            window_start = max(horizon_start, hour_ts - dt.timedelta(hours=hours_needed + 2))
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
            best_slot = (
                self._find_lowest_heat_energy_cost_slot(
                    window_prices,
                    weather,
                    hours_needed,
                    current_outdoor_temp,
                )
                if window_prices
                else None
            )
            slot_start = best_slot[0][0] if best_slot else hour_ts
            baseline_temperature, boost_temperature = boost_targets

            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
                    "payload": {
                        "offset": +2,
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
                baseline_temperature, boost_temperature = boost_targets
                actions.append(
                    {
                        "ts": horizon_start.isoformat(),
                        "type": str(ActionType.ZONE_TEMP_BOOST),
                        "payload": {
                            "offset": +2,
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

        sorted_prices = sorted(prices, key=lambda x: x[1], reverse=True)
        n_expensive = max(1, len(sorted_prices) // 20)
        expensive_hours = sorted_prices[:n_expensive]
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
    ) -> list[dict]:
        actions = []
        if not prices:
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
        current_mode = None

        for ts, price in prices:
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

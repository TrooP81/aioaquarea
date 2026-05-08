"""Strategy mixins for the rules optimizer."""

from __future__ import annotations

import datetime as dt

from packages.core.settings_service import dhw_deadlines_from_schedule, is_comfort_hour
from packages.ml.comfort_model import comfort_model
from packages.ml.thermal import thermal_model
from packages.optimizer.actions import ActionType


class SharedRuleHelpersMixin:
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
                (slot[j + 1][0] - slot[j][0]).total_seconds() == 3600
                for j in range(len(slot) - 1)
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


class DHWRulesMixin(SharedRuleHelpersMixin):
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
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        thermal_model.predict_tank_cooling_time(
            current_temp=float(tank_target),
            min_temp=current_tank_temp,
            outdoor_temp=current_outdoor_temp,
        )
        ready_hours = dhw_deadlines_from_schedule(comfort_schedule, horizon_start, tz_name=tz_name)

        for ready_hour in ready_hours:
            deadline = horizon_start.replace(hour=ready_hour, minute=0)
            if deadline <= horizon_start:
                deadline += dt.timedelta(days=1)

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

            best_slot = self._find_cheapest_slot(window_prices, hours_needed)
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
    ) -> list[dict]:
        actions = []
        if not weather:
            return actions

        cold_hours = [(ts, t) for ts, t in weather if t is not None and t < 2.0]
        if not cold_hours:
            return actions

        first_cold = cold_hours[0][0]
        target_indoor = current_indoor_temp + 2.0
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
            target_zone_boost = current_water_temp + 2.0

        outdoor_at_cold = cold_hours[0][1] if cold_hours[0][1] is not None else 0.0
        prediction = thermal_model.predict_zone_heating_time(
            current_temp=current_water_temp,
            target_temp=target_zone_boost,
            outdoor_temp=outdoor_at_cold,
        )
        hours_needed = max(1, int(prediction.estimated_hours + 0.9))
        preheat_window_start = max(first_cold - dt.timedelta(hours=hours_needed + 4), horizon_start)
        window_prices = [
            (ts, p) for ts, p in prices if preheat_window_start <= ts < first_cold
        ]
        if not window_prices:
            return actions

        best_slot = self._find_cheapest_slot(window_prices, hours_needed)
        if best_slot:
            slot_start = best_slot[0][0]
            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
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
                    "type": str(ActionType.ZONE_TEMP_RESTORE),
                    "payload": {"reason": "preheat_complete"},
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
        tz_name: str | None = None,
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
                {
                    "outdoor_temp": outdoor,
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "hour": (horizon_start + dt.timedelta(hours=h)).hour,
                }
            )

        curve = thermal_model.predict_indoor_curve(
            current_indoor=current_indoor_temp,
            zone_water_temps=[current_water_temp] * hours,
            weather_forecast=weather_forecast,
            hours=hours,
        )

        for h in range(hours):
            hour_ts = horizon_start + dt.timedelta(hours=h)
            if not is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
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
            window_prices = [(ts, p) for ts, p in prices if window_start <= ts <= hour_ts]
            best_slot = self._find_cheapest_slot(window_prices, hours_needed) if window_prices else None
            slot_start = best_slot[0][0] if best_slot else hour_ts

            actions.append(
                {
                    "ts": slot_start.isoformat(),
                    "type": str(ActionType.ZONE_TEMP_BOOST),
                    "payload": {
                        "offset": +2,
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
                    "payload": {"reason": "indoor_guardrail_complete"},
                }
            )
            break

        current_is_comfort = is_comfort_hour(comfort_schedule, horizon_start, tz_name=tz_name)
        if current_is_comfort and current_indoor_temp < comfort_temp_target + 1.0 and not actions:
            cooling_pred = thermal_model.predict_indoor_cooling_time(
                current_temp=current_indoor_temp,
                min_temp=comfort_temp_target - 1.0,
                outdoor_temp=current_outdoor_temp,
            )
            if 0 < cooling_pred.estimated_minutes < 120:
                actions.append(
                    {
                        "ts": horizon_start.isoformat(),
                        "type": str(ActionType.ZONE_TEMP_BOOST),
                        "payload": {
                            "offset": +2,
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
                        "payload": {"reason": "indoor_cooling_boost_complete"},
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
                    "payload": {"reason": f"peak_price_{price:.4f}_eur_kwh"},
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
        mild_outdoor_threshold = 5.0
        current_mode = None

        for ts, price in prices:
            scheduled_comfort = is_comfort_hour(comfort_schedule, ts, tz_name=tz_name)
            outdoor_temp = temp_by_ts.get(ts.isoformat())

            if scheduled_comfort:
                if not flat_price and price >= p_comfort:
                    target_mode = "normal"
                    reason = f"comfort_hour_but_peak_price_{price:.4f}"
                elif outdoor_temp is not None and outdoor_temp >= mild_outdoor_threshold:
                    target_mode = "normal"
                    reason = f"comfort_hour_but_mild_outdoor_{outdoor_temp:.1f}C"
                else:
                    target_mode = "comfort"
                    reason = "comfort_schedule"
            else:
                if not flat_price and price <= p_eco:
                    target_mode = "normal"
                    reason = f"eco_hour_but_cheap_price_{price:.4f}"
                else:
                    target_mode = "eco"
                    reason = "outside_comfort_schedule"

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

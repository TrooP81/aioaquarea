"""Core rules optimizer orchestration and data access helpers."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.heat_curve import HeatCurveConfig
from packages.core.control_temperature import get_control_temperature
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.models import ConsumptionRecord, ShowerEventRecord
from packages.core.time_slots import next_hour_boundary
from packages.core.settings_service import (
    get_effective_schedule,
    get_float_setting,
    get_heat_curve_config,
    get_int_setting,
    get_user_tz,
    is_comfort_hour,
)
from packages.ml.thermal import thermal_model

from .rule_mixins import DHWRulesMixin, GuardrailRulesMixin, ModeRulesMixin, PreheatRulesMixin

logger = structlog.get_logger()


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

    @staticmethod
    def _normalise_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate command transitions while preserving plan intent.

        Multiple rule sources can request different quiet levels. Collapse only
        identical levels, while preserving legitimate LEVEL1 -> LEVEL2 changes.
        """
        ordered = sorted(actions, key=lambda action: str(action.get("ts", "")))

        # A peak-avoidance window can end on the exact hour that scheduled
        # quiet time begins. Exposing OFF then ON is both noisy and misleading,
        # so keep the strongest requested level at a timestamp before applying
        # the normal state-machine collapse.
        def quiet_level(action: dict[str, Any]) -> int:
            if str(action.get("type", "")) == "quiet_mode_off":
                return 0
            level = action.get("payload", {}).get("level", 1)
            return (
                level
                if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 3
                else 1
            )

        quiet_at_timestamp: dict[str, dict[str, Any]] = {}
        non_quiet: list[dict[str, Any]] = []
        for action in ordered:
            if str(action.get("type", "")) in {"quiet_mode_on", "quiet_mode_off"}:
                timestamp = str(action.get("ts", ""))
                current = quiet_at_timestamp.get(timestamp)
                if current is None or quiet_level(action) >= quiet_level(current):
                    quiet_at_timestamp[timestamp] = action
            else:
                non_quiet.append(action)
        ordered = sorted(
            [*non_quiet, *quiet_at_timestamp.values()],
            key=lambda action: str(action.get("ts", "")),
        )
        normalised: list[dict[str, Any]] = []
        seen_exact: set[tuple[str, str, str]] = set()
        active_quiet_level: int | None = None

        for action in ordered:
            action_type = str(action.get("type", ""))
            timestamp = str(action.get("ts", ""))
            payload = action.get("payload", {})
            payload_key = json.dumps(payload, sort_keys=True, default=str)
            exact_key = (timestamp, action_type, payload_key)
            if exact_key in seen_exact:
                continue
            seen_exact.add(exact_key)

            if action_type == "quiet_mode_on":
                requested_level = quiet_level(action)
                if active_quiet_level == requested_level:
                    continue
                active_quiet_level = requested_level
            elif action_type == "quiet_mode_off":
                if active_quiet_level == 0:
                    continue
                active_quiet_level = 0

            normalised.append(action)

        return normalised

    async def generate_plan(self) -> dict[str, Any] | None:
        now = dt.datetime.now(dt.timezone.utc)
        horizon_start = next_hour_boundary(now)
        horizon_end = horizon_start + dt.timedelta(hours=24)

        thermal_model.load_latest()

        async with get_session() as session:
            prices = await self._get_prices(session, horizon_start, horizon_end)
            weather = await self._get_weather(session, horizon_start, horizon_end)
            weather_full = await self._get_weather_full(session, horizon_start, horizon_end)
            last_status = await self._get_last_status(session)
            outdoor_reading = await resolve_outdoor_temperature(
                session,
                heat_pump_c=(
                    last_status.heat_pump_outdoor_temp
                    if last_status is not None and last_status.heat_pump_outdoor_temp is not None
                    else last_status.outdoor_temp
                    if last_status is not None
                    else None
                ),
                at=now,
            )

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
            outdoor_reading.effective_c if outdoor_reading.effective_c is not None else 7.0
        )
        current_water_temp = (
            last_status.zone1_temp if last_status and last_status.zone1_temp is not None else 35.0
        )
        current_zone_target_temp = (
            last_status.zone1_target_temp
            if last_status and last_status.zone1_target_temp is not None
            else None
        )
        tank_target = (
            last_status.tank_target_temp if last_status and last_status.tank_target_temp else 52
        )

        control_temperature = await get_control_temperature(now=now)
        latest_indoor_temp = control_temperature.value
        # A measured indoor value is the controller state.  The comfort model
        # may estimate future changes, but must never overwrite that state with
        # a prediction (especially while its validation error is material).
        # Never turn a missing or stale observation into a fabricated 20 °C
        # indoor state.  Price/DHW planning can still continue, but every
        # indoor-comfort decision and saved indoor forecast must remain
        # explicitly unavailable until we have a trusted observation.
        current_indoor_temp = latest_indoor_temp if control_temperature.is_usable else None
        if not control_temperature.is_usable:
            logger.warning(
                "space_heating_control_paused_no_trusted_indoor_sensor",
                reason=control_temperature.reason,
                sample_count=control_temperature.sample_count,
            )

        actions: list[dict[str, Any]] = []

        heat_curve = await get_heat_curve_config()
        learned_threshold = await get_float_setting("learned_schedule_threshold")
        comfort_schedule = await get_effective_schedule(learned_threshold=learned_threshold)
        tz_name = await get_user_tz()
        comfort_temp_target = await get_float_setting("comfort_temp_target")
        comfort_temp_min = await get_float_setting("comfort_temp_min")

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
        if control_temperature.is_usable:
            actions.extend(
                self._plan_preheat(
                    prices,
                    weather,
                    horizon_start,
                    current_indoor_temp,
                    current_outdoor_temp,
                    current_water_temp,
                    heat_curve=heat_curve,
                    comfort_schedule=comfort_schedule,
                    comfort_temp_target=comfort_temp_target,
                    comfort_temp_min=comfort_temp_min,
                    tz_name=tz_name,
                    weather_full=weather_full,
                    current_zone_target_temp=current_zone_target_temp,
                )
            )
        actions.extend(self._plan_peak_avoidance(prices, weather, horizon_start))

        quiet_start = await get_int_setting("quiet_mode_start")
        quiet_end = await get_int_setting("quiet_mode_end")
        actions.extend(
            self._plan_quiet_mode(horizon_start, quiet_start, quiet_end, tz_name=tz_name)
        )

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
                current_indoor_temp=current_indoor_temp,
                current_outdoor_temp=current_outdoor_temp,
                current_water_temp=current_water_temp,
                heat_curve=heat_curve,
                comfort_temp_target=comfort_temp_target,
                comfort_temp_min=comfort_temp_min,
                weather_full=weather_full,
            )
        )
        if control_temperature.is_usable:
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
                    heat_curve=heat_curve,
                    tz_name=tz_name,
                    weather_full=weather_full,
                    current_zone_target_temp=current_zone_target_temp,
                )
            )

        if not actions:
            return None

        actions = self._normalise_actions(actions)
        cost_estimate = await self._estimate_cost(actions, prices)
        forecast_snapshot = self._build_forecast_snapshot(
            prices=prices,
            weather=weather,
            weather_full=weather_full,
            actions=actions,
            horizon_start=horizon_start,
            current_indoor=current_indoor_temp,
            current_water_temp=current_water_temp,
            heat_curve=heat_curve,
            comfort_schedule=comfort_schedule,
            comfort_temp_target=comfort_temp_target,
            comfort_temp_min=comfort_temp_min,
            tz_name=tz_name,
            control_input={
                "available": control_temperature.is_usable,
                "confidence": control_temperature.confidence,
                "reason": control_temperature.reason,
                "reference_sensor_id": control_temperature.reference_sensor_id,
                "reference_sensor_label": control_temperature.reference_sensor_label,
                "reference_room": control_temperature.reference_room,
                "sensor_ids": [sensor.device_id for sensor in control_temperature.sensors],
                "sensor_count": control_temperature.sensor_count,
                "sample_count": control_temperature.sample_count,
                "observed_at": (
                    control_temperature.latest_reading.isoformat()
                    if control_temperature.latest_reading is not None
                    else None
                ),
                "outdoor_temperature": {
                    "effective_c": outdoor_reading.effective_c,
                    "heat_pump_c": outdoor_reading.heat_pump_c,
                    "weather_c": outdoor_reading.weather_c,
                    "source": outdoor_reading.source,
                    "weather_provider": outdoor_reading.weather_provider,
                    "compensation_c": outdoor_reading.compensation_c,
                    "fallback_reason": outdoor_reading.fallback_reason,
                },
            },
        )

        return {
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
            "actions": actions,
            "version": self.VERSION,
            "cost_estimate": cost_estimate,
            "forecast_snapshot": forecast_snapshot,
            "control_input": {
                "indoor_temp": latest_indoor_temp,
                "confidence": control_temperature.confidence,
                "sensor_count": control_temperature.sensor_count,
                "sample_count": control_temperature.sample_count,
                "reason": control_temperature.reason,
                "outdoor_temperature": {
                    "effective_c": outdoor_reading.effective_c,
                    "heat_pump_c": outdoor_reading.heat_pump_c,
                    "weather_c": outdoor_reading.weather_c,
                    "source": outdoor_reading.source,
                    "weather_provider": outdoor_reading.weather_provider,
                    "compensation_c": outdoor_reading.compensation_c,
                    "fallback_reason": outdoor_reading.fallback_reason,
                },
            },
        }

    async def _estimate_cost(
        self, actions: list[dict], prices: list[tuple[dt.datetime, float]]
    ) -> float:
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

    async def _get_weather_full(
        self, session: AsyncSession, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, Any]]:
        from packages.optimizer.data_access import get_weather_full

        return await get_weather_full(session, start, end)

    @staticmethod
    def _build_forecast_snapshot(
        *,
        prices: list[tuple[dt.datetime, float]],
        weather: list[tuple[dt.datetime, float]],
        weather_full: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        horizon_start: dt.datetime,
        current_indoor: float | None,
        current_water_temp: float,
        heat_curve: HeatCurveConfig,
        comfort_schedule: dict[str, list[int]],
        comfort_temp_target: float,
        comfort_temp_min: float,
        tz_name: str | None,
        control_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Freeze the rules engine's own forecast inputs and control scenario.

        Rules plans do not have an LP state vector.  Their equivalent expected
        trajectory is the same thermal/comfort-model simulation the guardrails
        use, driven by the actual control actions that were selected for this
        plan.  Saving it means later UI refreshes cannot silently replace a
        plan's assumptions with newer weather or price feeds.
        """

        ordered_actions: list[tuple[dt.datetime, dict[str, Any]]] = []
        for action in actions:
            try:
                action_ts = dt.datetime.fromisoformat(str(action["ts"]))
            except (KeyError, TypeError, ValueError):
                continue
            if action_ts.tzinfo is None:
                action_ts = action_ts.replace(tzinfo=dt.timezone.utc)
            ordered_actions.append((action_ts, action))
        ordered_actions.sort(key=lambda item: item[0])

        def weather_for_slot(slot_ts: dt.datetime, fallback_temperature: float) -> dict[str, Any]:
            candidates = [row for row in weather_full if isinstance(row.get("ts"), dt.datetime)]
            if candidates:
                closest = min(
                    candidates, key=lambda row: abs((row["ts"] - slot_ts).total_seconds())
                )
                if abs((closest["ts"] - slot_ts).total_seconds()) <= 90 * 60:

                    def value(key: str, default: float, *, non_negative: bool = False) -> float:
                        try:
                            number = float(closest.get(key))
                        except (TypeError, ValueError):
                            return default
                        return max(0.0, number) if non_negative else number

                    return {
                        "outdoor_temp": value("temperature", fallback_temperature),
                        "wind_speed": value("wind_speed", 3.0, non_negative=True),
                        "irradiance": value("irradiance", 0.0, non_negative=True),
                        "precipitation": value("precipitation", 0.0, non_negative=True),
                        "weather_source": closest.get("source"),
                        "forecast_issued_at": (
                            closest["forecast_issued_at"].isoformat()
                            if isinstance(closest.get("forecast_issued_at"), dt.datetime)
                            else None
                        ),
                    }
            return {
                "outdoor_temp": float(fallback_temperature),
                "wind_speed": 3.0,
                "irradiance": 0.0,
                "precipitation": 0.0,
                "weather_source": "fallback",
                "forecast_issued_at": None,
            }

        weather_forecast: list[dict[str, Any]] = []
        price_forecast: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        zone_water_temps: list[float] = []
        heating_fractions: list[float] = []
        action_index = 0
        mode_offset = 0.0
        boost_offset = 0.0
        explicit_heat_fraction = 0.0
        manual_supply_override: float | None = None

        for hour, (slot_ts, price) in enumerate(prices):
            fallback_temperature = (
                weather[hour][1] if hour < len(weather) and weather[hour][1] is not None else 5.0
            )
            weather_slot = weather_for_slot(slot_ts, float(fallback_temperature))
            weather_forecast.append(
                {
                    "ts": slot_ts.isoformat(),
                    "hour": slot_ts.hour,
                    **weather_slot,
                }
            )
            price_forecast.append(
                {
                    "ts": slot_ts.isoformat(),
                    "price_eur_per_kwh": float(price),
                }
            )

            while (
                action_index < len(ordered_actions) and ordered_actions[action_index][0] <= slot_ts
            ):
                action = ordered_actions[action_index][1]
                action_type = str(action.get("type", ""))
                payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                if action_type == "zone_temp_boost":
                    boost_offset = float(payload.get("offset", 2.0))
                    explicit_heat_fraction = 1.0
                elif action_type == "zone_temp_restore":
                    boost_offset = 0.0
                    explicit_heat_fraction = 0.0
                    manual_supply_override = None
                elif action_type == "eco_mode_on":
                    mode_offset = -5.0
                elif action_type == "comfort_mode_on":
                    mode_offset = 5.0
                elif action_type in {"normal_mode_on", "eco_mode_off"}:
                    mode_offset = 0.0
                elif action_type == "set_zone_heat_temperature":
                    try:
                        manual_supply_override = float(payload["temperature"])
                        mode_offset = 0.0
                        boost_offset = 0.0
                        explicit_heat_fraction = 1.0
                    except (KeyError, TypeError, ValueError):
                        pass
                action_index += 1

            configured_supply = heat_curve.planned_supply_temperature(weather_slot["outdoor_temp"])
            zone_water_temps.append(
                (
                    manual_supply_override
                    if manual_supply_override is not None
                    else configured_supply
                )
                + mode_offset
                + boost_offset
            )
            # A NORMAL/ECO/QUIET mode is a heat-pump configuration, not an
            # explicit compressor-on command. Only an actual zone-temperature
            # action contributes planned space heat to this rules forecast.
            heating_fractions.append(
                explicit_heat_fraction
                if weather_slot["outdoor_temp"] < heat_curve.heating_off_outdoor_c
                else 0.0
            )
            target = (
                comfort_temp_target
                if is_comfort_hour(comfort_schedule, slot_ts, tz_name=tz_name)
                else comfort_temp_min
            )
            targets.append(
                {
                    "hour": hour + 1,
                    "ts": (slot_ts + dt.timedelta(hours=1)).isoformat(),
                    "target": round(target, 1),
                    "comfort_hour": is_comfort_hour(comfort_schedule, slot_ts, tz_name=tz_name),
                }
            )

        control_input = control_input or {
            "available": current_indoor is not None,
            "reason": "missing_control_temperature_provenance",
        }
        unavailable_snapshot = {
            "version": "indoor_forecast_v2",
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": control_input.get("reason")
            or "no_trusted_indoor_observation",
            "current_indoor": None,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": targets,
            "weather_forecast": weather_forecast,
            "price_forecast": price_forecast,
            "heat_curve": heat_curve.as_dict(),
            "control_input": control_input,
        }
        if current_indoor is None:
            return unavailable_snapshot

        with_plan = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor,
            zone_water_temps=zone_water_temps,
            heating_fractions=heating_fractions,
            weather_forecast=weather_forecast,
            hours=len(weather_forecast),
        )
        no_heating = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor,
            zone_water_temps=zone_water_temps,
            heating_fractions=[0.0] * len(weather_forecast),
            weather_forecast=weather_forecast,
            hours=len(weather_forecast),
        )

        def state_rows(
            rows: list[dict[str, Any]], source: str, fractions: list[float]
        ) -> list[dict[str, Any]]:
            return [
                {
                    **row,
                    "ts": (prices[index][0] + dt.timedelta(hours=1)).isoformat(),
                    "source": source,
                    # ``source`` describes the rule-plan scenario. Retain the
                    # actual prediction implementation separately so outcome
                    # scoring never mistakes a linear fallback for the learned
                    # comfort model.
                    "model_source": row.get("source", "unknown"),
                    "space_heating_fraction": fractions[index],
                }
                for index, row in enumerate(rows)
                if index < len(prices)
            ]

        planned_rows = state_rows(with_plan, "rules_explicit_controls", heating_fractions)
        return {
            "version": "indoor_forecast_v2",
            "forecast_status": "available",
            "current_indoor": round(current_indoor, 1),
            "forecast": planned_rows,
            "forecast_with_plan": planned_rows,
            "forecast_no_heating": state_rows(
                no_heating, "rules_counterfactual", [0.0] * len(no_heating)
            ),
            "target_schedule": targets,
            "weather_forecast": weather_forecast,
            "price_forecast": price_forecast,
            "heat_curve": heat_curve.as_dict(),
            "control_input": control_input,
        }

    async def _get_last_status(self, session: AsyncSession):
        from packages.optimizer.data_access import get_last_status

        return await get_last_status(session)

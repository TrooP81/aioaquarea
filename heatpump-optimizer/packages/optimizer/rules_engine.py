"""Core rules optimizer orchestration and data access helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.heat_curve import HeatCurveConfig
from packages.core.control_temperature import (
    ControlTemperature,
    build_forecast_observation_metadata,
    build_room_comfort_envelope,
    get_control_temperature,
)
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.models import ConsumptionRecord, ShowerEventRecord, SpaceHeatingGateRecord
from packages.core.space_heating_gate import project_gate_states, resolve_effective_gate
from packages.core.space_heating_baseline import (
    BaselineDutyProfile,
    build_baseline_duty_profile,
    load_recent_heating_evidence,
)
from packages.core.time_slots import next_hour_boundary
from packages.core.panasonic_control_state import (
    panasonic_tank_heating_available,
    panasonic_zone_heating_available,
)
from packages.core.settings_service import (
    get_effective_schedule,
    get_float_setting,
    get_heat_curve_config,
    get_int_setting,
    get_setting,
    get_space_heating_gate_config,
    get_user_tz,
    is_comfort_hour,
)
from packages.ml.thermal import thermal_model
from packages.ml.comfort_model import comfort_model
from packages.ml.forecast_quality import evaluate_live_control_gate

from .rule_mixins import DHWRulesMixin, GuardrailRulesMixin, ModeRulesMixin, PreheatRulesMixin

logger = structlog.get_logger()


def minimum_floor_protection_duties(
    *,
    basis_temperature: float | None,
    comfort_temp_min: float,
    indoor_rates: list[tuple[float, float]],
) -> list[float]:
    """Return the minimum hourly duty required to maintain the comfort floor.

    Each pair is the predicted one-hour temperature change at full heat and
    no heat.  The running temperature is projected at the minimum permitted
    value so a prior unavoidable deficit cannot make later values negative.
    """
    if basis_temperature is None:
        return [0.0] * len(indoor_rates)

    projected = float(basis_temperature)
    duties: list[float] = []
    for gain, loss in indoor_rates:
        denominator = gain - loss
        required = (comfort_temp_min - (projected + loss)) / denominator if denominator > 0 else 0.0
        duty = max(0.0, min(1.0, required))
        duties.append(duty)
        projected = max(comfort_temp_min, projected + loss + duty * denominator)
    return duties


async def _resolve_learning_mode():
    """Read observe-only mode once for a plan; errors must not admit scoring."""
    from packages.optimizer.executor_core import LearningModeState, resolve_learning_mode_state

    try:
        return await resolve_learning_mode_state()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - scoring admission must fail closed
        logger.warning("rules_learning_mode_check_failed", error_type=type(exc).__name__)
        return LearningModeState.UNKNOWN


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

    VERSION = "rules_v7"

    def __init__(self, cop_model=None):
        self._dhw_cop_model = cop_model

    @staticmethod
    def _unavailable_control_temperature() -> ControlTemperature:
        return ControlTemperature(
            value=None,
            confidence="low",
            sensor_count=0,
            sample_count=0,
            latest_reading=None,
            reason="room_control_evidence_unavailable",
        )

    @staticmethod
    def _action_timestamp(action: dict[str, Any]) -> dt.datetime | None:
        try:
            timestamp = dt.datetime.fromisoformat(str(action["ts"]))
        except (KeyError, TypeError, ValueError):
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
        return timestamp.astimezone(dt.timezone.utc)

    @classmethod
    def _zone_control_windows(
        cls,
        actions: list[dict[str, Any]],
        *,
        horizon_end: dt.datetime | None = None,
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        """Merge overlapping boost/restore pairs that own the zone target."""

        events: dict[dt.datetime, list[int]] = {}
        timestamps: list[dt.datetime] = []
        for action in actions:
            action_type = str(action.get("type", ""))
            if action_type not in {"zone_temp_boost", "zone_temp_restore"}:
                continue
            timestamp = cls._action_timestamp(action)
            if timestamp is None:
                continue
            timestamps.append(timestamp)
            counts = events.setdefault(timestamp, [0, 0])
            counts[0 if action_type == "zone_temp_boost" else 1] += 1

        depth = 0
        active_start: dt.datetime | None = None
        windows: list[tuple[dt.datetime, dt.datetime]] = []
        for timestamp in sorted(events):
            boosts, restores = events[timestamp]
            if depth == 0 and boosts:
                active_start = timestamp
            depth += boosts
            depth = max(0, depth - restores)
            if depth == 0 and active_start is not None:
                windows.append((active_start, timestamp))
                active_start = None

        if depth and active_start is not None:
            end = horizon_end or (max(timestamps) if timestamps else active_start)
            if end.tzinfo is None:
                end = end.replace(tzinfo=dt.timezone.utc)
            windows.append((active_start, max(active_start, end.astimezone(dt.timezone.utc))))

        return windows

    @classmethod
    def _is_in_zone_control_window(
        cls,
        action: dict[str, Any],
        windows: list[tuple[dt.datetime, dt.datetime]],
    ) -> bool:
        timestamp = cls._action_timestamp(action)
        return timestamp is not None and any(start <= timestamp <= end for start, end in windows)

    @classmethod
    def _normalise_actions(
        cls,
        actions: list[dict[str, Any]],
        *,
        initial_quiet_level: int | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve command ownership and duplicates while preserving plan intent.

        Zone boost windows exclusively own their Panasonic target. Multiple
        rule sources can also request different quiet levels; collapse only
        identical levels while preserving legitimate LEVEL1 -> LEVEL2 changes.
        """
        ordered = sorted(actions, key=lambda action: str(action.get("ts", "")))

        # Panasonic special status and absolute zone boosts both write the same
        # zone target. Never allow a mode transition to invalidate a frozen
        # boost/restore pair, including at either boundary of the interval.
        parsed_timestamps = [
            timestamp
            for action in ordered
            if (timestamp := cls._action_timestamp(action)) is not None
        ]
        zone_control_windows = cls._zone_control_windows(
            ordered,
            horizon_end=max(parsed_timestamps) if parsed_timestamps else None,
        )
        special_status_actions = {
            "eco_mode_on",
            "eco_mode_off",
            "normal_mode_on",
            "comfort_mode_on",
        }
        ordered = [
            action
            for action in ordered
            if str(action.get("type", "")) not in special_status_actions
            or not cls._is_in_zone_control_window(action, zone_control_windows)
        ]

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
        active_quiet_level = (
            initial_quiet_level
            if isinstance(initial_quiet_level, int)
            and not isinstance(initial_quiet_level, bool)
            and 0 <= initial_quiet_level <= 3
            else None
        )

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
        from packages.optimizer.executor_core import LearningModeState

        now = dt.datetime.now(dt.timezone.utc)
        horizon_start = next_hour_boundary(now)
        horizon_end = horizon_start + dt.timedelta(hours=24)
        learning_mode_state = await _resolve_learning_mode()
        if learning_mode_state is LearningModeState.UNKNOWN:
            logger.info("rules_planning_skipped_learning_state_unknown")
            return None
        learning_mode_active = learning_mode_state is LearningModeState.ACTIVE

        thermal_model.load_latest()

        async with get_session() as session:
            prices = await self._get_prices(session, horizon_start, horizon_end)
            weather = await self._get_weather(session, horizon_start, horizon_end)
            weather_full = await self._get_weather_full(session, horizon_start, horizon_end)
            last_status = await self._get_last_status(session)
            gate_row = (
                (
                    await session.execute(
                        select(SpaceHeatingGateRecord).where(
                            SpaceHeatingGateRecord.device_id == last_status.device_id
                        )
                    )
                ).scalar_one_or_none()
                if last_status is not None
                else None
            )
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
            baseline_rows: list[Any] = []
            baseline_truncated = False
            if last_status is not None:
                try:
                    baseline_rows, baseline_truncated = await load_recent_heating_evidence(
                        session, last_status.device_id, now
                    )
                except Exception as exc:  # noqa: BLE001 - live planning must remain available
                    logger.warning(
                        "space_heating_baseline_query_failed", error_type=type(exc).__name__
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
        current_zone_heat_min = (
            last_status.zone1_heat_min
            if last_status and last_status.zone1_heat_min is not None
            else None
        )
        current_zone_heat_max = (
            last_status.zone1_heat_max
            if last_status and last_status.zone1_heat_max is not None
            else None
        )
        special_status_supported = (
            last_status is not None
            and getattr(last_status, "special_status_supported", None) is True
        )
        current_special_status = (
            getattr(last_status, "special_status", None) if last_status is not None else None
        )
        current_quiet_level = (
            getattr(last_status, "quiet_mode", None) if last_status is not None else None
        )
        tank_heating_available = panasonic_tank_heating_available(last_status)
        zone_heating_available = panasonic_zone_heating_available(last_status)
        tank_target = (
            last_status.tank_target_temp if last_status and last_status.tank_target_temp else 52
        )

        try:
            control_temperature = await get_control_temperature(now=now)
        except Exception as exc:  # noqa: BLE001 - room evidence must never block DHW planning
            logger.warning(
                "room_control_evidence_unavailable",
                error=type(exc).__name__,
            )
            control_temperature = self._unavailable_control_temperature()
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

        quality_gate = await evaluate_live_control_gate(
            model_metrics=comfort_model.metrics,
            record_gate=comfort_model.record_forecast_quality_gate,
        )
        learned_forecast_allowed = bool(quality_gate and quality_gate.get("control_allowed"))
        actions: list[dict[str, Any]] = []

        heat_curve = await get_heat_curve_config()
        try:
            configured_baseline_mode = (
                (await get_setting("space_heating_baseline_mode")).strip().lower()
            )
        except Exception as exc:  # noqa: BLE001 - rollout settings must fail closed
            logger.warning(
                "space_heating_baseline_settings_unavailable", error_type=type(exc).__name__
            )
            configured_baseline_mode = "shadow"
        effective_baseline_mode = (
            configured_baseline_mode
            if configured_baseline_mode in {"off", "shadow", "on"}
            else "shadow"
        )
        try:
            default_baseline_fraction = float(await get_setting("space_heating_default_fraction"))
        except Exception:  # noqa: BLE001 - conservative configured fallback
            default_baseline_fraction = 0.35
        gate_config = await get_space_heating_gate_config()
        effective_gate = resolve_effective_gate(gate_row, gate_config)
        gate_projections = project_gate_states(
            effective_gate, [temperature for _, temperature in weather], gate_config
        )
        baseline_profile = build_baseline_duty_profile(
            baseline_rows,
            [
                {"timestamp": timestamp, "outdoor_temp": temperature}
                for timestamp, temperature in weather
            ],
            gate_projections,
            heat_curve.heating_off_outdoor_c,
            "UTC",
            zone_heating_available,
            default_baseline_fraction,
            truncated=baseline_truncated,
        )
        expected_baseline_fractions = [point.expected_fraction for point in baseline_profile.points]
        floor_baseline_fractions = [point.floor_fraction for point in baseline_profile.points]
        live_baseline_fractions = (
            expected_baseline_fractions if effective_baseline_mode == "on" else [0.0] * len(weather)
        )
        gate_evidence = (
            [(weather[index][0], gate_projections[index]) for index in range(len(weather))]
            if len(prices) == len(weather) == len(gate_projections)
            else None
        )
        learned_threshold = await get_float_setting("learned_schedule_threshold")
        comfort_schedule = await get_effective_schedule(learned_threshold=learned_threshold)
        tz_name = await get_user_tz()
        comfort_temp_target = await get_float_setting("comfort_temp_target")
        comfort_temp_min = await get_float_setting("comfort_temp_min")
        comfort_temp_max = await get_float_setting("comfort_temp_max")
        try:
            max_passive_change_c_per_hour = float(
                await get_float_setting("indoor_forecast_max_passive_change_c_per_hour")
            )
            if max_passive_change_c_per_hour <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            max_passive_change_c_per_hour = 0.5
        try:
            room_envelope = build_room_comfort_envelope(
                control_temperature, comfort_temp_max=comfort_temp_max
            )
        except Exception as exc:  # noqa: BLE001 - room evidence must never block DHW planning
            logger.warning(
                "room_comfort_envelope_unavailable",
                error_type=type(exc).__name__,
            )
            room_envelope = None
        room_overheat_active = bool(room_envelope and room_envelope.rooms_above_max)
        minimum_comfort_active = (
            room_overheat_active
            and current_indoor_temp is not None
            and current_indoor_temp < comfort_temp_min
        )

        if tank_heating_available:
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
        if control_temperature.is_usable and zone_heating_available and not room_overheat_active:
            actions.extend(
                self._plan_preheat(
                    prices,
                    weather,
                    horizon_start,
                    current_indoor_temp,
                    current_outdoor_temp,
                    current_water_temp,
                    heat_curve=heat_curve,
                    gate_projections=gate_projections,
                    gate_evidence=gate_evidence,
                    gate_control_enabled=True,
                    use_learned_forecast=learned_forecast_allowed,
                    baseline_heating_fractions=live_baseline_fractions,
                    max_passive_change_c_per_hour=max_passive_change_c_per_hour,
                    comfort_schedule=comfort_schedule,
                    comfort_temp_target=comfort_temp_target,
                    comfort_temp_min=comfort_temp_min,
                    comfort_temp_max=comfort_temp_max,
                    tz_name=tz_name,
                    weather_full=weather_full,
                    current_zone_target_temp=current_zone_target_temp,
                    current_zone_heat_min=current_zone_heat_min,
                    current_zone_heat_max=current_zone_heat_max,
                )
            )

        actions.extend(self._plan_peak_avoidance(prices, weather, horizon_start))

        quiet_start = await get_int_setting("quiet_mode_start")
        quiet_end = await get_int_setting("quiet_mode_end")
        actions.extend(
            self._plan_quiet_mode(
                horizon_start,
                quiet_start,
                quiet_end,
                tz_name=tz_name,
                current_level=current_quiet_level,
            )
        )

        if (
            control_temperature.is_usable
            and zone_heating_available
            and (not room_overheat_active or minimum_comfort_active)
        ):
            actions.extend(
                self._plan_indoor_guardrails(
                    prices,
                    weather,
                    horizon_start,
                    current_indoor_temp,
                    current_outdoor_temp,
                    current_water_temp,
                    comfort_schedule,
                    comfort_temp_min if room_overheat_active else comfort_temp_target,
                    comfort_temp_min,
                    heat_curve=heat_curve,
                    gate_projections=gate_projections,
                    tz_name=tz_name,
                    weather_full=weather_full,
                    current_zone_target_temp=current_zone_target_temp,
                    current_zone_heat_min=current_zone_heat_min,
                    current_zone_heat_max=current_zone_heat_max,
                    gate_control_enabled=True,
                    use_learned_forecast=learned_forecast_allowed,
                    max_passive_change_c_per_hour=max_passive_change_c_per_hour,
                    baseline_heating_fractions=live_baseline_fractions,
                    floor_heating_fractions=(
                        floor_baseline_fractions
                        if effective_baseline_mode == "on"
                        else [0.0] * len(weather)
                    ),
                )
            )

        comfort_override_pct = await get_int_setting("price_comfort_override_pct")
        eco_upgrade_pct = await get_int_setting("price_eco_upgrade_pct")
        if zone_heating_available and not room_overheat_active:
            zone_control_windows = self._zone_control_windows(actions, horizon_end=horizon_end)
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
                    special_status_supported=special_status_supported,
                    current_special_status=current_special_status,
                    zone_control_windows=zone_control_windows,
                    gate_projections=gate_projections,
                    gate_control_enabled=True,
                    max_passive_change_c_per_hour=max_passive_change_c_per_hour,
                    baseline_heating_fractions=live_baseline_fractions,
                    use_learned_forecast=learned_forecast_allowed,
                )
            )

        if not actions:
            return None

        actions = self._normalise_actions(
            actions,
            initial_quiet_level=current_quiet_level,
        )
        if last_status is not None:
            for action in actions:
                action["device_id"] = last_status.device_id
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
            max_passive_change_c_per_hour=max_passive_change_c_per_hour,
            gate_projections=gate_projections,
            quality_gate=quality_gate,
            control_temperature=control_temperature,
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
                "room_comfort": {
                    "basis_temperature": room_envelope.basis_temperature if room_envelope else None,
                    "fresh_inlier_min": room_envelope.fresh_inlier_min if room_envelope else None,
                    "fresh_inlier_max": room_envelope.fresh_inlier_max if room_envelope else None,
                    "affected_rooms": list(room_envelope.rooms_above_max) if room_envelope else [],
                },
            },
            baseline_profile=baseline_profile,
            configured_baseline_mode=configured_baseline_mode,
            effective_baseline_mode=effective_baseline_mode,
            live_baseline_fractions=live_baseline_fractions,
            learning_mode_active=learning_mode_active,
        )

        return {
            "device_id": last_status.device_id if last_status is not None else None,
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
            "space_heating_gate": {
                "state": effective_gate.state,
                "reason": effective_gate.reason_code,
                "profile_id": effective_gate.profile_id,
                "base_c": effective_gate.base_c,
                "on_threshold_c": effective_gate.on_threshold_c,
                "off_threshold_c": effective_gate.off_threshold_c,
                "last_raw_outdoor_c": effective_gate.last_raw_outdoor_c,
                "fingerprint_matches": effective_gate.fingerprint_matches,
                "projected_states": [evidence.state for evidence in gate_projections],
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
            logger.warning("failed to estimate average daily consumption", exc_info=True)

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
        max_passive_change_c_per_hour: float = 0.5,
        gate_projections: list | None = None,
        quality_gate: dict[str, object] | None = None,
        control_temperature: ControlTemperature | None = None,
        control_input: dict[str, Any] | None = None,
        baseline_profile: BaselineDutyProfile | None = None,
        configured_baseline_mode: str = "shadow",
        effective_baseline_mode: str = "shadow",
        live_baseline_fractions: list[float] | None = None,
        learning_mode_active: bool = False,
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
        baseline_points = baseline_profile.points if baseline_profile is not None else ()
        live_fractions = live_baseline_fractions or [0.0] * len(prices)
        heating_fractions: list[float] = []
        baseline_fractions: list[float] = []
        baseline_sources: list[str] = []
        explicit_heat_fractions: list[float] = []
        action_index = 0
        mode_offset = 0.0
        boost_offset = 0.0
        explicit_heat_fraction = 0.0
        manual_supply_override: float | None = None

        for hour, (slot_ts, price) in enumerate(prices):
            target_ts = slot_ts + dt.timedelta(hours=1)
            fallback_temperature = (
                weather[hour][1] if hour < len(weather) and weather[hour][1] is not None else 5.0
            )
            weather_slot = weather_for_slot(target_ts, float(fallback_temperature))
            weather_forecast.append(
                {
                    "ts": target_ts.isoformat(),
                    "hour": target_ts.hour,
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
            gate_state = (
                gate_projections[hour].state
                if gate_projections is not None and hour < len(gate_projections)
                else None
            )
            baseline_fraction = live_fractions[hour] if hour < len(live_fractions) else 0.0
            baseline_point = baseline_points[hour] if hour < len(baseline_points) else None
            baseline_fractions.append(baseline_fraction)
            baseline_sources.append(baseline_point.source if baseline_point is not None else "none")
            explicit_heat_fractions.append(explicit_heat_fraction)
            heating_fractions.append(
                (1.0 if explicit_heat_fraction else baseline_fraction)
                if gate_state == "ALLOWED"
                else 0.0
            )
            target = (
                comfort_temp_target
                if is_comfort_hour(comfort_schedule, target_ts, tz_name=tz_name)
                else comfort_temp_min
            )
            targets.append(
                {
                    "hour": hour + 1,
                    "ts": target_ts.isoformat(),
                    "target": round(target, 1),
                    "comfort_hour": is_comfort_hour(comfort_schedule, target_ts, tz_name=tz_name),
                }
            )

        control_input = control_input or {
            "available": current_indoor is not None,
            "reason": "missing_control_temperature_provenance",
        }
        unavailable_snapshot = {
            "version": "indoor_forecast_v5",
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

        learned_forecast_allowed = bool(quality_gate and quality_gate.get("control_allowed"))
        with_plan = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor,
            zone_water_temps=zone_water_temps,
            heating_fractions=heating_fractions,
            weather_forecast=weather_forecast,
            hours=len(weather_forecast),
            use_learned_forecast=learned_forecast_allowed,
            max_passive_change_c_per_hour=max_passive_change_c_per_hour,
        )
        no_heating = thermal_model.predict_indoor_controlled_curve(
            current_indoor=current_indoor,
            zone_water_temps=zone_water_temps,
            heating_fractions=[0.0] * len(weather_forecast),
            weather_forecast=weather_forecast,
            hours=len(weather_forecast),
            use_learned_forecast=learned_forecast_allowed,
            max_passive_change_c_per_hour=max_passive_change_c_per_hour,
        )
        comparison_baseline_fractions = [point.expected_fraction for point in baseline_points] or [
            0.0
        ] * len(weather_forecast)
        try:
            comparison_baseline = thermal_model.predict_indoor_controlled_curve(
                current_indoor=current_indoor,
                zone_water_temps=zone_water_temps,
                heating_fractions=comparison_baseline_fractions,
                weather_forecast=weather_forecast,
                hours=len(weather_forecast),
                use_learned_forecast=learned_forecast_allowed,
                max_passive_change_c_per_hour=max_passive_change_c_per_hour,
            )
        except Exception:  # noqa: BLE001 - comparison must never block a plan
            logger.warning("space_heating_baseline_comparison_unavailable", exc_info=True)
            comparison_baseline = []

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
                    "baseline_heating_fraction": baseline_fractions[index]
                    if index < len(baseline_fractions)
                    else 0.0,
                    "baseline_heating_source": baseline_sources[index]
                    if index < len(baseline_sources)
                    else "none",
                    "space_heating_source": (
                        "explicit_override"
                        if fractions[index] == 1.0
                        and index < len(explicit_heat_fractions)
                        and explicit_heat_fractions[index]
                        else "baseline"
                        if index < len(baseline_fractions) and baseline_fractions[index] > 0
                        else "none"
                    ),
                }
                for index, row in enumerate(rows)
                if index < len(prices)
            ]

        planned_rows = state_rows(with_plan, "rules_explicit_controls", heating_fractions)
        snapshot = {
            "version": "indoor_forecast_v5",
            "forecast_status": "available" if learned_forecast_allowed else "fallback",
            "forecast_quality": quality_gate
            or {"status": "fallback", "control_allowed": False, "reason": "quality_gate_missing"},
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
            "space_heating_baseline": {
                "configured_mode": configured_baseline_mode,
                "effective_mode": effective_baseline_mode,
                "live_baseline_applied": effective_baseline_mode == "on",
                "lookback_days": 14,
                "source": "history"
                if any(source == "history" for source in baseline_sources)
                else "default"
                if any(source == "default" for source in baseline_sources)
                else "none",
                "fallback_reason": None,
                "history_rows": baseline_profile.history_rows if baseline_profile else 0,
                "accepted_hours": baseline_profile.accepted_hours if baseline_profile else 0,
                "distinct_days": 0,
                "truncated": baseline_profile.truncated if baseline_profile else False,
            },
            "baseline_evaluation": {
                "learning_mode": learning_mode_active,
                "eligible": learning_mode_active and effective_baseline_mode == "shadow",
            },
            **(
                build_forecast_observation_metadata(
                    control_temperature,
                    horizon_start=horizon_start,
                )
                if control_temperature is not None
                else {
                    "scoring_schema": "forecast_outcome_v2_persistence_origin",
                    "issue_timestamp": horizon_start.isoformat(),
                    "sensor_basis": {
                        "label": control_input.get("reference_sensor_label")
                        or "Median indoor temperature",
                        "kind": "reference"
                        if control_input.get("reference_sensor_id")
                        else "median",
                    },
                    "observed_history": [
                        {
                            "hour": 0,
                            "ts": horizon_start.isoformat(),
                            "temperature": round(current_indoor, 1),
                        }
                    ],
                }
            ),
        }
        if comparison_baseline:
            snapshot["forecast_with_plan_baseline"] = state_rows(
                comparison_baseline, "rules_baseline_candidate", comparison_baseline_fractions
            )
            snapshot["forecast_with_plan_zero_baseline"] = state_rows(
                no_heating, "rules_zero_baseline", [0.0] * len(no_heating)
            )
        if not learned_forecast_allowed:

            def shadow_rows(
                rows: list[dict[str, Any]], source: str, fractions: list[float]
            ) -> list[dict[str, Any]]:
                result: list[dict[str, Any]] = []
                for index, row in enumerate(rows[:24]):
                    model_source = row.get("source")
                    segment_kind = row.get("segment_kind")
                    if (
                        index >= len(prices)
                        or model_source
                        not in {
                            "comfort_model_controlled",
                            "comfort_model_physics_continuation",
                            "comfort_model_passive_direct",
                            "comfort_model_passive_physics_continuation",
                        }
                        or segment_kind not in {"direct", "interpolated", "physics_continuation"}
                    ):
                        return []
                    result.append(
                        {
                            "hour": int(row["hour"]),
                            "ts": (prices[index][0] + dt.timedelta(hours=1)).isoformat(),
                            "predicted_indoor_temp": float(row["predicted_indoor_temp"]),
                            "source": source,
                            "model_source": model_source,
                            "segment_kind": segment_kind,
                            "space_heating_fraction": max(0.0, min(1.0, float(fractions[index]))),
                        }
                    )
                return result

            try:
                shadow_with_plan = thermal_model.predict_indoor_candidate_curve(
                    current_indoor=current_indoor,
                    zone_water_temps=zone_water_temps,
                    heating_fractions=heating_fractions,
                    weather_forecast=weather_forecast,
                    hours=min(24, len(weather_forecast)),
                    max_passive_change_c_per_hour=max_passive_change_c_per_hour,
                )
                shadow_no_heating = thermal_model.predict_indoor_candidate_curve(
                    current_indoor=current_indoor,
                    zone_water_temps=zone_water_temps,
                    heating_fractions=[0.0] * len(weather_forecast),
                    weather_forecast=weather_forecast,
                    hours=min(24, len(weather_forecast)),
                    max_passive_change_c_per_hour=max_passive_change_c_per_hour,
                )
                if shadow_with_plan is not None:
                    snapshot["shadow_forecast_with_plan"] = shadow_rows(
                        shadow_with_plan, "rules_shadow_candidate", heating_fractions
                    )
                if shadow_no_heating is not None:
                    snapshot["shadow_forecast_no_heating"] = shadow_rows(
                        shadow_no_heating, "rules_shadow_no_heating", [0.0] * len(weather_forecast)
                    )
            except Exception:  # noqa: BLE001 - shadow evidence must never affect a live plan
                logger.warning("rules_shadow_forecast_unavailable", exc_info=True)
        return snapshot

    async def _get_last_status(self, session: AsyncSession):
        from packages.optimizer.data_access import get_last_status

        return await get_last_status(session)

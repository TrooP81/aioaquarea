"""Controller heat-curve settings shared by planners and comfort advice."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Mapping


HEAT_CURVE_SETTING_KEYS = (
    "heat_curve_outdoor_cold_c",
    "heat_curve_supply_cold_c",
    "heat_curve_outdoor_warm_c",
    "heat_curve_supply_warm_c",
    "heat_curve_heating_off_outdoor_c",
    "heat_curve_delta_t_c",
)
VERIFICATION_MIN_HOURS = 24
VERIFICATION_MIN_INDOOR_SAMPLES = 6
VERIFICATION_MIN_HEATING_CONDITION_SAMPLES = 3
VERIFICATION_MEANINGFUL_EFFECT_C = 0.2

# Panasonic reports ``-5`` for the Zone 1 target while a weather-compensated
# curve is in charge.  That is a controller sentinel, not a physically
# meaningful water temperature.  Keep this normalization in the shared heat
# curve module so training, forecasting, and presentation agree on it.
MIN_VALID_ZONE_TARGET_C = 15.0
MAX_VALID_ZONE_TARGET_C = 65.0


@dataclass(frozen=True)
class HeatCurveConfig:
    """Panasonic outdoor-temperature to supply-water-temperature curve."""

    outdoor_cold_c: float = 5.0
    supply_cold_c: float = 47.0
    outdoor_warm_c: float = 15.0
    supply_warm_c: float = 23.0
    heating_off_outdoor_c: float = 13.0
    delta_t_c: float = 4.0

    @classmethod
    def from_settings(cls, values: Mapping[str, str]) -> "HeatCurveConfig":
        defaults = cls()

        def value(key: str, default: float) -> float:
            try:
                return float(values.get(key, default))
            except (TypeError, ValueError):
                return default

        config = cls(
            outdoor_cold_c=value("heat_curve_outdoor_cold_c", defaults.outdoor_cold_c),
            supply_cold_c=value("heat_curve_supply_cold_c", defaults.supply_cold_c),
            outdoor_warm_c=value("heat_curve_outdoor_warm_c", defaults.outdoor_warm_c),
            supply_warm_c=value("heat_curve_supply_warm_c", defaults.supply_warm_c),
            heating_off_outdoor_c=value(
                "heat_curve_heating_off_outdoor_c", defaults.heating_off_outdoor_c
            ),
            delta_t_c=value("heat_curve_delta_t_c", defaults.delta_t_c),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not -25 <= self.outdoor_cold_c < self.outdoor_warm_c <= 30:
            raise ValueError(
                "Cold outdoor point must be lower than warm outdoor point (-25 to 30°C)"
            )
        if not 15 <= self.supply_warm_c <= self.supply_cold_c <= 65:
            raise ValueError(
                "Supply-water points must be 15–65°C and decrease as outdoor temperature rises"
            )
        if not -10 <= self.heating_off_outdoor_c <= 30:
            raise ValueError("Heating-off outdoor temperature must be between -10 and 30°C")
        if not 1 <= self.delta_t_c <= 15:
            raise ValueError("Heat-pump ΔT must be between 1 and 15°C")

    def supply_temperature(self, outdoor_c: float) -> float:
        """Return the configured supply target, clamped outside the two points."""
        if outdoor_c <= self.outdoor_cold_c:
            return self.supply_cold_c
        if outdoor_c >= self.outdoor_warm_c:
            return self.supply_warm_c
        ratio = (outdoor_c - self.outdoor_cold_c) / (self.outdoor_warm_c - self.outdoor_cold_c)
        return self.supply_cold_c + ratio * (self.supply_warm_c - self.supply_cold_c)

    def planned_supply_temperature(self, outdoor_c: float) -> float:
        """Return supply target, or outdoor temperature when controller heating is off."""
        if outdoor_c >= self.heating_off_outdoor_c:
            return outdoor_c
        return self.supply_temperature(outdoor_c)

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def effective_zone_target_temperature(
    reported_target_c: float | None,
    outdoor_temp_c: float | None,
    *,
    config: HeatCurveConfig | None = None,
    fallback_c: float | None = None,
) -> float | None:
    """Return a physical Zone 1 target suitable for models and charts.

    The live controller uses a negative sentinel (commonly ``-5°C``) when the
    weather-compensated curve owns the target.  Prefer a valid target reported
    by the controller; otherwise derive the curve target for the current
    outdoor temperature.  When the controller is above the heating-off
    threshold this intentionally returns the outdoor temperature, matching
    :meth:`HeatCurveConfig.planned_supply_temperature` and signalling that the
    curve is inactive instead of inventing a heat demand.
    """
    try:
        target = float(reported_target_c)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        target = None
    if target is not None and MIN_VALID_ZONE_TARGET_C <= target <= MAX_VALID_ZONE_TARGET_C:
        return target

    if config is not None:
        try:
            return round(config.planned_supply_temperature(float(outdoor_temp_c)), 2)
        except (TypeError, ValueError):
            pass

    try:
        fallback = float(fallback_c)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fallback = None
    if fallback is not None and MIN_VALID_ZONE_TARGET_C <= fallback <= MAX_VALID_ZONE_TARGET_C:
        return fallback
    return None


def heat_curve_advice(
    config: HeatCurveConfig,
    indoor_temp: float | None,
    comfort_target: float,
    outdoor_temp: float | None,
) -> dict:
    """Return a bounded, manual-change recommendation for the heat curve.

    The advice deliberately changes at most 2°C per review cycle.  It never
    sends a command to the heat pump; the user changes the controller first and
    then records the resulting values in Settings.
    """
    suggested = config.as_dict()
    reasons: list[str] = []
    status = "insufficient_data"
    indoor_error: float | None = None

    # A high indoor temperature on a warm day is often caused by solar gain or
    # ventilation rather than the heat curve.  Never turn that into a manual
    # curve recommendation: the controller is already preventing space heat.
    if outdoor_temp is not None and outdoor_temp >= config.heating_off_outdoor_c:
        reasons.append(
            f"Space heating is off because outdoor temperature ({outdoor_temp:.1f}°C) is at or above the {config.heating_off_outdoor_c:.1f}°C cutoff."
        )
        reasons.append(
            "This comfort deviation is not currently heat-pump controllable; wait for cooler weather before changing the curve."
        )
        reasons.append(
            f"Keep ΔT at {config.delta_t_c:.1f}°C unless a hydraulic installer recommends a change; it is not a direct room-temperature target."
        )
        return {
            "status": "outside_heating_season",
            "indoor_error_c": round(indoor_temp - comfort_target, 1)
            if indoor_temp is not None
            else None,
            "current": config.as_dict(),
            "suggested": None,
            "reasons": reasons,
            "manual_only": True,
            "controllability": "not_controllable_by_heat_curve",
        }

    if indoor_temp is not None:
        indoor_error = round(indoor_temp - comfort_target, 1)
        if indoor_error > 0.5:
            step = min(2.0, max(1.0, math.ceil(indoor_error / 2.0)))
            suggested["supply_cold_c"] = round(max(15.0, config.supply_cold_c - step), 1)
            suggested["supply_warm_c"] = round(max(15.0, config.supply_warm_c - step), 1)
            suggested["heating_off_outdoor_c"] = round(
                max(-10.0, config.heating_off_outdoor_c - 1.0), 1
            )
            status = "too_warm"
            reasons.append(
                f"Indoor temperature is {indoor_error:.1f}°C above the {comfort_target:.1f}°C comfort target."
            )
            reasons.append(
                f"Try a {step:.0f}°C lower supply-water curve for one review cycle before making another change."
            )
        elif indoor_error < -0.5:
            step = min(2.0, max(1.0, math.ceil(abs(indoor_error) / 2.0)))
            suggested["supply_cold_c"] = round(min(65.0, config.supply_cold_c + step), 1)
            suggested["supply_warm_c"] = round(min(65.0, config.supply_warm_c + step), 1)
            suggested["heating_off_outdoor_c"] = round(
                min(30.0, config.heating_off_outdoor_c + 1.0), 1
            )
            status = "too_cold"
            reasons.append(
                f"Indoor temperature is {abs(indoor_error):.1f}°C below the {comfort_target:.1f}°C comfort target."
            )
            reasons.append(
                f"Try a {step:.0f}°C higher supply-water curve for one review cycle before making another change."
            )
        else:
            status = "on_target"
            reasons.append("Indoor temperature is already within ±0.5°C of the comfort target.")
    else:
        reasons.append("No recent indoor sensor reading is available yet.")

    reasons.append(
        f"Keep ΔT at {config.delta_t_c:.1f}°C unless a hydraulic installer recommends a change; it is not a direct room-temperature target."
    )

    return {
        "status": status,
        "indoor_error_c": indoor_error,
        "current": config.as_dict(),
        "suggested": suggested,
        "reasons": reasons,
        "manual_only": True,
        "controllability": "heat_curve_effective",
    }


def start_heat_curve_verification(
    *,
    started_at: str,
    previous_curve: HeatCurveConfig | None,
    applied_curve: HeatCurveConfig,
    baseline_indoor_temp: float | None,
    baseline_outdoor_temp: float | None,
    comfort_target: float,
    source: str = "settings_save",
) -> dict[str, Any]:
    """Create the persisted evidence window for one manual curve adjustment."""
    return {
        "status": "pending",
        "source": source,
        "started_at": started_at,
        "previous_curve": previous_curve.as_dict() if previous_curve else None,
        "applied_curve": applied_curve.as_dict(),
        "baseline_indoor_temp_c": baseline_indoor_temp,
        "baseline_outdoor_temp_c": baseline_outdoor_temp,
        "comfort_target_c": comfort_target,
        "minimum_hours": VERIFICATION_MIN_HOURS,
        "minimum_indoor_samples": VERIFICATION_MIN_INDOOR_SAMPLES,
        "minimum_heating_condition_samples": VERIFICATION_MIN_HEATING_CONDITION_SAMPLES,
    }


def evaluate_heat_curve_verification(
    state: Mapping[str, Any] | None,
    *,
    elapsed_hours: float,
    indoor_samples: list[float],
    heating_condition_samples: int,
    current_comfort_target: float,
) -> dict[str, Any]:
    """Return whether there is enough observed evidence for another change.

    A new recommendation stays locked while this window is pending.  The
    required outdoor-condition samples ensure the controller's curve had a
    chance to influence the room temperature, instead of interpreting a warm
    day with ``Värme AV`` as evidence about the curve.
    """
    if not state:
        return {
            "status": "not_started",
            "recommendation_available": True,
            "summary": "No heat-curve verification is active yet.",
            "reasons": [],
        }

    if state.get("status") == "verified":
        completed = dict(state)
        completed["recommendation_available"] = True
        return completed

    minimum_hours = int(state.get("minimum_hours") or VERIFICATION_MIN_HOURS)
    minimum_indoor = int(state.get("minimum_indoor_samples") or VERIFICATION_MIN_INDOOR_SAMPLES)
    minimum_heating = int(
        state.get("minimum_heating_condition_samples") or VERIFICATION_MIN_HEATING_CONDITION_SAMPLES
    )
    samples = [float(value) for value in indoor_samples]
    reasons: list[str] = []
    if elapsed_hours < minimum_hours:
        reasons.append(
            f"Waiting for {minimum_hours - elapsed_hours:.1f} more hours of observation."
        )
    if len(samples) < minimum_indoor:
        reasons.append(
            f"Waiting for {minimum_indoor - len(samples)} more indoor-temperature samples."
        )
    if heating_condition_samples < minimum_heating:
        reasons.append(
            "Waiting for cooler outdoor readings below the controller heating-off threshold."
        )

    progress = {
        "status": "pending",
        "recommendation_available": False,
        "started_at": state.get("started_at"),
        "elapsed_hours": round(max(0.0, elapsed_hours), 1),
        "minimum_hours": minimum_hours,
        "indoor_sample_count": len(samples),
        "minimum_indoor_samples": minimum_indoor,
        "heating_condition_samples": heating_condition_samples,
        "minimum_heating_condition_samples": minimum_heating,
        "baseline_indoor_temp_c": state.get("baseline_indoor_temp_c"),
        "reasons": reasons,
    }
    if reasons:
        progress["summary"] = (
            "Measuring the effect of the latest controller change before another recommendation."
        )
        return progress

    observed_indoor = sum(samples) / len(samples)
    target = float(state.get("comfort_target_c") or current_comfort_target)
    baseline = state.get("baseline_indoor_temp_c")
    try:
        baseline_error = float(baseline) - target
    except (TypeError, ValueError):
        baseline_error = None
    observed_error = observed_indoor - target
    improvement = abs(baseline_error) - abs(observed_error) if baseline_error is not None else None
    standard_error = None
    if len(samples) >= 2:
        # This is a consistency score, not a clinical significance claim: the
        # baseline is a pre-change snapshot, so we expose evidence strength
        # instead of overstating causal certainty.
        standard_error = statistics.pstdev(samples) / math.sqrt(len(samples))
    evidence_ratio = (
        abs(improvement) / max(0.1, standard_error or 0.0) if improvement is not None else None
    )
    if improvement is None:
        summary = "Verification collected enough new observations; the next recommendation is now available."
        decision = "inconclusive"
    elif improvement >= VERIFICATION_MEANINGFUL_EFFECT_C and (evidence_ratio or 0.0) >= 1.0:
        summary = "Observed indoor temperature moved closer to the comfort target during the verification window."
        decision = "accepted"
    elif improvement <= -VERIFICATION_MEANINGFUL_EFFECT_C and (evidence_ratio or 0.0) >= 1.0:
        summary = "Observed indoor temperature moved farther from the comfort target during the verification window."
        decision = "rejected"
    else:
        summary = "The verification window is complete, but the observed effect is too small or variable to support another curve change."
        decision = "inconclusive"

    evidence = "low"
    if evidence_ratio is not None:
        evidence = "high" if evidence_ratio >= 2.0 else "medium" if evidence_ratio >= 1.0 else "low"

    completed = dict(state)
    completed.update(
        {
            **progress,
            "status": "verified",
            "recommendation_available": True,
            "observed_mean_indoor_temp_c": round(observed_indoor, 2),
            "baseline_error_c": round(baseline_error, 2) if baseline_error is not None else None,
            "observed_error_c": round(observed_error, 2),
            "comfort_improvement_c": round(improvement, 2) if improvement is not None else None,
            "effect_standard_error_c": round(standard_error, 3)
            if standard_error is not None
            else None,
            "effect_evidence": evidence,
            "verification_decision": decision,
            "summary": summary,
            "reasons": [],
        }
    )
    return completed

"""Explain forecast comfort gaps without issuing heat-pump commands.

The optimizer may be prevented from heating by the configured Panasonic heat
curve.  A forecast still needs to tell the user that a target will be missed,
why, and what *manual* prerequisite would make a later trial possible.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from packages.core.heat_curve import HeatCurveConfig


COMFORT_MISS_MARGIN_C = 0.3


def build_comfort_assessment(
    *,
    forecast: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    weather: Sequence[Mapping[str, Any]],
    planned_actions: Sequence[Mapping[str, Any]],
    heat_curve: HeatCurveConfig,
    forecast_status: str = "available",
) -> dict[str, Any]:
    """Summarise target misses and provide bounded, manual-only advice.

    This intentionally does not estimate a temperature gain when the system
    lacks verified active-heating evidence.  Presenting a precise gain in that
    situation would be less honest than saying that a short, verified manual
    trial is required.
    """
    if forecast_status != "available":
        return {
            "state": "unavailable",
            "summary": "A fresh trusted indoor observation is required before comfort risk can be assessed.",
            "misses": [],
            "recommendations": [],
        }

    misses: list[dict[str, Any]] = []
    for index, (point, target_row) in enumerate(zip(forecast, targets)):
        predicted = point.get("predicted_indoor_temp")
        target = target_row.get("target")
        if predicted is None or target is None:
            continue
        try:
            deficit = float(target) - float(predicted)
        except (TypeError, ValueError):
            continue
        if deficit < COMFORT_MISS_MARGIN_C:
            continue
        weather_row = weather[index] if index < len(weather) else {}
        outdoor = weather_row.get("outdoor_temp") if isinstance(weather_row, Mapping) else None
        try:
            outdoor_c = float(outdoor) if outdoor is not None else None
        except (TypeError, ValueError):
            outdoor_c = None
        misses.append(
            {
                "hour": point.get("hour", index + 1),
                "ts": point.get("ts"),
                "predicted_c": round(float(predicted), 1),
                "target_c": round(float(target), 1),
                "shortfall_c": round(deficit, 1),
                "comfort_hour": bool(target_row.get("comfort_hour")),
                "outdoor_temp_c": round(outdoor_c, 1) if outdoor_c is not None else None,
                "space_heating_fraction": point.get("space_heating_fraction"),
                "model_source": point.get("model_source") or point.get("source"),
                "prediction_interval_status": point.get("prediction_interval_status"),
            }
        )

    if not misses:
        return {
            "state": "on_target",
            "summary": "The active forecast remains within the configured temperature targets.",
            "misses": [],
            "recommendations": [],
        }

    comfort_misses = [miss for miss in misses if miss["comfort_hour"]]
    relevant = comfort_misses or misses
    worst = max(relevant, key=lambda item: item["shortfall_c"])
    blocked = [
        miss
        for miss in relevant
        if miss["outdoor_temp_c"] is not None
        and float(miss["outdoor_temp_c"]) >= heat_curve.heating_off_outdoor_c
        and not float(miss.get("space_heating_fraction") or 0.0)
    ]
    miss_hours = {int(miss["hour"]) for miss in relevant if miss.get("hour") is not None}
    has_mode_only_action = any(
        str(action.get("action_type")) in {"normal_mode_on", "eco_mode_on", "comfort_mode_on"}
        and int(action.get("hour", -1000)) in miss_hours
        for action in planned_actions
    )

    recommendations: list[dict[str, Any]] = []
    if blocked:
        required_cutoff = min(
            30.0,
            max(float(miss["outdoor_temp_c"]) for miss in blocked) + 0.5,
        )
        recommendations.append(
            {
                "kind": "manual_heat_curve_cutoff_trial",
                "title": "Allow space heating for the affected window",
                "manual_only": True,
                "setting_key": "heat_curve_heating_off_outdoor_c",
                "current_value_c": round(heat_curve.heating_off_outdoor_c, 1),
                "minimum_candidate_value_c": round(required_cutoff, 1),
                "expected_effect": "unknown_until_verified",
                "confidence": "low",
                "verification_required": True,
                "summary": (
                    "The current Värme AV cutoff blocks room heating during the forecast miss. "
                    "Use a bounded manual trial only, then verify the measured outcome before another adjustment."
                ),
            }
        )
        controllability = "blocked_by_heating_off_cutoff"
        summary = (
            f"Comfort target is forecast to be missed by up to {worst['shortfall_c']:.1f}°C. "
            "Space heating is blocked by the configured Värme AV cutoff."
        )
    elif any(float(miss.get("space_heating_fraction") or 0.0) > 0 for miss in relevant):
        controllability = "planned_heat_insufficient"
        summary = (
            f"Comfort target is forecast to be missed by up to {worst['shortfall_c']:.1f}°C despite planned space heating. "
            "The system should re-plan rather than change controller settings automatically."
        )
    elif has_mode_only_action:
        controllability = "mode_only_no_space_heat"
        summary = (
            f"Comfort target is forecast to be missed by up to {worst['shortfall_c']:.1f}°C. "
            "A mode change is scheduled, but it does not request space heating."
        )
    else:
        controllability = "no_space_heat_planned"
        summary = f"Comfort target is forecast to be missed by up to {worst['shortfall_c']:.1f}°C and no space heat is planned."

    return {
        "state": "at_risk",
        "summary": summary,
        "controllability": {
            "status": controllability,
            "cutoff_c": round(heat_curve.heating_off_outdoor_c, 1),
        },
        "first_miss": relevant[0],
        "worst_miss": worst,
        "misses": relevant,
        "recommendations": recommendations,
    }

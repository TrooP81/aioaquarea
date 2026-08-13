"""Safety-first measurement experiments for manual heat-curve changes.

The module intentionally creates no device action. It only exposes a bounded
manual-review card; a controller change must still be made on the Panasonic
panel and the existing verification loop remains the sole evaluator.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select

from packages.core.database import get_session
from packages.core.heat_curve import HeatCurveConfig
from packages.core.models import DeviceStatusRecord
from packages.core.settings_service import (
    get_all_settings,
    get_bool_setting,
    get_float_setting,
)


def assess_manual_trial_conditions(
    config: HeatCurveConfig,
    status: Any | None,
) -> dict[str, object]:
    """Decide whether a *manual* curve trial can be proposed safely.

    A manual trial is still never a device command.  This guard only prevents
    the UI from presenting a change as measurable when the controller is off
    for warm weather, producing domestic hot water, cooling, or defrosting.
    """

    if status is None or getattr(status, "outdoor_temp", None) is None:
        return {
            "ready": False,
            "reason": "waiting_for_current_outdoor_temperature",
            "outdoor_temp_c": None,
            "heating_off_outdoor_c": config.heating_off_outdoor_c,
        }

    outdoor_temp = float(status.outdoor_temp)
    if outdoor_temp >= config.heating_off_outdoor_c:
        return {
            "ready": False,
            "reason": "above_heating_off_threshold",
            "outdoor_temp_c": round(outdoor_temp, 1),
            "heating_off_outdoor_c": config.heating_off_outdoor_c,
        }

    evidence = getattr(status, "space_heating_evidence", None)
    if getattr(status, "defrost_active", False) or evidence == "defrost":
        reason = "defrost_active"
    elif (
        evidence == "domestic_hot_water"
        or getattr(status, "device_action", None) == "HEATING_WATER"
    ):
        reason = "domestic_hot_water_active"
    elif evidence == "cooling" or getattr(status, "device_action", None) == "COOLING":
        reason = "cooling_active"
    else:
        reason = None
    if reason:
        return {
            "ready": False,
            "reason": reason,
            "outdoor_temp_c": round(outdoor_temp, 1),
            "heating_off_outdoor_c": config.heating_off_outdoor_c,
        }

    return {
        "ready": True,
        "reason": "heating_conditions_available",
        "outdoor_temp_c": round(outdoor_temp, 1),
        "heating_off_outdoor_c": config.heating_off_outdoor_c,
        "space_heating_confirmed": bool(getattr(status, "space_heating_active", False)),
    }


async def get_outcome_experiment_status() -> dict[str, object]:
    enabled = await get_bool_setting("outcome_experiments_enabled")
    max_step_c = await get_float_setting("outcome_experiment_max_curve_step_c")
    values = await get_all_settings()
    config = HeatCurveConfig.from_settings(values)
    async with get_session() as session:
        status = (
            await session.execute(
                select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
            )
        ).scalar_one_or_none()
    conditions = assess_manual_trial_conditions(config, status)
    safe_step_c = round(min(1.0, max(0.1, max_step_c)), 1)
    state = (
        "manual_review_ready"
        if enabled and conditions["ready"]
        else "waiting_for_heating_conditions"
        if enabled
        else "disabled"
    )
    return {
        "enabled": enabled,
        "status": state,
        "mode": "manual_review_only",
        "maximum_curve_step_c": safe_step_c,
        "conditions": conditions,
        "guardrails": [
            "No heat-pump command is created by this feature.",
            "Change at most one curve setting manually, then save the recorded value.",
            "The next recommendation remains locked until cool-weather verification is complete.",
            "Suggestions are withheld outside confirmed heating conditions.",
        ],
    }

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
from packages.core.models import DeviceStatusRecord, SpaceHeatingGateRecord
from packages.core.settings_service import (
    get_all_settings,
    get_bool_setting,
    get_float_setting,
    get_space_heating_gate_config,
)
from packages.core.space_heating_gate import (
    HeatingGateConfig,
    SpaceHeatingGateState,
    resolve_effective_gate,
)


def assess_manual_trial_conditions(
    config: HeatCurveConfig,
    status: Any | None,
    gate_row: Any | None = None,
    gate_config: HeatingGateConfig | None = None,
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
    effective_gate = resolve_effective_gate(gate_row, gate_config or HeatingGateConfig())
    if effective_gate.state is not SpaceHeatingGateState.ALLOWED:
        return {
            "ready": False,
            "reason": f"space_heating_gate_{effective_gate.state.lower()}",
            "outdoor_temp_c": round(outdoor_temp, 1),
            "gate_reason": effective_gate.reason_code,
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
    gate_config = await get_space_heating_gate_config()
    async with get_session() as session:
        status = (
            await session.execute(
                select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
            )
        ).scalar_one_or_none()
        gate_row = (
            (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == status.device_id
                    )
                )
            ).scalar_one_or_none()
            if status is not None
            else None
        )
    conditions = assess_manual_trial_conditions(config, status, gate_row, gate_config)
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

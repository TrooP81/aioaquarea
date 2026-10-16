"""Safety-first measurement experiments for manual heat-curve changes.

The module intentionally creates no device action. It only exposes a bounded
manual-review card; a controller change must still be made on the Panasonic
panel and the existing verification loop remains the sole evaluator.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import desc, select

from packages.core.database import get_session
from packages.core.device_data_quality import (
    device_data_quality_from_inputs,
    device_status_clock_issue,
    observation_clock_issue,
)
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
    *,
    now: dt.datetime | None = None,
    max_age_seconds: float = 900,
) -> dict[str, object]:
    """Decide whether a *manual* curve trial can be proposed safely.

    A manual trial is still never a device command.  This guard only prevents
    the UI from presenting a change as measurable when the controller is off
    for warm weather, producing domestic hot water, cooling, or defrosting.
    """

    now = now or dt.datetime.now(dt.timezone.utc)
    outdoor = getattr(status, "outdoor_temp", None)
    if (
        isinstance(outdoor, bool)
        or not isinstance(outdoor, (int, float))
        or not math.isfinite(outdoor)
    ):
        return {
            "ready": False,
            "reason": "waiting_for_current_outdoor_temperature",
            "outdoor_temp_c": None,
            "heating_off_outdoor_c": config.heating_off_outdoor_c,
        }

    clock_issue = device_status_clock_issue(status, as_of=now, max_age_seconds=max_age_seconds)
    if clock_issue:
        return {"ready": False, "reason": clock_issue, "outdoor_temp_c": None}
    gate_issue = (
        observation_clock_issue(
            as_of=now,
            timestamp=gate_row.source_status_ts,
            captured_at=gate_row.evaluated_at,
            max_age_seconds=max_age_seconds,
        )
        if getattr(gate_row, "source_status_ts", None) is not None
        and getattr(gate_row, "evaluated_at", None) is not None
        else "device_status_timing_unknown"
    )
    if gate_issue:
        return {
            "ready": False,
            "reason": "space_heating_gate_" + gate_issue.removeprefix("device_status_"),
            "outdoor_temp_c": None,
        }
    outdoor_temp = float(outdoor)
    effective_gate = resolve_effective_gate(gate_row, gate_config or HeatingGateConfig())
    if effective_gate.state is not SpaceHeatingGateState.ALLOWED:
        return {
            "ready": False,
            "reason": f"space_heating_gate_{effective_gate.state.lower()}",
            "outdoor_temp_c": round(outdoor_temp, 1),
            "gate_reason": effective_gate.reason_code,
        }
    if effective_gate.last_raw_outdoor_c is None:
        return {
            "ready": False,
            "reason": "space_heating_gate_temperature_invalid",
            "outdoor_temp_c": None,
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
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session(read_only=True) as session:
        status = (
            await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts <= now)
                .order_by(desc(DeviceStatusRecord.ts))
                .limit(1)
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
        quality = device_data_quality_from_inputs(now=now, values=values, status=status)
        conditions = assess_manual_trial_conditions(
            config,
            status,
            gate_row,
            gate_config,
            now=now,
            max_age_seconds=quality["threshold_seconds"],
        )
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

"""Opt-in, observe-only collection during the space-heating season."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord
from packages.core.settings_service import (
    get_bool_setting,
    get_float_setting,
    get_int_setting,
    set_settings_bulk,
)


async def get_seasonal_calibration_status(
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Return whether an explicitly enabled seasonal observation window is active.

    It is intentionally based on recent outdoor observations rather than month
    names.  A cold September can qualify and a warm April will not.  The caller
    uses ``observe_only_active`` to suppress command dispatch; this module
    never creates a heat-pump command itself.
    """

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)

    enabled, threshold_c, minimum_days, auto_train, auto_exit = await _settings()
    since = now - dt.timedelta(days=minimum_days)
    async with get_session() as session:
        average_outdoor, readings = (
            await session.execute(
                select(
                    func.avg(DeviceStatusRecord.outdoor_temp),
                    func.count(DeviceStatusRecord.outdoor_temp),
                ).where(
                    DeviceStatusRecord.ts >= since,
                    DeviceStatusRecord.outdoor_temp.is_not(None),
                )
            )
        ).one()

    average_c = round(float(average_outdoor), 1) if average_outdoor is not None else None
    # A day's normal 5-minute polls is enough to avoid treating one cold sample
    # as a seasonal transition.
    enough_readings = int(readings or 0) >= 24
    heating_season = bool(enough_readings and average_c is not None and average_c <= threshold_c)
    if not enough_readings:
        reason = "waiting_for_recent_outdoor_data"
    elif not heating_season:
        reason = "waiting_for_heating_season"
    elif not enabled:
        reason = "available_but_not_enabled"
    else:
        reason = "observe_only_seasonal_calibration_active"

    return {
        "enabled": enabled,
        "observe_only_active": bool(enabled and heating_season),
        "heating_season_detected": heating_season,
        "reason": reason,
        "average_outdoor_c": average_c,
        "outdoor_readings": int(readings or 0),
        "window_days": minimum_days,
        "activation_threshold_c": threshold_c,
        "auto_train": auto_train,
        "auto_exit": auto_exit,
    }


async def _settings() -> tuple[bool, float, int, bool, bool]:
    """Read settings in one helper so failures remain explicit to callers."""

    return (
        await get_bool_setting("seasonal_calibration_enabled"),
        await get_float_setting("seasonal_calibration_max_outdoor_c"),
        await get_int_setting("seasonal_calibration_window_days"),
        await get_bool_setting("seasonal_calibration_auto_train"),
        await get_bool_setting("seasonal_calibration_auto_exit"),
    )


async def run_seasonal_calibration_cycle() -> dict[str, object]:
    """Train only when an opted-in cold-weather campaign has sufficient data.

    This is called by the poller on a low-frequency schedule.  It never
    changes heat-pump controls; it only calibrates local models and can turn
    off the *explicitly enabled* observe-only switch when both the demand and
    indoor-heating evidence are ready.
    """

    status = await get_seasonal_calibration_status()
    if not status["observe_only_active"]:
        return {**status, "cycle": "not_active"}

    from packages.ml.models import demand_model
    from packages.ml.thermal import MIN_LEARNED_RATE_SAMPLES, thermal_model

    quality = await demand_model.training_data_quality()
    thermal_result = await thermal_model.calibrate()
    indoor_samples = int(thermal_model.params.indoor_heating_samples)
    demand_ready = bool(quality.get("ready_to_train"))
    indoor_ready = indoor_samples >= MIN_LEARNED_RATE_SAMPLES
    train_result: dict[str, object] | None = None
    if status["auto_train"] and demand_ready:
        train_result = await demand_model.train()

    trained_demand = demand_model.is_trained
    completed = trained_demand and indoor_ready
    if completed and status["auto_exit"]:
        await set_settings_bulk({"seasonal_calibration_enabled": "false"})
        status = await get_seasonal_calibration_status()

    next_step = "collect_space_heating_evidence"
    if not demand_ready:
        next_step = "collect_space_heating_evidence"
    elif not trained_demand:
        next_step = "await_successful_demand_training"
    elif not indoor_ready:
        next_step = "collect_indoor_heating_evidence"
    elif completed:
        next_step = "completed"
    return {
        **status,
        "cycle": "completed" if completed else "collecting",
        "next_step": next_step,
        "demand": {
            "usable_samples": quality.get("usable_samples"),
            "minimum_samples": quality.get("minimum_samples"),
            "remaining_samples": quality.get("remaining_samples"),
            "trained": trained_demand,
            "train_result": train_result,
        },
        "indoor_heating": {
            "samples": indoor_samples,
            "minimum_samples": MIN_LEARNED_RATE_SAMPLES,
            "remaining_samples": max(0, MIN_LEARNED_RATE_SAMPLES - indoor_samples),
            "calibration": thermal_result,
        },
    }


async def get_seasonal_calibration_progress() -> dict[str, object]:
    """Expose the next evidence milestone without running a calibration job."""

    status = await get_seasonal_calibration_status()
    from packages.ml.models import demand_model
    from packages.ml.thermal import MIN_LEARNED_RATE_SAMPLES, thermal_model

    quality = await demand_model.training_data_quality()
    thermal_model.load_latest()
    indoor_samples = int(thermal_model.params.indoor_heating_samples)
    demand_ready = bool(quality.get("ready_to_train"))
    if not status["heating_season_detected"]:
        next_step = "wait_for_heating_season"
    elif not status["enabled"]:
        next_step = "enable_observe_only_campaign"
    elif not demand_ready:
        next_step = "collect_space_heating_evidence"
    elif not demand_model.is_trained:
        next_step = "await_successful_demand_training"
    elif indoor_samples < MIN_LEARNED_RATE_SAMPLES:
        next_step = "collect_indoor_heating_evidence"
    else:
        next_step = "ready_to_complete"
    return {
        **status,
        "next_step": next_step,
        "demand": {
            "usable_samples": quality.get("usable_samples"),
            "minimum_samples": quality.get("minimum_samples"),
            "remaining_samples": quality.get("remaining_samples"),
            "trained": demand_model.is_trained,
        },
        "indoor_heating": {
            "samples": indoor_samples,
            "minimum_samples": MIN_LEARNED_RATE_SAMPLES,
            "remaining_samples": max(0, MIN_LEARNED_RATE_SAMPLES - indoor_samples),
        },
    }

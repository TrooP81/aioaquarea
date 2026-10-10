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
    get_setting,
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

    enabled, threshold_c, minimum_days, auto_train, _auto_exit = await _settings()
    campaign_since = await get_setting("_seasonal_calibration_campaign_since")
    campaign_active = bool(enabled and campaign_since)
    since = now - dt.timedelta(days=minimum_days)
    outdoor_data_reliable = True
    try:
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
    except Exception:
        if not campaign_active:
            raise
        average_outdoor, readings = None, 0
        outdoor_data_reliable = False

    average_c = round(float(average_outdoor), 1) if average_outdoor is not None else None
    # A day's normal 5-minute polls is enough to avoid treating one cold sample
    # as a seasonal transition.
    enough_readings = int(readings or 0) >= 24
    heating_season = bool(enough_readings and average_c is not None and average_c <= threshold_c)
    safety_deferred_since: dt.datetime | None = None
    unresolved_summary = {"count": 0}
    if enabled and heating_season and not campaign_active:
        from packages.core.safety_reverts import unresolved_revert_summary

        async with get_session() as session:
            unresolved_summary = await unresolved_revert_summary(session, now=now)
        if int(unresolved_summary["count"]) > 0:
            raw_deferred_since = await get_setting("_seasonal_calibration_safety_deferred_since")
            try:
                safety_deferred_since = dt.datetime.fromisoformat(raw_deferred_since)
                if safety_deferred_since.tzinfo is None:
                    safety_deferred_since = safety_deferred_since.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                safety_deferred_since = now
                await set_settings_bulk(
                    {"_seasonal_calibration_safety_deferred_since": now.isoformat()}
                )
        elif await get_setting("_seasonal_calibration_safety_deferred_since"):
            await set_settings_bulk({"_seasonal_calibration_safety_deferred_since": ""})
    elif await get_setting("_seasonal_calibration_safety_deferred_since"):
        await set_settings_bulk({"_seasonal_calibration_safety_deferred_since": ""})

    if enabled and heating_season and safety_deferred_since is None and not campaign_active:
        campaign_since = now.isoformat()
        await set_settings_bulk({"_seasonal_calibration_campaign_since": campaign_since})
        campaign_active = True

    if campaign_active:
        reason = "observe_only_seasonal_calibration_active"
    elif not enough_readings:
        reason = "waiting_for_recent_outdoor_data"
    elif not heating_season:
        reason = "waiting_for_heating_season"
    elif not enabled:
        reason = "available_but_not_enabled"
    elif safety_deferred_since is not None:
        reason = "blocked_by_unresolved_safety_revert"
    else:
        reason = "observe_only_seasonal_calibration_active"

    deferred_seconds = (
        max(0, int((now - safety_deferred_since).total_seconds()))
        if safety_deferred_since is not None
        else None
    )

    return {
        "enabled": enabled,
        "observe_only_active": campaign_active,
        "campaign_since": campaign_since or None,
        "heating_season_detected": heating_season,
        "reason": reason,
        "average_outdoor_c": average_c,
        "outdoor_data_reliable": outdoor_data_reliable,
        "outdoor_readings": int(readings or 0),
        "window_days": minimum_days,
        "activation_threshold_c": threshold_c,
        "auto_train": auto_train,
        "auto_exit": False,
        "seasonal_blocked_by_unresolved_safety_revert": safety_deferred_since is not None,
        "seasonal_first_deferred_at": (
            safety_deferred_since.isoformat() if safety_deferred_since is not None else None
        ),
        "seasonal_deferred_seconds": deferred_seconds,
        "open_revert_obligations": unresolved_summary,
    }


async def _settings() -> tuple[bool, float, int, bool, bool]:
    """Read settings in one helper so failures remain explicit to callers."""

    return (
        await get_bool_setting("seasonal_calibration_enabled"),
        await get_float_setting("seasonal_calibration_max_outdoor_c"),
        await get_int_setting("seasonal_calibration_window_days"),
        await get_bool_setting("seasonal_calibration_auto_train"),
        False,
    )


async def run_seasonal_calibration_cycle() -> dict[str, object]:
    """Queue opted-in seasonal maintenance without fitting or exiting observation."""

    status = await get_seasonal_calibration_status()
    if not status["observe_only_active"]:
        return {**status, "cycle": "not_active"}
    if not await get_bool_setting("ml_maintenance_enabled"):
        return {**status, "cycle": "maintenance_disabled", "requests": []}

    from packages.ml.lifecycle import enqueue_maintenance_request
    from packages.ml.models import demand_model
    from packages.ml.thermal import MIN_LEARNED_RATE_SAMPLES, thermal_model

    quality = await demand_model.training_data_quality()
    await demand_model.arefresh_committed()
    await thermal_model.arefresh_if_changed()
    indoor_samples = int(thermal_model.params.indoor_heating_samples)
    demand_ready = bool(quality.get("ready_to_train"))
    thermal_result = None
    train_result: dict[str, object] | None = None
    slot = int(dt.datetime.now(dt.timezone.utc).timestamp() // (6 * 3600))
    if status["auto_train"]:
        thermal_result = await enqueue_maintenance_request(
            "thermal", "seasonal", automatic=True, dedup_key=f"seasonal:{slot}"
        )
        if demand_ready:
            train_result = await enqueue_maintenance_request(
                "demand", "seasonal", automatic=True, dedup_key=f"seasonal:{slot}"
            )

    trained_demand = demand_model.is_trained
    requests = [request for request in (train_result, thermal_result) if request is not None]
    next_step = "await_model_maintenance" if requests else "collect_space_heating_evidence"
    return {
        **status,
        "cycle": "queued" if requests else "collecting",
        "next_step": next_step,
        "requests": requests,
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
    await demand_model.arefresh_committed()
    await thermal_model.arefresh_if_changed()
    indoor_samples = int(thermal_model.params.indoor_heating_samples)
    demand_ready = bool(quality.get("ready_to_train"))
    if not status["heating_season_detected"] and not status["observe_only_active"]:
        next_step = "wait_for_heating_season"
    elif not status["enabled"]:
        next_step = "enable_observe_only_campaign"
    elif not demand_ready:
        next_step = "collect_space_heating_evidence"
    elif not demand_model.is_trained:
        next_step = "await_successful_demand_training"
    elif thermal_model.confidence_for("indoor_heating") != "learned":
        next_step = "collect_indoor_heating_evidence"
    else:
        next_step = "await_prospective_reporting_qualification"
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

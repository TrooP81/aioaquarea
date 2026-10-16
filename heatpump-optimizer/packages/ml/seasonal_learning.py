"""Opt-in, observe-only collection during the space-heating season."""

from __future__ import annotations

import datetime as dt
import copy
import hashlib
from contextlib import nullcontext

from sqlalchemy import func, select, text

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord
from packages.core.settings_service import (
    SETTING_SPECS,
    get_all_settings,
    get_bool_setting,
    get_float_setting,
    get_int_setting,
    set_settings_bulk,
)


async def get_seasonal_calibration_status(
    *,
    now: dt.datetime | None = None,
    context: dict | None = None,
    materialized: dict | None = None,
    session=None,
) -> dict[str, object]:
    """Read the persisted campaign; only the explicit cycle can latch activation."""
    now = now or dt.datetime.now(dt.timezone.utc)
    now = (
        now.replace(tzinfo=dt.timezone.utc)
        if now.tzinfo is None
        else now.astimezone(dt.timezone.utc)
    )
    if context is None:
        async with (
            get_session(read_only=True, isolation_level="REPEATABLE READ")
            if session is None
            else nullcontext(session)
        ) as borrowed:
            values = await get_all_settings(session=borrowed)
            return await get_seasonal_calibration_status(
                now=now, context={"settings": values}, materialized=materialized, session=borrowed
            )
    values = context["settings"]
    enabled, threshold_c, minimum_days, auto_train, _auto_exit = await _settings(context=context)
    campaign_since = values.get("_seasonal_calibration_campaign_since", "")
    campaign_active = bool(enabled and campaign_since)
    if materialized is None:
        if session is None:
            raise ValueError("Captured seasonal evidence is unavailable")
        else:
            try:
                materialized = await materialize_seasonal_inputs(
                    now=now, window_days=minimum_days, session=session
                )
            except Exception:
                if not campaign_active:
                    raise
                materialized = {
                    "average_outdoor": None,
                    "outdoor_readings": None,
                    "outdoor_data_reliable": False,
                    "open_revert_obligations": {"count": None},
                }
    average_outdoor = materialized.get("average_outdoor")
    readings = materialized.get("outdoor_readings")
    outdoor_data_reliable = materialized.get("outdoor_data_reliable", True)
    average_c = round(float(average_outdoor), 1) if average_outdoor is not None else None
    enough_readings = int(readings or 0) >= 24
    heating_season = bool(enough_readings and average_c is not None and average_c <= threshold_c)
    safety_deferred_since: dt.datetime | None = None
    unresolved_summary = materialized.get("open_revert_obligations", {"count": None})
    safety_count = unresolved_summary.get("count")
    safety_unavailable = type(safety_count) is not int or safety_count < 0
    blocked = not safety_unavailable and safety_count > 0
    if blocked:
        try:
            safety_deferred_since = dt.datetime.fromisoformat(
                values.get("_seasonal_calibration_safety_deferred_since", "")
            )
            if safety_deferred_since.tzinfo is None:
                safety_deferred_since = safety_deferred_since.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            pass
    if safety_unavailable:
        reason = "safety_obligations_unavailable"
    elif blocked:
        reason = "blocked_by_unresolved_safety_revert"
    elif campaign_active:
        reason = "observe_only_seasonal_calibration_active"
    elif not enough_readings:
        reason = "waiting_for_recent_outdoor_data"
    elif not heating_season:
        reason = "waiting_for_heating_season"
    elif not enabled:
        reason = "available_but_not_enabled"
    else:
        reason = "awaiting_explicit_seasonal_cycle"

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
        "outdoor_readings": int(readings) if readings is not None else None,
        "window_days": minimum_days,
        "activation_threshold_c": threshold_c,
        "auto_train": auto_train,
        "auto_exit": False,
        "seasonal_blocked_by_unresolved_safety_revert": blocked,
        "seasonal_first_deferred_at": (
            safety_deferred_since.isoformat() if safety_deferred_since is not None else None
        ),
        "seasonal_deferred_seconds": deferred_seconds,
        "open_revert_obligations": unresolved_summary,
        "reset_history": materialized.get("reset_history"),
    }


async def materialize_seasonal_inputs(*, now: dt.datetime, window_days: int, session) -> dict:
    from packages.core.safety_reverts import unresolved_revert_summary
    from packages.core.data_reset import reset_history_summary

    average, readings = (
        await session.execute(
            select(
                func.avg(DeviceStatusRecord.outdoor_temp),
                func.count(DeviceStatusRecord.outdoor_temp),
            )
            .where(DeviceStatusRecord.ts >= now - dt.timedelta(days=window_days))
            .where(DeviceStatusRecord.ts <= now, DeviceStatusRecord.outdoor_temp.is_not(None))
        )
    ).one()
    return {
        "average_outdoor": float(average) if average is not None else None,
        "outdoor_readings": int(readings or 0),
        "outdoor_data_reliable": True,
        "open_revert_obligations": await unresolved_revert_summary(session, now=now),
        "reset_history": await reset_history_summary(session),
    }


async def _settings(*, context=None) -> tuple[bool, float, int, bool, bool]:
    """Read settings in one helper so failures remain explicit to callers."""
    if context is not None:
        values = context["settings"]
        keys = (
            "seasonal_calibration_enabled",
            "seasonal_calibration_max_outdoor_c",
            "seasonal_calibration_window_days",
            "seasonal_calibration_auto_train",
        )
        parsed = [SETTING_SPECS[key].parse(values[key]) for key in keys]
        return bool(parsed[0]), float(parsed[1]), int(parsed[2]), bool(parsed[3]), False
    return (
        await get_bool_setting("seasonal_calibration_enabled"),
        await get_float_setting("seasonal_calibration_max_outdoor_c"),
        await get_int_setting("seasonal_calibration_window_days"),
        await get_bool_setting("seasonal_calibration_auto_train"),
        False,
    )


async def _transition_seasonal_campaign(*, now: dt.datetime) -> dict:
    status = await get_seasonal_calibration_status(now=now)
    from packages.core.safety_reverts import unresolved_revert_summary

    try:
        async with get_session() as session:
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": int.from_bytes(
                        hashlib.sha256(b"settings:heat-gate").digest()[:8], "big", signed=True
                    )
                },
            )
            await session.execute(text("LOCK TABLE plan_actions IN SHARE MODE"))
            values = await get_all_settings(session=session)
            evidence = {
                "average_outdoor": status["average_outdoor_c"],
                "outdoor_readings": status["outdoor_readings"],
                "outdoor_data_reliable": status["outdoor_data_reliable"],
                "open_revert_obligations": await unresolved_revert_summary(session, now=now),
            }
            current = await get_seasonal_calibration_status(
                now=now, context={"settings": values}, materialized=evidence
            )
            if current["reason"] == "safety_obligations_unavailable":
                return {**current, "transition": "unavailable"}
            if current["window_days"] != status["window_days"]:
                return {**current, "transition": "context_changed"}
            updates = {}
            if (
                current["enabled"]
                and current["heating_season_detected"]
                and not current["observe_only_active"]
            ):
                if current["seasonal_blocked_by_unresolved_safety_revert"]:
                    if not current["seasonal_first_deferred_at"]:
                        updates["_seasonal_calibration_safety_deferred_since"] = now.isoformat()
                else:
                    updates = {
                        "_seasonal_calibration_campaign_since": now.isoformat(),
                        "_seasonal_calibration_safety_deferred_since": "",
                    }
            elif values.get("_seasonal_calibration_safety_deferred_since"):
                updates["_seasonal_calibration_safety_deferred_since"] = ""
            if updates:
                await set_settings_bulk(updates, session=session)
                values = {**values, **updates}
                current = await get_seasonal_calibration_status(
                    now=now, context={"settings": values}, materialized=evidence
                )
        return current
    except Exception:
        return {**status, "transition": "unavailable", "reason": "seasonal_transition_unavailable"}


async def run_seasonal_calibration_cycle(*, now: dt.datetime | None = None) -> dict[str, object]:
    """Queue opted-in seasonal maintenance without fitting or exiting observation."""

    now = now or dt.datetime.now(dt.timezone.utc)
    status = await _transition_seasonal_campaign(now=now)
    if status.get("transition"):
        return {**status, "cycle": "transition_unavailable", "requests": []}
    if not status["observe_only_active"]:
        return {**status, "cycle": "not_active"}
    if not await get_bool_setting("ml_maintenance_enabled"):
        return {**status, "cycle": "maintenance_disabled", "requests": []}

    from packages.ml.lifecycle import enqueue_maintenance_request
    from packages.ml.models import demand_model
    from packages.ml.thermal import MIN_LEARNED_RATE_SAMPLES

    quality = await demand_model.training_data_quality()
    progress = await get_seasonal_calibration_progress()
    indoor_samples = progress["indoor_heating"]["samples"]
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

    trained_demand = progress["demand"]["trained"]
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
            "remaining_samples": progress["indoor_heating"]["remaining_samples"],
            "calibration": thermal_result,
        },
    }


async def get_seasonal_calibration_progress(
    *, snapshot=None, context=None, materialized=None
) -> dict[str, object]:
    """Expose the next evidence milestone without running a calibration job."""

    from packages.ml.model_status import get_readiness_snapshot

    if snapshot is None:
        if context is not None or materialized is not None:
            if context is None or materialized is None:
                raise ValueError("Captured progress requires context and materialized evidence")
            snapshot = {**materialized, "context": context, "as_of": context["as_of"]}
        else:
            snapshot = await get_readiness_snapshot(route="seasonal-progress")
    readiness = copy.deepcopy(snapshot["readiness"])
    if snapshot["context"] is None:
        return {
            "enabled": None,
            "observe_only_active": None,
            "heating_season_detected": None,
            "reason": "seasonal_reporting_unavailable",
            "auto_train": None,
            "auto_exit": False,
            "next_step": "readiness_unavailable",
            "readiness": readiness,
            "reporting_ready": False,
            "control_ready": False,
            "demand": {
                "usable_samples": None,
                "minimum_samples": 168,
                "remaining_samples": None,
                "trained": False,
            },
            "indoor_heating": {"samples": None, "minimum_samples": 20, "remaining_samples": None},
        }
    try:
        status = await get_seasonal_calibration_status(
            now=snapshot["as_of"],
            context=snapshot["context"],
            materialized=snapshot["seasonal_inputs"],
        )
    except Exception:
        values = snapshot["context"]["settings"]
        seasonal_inputs = snapshot.get("seasonal_inputs") or {}
        try:
            enabled = SETTING_SPECS["seasonal_calibration_enabled"].parse(
                values["seasonal_calibration_enabled"]
            )
        except (ValueError, KeyError):
            enabled = None
        try:
            auto_train = SETTING_SPECS["seasonal_calibration_auto_train"].parse(
                values["seasonal_calibration_auto_train"]
            )
        except (ValueError, KeyError):
            auto_train = None
        status = {
            "enabled": enabled,
            "observe_only_active": bool(
                values.get("_seasonal_calibration_campaign_since") and enabled
            )
            if enabled is not None
            else None,
            "heating_season_detected": None,
            "reason": "seasonal_reporting_unavailable",
            "auto_train": auto_train,
            "auto_exit": False,
            "open_revert_obligations": seasonal_inputs.get(
                "open_revert_obligations", {"count": None}
            ),
            "reset_history": seasonal_inputs.get("reset_history"),
        }
    demand_collection = readiness["components"]["demand"]["collection"]
    indoor_collection = readiness["components"]["indoor_heating"]["collection"]
    demand_fit = readiness["components"]["demand"]["fit"]
    indoor_fit = readiness["components"]["indoor_heating"]["fit"]
    demand_ready = (
        demand_collection["availability"] == "assessed"
        and not demand_collection["remaining_train"]
        and not demand_collection["remaining_holdout"]
    )
    indoor_ready = (
        indoor_collection["availability"] == "assessed"
        and not indoor_collection["remaining_train"]
        and not indoor_collection["remaining_holdout"]
    )
    reporting_ready = all(
        readiness["components"][component]["reporting"]["status"] == "qualified-for-reporting"
        for component in ("demand", "indoor_heating")
    )
    safety_count = status.get("open_revert_obligations", {}).get("count")
    if type(safety_count) is not int or safety_count < 0:
        next_step = "safety_obligations_unavailable"
    elif safety_count:
        next_step = "resolve_current_safety_obligations"
    elif status["heating_season_detected"] is None or status["observe_only_active"] is None:
        next_step = "readiness_unavailable"
    elif not status["heating_season_detected"] and not status["observe_only_active"]:
        next_step = "wait_for_heating_season"
        for name in ("demand", "indoor_heating"):
            if readiness["components"][name]["next_step"] != "ready_for_reporting":
                readiness["components"][name]["next_step"] = next_step
    elif not status["enabled"]:
        next_step = "enable_observe_only_campaign"
    elif (
        demand_collection["availability"] != "assessed"
        or indoor_collection["availability"] != "assessed"
    ):
        next_step = "readiness_unavailable"
    elif not demand_ready:
        next_step = "collect_space_heating_evidence"
    elif demand_fit["status"] != "validated":
        next_step = "await_successful_demand_training"
    elif not indoor_ready:
        next_step = "collect_indoor_heating_evidence"
    elif indoor_fit["status"] != "validated":
        next_step = "await_indoor_heating_fit"
    elif reporting_ready:
        next_step = "ready_for_reporting"
    else:
        next_step = "await_prospective_reporting_qualification"
    return {
        **status,
        "next_step": next_step,
        "readiness": readiness,
        "reporting_ready": reporting_ready,
        "control_ready": False,
        "demand": {
            "usable_samples": demand_collection["total"],
            "minimum_samples": demand_collection["minimum_train"],
            "remaining_samples": demand_collection["remaining_train"],
            "train_samples": demand_collection["train"],
            "holdout_samples": demand_collection["holdout"],
            "trained": snapshot["model_lifecycle"]["demand"]["trained"],
        },
        "indoor_heating": {
            "samples": indoor_collection["total"],
            "minimum_samples": indoor_collection["minimum_train"],
            "remaining_samples": indoor_collection["remaining_train"],
            "train_samples": indoor_collection["train"],
            "holdout_samples": indoor_collection["holdout"],
        },
    }

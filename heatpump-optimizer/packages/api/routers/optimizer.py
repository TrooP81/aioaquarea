from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from packages.api._helpers import get_price_area
from packages.api.schemas import (
    OverrideCreate,
    PlanActivityResponse,
    PlanDetailResponse,
    PlanResponse,
)
from packages.core.database import get_session
from packages.core.plan_outcome import measured_window_outcome, plan_measurement
from packages.core.models import (
    AuditLogRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    OptimizationRequestRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
)

router = APIRouter()


def _plan_provenance(plan: PlanRecord) -> dict:
    try:
        parsed = json.loads(plan.input_provenance_json) if plan.input_provenance_json else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _action_signature(action: PlanActionRecord) -> tuple[str, str, tuple[tuple[str, object], ...]]:
    payload = json.loads(action.payload_json) if action.payload_json else {}
    relevant = tuple(
        sorted(
            (key, payload[key])
            for key in ("temperature", "offset", "level", "zone_id")
            if key in payload
        )
    )
    return action.action_type, action.scheduled_ts.isoformat(), relevant


def _plan_outcome(actions: list[PlanActionRecord]) -> dict:
    statuses: dict[str, int] = {}
    delays: list[int] = []
    verified = 0
    for action in actions:
        statuses[action.status] = statuses.get(action.status, 0) + 1
        result = json.loads(action.result_json) if action.result_json else {}
        # Older rows and manually repaired data can contain a JSON scalar.
        # Treat it as unstructured rather than letting the history endpoint
        # fail for the whole plan.
        if not isinstance(result, dict):
            result = {}
        if result.get("verified") is True:
            verified += 1
        if action.executed_at is not None:
            delays.append(max(0, round((action.executed_at - action.scheduled_ts).total_seconds())))
    return {
        "statuses": statuses,
        "verified_actions": verified,
        "timing": {
            "measured_actions": len(delays),
            "on_time_actions": sum(delay <= 120 for delay in delays),
            "average_lateness_seconds": round(sum(delays) / len(delays)) if delays else None,
            "max_lateness_seconds": max(delays) if delays else None,
        },
        "cost_note": "Actual electricity cost is shown daily; individual commands are not separately metered.",
    }


def _plan_change_summary(
    actions: list[PlanActionRecord],
    previous_plan_id: int | None,
    previous_actions: list[PlanActionRecord],
) -> dict:
    if previous_plan_id is None:
        return {"kind": "first_plan", "message": "First retained plan in this history."}
    current = {_action_signature(action) for action in actions}
    previous = {_action_signature(action) for action in previous_actions}
    added = current - previous
    removed = previous - current
    drivers: list[str] = []
    for action in actions:
        if _action_signature(action) not in added:
            continue
        payload = json.loads(action.payload_json) if action.payload_json else {}
        reason = payload.get("reason")
        if isinstance(reason, str) and reason not in drivers:
            drivers.append(reason)
    if not added and not removed:
        message = "No command-level change from the preceding plan."
    else:
        message = f"{len(added)} command(s) added and {len(removed)} removed versus plan #{previous_plan_id}."
    return {
        "kind": "diff",
        "compared_to_plan_id": previous_plan_id,
        "added_actions": len(added),
        "removed_actions": len(removed),
        "drivers": drivers[:3],
        "message": message,
    }


class LearningModeUpdate(BaseModel):
    enabled: bool


@router.get("/api/plans", response_model=list[PlanResponse])
async def get_plans(limit: int = Query(10, ge=1, le=50)):
    """Get recent optimizer plans."""
    async with get_session() as session:
        count_subq = (
            select(
                PlanActionRecord.plan_id,
                func.count(PlanActionRecord.id).label("cnt"),
            )
            .group_by(PlanActionRecord.plan_id)
            .subquery()
        )

        result = await session.execute(
            select(PlanRecord, count_subq.c.cnt)
            .outerjoin(count_subq, PlanRecord.id == count_subq.c.plan_id)
            .order_by(desc(PlanRecord.created_at))
            .limit(limit)
        )

        responses = []
        for p, count in result.all():
            responses.append(
                PlanResponse(
                    id=p.id,
                    created_at=p.created_at,
                    horizon_start=p.horizon_start,
                    horizon_end=p.horizon_end,
                    optimizer_version=p.optimizer_version,
                    cost_estimate_eur=p.cost_estimate_eur,
                    price_currency=p.price_currency,
                    price_source=p.price_source,
                    actions_count=count or 0,
                    status=p.status,
                    status_reason=p.status_reason,
                    superseded_at=p.superseded_at,
                    superseded_by_plan_id=p.superseded_by_plan_id,
                )
            )
    return responses


@router.get("/api/plans/{plan_id}", response_model=PlanDetailResponse)
async def get_plan_detail(plan_id: int):
    """Get plan details with actions."""
    price_area = await get_price_area()
    comfort_min_c, comfort_max_c = await _comfort_bounds()
    async with get_session() as session:
        plan_result = await session.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        actions_result = await session.execute(
            select(PlanActionRecord)
            .where(PlanActionRecord.plan_id == plan_id)
            .order_by(PlanActionRecord.scheduled_ts)
        )
        actions = actions_result.scalars().all()
        previous_plan = (
            await session.execute(
                select(PlanRecord)
                .where(PlanRecord.created_at < plan.created_at)
                .order_by(desc(PlanRecord.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        previous_actions: list[PlanActionRecord] = []
        if previous_plan is not None:
            previous_actions = (
                (
                    await session.execute(
                        select(PlanActionRecord).where(PlanActionRecord.plan_id == previous_plan.id)
                    )
                )
                .scalars()
                .all()
            )
        measurement = await plan_measurement(
            session,
            plan=plan,
            price_area=price_area,
            comfort_min_c=comfort_min_c,
            comfort_max_c=comfort_max_c,
        )

    return PlanDetailResponse(
        id=plan.id,
        created_at=plan.created_at,
        horizon_start=plan.horizon_start,
        horizon_end=plan.horizon_end,
        optimizer_version=plan.optimizer_version,
        cost_estimate_eur=plan.cost_estimate_eur,
        price_currency=plan.price_currency,
        price_source=plan.price_source,
        actions_count=len(actions),
        status=plan.status,
        status_reason=plan.status_reason,
        superseded_at=plan.superseded_at,
        superseded_by_plan_id=plan.superseded_by_plan_id,
        actions=[
            {
                "id": a.id,
                "scheduled_ts": a.scheduled_ts.isoformat(),
                "action_type": a.action_type,
                "payload": json.loads(a.payload_json) if a.payload_json else {},
                "status": a.status,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                "result": json.loads(a.result_json) if a.result_json else None,
            }
            for a in actions
        ],
        outcome={**_plan_outcome(actions), "measurement": measurement},
        change_summary=_plan_change_summary(
            actions,
            previous_plan.id if previous_plan is not None else None,
            previous_actions,
        ),
        provenance=_plan_provenance(plan),
    )


async def _comfort_bounds() -> tuple[float, float]:
    """Read the configured comfort band once for measured outcome endpoints."""

    from packages.core.settings_service import get_float_setting

    return (
        await get_float_setting("comfort_temp_min"),
        await get_float_setting("comfort_temp_max"),
    )


@router.get("/api/outcomes/summary")
async def get_outcome_summary(days: int = Query(7, ge=1, le=30)):
    """Summarise measured cost exposure and indoor comfort for a recent period.

    Savings are intentionally labelled as *price-shift* estimates: the system
    has one cumulative heat-pump meter and cannot prove a per-command energy
    counterfactual.
    """

    from packages.core.outcome_experiments import get_outcome_experiment_status

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    price_area = await get_price_area()
    comfort_min_c, comfort_max_c = await _comfort_bounds()
    async with get_session() as session:
        measurement = await measured_window_outcome(
            session,
            start=start,
            end=now,
            price_area=price_area,
            price_currency=None,
            price_source=None,
            comfort_min_c=comfort_min_c,
            comfort_max_c=comfort_max_c,
        )
    return {
        "days": days,
        **measurement,
        "experiment": await get_outcome_experiment_status(),
        "baseline_method": (
            "Estimated price-shift savings compare measured energy with the same energy "
            "bought at the simple average available market price in the selected period."
        ),
        "note": "This is a measured period outcome, not a claim that individual commands caused specific kWh.",
    }


@router.get("/api/operations/alerts")
async def get_operations_alerts():
    """Return current actionable operational alerts for the dashboard."""

    from packages.core.operational_alerts import get_operational_alerts

    return await get_operational_alerts()


@router.get("/api/plan-activity", response_model=list[PlanActivityResponse])
async def get_plan_activity(
    limit: int = Query(25, ge=1, le=100),
    status: list[str] | None = Query(default=None),
):
    """Get recent actions that were actually attempted by the optimizer.

    Pending actions are intentionally excluded: they belong to a current plan,
    not to the record of what the system has already done.
    """
    async with get_session() as session:
        query = (
            select(PlanActionRecord, PlanRecord.created_at, PlanRecord.optimizer_version)
            .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
            .where(PlanActionRecord.status != "pending")
            .order_by(
                desc(func.coalesce(PlanActionRecord.executed_at, PlanActionRecord.scheduled_ts))
            )
            .limit(limit)
        )
        if status:
            allowed = {
                "executed",
                "executed_unverified",
                "failed",
                "expired",
                "skipped",
                "cancelled",
            }
            selected = [value for value in status if value in allowed]
            if selected:
                query = query.where(PlanActionRecord.status.in_(selected))
        result = await session.execute(query)

        return [
            PlanActivityResponse(
                id=action.id,
                plan_id=action.plan_id,
                plan_created_at=plan_created_at,
                optimizer_version=optimizer_version,
                scheduled_ts=action.scheduled_ts,
                action_type=action.action_type,
                status=action.status,
                executed_at=action.executed_at,
                lateness_seconds=(
                    max(0, round((action.executed_at - action.scheduled_ts).total_seconds()))
                    if action.executed_at is not None
                    else None
                ),
                payload=json.loads(action.payload_json) if action.payload_json else {},
                result=json.loads(action.result_json) if action.result_json else None,
            )
            for action, plan_created_at, optimizer_version in result.all()
        ]


@router.post("/api/overrides")
async def create_override(override: OverrideCreate):
    """Create a manual override (pauses optimizer for a period)."""
    if override.ts_to <= override.ts_from:
        raise HTTPException(status_code=422, detail="ts_to must be after ts_from")
    max_duration = dt.timedelta(days=7)
    if override.ts_to - override.ts_from > max_duration:
        raise HTTPException(status_code=422, detail="Override duration cannot exceed 7 days")

    async with get_session() as session:
        record = OverrideRecord(
            ts_from=override.ts_from,
            ts_to=override.ts_to,
            action_type=override.action_type,
            reason=override.reason,
            active=True,
        )
        session.add(record)
        session.add(
            AuditLogRecord(
                actor="user",
                action="create_override",
                payload_json=json.dumps(
                    {
                        "ts_from": override.ts_from.isoformat(),
                        "ts_to": override.ts_to.isoformat(),
                        "reason": override.reason,
                    }
                ),
                result="created",
            )
        )
    return {"status": "created"}


@router.delete("/api/overrides/{override_id}")
async def cancel_override(override_id: int):
    """Cancel an active override."""
    from sqlalchemy import update

    async with get_session() as session:
        await session.execute(
            update(OverrideRecord).where(OverrideRecord.id == override_id).values(active=False)
        )
    return {"status": "cancelled"}


async def _learning_mode_status() -> dict[str, object]:
    """Return learning-mode state plus how long it has been collecting data."""
    from packages.core.settings_service import get_bool_setting, get_setting

    enabled = await get_bool_setting("learning_mode_enabled")
    since_raw = await get_setting("learning_mode_since")
    since_iso: str | None = since_raw or None
    days_elapsed: float | None = None
    if enabled and since_iso:
        try:
            started = dt.datetime.fromisoformat(since_iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=dt.timezone.utc)
            days_elapsed = round(
                (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 86400, 2
            )
        except ValueError:
            since_iso = None

    return {"enabled": enabled, "since": since_iso, "days_elapsed": days_elapsed}


def _decision_readiness_summary(
    *,
    active_layer: str,
    demand_quality: dict[str, object],
    indoor_heating_samples: int,
    indoor_heating_confidence: str,
) -> dict[str, str]:
    """Give the UI one plain-language reason for the selected safe layer."""

    if "rules" not in active_layer:
        return {
            "state": "ready",
            "title": "Data requirements met for the active decision layer",
            "detail": "The optimizer has enough current evidence for this layer.",
        }
    remaining = int(demand_quality.get("remaining_samples") or 0)
    blocker = str(demand_quality.get("training_blocker") or "")
    if remaining:
        season_note = (
            " Heating-season evidence is naturally sparse in mild weather."
            if blocker == "waiting_for_space_heating_season"
            else ""
        )
        return {
            "state": "collecting",
            "title": "Rules are active while the demand model gathers heating evidence",
            "detail": f"{remaining} more valid space-heating interval(s) are needed before demand training can begin.{season_note}",
        }
    if indoor_heating_confidence != "learned":
        return {
            "state": "collecting",
            "title": "Rules are active while indoor heating response is still being learned",
            "detail": f"Only {indoor_heating_samples} trusted indoor-heating sample(s) are currently available.",
        }
    return {
        "state": "fallback",
        "title": "Rules are active as the current safe fallback",
        "detail": "Review the forecast-quality card and planning-input status for the condition keeping ML control inactive.",
    }


@router.get("/api/learning-mode")
async def get_learning_mode():
    """Get the current learning-mode state."""
    return await _learning_mode_status()


@router.post("/api/learning-mode")
async def set_learning_mode(body: LearningModeUpdate):
    """Enable or disable observe-only learning mode.

    While enabled the optimizer keeps generating plans but the executor dispatches
    no device commands, so the heat pump runs naturally and clean training data is
    collected over a long period.
    """
    from packages.core.settings_service import get_bool_setting, set_settings_bulk

    was_enabled = await get_bool_setting("learning_mode_enabled")
    now = dt.datetime.now(dt.timezone.utc)

    updates = {"learning_mode_enabled": "true" if body.enabled else "false"}
    if body.enabled and not was_enabled:
        updates["learning_mode_since"] = now.isoformat()
    elif not body.enabled:
        updates["learning_mode_since"] = ""

    await set_settings_bulk(updates)

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="set_learning_mode",
                payload_json=json.dumps({"enabled": body.enabled}),
                result="enabled" if body.enabled else "disabled",
            )
        )

    return await _learning_mode_status()


@router.get("/api/optimizer/status")
async def get_optimizer_status():
    """Get the current optimizer layer status, including ML model readiness."""
    from packages.core.config import settings as app_settings
    from packages.core.settings_service import get_float_setting, get_heat_curve_config, get_setting
    from packages.core.control_temperature import get_control_temperature
    from packages.ml.cop_model_core import COP_MODEL_ARTIFACT_GLOB, COP_MODEL_ARTIFACT_PREFIX
    from packages.ml.models import MODEL_DIR, cop_model, demand_model
    from packages.ml.seasonal_learning import get_seasonal_calibration_progress
    from packages.ml.thermal import thermal_model
    from packages.optimizer.main import get_optimizer_status_snapshot

    layer = await get_setting("optimizer_layer") or "rules_only"
    optimizer_status = await get_optimizer_status_snapshot(layer, reload_models=True)
    learning_mode = await _learning_mode_status()
    seasonal_calibration = await get_seasonal_calibration_progress()

    cop_models = sorted(MODEL_DIR.glob(COP_MODEL_ARTIFACT_GLOB))
    demand_models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))
    cop_model.load_latest()
    thermal_model.load_latest()

    demand_data_quality = await demand_model.training_data_quality()
    now = dt.datetime.now(dt.timezone.utc)
    device_max_age_seconds = max(int(app_settings.poll_interval_seconds) * 3, 15 * 60)
    async with get_session() as session:
        energy_record_count = (
            await session.execute(select(func.count()).select_from(ConsumptionRecord))
        ).scalar() or 0
        latest_device_status = (
            await session.execute(
                select(DeviceStatusRecord.ts).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()
        latest_status_record = (
            await session.execute(
                select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()
        last_plan = (
            await session.execute(select(PlanRecord).order_by(desc(PlanRecord.created_at)).limit(1))
        ).scalar_one_or_none()
        control_temperature = await get_control_temperature(session=session)

    last_plan_info = None
    if last_plan is not None:
        version = last_plan.optimizer_version or ""
        engine = "milp" if version.startswith("milp") else "rules"
        last_plan_info = {
            "version": version,
            "engine": engine,
            "fell_back": layer != "rules_only" and engine == "rules",
            "created_at": last_plan.created_at.isoformat() if last_plan.created_at else None,
        }

    heat_curve = await get_heat_curve_config()
    comfort_min_c = await get_float_setting("comfort_temp_min")
    comfort_max_c = await get_float_setting("comfort_temp_max")
    outdoor_c = (
        float(latest_status_record.outdoor_temp)
        if latest_status_record is not None and latest_status_record.outdoor_temp is not None
        else None
    )
    if outdoor_c is not None and outdoor_c >= heat_curve.heating_off_outdoor_c:
        comfort_controllability = {
            "status": "not_heatpump_controllable",
            "message": "Space heating is off above the controller cutoff, so warm-weather indoor deviations are shown separately from optimizer comfort performance.",
            "outdoor_temp_c": outdoor_c,
            "cutoff_c": heat_curve.heating_off_outdoor_c,
        }
    elif control_temperature.value is None:
        comfort_controllability = {
            "status": "awaiting_sensor",
            "message": "A fresh indoor sensor reading is needed before comfort controllability can be assessed.",
            "outdoor_temp_c": outdoor_c,
            "cutoff_c": heat_curve.heating_off_outdoor_c,
        }
    elif comfort_min_c <= control_temperature.value <= comfort_max_c:
        comfort_controllability = {
            "status": "within_band",
            "message": "Indoor temperature is within the configured comfort band.",
            "outdoor_temp_c": outdoor_c,
            "cutoff_c": heat_curve.heating_off_outdoor_c,
        }
    else:
        comfort_controllability = {
            "status": "heat_curve_controllable",
            "message": "The controller can influence space heating under the current outdoor conditions.",
            "outdoor_temp_c": outdoor_c,
            "cutoff_c": heat_curve.heating_off_outdoor_c,
        }

    def _version_to_iso(prefix: str, models: list) -> str | None:
        if not models:
            return None
        version = models[-1].stem.replace(prefix, "")
        try:
            return (
                dt.datetime.strptime(version, "%Y%m%d_%H%M")
                .replace(tzinfo=dt.timezone.utc)
                .isoformat()
            )
        except ValueError:
            return version

    return {
        "configured_layer": layer,
        "active_layer": optimizer_status["active_layer"],
        "fallback_layer": "rules_v6",
        "last_plan": last_plan_info,
        "data_freshness": {
            "latest_device_status": latest_device_status.isoformat()
            if latest_device_status
            else None,
            "age_seconds": round((now - latest_device_status).total_seconds())
            if latest_device_status
            else None,
            "stale_after_seconds": device_max_age_seconds,
            "fresh": latest_device_status is not None
            and latest_device_status >= now - dt.timedelta(seconds=device_max_age_seconds),
        },
        "planning_data_quality": optimizer_status["planning_data_quality"],
        "comfort_controllability": comfort_controllability,
        "learning_mode": learning_mode,
        "seasonal_calibration": seasonal_calibration,
        "decision_readiness": _decision_readiness_summary(
            active_layer=optimizer_status["active_layer"],
            demand_quality=demand_data_quality,
            indoor_heating_samples=thermal_model.params.indoor_heating_samples,
            indoor_heating_confidence=thermal_model.confidence_for("indoor_heating"),
        ),
        "cop_model": {
            "trained": optimizer_status["cop_trained"],
            "last_trained": _version_to_iso(COP_MODEL_ARTIFACT_PREFIX, cop_models),
            # COP has a different pairing/filter pipeline. Expose the raw input
            # count under its own name rather than mislabelling it as samples.
            "source_records": energy_record_count,
            "metrics": cop_model.metrics if cop_model.is_trained else {},
        },
        "demand_model": {
            "trained": optimizer_status["demand_trained"],
            "last_trained": _version_to_iso("demand_model_", demand_models),
            "samples": demand_data_quality["usable_samples"],
            "data_quality": demand_data_quality,
        },
        "thermal_model": {
            "calibrated": thermal_model.params.last_calibrated is not None,
            # Effective, outdoor-adjusted and clamped rate. The raw stored
            # intercept can be negative if calibration data only covered one
            # season, which is confusing when surfaced to the dashboard.
            "tank_heating_rate": round(
                thermal_model._tank_heating_rate(outdoor_c if outdoor_c is not None else 10.0),
                2,
            ),
            "tank_heating_rate_intercept_c0": round(
                thermal_model.params.tank_heating_rate, 2
            ),
            "tank_heating_outdoor_factor": round(
                thermal_model.params.tank_heating_outdoor_factor, 3
            ),
            "confidence": thermal_model.confidence_for("tank_heating"),
            "indoor_heating_confidence": thermal_model.confidence_for("indoor_heating"),
            "indoor_heating_samples": thermal_model.params.indoor_heating_samples,
            "calibration_status": thermal_model.params.calibration_status,
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
        },
    }


@router.post("/api/optimize-now")
async def optimize_now():
    """Queue a one-off optimization for the optimizer service.

    Running the solver in the API process used to compete with the scheduled
    optimizer and could create overlapping plans. Requests are durable, so the
    UI can follow their state even if the API restarts.
    """
    async with get_session() as session:
        request = OptimizationRequestRecord(requested_by="api")
        session.add(request)
        await session.flush()
        request_id = request.id

    return {"status": "queued", "request_id": request_id}


@router.get("/api/optimize-now/{request_id}")
async def get_optimization_request(request_id: int):
    """Return the status of a queued manual optimization."""

    async with get_session() as session:
        request = await session.get(OptimizationRequestRecord, request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="Optimization request not found")

    return {
        "id": request.id,
        "status": request.status,
        "requested_at": request.requested_at,
        "started_at": request.started_at,
        "completed_at": request.completed_at,
        "plan_id": request.plan_id,
        "error": request.error,
    }

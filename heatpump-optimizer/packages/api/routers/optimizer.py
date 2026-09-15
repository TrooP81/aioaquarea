from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from packages.api.schemas import OverrideCreate, PlanDetailResponse, PlanResponse
from packages.core.database import get_session
from packages.core.models import (
    AuditLogRecord,
    OptimizationRequestRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
)
from packages.core.plan_outcome import measured_window_outcome, plan_measurement

router = APIRouter()


class LearningModeUpdate(BaseModel):
    enabled: bool


def _json_object(value: str | None) -> dict[str, object]:
    """Decode persisted object JSON without making a history endpoint fail."""
    try:
        decoded = json.loads(value) if value else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
        from packages.api._helpers import get_price_area
        from packages.core.settings_service import get_float_setting, get_user_tz

        outcome = await plan_measurement(
            session,
            plan=plan,
            price_area=await get_price_area(),
            comfort_min_c=await get_float_setting("comfort_temp_min"),
            comfort_max_c=await get_float_setting("comfort_temp_max"),
            timezone_name=await get_user_tz(),
        )

    plan_data = _json_object(plan.plan_json)
    provenance = _json_object(plan.input_provenance_json)
    statuses: dict[str, int] = {}
    lateness_values = []
    for action in actions:
        statuses[action.status] = statuses.get(action.status, 0) + 1
        if action.executed_at is not None:
            lateness_values.append(
                max(0, (action.executed_at - action.scheduled_ts).total_seconds())
            )
    outcome.update(
        {
            "statuses": statuses,
            "verified_actions": sum(
                _json_object(action.result_json).get("verified") is True for action in actions
            ),
            "timing": {
                "measured_actions": len(lateness_values),
                "on_time_actions": sum(value <= 120 for value in lateness_values),
                "average_lateness_seconds": (
                    round(sum(lateness_values) / len(lateness_values)) if lateness_values else None
                ),
                "max_lateness_seconds": round(max(lateness_values)) if lateness_values else None,
            },
        }
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
        outcome=outcome,
        change_summary=_json_object(plan_data.get("change_summary")),
        provenance=provenance,
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
    )


@router.get("/api/plan-activity")
async def get_plan_activity(
    limit: int = Query(100, ge=1, le=200),
    status: list[str] | None = Query(None),
):
    """Return recent plan actions and their lifecycle outcomes."""
    meaningful_statuses = [
        "executed",
        "executed_unverified",
        "failed",
        "expired",
        "skipped",
        "cancelled",
    ]
    statuses = status if status else meaningful_statuses
    async with get_session() as session:
        result = await session.execute(
            select(PlanActionRecord, PlanRecord.created_at, PlanRecord.optimizer_version)
            .join(PlanRecord, PlanRecord.id == PlanActionRecord.plan_id)
            .where(PlanActionRecord.status.in_(statuses))
            .order_by(
                desc(func.coalesce(PlanActionRecord.executed_at, PlanActionRecord.scheduled_ts))
            )
            .limit(limit)
        )
        rows = result.all()

    return [
        {
            "id": action.id,
            "plan_id": action.plan_id,
            "plan_created_at": created_at,
            "optimizer_version": optimizer_version,
            "scheduled_ts": action.scheduled_ts,
            "action_type": action.action_type,
            "status": action.status,
            "executed_at": action.executed_at,
            "lateness_seconds": (
                max(0, round((action.executed_at - action.scheduled_ts).total_seconds()))
                if action.executed_at is not None
                else None
            ),
            "payload": json.loads(action.payload_json) if action.payload_json else {},
            "result": json.loads(action.result_json) if action.result_json else None,
        }
        for action, created_at, optimizer_version in rows
    ]


@router.get("/api/outcomes/summary")
async def get_outcome_summary(days: int = Query(7, ge=1, le=30)):
    """Return measured energy, cost, and comfort outcomes for a recent period."""
    from packages.api._helpers import get_price_area
    from packages.core.outcome_experiments import get_outcome_experiment_status
    from packages.core.settings_service import get_float_setting, get_user_tz

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    async with get_session() as session:
        outcome = await measured_window_outcome(
            session,
            start=start,
            end=now,
            price_area=await get_price_area(),
            price_currency=None,
            price_source=None,
            comfort_min_c=await get_float_setting("comfort_temp_min"),
            comfort_max_c=await get_float_setting("comfort_temp_max"),
            timezone_name=await get_user_tz(),
        )

    return {
        "days": days,
        "cost": outcome["cost"],
        "comfort": outcome["comfort"],
        "weather_matched_comparison": outcome["weather_matched_comparison"],
        "baseline_method": (
            "Estimated price-shift savings compare measured energy with the simple average "
            "available market price over this period. This is not proof of per-command savings."
        ),
        "experiment": await get_outcome_experiment_status(),
    }


@router.get("/api/operations/alerts")
async def get_operations_alerts():
    """Return current operational warnings for the dashboard."""
    from packages.core.operational_alerts import get_operational_alerts

    return await get_operational_alerts()


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
    from packages.core.settings_service import get_setting
    from packages.ml.models import MODEL_DIR
    from packages.ml.thermal import thermal_model
    from packages.optimizer.main import get_optimizer_status_snapshot

    layer = await get_setting("optimizer_layer") or "rules_only"
    optimizer_status = await get_optimizer_status_snapshot(layer)
    learning_mode = await _learning_mode_status()

    cop_models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
    demand_models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))

    async with get_session() as session:
        consumption_count = await session.execute(
            select(func.count()).select_from(PlanActionRecord)
        )
        total_consumption = consumption_count.scalar() or 0

    estimated_days = max(1, total_consumption // 96) if total_consumption > 0 else 0
    cop_samples = max(0, total_consumption - estimated_days)
    demand_samples = total_consumption

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
        "fallback_layer": "rules_v3",
        "learning_mode": learning_mode,
        "cop_model": {
            "trained": optimizer_status["cop_trained"],
            "last_trained": _version_to_iso("cop_model_", cop_models),
            "samples": cop_samples,
        },
        "demand_model": {
            "trained": optimizer_status["demand_trained"],
            "last_trained": _version_to_iso("demand_model_", demand_models),
            "samples": demand_samples,
        },
        "thermal_model": {
            "calibrated": thermal_model.params.last_calibrated is not None,
            "tank_heating_rate": round(thermal_model.params.tank_heating_rate, 2),
            "confidence": "learned" if thermal_model.params.last_calibrated else "default",
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
        },
    }


@router.post("/api/optimize-now")
async def optimize_now():
    """Queue a durable manual optimization request for the optimizer service."""
    async with get_session() as session:
        request = OptimizationRequestRecord(requested_by="api")
        session.add(request)
        await session.flush()
        request_id = request.id

    return {"status": "queued", "request_id": request_id}


@router.get("/api/optimize-now/{request_id}")
async def get_optimization_request(request_id: int):
    """Return the durable state of a manual optimization request."""
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

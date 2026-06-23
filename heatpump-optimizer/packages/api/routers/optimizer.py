from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from packages.api.schemas import OverrideCreate, PlanDetailResponse, PlanResponse
from packages.core.database import get_session
from packages.core.models import AuditLogRecord, OverrideRecord, PlanActionRecord, PlanRecord

router = APIRouter()


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
                    actions_count=count or 0,
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

    return PlanDetailResponse(
        id=plan.id,
        created_at=plan.created_at,
        horizon_start=plan.horizon_start,
        horizon_end=plan.horizon_end,
        optimizer_version=plan.optimizer_version,
        cost_estimate_eur=plan.cost_estimate_eur,
        actions_count=len(actions),
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
    optimizer_status = await get_optimizer_status_snapshot(layer, reload_models=True)
    learning_mode = await _learning_mode_status()

    cop_models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
    demand_models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))

    # What actually produced the most recent plan — this reveals silent
    # MILP→rules fallbacks (configured layer says MILP but the stored plan
    # was generated by the rules engine).
    async with get_session() as session:
        last_plan = (
            await session.execute(
                select(PlanRecord).order_by(desc(PlanRecord.created_at)).limit(1)
            )
        ).scalar_one_or_none()

    last_plan_info = None
    if last_plan is not None:
        version = last_plan.optimizer_version or ""
        engine = "milp" if version.startswith("milp") else "rules"
        fell_back = layer != "rules_only" and engine == "rules"
        last_plan_info = {
            "version": version,
            "engine": engine,
            "fell_back": fell_back,
            "created_at": last_plan.created_at.isoformat() if last_plan.created_at else None,
        }

    def _version_to_iso(prefix: str, models: list) -> str | None:
        if not models:
            return None
        version = models[-1].stem.replace(prefix, "")
        for fmt in ("%Y%m%d_%H%M%S", "%Y%m%d_%H%M"):
            try:
                return (
                    dt.datetime.strptime(version, fmt)
                    .replace(tzinfo=dt.timezone.utc)
                    .isoformat()
                )
            except ValueError:
                continue
        return version

    cop_last_trained = optimizer_status.get("cop_trained_at") or _version_to_iso(
        "cop_model_", cop_models
    )
    demand_last_trained = optimizer_status.get("demand_trained_at") or _version_to_iso(
        "demand_model_", demand_models
    )

    return {
        "configured_layer": layer,
        "active_layer": optimizer_status["active_layer"],
        "fallback_layer": "rules_v3",
        "last_plan": last_plan_info,
        "learning_mode": learning_mode,
        "cop_model": {
            "trained": optimizer_status["cop_trained"],
            "last_trained": cop_last_trained,
            "samples": optimizer_status.get("cop_samples"),
        },
        "demand_model": {
            "trained": optimizer_status["demand_trained"],
            "last_trained": demand_last_trained,
            "samples": optimizer_status.get("demand_samples"),
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
    """Manually trigger a one-off optimization run and return the new plan summary."""
    from packages.optimizer.main import run_optimization

    await run_optimization()

    async with get_session() as session:
        plan_result = await session.execute(select(PlanRecord).order_by(desc(PlanRecord.created_at)).limit(1))
        plan = plan_result.scalar_one_or_none()

    if not plan:
        return {"status": "no_plan", "message": "Optimization produced no plan"}

    async with get_session() as session:
        actions_result = await session.execute(
            select(func.count()).select_from(PlanActionRecord).where(PlanActionRecord.plan_id == plan.id)
        )
        action_count = actions_result.scalar() or 0

    return {
        "status": "ok",
        "plan_id": plan.id,
        "version": plan.optimizer_version,
        "actions": action_count,
        "horizon_start": plan.horizon_start.isoformat() if plan.horizon_start else None,
        "horizon_end": plan.horizon_end.isoformat() if plan.horizon_end else None,
        "cost_estimate_eur": plan.cost_estimate_eur,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }

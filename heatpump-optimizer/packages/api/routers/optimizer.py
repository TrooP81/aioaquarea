from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, func, select

from packages.api.schemas import OverrideCreate, PlanDetailResponse, PlanResponse
from packages.core.database import get_session
from packages.core.models import AuditLogRecord, OverrideRecord, PlanActionRecord, PlanRecord

router = APIRouter()


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


@router.get("/api/optimizer/status")
async def get_optimizer_status():
    """Get the current optimizer layer status, including ML model readiness."""
    from packages.core.settings_service import get_setting
    from packages.ml.models import MODEL_DIR
    from packages.ml.thermal import thermal_model
    from packages.optimizer.main import get_optimizer_status_snapshot

    layer = await get_setting("optimizer_layer") or "rules_only"
    optimizer_status = await get_optimizer_status_snapshot(layer, reload_models=True)

    cop_models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
    demand_models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))

    async with get_session() as session:
        consumption_count = await session.execute(select(func.count()).select_from(PlanActionRecord))
        total_consumption = consumption_count.scalar() or 0

    estimated_days = max(1, total_consumption // 96) if total_consumption > 0 else 0
    cop_samples = max(0, total_consumption - estimated_days)
    demand_samples = total_consumption

    def _version_to_iso(prefix: str, models: list) -> str | None:
        if not models:
            return None
        version = models[-1].stem.replace(prefix, "")
        try:
            return dt.datetime.strptime(version, "%Y%m%d_%H%M").replace(
                tzinfo=dt.timezone.utc
            ).isoformat()
        except ValueError:
            return version

    return {
        "configured_layer": layer,
        "active_layer": optimizer_status["active_layer"],
        "fallback_layer": "rules_v3",
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

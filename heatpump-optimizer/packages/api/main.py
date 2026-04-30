"""FastAPI application: REST API for the heat pump optimizer dashboard."""

from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, and_, func, desc

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    AuditLogRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    WeatherRecord,
)

app = FastAPI(
    title="Heat Pump Optimizer API",
    version="0.1.0",
    description="API for monitoring and optimizing Panasonic Aquarea heat pump costs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Schemas ---


class DeviceStatusResponse(BaseModel):
    ts: dt.datetime
    device_id: str
    mode: Optional[str] = None
    operation_status: Optional[int] = None
    outdoor_temp: Optional[float] = None
    tank_temp: Optional[float] = None
    tank_target_temp: Optional[int] = None
    zone1_temp: Optional[float] = None
    zone1_target_temp: Optional[float] = None
    quiet_mode: Optional[int] = None
    powerful_mode: Optional[int] = None


class ConsumptionResponse(BaseModel):
    ts: dt.datetime
    heat_kwh: Optional[float] = None
    cool_kwh: Optional[float] = None
    tank_kwh: Optional[float] = None
    total_kwh: Optional[float] = None
    outdoor_temp: Optional[float] = None


class PriceResponse(BaseModel):
    ts: dt.datetime
    price_eur_per_kwh: float


class WeatherResponse(BaseModel):
    ts: dt.datetime
    temperature: Optional[float] = None
    wind_speed: Optional[float] = None
    humidity: Optional[float] = None


class PlanResponse(BaseModel):
    id: int
    created_at: dt.datetime
    horizon_start: dt.datetime
    horizon_end: dt.datetime
    optimizer_version: str
    cost_estimate_eur: Optional[float] = None
    actions_count: int = 0


class PlanDetailResponse(PlanResponse):
    actions: list[dict]


class OverrideCreate(BaseModel):
    ts_from: dt.datetime
    ts_to: dt.datetime
    action_type: str
    reason: Optional[str] = None


class StatsResponse(BaseModel):
    period: str
    total_kwh: float
    total_cost_eur: float
    avg_cop: Optional[float] = None
    avg_price_eur_kwh: float
    savings_vs_baseline_eur: Optional[float] = None


class DashboardResponse(BaseModel):
    current_status: Optional[DeviceStatusResponse] = None
    current_price: Optional[float] = None
    today_kwh: float = 0
    today_cost_eur: float = 0
    active_plan: Optional[PlanResponse] = None
    has_override: bool = False


# --- Routes ---


@app.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get dashboard overview data."""
    now = dt.datetime.now(dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        # Latest device status
        status_result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = status_result.scalar_one_or_none()

        # Current price
        price_result = await session.execute(
            select(PriceRecord.price_eur_per_kwh)
            .where(
                and_(
                    PriceRecord.ts <= now,
                    PriceRecord.ts > now - dt.timedelta(hours=1),
                )
            )
            .order_by(desc(PriceRecord.ts))
            .limit(1)
        )
        current_price_row = price_result.scalar_one_or_none()

        # Today's consumption
        consumption_result = await session.execute(
            select(
                func.sum(ConsumptionRecord.heat_kwh),
                func.sum(ConsumptionRecord.cool_kwh),
                func.sum(ConsumptionRecord.tank_kwh),
            ).where(ConsumptionRecord.ts >= today_start)
        )
        consumption_row = consumption_result.one_or_none()

        # Active plan
        plan_result = await session.execute(
            select(PlanRecord)
            .where(PlanRecord.horizon_end > now)
            .order_by(desc(PlanRecord.created_at))
            .limit(1)
        )
        active_plan = plan_result.scalar_one_or_none()

        # Active override
        override_result = await session.execute(
            select(func.count(OverrideRecord.id)).where(
                and_(
                    OverrideRecord.active == True,
                    OverrideRecord.ts_from <= now,
                    OverrideRecord.ts_to >= now,
                )
            )
        )
        override_count = override_result.scalar() or 0

    today_kwh = 0.0
    if consumption_row and consumption_row[0] is not None:
        today_kwh = (consumption_row[0] or 0) + (consumption_row[1] or 0) + (consumption_row[2] or 0)

    avg_price = current_price_row or 0.10  # Default fallback
    today_cost = today_kwh * avg_price

    return DashboardResponse(
        current_status=DeviceStatusResponse(
            ts=status.ts,
            device_id=status.device_id,
            mode=status.mode,
            operation_status=status.operation_status,
            outdoor_temp=status.outdoor_temp,
            tank_temp=status.tank_temp,
            tank_target_temp=status.tank_target_temp,
            zone1_temp=status.zone1_temp,
            zone1_target_temp=status.zone1_target_temp,
            quiet_mode=status.quiet_mode,
            powerful_mode=status.powerful_mode,
        )
        if status
        else None,
        current_price=current_price_row,
        today_kwh=today_kwh,
        today_cost_eur=today_cost,
        active_plan=PlanResponse(
            id=active_plan.id,
            created_at=active_plan.created_at,
            horizon_start=active_plan.horizon_start,
            horizon_end=active_plan.horizon_end,
            optimizer_version=active_plan.optimizer_version,
            cost_estimate_eur=active_plan.cost_estimate_eur,
        )
        if active_plan
        else None,
        has_override=override_count > 0,
    )


@app.get("/api/status/history", response_model=list[DeviceStatusResponse])
async def get_status_history(
    hours: int = Query(24, ge=1, le=720),
):
    """Get device status history."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord)
            .where(DeviceStatusRecord.ts >= since)
            .order_by(DeviceStatusRecord.ts)
        )
        rows = result.scalars().all()

    return [
        DeviceStatusResponse(
            ts=r.ts,
            device_id=r.device_id,
            mode=r.mode,
            operation_status=r.operation_status,
            outdoor_temp=r.outdoor_temp,
            tank_temp=r.tank_temp,
            tank_target_temp=r.tank_target_temp,
            zone1_temp=r.zone1_temp,
            zone1_target_temp=r.zone1_target_temp,
            quiet_mode=r.quiet_mode,
            powerful_mode=r.powerful_mode,
        )
        for r in rows
    ]


@app.get("/api/consumption/history", response_model=list[ConsumptionResponse])
async def get_consumption_history(hours: int = Query(24, ge=1, le=720)):
    """Get consumption history."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= since)
            .order_by(ConsumptionRecord.ts)
        )
        rows = result.scalars().all()

    return [
        ConsumptionResponse(
            ts=r.ts,
            heat_kwh=r.heat_kwh,
            cool_kwh=r.cool_kwh,
            tank_kwh=r.tank_kwh,
            total_kwh=(r.heat_kwh or 0) + (r.cool_kwh or 0) + (r.tank_kwh or 0),
            outdoor_temp=r.outdoor_temp,
        )
        for r in rows
    ]


@app.get("/api/prices", response_model=list[PriceResponse])
async def get_prices(hours: int = Query(48, ge=1, le=168)):
    """Get electricity prices (past + future)."""
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours // 2)
    until = now + dt.timedelta(hours=hours // 2)

    async with get_session() as session:
        result = await session.execute(
            select(PriceRecord)
            .where(and_(PriceRecord.ts >= since, PriceRecord.ts <= until))
            .order_by(PriceRecord.ts)
        )
        rows = result.scalars().all()

    return [PriceResponse(ts=r.ts, price_eur_per_kwh=r.price_eur_per_kwh) for r in rows]


@app.get("/api/weather", response_model=list[WeatherResponse])
async def get_weather(hours: int = Query(48, ge=1, le=168)):
    """Get weather data."""
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=12)
    until = now + dt.timedelta(hours=hours)

    async with get_session() as session:
        result = await session.execute(
            select(WeatherRecord)
            .where(and_(WeatherRecord.ts >= since, WeatherRecord.ts <= until))
            .order_by(WeatherRecord.ts)
        )
        rows = result.scalars().all()

    return [
        WeatherResponse(
            ts=r.ts,
            temperature=r.temperature,
            wind_speed=r.wind_speed,
            humidity=r.humidity,
        )
        for r in rows
    ]


@app.get("/api/plans", response_model=list[PlanResponse])
async def get_plans(limit: int = Query(10, ge=1, le=50)):
    """Get recent optimizer plans."""
    async with get_session() as session:
        result = await session.execute(
            select(PlanRecord).order_by(desc(PlanRecord.created_at)).limit(limit)
        )
        plans = result.scalars().all()

        responses = []
        for p in plans:
            actions_count_result = await session.execute(
                select(func.count(PlanActionRecord.id)).where(
                    PlanActionRecord.plan_id == p.id
                )
            )
            count = actions_count_result.scalar() or 0
            responses.append(
                PlanResponse(
                    id=p.id,
                    created_at=p.created_at,
                    horizon_start=p.horizon_start,
                    horizon_end=p.horizon_end,
                    optimizer_version=p.optimizer_version,
                    cost_estimate_eur=p.cost_estimate_eur,
                    actions_count=count,
                )
            )
    return responses


@app.get("/api/plans/{plan_id}", response_model=PlanDetailResponse)
async def get_plan_detail(plan_id: int):
    """Get plan details with actions."""
    async with get_session() as session:
        plan_result = await session.execute(
            select(PlanRecord).where(PlanRecord.id == plan_id)
        )
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
            }
            for a in actions
        ],
    )


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats(period: str = Query("day", pattern="^(day|week|month)$")):
    """Get aggregated statistics."""
    now = dt.datetime.now(dt.timezone.utc)
    if period == "day":
        since = now - dt.timedelta(days=1)
    elif period == "week":
        since = now - dt.timedelta(weeks=1)
    else:
        since = now - dt.timedelta(days=30)

    async with get_session() as session:
        # Total consumption
        cons_result = await session.execute(
            select(
                func.sum(ConsumptionRecord.heat_kwh),
                func.sum(ConsumptionRecord.cool_kwh),
                func.sum(ConsumptionRecord.tank_kwh),
            ).where(ConsumptionRecord.ts >= since)
        )
        cons = cons_result.one()
        total_kwh = (cons[0] or 0) + (cons[1] or 0) + (cons[2] or 0)

        # Average price
        price_result = await session.execute(
            select(func.avg(PriceRecord.price_eur_per_kwh)).where(PriceRecord.ts >= since)
        )
        avg_price = price_result.scalar() or 0.10

    total_cost = total_kwh * avg_price

    return StatsResponse(
        period=period,
        total_kwh=total_kwh,
        total_cost_eur=total_cost,
        avg_price_eur_kwh=avg_price,
    )


@app.post("/api/overrides")
async def create_override(override: OverrideCreate):
    """Create a manual override (pauses optimizer for a period)."""
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


@app.delete("/api/overrides/{override_id}")
async def cancel_override(override_id: int):
    """Cancel an active override."""
    from sqlalchemy import update

    async with get_session() as session:
        await session.execute(
            update(OverrideRecord)
            .where(OverrideRecord.id == override_id)
            .values(active=False)
        )
    return {"status": "cancelled"}


@app.get("/api/audit", response_model=list[dict])
async def get_audit_log(limit: int = Query(50, ge=1, le=200)):
    """Get recent audit log entries."""
    async with get_session() as session:
        result = await session.execute(
            select(AuditLogRecord).order_by(desc(AuditLogRecord.ts)).limit(limit)
        )
        rows = result.scalars().all()

    return [
        {
            "ts": r.ts.isoformat(),
            "actor": r.actor,
            "action": r.action,
            "target_device": r.target_device,
            "payload": json.loads(r.payload_json) if r.payload_json else None,
            "result": r.result,
        }
        for r in rows
    ]


@app.get("/health")
async def health():
    return {"status": "ok"}

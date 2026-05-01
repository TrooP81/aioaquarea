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
    COPRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    FaultRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.core.settings_service import (
    SETTINGS_SCHEMA,
    get_all_settings,
    get_comfort_schedule,
    get_effective_schedule,
    get_learned_usage,
    set_settings_bulk,
    set_setting,
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
    # New compressor/activity fields
    direction: Optional[str] = None
    device_action: Optional[str] = None
    defrost_active: Optional[bool] = None
    force_dhw: Optional[int] = None
    force_heater: Optional[int] = None
    holiday_mode: Optional[int] = None


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
            direction=status.direction,
            device_action=status.device_action,
            defrost_active=status.defrost_active,
            force_dhw=status.force_dhw,
            force_heater=status.force_heater,
            holiday_mode=status.holiday_mode,
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
            direction=r.direction,
            device_action=r.device_action,
            defrost_active=r.defrost_active,
            force_dhw=r.force_dhw,
            force_heater=r.force_heater,
            holiday_mode=r.holiday_mode,
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


@app.get("/api/settings")
async def get_settings():
    """Get all configurable settings with schema metadata."""
    values = await get_all_settings()
    result = {}
    for key, schema in SETTINGS_SCHEMA.items():
        val = values.get(key, "")
        # Mask secrets in response
        if schema.get("type") == "secret" and val:
            display_val = val[:2] + "***" + val[-2:] if len(val) > 4 else "***"
        else:
            display_val = val
        result[key] = {
            "value": display_val,
            "type": schema["type"],
            "description": schema.get("description", ""),
            "options": schema.get("options"),
        }
    return result


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


@app.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    """Update multiple settings at once."""
    # Validate keys
    invalid_keys = [k for k in body.settings if k not in SETTINGS_SCHEMA]
    if invalid_keys:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {invalid_keys}")

    await set_settings_bulk(body.settings)

    # Audit log
    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="update_settings",
                payload_json=json.dumps(
                    {k: "***" if SETTINGS_SCHEMA[k].get("type") == "secret" else v
                     for k, v in body.settings.items()}
                ),
                result="updated",
            )
        )

    return {"status": "updated", "count": len(body.settings)}


# --- Comfort Schedule ---


class ComfortScheduleUpdate(BaseModel):
    weekday: list[int]
    weekend: list[int]


@app.get("/api/comfort-schedule")
async def get_schedule():
    """Get the current comfort schedule (hours marked as comfort for weekdays/weekends)."""
    return await get_comfort_schedule()


@app.put("/api/comfort-schedule")
async def update_schedule(body: ComfortScheduleUpdate):
    """Update the comfort schedule. Hours are 0-23."""
    # Validate hours are in range
    for h in body.weekday + body.weekend:
        if not (0 <= h <= 23):
            raise HTTPException(status_code=400, detail=f"Invalid hour: {h}. Must be 0-23.")

    schedule = {
        "weekday": sorted(set(body.weekday)),
        "weekend": sorted(set(body.weekend)),
    }
    await set_setting("comfort_schedule", json.dumps(schedule))

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="update_comfort_schedule",
                payload_json=json.dumps(schedule),
                result="updated",
            )
        )

    return schedule


@app.get("/api/comfort-schedule/learned")
async def get_learned_schedule(days: int = Query(14, ge=1, le=90)):
    """
    Analyze actual heating usage over the last N days and return a
    per-hour activity score (0.0-1.0) for weekdays and weekends.
    """
    learned = await get_learned_usage(days=days)

    # Convert int keys to string keys for JSON
    return {
        day_type: {str(h): score for h, score in hours.items()}
        for day_type, hours in learned.items()
    }


@app.post("/api/comfort-schedule/apply-learned")
async def apply_learned_schedule(threshold: float = Query(0.3, ge=0.1, le=0.9)):
    """
    Merge the base schedule with learned usage patterns.

    Any hour with a learned activity score >= threshold that isn't
    already in the base schedule gets added.
    """
    merged = await get_effective_schedule(learned_threshold=threshold)

    await set_setting("comfort_schedule", json.dumps(merged))

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="system",
                action="apply_learned_schedule",
                payload_json=json.dumps({"threshold": threshold, "result": merged}),
                result="updated",
            )
        )

    return merged


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


# --- Optimizer status ---


@app.get("/api/optimizer/status")
async def get_optimizer_status():
    """Get the current optimizer layer status, including ML model readiness."""
    from packages.ml.models import COPModel, DemandModel, MODEL_DIR
    from packages.ml.thermal import thermal_model
    from packages.core.settings_service import get_setting

    layer = await get_setting("optimizer_layer") or "rules_only"

    # Determine active layer
    cop = COPModel()
    cop.load_latest()
    demand = DemandModel()
    demand.load_latest()

    if layer == "rules_only":
        active_layer = "rules_v3"
    elif layer == "milp_preferred":
        active_layer = "milp_v1+ml" if cop.is_trained else "milp_v1"
    elif layer == "auto":
        if cop.is_trained and demand.is_trained:
            active_layer = "milp_v1+ml"
        else:
            active_layer = "rules_v3"
    else:
        active_layer = "rules_v3"

    # Find latest model timestamps
    cop_models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
    demand_models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))

    # Count training samples from DB
    async with get_session() as session:
        cop_count = await session.execute(
            select(func.count()).select_from(COPRecord)
        )
        cop_samples = cop_count.scalar() or 0

        consumption_count = await session.execute(
            select(func.count()).select_from(ConsumptionRecord)
        )
        demand_samples = consumption_count.scalar() or 0

    return {
        "configured_layer": layer,
        "active_layer": active_layer,
        "fallback_layer": "rules_v3",
        "cop_model": {
            "trained": cop.is_trained,
            "last_trained": cop_models[-1].stem.replace("cop_model_", "") if cop_models else None,
            "samples": cop_samples,
        },
        "demand_model": {
            "trained": demand.is_trained,
            "last_trained": demand_models[-1].stem.replace("demand_model_", "") if demand_models else None,
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


# --- Thermal predictions ---


@app.get("/api/thermal/status")
async def get_thermal_status():
    """Get current thermal model parameters and predictions."""
    from packages.ml.thermal import thermal_model

    # Get latest device status for current temps
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp or 48.0
    tank_target = status.tank_target_temp or 52
    outdoor = status.outdoor_temp or 7.0
    current_zone = status.zone1_temp or 20.0

    # Predictions
    heating_pred = thermal_model.predict_tank_heating_time(current_tank, float(tank_target), outdoor)
    cooling_pred = thermal_model.predict_tank_cooling_time(current_tank, float(tank_target - 7), outdoor)
    zone_pred = thermal_model.predict_zone_heating_time(current_zone, current_zone + 2, outdoor)

    return {
        "current": {
            "tank_temp": current_tank,
            "tank_target": tank_target,
            "outdoor_temp": outdoor,
            "zone1_temp": current_zone,
            "timestamp": status.ts.isoformat(),
        },
        "predictions": {
            "tank_heating": {
                "minutes_to_target": round(heating_pred.estimated_minutes, 1),
                "heating_rate_per_hour": round(heating_pred.heating_rate_per_hour, 2),
                "confidence": heating_pred.confidence,
            },
            "tank_cooling": {
                "minutes_until_min": round(cooling_pred.estimated_minutes, 1)
                if cooling_pred.estimated_minutes != float("inf")
                else None,
                "loss_rate_per_hour": round(cooling_pred.heating_rate_per_hour, 2),
                "confidence": cooling_pred.confidence,
            },
            "zone_boost": {
                "minutes_for_2deg": round(zone_pred.estimated_minutes, 1),
                "heating_rate_per_hour": round(zone_pred.heating_rate_per_hour, 2),
                "confidence": zone_pred.confidence,
            },
        },
        "model_params": {
            "tank_heating_rate": round(thermal_model.params.tank_heating_rate, 3),
            "tank_heating_outdoor_factor": round(thermal_model.params.tank_heating_outdoor_factor, 4),
            "tank_standby_loss": round(thermal_model.params.tank_standby_loss, 3),
            "zone_heating_rate": round(thermal_model.params.zone_heating_rate, 3),
            "zone_standby_loss": round(thermal_model.params.zone_standby_loss, 3),
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
            "sample_count": thermal_model.params.sample_count,
        },
    }


@app.post("/api/thermal/calibrate")
async def calibrate_thermal_model():
    """Manually trigger thermal model calibration from historical data."""
    from packages.ml.thermal import thermal_model

    result = await thermal_model.calibrate()
    return result


@app.get("/api/thermal/curve")
async def get_thermal_curve(hours: int = Query(24, ge=1, le=72)):
    """
    Get predicted temperature curves for tank and zone.
    Shows what happens with and without heating over the next N hours.
    """
    from packages.ml.thermal import thermal_model

    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp or 48.0
    tank_target = status.tank_target_temp or 52
    outdoor = status.outdoor_temp or 7.0
    current_zone = status.zone1_temp or 20.0

    # Tank: predict standby loss (no heating)
    tank_standby_curve = thermal_model.predict_temperature_curve(
        current_temp=current_tank,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=None,
        is_tank=True,
    )

    # Tank: predict with heating to target
    tank_heating_curve = thermal_model.predict_temperature_curve(
        current_temp=current_tank,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=float(tank_target),
        is_tank=True,
    )

    # Zone: predict standby loss
    zone_standby_curve = thermal_model.predict_temperature_curve(
        current_temp=current_zone,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=None,
        is_tank=False,
    )

    return {
        "current": {
            "tank_temp": current_tank,
            "tank_target": tank_target,
            "outdoor_temp": outdoor,
            "zone1_temp": current_zone,
        },
        "curves": {
            "tank_standby": tank_standby_curve,
            "tank_heating": tank_heating_curve,
            "zone_standby": zone_standby_curve,
        },
    }


# --- Manual poll trigger ---


@app.post("/api/poll-now")
async def poll_now():
    """
    Manually trigger a one-off poll of device status, prices, and weather.
    Useful for initial setup or to force a refresh without waiting for the timer.
    """
    import aiohttp
    from aioaquarea import Client, AquareaEnvironment
    from packages.core.settings_service import get_setting
    from packages.poller.feeds import fetch_prices, fetch_weather
    from packages.core.models import WeatherRecord

    results = {"device": None, "prices": None, "weather": None}

    # --- Poll device status ---
    username = await get_setting("aquarea_username")
    password = await get_setting("aquarea_password")

    if username and password:
        try:
            async with aiohttp.ClientSession() as session:
                client = Client(
                    session=session,
                    username=username,
                    password=password,
                    device_direct=True,
                    refresh_login=False,
                    environment=AquareaEnvironment.PRODUCTION,
                )
                await client.login()
                devices = await client.get_devices()
                if devices:
                    from datetime import timedelta

                    device = await client.get_device(
                        device_info=devices[0],
                        consumption_refresh_interval=timedelta(minutes=5),
                    )
                    await device.refresh_data()

                    zones = device.zones
                    zone1 = zones.get(1)
                    zone2 = zones.get(2)

                    # Compressor activity
                    direction = device.current_direction.name
                    device_action = device.current_action.name
                    defrost_active = device.device_mode_status.name == "DEFROST"

                    record = DeviceStatusRecord(
                        ts=dt.datetime.now(dt.timezone.utc),
                        device_id=device.long_id,
                        mode=str(device.mode),
                        operation_status=device.operation_status.value,
                        outdoor_temp=device.temperature_outdoor,
                        tank_temp=device.tank.temperature if device.tank else None,
                        tank_target_temp=device.tank.target_temperature if device.tank else None,
                        tank_operation_status=device.tank.operation_status.value if device.tank else None,
                        zone1_temp=zone1.temperature if zone1 else None,
                        zone1_target_temp=zone1.heat_target_temperature if zone1 else None,
                        zone2_temp=zone2.temperature if zone2 else None,
                        zone2_target_temp=zone2.heat_target_temperature if zone2 else None,
                        quiet_mode=device.quiet_mode.value,
                        powerful_mode=device.powerful_time.value,
                        special_status=device.special_status.value if device.special_status else None,
                        direction=direction,
                        pump_duty=device.pump_duty,
                        device_action=device_action,
                        defrost_active=defrost_active,
                        force_dhw=device.force_dhw.value,
                        force_heater=device.force_heater.value,
                        holiday_mode=device.holiday_timer.value,
                        zone1_operation_status=zone1.operation_status.value if zone1 else None,
                        zone2_operation_status=zone2.operation_status.value if zone2 else None,
                        tank_heat_max=device.tank.heat_max if device.tank else None,
                        tank_heat_min=device.tank.heat_min if device.tank else None,
                    )

                    async with get_session() as db:
                        db.add(record)

                    # Also fetch today's consumption
                    from aioaquarea.statistics import ConsumptionType

                    now = dt.datetime.now(dt.timezone.utc)
                    try:
                        heat = await device.get_and_refresh_consumption(now, ConsumptionType.HEAT) or 0
                        cool = await device.get_and_refresh_consumption(now, ConsumptionType.COOL) or 0
                        tank = await device.get_and_refresh_consumption(now, ConsumptionType.WATER_TANK) or 0

                        cons_record = ConsumptionRecord(
                            ts=now,
                            device_id=device.long_id,
                            heat_kwh=heat,
                            cool_kwh=cool,
                            tank_kwh=tank,
                            outdoor_temp=device.temperature_outdoor,
                        )
                        async with get_session() as db:
                            db.add(cons_record)

                        total = heat + cool + tank
                        results["device"] = {"success": True, "message": f"Device polled: outdoor={record.outdoor_temp}°C, tank={record.tank_temp}°C, action={device_action}, consumption={total:.1f} kWh"}
                    except Exception as ce:
                        # Consumption may not be available yet, that's ok
                        results["device"] = {"success": True, "message": f"Device polled: outdoor={record.outdoor_temp}°C, tank={record.tank_temp}°C, action={device_action} (consumption not yet available: {ce})"}
                else:
                    results["device"] = {"success": False, "message": "No devices found"}
        except Exception as e:
            results["device"] = {"success": False, "message": str(e)}
    else:
        results["device"] = {"success": False, "message": "Credentials not configured"}

    # --- Poll prices ---
    try:
        prices = await fetch_prices()
        if prices:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            price_provider = await get_setting("price_provider")
            area = (await get_setting("entsoe_area")) if price_provider == "entsoe" else "tibber"
            async with get_session() as db:
                for ts, price in prices:
                    stmt = pg_insert(PriceRecord).values(
                        ts=ts, area=area, price_eur_per_kwh=price
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "area"],
                        set_={"price_eur_per_kwh": price},
                    )
                    await db.execute(stmt)
            results["prices"] = {"success": True, "message": f"Fetched {len(prices)} price points"}
        else:
            results["prices"] = {"success": False, "message": "No price data returned"}
    except Exception as e:
        results["prices"] = {"success": False, "message": str(e)}

    # --- Poll weather ---
    try:
        weather_data = await fetch_weather()
        if weather_data:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            async with get_session() as db:
                for entry in weather_data:
                    stmt = pg_insert(WeatherRecord).values(
                        ts=entry["ts"],
                        source="open-meteo",
                        temperature=entry["temperature"],
                        irradiance=entry.get("irradiance"),
                        wind_speed=entry.get("wind_speed"),
                        humidity=entry.get("humidity"),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "source"],
                        set_={
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                        },
                    )
                    await db.execute(stmt)
            results["weather"] = {"success": True, "message": f"Fetched {len(weather_data)} weather entries"}
        else:
            results["weather"] = {"success": False, "message": "No weather data returned"}
    except Exception as e:
        results["weather"] = {"success": False, "message": str(e)}

    all_success = all(r and r["success"] for r in results.values() if r)
    return {"status": "ok" if all_success else "partial", "results": results}


# --- Connection test endpoints ---


class TestConnectionRequest(BaseModel):
    service: str  # "aquarea", "entsoe", "tibber"
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None
    area: Optional[str] = None


class TestConnectionResponse(BaseModel):
    service: str
    success: bool
    message: str
    details: Optional[dict] = None


@app.post("/api/test-connection", response_model=TestConnectionResponse)
async def test_connection(body: TestConnectionRequest):
    """
    Test connectivity to an external service using provided or stored credentials.
    Does NOT persist credentials — only validates they work.
    """
    if body.service == "aquarea":
        return await _test_aquarea(body.username, body.password)
    elif body.service == "entsoe":
        return await _test_entsoe(body.api_token, body.area)
    elif body.service == "tibber":
        return await _test_tibber(body.api_token)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown service: {body.service}")


async def _test_aquarea(
    username: Optional[str], password: Optional[str]
) -> TestConnectionResponse:
    """Test Panasonic Aquarea credentials by attempting login."""
    import aiohttp
    from aioaquarea import Client, AquareaEnvironment

    # Use provided credentials or fall back to stored settings
    from packages.core.settings_service import get_setting

    if not username:
        username = await get_setting("aquarea_username")
    if not password:
        password = await get_setting("aquarea_password")

    if not username or not password:
        return TestConnectionResponse(
            service="aquarea",
            success=False,
            message="Username and password are required",
        )

    try:
        async with aiohttp.ClientSession() as session:
            client = Client(
                session=session,
                username=username,
                password=password,
                device_direct=True,
                refresh_login=False,
                environment=AquareaEnvironment.PRODUCTION,
            )
            await client.login()

            # Try to list devices to confirm full access
            devices = await client.get_devices()
            device_count = len(devices) if devices else 0

        return TestConnectionResponse(
            service="aquarea",
            success=True,
            message=f"Authentication successful. Found {device_count} device(s).",
            details={"device_count": device_count},
        )
    except Exception as e:
        return TestConnectionResponse(
            service="aquarea",
            success=False,
            message=f"Authentication failed: {str(e)}",
        )


async def _test_entsoe(
    api_token: Optional[str], area: Optional[str]
) -> TestConnectionResponse:
    """Test ENTSO-E API token by fetching today's prices."""
    import httpx
    from packages.core.settings_service import get_setting

    if not api_token:
        api_token = await get_setting("entsoe_api_token")
    if not area:
        area = await get_setting("entsoe_area")

    if not api_token:
        return TestConnectionResponse(
            service="entsoe",
            success=False,
            message="ENTSO-E API token is required",
        )

    now = dt.datetime.now(dt.timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + dt.timedelta(days=1)

    params = {
        "securityToken": api_token,
        "documentType": "A44",
        "in_Domain": area or "10YNL----------L",
        "out_Domain": area or "10YNL----------L",
        "periodStart": period_start.strftime("%Y%m%d%H00"),
        "periodEnd": period_end.strftime("%Y%m%d%H00"),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://web-api.tp.entsoe.eu/api", params=params
            )

        if resp.status_code == 401:
            return TestConnectionResponse(
                service="entsoe",
                success=False,
                message="Invalid API token (401 Unauthorized)",
            )
        elif resp.status_code == 400:
            return TestConnectionResponse(
                service="entsoe",
                success=False,
                message="Bad request — check area code",
                details={"status_code": 400, "area": area},
            )

        resp.raise_for_status()

        return TestConnectionResponse(
            service="entsoe",
            success=True,
            message="ENTSO-E API connection successful. Price data available.",
            details={"status_code": resp.status_code, "area": area},
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            service="entsoe",
            success=False,
            message="Connection timed out after 30s",
        )
    except Exception as e:
        return TestConnectionResponse(
            service="entsoe",
            success=False,
            message=f"Connection failed: {str(e)}",
        )


async def _test_tibber(api_token: Optional[str]) -> TestConnectionResponse:
    """Test Tibber API token by querying viewer info."""
    import httpx
    from packages.core.settings_service import get_setting

    if not api_token:
        api_token = await get_setting("tibber_api_token")

    if not api_token:
        return TestConnectionResponse(
            service="tibber",
            success=False,
            message="Tibber API token is required",
        )

    query = """
    {
      viewer {
        name
        homes {
          address {
            city
          }
        }
      }
    }
    """

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tibber.com/v1-beta/gql",
                json={"query": query},
                headers=headers,
            )

        if resp.status_code == 403:
            return TestConnectionResponse(
                service="tibber",
                success=False,
                message="Invalid API token (403 Forbidden)",
            )

        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            return TestConnectionResponse(
                service="tibber",
                success=False,
                message=f"API error: {data['errors'][0].get('message', 'Unknown')}",
            )

        viewer = data.get("data", {}).get("viewer", {})
        homes = viewer.get("homes", [])

        return TestConnectionResponse(
            service="tibber",
            success=True,
            message=f"Tibber connection successful. Account: {viewer.get('name', 'N/A')}, {len(homes)} home(s).",
            details={"name": viewer.get("name"), "home_count": len(homes)},
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            service="tibber",
            success=False,
            message="Connection timed out after 30s",
        )
    except Exception as e:
        return TestConnectionResponse(
            service="tibber",
            success=False,
            message=f"Connection failed: {str(e)}",
        )


# --- Fault endpoints ---


@app.get("/api/faults")
async def get_faults(
    hours: int = Query(168, ge=1, le=8760),
    active_only: bool = Query(False),
):
    """Get fault/error history."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        query = select(FaultRecord).where(FaultRecord.ts >= since)
        if active_only:
            query = query.where(FaultRecord.resolved_at.is_(None))
        result = await session.execute(query.order_by(desc(FaultRecord.ts)))
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "device_id": r.device_id,
            "error_code": r.error_code,
            "error_message": r.error_message,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "outdoor_temp": r.outdoor_temp,
            "active": r.resolved_at is None,
        }
        for r in rows
    ]


@app.post("/api/faults/{fault_id}/resolve")
async def resolve_fault(fault_id: int):
    """Mark a fault as resolved."""
    from sqlalchemy import update

    async with get_session() as session:
        await session.execute(
            update(FaultRecord)
            .where(FaultRecord.id == fault_id)
            .values(resolved_at=dt.datetime.now(dt.timezone.utc))
        )
    return {"status": "resolved"}


# --- COP endpoints ---


@app.get("/api/cop/history")
async def get_cop_history(hours: int = Query(168, ge=1, le=8760)):
    """Get computed COP history."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(COPRecord)
            .where(COPRecord.ts >= since)
            .order_by(COPRecord.ts)
        )
        rows = result.scalars().all()

    return [
        {
            "ts": r.ts.isoformat(),
            "cop_value": r.cop_value,
            "mode": r.mode,
            "outdoor_temp": r.outdoor_temp,
            "electrical_kwh": r.electrical_kwh,
            "thermal_kwh": r.thermal_kwh,
        }
        for r in rows
    ]


@app.get("/api/cop/stats")
async def get_cop_stats(
    hours: int = Query(168, ge=1, le=8760),
    mode: Optional[str] = Query(None),
):
    """Get aggregated COP statistics."""
    from packages.ml.models import direction_cop

    return await direction_cop.get_average_cop(hours=hours, mode=mode)


@app.post("/api/cop/compute")
async def compute_cop(hours: int = Query(24, ge=1, le=168)):
    """Trigger COP computation for recent intervals."""
    from packages.ml.models import direction_cop

    intervals = await direction_cop.compute_cop_intervals(hours=hours)
    return {
        "status": "computed",
        "intervals_found": len(intervals),
        "intervals": intervals[:20],  # Return first 20
    }


# --- Compressor activity endpoint ---


@app.get("/api/compressor/activity")
async def get_compressor_activity(hours: int = Query(24, ge=1, le=168)):
    """Get compressor activity timeline (direction + action over time)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(
                DeviceStatusRecord.ts,
                DeviceStatusRecord.direction,
                DeviceStatusRecord.device_action,
                DeviceStatusRecord.defrost_active,
                DeviceStatusRecord.force_dhw,
                DeviceStatusRecord.force_heater,
                DeviceStatusRecord.outdoor_temp,
            )
            .where(DeviceStatusRecord.ts >= since)
            .order_by(DeviceStatusRecord.ts)
        )
        rows = result.all()

    return [
        {
            "ts": r.ts.isoformat(),
            "direction": r.direction,
            "action": r.device_action,
            "defrost": r.defrost_active,
            "force_dhw": r.force_dhw,
            "force_heater": r.force_heater,
            "outdoor_temp": r.outdoor_temp,
        }
        for r in rows
    ]

from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select

from packages.core.database import get_session
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    PlanActionRecord,
    PlanRecord,
    WeatherRecord,
)

router = APIRouter()


def _enforce_physical_ordering(
    forecast: list[dict],
    forecast_with_plan: list[dict],
    forecast_no_heating: list[dict],
) -> None:
    """Clamp indoor-temperature curves in place to a physically valid ordering.

    Active heating can never leave the house cooler than the no-heating
    baseline, so both the base forecast and the schedule-managed "with plan"
    forecast are clamped to never fall below ``forecast_no_heating``. This is a
    cheap safety net so the chart can never show the nonsensical "predicted
    indoor below no-heating" case (e.g. when the model is untrained and the
    linear fallback is used).

    Note: the managed forecast is intentionally allowed to dip *below* the base
    forecast overnight (it reflects the comfort-schedule setback), so it is not
    clamped up to the base — only to the no-heating floor.
    """
    key = "predicted_indoor_temp"
    n = min(len(forecast), len(forecast_with_plan), len(forecast_no_heating))
    for h in range(n):
        floor = forecast_no_heating[h][key]
        forecast[h][key] = round(max(forecast[h][key], floor), 1)
        forecast_with_plan[h][key] = round(max(forecast_with_plan[h][key], floor), 1)


@router.get("/api/comfort-model/status")
async def get_comfort_model_status():
    from packages.ml.comfort_model import comfort_model

    return {
        "trained": comfort_model.is_trained,
        "last_trained": comfort_model.last_trained.isoformat() if comfort_model.last_trained else None,
        "training_samples": comfort_model.training_samples,
        "metrics": comfort_model.metrics,
    }


@router.post("/api/comfort-model/train")
async def trigger_comfort_model_training():
    import structlog
    import traceback

    from packages.core.settings_service import get_setting
    from packages.ml.comfort_model import comfort_model

    _log = structlog.get_logger()
    lag_str = await get_setting("thermal_lag_minutes")
    lag = int(lag_str) if lag_str else None

    try:
        result = await comfort_model.train(thermal_lag_minutes=lag)
    except Exception as exc:
        _log.error("comfort_train_error", error=str(exc), traceback=traceback.format_exc())
        result = {"error": f"Training failed: {exc}"}

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="train_comfort_model",
                payload_json=json.dumps(result, default=str),
                result=result.get("status", "unknown"),
            )
        )

    return result


@router.get("/api/comfort-model/predict")
async def predict_indoor_temp(
    water_temp: float = Query(..., description="Zone water supply temperature (C)"),
    outdoor_temp: float = Query(..., description="Outdoor temperature (C)"),
    hour: int = Query(12, ge=0, le=23),
    indoor_temp: float | None = Query(None, description="Current indoor temperature (C) from SmartThings"),
):
    from packages.ml.comfort_model import comfort_model

    if not comfort_model.is_trained:
        raise HTTPException(status_code=409, detail="Comfort model not yet trained")

    indoor = comfort_model.predict_indoor_temp(
        zone_water_temp=water_temp,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
    )
    required_water = comfort_model.required_zone_temp(
        target_indoor=21.0,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
    )

    return {
        "predicted_indoor_temp": round(indoor, 1) if indoor is not None else None,
        "required_water_temp_for_21c": round(required_water, 1) if required_water is not None else None,
    }


@router.post("/api/ml/train")
async def trigger_ml_training():
    import structlog
    import traceback

    from packages.ml.models import COPModel, DemandModel

    _log = structlog.get_logger()
    cop = COPModel()
    demand = DemandModel()

    try:
        cop_result = await cop.train()
    except Exception as exc:
        _log.error("cop_train_error", error=str(exc), traceback=traceback.format_exc())
        cop_result = {"error": f"Training failed: {exc}"}

    try:
        demand_result = await demand.train()
    except Exception as exc:
        _log.error("demand_train_error", error=str(exc), traceback=traceback.format_exc())
        demand_result = {"error": f"Training failed: {exc}"}

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="train_ml_models",
                payload_json=json.dumps({"cop": cop_result, "demand": demand_result}, default=str),
                result="ok",
            )
        )

    return {"cop": cop_result, "demand": demand_result}


@router.get("/api/thermal/status")
async def get_thermal_status():
    from packages.ml.thermal import thermal_model

    async with get_session() as session:
        result = await session.execute(select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1))
        status = result.scalar_one_or_none()

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp or 48.0
    tank_target = status.tank_target_temp or 52
    outdoor = status.outdoor_temp or 7.0
    current_zone = status.zone1_temp or 20.0

    heating_pred = thermal_model.predict_tank_heating_time(current_tank, float(tank_target), outdoor)
    cooling_pred = thermal_model.predict_tank_cooling_time(current_tank, float(tank_target - 7), outdoor)
    zone_pred = thermal_model.predict_zone_heating_time(current_zone, current_zone + 2, outdoor)

    latest_indoor: float | None = None
    async with get_session() as session:
        row = (
            await session.execute(
                select(IndoorTempReading.temperature)
                .order_by(IndoorTempReading.timestamp.desc())
                .limit(1)
            )
        ).scalar()
        if row is not None:
            latest_indoor = float(row)

    current_indoor = latest_indoor or 20.0
    indoor_cooling_pred = thermal_model.predict_indoor_cooling_time(current_indoor, current_indoor - 2.0, outdoor)
    indoor_heating_pred = thermal_model.predict_indoor_heating_time(current_indoor, current_indoor + 1.0, outdoor)

    return {
        "current": {
            "tank_temp": current_tank,
            "tank_target": tank_target,
            "outdoor_temp": outdoor,
            "zone1_temp": current_zone,
            "indoor_temp": latest_indoor,
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
            "indoor": {
                "current_indoor_temp": latest_indoor,
                "minutes_to_cool_2deg": round(indoor_cooling_pred.estimated_minutes, 1)
                if indoor_cooling_pred.estimated_minutes != float("inf")
                else None,
                "minutes_to_heat_1deg": round(indoor_heating_pred.estimated_minutes, 1),
                "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
                "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
                "indoor_heating_samples": thermal_model.params.indoor_heating_samples,
                "indoor_cooling_samples": thermal_model.params.indoor_cooling_samples,
                "confidence": indoor_heating_pred.confidence,
            },
        },
        "model_params": {
            "tank_heating_rate": round(thermal_model.params.tank_heating_rate, 3),
            "tank_heating_outdoor_factor": round(thermal_model.params.tank_heating_outdoor_factor, 4),
            "tank_standby_loss": round(thermal_model.params.tank_standby_loss, 3),
            "zone_heating_rate": round(thermal_model.params.zone_heating_rate, 3),
            "zone_standby_loss": round(thermal_model.params.zone_standby_loss, 3),
            "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
            "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
            "sample_count": thermal_model.params.sample_count,
        },
    }


@router.post("/api/thermal/calibrate")
async def calibrate_thermal_model():
    from packages.ml.thermal import thermal_model

    return await thermal_model.calibrate()


@router.get("/api/thermal/curve")
async def get_thermal_curve(hours: int = Query(24, ge=1, le=72)):
    from packages.core.config import settings
    from packages.core.settings_service import (
        get_comfort_schedule,
        get_setting,
        get_user_tz,
        is_comfort_hour,
    )
    from packages.ml.thermal import thermal_model
    from packages.optimizer.executor_core import is_learning_mode_active

    async with get_session() as session:
        result = await session.execute(select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1))
        status = result.scalar_one_or_none()

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp or 48.0
    tank_target = status.tank_target_temp or 52
    outdoor = status.outdoor_temp or 7.0
    current_zone = status.zone1_temp or 20.0

    now = dt.datetime.now(dt.timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)

    # Per-hour tank reheat floor (deadband fallback), matching the optimizer
    # (milp.py): the tank may coast lower overnight/off-peak than during comfort
    # hours. Only used when there is no executable plan to follow.
    comfort_schedule = await get_comfort_schedule()
    tz_name = await get_user_tz()
    tank_min_temp = int(await get_setting("tank_min_temp") or settings.tank_min_temp)
    tank_min_temp_offpeak = int(
        await get_setting("tank_min_temp_offpeak") or settings.tank_min_temp_offpeak
    )
    tank_min_per_hour = []
    for h in range(hours):
        hour_ts = hour_start + dt.timedelta(hours=h)
        if is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
            tank_min_per_hour.append(float(tank_min_temp))
        else:
            tank_min_per_hour.append(float(tank_min_temp_offpeak))

    # Prefer the actual optimizer plan: the MILP chose *when* to reheat DHW based
    # on price/COP, so the "with heating" curve should follow those scheduled
    # cycles rather than a generic deadband. In learning mode the executor
    # dispatches nothing — the plan is created but not run, so the tank follows
    # the heat pump's native behaviour — so we fall back to the deadband estimate
    # and flag it instead of pretending the plan drives the tank.
    learning_mode = await is_learning_mode_active()
    dhw_minutes_per_hour = [0.0] * hours
    plan_id = None
    plan_driven = False
    async with get_session() as session:
        plan_result = await session.execute(
            select(PlanRecord)
            .where(PlanRecord.horizon_end > now)
            .order_by(desc(PlanRecord.created_at))
            .limit(1)
        )
        active_plan = plan_result.scalar_one_or_none()
        if active_plan:
            plan_id = active_plan.id
            if not learning_mode:
                actions_result = await session.execute(
                    select(PlanActionRecord)
                    .where(PlanActionRecord.plan_id == active_plan.id)
                    .order_by(PlanActionRecord.scheduled_ts)
                )
                for action in actions_result.scalars().all():
                    if action.action_type != "force_dhw_on":
                        continue
                    hour_offset = int((action.scheduled_ts - hour_start).total_seconds() // 3600)
                    if hour_offset < 0 or hour_offset >= hours:
                        continue
                    payload = json.loads(action.payload_json) if action.payload_json else {}
                    minutes = float(payload.get("dhw_minutes", 60))
                    dhw_minutes_per_hour[hour_offset] = min(
                        60.0, dhw_minutes_per_hour[hour_offset] + minutes
                    )
                plan_driven = any(m > 0 for m in dhw_minutes_per_hour)

    tank_standby_curve = thermal_model.predict_temperature_curve(
        current_temp=current_tank,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=None,
        is_tank=True,
    )
    if plan_driven:
        # "With heating" follows the optimizer's actual DHW schedule: reheat
        # during planned hot-water cycles, coast on standby loss in between.
        tank_heating_curve = thermal_model.predict_planned_tank_curve(
            current_temp=current_tank,
            outdoor_temp=outdoor,
            tank_target=float(tank_target),
            dhw_minutes_per_hour=dhw_minutes_per_hour,
            hours=hours,
        )
    else:
        # No executable plan (or learning mode): fall back to the comfort-schedule
        # deadband — coast to the per-hour floor and reheat to target.
        tank_heating_curve = thermal_model.predict_managed_tank_curve(
            current_temp=current_tank,
            outdoor_temp=outdoor,
            tank_target=float(tank_target),
            tank_min_per_hour=tank_min_per_hour,
            hours=hours,
        )
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
            "tank_min_temp": tank_min_temp,
            "tank_min_temp_offpeak": tank_min_temp_offpeak,
            "plan_driven": plan_driven,
            "learning_mode": learning_mode,
            "plan_id": plan_id,
        },
        "curves": {
            "tank_standby": tank_standby_curve,
            "tank_heating": tank_heating_curve,
            "zone_standby": zone_standby_curve,
        },
    }


@router.get("/api/thermal/indoor-forecast")
async def get_indoor_forecast(hours: int = Query(24, ge=1, le=48)):
    from packages.core.settings_service import (
        get_comfort_schedule,
        get_setting,
        get_user_tz,
        is_comfort_hour,
    )
    from packages.ml.thermal import thermal_model

    async with get_session() as session:
        status_result = await session.execute(select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1))
        status = status_result.scalar_one_or_none()

        indoor_row = (
            await session.execute(
                select(IndoorTempReading.temperature)
                .order_by(IndoorTempReading.timestamp.desc())
                .limit(1)
            )
        ).scalar()

        now = dt.datetime.now(dt.timezone.utc)
        weather_result = await session.execute(
            select(WeatherRecord).where(WeatherRecord.ts >= now).order_by(WeatherRecord.ts).limit(hours)
        )
        weather_records = weather_result.scalars().all()

    if not status:
        return {"error": "No device data available"}

    current_indoor = float(indoor_row) if indoor_row is not None else 20.0
    outdoor = status.outdoor_temp or 7.0
    current_zone = status.zone1_temp or 35.0

    weather_forecast = []
    for i in range(hours):
        if i < len(weather_records):
            w = weather_records[i]
            weather_forecast.append(
                {
                    "outdoor_temp": w.temperature if w.temperature is not None else outdoor,
                    "wind_speed": w.wind_speed if w.wind_speed is not None else 3.0,
                    "irradiance": getattr(w, "irradiance", 0.0) or 0.0,
                    "hour": w.ts.hour,
                }
            )
        else:
            weather_forecast.append(
                {
                    "outdoor_temp": outdoor,
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "hour": (now.hour + i) % 24,
                }
            )

    zone_water_temps = [current_zone] * hours
    planned_actions = []
    async with get_session() as session:
        plan_result = await session.execute(
            select(PlanRecord)
            .where(PlanRecord.horizon_end > now)
            .order_by(desc(PlanRecord.created_at))
            .limit(1)
        )
        active_plan = plan_result.scalar_one_or_none()

        if active_plan:
            actions_result = await session.execute(
                select(PlanActionRecord)
                .where(PlanActionRecord.plan_id == active_plan.id)
                .order_by(PlanActionRecord.scheduled_ts)
            )
            plan_actions = actions_result.scalars().all()
            hour_start = now.replace(minute=0, second=0, microsecond=0)
            for action in plan_actions:
                hour_offset = int((action.scheduled_ts - hour_start).total_seconds() / 3600)
                if hour_offset < 0 or hour_offset >= hours:
                    continue

                if action.action_type in (
                    "zone_temp_boost",
                    "comfort_mode_on",
                    "zone_temp_restore",
                    "eco_mode_on",
                    "eco_mode_off",
                    "normal_mode_on",
                    "force_dhw_on",
                    "force_dhw_off",
                    "quiet_mode_on",
                    "quiet_mode_off",
                ):
                    payload = json.loads(action.payload_json) if action.payload_json else {}
                    planned_actions.append(
                        {
                            "hour": hour_offset,
                            "action_type": action.action_type,
                            "status": action.status,
                            "payload": payload,
                        }
                    )

    # Schedule-aware comfort setpoint per hour, matching the optimizer: the home
    # is held near comfort_temp_target during comfort hours and allowed to set
    # back toward comfort_temp_min overnight/off-peak.
    comfort_schedule = await get_comfort_schedule()
    comfort_temp_target = float(await get_setting("comfort_temp_target") or 20.5)
    comfort_temp_min_val = float(await get_setting("comfort_temp_min") or 18.0)
    tz_name = await get_user_tz()

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    target_schedule = []
    indoor_target_per_hour = []
    for h in range(hours):
        hour_ts = hour_start + dt.timedelta(hours=h)
        in_comfort = is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name)
        target = comfort_temp_target if in_comfort else comfort_temp_min_val
        indoor_target_per_hour.append(target)
        target_schedule.append(
            {
                "hour": h + 1,
                "target": target,
                "comfort_hour": in_comfort,
            }
        )

    # "Predicted Indoor" = the home under the optimizer's schedule-aware control,
    # coasting toward the off-peak setback overnight and reheating during comfort
    # hours, rather than being held flat at the current zone temp.
    forecast_with_plan = thermal_model.predict_managed_indoor_curve(
        current_indoor=current_indoor,
        indoor_target_per_hour=indoor_target_per_hour,
        weather_forecast=weather_forecast,
        hours=hours,
    )
    # Base reference: indoor if the current zone water temp were held constantly
    # (full heating, no setback). Not plotted directly, but used as the upper
    # reference and to drive the frontend's per-hour iteration length.
    forecast = thermal_model.predict_indoor_curve(
        current_indoor=current_indoor,
        zone_water_temps=zone_water_temps,
        weather_forecast=weather_forecast,
        hours=hours,
    )
    forecast_no_heating = thermal_model.predict_indoor_curve(
        current_indoor=current_indoor,
        zone_water_temps=[outdoor] * hours,
        weather_forecast=weather_forecast,
        hours=hours,
    )

    _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

    return {
        "current_indoor": current_indoor,
        "outdoor_temp": outdoor,
        "forecast": forecast,
        "forecast_with_plan": forecast_with_plan,
        "forecast_no_heating": forecast_no_heating,
        "target_schedule": target_schedule,
        "planned_actions": planned_actions,
    }


@router.get("/health")
async def health():
    from sqlalchemy import text

    from packages.core.database import engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unreachable")

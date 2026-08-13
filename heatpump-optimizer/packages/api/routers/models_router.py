from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import desc, select

from packages.api._helpers import get_price_area
from packages.api.schemas import IndoorForecastResponse
from packages.core.database import get_session
from packages.core.plan_lifecycle import active_plan_query
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    PlanActionRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.ml.forecast_quality import get_forecast_scorecard as build_forecast_scorecard

router = APIRouter()


@router.get("/api/thermal/forecast-scorecard")
async def get_forecast_scorecard():
    """Score immutable indoor forecasts against later sensor observations."""
    return await build_forecast_scorecard()


@router.get("/api/sensors/diagnostics")
async def get_sensor_diagnostics(hours: int = Query(168, ge=1, le=720)):
    """Return observation-only SmartThings sensor diagnostics."""
    from packages.core.sensor_diagnostics import summarize_sensor_diagnostics
    from packages.core.settings_service import get_setting

    now = dt.datetime.now(dt.timezone.utc)
    reference_sensor_id = (await get_setting("comfort_reference_sensor_id")).strip()
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(IndoorTempReading)
                    .where(IndoorTempReading.timestamp >= now - dt.timedelta(hours=hours))
                    .order_by(IndoorTempReading.timestamp)
                )
            )
            .scalars()
            .all()
        )
    return summarize_sensor_diagnostics(rows, reference_sensor_id=reference_sensor_id, now=now)


@router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Expose a tiny Prometheus-compatible operational surface.

    It intentionally contains aggregate health signals only: no device ids,
    credentials, raw indoor readings, or plan payloads leave the API here.
    """

    from packages.core.operational_alerts import get_operational_alerts

    alerts = await get_operational_alerts()
    summary = alerts.get("summary", {}) if isinstance(alerts, dict) else {}
    critical = int(summary.get("critical", 0)) if isinstance(summary, dict) else 0
    warning = int(summary.get("warning", 0)) if isinstance(summary, dict) else 0
    enabled = 1 if alerts.get("enabled") else 0
    return PlainTextResponse(
        "\n".join(
            (
                "# HELP heatpump_optimizer_operational_alerts Active operational alerts by severity.",
                "# TYPE heatpump_optimizer_operational_alerts gauge",
                f'heatpump_optimizer_operational_alerts{{severity="critical"}} {critical}',
                f'heatpump_optimizer_operational_alerts{{severity="warning"}} {warning}',
                "# HELP heatpump_optimizer_operational_alerting_enabled Whether in-app operational alerts are enabled.",
                "# TYPE heatpump_optimizer_operational_alerting_enabled gauge",
                f"heatpump_optimizer_operational_alerting_enabled {enabled}",
                "",
            )
        ),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _plan_forecast_window(
    plan_json: str,
    forecast_start: dt.datetime,
    hours: int,
) -> dict[str, Any] | None:
    """Return the stored plan snapshot as a window relative to the current hour."""
    try:
        payload = json.loads(plan_json)
        snapshot = payload.get("forecast_snapshot") if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get("version") not in {
        "indoor_forecast_v1",
        "indoor_forecast_v2",
    }:
        return None

    if snapshot.get("forecast_status") == "unavailable":
        return {
            "current_indoor": None,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": snapshot.get("target_schedule", []),
            "weather_forecast": snapshot.get("weather_forecast", []),
            "price_forecast": snapshot.get("price_forecast", []),
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": snapshot.get("forecast_unavailable_reason"),
        }

    keys = (
        "forecast",
        "forecast_with_plan",
        "forecast_no_heating",
        "target_schedule",
        "weather_forecast",
        "price_forecast",
    )
    sequences = [snapshot.get(key) for key in keys]
    if not all(isinstance(sequence, list) for sequence in sequences):
        return None

    indexes: list[int] = []
    for index, point in enumerate(snapshot["forecast_with_plan"]):
        if not isinstance(point, dict) or not isinstance(point.get("ts"), str):
            return None
        try:
            state_ts = dt.datetime.fromisoformat(point["ts"])
        except ValueError:
            return None
        if state_ts.tzinfo is None:
            state_ts = state_ts.replace(tzinfo=dt.timezone.utc)
        if state_ts >= forecast_start:
            indexes.append(index)
        if len(indexes) == hours:
            break

    if not indexes or any(index >= len(sequence) for index in indexes for sequence in sequences):
        return None

    def curve(key: str) -> list[dict[str, Any]]:
        result = []
        for relative_hour, index in enumerate(indexes, start=1):
            source = snapshot[key][index]
            if not isinstance(source, dict) or source.get("predicted_indoor_temp") is None:
                return []
            result.append(
                {
                    "hour": relative_hour,
                    "ts": source.get("ts"),
                    "predicted_indoor_temp": source["predicted_indoor_temp"],
                    "source": source.get("source"),
                    "model_source": source.get("model_source"),
                    "space_heating_fraction": source.get("space_heating_fraction"),
                    "prediction_lower_c": source.get("prediction_lower_c"),
                    "prediction_upper_c": source.get("prediction_upper_c"),
                    "prediction_interval_status": source.get("prediction_interval_status"),
                }
            )
        return result

    forecast = curve("forecast")
    with_plan = curve("forecast_with_plan")
    no_heating = curve("forecast_no_heating")
    if not forecast or not with_plan or not no_heating:
        return None

    targets = []
    weather = []
    prices = []
    for relative_hour, index in enumerate(indexes, start=1):
        target = snapshot["target_schedule"][index]
        weather_row = snapshot["weather_forecast"][index]
        price = snapshot["price_forecast"][index]
        if not all(isinstance(row, dict) for row in (target, weather_row, price)):
            return None
        if target.get("target") is None or weather_row.get("ts") is None or price.get("ts") is None:
            return None
        targets.append(
            {
                "hour": relative_hour,
                "ts": target.get("ts"),
                "target": target["target"],
                "comfort_hour": bool(target.get("comfort_hour", target["target"] >= 20.0)),
            }
        )
        weather.append(
            {
                "ts": weather_row["ts"],
                "hour": weather_row.get("hour", 0),
                "outdoor_temp": weather_row.get("outdoor_temp"),
                "wind_speed": weather_row.get("wind_speed"),
                "irradiance": weather_row.get("irradiance"),
                "precipitation": weather_row.get("precipitation"),
                "input_status": weather_row.get("input_status", "observed"),
                "imputed_fields": weather_row.get("imputed_fields", []),
            }
        )
        prices.append(
            {
                "ts": price["ts"],
                "price_eur_per_kwh": price.get("price_eur_per_kwh"),
                "price_per_kwh": price.get("price_per_kwh", price.get("price_eur_per_kwh")),
                "currency": price.get("currency"),
            }
        )

    try:
        current_indoor = float(snapshot.get("current_indoor"))
    except (TypeError, ValueError):
        current_indoor = None

    return {
        "current_indoor": current_indoor,
        "forecast": forecast,
        "forecast_with_plan": with_plan,
        "forecast_no_heating": no_heating,
        "target_schedule": targets,
        "weather_forecast": weather,
        "price_forecast": prices,
        "forecast_status": "available",
        "forecast_unavailable_reason": None,
        "forecast_provenance": {
            "control_input": snapshot.get("control_input", {}),
            "heat_curve": snapshot.get("heat_curve", {}),
        },
    }


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
        "control_ready": comfort_model.is_ready_for_control,
        "control_readiness": comfort_model.control_readiness,
        "last_trained": comfort_model.last_trained.isoformat()
        if comfort_model.last_trained
        else None,
        "training_samples": comfort_model.training_samples,
        "metrics": comfort_model.metrics,
        "training_notice": comfort_model.training_notice,
        "control_margin_c": comfort_model.control_margin_c,
        "passive_forecast": {
            str(horizon): comfort_model.passive_forecast_readiness(horizon)
            for horizon in (60, 180, 360, 720)
        },
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
        result["control_ready"] = comfort_model.is_ready_for_control
        result["control_readiness"] = comfort_model.control_readiness
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
    indoor_temp: float | None = Query(
        None, description="Current indoor temperature (C) from SmartThings"
    ),
    precipitation: float = Query(
        0.0, ge=0, description="Precipitation in mm for the forecast hour"
    ),
    humidity: float = Query(60.0, ge=0, le=100, description="Relative humidity (%)"),
    cloud_cover: float = Query(0.5, ge=0, le=1, description="Cloud cover fraction (0-1)"),
):
    from packages.ml.comfort_model import comfort_model

    if not comfort_model.is_trained:
        raise HTTPException(status_code=409, detail="Comfort model not yet trained")

    indoor = comfort_model.predict_indoor_temp(
        zone_water_temp=water_temp,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
        precipitation=precipitation,
        humidity=humidity,
        cloud_cover=cloud_cover,
    )
    required_water = comfort_model.required_zone_temp(
        target_indoor=21.0,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
        precipitation=precipitation,
        humidity=humidity,
        cloud_cover=cloud_cover,
    )

    return {
        "predicted_indoor_temp": round(indoor, 1) if indoor is not None else None,
        "required_water_temp_for_21c": round(required_water, 1)
        if required_water is not None
        else None,
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
    from packages.core.control_temperature import get_control_temperature
    from packages.ml.thermal import thermal_model

    thermal_model.load_latest()
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()

    if not status:
        return {
            "current_indoor": None,
            "outdoor_temp": None,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": [],
            "price_forecast": [],
            "planned_actions": [],
            "forecast_source": "unavailable",
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": "no_device_data",
            "comfort_assessment": {
                "state": "unavailable",
                "summary": "No recent heat-pump status is available.",
                "misses": [],
                "recommendations": [],
            },
            "forecast_provenance": {"device_status": "missing"},
            "display_status": "unavailable",
        }

    current_tank = status.tank_temp if status.tank_temp is not None else 48.0
    tank_target = status.tank_target_temp if status.tank_target_temp is not None else 52
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    current_zone = status.zone1_temp if status.zone1_temp is not None else 20.0

    heating_pred = thermal_model.predict_tank_heating_time(
        current_tank, float(tank_target), outdoor
    )
    cooling_pred = thermal_model.predict_tank_cooling_time(
        current_tank, float(tank_target - 7), outdoor
    )
    zone_pred = thermal_model.predict_zone_heating_time(current_zone, current_zone + 2, outdoor)

    async with get_session() as session:
        control_temperature = await get_control_temperature(session=session)

    latest_indoor = float(control_temperature.value) if control_temperature.is_usable else None
    indoor_cooling_pred = (
        thermal_model.predict_indoor_cooling_time(latest_indoor, latest_indoor - 2.0, outdoor)
        if latest_indoor is not None
        else None
    )
    indoor_heating_pred = (
        thermal_model.predict_indoor_heating_time(latest_indoor, latest_indoor + 1.0, outdoor)
        if latest_indoor is not None
        else None
    )

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
                if indoor_cooling_pred is not None
                and indoor_cooling_pred.estimated_minutes != float("inf")
                else None,
                "minutes_to_heat_1deg": (
                    round(indoor_heating_pred.estimated_minutes, 1)
                    if indoor_heating_pred is not None
                    else None
                ),
                "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
                "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
                "indoor_heating_samples": thermal_model.params.indoor_heating_samples,
                "indoor_cooling_samples": thermal_model.params.indoor_cooling_samples,
                "confidence": (
                    indoor_heating_pred.confidence
                    if indoor_heating_pred is not None
                    else "unavailable"
                ),
                "reason": control_temperature.reason if latest_indoor is None else None,
                "heating_confidence": thermal_model.confidence_for("indoor_heating"),
                "cooling_confidence": thermal_model.confidence_for("indoor_cooling"),
            },
        },
        "model_params": {
            "tank_heating_rate": round(thermal_model.params.tank_heating_rate, 3),
            "tank_heating_outdoor_factor": round(
                thermal_model.params.tank_heating_outdoor_factor, 4
            ),
            "tank_standby_loss": round(thermal_model.params.tank_standby_loss, 3),
            "zone_heating_rate": round(thermal_model.params.zone_heating_rate, 3),
            "zone_standby_loss": round(thermal_model.params.zone_standby_loss, 3),
            "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
            "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
            "sample_count": thermal_model.params.sample_count,
            "calibration_status": thermal_model.params.calibration_status,
            "component_confidence": {
                "tank_heating": thermal_model.confidence_for("tank_heating"),
                "zone_heating": thermal_model.confidence_for("zone_heating"),
                "indoor_heating": thermal_model.confidence_for("indoor_heating"),
                "indoor_cooling": thermal_model.confidence_for("indoor_cooling"),
            },
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

    thermal_model.load_latest()
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp if status.tank_temp is not None else 48.0
    tank_target = status.tank_target_temp if status.tank_target_temp is not None else 52
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    current_zone = status.zone1_temp if status.zone1_temp is not None else 20.0

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
        plan_result = await session.execute(active_plan_query(now))
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


@router.get("/api/thermal/heat-curve-advice")
async def get_heat_curve_advice():
    """Explain a small, manual Panasonic curve adjustment from live comfort data."""
    from packages.core.heat_curve import (
        HEAT_CURVE_SETTING_KEYS,
        evaluate_heat_curve_verification,
        heat_curve_advice,
        start_heat_curve_verification,
    )
    from packages.core.settings_service import (
        get_float_setting,
        get_heat_curve_config,
        get_heat_curve_verification_state,
        set_heat_curve_verification_state,
    )

    heat_curve = await get_heat_curve_config()
    comfort_target = await get_float_setting("comfort_temp_target")
    verification_state = await get_heat_curve_verification_state()
    now = dt.datetime.now(dt.timezone.utc)

    async with get_session() as session:
        status = (
            await session.execute(
                select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
            )
        ).scalar_one_or_none()
        indoor_temp = (
            await session.execute(
                select(IndoorTempReading.temperature)
                .order_by(IndoorTempReading.timestamp.desc())
                .limit(1)
            )
        ).scalar()

        # The first deployment of this feature may happen just after a user
        # saved a curve. Recover a recent matching audit entry so that change
        # begins its evidence window instead of receiving another suggestion.
        if not verification_state:
            audit_rows = (
                (
                    await session.execute(
                        select(AuditLogRecord)
                        .where(AuditLogRecord.action == "update_settings")
                        .order_by(desc(AuditLogRecord.ts))
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            recent_curve_audit = None
            for audit in audit_rows:
                if audit.ts < now - dt.timedelta(days=7):
                    break
                try:
                    payload = json.loads(audit.payload_json or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and any(
                    key in payload for key in HEAT_CURVE_SETTING_KEYS
                ):
                    recent_curve_audit = audit
                    break
            if recent_curve_audit is not None:
                baseline_indoor = (
                    await session.execute(
                        select(IndoorTempReading.temperature)
                        .where(IndoorTempReading.timestamp <= recent_curve_audit.ts)
                        .order_by(IndoorTempReading.timestamp.desc())
                        .limit(1)
                    )
                ).scalar()
                baseline_status = (
                    await session.execute(
                        select(DeviceStatusRecord)
                        .where(DeviceStatusRecord.ts <= recent_curve_audit.ts)
                        .order_by(desc(DeviceStatusRecord.ts))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                verification_state = start_heat_curve_verification(
                    started_at=recent_curve_audit.ts.isoformat(),
                    previous_curve=None,
                    applied_curve=heat_curve,
                    baseline_indoor_temp=(
                        float(baseline_indoor) if baseline_indoor is not None else None
                    ),
                    baseline_outdoor_temp=(
                        float(baseline_status.outdoor_temp)
                        if baseline_status is not None and baseline_status.outdoor_temp is not None
                        else None
                    ),
                    comfort_target=comfort_target,
                    source="audit_recovery",
                )

        try:
            started_at = dt.datetime.fromisoformat(str(verification_state.get("started_at")))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=dt.timezone.utc)
        except (AttributeError, TypeError, ValueError):
            started_at = None

        indoor_samples: list[float] = []
        heating_condition_samples = 0
        if started_at is not None and verification_state.get("status") != "verified":
            indoor_samples = [
                float(value)
                for value in (
                    await session.execute(
                        select(IndoorTempReading.temperature)
                        .where(IndoorTempReading.timestamp > started_at)
                        .order_by(IndoorTempReading.timestamp)
                    )
                )
                .scalars()
                .all()
            ]
            status_rows = (
                (
                    await session.execute(
                        select(DeviceStatusRecord.outdoor_temp).where(
                            DeviceStatusRecord.ts > started_at,
                            DeviceStatusRecord.outdoor_temp.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            heating_condition_samples = sum(
                1
                for temperature in status_rows
                if float(temperature) < heat_curve.heating_off_outdoor_c
            )

    outdoor_temp = (
        float(status.outdoor_temp)
        if status is not None and status.outdoor_temp is not None
        else None
    )
    elapsed_hours = (now - started_at).total_seconds() / 3600 if started_at is not None else 0.0
    verification = evaluate_heat_curve_verification(
        verification_state,
        elapsed_hours=elapsed_hours,
        indoor_samples=indoor_samples,
        heating_condition_samples=heating_condition_samples,
        current_comfort_target=comfort_target,
    )
    if (
        verification_state
        and verification["status"] == "verified"
        and verification_state.get("status") != "verified"
    ):
        verification["completed_at"] = now.isoformat()
        await set_heat_curve_verification_state(verification)
    elif verification_state and verification_state.get("source") == "audit_recovery":
        await set_heat_curve_verification_state(verification_state)

    readings = {
        "indoor_temp_c": float(indoor_temp) if indoor_temp is not None else None,
        "comfort_target_c": comfort_target,
        "outdoor_temp_c": outdoor_temp,
        "curve_supply_target_c": (
            round(heat_curve.supply_temperature(outdoor_temp), 1)
            if outdoor_temp is not None
            else None
        ),
        "controller_heating_enabled": (
            outdoor_temp < heat_curve.heating_off_outdoor_c if outdoor_temp is not None else None
        ),
    }
    if not verification["recommendation_available"]:
        return {
            "status": "verification_pending",
            "indoor_error_c": (
                round(float(indoor_temp) - comfort_target, 1) if indoor_temp is not None else None
            ),
            "current": heat_curve.as_dict(),
            "suggested": None,
            "reasons": verification["reasons"],
            "manual_only": True,
            "readings": readings,
            "verification": verification,
        }
    advice = heat_curve_advice(
        heat_curve,
        float(indoor_temp) if indoor_temp is not None else None,
        comfort_target,
        outdoor_temp,
    )
    return {
        **advice,
        "readings": readings,
        "verification": verification,
    }


@router.get("/api/thermal/indoor-forecast", response_model=IndoorForecastResponse)
async def get_indoor_forecast(hours: int = Query(24, ge=1, le=48)):
    from packages.core.comfort_assessment import build_comfort_assessment
    from packages.core.control_temperature import get_control_temperature
    from packages.core.settings_service import (
        get_comfort_schedule,
        get_heat_curve_config,
        get_setting,
        get_user_tz,
        is_comfort_hour,
    )
    from packages.ml.thermal import thermal_model

    thermal_model.load_latest()
    price_area = await get_price_area()
    async with get_session() as session:
        status_result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = status_result.scalar_one_or_none()

        control_temperature = await get_control_temperature(session=session)

        now = dt.datetime.now(dt.timezone.utc)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        forecast_start = hour_start + dt.timedelta(hours=1)
        forecast_end = forecast_start + dt.timedelta(hours=hours)
        weather_result = await session.execute(
            select(WeatherRecord)
            .where(WeatherRecord.ts >= forecast_start, WeatherRecord.ts < forecast_end)
            .order_by(WeatherRecord.ts)
        )
        weather_records = weather_result.scalars().all()
        price_result = await session.execute(
            select(PriceRecord)
            .where(
                PriceRecord.ts >= forecast_start,
                PriceRecord.ts < forecast_end,
                PriceRecord.area == price_area,
            )
            .order_by(PriceRecord.ts)
        )
        price_records = price_result.scalars().all()

    if not status:
        return {"error": "No device data available"}

    current_indoor = float(control_temperature.value) if control_temperature.is_usable else None
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    weather_by_timestamp = {
        record.ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0): record
        for record in weather_records
    }
    price_by_timestamp = {
        record.ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0): record
        for record in price_records
    }
    weather_forecast = []
    for i in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=i)
        w = weather_by_timestamp.get(forecast_ts)
        imputed_fields: list[str] = []
        if w is not None:
            if w.temperature is None:
                imputed_fields.append("outdoor_temp")
            if w.wind_speed is None:
                imputed_fields.append("wind_speed")
            if getattr(w, "irradiance", None) is None:
                imputed_fields.append("irradiance")
            if getattr(w, "precipitation", None) is None:
                imputed_fields.append("precipitation")
            weather_forecast.append(
                {
                    "ts": forecast_ts.isoformat(),
                    "outdoor_temp": float(w.temperature)
                    if w.temperature is not None
                    else float(outdoor),
                    "wind_speed": float(w.wind_speed) if w.wind_speed is not None else 3.0,
                    "irradiance": float(getattr(w, "irradiance", 0.0) or 0.0),
                    "precipitation": (
                        float(w.precipitation)
                        if getattr(w, "precipitation", None) is not None
                        else None
                    ),
                    "hour": forecast_ts.hour,
                    "input_status": "imputed" if imputed_fields else "observed",
                    "imputed_fields": imputed_fields,
                }
            )
        else:
            weather_forecast.append(
                {
                    "ts": forecast_ts.isoformat(),
                    "outdoor_temp": float(outdoor),
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "precipitation": None,
                    "hour": forecast_ts.hour,
                    "input_status": "imputed",
                    "imputed_fields": [
                        "outdoor_temp",
                        "wind_speed",
                        "irradiance",
                        "precipitation",
                    ],
                }
            )

    heat_curve = await get_heat_curve_config()
    # In the no-plan fallback, use the same outdoor-to-supply curve that the
    # Panasonic controller is set to, rather than freezing today's zone reading
    # across the whole forecast horizon.
    zone_water_temps = [
        heat_curve.planned_supply_temperature(row["outdoor_temp"]) for row in weather_forecast
    ]
    planned_actions = []
    plan_snapshot = None
    active_plan = None
    async with get_session() as session:
        plan_result = await session.execute(active_plan_query(now))
        active_plan = plan_result.scalar_one_or_none()

        if active_plan:
            plan_snapshot = _plan_forecast_window(active_plan.plan_json, forecast_start, hours)
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

    if plan_snapshot is not None and active_plan is not None:
        snapshot_indoor = plan_snapshot.get("current_indoor")
        snapshot_weather = plan_snapshot.get("weather_forecast", [])
        plan_age_seconds = max(0, round((now - active_plan.created_at).total_seconds()))
        sensor_age_seconds = (
            max(0, round((now - control_temperature.latest_reading).total_seconds()))
            if control_temperature.latest_reading is not None
            else None
        )
        current_vs_plan_delta_c = (
            round(float(current_indoor) - float(snapshot_indoor), 1)
            if current_indoor is not None and snapshot_indoor is not None
            else None
        )
        if not control_temperature.is_usable:
            display_status = "stale"
        elif current_vs_plan_delta_c is not None and abs(current_vs_plan_delta_c) >= 1.0:
            display_status = "diverged"
        elif plan_age_seconds > 6 * 3600:
            display_status = "stale"
        elif plan_age_seconds > 2 * 3600:
            display_status = "aging"
        else:
            display_status = "fresh"
        assessment = build_comfort_assessment(
            forecast=plan_snapshot.get("forecast_with_plan", []),
            targets=plan_snapshot.get("target_schedule", []),
            weather=snapshot_weather,
            planned_actions=planned_actions,
            heat_curve=heat_curve,
            forecast_status=plan_snapshot.get("forecast_status", "available"),
        )
        return {
            "current_indoor": snapshot_indoor if snapshot_indoor is not None else current_indoor,
            "outdoor_temp": snapshot_weather[0]["outdoor_temp"] if snapshot_weather else outdoor,
            "forecast": plan_snapshot.get("forecast", []),
            "forecast_with_plan": plan_snapshot.get("forecast_with_plan", []),
            "forecast_no_heating": plan_snapshot.get("forecast_no_heating", []),
            "target_schedule": plan_snapshot.get("target_schedule", []),
            "weather_forecast": snapshot_weather,
            "price_forecast": plan_snapshot.get("price_forecast", []),
            "planned_actions": planned_actions,
            "forecast_source": "active_plan"
            if plan_snapshot.get("forecast_status") != "unavailable"
            else "unavailable",
            "forecast_status": plan_snapshot.get("forecast_status", "available"),
            "forecast_unavailable_reason": plan_snapshot.get("forecast_unavailable_reason"),
            "plan_id": active_plan.id,
            "plan_created_at": active_plan.created_at,
            "comfort_assessment": assessment,
            "forecast_provenance": {
                **(plan_snapshot.get("forecast_provenance", {})),
                "current_live_indoor_c": current_indoor,
                "plan_start_indoor_c": snapshot_indoor,
                "plan_created_at": active_plan.created_at.isoformat(),
            },
            "display_status": display_status,
            "plan_age_seconds": plan_age_seconds,
            "sensor_age_seconds": sensor_age_seconds,
            "current_vs_plan_delta_c": current_vs_plan_delta_c,
        }

    if current_indoor is None:
        return {
            "current_indoor": None,
            "outdoor_temp": outdoor,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": weather_forecast,
            "price_forecast": [],
            "planned_actions": planned_actions,
            "forecast_source": "unavailable",
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": control_temperature.reason
            or "no_trusted_indoor_observation",
            "plan_id": active_plan.id if active_plan else None,
            "plan_created_at": active_plan.created_at if active_plan else None,
            "display_status": "unavailable",
            "sensor_age_seconds": (
                max(0, round((now - control_temperature.latest_reading).total_seconds()))
                if control_temperature.latest_reading is not None
                else None
            ),
        }

    # Schedule-aware comfort setpoint per hour, matching the optimizer: the home
    # is held near comfort_temp_target during comfort hours and allowed to set
    # back toward comfort_temp_min overnight/off-peak.
    comfort_schedule = await get_comfort_schedule()
    comfort_temp_target = float(await get_setting("comfort_temp_target") or 20.5)
    comfort_temp_min_val = float(await get_setting("comfort_temp_min") or 18.0)
    tz_name = await get_user_tz()

    target_schedule = []
    indoor_target_per_hour = []
    for h in range(hours):
        hour_ts = hour_start + dt.timedelta(hours=h + 1)
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
    # "No heating" baseline: the home with the heat pump fully off, drifting
    # toward outdoor through its envelope. A pure physical free-float, rather
    # than the ML comfort model fed a synthetic water_temp=outdoor (which
    # extrapolated and drifted the baseline upward in summer).
    forecast_no_heating = thermal_model.predict_free_float_curve(
        current_indoor=current_indoor,
        weather_forecast=weather_forecast,
        hours=hours,
    )

    _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

    for index in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=index)
        for curve in (forecast, forecast_with_plan, forecast_no_heating):
            if index < len(curve):
                curve[index]["ts"] = forecast_ts.isoformat()

    price_forecast = []
    for index in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=index)
        price = price_by_timestamp.get(forecast_ts)
        price_forecast.append(
            {
                "ts": forecast_ts.isoformat(),
                "price_eur_per_kwh": float(price.price_eur_per_kwh) if price else None,
            }
        )

    return {
        "current_indoor": current_indoor,
        "outdoor_temp": outdoor,
        "forecast": forecast,
        "forecast_with_plan": forecast_with_plan,
        "forecast_no_heating": forecast_no_heating,
        "target_schedule": target_schedule,
        "weather_forecast": weather_forecast,
        "price_forecast": price_forecast,
        "planned_actions": planned_actions,
        "forecast_source": "live_estimate",
        "forecast_status": "available",
        "forecast_unavailable_reason": None,
        "plan_id": active_plan.id if active_plan else None,
        "plan_created_at": active_plan.created_at if active_plan else None,
        "comfort_assessment": build_comfort_assessment(
            forecast=forecast_with_plan,
            targets=target_schedule,
            weather=weather_forecast,
            planned_actions=planned_actions,
            heat_curve=heat_curve,
        ),
        "forecast_provenance": {
            "current_live_indoor_c": current_indoor,
            "control_input": {
                "confidence": control_temperature.confidence,
                "reference_sensor_id": control_temperature.reference_sensor_id,
                "reference_sensor_label": control_temperature.reference_sensor_label,
                "observed_at": (
                    control_temperature.latest_reading.isoformat()
                    if control_temperature.latest_reading
                    else None
                ),
            },
            "heat_curve": heat_curve.as_dict(),
        },
        "display_status": (
            "degraded"
            if any(row.get("input_status") == "imputed" for row in weather_forecast)
            else "fresh"
        ),
        "sensor_age_seconds": (
            max(0, round((now - control_temperature.latest_reading).total_seconds()))
            if control_temperature.latest_reading is not None
            else None
        ),
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


@router.get("/health/ready")
async def readiness():
    """Readiness includes the data-collection and control service freshness."""

    import os

    from packages.core.config import settings
    from packages.core.models import ServiceHeartbeatRecord

    now = dt.datetime.now(dt.timezone.utc)
    service_cutoff = now - dt.timedelta(minutes=3)
    backup_max_age_seconds = int(os.getenv("BACKUP_MAX_AGE_SECONDS", str(26 * 3600)))
    backup_cutoff = now - dt.timedelta(seconds=backup_max_age_seconds)
    device_max_age_seconds = max(int(settings.poll_interval_seconds) * 3, 15 * 60)
    device_cutoff = now - dt.timedelta(seconds=device_max_age_seconds)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(ServiceHeartbeatRecord).where(
                        ServiceHeartbeatRecord.service.in_(["poller", "optimizer", "backup"])
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_device_status = (
            await session.execute(
                select(DeviceStatusRecord.ts).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()

    by_service = {row.service: row.updated_at for row in rows}
    from packages.core.planning_data_quality import get_planning_data_quality

    planning_data_quality = await get_planning_data_quality(now=now)
    stale = [
        service
        for service, cutoff in {
            "poller": service_cutoff,
            "optimizer": service_cutoff,
            "backup": backup_cutoff,
        }.items()
        if by_service.get(service) is None or by_service[service] < cutoff
    ]
    if latest_device_status is None or latest_device_status < device_cutoff:
        stale.append("device_status")
    if not planning_data_quality["control_allowed"]:
        stale.append("planning_inputs")
    if stale:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "stale": stale,
                "latest_device_status": latest_device_status.isoformat()
                if latest_device_status
                else None,
                "planning_data_quality": planning_data_quality,
            },
        )
    return {
        "status": "ready",
        "services": {service: by_service[service].isoformat() for service in by_service},
        "data": {
            "latest_device_status": latest_device_status.isoformat(),
            "age_seconds": round((now - latest_device_status).total_seconds()),
            "stale_after_seconds": device_max_age_seconds,
        },
        "backup": {
            "last_success": by_service["backup"].isoformat(),
            "stale_after_seconds": backup_max_age_seconds,
        },
        "planning_data_quality": planning_data_quality,
    }

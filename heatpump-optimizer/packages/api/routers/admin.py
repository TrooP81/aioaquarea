from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
import structlog

from packages.core.database import get_session
from packages.core.models import (
    AppLogRecord,
    AuditLogRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    FaultRecord,
    IndoorTempReading,
    ManualCOPObservationRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    SettingRecord,
    ShowerEventRecord,
    WeatherRecord,
)
from packages.core.safety_reverts import unresolved_revert_predicate
from packages.ml.lifecycle import invalidate_lifecycle

router = APIRouter()
logger = structlog.get_logger()

# Each scope maps to the tables it clears, ordered so that child rows are
# removed before any parent rows. Settings and SmartThings tokens are never
# touched, so credentials, sensor selection, and the OAuth connection survive.
RESET_SCOPES: dict[str, list] = {
    "indoor_temp": [IndoorTempReading],
    "energy": [ConsumptionRecord],
    "device_status": [ShowerEventRecord, FaultRecord, DeviceStatusRecord],
    "weather": [WeatherRecord],
    "prices": [PriceRecord],
    "plans": [PlanActionRecord, PlanRecord, OverrideRecord],
    "logs": [AppLogRecord, AuditLogRecord],
}

# Clearing any of these training inputs invalidates the trained ML models, so a
# reset of those scopes also resets the models to avoid stale predictions.
MODEL_FEEDING_SCOPES = {"indoor_temp", "energy", "device_status", "weather"}

# The manual controller-COP ledger is never part of the scopes above. It can only
# be cleared on its own, for every device at once, together with disabling its
# experimental forecast. The revision sequence is never reset, so revision tokens
# issued before the reset keep conflicting instead of becoming valid again.
MANUAL_COP_SCOPE = "manual_cop"
MANUAL_COP_FORECAST_SETTING = "manual_cop_monitoring_enabled"


class ResetRequest(BaseModel):
    scopes: list[str]
    reset_models: bool = True


def reset_ml_models() -> list[str]:
    """Reset process-local singletons after authority commits; retain every artifact."""
    from packages.ml.comfort_model import comfort_model
    from packages.ml.models import demand_model
    from packages.ml.model_status import clear_artifact_status_cache
    from packages.ml.thermal import thermal_model

    demand_model.reset()
    comfort_model.reset()
    thermal_model.reset()
    clear_artifact_status_cache()
    return []


async def reset_manual_cop_ledger() -> dict:
    """Clear every device's manual COP rows, disable forecasting and audit, atomically."""
    model = ManualCOPObservationRecord
    async with get_session() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '20s'"))
        await session.execute(
            text("LOCK TABLE public.manual_cop_observations IN SHARE ROW EXCLUSIVE MODE")
        )
        count = await session.scalar(select(func.count()).select_from(model))
        await session.execute(delete(model))
        await session.execute(
            pg_insert(SettingRecord)
            .values(key=MANUAL_COP_FORECAST_SETTING, value="false")
            .on_conflict_do_update(index_elements=["key"], set_={"value": "false"})
        )
        deleted_rows = {model.__tablename__: count or 0}
        session.add(
            AuditLogRecord(
                actor="user",
                action="reset_data",
                payload_json=json.dumps(
                    {
                        "scopes": [MANUAL_COP_SCOPE],
                        "deleted_rows": deleted_rows,
                        "settings": {MANUAL_COP_FORECAST_SETTING: "false"},
                        "models_reset": False,
                        "deleted_models": [],
                        "model_lifecycle": None,
                    }
                ),
                result="ok",
            )
        )
    return {
        "status": "ok",
        "scopes": [MANUAL_COP_SCOPE],
        "deleted_rows": deleted_rows,
        "total_rows_deleted": deleted_rows[model.__tablename__],
        "manual_cop_monitoring_enabled": False,
        "models_reset": False,
        "deleted_models": [],
        "model_lifecycle": None,
    }


@router.post("/api/admin/reset")
async def reset_data(body: ResetRequest):
    invalid = [s for s in body.scopes if s not in RESET_SCOPES and s != MANUAL_COP_SCOPE]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown reset scopes: {invalid}")
    if not body.scopes:
        raise HTTPException(status_code=400, detail="No reset scopes provided")
    if MANUAL_COP_SCOPE in body.scopes:
        if body.scopes != [MANUAL_COP_SCOPE]:
            raise HTTPException(
                status_code=400,
                detail="The manual_cop reset scope must be requested alone",
            )
        try:
            return await reset_manual_cop_ledger()
        except SQLAlchemyError as error:
            logger.warning("manual_cop_reset_unavailable")
            raise HTTPException(
                status_code=503, detail={"code": "manual_cop_reset_unavailable"}
            ) from error

    models_affected = body.reset_models and bool(MODEL_FEEDING_SCOPES.intersection(body.scopes))
    lifecycle_reset = None
    deleted_rows: dict[str, int] = {}
    async with get_session() as session:
        if "plans" in body.scopes:
            open_restore = (
                await session.execute(
                    select(PlanActionRecord.id)
                    .where(unresolved_revert_predicate())
                    .with_for_update()
                    .limit(1)
                )
            ).scalar_one_or_none()
            if open_restore is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "unresolved_safety_reverts", "action_id": open_restore},
                )
        if models_affected:
            try:
                lifecycle_reset = await invalidate_lifecycle(session)
            except SQLAlchemyError as error:
                logger.warning("ml_reset_lifecycle_unavailable")
                raise HTTPException(
                    status_code=503, detail={"code": "ml_lifecycle_unavailable"}
                ) from error
        for scope in body.scopes:
            for model in RESET_SCOPES[scope]:
                count = await session.scalar(select(func.count()).select_from(model))
                await session.execute(delete(model))
                deleted_rows[model.__tablename__] = count or 0

    deleted_models: list[str] = reset_ml_models() if models_affected else []

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="reset_data",
                payload_json=json.dumps(
                    {
                        "scopes": body.scopes,
                        "deleted_rows": deleted_rows,
                        "models_reset": models_affected,
                        "deleted_models": deleted_models,
                        "model_lifecycle": lifecycle_reset,
                    }
                ),
                result="ok",
            )
        )

    return {
        "status": "ok",
        "scopes": body.scopes,
        "deleted_rows": deleted_rows,
        "total_rows_deleted": sum(deleted_rows.values()),
        "models_reset": models_affected,
        "deleted_models": deleted_models,
        "model_lifecycle": lifecycle_reset,
    }

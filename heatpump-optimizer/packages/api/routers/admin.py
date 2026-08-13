from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from packages.core.database import get_session
from packages.core.models import (
    AppLogRecord,
    AuditLogRecord,
    ConsumptionRecord,
    COPRecord,
    DeviceStatusRecord,
    FaultRecord,
    IndoorTempReading,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    ShowerEventRecord,
    WeatherRecord,
)

router = APIRouter()

# Each scope maps to the tables it clears, ordered so that child rows are
# removed before any parent rows. Settings and SmartThings tokens are never
# touched, so credentials, sensor selection, and the OAuth connection survive.
RESET_SCOPES: dict[str, list] = {
    "indoor_temp": [IndoorTempReading],
    "energy": [COPRecord, ConsumptionRecord],
    "device_status": [ShowerEventRecord, FaultRecord, DeviceStatusRecord],
    "weather": [WeatherRecord],
    "prices": [PriceRecord],
    "plans": [PlanActionRecord, PlanRecord, OverrideRecord],
    "logs": [AppLogRecord, AuditLogRecord],
}

# Clearing any of these training inputs invalidates the trained ML models, so a
# reset of those scopes also resets the models to avoid stale predictions.
MODEL_FEEDING_SCOPES = {"indoor_temp", "energy", "device_status", "weather"}


class ResetRequest(BaseModel):
    scopes: list[str]
    reset_models: bool = True


def reset_ml_models() -> list[str]:
    """Reset in-memory model state and delete persisted model files.

    Singletons are reset in place because other modules import them by
    reference. Returns the list of deleted model file names.
    """
    from packages.ml.comfort_model import comfort_model
    from packages.ml.models import cop_model, demand_model
    from packages.ml.models_common import MODEL_DIR
    from packages.ml.thermal import thermal_model

    cop_model.reset()
    demand_model.reset()
    comfort_model.reset()
    thermal_model.reset()

    deleted: list[str] = []
    for pattern in (
        "cop_model_*.pkl",
        "demand_model_*.pkl",
        "comfort_model_*.pkl",
        "thermal_params_v1.pkl",
    ):
        for path in MODEL_DIR.glob(pattern):
            try:
                path.unlink()
                deleted.append(path.name)
            except OSError:
                pass
    return deleted


@router.post("/api/admin/reset")
async def reset_data(body: ResetRequest):
    invalid = [s for s in body.scopes if s not in RESET_SCOPES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown reset scopes: {invalid}")
    if not body.scopes:
        raise HTTPException(status_code=400, detail="No reset scopes provided")

    deleted_rows: dict[str, int] = {}
    async with get_session() as session:
        for scope in body.scopes:
            for model in RESET_SCOPES[scope]:
                count = await session.scalar(select(func.count()).select_from(model))
                await session.execute(delete(model))
                deleted_rows[model.__tablename__] = count or 0

    models_affected = body.reset_models and bool(MODEL_FEEDING_SCOPES.intersection(body.scopes))
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
    }

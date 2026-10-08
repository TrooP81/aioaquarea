"""Compatibility facade for retained Demand and read-only COP archives."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from packages.core.database import get_session
from packages.core.models import COPRecord
from packages.ml.cop_model_core import COPModel as _CoreCOPModel
from packages.ml.demand_model_core import DemandModel as _CoreDemandModel
from packages.ml.models_common import HAS_SKLEARN as _HAS_SKLEARN, MODEL_DIR

HAS_SKLEARN = _HAS_SKLEARN


class COPModel(_CoreCOPModel):
    """Retired COP import compatibility; no artifact access or inference."""


class DemandModel(_CoreDemandModel):
    """Compatibility subclass that preserves MODEL_DIR patching via packages.ml.models."""

    async def train(self) -> dict[str, object]:
        import packages.ml.demand_model_core as demand_model_core

        original_model_dir = demand_model_core.MODEL_DIR
        demand_model_core.MODEL_DIR = MODEL_DIR
        try:
            return await super().train()
        finally:
            demand_model_core.MODEL_DIR = original_model_dir

    def load_latest(self) -> bool:
        import packages.ml.demand_model_core as demand_model_core

        original_model_dir = demand_model_core.MODEL_DIR
        demand_model_core.MODEL_DIR = MODEL_DIR
        try:
            return super().load_latest()
        finally:
            demand_model_core.MODEL_DIR = original_model_dir


class DirectionAwareCOP:
    """Retired computation compatibility and unverified archival aggregation."""

    async def compute_cop_intervals(self, hours: int = 24) -> list[dict]:
        raise RuntimeError("cop_model_retired")

    async def get_average_cop(self, hours: int = 24, mode: str | None = None) -> dict:
        """Read legacy, unverified estimates without recomputing or updating them."""

        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

        async with get_session() as session:
            query = select(
                func.avg(COPRecord.cop_value),
                func.count(COPRecord.cop_value),
                func.min(COPRecord.cop_value),
                func.max(COPRecord.cop_value),
            ).where(COPRecord.ts >= since)

            if mode:
                query = query.where(COPRecord.mode == mode)

            result = await session.execute(query)
            row = result.one()

        return {
            "avg_cop": round(row[0], 2) if row[0] else None,
            "sample_count": row[1] or 0,
            "min_cop": round(row[2], 2) if row[2] else None,
            "max_cop": round(row[3], 2) if row[3] else None,
            "period_hours": hours,
            "mode_filter": mode,
            "status": "retired",
            "provenance": "legacy_unverified_estimate",
            "confidence": "estimated",
            "measured": False,
        }


# Module-level singletons
cop_model = COPModel()
demand_model = DemandModel()
direction_cop = DirectionAwareCOP()

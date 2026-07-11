"""Compatibility facade for ML models and direction-aware COP helpers."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from packages.core.database import get_session
from packages.core.models import COPRecord, ConsumptionRecord, DeviceStatusRecord
from packages.ml.cop_model_core import COPModel as _CoreCOPModel
from packages.ml.demand_model_core import DemandModel as _CoreDemandModel
from packages.ml.models_common import HAS_SKLEARN as _HAS_SKLEARN, MODEL_DIR

HAS_SKLEARN = _HAS_SKLEARN


class COPModel(_CoreCOPModel):
    """Compatibility subclass that preserves MODEL_DIR patching via packages.ml.models."""

    async def train(self) -> dict[str, object]:
        import packages.ml.cop_model_core as cop_model_core

        original_model_dir = cop_model_core.MODEL_DIR
        cop_model_core.MODEL_DIR = MODEL_DIR
        try:
            return await super().train()
        finally:
            cop_model_core.MODEL_DIR = original_model_dir

    def load_latest(self) -> bool:
        import packages.ml.cop_model_core as cop_model_core

        original_model_dir = cop_model_core.MODEL_DIR
        cop_model_core.MODEL_DIR = MODEL_DIR
        try:
            return super().load_latest()
        finally:
            cop_model_core.MODEL_DIR = original_model_dir


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
    """
    Computes real COP from direction-tagged device status + consumption data.

    Approach:
    - Groups consecutive status records by device_action (HEATING, HEATING_WATER, COOLING)
    - For each active interval, calculates:
      * Thermal energy delivered: ΔT × mass_flow_estimate
      * Electrical energy consumed: matched consumption record
      * COP = thermal / electrical
    - Stores results in cop_history table for trending

    This replaces the old black-box COP model with measured COP values.
    """

    # Water circuit thermal mass estimate (zone1_temp is water supply temp)
    WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG = 0.5

    @staticmethod
    def _tank_kwh_per_degree() -> float:
        """Tank thermal capacity from configured volume."""
        from packages.core.config import settings
        return settings.tank_kwh_per_degree

    async def compute_cop_intervals(self, hours: int = 24) -> list[dict]:
        """
        Compute COP for each active heating/cooling interval in the last N hours.

        Returns list of {ts, mode, cop, outdoor_temp, electrical_kwh, thermal_kwh}
        """

        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

        async with get_session() as session:
            status_result = await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts >= since)
                .order_by(DeviceStatusRecord.ts)
            )
            records = status_result.scalars().all()

            consumption_result = await session.execute(
                select(ConsumptionRecord)
                .where(ConsumptionRecord.ts >= since)
                .order_by(ConsumptionRecord.ts)
            )
            consumption_records = consumption_result.scalars().all()

        if len(records) < 2:
            return []

        # Count active hours by mode from status records to estimate
        # how many hours/day each mode runs (for daily→hourly conversion)
        active_hours_by_mode = self._count_active_hours(records)

        cop_intervals = []

        for i in range(1, len(records)):
            prev = records[i - 1]
            curr = records[i]

            action = getattr(curr, 'device_action', None)
            if not action or action in ("OFF", "IDLE"):
                continue

            dt_hours = (curr.ts - prev.ts).total_seconds() / 3600.0
            if dt_hours <= 0 or dt_hours > 2.0:
                continue

            # Skip defrost — not real heating
            if getattr(curr, 'defrost_active', None):
                continue

            outdoor = curr.outdoor_temp if curr.outdoor_temp is not None else 5.0
            thermal_kwh = 0.0
            # DHW tank heating is the only physically-grounded measurement
            # (fixed thermal mass × ΔT). Space heating/cooling use the water
            # supply temp as a proxy, which is unreliable (the circuit has no
            # fixed thermal mass and supply temp is near-constant in steady
            # state), so those COP values are flagged low-confidence.
            confidence = "measured"

            if action == "HEATING_WATER":
                # Tank heating: thermal = tank_mass × ΔT
                if prev.tank_temp and curr.tank_temp and curr.tank_temp > prev.tank_temp:
                    delta_t = curr.tank_temp - prev.tank_temp
                    thermal_kwh = delta_t * self._tank_kwh_per_degree()
            elif action == "HEATING":
                # Zone heating: thermal output from water circuit ΔT
                # (zone1_temp is water supply temperature, not indoor air)
                confidence = "estimated"
                if prev.zone1_temp and curr.zone1_temp and curr.zone1_temp > prev.zone1_temp:
                    delta_t = curr.zone1_temp - prev.zone1_temp
                    thermal_kwh = delta_t * self.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG
            elif action == "COOLING":
                # Cooling: thermal = building_mass × |ΔT|
                confidence = "estimated"
                if prev.zone1_temp and curr.zone1_temp and curr.zone1_temp < prev.zone1_temp:
                    delta_t = prev.zone1_temp - curr.zone1_temp
                    thermal_kwh = delta_t * self.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG

            if thermal_kwh <= 0:
                continue

            # Estimate electrical consumption for this interval
            electrical_kwh = self._estimate_electrical(
                curr.ts, dt_hours, consumption_records, action, active_hours_by_mode
            )

            if electrical_kwh <= 0:
                continue

            cop = thermal_kwh / electrical_kwh

            # Sanity check: COP should be between 1 and 8 for heat pumps
            if 0.5 < cop < 10.0:
                cop_intervals.append({
                    "ts": curr.ts,
                    "device_id": curr.device_id,
                    "mode": action,
                    "cop": round(cop, 2),
                    "outdoor_temp": outdoor,
                    "electrical_kwh": round(electrical_kwh, 4),
                    "thermal_kwh": round(thermal_kwh, 4),
                    "confidence": confidence,
                })

        # Persist COP records
        if cop_intervals:
            async with get_session() as session:
                for entry in cop_intervals:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    stmt = pg_insert(COPRecord).values(
                        ts=entry["ts"],
                        device_id=entry["device_id"],
                        cop_value=entry["cop"],
                        mode=entry["mode"],
                        outdoor_temp=entry["outdoor_temp"],
                        electrical_kwh=entry["electrical_kwh"],
                        thermal_kwh=entry["thermal_kwh"],
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "device_id"],
                        set_={
                            "cop_value": entry["cop"],
                            "mode": entry["mode"],
                            "outdoor_temp": entry["outdoor_temp"],
                            "electrical_kwh": entry["electrical_kwh"],
                            "thermal_kwh": entry["thermal_kwh"],
                        },
                    )
                    await session.execute(stmt)

        return cop_intervals

    @staticmethod
    def _count_active_hours(records: list) -> dict[str, float]:
        """Count total active hours per mode from status records.

        Returns dict mapping mode category → total hours active.
        Categories: 'dhw', 'sh', 'total'.
        """
        dhw_hours = 0.0
        sh_hours = 0.0
        for i in range(1, len(records)):
            prev_r = records[i - 1]
            curr_r = records[i]
            gap = (curr_r.ts - prev_r.ts).total_seconds() / 3600.0
            if gap <= 0 or gap > 2.0:
                continue
            action = getattr(curr_r, "device_action", None)
            if action == "HEATING_WATER":
                dhw_hours += gap
            elif action in ("HEATING", "COOLING"):
                sh_hours += gap
        return {
            "dhw": max(dhw_hours, 1.0),
            "sh": max(sh_hours, 1.0),
            "total": max(dhw_hours + sh_hours, 1.0),
        }

    @staticmethod
    def _estimate_electrical(
        ts: dt.datetime,
        dt_hours: float,
        consumption_records: list,
        action: str,
        active_hours: dict[str, float] | None = None,
    ) -> float:
        """
        Estimate electrical kWh consumed during an interval.

        Uses the closest consumption record, divides daily kWh by actual
        compressor-on hours for the relevant mode.
        """
        if not consumption_records:
            return 0.0

        # Find closest consumption record by time
        closest = min(
            consumption_records,
            key=lambda c: abs((c.ts - ts).total_seconds()),
        )

        # Only use if within 30 minutes
        if abs((closest.ts - ts).total_seconds()) > 1800:
            return 0.0

        # Estimate hourly rate from daily consumption ÷ real active hours
        if action == "HEATING_WATER":
            daily_kwh = closest.tank_kwh or 0
            divisor = active_hours["dhw"] if active_hours else 10.0
        elif action in ("HEATING", "COOLING"):
            daily_kwh = (closest.heat_kwh or 0) + (closest.cool_kwh or 0)
            divisor = active_hours["sh"] if active_hours else 10.0
        else:
            daily_kwh = (closest.heat_kwh or 0) + (closest.tank_kwh or 0) + (closest.cool_kwh or 0)
            divisor = active_hours["total"] if active_hours else 10.0

        hourly_rate = daily_kwh / divisor if daily_kwh > 0 else 0
        return hourly_rate * dt_hours

    async def get_average_cop(self, hours: int = 24, mode: str | None = None) -> dict:
        """Get average COP statistics for a time period."""

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
        }


# Module-level singletons
cop_model = COPModel()
demand_model = DemandModel()
direction_cop = DirectionAwareCOP()

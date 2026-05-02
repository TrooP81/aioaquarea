"""ML models for COP prediction and demand forecasting."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, WeatherRecord, COPRecord
from packages.core.config import settings as app_settings

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)


class COPModel:
    """
    Predicts COP (Coefficient of Performance) given operating conditions.

    Features:
    - outdoor_temperature
    - tank_target_temp (supply temp proxy)
    - hour_of_day (cyclical)
    - mode (encoded)

    Target: electrical_kwh / thermal_kwh_delivered (inverted COP)
    We actually predict kWh electrical since that's what we measure.
    """

    def __init__(self):
        self._model: Pipeline | None = None
        self._version: str = "untrained"
        self._metrics: dict[str, float] = {}

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    async def train(self) -> dict[str, Any]:
        """Train the COP model on historical data."""
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required: pip install scikit-learn")

        X, y = await self._prepare_training_data()

        if len(X) < 100:
            return {"error": "Insufficient data", "samples": len(X)}

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "model",
                    GradientBoostingRegressor(
                        n_estimators=200,
                        max_depth=5,
                        learning_rate=0.1,
                        random_state=42,
                    ),
                ),
            ]
        )

        # Cross-validate
        scores = cross_val_score(pipeline, X, y, cv=5, scoring="neg_mean_absolute_error")
        mae = -scores.mean()

        # Train on full data
        pipeline.fit(X, y)

        self._model = pipeline
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        self._metrics = {"mae": mae, "samples": len(X), "cv_std": scores.std()}

        # Save model
        model_path = MODEL_DIR / f"cop_model_{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump
        safe_dump(pipeline, model_path)

        return {
            "version": self._version,
            "metrics": self._metrics,
            "model_path": str(model_path),
        }

    def predict(self, outdoor_temp: float, tank_target: int, hour: int) -> float:
        """Predict electrical kWh for one hour given conditions."""
        if self._model is None:
            # Fallback to simple curve
            return self._fallback_predict(outdoor_temp)

        features = self._make_features(outdoor_temp, tank_target, hour)
        return float(self._model.predict(features.reshape(1, -1))[0])

    def predict_cop(self, outdoor_temp: float, tank_target: int, hour: int) -> float:
        """Predict COP given conditions."""
        kwh_electrical = self.predict(outdoor_temp, tank_target, hour)
        if kwh_electrical <= 0:
            return 3.0  # fallback
        # Assume ~3kW thermal output typical
        thermal_output = 3.0  # This would come from a thermal model
        return thermal_output / kwh_electrical

    @staticmethod
    def _fallback_predict(outdoor_temp: float) -> float:
        """Simple fallback when no trained model available."""
        # Higher outdoor temp = lower electrical consumption (higher COP)
        base_kwh = 2.0
        temp_factor = max(0.3, 1.0 - outdoor_temp * 0.03)
        return base_kwh * temp_factor

    @staticmethod
    def _make_features(outdoor_temp: float, tank_target: int, hour: int) -> np.ndarray:
        """Create feature vector."""
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        return np.array([outdoor_temp, tank_target, hour_sin, hour_cos])

    async def _prepare_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Load and join consumption + status data for training."""
        async with get_session() as session:
            # Get consumption records with matching weather
            consumption_result = await session.execute(
                select(
                    ConsumptionRecord.ts,
                    ConsumptionRecord.heat_kwh,
                    ConsumptionRecord.tank_kwh,
                    ConsumptionRecord.outdoor_temp,
                ).order_by(ConsumptionRecord.ts)
            )
            consumption_rows = consumption_result.all()

            # Get device status for tank targets
            status_result = await session.execute(
                select(
                    DeviceStatusRecord.ts,
                    DeviceStatusRecord.tank_target_temp,
                    DeviceStatusRecord.outdoor_temp,
                ).order_by(DeviceStatusRecord.ts)
            )
            status_rows = status_result.all()

        if not consumption_rows:
            return np.array([]), np.array([])

        # Build a lookup of tank_target by hour from status records
        # Key: (date, hour) -> tank_target_temp
        status_by_hour: dict[tuple, float] = {}
        for s in status_rows:
            if s.tank_target_temp is not None:
                key = (s.ts.date(), s.ts.hour)
                status_by_hour[key] = s.tank_target_temp

        # Consumption values are cumulative per day — compute hourly deltas.
        # Group consecutive records by (device day) and diff them.
        X_list = []
        y_list = []

        prev_row = None
        for row in consumption_rows:
            if prev_row is not None and row.ts.date() == prev_row.ts.date():
                # Same day: delta = current cumulative - previous cumulative
                delta_kwh = (
                    ((row.heat_kwh or 0) + (row.tank_kwh or 0))
                    - ((prev_row.heat_kwh or 0) + (prev_row.tank_kwh or 0))
                )
                elapsed_hours = (row.ts - prev_row.ts).total_seconds() / 3600.0

                # Only use if positive delta and reasonable time gap (15 min to 2 hours)
                if delta_kwh > 0 and 0.2 <= elapsed_hours <= 2.0:
                    # Normalize to per-hour consumption
                    kwh_per_hour = delta_kwh / elapsed_hours

                    outdoor_temp = row.outdoor_temp or 5.0
                    hour = row.ts.hour
                    # Look up tank_target from nearest status record
                    tank_target = status_by_hour.get(
                        (row.ts.date(), hour),
                        status_by_hour.get((row.ts.date(), hour - 1), 50),
                    )

                    features = self._make_features(outdoor_temp, int(tank_target), hour)
                    X_list.append(features)
                    y_list.append(kwh_per_hour)

            prev_row = row

        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        """Load the latest saved model."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
        if not models:
            return False
        try:
            self._model = safe_load(models[-1])
        except ValueError:
            return False
        self._version = models[-1].stem.replace("cop_model_", "")
        return True


class DemandModel:
    """
    Predicts thermal demand (kWh) for the next 24h.

    Features:
    - Weather forecast (temperature, wind, irradiance)
    - Day of week
    - Historical demand pattern

    Target: total kWh consumed per hour
    """

    def __init__(self):
        self._model: Pipeline | None = None
        self._version: str = "untrained"

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    async def train(self) -> dict[str, Any]:
        """Train demand model on historical consumption + weather."""
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required")

        X, y = await self._prepare_data()
        if len(X) < 168:  # Need at least a week of hourly data
            return {"error": "Insufficient data", "samples": len(X)}

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "model",
                    GradientBoostingRegressor(
                        n_estimators=150,
                        max_depth=4,
                        learning_rate=0.1,
                        random_state=42,
                    ),
                ),
            ]
        )

        scores = cross_val_score(pipeline, X, y, cv=5, scoring="neg_mean_absolute_error")
        pipeline.fit(X, y)

        self._model = pipeline
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")

        model_path = MODEL_DIR / f"demand_model_{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump
        safe_dump(pipeline, model_path)

        return {
            "version": self._version,
            "mae": -scores.mean(),
            "samples": len(X),
        }

    def predict_hourly(
        self, weather_forecast: list[dict], hours: int = 24
    ) -> list[float]:
        """Predict hourly demand for the next N hours."""
        predictions = []
        now = dt.datetime.now(dt.timezone.utc)

        for h in range(hours):
            ts = now + dt.timedelta(hours=h)
            weather = weather_forecast[h] if h < len(weather_forecast) else {}

            temp = weather.get("temperature", 5.0)
            wind = weather.get("wind_speed", 3.0)
            irradiance = weather.get("irradiance", 0.0)
            hour = ts.hour
            dow = ts.weekday()

            if self._model is not None:
                features = np.array(
                    [
                        temp,
                        wind,
                        irradiance,
                        np.sin(2 * np.pi * hour / 24),
                        np.cos(2 * np.pi * hour / 24),
                        np.sin(2 * np.pi * dow / 7),
                        np.cos(2 * np.pi * dow / 7),
                    ]
                ).reshape(1, -1)
                pred = float(self._model.predict(features)[0])
            else:
                # Fallback: simple temperature-based estimate
                pred = max(0.5, 3.0 - 0.1 * temp)

            predictions.append(max(0, pred))

        return predictions

    async def _prepare_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Prepare training data from consumption + weather history."""
        async with get_session() as session:
            result = await session.execute(
                select(
                    ConsumptionRecord.ts,
                    ConsumptionRecord.heat_kwh,
                    ConsumptionRecord.cool_kwh,
                    ConsumptionRecord.tank_kwh,
                    ConsumptionRecord.outdoor_temp,
                ).order_by(ConsumptionRecord.ts)
            )
            rows = result.all()

        if not rows:
            return np.array([]), np.array([])

        X_list = []
        y_list = []

        for row in rows:
            total = (row.heat_kwh or 0) + (row.cool_kwh or 0) + (row.tank_kwh or 0)
            temp = row.outdoor_temp or 5.0
            hour = row.ts.hour
            dow = row.ts.weekday()

            features = np.array(
                [
                    temp,
                    3.0,  # wind placeholder
                    0.0,  # irradiance placeholder
                    np.sin(2 * np.pi * hour / 24),
                    np.cos(2 * np.pi * hour / 24),
                    np.sin(2 * np.pi * dow / 7),
                    np.cos(2 * np.pi * dow / 7),
                ]
            )
            X_list.append(features)
            y_list.append(total)

        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        """Load the latest saved model."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))
        if not models:
            return False
        try:
            self._model = safe_load(models[-1])
        except ValueError:
            return False
        self._version = models[-1].stem.replace("demand_model_", "")
        return True


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

    # Estimated thermal capacity (kW) per °C/hour of temperature change
    # These are rough estimates — calibrated over time from consumption data
    TANK_THERMAL_MASS_KWH_PER_DEG = 0.058  # ~50L tank ≈ 58 Wh per °C
    WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG = 0.5  # Water circuit thermal mass estimate (zone1_temp is water supply temp)

    async def compute_cop_intervals(self, hours: int = 24) -> list[dict]:
        """
        Compute COP for each active heating/cooling interval in the last N hours.

        Returns list of {ts, mode, cop, outdoor_temp, electrical_kwh, thermal_kwh}
        """
        from packages.core.models import COPRecord

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

            outdoor = curr.outdoor_temp or 5.0
            thermal_kwh = 0.0

            if action == "HEATING_WATER":
                # Tank heating: thermal = mass × ΔT
                if prev.tank_temp and curr.tank_temp and curr.tank_temp > prev.tank_temp:
                    delta_t = curr.tank_temp - prev.tank_temp
                    thermal_kwh = delta_t * self.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG
            elif action == "HEATING":
                # Zone heating: thermal output from water circuit ΔT
                # (zone1_temp is water supply temperature, not indoor air)
                if prev.zone1_temp and curr.zone1_temp and curr.zone1_temp > prev.zone1_temp:
                    delta_t = curr.zone1_temp - prev.zone1_temp
                    thermal_kwh = delta_t * self.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG
            elif action == "COOLING":
                # Cooling: thermal = building_mass × |ΔT|
                if prev.zone1_temp and curr.zone1_temp and curr.zone1_temp < prev.zone1_temp:
                    delta_t = prev.zone1_temp - curr.zone1_temp
                    thermal_kwh = delta_t * self.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG

            if thermal_kwh <= 0:
                continue

            # Estimate electrical consumption for this interval
            electrical_kwh = self._estimate_electrical(
                curr.ts, dt_hours, consumption_records, action
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
    def _estimate_electrical(
        ts: dt.datetime,
        dt_hours: float,
        consumption_records: list,
        action: str,
    ) -> float:
        """
        Estimate electrical kWh consumed during an interval.

        Uses the closest consumption record and proportions by action type.
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

        # Estimate hourly rate from daily consumption
        if action == "HEATING_WATER":
            daily_kwh = closest.tank_kwh or 0
        elif action in ("HEATING", "COOLING"):
            daily_kwh = (closest.heat_kwh or 0) + (closest.cool_kwh or 0)
        else:
            daily_kwh = (closest.heat_kwh or 0) + (closest.tank_kwh or 0) + (closest.cool_kwh or 0)

        # Rough: assume consumption is spread across 10 active hours/day
        hourly_rate = daily_kwh / 10.0 if daily_kwh > 0 else 0
        return hourly_rate * dt_hours

    async def get_average_cop(self, hours: int = 24, mode: str | None = None) -> dict:
        """Get average COP statistics for a time period."""
        from packages.core.models import COPRecord

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

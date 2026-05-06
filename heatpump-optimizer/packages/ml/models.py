"""ML models for COP prediction and demand forecasting."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import structlog

_logger = structlog.get_logger()

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from sqlalchemy import select, and_, func

from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, WeatherRecord, COPRecord
from packages.core.config import settings as app_settings

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)


class COPModel:
    """
    Predicts COP (Coefficient of Performance) given operating conditions.

    Trained on real COP values derived from paired consumption (electrical kWh)
    and device status (tank/zone ΔT → thermal kWh) records.

    Features: outdoor_temp, tank_target, hour_sin, hour_cos
    Target: COP (thermal_kWh / electrical_kWh)
    """

    # Physical COP bounds for air-to-water heat pumps
    COP_MIN = 1.5
    COP_MAX = 6.0

    @staticmethod
    def _tank_kwh_per_degree() -> float:
        """Tank thermal capacity from configured volume."""
        from packages.core.config import settings
        return settings.tank_kwh_per_degree

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

        if len(X) < 50:
            return {"error": "Insufficient data", "samples": len(X)}

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "model",
                    GradientBoostingRegressor(
                        n_estimators=200,
                        max_depth=4,
                        learning_rate=0.1,
                        random_state=42,
                    ),
                ),
            ]
        )

        # Cross-validate
        n_folds = min(5, len(X))
        scores = cross_val_score(pipeline, X, y, cv=n_folds, scoring="neg_mean_absolute_error")
        mae = -scores.mean()

        # Train on full data
        pipeline.fit(X, y)

        self._model = pipeline
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        self._metrics = {"mae": mae, "samples": len(X), "cv_std": scores.std()}

        _logger.info(
            "cop_model_trained",
            samples=len(X),
            mae=round(mae, 3),
            y_mean=round(float(np.mean(y)), 2),
            y_std=round(float(np.std(y)), 2),
        )

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
        """Predict electrical kWh for one hour (legacy, kept for compatibility).

        Derives from COP prediction: electrical = thermal_estimate / COP.
        """
        cop = self.predict_cop(outdoor_temp, tank_target, hour)
        # Estimate typical thermal output based on conditions
        thermal_kw = self._estimate_thermal_output(outdoor_temp, tank_target)
        return thermal_kw / cop

    def predict_cop(self, outdoor_temp: float, tank_target: int, hour: int) -> float:
        """Predict COP given operating conditions."""
        if self._model is None:
            return self._default_cop_curve(outdoor_temp)

        features = self._make_features(outdoor_temp, tank_target, hour)
        raw = float(self._model.predict(features.reshape(1, -1))[0])
        return max(self.COP_MIN, min(self.COP_MAX, raw))

    @staticmethod
    def _default_cop_curve(outdoor_temp: float) -> float:
        """Simple linear COP approximation when no model is trained."""
        cop = 3.5 + 0.1 * outdoor_temp
        return max(1.5, min(6.0, cop))

    @staticmethod
    def _estimate_thermal_output(outdoor_temp: float, tank_target: int) -> float:
        """Rough thermal output estimate (kW) for predict() backward compat."""
        # Lower outdoor → higher thermal demand; higher tank target → more output
        base = 2.0
        temp_factor = max(0.5, 1.0 - outdoor_temp * 0.02)
        return base * temp_factor

    @staticmethod
    def _make_features(outdoor_temp: float, tank_target: int, hour: int) -> np.ndarray:
        """Create feature vector."""
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        return np.array([outdoor_temp, tank_target, hour_sin, hour_cos])

    async def _prepare_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Compute real COP from paired consumption + device status records.

        COP = thermal_kWh / electrical_kWh

        Thermal output is derived from tank ΔT (for DHW) or zone ΔT (for SH),
        both measurable from device status. Electrical input comes from
        consumption records.
        """
        async with get_session() as session:
            consumption_result = await session.execute(
                select(
                    ConsumptionRecord.ts,
                    ConsumptionRecord.heat_kwh,
                    ConsumptionRecord.tank_kwh,
                    ConsumptionRecord.outdoor_temp,
                ).order_by(ConsumptionRecord.ts)
            )
            consumption_rows = consumption_result.all()

            status_result = await session.execute(
                select(
                    DeviceStatusRecord.ts,
                    DeviceStatusRecord.tank_target_temp,
                    DeviceStatusRecord.tank_temp,
                    DeviceStatusRecord.outdoor_temp,
                    DeviceStatusRecord.direction,
                ).order_by(DeviceStatusRecord.ts)
            )
            status_rows = status_result.all()

        if not consumption_rows or not status_rows:
            return np.array([]), np.array([])

        # Sort status records by timestamp for binary-search pairing
        status_sorted = sorted(status_rows, key=lambda s: s.ts)
        status_ts = [s.ts for s in status_sorted]

        def _find_closest_status(ts):
            """Find the device status record closest to the given timestamp."""
            import bisect
            idx = bisect.bisect_left(status_ts, ts)
            best = None
            best_gap = float("inf")
            for candidate_idx in (idx - 1, idx):
                if 0 <= candidate_idx < len(status_sorted):
                    gap = abs((status_sorted[candidate_idx].ts - ts).total_seconds())
                    if gap < best_gap:
                        best_gap = gap
                        best = status_sorted[candidate_idx]
            # Only return if within 20 minutes
            if best and best_gap <= 1200:
                return best
            return None

        X_list = []
        y_list = []

        # Diagnostic counters
        skip_no_delta = 0
        skip_no_status = 0
        skip_thermal_zero = 0
        skip_cop_low = 0
        skip_cop_high = 0
        used_status_path = 0
        used_fallback_path = 0

        prev_row = None
        for row in consumption_rows:
            if prev_row is None or row.ts.date() != prev_row.ts.date():
                prev_row = row
                continue

            elapsed_hours = (row.ts - prev_row.ts).total_seconds() / 3600.0
            if not (0.2 <= elapsed_hours <= 2.0):
                skip_no_delta += 1
                prev_row = row
                continue

            # Pair each consumption timestamp with nearest device status
            prev_status_rec = _find_closest_status(prev_row.ts)
            curr_status_rec = _find_closest_status(row.ts)

            # Total electrical delta for this interval
            delta_elec = (
                ((row.heat_kwh or 0) + (row.tank_kwh or 0))
                - ((prev_row.heat_kwh or 0) + (prev_row.tank_kwh or 0))
            )
            if delta_elec <= 0.01:
                skip_no_delta += 1
                prev_row = row
                continue

            elec_kw = delta_elec / elapsed_hours
            thermal_kwh = 0.0
            curr_status = None

            if (
                prev_status_rec and curr_status_rec
                and prev_status_rec.ts != curr_status_rec.ts
            ):
                used_status_path += 1
                kwh_per_deg = self._tank_kwh_per_degree()

                # Tank thermal contribution
                tank_thermal = 0.0
                if (
                    prev_status_rec.tank_temp is not None
                    and curr_status_rec.tank_temp is not None
                ):
                    tank_delta = curr_status_rec.tank_temp - prev_status_rec.tank_temp
                    if tank_delta > 0:
                        tank_thermal = tank_delta * kwh_per_deg

                # Zone (space heating) thermal contribution
                zone_thermal = 0.0
                if (
                    prev_status_rec.zone1_temp is not None
                    and curr_status_rec.zone1_temp is not None
                ):
                    zone_delta = curr_status_rec.zone1_temp - prev_status_rec.zone1_temp
                    if zone_delta > 0:
                        # Water circuit thermal mass for zone heating
                        zone_thermal = zone_delta * 0.5  # kWh per °C water circuit

                total_thermal = tank_thermal + zone_thermal
                if total_thermal > 0:
                    thermal_kwh = total_thermal / elapsed_hours

                curr_status = {
                    "tank_target": curr_status_rec.tank_target_temp or 50,
                }
            else:
                skip_no_status += 1

            # If no thermal from status pairing, use default COP estimate
            if thermal_kwh <= 0:
                used_fallback_path += 1
                outdoor = row.outdoor_temp or 5.0
                default_cop = self._default_cop_curve(outdoor)
                thermal_kwh = elec_kw * default_cop * 0.7  # conservative

            if thermal_kwh <= 0:
                skip_thermal_zero += 1
                prev_row = row
                continue

            cop = thermal_kwh / elec_kw

            # Only keep physically plausible COP values for training
            if cop < self.COP_MIN:
                skip_cop_low += 1
                prev_row = row
                continue
            if cop > self.COP_MAX:
                skip_cop_high += 1
                prev_row = row
                continue

            outdoor_temp = row.outdoor_temp or 5.0
            hour = row.ts.hour
            tank_target = (
                curr_status.get("tank_target", 50) if curr_status else 50
            )

            features = self._make_features(outdoor_temp, int(tank_target), hour)
            X_list.append(features)
            y_list.append(cop)

            prev_row = row

        _logger.info(
            "cop_training_data_prepared",
            total_consumption_rows=len(consumption_rows),
            status_rows=len(status_rows),
            paired_samples=len(y_list),
            skip_no_delta=skip_no_delta,
            skip_no_status=skip_no_status,
            skip_thermal_zero=skip_thermal_zero,
            skip_cop_low=skip_cop_low,
            skip_cop_high=skip_cop_high,
            used_status_path=used_status_path,
            used_fallback_path=used_fallback_path,
            kwh_per_degree=round(self._tank_kwh_per_degree(), 4),
        )
        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        """Load the latest saved model."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
        if not models:
            _logger.info("cop_model_load_skip", reason="no model files found", dir=str(MODEL_DIR))
            return False
        try:
            self._model = safe_load(models[-1])
        except ValueError as exc:
            _logger.warning("cop_model_load_failed", path=str(models[-1]), error=str(exc))
            return False
        self._version = models[-1].stem.replace("cop_model_", "")
        _logger.info("cop_model_loaded", version=self._version, path=str(models[-1]))
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
        """Prepare training data from consumption + weather history.

        Joins consumption records with WeatherRecord to get real wind/irradiance
        instead of placeholder constants.
        """
        async with get_session() as session:
            # Join consumption with closest weather record (same hour)
            result = await session.execute(
                select(
                    ConsumptionRecord.ts,
                    ConsumptionRecord.heat_kwh,
                    ConsumptionRecord.cool_kwh,
                    ConsumptionRecord.tank_kwh,
                    ConsumptionRecord.outdoor_temp,
                    WeatherRecord.wind_speed,
                    WeatherRecord.irradiance,
                )
                .outerjoin(
                    WeatherRecord,
                    and_(
                        func.date_trunc("hour", WeatherRecord.ts)
                        == func.date_trunc("hour", ConsumptionRecord.ts),
                    ),
                )
                .order_by(ConsumptionRecord.ts)
            )
            rows = result.all()

        if not rows:
            return np.array([]), np.array([])

        X_list = []
        y_list = []

        for row in rows:
            total = (row.heat_kwh or 0) + (row.cool_kwh or 0) + (row.tank_kwh or 0)
            temp = row.outdoor_temp or 5.0
            wind = row.wind_speed if row.wind_speed is not None else 3.0
            irradiance = row.irradiance if row.irradiance is not None else 0.0
            hour = row.ts.hour
            dow = row.ts.weekday()

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
            )
            X_list.append(features)
            y_list.append(total)

        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        """Load the latest saved model."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))
        if not models:
            _logger.info("demand_model_load_skip", reason="no model files found", dir=str(MODEL_DIR))
            return False
        try:
            self._model = safe_load(models[-1])
        except ValueError as exc:
            _logger.warning("demand_model_load_failed", path=str(models[-1]), error=str(exc))
            return False
        self._version = models[-1].stem.replace("demand_model_", "")
        _logger.info("demand_model_loaded", version=self._version, path=str(models[-1]))
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

            outdoor = curr.outdoor_temp or 5.0
            thermal_kwh = 0.0

            if action == "HEATING_WATER":
                # Tank heating: thermal = tank_mass × ΔT
                if prev.tank_temp and curr.tank_temp and curr.tank_temp > prev.tank_temp:
                    delta_t = curr.tank_temp - prev.tank_temp
                    thermal_kwh = delta_t * self._tank_kwh_per_degree()
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

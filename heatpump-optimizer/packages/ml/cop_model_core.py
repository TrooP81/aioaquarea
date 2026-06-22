"""COP model implementation."""

from __future__ import annotations

import datetime as dt

import numpy as np
from sqlalchemy import select

from packages.core.config import settings as app_settings
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord
from packages.ml.models_common import HAS_SKLEARN, MODEL_DIR, _logger, cross_val_score, make_monotonic_regressor

# Physical monotonicity for COP features [outdoor_temp, tank_target, hour_sin, hour_cos]:
# COP rises as it gets warmer outside (+1) and falls as the tank target rises (-1).
_COP_MONOTONIC_CST = [1, -1, 0, 0]


class COPModel:
    COP_MIN = 1.5
    COP_MAX = 6.0

    @staticmethod
    def _tank_kwh_per_degree() -> float:
        return app_settings.tank_kwh_per_degree

    def __init__(self):
        self._model = None
        self._version: str = "untrained"
        self._metrics: dict[str, float] = {}

    def reset(self) -> None:
        """Discard the trained model and return to the untrained fallback state."""
        self._model = None
        self._version = "untrained"
        self._metrics = {}

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    async def train(self) -> dict[str, object]:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required: pip install scikit-learn")

        X, y = await self._prepare_training_data()
        if len(X) < 50:
            return {"error": "Insufficient data", "samples": len(X)}

        model = make_monotonic_regressor(_COP_MONOTONIC_CST, max_iter=300)
        scores = cross_val_score(model, X, y, cv=min(5, len(X)), scoring="neg_mean_absolute_error")
        mae = -scores.mean()
        model.fit(X, y)

        self._model = model
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        self._metrics = {"mae": mae, "samples": len(X), "cv_std": scores.std()}

        _logger.info("cop_model_trained", samples=len(X), mae=round(mae, 3), y_mean=round(float(np.mean(y)), 2), y_std=round(float(np.std(y)), 2))

        model_path = MODEL_DIR / f"cop_model_{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump

        safe_dump(model, model_path)
        return {"version": self._version, "metrics": self._metrics, "model_path": str(model_path)}

    def predict(self, outdoor_temp: float, tank_target: int, hour: int) -> float:
        return self._estimate_thermal_output(outdoor_temp, tank_target) / self.predict_cop(outdoor_temp, tank_target, hour)

    def predict_cop(self, outdoor_temp: float, tank_target: int, hour: int) -> float:
        if self._model is None:
            return self._default_cop_curve(outdoor_temp)
        features = self._make_features(outdoor_temp, tank_target, hour)
        raw = float(self._model.predict(features.reshape(1, -1))[0])
        return max(self.COP_MIN, min(self.COP_MAX, raw))

    @staticmethod
    def _default_cop_curve(outdoor_temp: float) -> float:
        return max(1.5, min(6.0, 3.5 + 0.1 * outdoor_temp))

    @staticmethod
    def _estimate_thermal_output(outdoor_temp: float, tank_target: int) -> float:
        return 2.0 * max(0.5, 1.0 - outdoor_temp * 0.02)

    @staticmethod
    def _make_features(outdoor_temp: float, tank_target: int, hour: int) -> np.ndarray:
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        return np.array([outdoor_temp, tank_target, hour_sin, hour_cos])

    async def _prepare_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        async with get_session() as session:
            consumption_rows = (await session.execute(select(ConsumptionRecord.ts, ConsumptionRecord.heat_kwh, ConsumptionRecord.tank_kwh, ConsumptionRecord.outdoor_temp).order_by(ConsumptionRecord.ts))).all()
            status_rows = (await session.execute(select(DeviceStatusRecord.ts, DeviceStatusRecord.tank_target_temp, DeviceStatusRecord.tank_temp, DeviceStatusRecord.outdoor_temp, DeviceStatusRecord.direction, DeviceStatusRecord.zone1_temp).order_by(DeviceStatusRecord.ts))).all()

        if not consumption_rows or not status_rows:
            return np.array([]), np.array([])

        status_sorted = sorted(status_rows, key=lambda s: s.ts)
        status_ts = [s.ts for s in status_sorted]

        def _find_closest_status(ts):
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
            return best if best and best_gap <= 1200 else None

        X_list = []
        y_list = []
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

            prev_status_rec = _find_closest_status(prev_row.ts)
            curr_status_rec = _find_closest_status(row.ts)
            delta_elec = ((row.heat_kwh or 0) + (row.tank_kwh or 0)) - ((prev_row.heat_kwh or 0) + (prev_row.tank_kwh or 0))
            if delta_elec <= 0.01:
                skip_no_delta += 1
                prev_row = row
                continue

            elec_kw = delta_elec / elapsed_hours
            thermal_kwh = 0.0
            curr_status = None
            if prev_status_rec and curr_status_rec and prev_status_rec.ts != curr_status_rec.ts:
                used_status_path += 1
                tank_thermal = 0.0
                if prev_status_rec.tank_temp is not None and curr_status_rec.tank_temp is not None:
                    tank_delta = curr_status_rec.tank_temp - prev_status_rec.tank_temp
                    if tank_delta > 0:
                        tank_thermal = tank_delta * self._tank_kwh_per_degree()
                zone_thermal = 0.0
                if prev_status_rec.zone1_temp is not None and curr_status_rec.zone1_temp is not None:
                    zone_delta = curr_status_rec.zone1_temp - prev_status_rec.zone1_temp
                    if zone_delta > 0:
                        zone_thermal = zone_delta * 0.5
                total_thermal = tank_thermal + zone_thermal
                if total_thermal > 0:
                    thermal_kwh = total_thermal / elapsed_hours
                curr_status = {"tank_target": curr_status_rec.tank_target_temp or 50}
            else:
                skip_no_status += 1

            if thermal_kwh <= 0:
                used_fallback_path += 1
                thermal_kwh = elec_kw * self._default_cop_curve(row.outdoor_temp or 5.0) * 0.7
            if thermal_kwh <= 0:
                skip_thermal_zero += 1
                prev_row = row
                continue

            cop = thermal_kwh / elec_kw
            if cop < self.COP_MIN:
                skip_cop_low += 1
                prev_row = row
                continue
            if cop > self.COP_MAX:
                skip_cop_high += 1
                prev_row = row
                continue

            features = self._make_features(row.outdoor_temp or 5.0, int(curr_status.get("tank_target", 50) if curr_status else 50), row.ts.hour)
            X_list.append(features)
            y_list.append(cop)
            prev_row = row

        _logger.info("cop_training_data_prepared", total_consumption_rows=len(consumption_rows), status_rows=len(status_rows), paired_samples=len(y_list), skip_no_delta=skip_no_delta, skip_no_status=skip_no_status, skip_thermal_zero=skip_thermal_zero, skip_cop_low=skip_cop_low, skip_cop_high=skip_cop_high, used_status_path=used_status_path, used_fallback_path=used_fallback_path, kwh_per_degree=round(self._tank_kwh_per_degree(), 4))
        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
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

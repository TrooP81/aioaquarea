"""Demand model implementation."""

from __future__ import annotations

import datetime as dt

import numpy as np
from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, WeatherRecord
from packages.ml.models_common import GradientBoostingRegressor, HAS_SKLEARN, MODEL_DIR, Pipeline, StandardScaler, _logger, cross_val_score


class DemandModel:
    def __init__(self):
        self._model: Pipeline | None = None
        self._version: str = "untrained"

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    async def train(self) -> dict[str, object]:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required")
        X, y = await self._prepare_data()
        if len(X) < 168:
            return {"error": "Insufficient data", "samples": len(X)}

        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("model", GradientBoostingRegressor(n_estimators=150, max_depth=4, learning_rate=0.1, random_state=42)),
        ])
        scores = cross_val_score(pipeline, X, y, cv=5, scoring="neg_mean_absolute_error")
        pipeline.fit(X, y)

        self._model = pipeline
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        model_path = MODEL_DIR / f"demand_model_{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump

        safe_dump(pipeline, model_path)
        return {"version": self._version, "mae": -scores.mean(), "samples": len(X)}

    def predict_hourly(self, weather_forecast: list[dict], hours: int = 24) -> list[float]:
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
                features = np.array([temp, wind, irradiance, np.sin(2 * np.pi * hour / 24), np.cos(2 * np.pi * hour / 24), np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)]).reshape(1, -1)
                pred = float(self._model.predict(features)[0])
            else:
                pred = max(0.5, 3.0 - 0.1 * temp)
            predictions.append(max(0, pred))
        return predictions

    async def _prepare_data(self) -> tuple[np.ndarray, np.ndarray]:
        async with get_session() as session:
            consumption_rows = (await session.execute(select(ConsumptionRecord.ts, ConsumptionRecord.heat_kwh, ConsumptionRecord.cool_kwh, ConsumptionRecord.tank_kwh, ConsumptionRecord.outdoor_temp).order_by(ConsumptionRecord.ts))).all()
            weather_rows = (await session.execute(select(WeatherRecord.ts, WeatherRecord.wind_speed, WeatherRecord.irradiance).order_by(WeatherRecord.ts))).all()
        if not consumption_rows:
            return np.array([]), np.array([])

        weather_by_hour = {(w.ts.date(), w.ts.hour): {"wind_speed": w.wind_speed, "irradiance": w.irradiance} for w in weather_rows}
        X_list = []
        y_list = []
        for row in consumption_rows:
            total = (row.heat_kwh or 0) + (row.cool_kwh or 0) + (row.tank_kwh or 0)
            hour = row.ts.hour
            dow = row.ts.weekday()
            w = weather_by_hour.get((row.ts.date(), hour), {})
            X_list.append(np.array([row.outdoor_temp or 5.0, w.get("wind_speed") or 3.0, w.get("irradiance") or 0.0, np.sin(2 * np.pi * hour / 24), np.cos(2 * np.pi * hour / 24), np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)]))
            y_list.append(total)
        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
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

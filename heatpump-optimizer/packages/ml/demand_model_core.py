"""Demand model implementation."""

from __future__ import annotations

import datetime as dt

import numpy as np
from sqlalchemy import select

from packages.core.config import settings as app_settings
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, WeatherRecord
from packages.ml.models_common import (
    GradientBoostingRegressor,
    HAS_SKLEARN,
    MODEL_DIR,
    Pipeline,
    StandardScaler,
    _logger,
    cross_val_score,
    iter_consumption_intervals,
)

# Weather samples are typically hourly; only join a consumption interval to a
# weather observation within this gap, otherwise fall back to defaults.
MAX_WEATHER_GAP_SECONDS = 2 * 3600


class DemandModel:
    def __init__(self):
        self._model: Pipeline | None = None
        self._version: str = "untrained"

    def reset(self) -> None:
        """Discard the trained model and return to the untrained fallback state."""
        self._model = None
        self._version = "untrained"

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @staticmethod
    def _make_features(outdoor_temp: float, wind_speed: float, irradiance: float, hour: int, dow: int) -> np.ndarray:
        """Build the 7-feature vector shared by training and prediction.

        Order is fixed for backward compatibility with persisted models:
        [temp, wind, irradiance, hour_sin, hour_cos, dow_sin, dow_cos].
        """
        return np.array([
            outdoor_temp,
            wind_speed,
            irradiance,
            np.sin(2 * np.pi * hour / 24),
            np.cos(2 * np.pi * hour / 24),
            np.sin(2 * np.pi * dow / 7),
            np.cos(2 * np.pi * dow / 7),
        ])

    @staticmethod
    def _max_plausible_rate_kw() -> float:
        """Generous ceiling for electrical demand (kW) used to drop bad intervals."""
        return app_settings.sh_max_power_kw * 1.5 + 3.0

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
            if self._model is not None:
                features = self._make_features(temp, wind, irradiance, ts.hour, ts.weekday()).reshape(1, -1)
                pred = float(self._model.predict(features)[0])
            else:
                pred = max(0.5, 3.0 - 0.1 * temp)
            predictions.append(max(0, pred))
        return predictions

    async def _prepare_data(self) -> tuple[np.ndarray, np.ndarray]:
        async with get_session() as session:
            consumption_rows = (await session.execute(select(ConsumptionRecord.ts, ConsumptionRecord.heat_kwh, ConsumptionRecord.cool_kwh, ConsumptionRecord.tank_kwh, ConsumptionRecord.outdoor_temp).order_by(ConsumptionRecord.ts))).all()
            weather_rows = (await session.execute(select(WeatherRecord.ts, WeatherRecord.temperature, WeatherRecord.wind_speed, WeatherRecord.irradiance).order_by(WeatherRecord.ts))).all()
        if not consumption_rows:
            return np.array([]), np.array([])

        # Nearest-neighbour weather lookup over ALL weather samples (not exact-hour
        # buckets) so every usable consumption interval gets matched to real data.
        weather_seconds = np.array([w.ts.timestamp() for w in weather_rows]) if weather_rows else np.array([])

        def _nearest_weather(ts: dt.datetime):
            if weather_seconds.size == 0:
                return None
            idx = int(np.argmin(np.abs(weather_seconds - ts.timestamp())))
            if abs(weather_rows[idx].ts.timestamp() - ts.timestamp()) > MAX_WEATHER_GAP_SECONDS:
                return None
            return weather_rows[idx]

        max_rate = self._max_plausible_rate_kw()
        X_list: list[np.ndarray] = []
        y_list: list[float] = []
        skip_rate_bounds = 0
        used_weather_temp = 0
        for interval in iter_consumption_intervals(consumption_rows):
            rate = interval.total_rate_kw
            if rate <= 0 or rate > max_rate:
                skip_rate_bounds += 1
                continue

            weather = _nearest_weather(interval.ts)
            outdoor = interval.outdoor_temp
            wind = 3.0
            irradiance = 0.0
            if weather is not None:
                if outdoor is None and weather.temperature is not None:
                    outdoor = weather.temperature
                    used_weather_temp += 1
                if weather.wind_speed is not None:
                    wind = weather.wind_speed
                if weather.irradiance is not None:
                    irradiance = weather.irradiance
            if outdoor is None:
                outdoor = 5.0

            X_list.append(self._make_features(outdoor, wind, irradiance, interval.ts.hour, interval.ts.weekday()))
            y_list.append(rate)

        _logger.info("demand_training_data_prepared", consumption_rows=len(consumption_rows), weather_rows=len(weather_rows), samples=len(y_list), skip_rate_bounds=skip_rate_bounds, used_weather_temp=used_weather_temp, max_rate_kw=round(max_rate, 2))
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

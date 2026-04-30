"""ML models for COP prediction and demand forecasting."""

from __future__ import annotations

import datetime as dt
import json
import pickle
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

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, WeatherRecord

MODEL_DIR = Path("/app/models")
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
        with open(model_path, "wb") as f:
            pickle.dump(pipeline, f)

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

        X_list = []
        y_list = []

        for row in consumption_rows:
            total_kwh = (row.heat_kwh or 0) + (row.tank_kwh or 0)
            if total_kwh <= 0:
                continue

            outdoor_temp = row.outdoor_temp or 5.0
            hour = row.ts.hour
            tank_target = 50  # Default; could match with status

            features = self._make_features(outdoor_temp, tank_target, hour)
            X_list.append(features)
            y_list.append(total_kwh)

        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        """Load the latest saved model."""
        models = sorted(MODEL_DIR.glob("cop_model_*.pkl"))
        if not models:
            return False
        with open(models[-1], "rb") as f:
            self._model = pickle.load(f)
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
        with open(model_path, "wb") as f:
            pickle.dump(pipeline, f)

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
        models = sorted(MODEL_DIR.glob("demand_model_*.pkl"))
        if not models:
            return False
        with open(models[-1], "rb") as f:
            self._model = pickle.load(f)
        self._version = models[-1].stem.replace("demand_model_", "")
        return True

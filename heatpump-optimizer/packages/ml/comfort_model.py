"""Comfort model — learns (water_temp, outdoor_temp, weather) → indoor air temp.

Also provides the *inverse*: given a target indoor temperature, what water supply
temperature should the heat pump deliver?
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from sqlalchemy import select, and_, func

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord, WeatherRecord, IndoorTempReading
from packages.core.config import settings as app_settings

import structlog

logger = structlog.get_logger()

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MIN_TRAINING_ROWS = 100
DEFAULT_THERMAL_LAG_MINUTES = 30

# Candidate lags to search when auto-detecting (minutes)
_LAG_CANDIDATES = [10, 15, 20, 25, 30, 40, 50, 60, 90, 120]

# Reasonable bounds for bisection search
MIN_ZONE_WATER_TEMP = 20.0
MAX_ZONE_WATER_TEMP = 65.0


class ComfortModel:
    """
    Predicts indoor air temperature from heat-pump operating conditions.

    Features (per sample):
        - zone1_temp (water supply temperature °C)
        - outdoor_temp (°C)
        - wind_speed (m/s)
        - irradiance / solar (W/m²)
        - hour_sin, hour_cos (cyclical hour of day)

    Target:
        - indoor air temperature (°C) from SmartThings sensor

    Training data is joined on time — each SmartThings reading is matched
    with the DeviceStatusRecord and WeatherRecord closest in time (shifted
    by the thermal lag so the water-temp reading precedes the air-temp
    measurement).
    """

    def __init__(self) -> None:
        self._model: Pipeline | None = None
        self._metrics: dict[str, float] = {}
        self._last_trained: dt.datetime | None = None
        self._training_samples: int = 0
        self._thermal_lag_minutes: int = DEFAULT_THERMAL_LAG_MINUTES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def last_trained(self) -> dt.datetime | None:
        return self._last_trained

    @property
    def training_samples(self) -> int:
        return self._training_samples

    @property
    def metrics(self) -> dict[str, float]:
        return dict(self._metrics)

    async def train(self, thermal_lag_minutes: int | None = None) -> dict[str, Any]:
        """
        Train (or retrain) the comfort model from the database.

        If *thermal_lag_minutes* is ``None``, the optimal lag is discovered
        automatically by training with each candidate in ``_LAG_CANDIDATES``
        and picking the lag that minimises MAE on the training set.

        Returns a dict with training metrics or an insufficient-data status.
        """
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn is required for the comfort model")

        if thermal_lag_minutes is not None:
            # Explicit lag — train once
            self._thermal_lag_minutes = thermal_lag_minutes
            return await self._train_with_current_lag()

        # --- Auto-detect optimal thermal lag ---
        best_lag: int = DEFAULT_THERMAL_LAG_MINUTES
        best_mae: float = float("inf")
        best_result: dict[str, Any] | None = None

        for candidate in _LAG_CANDIDATES:
            self._thermal_lag_minutes = candidate
            result = await self._train_with_current_lag()

            if result.get("status") == "insufficient_data":
                continue

            mae = result.get("mae", float("inf"))
            if mae < best_mae:
                best_mae = mae
                best_lag = candidate
                best_result = result

        if best_result is None:
            # None of the candidates had enough data
            self._thermal_lag_minutes = DEFAULT_THERMAL_LAG_MINUTES
            return await self._train_with_current_lag()

        # Re-train with the best lag so the model state is correct
        if self._thermal_lag_minutes != best_lag:
            self._thermal_lag_minutes = best_lag
            best_result = await self._train_with_current_lag()

        logger.info(
            "comfort_model_auto_lag",
            best_lag_min=best_lag,
            mae=best_mae,
            candidates_tested=len(_LAG_CANDIDATES),
        )
        best_result["auto_lag_minutes"] = best_lag
        return best_result

    async def _train_with_current_lag(self) -> dict[str, Any]:
        """Train once using the currently set ``_thermal_lag_minutes``."""
        X, y, n_rows = await self._build_dataset()

        if n_rows < MIN_TRAINING_ROWS:
            return {
                "status": "insufficient_data",
                "rows": n_rows,
                "required": MIN_TRAINING_ROWS,
            }

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "gbr",
                    GradientBoostingRegressor(
                        n_estimators=200,
                        max_depth=4,
                        learning_rate=0.05,
                        subsample=0.8,
                        random_state=42,
                    ),
                ),
            ]
        )

        pipeline.fit(X, y)

        # Metrics
        from sklearn.metrics import mean_absolute_error, r2_score

        y_pred = pipeline.predict(X)
        mae = mean_absolute_error(y, y_pred)
        r2 = r2_score(y, y_pred)

        self._model = pipeline
        self._metrics = {
            "mae": round(mae, 3),
            "r2": round(r2, 3),
            "thermal_lag_min": self._thermal_lag_minutes,
        }
        self._last_trained = dt.datetime.now(dt.timezone.utc)
        self._training_samples = n_rows

        # Persist
        self._save()

        logger.info(
            "comfort_model_trained",
            samples=n_rows,
            mae=mae,
            r2=r2,
            thermal_lag_min=self._thermal_lag_minutes,
        )
        return {"status": "trained", "samples": n_rows, **self._metrics}

    def predict_indoor_temp(
        self,
        zone_water_temp: float,
        outdoor_temp: float,
        wind_speed: float = 3.0,
        irradiance: float = 0.0,
        hour: int = 12,
        indoor_temp: float | None = None,
    ) -> float | None:
        """Predict the indoor air temperature given operating conditions.

        *indoor_temp* is the current measured indoor temperature (from
        SmartThings).  Providing it makes the prediction autoregressive:
        the model learns how indoor temp *changes* from the current value
        given the applied water temperature over the thermal-lag window.
        """
        if not self.is_trained:
            return None

        features = self._make_features(
            zone_water_temp, outdoor_temp, wind_speed, irradiance, hour,
            indoor_temp=indoor_temp,
        )
        return float(self._model.predict(features.reshape(1, -1))[0])

    def required_zone_temp(
        self,
        target_indoor: float,
        outdoor_temp: float,
        wind_speed: float = 3.0,
        irradiance: float = 0.0,
        hour: int = 12,
        indoor_temp: float | None = None,
    ) -> float | None:
        """
        Inverse prediction: find the water supply temperature needed to
        achieve *target_indoor* air temperature.

        *indoor_temp* is the current measured indoor temperature (from
        SmartThings).  When provided it anchors the bisection search to
        the building's actual thermal state.

        Uses bisection search over ``[MIN_ZONE_WATER_TEMP, MAX_ZONE_WATER_TEMP]``.
        """
        if not self.is_trained:
            return None

        lo, hi = MIN_ZONE_WATER_TEMP, MAX_ZONE_WATER_TEMP

        # Early bounds check
        pred_lo = self.predict_indoor_temp(lo, outdoor_temp, wind_speed, irradiance, hour, indoor_temp)
        pred_hi = self.predict_indoor_temp(hi, outdoor_temp, wind_speed, irradiance, hour, indoor_temp)

        if pred_lo is None or pred_hi is None:
            return None

        # If even max water temp can't reach target, return max
        if pred_hi < target_indoor:
            return MAX_ZONE_WATER_TEMP

        # If min water temp already exceeds target, return min
        if pred_lo > target_indoor:
            return MIN_ZONE_WATER_TEMP

        # Bisection
        for _ in range(50):
            mid = (lo + hi) / 2.0
            pred = self.predict_indoor_temp(mid, outdoor_temp, wind_speed, irradiance, hour, indoor_temp)
            if pred is None:
                return None
            if abs(pred - target_indoor) < 0.05:
                return round(mid, 1)
            if pred < target_indoor:
                lo = mid
            else:
                hi = mid

        return round((lo + hi) / 2.0, 1)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        from packages.ml.safe_persistence import safe_dump

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = MODEL_DIR / f"comfort_model_{ts}.pkl"
        safe_dump(
            {
                "model": self._model,
                "metrics": self._metrics,
                "trained_at": self._last_trained,
                "samples": self._training_samples,
                "thermal_lag": self._thermal_lag_minutes,
            },
            path,
        )
        logger.info("comfort_model_saved", path=str(path))

    def load_latest(self) -> bool:
        """Load the most recent saved model.  Returns True if loaded."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob("comfort_model_*.pkl"))
        if not models:
            return False
        try:
            data = safe_load(models[-1])
        except ValueError:
            logger.warning("comfort_model_integrity_failed", path=str(models[-1]))
            return False
        self._model = data["model"]
        self._metrics = data.get("metrics", {})
        self._last_trained = data.get("trained_at")
        self._training_samples = data.get("samples", 0)
        self._thermal_lag_minutes = data.get("thermal_lag", DEFAULT_THERMAL_LAG_MINUTES)
        return True

    # ------------------------------------------------------------------
    # Dataset builder
    # ------------------------------------------------------------------

    async def _build_dataset(self) -> tuple[np.ndarray, np.ndarray, int]:
        """
        Join indoor_temp_reading + device_status + weather on time, shifting
        by thermal lag so we correlate *past* water temp with *current* air temp.

        Limits data to the last 30 days to bound memory usage.
        """
        lag = dt.timedelta(minutes=self._thermal_lag_minutes)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)

        async with get_session() as session:
            # Get indoor temp readings from the last 30 days
            result = await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= cutoff)
                .order_by(IndoorTempReading.timestamp)
            )
            readings = result.scalars().all()

            if not readings:
                return np.array([]), np.array([]), 0

            # Prefetch device status and weather for the time range
            earliest = readings[0].timestamp - lag - dt.timedelta(minutes=15)
            latest = readings[-1].timestamp

            status_result = await session.execute(
                select(DeviceStatusRecord)
                .where(
                    and_(
                        DeviceStatusRecord.ts >= earliest,
                        DeviceStatusRecord.ts <= latest,
                    )
                )
                .order_by(DeviceStatusRecord.ts)
            )
            statuses = status_result.scalars().all()

            weather_result = await session.execute(
                select(WeatherRecord)
                .where(
                    and_(
                        WeatherRecord.ts >= earliest,
                        WeatherRecord.ts <= latest,
                    )
                )
                .order_by(WeatherRecord.ts)
            )
            weathers = weather_result.scalars().all()

        if not statuses:
            return np.array([]), np.array([]), 0

        # Build lookup arrays for nearest-neighbor matching
        status_times = np.array([(s.ts - earliest).total_seconds() for s in statuses])
        weather_times = (
            np.array([(w.ts - earliest).total_seconds() for w in weathers])
            if weathers
            else np.array([])
        )
        # Indoor-temp lookup: find prev indoor temp at (T - lag) for each sample
        reading_times = np.array(
            [(r.timestamp - earliest).total_seconds() for r in readings]
        )
        reading_temps = np.array([r.temperature for r in readings])

        X_rows = []
        y_rows = []

        for i, reading in enumerate(readings):
            target_ts = reading.timestamp - lag
            t_sec = (target_ts - earliest).total_seconds()

            # Nearest device status
            idx = int(np.argmin(np.abs(status_times - t_sec)))
            status = statuses[idx]
            # Skip if too far (> 15 min)
            gap = abs((status.ts - target_ts).total_seconds())
            if gap > 900:
                continue

            zone_water_temp = status.zone1_temp
            outdoor_temp = status.outdoor_temp
            if zone_water_temp is None or outdoor_temp is None:
                continue

            # Nearest weather
            wind_speed = 3.0
            irradiance = 0.0
            if len(weather_times) > 0:
                w_idx = int(np.argmin(np.abs(weather_times - t_sec)))
                w = weathers[w_idx]
                wind_speed = w.wind_speed if w.wind_speed is not None else 3.0
                irradiance = getattr(w, "irradiance", 0.0) or 0.0

            # Previous indoor temp at the lag-shifted time
            prev_indoor: float | None = None
            prev_idx = int(np.argmin(np.abs(reading_times - t_sec)))
            prev_gap = abs(reading_times[prev_idx] - t_sec)
            if prev_gap <= 900 and prev_idx != i:  # within 15 min, not self
                prev_indoor = float(reading_temps[prev_idx])

            hour = reading.timestamp.hour
            features = self._make_features(
                zone_water_temp, outdoor_temp, wind_speed, irradiance, hour,
                indoor_temp=prev_indoor,
            )
            X_rows.append(features)
            y_rows.append(reading.temperature)

        n = len(X_rows)
        if n == 0:
            return np.array([]), np.array([]), 0

        return np.array(X_rows), np.array(y_rows), n

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    @staticmethod
    def _make_features(
        zone_water_temp: float,
        outdoor_temp: float,
        wind_speed: float,
        irradiance: float,
        hour: int,
        indoor_temp: float | None = None,
    ) -> np.ndarray:
        hour_rad = 2.0 * np.pi * hour / 24.0
        return np.array(
            [
                zone_water_temp,
                outdoor_temp,
                wind_speed,
                irradiance,
                np.sin(hour_rad),
                np.cos(hour_rad),
                indoor_temp if indoor_temp is not None else outdoor_temp,
            ]
        )


# Module-level singleton
comfort_model = ComfortModel()

"""Demand model implementation."""

from __future__ import annotations

import datetime as dt

import numpy as np
from sqlalchemy import select

from packages.core.config import settings as app_settings
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, WeatherRecord
from packages.ml.models_common import (
    HAS_SKLEARN,
    MODEL_DIR,
    _logger,
    evaluate_regression,
    iter_consumption_intervals,
    make_monotonic_regressor,
    prune_old_models,
    time_series_cv_mae,
    write_mae_baseline,
)

# Weather samples are typically hourly; only join a consumption interval to a
# weather observation within this gap, otherwise fall back to defaults.
MAX_WEATHER_GAP_SECONDS = 2 * 3600
MIN_TRAINING_SAMPLES = 168

# Physical monotonicity for demand features
# [outdoor_temp, wind_speed, irradiance, precipitation, humidity, cloud_cover,
#  hour_sin, hour_cos, dow_sin, dow_cos]:
# heating demand falls as it warms up outside (-1) and as solar gain rises (-1),
# and rises with wind-driven heat loss (+1). Time-of-day/week are unconstrained.
_DEMAND_MONOTONIC_CST = [-1, 1, -1, 0, 0, 0, 0, 0, 0, 0]
DEMAND_MODEL_ARTIFACT_PREFIX = "demand_model_weather_v3_"
DEMAND_MODEL_ARTIFACT_GLOB = f"{DEMAND_MODEL_ARTIFACT_PREFIX}*.pkl"
DEMAND_MAE_BASELINE = "demand_weather_v3"


class DemandModel:
    # Quantile levels for the uncertainty band. The median (0.5) is the point
    # prediction; the optimizer can use the p10/p90 spread to price risk.
    QUANTILES = (0.1, 0.5, 0.9)

    def __init__(self):
        self._model = None  # median (p50) — the point predictor
        self._model_lower = None  # p10
        self._model_upper = None  # p90
        self._version: str = "untrained"
        self._last_data_quality: dict[str, object] = self._empty_data_quality()

    @staticmethod
    def _empty_data_quality() -> dict[str, object]:
        return {
            "raw_records": 0,
            "intervals": 0,
            "usable_samples": 0,
            "minimum_samples": MIN_TRAINING_SAMPLES,
            "rejected_nonpositive": 0,
            "rejected_rate_bounds": 0,
            "weather_records": 0,
            "weather_matches": 0,
            "weather_temperature_fallbacks": 0,
            "max_rate_kw": 0.0,
            "remaining_samples": MIN_TRAINING_SAMPLES,
            "heating_activity_ratio": 0.0,
            "training_blocker": "no_consumption_history",
            "seasonal_guidance": "Waiting for space-heating intervals before demand training can begin.",
            "ready_to_train": False,
        }

    @property
    def last_data_quality(self) -> dict[str, object]:
        """Diagnostics from the most recent data preparation pass."""
        return dict(self._last_data_quality)

    def reset(self) -> None:
        """Discard the trained model and return to the untrained fallback state."""
        self._model = None
        self._model_lower = None
        self._model_upper = None
        self._version = "untrained"

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @staticmethod
    def _make_features(
        outdoor_temp: float,
        wind_speed: float,
        irradiance: float,
        hour: int,
        dow: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> np.ndarray:
        """Build the 10-feature vector shared by training and prediction.

        Order is fixed for this persisted feature schema:
        [temp, wind, irradiance, precipitation, humidity, cloud_cover,
         hour_sin, hour_cos, dow_sin, dow_cos].
        """
        return np.array(
            [
                outdoor_temp,
                wind_speed,
                irradiance,
                max(0.0, precipitation),
                max(0.0, min(100.0, humidity)),
                max(0.0, min(1.0, cloud_cover)),
                np.sin(2 * np.pi * hour / 24),
                np.cos(2 * np.pi * hour / 24),
                np.sin(2 * np.pi * dow / 7),
                np.cos(2 * np.pi * dow / 7),
            ]
        )

    @staticmethod
    def _max_plausible_rate_kw() -> float:
        """Generous ceiling for electrical demand (kW) used to drop bad intervals."""
        return app_settings.sh_max_power_kw * 1.5 + 3.0

    def _make_quantile_model(self, quantile: float):
        """Monotonic gradient-boosting regressor fitting the given quantile."""
        return make_monotonic_regressor(
            _DEMAND_MONOTONIC_CST, max_iter=250, loss="quantile", quantile=quantile
        )

    async def train(self) -> dict[str, object]:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required")
        X, y = await self._prepare_data()
        if len(X) < MIN_TRAINING_SAMPLES:
            return {
                "error": "Insufficient data",
                "samples": len(X),
                "data_quality": self.last_data_quality,
            }

        # Median (p50) model is the deployed point predictor. Evaluate it with
        # forward-chaining time-series CV so the MAE reflects genuine future
        # accuracy (plain KFold leaks later hours into earlier folds).
        median = self._make_quantile_model(0.5)
        mae, cv_std = time_series_cv_mae(median, X, y)

        has_prior = bool(list(MODEL_DIR.glob(DEMAND_MODEL_ARTIFACT_GLOB)))
        decision = evaluate_regression(DEMAND_MAE_BASELINE, mae, has_prior)
        if not decision["deploy"]:
            _logger.warning(
                "demand_model_deploy_skipped",
                mae=round(mae, 3),
                baseline_mae=decision["baseline_mae"],
                samples=len(X),
            )
            return {
                "status": "regressed",
                "mae": mae,
                "baseline_mae": decision["baseline_mae"],
                "samples": len(X),
            }

        median.fit(X, y)
        lower = self._make_quantile_model(0.1)
        lower.fit(X, y)
        upper = self._make_quantile_model(0.9)
        upper.fit(X, y)

        self._model = median
        self._model_lower = lower
        self._model_upper = upper
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        model_path = MODEL_DIR / f"{DEMAND_MODEL_ARTIFACT_PREFIX}{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump

        safe_dump({"median": median, "lower": lower, "upper": upper}, model_path)
        prune_old_models(DEMAND_MODEL_ARTIFACT_GLOB, model_dir=MODEL_DIR)
        write_mae_baseline("demand", mae)
        return {
            "version": self._version,
            "mae": mae,
            "cv_std": cv_std,
            "baseline_mae": decision["baseline_mae"],
            "samples": len(X),
        }

    def predict_hourly(self, weather_forecast: list[dict], hours: int = 24) -> list[float]:
        predictions = []
        now = dt.datetime.now(dt.timezone.utc)
        for h in range(hours):
            ts = now + dt.timedelta(hours=h)
            weather = weather_forecast[h] if h < len(weather_forecast) else {}
            temp = weather.get("temperature", 5.0)
            wind = weather.get("wind_speed", 3.0)
            irradiance = weather.get("irradiance", 0.0)
            precipitation = weather.get("precipitation", 0.0)
            humidity = weather.get("humidity", 60.0)
            cloud_cover = weather.get("cloud_cover", 0.5)
            if self._model is not None:
                features = self._make_features(
                    temp,
                    wind,
                    irradiance,
                    ts.hour,
                    ts.weekday(),
                    precipitation,
                    humidity,
                    cloud_cover,
                ).reshape(1, -1)
                pred = float(self._model.predict(features)[0])
            else:
                pred = max(0.5, 3.0 - 0.1 * temp)
            predictions.append(max(0, pred))
        return predictions

    def predict_hourly_quantiles(self, weather_forecast: list[dict], hours: int = 24) -> list[dict]:
        """Predict demand with an uncertainty band per hour.

        Returns a list of ``{"p10", "p50", "p90"}`` dicts (kW). When the model
        is untrained, the same heuristic point estimate is returned for all
        three levels so callers get a consistent shape. ``p10 <= p50 <= p90`` is
        enforced (separately-fit quantile regressors can occasionally cross).
        """
        results: list[dict] = []
        now = dt.datetime.now(dt.timezone.utc)
        for h in range(hours):
            ts = now + dt.timedelta(hours=h)
            weather = weather_forecast[h] if h < len(weather_forecast) else {}
            temp = weather.get("temperature", 5.0)
            wind = weather.get("wind_speed", 3.0)
            irradiance = weather.get("irradiance", 0.0)
            precipitation = weather.get("precipitation", 0.0)
            humidity = weather.get("humidity", 60.0)
            cloud_cover = weather.get("cloud_cover", 0.5)
            if self._model is not None:
                features = self._make_features(
                    temp,
                    wind,
                    irradiance,
                    ts.hour,
                    ts.weekday(),
                    precipitation,
                    humidity,
                    cloud_cover,
                ).reshape(1, -1)
                p50 = float(self._model.predict(features)[0])
                p10 = (
                    float(self._model_lower.predict(features)[0])
                    if self._model_lower is not None
                    else p50
                )
                p90 = (
                    float(self._model_upper.predict(features)[0])
                    if self._model_upper is not None
                    else p50
                )
            else:
                p50 = max(0.5, 3.0 - 0.1 * temp)
                p10 = p90 = p50
            # Clamp to non-negative and repair any quantile crossing.
            p10, p50, p90 = (max(0.0, v) for v in (p10, p50, p90))
            p50 = max(p50, p10)
            p90 = max(p90, p50)
            results.append({"p10": p10, "p50": p50, "p90": p90})
        return results

    async def training_data_quality(self) -> dict[str, object]:
        """Return live, explainable readiness data without training a model."""
        await self._prepare_data()
        return self.last_data_quality

    async def _prepare_data(self) -> tuple[np.ndarray, np.ndarray]:
        async with get_session() as session:
            consumption_rows = (
                await session.execute(
                    select(
                        ConsumptionRecord.ts,
                        ConsumptionRecord.heat_kwh,
                        ConsumptionRecord.cool_kwh,
                        ConsumptionRecord.tank_kwh,
                        ConsumptionRecord.outdoor_temp,
                    ).order_by(ConsumptionRecord.ts)
                )
            ).all()
            weather_rows = (
                await session.execute(
                    select(
                        WeatherRecord.ts,
                        WeatherRecord.temperature,
                        WeatherRecord.wind_speed,
                        WeatherRecord.irradiance,
                        WeatherRecord.precipitation,
                        WeatherRecord.humidity,
                        WeatherRecord.cloud_cover,
                    ).order_by(WeatherRecord.ts)
                )
            ).all()
        if not consumption_rows:
            self._last_data_quality = self._empty_data_quality()
            return np.array([]), np.array([])

        # Nearest-neighbour weather lookup over ALL weather samples (not exact-hour
        # buckets) so every usable consumption interval gets matched to real data.
        weather_seconds = (
            np.array([w.ts.timestamp() for w in weather_rows]) if weather_rows else np.array([])
        )

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
        intervals = 0
        skip_rate_bounds = 0
        rejected_nonpositive = 0
        weather_matches = 0
        weather_temperature_fallbacks = 0
        for interval in iter_consumption_intervals(consumption_rows):
            intervals += 1
            # Target is *space-heating* electrical demand only. DHW and cooling
            # are excluded because they violate the model's monotonic physics
            # (DHW is outdoor-independent; cooling demand rises — not falls —
            # with outdoor temperature). Intervals with no space-heating draw
            # are naturally dropped by the rate<=0 guard below.
            rate = interval.heat_rate_kw
            if rate <= 0:
                rejected_nonpositive += 1
                continue
            if rate > max_rate:
                skip_rate_bounds += 1
                continue

            weather = _nearest_weather(interval.ts)
            outdoor = interval.outdoor_temp
            wind = 3.0
            irradiance = 0.0
            precipitation = 0.0
            humidity = 60.0
            cloud_cover = 0.5
            if weather is not None:
                weather_matches += 1
                # The configured weather report is the canonical ambient
                # temperature. The Aquarea sensor may be sun-heated depending
                # on installation, so it is only a fallback when weather is
                # unavailable.
                if weather.temperature is not None:
                    outdoor = weather.temperature
                else:
                    weather_temperature_fallbacks += 1
                if weather.wind_speed is not None:
                    wind = weather.wind_speed
                if weather.irradiance is not None:
                    irradiance = weather.irradiance
                weather_precipitation = getattr(weather, "precipitation", None)
                if weather_precipitation is not None:
                    precipitation = weather_precipitation
                weather_humidity = getattr(weather, "humidity", None)
                weather_cloud_cover = getattr(weather, "cloud_cover", None)
                if weather_humidity is not None:
                    humidity = weather_humidity
                if weather_cloud_cover is not None:
                    cloud_cover = weather_cloud_cover
            if outdoor is None:
                outdoor = 5.0

            X_list.append(
                self._make_features(
                    outdoor,
                    wind,
                    irradiance,
                    interval.ts.hour,
                    interval.ts.weekday(),
                    precipitation,
                    humidity,
                    cloud_cover,
                )
            )
            y_list.append(rate)

        heating_activity_ratio = len(y_list) / intervals if intervals else 0.0
        ready_to_train = len(y_list) >= MIN_TRAINING_SAMPLES
        if ready_to_train:
            training_blocker = None
            seasonal_guidance = "Enough space-heating intervals are available for training."
        elif intervals and heating_activity_ratio < 0.2:
            training_blocker = "waiting_for_space_heating_season"
            seasonal_guidance = (
                "Most observed intervals have no space-heating draw. This is expected in mild weather; "
                "the model will continue collecting evidence until the heating season resumes."
            )
        else:
            training_blocker = "collecting_space_heating_intervals"
            seasonal_guidance = (
                "Collecting additional valid space-heating intervals before training."
            )

        self._last_data_quality = {
            "raw_records": len(consumption_rows),
            "intervals": intervals,
            "usable_samples": len(y_list),
            "minimum_samples": MIN_TRAINING_SAMPLES,
            "rejected_nonpositive": rejected_nonpositive,
            "rejected_rate_bounds": skip_rate_bounds,
            "weather_records": len(weather_rows),
            "weather_matches": weather_matches,
            "weather_temperature_fallbacks": weather_temperature_fallbacks,
            "max_rate_kw": round(max_rate, 2),
            "remaining_samples": max(0, MIN_TRAINING_SAMPLES - len(y_list)),
            "heating_activity_ratio": round(heating_activity_ratio, 3),
            "training_blocker": training_blocker,
            "seasonal_guidance": seasonal_guidance,
            "ready_to_train": ready_to_train,
        }
        _logger.info("demand_training_data_prepared", **self._last_data_quality)
        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob(DEMAND_MODEL_ARTIFACT_GLOB))
        if not models:
            _logger.info(
                "demand_model_load_skip", reason="no model files found", dir=str(MODEL_DIR)
            )
            return False
        for path in reversed(models):
            try:
                payload = safe_load(path)
            except ValueError as exc:
                _logger.warning("demand_model_load_failed", path=str(path), error=str(exc))
                continue
            candidate = payload.get("median") if isinstance(payload, dict) else payload
            if getattr(candidate, "n_features_in_", None) != len(_DEMAND_MONOTONIC_CST):
                _logger.info(
                    "demand_model_load_skip", path=str(path), reason="obsolete_feature_schema"
                )
                continue
            self._model = candidate
            self._model_lower = payload.get("lower") if isinstance(payload, dict) else None
            self._model_upper = payload.get("upper") if isinstance(payload, dict) else None
            self._version = path.stem.replace(DEMAND_MODEL_ARTIFACT_PREFIX, "")
            _logger.info(
                "demand_model_loaded",
                version=self._version,
                path=str(path),
                has_quantiles=self._model_lower is not None,
            )
            return True
        return False

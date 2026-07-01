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
    time_series_cv_mae,
    write_mae_baseline,
)

# Weather samples are typically hourly; only join a consumption interval to a
# weather observation within this gap, otherwise fall back to defaults.
MAX_WEATHER_GAP_SECONDS = 2 * 3600

# Physical monotonicity for demand features
# [outdoor_temp, wind_speed, irradiance, hour_sin, hour_cos, dow_sin, dow_cos]:
# heating demand falls as it warms up outside (-1) and as solar gain rises (-1),
# and rises with wind-driven heat loss (+1). Time-of-day/week are unconstrained.
_DEMAND_MONOTONIC_CST = [-1, 1, -1, 0, 0, 0, 0]


class DemandModel:
    # Quantile levels for the uncertainty band. The median (0.5) is the point
    # prediction; the optimizer can use the p10/p90 spread to price risk.
    QUANTILES = (0.1, 0.5, 0.9)

    def __init__(self):
        self._model = None  # median (p50) — the point predictor
        self._model_lower = None  # p10
        self._model_upper = None  # p90
        self._version: str = "untrained"

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

    def _make_quantile_model(self, quantile: float):
        """Monotonic gradient-boosting regressor fitting the given quantile."""
        return make_monotonic_regressor(
            _DEMAND_MONOTONIC_CST, max_iter=250, loss="quantile", quantile=quantile
        )

    async def train(self) -> dict[str, object]:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required")
        X, y = await self._prepare_data()
        if len(X) < 168:
            return {"error": "Insufficient data", "samples": len(X)}

        # Median (p50) model is the deployed point predictor. Evaluate it with
        # forward-chaining time-series CV so the MAE reflects genuine future
        # accuracy (plain KFold leaks later hours into earlier folds).
        median = self._make_quantile_model(0.5)
        mae, cv_std = time_series_cv_mae(median, X, y)

        has_prior = bool(list(MODEL_DIR.glob("demand_model_*.pkl")))
        decision = evaluate_regression("demand", mae, has_prior)
        if not decision["deploy"]:
            _logger.warning("demand_model_deploy_skipped", mae=round(mae, 3), baseline_mae=decision["baseline_mae"], samples=len(X))
            return {"status": "regressed", "mae": mae, "baseline_mae": decision["baseline_mae"], "samples": len(X)}

        median.fit(X, y)
        lower = self._make_quantile_model(0.1)
        lower.fit(X, y)
        upper = self._make_quantile_model(0.9)
        upper.fit(X, y)

        self._model = median
        self._model_lower = lower
        self._model_upper = upper
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        model_path = MODEL_DIR / f"demand_model_{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump

        safe_dump({"median": median, "lower": lower, "upper": upper}, model_path)
        write_mae_baseline("demand", mae)
        return {"version": self._version, "mae": mae, "cv_std": cv_std, "baseline_mae": decision["baseline_mae"], "samples": len(X)}

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
            if self._model is not None:
                features = self._make_features(temp, wind, irradiance, ts.hour, ts.weekday()).reshape(1, -1)
                p50 = float(self._model.predict(features)[0])
                p10 = float(self._model_lower.predict(features)[0]) if self._model_lower is not None else p50
                p90 = float(self._model_upper.predict(features)[0]) if self._model_upper is not None else p50
            else:
                p50 = max(0.5, 3.0 - 0.1 * temp)
                p10 = p90 = p50
            # Clamp to non-negative and repair any quantile crossing.
            p10, p50, p90 = (max(0.0, v) for v in (p10, p50, p90))
            p50 = max(p50, p10)
            p90 = max(p90, p50)
            results.append({"p10": p10, "p50": p50, "p90": p90})
        return results

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
            # Target is *space-heating* electrical demand only. DHW and cooling
            # are excluded because they violate the model's monotonic physics
            # (DHW is outdoor-independent; cooling demand rises — not falls —
            # with outdoor temperature). Intervals with no space-heating draw
            # are naturally dropped by the rate<=0 guard below.
            rate = interval.heat_rate_kw
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
            payload = safe_load(models[-1])
        except ValueError as exc:
            _logger.warning("demand_model_load_failed", path=str(models[-1]), error=str(exc))
            return False
        # New format persists a dict of quantile models; older files stored a
        # single bare estimator. Support both for backward compatibility.
        if isinstance(payload, dict):
            self._model = payload.get("median")
            self._model_lower = payload.get("lower")
            self._model_upper = payload.get("upper")
        else:
            self._model = payload
            self._model_lower = None
            self._model_upper = None
        self._version = models[-1].stem.replace("demand_model_", "")
        _logger.info("demand_model_loaded", version=self._version, path=str(models[-1]), has_quantiles=self._model_lower is not None)
        return True

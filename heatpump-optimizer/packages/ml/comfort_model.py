"""Comfort model — learns (water_temp, outdoor_temp, weather) → indoor air temp.

Also provides the *inverse*: given a target indoor temperature, what water supply
temperature should the heat pump deliver?
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    import sklearn.ensemble  # noqa: F401  (availability probe)

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from sqlalchemy import and_, select

from packages.core.database import get_session
from packages.core.heat_curve import HeatCurveConfig, effective_zone_target_temperature
from packages.core.heating_evidence import has_confirmed_space_heating
from packages.core.models import DeviceStatusRecord, WeatherRecord, IndoorTempReading
from packages.core.config import settings as app_settings
from packages.core.settings_service import get_all_settings
from packages.ml.models_common import (
    make_monotonic_regressor,
    prune_old_models,
    read_mae_baseline,
    write_mae_baseline,
)

import structlog

logger = structlog.get_logger()

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MIN_TRAINING_ROWS = 100
# The optimiser forecasts in whole-hour steps. Training a ten-minute target
# then rolling it forward hourly compounded error in the plan horizon.
DEFAULT_THERMAL_LAG_MINUTES = 60

# A checkpoint can be useful for charts before it is safe enough to influence
# a physical controller. Keep weak or in-sample-only models observational.
MAX_CONTROL_MAE_C = 0.60
MIN_CONTROL_R2 = 0.15
MIN_CONTROL_ACTIVE_HEATING_ROWS = 20
MIN_CONTROL_MARGIN_C = 0.15
MAX_CONTROL_MARGIN_C = 0.45

# Earlier artifacts were trained with nearest-neighbour indoor and status
# samples, which could select values recorded *after* the feature timestamp.
# Keep them separate from the causal dataset definition below.
COMFORT_MODEL_ARTIFACT_PREFIX = "comfort_model_weather_causal_v5_"
COMFORT_MODEL_ARTIFACT_GLOB = f"{COMFORT_MODEL_ARTIFACT_PREFIX}*.pkl"

# Candidate lags surround the one-hour planning step and are selected with a
# chronological validation split.
_LAG_CANDIDATES = [45, 60, 75, 90]
# Direct models avoid repeatedly feeding a one-hour estimate back into itself
# for the chart's longer lead times. They stay observational until the common
# comfort-control readiness gate approves the primary model.
DIRECT_FORECAST_HORIZONS_MINUTES = (60, 180, 360, 720)
MAX_PASSIVE_DIRECT_MAE_C = 1.0

# Reasonable bounds for bisection search
MIN_ZONE_WATER_TEMP = 20.0
MAX_ZONE_WATER_TEMP = 65.0

# Physical monotonicity constraints for the 14 model features, in order:
# [zone_water_temp, zone_target_temp, space_heating_fraction,
#  recent_heat_fraction, outdoor_temp, wind_speed, irradiance, precipitation,
#  humidity, cloud_cover, hour_sin, hour_cos, indoor_temp, indoor_trend]
#   +1 = predicted indoor must not decrease as the feature increases
#   -1 = predicted indoor must not increase as the feature increases
#    0 = unconstrained
# More heat input (water temp), warmer outside, more sun, and a warmer current
# indoor temperature can only raise (or hold) the predicted indoor temperature;
# stronger wind can only lower (or hold) it. This guarantees, for example, that a
# heating forecast is never below the no-heating baseline — even when training
# data is noisy.
_MONOTONIC_CST = [1, 1, 1, 1, 1, -1, 1, 0, 0, -1, 0, 0, 1, 0]
_INDOOR_TEMPERATURE_FEATURE_INDEX = 12

# Fraction of (time-ordered) samples held out at the end for honest validation.
_VALIDATION_FRACTION = 0.2
_MIN_VALIDATION_ROWS = 10


@dataclass(frozen=True)
class _IndoorObservation:
    timestamp: dt.datetime
    temperature: float


class ComfortModel:
    """
    Predicts indoor air temperature from heat-pump operating conditions.

    Features (per sample):
        - zone1_temp (water supply temperature °C)
        - reported heat-curve target plus current and recent confirmed
          space-heating fractions
        - outdoor_temp (°C)
        - wind_speed (m/s)
        - irradiance / solar (W/m²)
        - precipitation (mm/h), humidity (%), and cloud cover (0–1)
        - hour_sin, hour_cos (cyclical hour of day)
        - current indoor temperature and its one-hour trend

    Target:
        - indoor air temperature (°C) from SmartThings sensor

    Training data is joined causally — each target is paired only with
    DeviceStatusRecord, WeatherRecord, and indoor observations that existed
    before the target time.  The heat-pump state is shifted by the selected
    thermal lag so the input precedes the response.
    """

    def __init__(self) -> None:
        self._model: Any | None = None
        self._direct_models: dict[int, Any] = {}
        self._metrics: dict[str, Any] = {}
        self._last_trained: dt.datetime | None = None
        self._training_samples: int = 0
        self._thermal_lag_minutes: int = DEFAULT_THERMAL_LAG_MINUTES
        self._last_dataset_evidence: dict[str, Any] = {"active_heating_rows": 0}
        self._training_notice: str | None = None

    def reset(self) -> None:
        """Discard the trained model and learned metadata."""
        self._model = None
        self._direct_models = {}
        self._metrics = {}
        self._last_trained = None
        self._training_samples = 0
        self._thermal_lag_minutes = DEFAULT_THERMAL_LAG_MINUTES
        self._last_dataset_evidence = {"active_heating_rows": 0}
        self._training_notice = None

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
    def direct_forecast_horizons_minutes(self) -> tuple[int, ...]:
        """Lead times with an independently trained forecast model."""
        return tuple(sorted(self._direct_models))

    def passive_forecast_readiness(self, forecast_horizon_minutes: int) -> dict[str, Any]:
        """Describe whether a direct, no-space-heat forecast is trustworthy.

        Passive forecasts do not infer a heating response, so they can safely
        use direct models during summer even while the full comfort controller
        is observation-only pending verified space-heating intervals.
        """

        if not self.is_trained or not self._direct_models:
            return {"ready": False, "reason": "no_direct_forecast_model"}
        horizon = min(
            self._direct_models,
            key=lambda candidate: abs(candidate - int(forecast_horizon_minutes)),
        )
        direct_metrics = self._metrics.get("direct_horizons", {})
        metrics = direct_metrics.get(str(horizon), {}) if isinstance(direct_metrics, dict) else {}
        mae = metrics.get("mae") if isinstance(metrics, dict) else None
        if metrics.get("status") != "trained" or not isinstance(mae, (int, float)):
            return {
                "ready": False,
                "reason": "direct_forecast_not_validated",
                "horizon_minutes": horizon,
            }
        if float(mae) > MAX_PASSIVE_DIRECT_MAE_C:
            return {
                "ready": False,
                "reason": "direct_forecast_mae_above_threshold",
                "horizon_minutes": horizon,
                "mae": float(mae),
                "maximum_mae": MAX_PASSIVE_DIRECT_MAE_C,
            }
        return {"ready": True, "horizon_minutes": horizon, "mae": float(mae)}

    def predict_passive_indoor_temp(
        self,
        *,
        outdoor_temp: float,
        wind_speed: float = 3.0,
        irradiance: float = 0.0,
        hour: int = 12,
        indoor_temp: float,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
        forecast_horizon_minutes: int,
    ) -> tuple[float | None, dict[str, Any]]:
        """Direct weather-aware indoor forecast with no space heating input."""

        readiness = self.passive_forecast_readiness(forecast_horizon_minutes)
        if not readiness["ready"]:
            return None, readiness
        predicted = self.predict_indoor_temp(
            zone_water_temp=outdoor_temp,
            outdoor_temp=outdoor_temp,
            wind_speed=wind_speed,
            irradiance=irradiance,
            hour=hour,
            indoor_temp=indoor_temp,
            precipitation=precipitation,
            humidity=humidity,
            cloud_cover=cloud_cover,
            zone_target_temp=outdoor_temp,
            space_heating_fraction=0.0,
            recent_heat_fraction=0.0,
            forecast_horizon_minutes=forecast_horizon_minutes,
        )
        return predicted, readiness

    @property
    def metrics(self) -> dict[str, Any]:
        return dict(self._metrics)

    @property
    def training_notice(self) -> str | None:
        """Explain an intentional retraining requirement to the UI."""

        return self._training_notice

    @property
    def control_readiness(self) -> dict[str, Any]:
        """Explain whether this checkpoint may influence live control."""

        if not self.is_trained:
            return {"ready": False, "reason": "model_untrained"}
        if not self._metrics.get("validated"):
            return {"ready": False, "reason": "metrics_not_out_of_sample"}
        mae = self._metrics.get("mae")
        r2 = self._metrics.get("r2")
        if not isinstance(mae, (float, int)) or not isinstance(r2, (float, int)):
            return {"ready": False, "reason": "metrics_missing"}
        if mae > MAX_CONTROL_MAE_C:
            return {
                "ready": False,
                "reason": "mae_above_control_threshold",
                "mae": mae,
                "max_mae": MAX_CONTROL_MAE_C,
            }
        if r2 < MIN_CONTROL_R2:
            return {
                "ready": False,
                "reason": "r2_below_control_threshold",
                "r2": r2,
                "min_r2": MIN_CONTROL_R2,
            }
        active_heating_rows = self._metrics.get("active_heating_rows")
        if (
            not isinstance(active_heating_rows, int)
            or active_heating_rows < MIN_CONTROL_ACTIVE_HEATING_ROWS
        ):
            return {
                "ready": False,
                "reason": "insufficient_active_heating_evidence",
                "active_heating_rows": active_heating_rows or 0,
                "minimum_active_heating_rows": MIN_CONTROL_ACTIVE_HEATING_ROWS,
            }
        baseline_mae = self._metrics.get("baseline_mae")
        if isinstance(baseline_mae, (float, int)) and mae >= baseline_mae:
            return {
                "ready": False,
                "reason": "not_better_than_persistence_baseline",
                "mae": mae,
                "baseline_mae": baseline_mae,
            }
        return {"ready": True, "reason": "validated_metrics_passed", "mae": mae, "r2": r2}

    @property
    def is_ready_for_control(self) -> bool:
        return bool(self.control_readiness["ready"])

    @property
    def control_margin_c(self) -> float:
        """Bounded comfort reserve derived from validated forecast error.

        The margin is applied only to a planning constraint, never written as a
        thermostat target. It lets the MILP protect comfort when an otherwise
        control-ready model still has a non-zero out-of-sample error.
        """

        if not self.is_ready_for_control:
            return 0.0
        mae = self._metrics.get("mae")
        if not isinstance(mae, (int, float)):
            return 0.0
        return round(min(MAX_CONTROL_MARGIN_C, max(MIN_CONTROL_MARGIN_C, float(mae) * 0.75)), 2)

    async def train(self, thermal_lag_minutes: int | None = None) -> dict[str, Any]:
        """
        Train (or retrain) the comfort model from the database.

        If *thermal_lag_minutes* is ``None``, the optimal lag is discovered
        from hour-aligned candidates using chronological validation MAE.

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
            result = await self._train_with_current_lag(persist=False, log_result=False)

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

        # Candidate fits are intentionally ephemeral. Persist and report only
        # the selected lag so operators do not see a stream of misleading
        # "trained" events for models that were never selected.
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

    async def _train_with_current_lag(
        self, *, persist: bool = True, log_result: bool = True
    ) -> dict[str, Any]:
        """Train once using the currently set ``_thermal_lag_minutes``.

        Metrics are computed on a chronological hold-out (the most recent
        ``_VALIDATION_FRACTION`` of samples) so the reported MAE/R² reflect
        out-of-sample accuracy rather than how well the model memorised the
        training set. The deployed model is then refit on *all* available
        samples for the best possible predictions.
        """
        X, y, n_rows = await self._build_dataset()

        if n_rows < MIN_TRAINING_ROWS:
            return {
                "status": "insufficient_data",
                "rows": n_rows,
                "required": MIN_TRAINING_ROWS,
            }

        from sklearn.metrics import mean_absolute_error, r2_score

        # Honest, out-of-sample metrics on the most recent slice of data.
        split = int(n_rows * (1.0 - _VALIDATION_FRACTION))
        n_val = n_rows - split
        if n_val >= _MIN_VALIDATION_ROWS:
            eval_model = self._build_regressor()
            eval_model.fit(X[:split], y[:split])
            y_val_pred = eval_model.predict(X[split:])
            mae = mean_absolute_error(y[split:], y_val_pred)
            r2 = r2_score(y[split:], y_val_pred)
            baseline_mae = mean_absolute_error(
                y[split:], X[split:, _INDOOR_TEMPERATURE_FEATURE_INDEX]
            )
            validated = True
        else:
            # Too few rows to hold out — fall back to in-sample metrics.
            tmp = self._build_regressor()
            tmp.fit(X, y)
            y_pred = tmp.predict(X)
            mae = mean_absolute_error(y, y_pred)
            r2 = r2_score(y, y_pred)
            baseline_mae = mean_absolute_error(y, X[:, _INDOOR_TEMPERATURE_FEATURE_INDEX])
            validated = False

        # Candidate evaluations must not replace the current checkpoint.
        model = self._build_regressor()
        model.fit(X, y)

        if not persist:
            return {
                "status": "evaluated",
                "samples": n_rows,
                "mae": round(float(mae), 3),
                "r2": round(float(r2), 3),
                "validated": validated,
            }

        # Do not replace a deployed checkpoint with a worse chronological
        # validation score. Comfort control is a physical decision, so this is
        # deliberately stricter than the generic model regression tolerance.
        prior_mae = read_mae_baseline("comfort")
        # Compare only with the checkpoint actually loaded by this service.
        # A stale file/baseline from another process must not make a fresh
        # model instance reject training based on unrelated historical state.
        has_prior_checkpoint = self.is_trained
        if has_prior_checkpoint and prior_mae is not None and mae > prior_mae:
            logger.warning(
                "comfort_model_retrain_regressed",
                mae=round(float(mae), 3),
                prior_mae=round(float(prior_mae), 3),
            )
            # Direct lead-time forecasts are observational additions. They do
            # not replace the approved one-step checkpoint or relax its
            # readiness gate, so it is safe to attach them even when a primary
            # retrain is rejected for worse validation MAE.
            direct_metrics: dict[str, dict[str, Any]] = {}
            if self._model is not None and not self._direct_models:
                self._direct_models, direct_metrics = await self._train_direct_forecasts()
                self._metrics["direct_horizons"] = direct_metrics
                self._save()
            return {
                "status": "regressed",
                "samples": n_rows,
                "mae": round(float(mae), 3),
                "prior_deployed_mae": round(float(prior_mae), 3),
                "direct_horizons": direct_metrics,
            }

        self._model = model
        self._training_notice = None
        self._metrics = {
            "mae": round(float(mae), 3),
            "r2": round(float(r2), 3),
            "thermal_lag_min": self._thermal_lag_minutes,
            "training_horizon_minutes": self._thermal_lag_minutes,
            "validated": validated,
            "baseline_mae": round(float(baseline_mae), 3),
            "prior_deployed_mae": round(float(prior_mae), 3) if prior_mae is not None else None,
            "active_heating_rows": self._last_dataset_evidence.get("active_heating_rows", 0),
            "sensor_strategy": self._last_dataset_evidence.get("sensor_strategy", "unknown"),
            "source_sensor_count": self._last_dataset_evidence.get("source_sensor_count", 0),
        }
        self._last_trained = dt.datetime.now(dt.timezone.utc)
        self._training_samples = n_rows

        self._direct_models, direct_metrics = await self._train_direct_forecasts()
        self._metrics["direct_horizons"] = direct_metrics
        self._save()
        write_mae_baseline("comfort", float(mae))

        if log_result:
            logger.info(
                "comfort_model_trained",
                samples=n_rows,
                mae=mae,
                r2=r2,
                baseline_mae=baseline_mae,
                active_heating_rows=self._last_dataset_evidence.get("active_heating_rows", 0),
                validated=validated,
                thermal_lag_min=self._thermal_lag_minutes,
            )
        return {"status": "trained", "samples": n_rows, **self._metrics}

    async def _train_direct_forecasts(self) -> tuple[dict[int, Any], dict[str, dict[str, Any]]]:
        """Fit independent lead-time models from the same causal feature set.

        Each target is paired with the state at the beginning of its own lead
        time.  This prevents a 12-hour forecast from accumulating eleven
        one-hour prediction errors.  Sparse lead times are reported rather
        than substituted into live control.
        """
        from sklearn.metrics import mean_absolute_error, r2_score

        original_lag = self._thermal_lag_minutes
        models: dict[int, Any] = {}
        metrics: dict[str, dict[str, Any]] = {}
        try:
            for horizon in DIRECT_FORECAST_HORIZONS_MINUTES:
                self._thermal_lag_minutes = horizon
                X, y, rows = await self._build_dataset()
                if rows < MIN_TRAINING_ROWS:
                    metrics[str(horizon)] = {"status": "insufficient_data", "samples": rows}
                    continue
                split = int(rows * (1.0 - _VALIDATION_FRACTION))
                if rows - split < _MIN_VALIDATION_ROWS:
                    metrics[str(horizon)] = {"status": "insufficient_validation", "samples": rows}
                    continue
                evaluation = self._build_regressor()
                evaluation.fit(X[:split], y[:split])
                predicted = evaluation.predict(X[split:])
                direct = self._build_regressor()
                direct.fit(X, y)
                models[horizon] = direct
                metrics[str(horizon)] = {
                    "status": "trained",
                    "samples": rows,
                    "mae": round(float(mean_absolute_error(y[split:], predicted)), 3),
                    "r2": round(float(r2_score(y[split:], predicted)), 3),
                }
        finally:
            self._thermal_lag_minutes = original_lag
        return models, metrics

    @staticmethod
    def _build_regressor():
        """Build the monotonic gradient-boosting regressor for indoor-temp prediction."""
        return make_monotonic_regressor(_MONOTONIC_CST)

    def predict_indoor_temp(
        self,
        zone_water_temp: float,
        outdoor_temp: float,
        wind_speed: float = 3.0,
        irradiance: float = 0.0,
        hour: int = 12,
        indoor_temp: float | None = None,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
        zone_target_temp: float | None = None,
        space_heating_fraction: float = 0.0,
        recent_heat_fraction: float = 0.0,
        indoor_trend_c_per_hour: float = 0.0,
        forecast_horizon_minutes: int | None = None,
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
            zone_water_temp,
            outdoor_temp,
            wind_speed,
            irradiance,
            hour,
            indoor_temp=indoor_temp,
            precipitation=precipitation,
            humidity=humidity,
            cloud_cover=cloud_cover,
            zone_target_temp=zone_target_temp,
            space_heating_fraction=space_heating_fraction,
            recent_heat_fraction=recent_heat_fraction,
            indoor_trend_c_per_hour=indoor_trend_c_per_hour,
        )
        model = self._model
        if forecast_horizon_minutes is not None and self._direct_models:
            horizon = min(
                self._direct_models,
                key=lambda candidate: abs(candidate - int(forecast_horizon_minutes)),
            )
            model = self._direct_models[horizon]
        return float(model.predict(features.reshape(1, -1))[0])

    def required_zone_temp(
        self,
        target_indoor: float,
        outdoor_temp: float,
        wind_speed: float = 3.0,
        irradiance: float = 0.0,
        hour: int = 12,
        indoor_temp: float | None = None,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
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

        # Early bounds check. The inverse question explicitly assumes the
        # controller is asking for room heat rather than merely carrying warm
        # water in an idle circuit.
        common = {
            "outdoor_temp": outdoor_temp,
            "wind_speed": wind_speed,
            "irradiance": irradiance,
            "hour": hour,
            "indoor_temp": indoor_temp,
            "precipitation": precipitation,
            "humidity": humidity,
            "cloud_cover": cloud_cover,
            "space_heating_fraction": 1.0,
            "recent_heat_fraction": 1.0,
        }
        pred_lo = self.predict_indoor_temp(lo, zone_target_temp=lo, **common)
        pred_hi = self.predict_indoor_temp(hi, zone_target_temp=hi, **common)

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
            pred = self.predict_indoor_temp(mid, zone_target_temp=mid, **common)
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
        path = MODEL_DIR / f"{COMFORT_MODEL_ARTIFACT_PREFIX}{ts}.pkl"
        safe_dump(
            {
                "model": self._model,
                "direct_models": self._direct_models,
                "metrics": self._metrics,
                "trained_at": self._last_trained,
                "samples": self._training_samples,
                "thermal_lag": self._thermal_lag_minutes,
                "feature_schema": "causal_v4_hourly_heat_and_trend",
            },
            path,
        )
        prune_old_models(COMFORT_MODEL_ARTIFACT_GLOB, model_dir=MODEL_DIR)
        logger.info("comfort_model_saved", path=str(path))

    def load_latest(self) -> bool:
        """Load the most recent saved model.  Returns True if loaded."""
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob(COMFORT_MODEL_ARTIFACT_GLOB))
        if not models:
            legacy_models = list(MODEL_DIR.glob("comfort_model_*.pkl"))
            if legacy_models:
                self._training_notice = (
                    "A previous comfort-model artifact uses an older feature schema and was "
                    "retired safely. Retrain to use confirmed heating evidence."
                )
            return False
        try:
            data = safe_load(models[-1])
        except ValueError:
            logger.warning("comfort_model_integrity_failed", path=str(models[-1]))
            return False
        candidate = data["model"]
        if getattr(candidate, "n_features_in_", None) != len(_MONOTONIC_CST):
            logger.info(
                "comfort_model_load_skip", path=str(models[-1]), reason="obsolete_feature_schema"
            )
            return False
        self._model = candidate
        direct_models = data.get("direct_models", {})
        self._direct_models = (
            {
                int(horizon): model
                for horizon, model in direct_models.items()
                if int(horizon) in DIRECT_FORECAST_HORIZONS_MINUTES
                and getattr(model, "n_features_in_", None) == len(_MONOTONIC_CST)
            }
            if isinstance(direct_models, dict)
            else {}
        )
        self._metrics = data.get("metrics", {})
        self._last_trained = data.get("trained_at")
        self._training_samples = data.get("samples", 0)
        self._thermal_lag_minutes = data.get("thermal_lag", DEFAULT_THERMAL_LAG_MINUTES)
        self._training_notice = None
        return True

    # ------------------------------------------------------------------
    # Dataset builder
    # ------------------------------------------------------------------

    @staticmethod
    def _latest_index_at_or_before(times: np.ndarray, target: float) -> int | None:
        """Find the latest recorded sample available at ``target``.

        Forecast features must be causal: a reading taken after the target time
        was not available when the prediction would have been made.  Selecting
        the nearest row instead leaks future indoor or device measurements into
        training and inflates validation scores.
        """
        index = int(np.searchsorted(times, target, side="right")) - 1
        return index if index >= 0 else None

    async def _build_dataset(self) -> tuple[np.ndarray, np.ndarray, int]:
        """
        Join indoor_temp_reading + device_status + weather on time, shifting
        by thermal lag so we correlate *past* water temp with *current* air temp.

        Limits data to the last 90 days to maximise training signal.
        """
        lag = dt.timedelta(minutes=self._thermal_lag_minutes)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        values = await get_all_settings()
        reference_sensor_id = str(values.get("comfort_reference_sensor_id") or "").strip()
        weather_provider = str(values.get("weather_provider") or "open-meteo")
        try:
            heat_curve = HeatCurveConfig.from_settings(values)
        except ValueError:
            # A malformed manual setting must not turn the controller sentinel
            # into training data.  Use the safe defaults and let Settings show
            # its own validation error to the user.
            heat_curve = HeatCurveConfig()

        async with get_session() as session:
            # Get indoor temp readings from the last 90 days. Stale readings are
            # excluded: they carry a fresh row timestamp but an old sensor value,
            # so pairing them with same-time device/weather data would train the
            # model on a mislabelled target (matches the thermal model filter).
            result = await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= cutoff)
                .where(IndoorTempReading.is_stale == False)  # noqa: E712
                .order_by(IndoorTempReading.timestamp)
            )
            raw_readings = result.scalars().all()
            readings, sensor_strategy, source_sensor_count = self._select_indoor_observations(
                raw_readings, reference_sensor_id
            )

            if not readings:
                self._last_dataset_evidence = {
                    "active_heating_rows": 0,
                    "sensor_strategy": "reference_missing"
                    if reference_sensor_id
                    else "no_readings",
                    "source_sensor_count": 0,
                }
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
                        WeatherRecord.source == weather_provider,
                    )
                )
                .order_by(WeatherRecord.ts)
            )
            weathers = weather_result.scalars().all()

        if not statuses:
            self._last_dataset_evidence = {
                "active_heating_rows": 0,
                "sensor_strategy": sensor_strategy,
                "source_sensor_count": source_sensor_count,
            }
            return np.array([]), np.array([]), 0

        # Build lookup arrays. Status and prior-indoor values are resolved
        # causally (at or before the feature time); weather can be joined to
        # the forecast slot itself because it is an input available to the
        # optimiser when making a future prediction.
        status_times = np.array([(s.ts - earliest).total_seconds() for s in statuses])
        weather_times = (
            np.array([(w.ts - earliest).total_seconds() for w in weathers])
            if weathers
            else np.array([])
        )
        # Indoor-temp lookup: find prev indoor temp at (T - lag) for each sample
        reading_times = np.array([(r.timestamp - earliest).total_seconds() for r in readings])
        reading_temps = np.array([r.temperature for r in readings])

        X_rows = []
        y_rows = []
        active_heating_rows = 0

        for reading in readings:
            target_ts = reading.timestamp - lag
            t_sec = (target_ts - earliest).total_seconds()

            # Latest device status known at the feature timestamp. Using a
            # later status would leak a water-temperature change that had not
            # yet happened when predicting this indoor reading.
            idx = self._latest_index_at_or_before(status_times, t_sec)
            if idx is None:
                continue
            status = statuses[idx]
            # Skip if too far (> 15 min)
            gap = (target_ts - status.ts).total_seconds()
            if gap > 900:
                continue

            is_active_heating = has_confirmed_space_heating(status)
            recent_heat_fraction = self._recent_heat_fraction(statuses, status_times, idx, t_sec)

            zone_water_temp = status.zone1_temp
            outdoor_temp = status.outdoor_temp

            # Weather at the response slot is a forecast input available when
            # the plan is created.  The old one-hour model accidentally used
            # weather from the start of the lag window, weakening longer direct
            # forecasts and hiding weather transitions.
            wind_speed = 3.0
            irradiance = 0.0
            precipitation = 0.0
            humidity = 60.0
            cloud_cover = 0.5
            if len(weather_times) > 0:
                response_t_sec = (reading.timestamp - earliest).total_seconds()
                w_idx = int(np.argmin(np.abs(weather_times - response_t_sec)))
                w = weathers[w_idx]
                if abs(weather_times[w_idx] - response_t_sec) <= 7200:
                    # Weather is the canonical ambient temperature; the raw
                    # Aquarea sensor may be warmed by direct sun.
                    if w.temperature is not None:
                        outdoor_temp = w.temperature
                    wind_speed = w.wind_speed if w.wind_speed is not None else 3.0
                    irradiance = getattr(w, "irradiance", 0.0) or 0.0
                    precipitation = getattr(w, "precipitation", 0.0) or 0.0
                    humidity = getattr(w, "humidity", 60.0)
                    humidity = 60.0 if humidity is None else humidity
                    cloud_cover = getattr(w, "cloud_cover", 0.5)
                    cloud_cover = 0.5 if cloud_cover is None else cloud_cover
            if zone_water_temp is None or outdoor_temp is None:
                continue

            # Previous indoor temperature available at the lag-shifted time.
            # This deliberately uses the latest earlier sample rather than a
            # nearest neighbour, so no future sensor value can leak into X.
            prev_indoor: float | None = None
            prev_idx = self._latest_index_at_or_before(reading_times, t_sec)
            prev_gap = t_sec - reading_times[prev_idx] if prev_idx is not None else float("inf")
            if prev_idx is not None and prev_gap <= 900:
                prev_indoor = float(reading_temps[prev_idx])
            indoor_trend = self._indoor_trend(reading_times, reading_temps, t_sec, prev_idx)

            hour = reading.timestamp.hour
            features = self._make_features(
                zone_water_temp,
                outdoor_temp,
                wind_speed,
                irradiance,
                hour,
                indoor_temp=prev_indoor,
                precipitation=precipitation,
                humidity=humidity,
                cloud_cover=cloud_cover,
                zone_target_temp=effective_zone_target_temperature(
                    status.zone1_target_temp,
                    outdoor_temp,
                    config=heat_curve,
                    fallback_c=zone_water_temp,
                ),
                space_heating_fraction=1.0 if is_active_heating else 0.0,
                recent_heat_fraction=recent_heat_fraction,
                indoor_trend_c_per_hour=indoor_trend,
            )
            X_rows.append(features)
            y_rows.append(reading.temperature)
            if is_active_heating:
                active_heating_rows += 1

        n = len(X_rows)
        self._last_dataset_evidence = {
            "active_heating_rows": active_heating_rows,
            "sensor_strategy": sensor_strategy,
            "source_sensor_count": source_sensor_count,
        }
        if n == 0:
            return np.array([]), np.array([]), 0

        return np.array(X_rows), np.array(y_rows), n

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    @staticmethod
    def _select_indoor_observations(
        readings: list[IndoorTempReading],
        reference_sensor_id: str,
    ) -> tuple[list[_IndoorObservation], str, int]:
        """Use one configured room or a robust five-minute cross-room median."""

        if reference_sensor_id:
            selected = [
                _IndoorObservation(timestamp=row.timestamp, temperature=float(row.temperature))
                for row in readings
                if row.device_id == reference_sensor_id
            ]
            return selected, "reference_sensor", 1 if selected else 0

        buckets: dict[dt.datetime, list[IndoorTempReading]] = {}
        for row in readings:
            bucket = row.timestamp.replace(
                minute=(row.timestamp.minute // 5) * 5,
                second=0,
                microsecond=0,
            )
            buckets.setdefault(bucket, []).append(row)
        observations = [
            _IndoorObservation(
                timestamp=max(row.timestamp for row in rows),
                temperature=float(np.median([row.temperature for row in rows])),
            )
            for _, rows in sorted(buckets.items())
        ]
        return observations, "robust_5m_median", len({row.device_id for row in readings})

    @staticmethod
    def _recent_heat_fraction(
        statuses: list[DeviceStatusRecord],
        status_times: np.ndarray,
        latest_index: int,
        feature_time: float,
    ) -> float:
        start = int(np.searchsorted(status_times, feature_time - 3600.0, side="left"))
        window = statuses[start : latest_index + 1]
        if not window:
            return 0.0
        return round(
            sum(1.0 for row in window if has_confirmed_space_heating(row)) / len(window), 3
        )

    @staticmethod
    def _indoor_trend(
        reading_times: np.ndarray,
        reading_temps: np.ndarray,
        feature_time: float,
        latest_index: int | None,
    ) -> float:
        if latest_index is None:
            return 0.0
        older_index = ComfortModel._latest_index_at_or_before(reading_times, feature_time - 3600.0)
        if older_index is None or older_index == latest_index:
            return 0.0
        elapsed_hours = (reading_times[latest_index] - reading_times[older_index]) / 3600.0
        if elapsed_hours < 0.25 or elapsed_hours > 2.0:
            return 0.0
        trend = (reading_temps[latest_index] - reading_temps[older_index]) / elapsed_hours
        return round(max(-3.0, min(3.0, trend)), 3)

    @staticmethod
    def _make_features(
        zone_water_temp: float,
        outdoor_temp: float,
        wind_speed: float,
        irradiance: float,
        hour: int,
        indoor_temp: float | None = None,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
        zone_target_temp: float | None = None,
        space_heating_fraction: float = 0.0,
        recent_heat_fraction: float = 0.0,
        indoor_trend_c_per_hour: float = 0.0,
    ) -> np.ndarray:
        hour_rad = 2.0 * np.pi * hour / 24.0
        return np.array(
            [
                zone_water_temp,
                zone_water_temp if zone_target_temp is None else zone_target_temp,
                max(0.0, min(1.0, space_heating_fraction)),
                max(0.0, min(1.0, recent_heat_fraction)),
                outdoor_temp,
                wind_speed,
                irradiance,
                max(0.0, precipitation),
                max(0.0, min(100.0, humidity)),
                max(0.0, min(1.0, cloud_cover)),
                np.sin(hour_rad),
                np.cos(hour_rad),
                indoor_temp if indoor_temp is not None else outdoor_temp,
                max(-3.0, min(3.0, indoor_trend_c_per_hour)),
            ]
        )


# Module-level singleton
comfort_model = ComfortModel()

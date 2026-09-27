"""Comfort model — learns (water_temp, outdoor_temp, weather) → indoor air temp.

Also provides the *inverse*: given a target indoor temperature, what water supply
temperature should the heat pump deliver?
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import pickle
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

try:
    import sklearn.ensemble  # noqa: F401  (availability probe)

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from sqlalchemy import and_, select, text

from packages.core.database import engine, get_session
from packages.core.heat_curve import HeatCurveConfig, effective_zone_target_temperature
from packages.core.heating_evidence import classify_space_heating, has_confirmed_space_heating
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
MIN_CONTROL_ACTIVE_INPUT_BUCKETS = 4
MIN_CONTROL_ACTIVE_INPUT_RANGE_C = 3.0
MIN_CONTROL_MARGIN_C = 0.15
MAX_CONTROL_MARGIN_C = 0.45

# Earlier artifacts were trained with nearest-neighbour indoor and status
# samples, which could select values recorded *after* the feature timestamp.
# Keep them separate from the causal dataset definition below.
COMFORT_MODEL_ARTIFACT_PREFIX = "comfort_model_weather_delta_v7_window_heat_"
COMFORT_MODEL_ARTIFACT_GLOB = f"{COMFORT_MODEL_ARTIFACT_PREFIX}*.pkl"
COMFORT_MODEL_FEATURE_SCHEMA = "weather_delta_v7_window_heat_controlled"
COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA = "weather_delta_v7_window_heat_passive_no_clock"
FORECAST_QUALITY_GATE_SCHEMA = "indoor_forecast_v3"
FORECAST_QUALITY_GATE_SCHEMA_V4_DELTA_WINDOW_HEAT = "indoor_forecast_v4_delta_window_heat"
FORECAST_QUALITY_REQUIRED_HORIZONS = (1, 3, 6, 12, 24)
COMFORT_MODEL_FORMAT_VERSION = 7
COMFORT_MODEL_TARGET_KIND = "delta_temperature_c"
COMFORT_MODEL_CONSTRAINT_VERSION = "window_heat_v1"
COMFORT_MODEL_KNOT_RANGE = (5.0, 35.0)
COMFORT_MODEL_PROJECTION_EPSILON = 0.01
_TRAINING_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"comfort_model_training_v1").digest()[:8], "big", signed=True
)

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
_MONOTONIC_CST = [1, 1, 1, 1, 1, -1, 1, 0, 0, -1, 0, 0, -1, 0]
_INDOOR_TEMPERATURE_FEATURE_INDEX = 12
_PASSIVE_MONOTONIC_CST = [1, -1, 1, 0, 0, -1, 0, -1, 0, 0, -1, 0]
CONTROLLED_FEATURE_NAMES = (
    "mean_supply_lift",
    "mean_target_lift",
    "window_heat_fraction",
    "final_hour_heat_fraction",
    "outdoor_temp",
    "wind_speed",
    "irradiance",
    "precipitation",
    "humidity",
    "cloud_cover",
    "hour_sin",
    "hour_cos",
    "issue_indoor_temp",
    "issue_indoor_trend",
)
PASSIVE_FEATURE_NAMES = tuple(
    name for index, name in enumerate(CONTROLLED_FEATURE_NAMES) if index not in (10, 11)
)
WINDOW_COVERAGE_MINIMUM = 0.80
_STATUS_INTERVAL_MAX_SECONDS = 15 * 60

# Fraction of (time-ordered) samples held out at the end for honest validation.
_VALIDATION_FRACTION = 0.2
_MIN_VALIDATION_ROWS = 10


@dataclass(frozen=True)
class _IndoorObservation:
    timestamp: dt.datetime
    temperature: float


@dataclass(frozen=True)
class WindowDataset:
    controlled: dict[int, tuple[np.ndarray, np.ndarray]]
    passive: dict[int, tuple[np.ndarray, np.ndarray]]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ComfortModelCandidate:
    """A complete checkpoint which can be installed as one in-memory operation."""

    model: Any
    direct_models: dict[int, Any]
    passive_direct_models: dict[int, Any]
    metrics: dict[str, Any]
    samples: int
    thermal_lag_minutes: int
    training_notice: str | None


def _finite(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and np.isfinite(value) else default


def _confirmed_absent(status: Any) -> bool:
    evidence = classify_space_heating(
        operation_status=getattr(status, "operation_status", None),
        mode=getattr(status, "mode", None),
        direction=getattr(status, "direction", None),
        pump_duty=getattr(status, "pump_duty", None),
        device_action=getattr(status, "device_action", None),
        defrost_active=getattr(status, "defrost_active", None),
        zone1_operation_status=getattr(status, "zone1_operation_status", None),
        zone2_operation_status=getattr(status, "zone2_operation_status", None),
    )
    return evidence.code in {"device_off", "idle", "domestic_hot_water", "cooling", "defrost"}


def build_window_dataset(
    readings: list[_IndoorObservation],
    statuses: list[Any],
    weathers: list[Any],
    horizons: tuple[int, ...],
) -> WindowDataset:
    """Build DB-free, duration-weighted v7 training rows from plain records."""
    controlled_rows: dict[int, list[np.ndarray]] = {horizon: [] for horizon in horizons}
    controlled_targets: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    passive_rows: dict[int, list[np.ndarray]] = {horizon: [] for horizon in horizons}
    passive_targets: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    ordered_readings = sorted(readings, key=lambda row: row.timestamp)
    ordered_statuses = sorted(statuses, key=lambda row: row.ts)
    ordered_weather = sorted(weathers, key=lambda row: row.ts)
    accepted = rejected_coverage = 0
    for target in ordered_readings:
        for horizon in horizons:
            issue_ts = target.timestamp - dt.timedelta(minutes=horizon)
            issue_candidates = [row for row in ordered_readings if row.timestamp <= issue_ts]
            if (
                not issue_candidates
                or (issue_ts - issue_candidates[-1].timestamp).total_seconds() > 900
            ):
                continue
            issue = issue_candidates[-1]
            intervals: list[tuple[Any, float]] = []
            covered = active = final_active = 0.0
            for index, status in enumerate(ordered_statuses):
                next_ts = (
                    ordered_statuses[index + 1].ts
                    if index + 1 < len(ordered_statuses)
                    else target.timestamp
                )
                start, end = max(status.ts, issue_ts), min(next_ts, target.timestamp)
                seconds = (end - start).total_seconds()
                if (
                    seconds <= 0
                    or (next_ts - status.ts).total_seconds() > _STATUS_INTERVAL_MAX_SECONDS
                ):
                    continue
                intervals.append((status, seconds))
                covered += seconds
                if has_confirmed_space_heating(status):
                    active += seconds
                    if end > target.timestamp - dt.timedelta(hours=1):
                        final_active += (
                            end - max(start, target.timestamp - dt.timedelta(hours=1))
                        ).total_seconds()
            duration = (target.timestamp - issue_ts).total_seconds()
            if duration <= 0 or covered / duration < WINDOW_COVERAGE_MINIMUM:
                rejected_coverage += 1
                continue
            weather = min(
                ordered_weather,
                key=lambda row: abs((row.ts - target.timestamp).total_seconds()),
                default=None,
            )
            if weather is None or abs((weather.ts - target.timestamp).total_seconds()) > 7200:
                continue
            supply_sum = target_sum = 0.0
            passive_window = (
                covered >= duration
                and bool(intervals)
                and all(_confirmed_absent(status) for status, _ in intervals)
            )
            for status, seconds in intervals:
                if has_confirmed_space_heating(status):
                    supply_sum += (
                        max(
                            _finite(getattr(status, "zone1_temp", None), 0.0) - issue.temperature,
                            0.0,
                        )
                        * seconds
                    )
                    target_sum += (
                        max(
                            _finite(getattr(status, "zone1_target_temp", None), 0.0)
                            - issue.temperature,
                            0.0,
                        )
                        * seconds
                    )
            older = [
                row
                for row in ordered_readings
                if row.timestamp <= issue.timestamp - dt.timedelta(hours=1)
            ]
            trend = (
                (issue.temperature - older[-1].temperature)
                / max((issue.timestamp - older[-1].timestamp).total_seconds() / 3600.0, 1.0)
                if older
                else 0.0
            )
            row = ComfortModel._controlled_window_features(
                mean_supply_lift=supply_sum / duration,
                mean_target_lift=target_sum / duration,
                window_heat_fraction=active / duration,
                final_hour_heat_fraction=final_active / min(duration, 3600.0),
                outdoor_temp=_finite(
                    getattr(weather, "temperature", None),
                    _finite(getattr(intervals[-1][0], "outdoor_temp", None), 0.0),
                ),
                wind_speed=_finite(getattr(weather, "wind_speed", None), 3.0),
                irradiance=_finite(getattr(weather, "irradiance", None), 0.0),
                precipitation=_finite(getattr(weather, "precipitation", None), 0.0),
                humidity=_finite(getattr(weather, "humidity", None), 60.0),
                cloud_cover=_finite(getattr(weather, "cloud_cover", None), 0.5),
                hour=target.timestamp.hour,
                issue_indoor_temp=issue.temperature,
                issue_indoor_trend=trend,
            )
            delta = float(target.temperature - issue.temperature)
            controlled_rows[horizon].append(row)
            controlled_targets[horizon].append(delta)
            if passive_window:
                passive_rows[horizon].append(ComfortModel._passive_window_features(row))
                passive_targets[horizon].append(delta)
            accepted += 1
    return WindowDataset(
        {h: (np.asarray(controlled_rows[h]), np.asarray(controlled_targets[h])) for h in horizons},
        {h: (np.asarray(passive_rows[h]), np.asarray(passive_targets[h])) for h in horizons},
        {"window_rows": accepted, "rejected_incomplete_windows": rejected_coverage},
    )


def build_candidate_bundle(dataset: WindowDataset) -> dict[str, Any]:
    """Fit B1 candidates without reading a DB or mutating a ComfortModel."""
    from sklearn.metrics import mean_absolute_error, r2_score

    controlled_models: dict[int, Any] = {}
    passive_models: dict[int, Any] = {}
    metrics: dict[str, Any] = {"direct_horizons": {}, "passive_horizons": {}}
    for family, rows, constraints, models, metric_key in (
        ("controlled", dataset.controlled, _MONOTONIC_CST, controlled_models, "direct_horizons"),
        ("passive", dataset.passive, _PASSIVE_MONOTONIC_CST, passive_models, "passive_horizons"),
    ):
        for horizon, (features, targets) in rows.items():
            if len(targets) < MIN_TRAINING_ROWS:
                metrics[metric_key][str(horizon)] = {
                    "status": "insufficient_data",
                    "samples": len(targets),
                }
                continue
            split = int(len(targets) * (1 - _VALIDATION_FRACTION))
            evaluator = make_monotonic_regressor(constraints)
            evaluator.fit(features[:split], targets[:split])
            predicted = evaluator.predict(features[split:])
            model = make_monotonic_regressor(constraints)
            model.fit(features, targets)
            models[horizon] = model
            metrics[metric_key][str(horizon)] = {
                "status": "trained",
                "samples": len(targets),
                "mae": round(float(mean_absolute_error(targets[split:], predicted)), 3),
                "r2": round(float(r2_score(targets[split:], predicted)), 3),
            }
    metrics.update(dataset.evidence)
    metrics["feature_names"] = list(CONTROLLED_FEATURE_NAMES)
    metrics["passive_feature_names"] = list(PASSIVE_FEATURE_NAMES)
    metrics["constraint_version"] = COMFORT_MODEL_CONSTRAINT_VERSION
    notice = None
    if not passive_models:
        notice = (
            "passive_model_unavailable:no_zero_heating_windows;passive_forecast=physics_fallback"
        )
    return {
        "controlled_models": controlled_models,
        "passive_models": passive_models,
        "metrics": metrics,
        "notice": notice,
    }


def build_candidate(
    dataset: WindowDataset, thermal_lag_minutes: int | None
) -> ComfortModelCandidate | None:
    """Build a v7 checkpoint from materialized inputs without touching model state."""
    bundle = build_candidate_bundle(dataset)
    controlled_models = bundle["controlled_models"]
    primary_horizon = DIRECT_FORECAST_HORIZONS_MINUTES[0]
    primary_model = controlled_models.get(primary_horizon)
    if primary_model is None:
        return None
    primary_metrics = bundle["metrics"]["direct_horizons"][str(primary_horizon)]
    metrics = {
        **bundle["metrics"],
        "mae": primary_metrics.get("mae"),
        "r2": primary_metrics.get("r2"),
        "validated": True,
        "delta_target": True,
        "thermal_lag_min": thermal_lag_minutes or primary_horizon,
        "training_horizon_minutes": primary_horizon,
        "forecast_quality_feature_schema": COMFORT_MODEL_FEATURE_SCHEMA,
        "forecast_quality_gate_schema": FORECAST_QUALITY_GATE_SCHEMA_V4_DELTA_WINDOW_HEAT,
    }
    return ComfortModelCandidate(
        model=primary_model,
        direct_models=controlled_models,
        passive_direct_models=bundle["passive_models"],
        metrics=metrics,
        samples=int(primary_metrics["samples"]),
        thermal_lag_minutes=thermal_lag_minutes or primary_horizon,
        training_notice=bundle["notice"],
    )


class TrainingLockLease(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def invalidate(self) -> None: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...


class TrainingLock(Protocol):
    async def acquire(self) -> TrainingLockLease | None: ...


class _PostgresTrainingLockLease:
    def __init__(self, connection: Any, transaction: Any) -> None:
        self.connection = connection
        self.transaction = transaction

    async def commit(self) -> None:
        try:
            await self.transaction.commit()
        except BaseException:
            await self.connection.invalidate()
            raise

    async def rollback(self) -> None:
        try:
            await self.transaction.rollback()
        except BaseException:
            await self.connection.invalidate()
            raise

    async def invalidate(self) -> None:
        await self.connection.invalidate()

    async def health_check(self) -> bool:
        await self.connection.execute(text("SELECT 1"))
        return True

    async def close(self) -> None:
        await self.connection.close()


class PostgresTrainingLock:
    """Non-blocking transaction advisory lock for cross-process training."""

    def __init__(self) -> None:
        self.reason = "training_lock_unavailable"

    async def acquire(self) -> TrainingLockLease | None:
        if engine.dialect.name != "postgresql":
            return None
        connection = await engine.connect()
        transaction = await connection.begin()
        try:
            result = await connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _TRAINING_LOCK_KEY}
            )
            acquired = result.scalar_one_or_none()
            if not isinstance(acquired, bool) or not acquired:
                self.reason = (
                    "training_in_progress" if acquired is False else "training_lock_unavailable"
                )
                await transaction.rollback()
                await connection.close()
                return None
            self.reason = "acquired"
            return _PostgresTrainingLockLease(connection, transaction)
        except BaseException:
            try:
                await transaction.rollback()
            except BaseException:
                await connection.invalidate()
            await connection.close()
            raise


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

    def __init__(self, lock_strategy: TrainingLock | None = None) -> None:
        self._model: Any | None = None
        self._direct_models: dict[int, Any] = {}
        self._passive_direct_models: dict[int, Any] = {}
        self._metrics: dict[str, Any] = {}
        self._last_trained: dt.datetime | None = None
        self._training_samples: int = 0
        self._thermal_lag_minutes: int = DEFAULT_THERMAL_LAG_MINUTES
        self._last_dataset_evidence: dict[str, Any] = {"active_heating_rows": 0}
        self._training_notice: str | None = None
        self._artifact_fingerprint: tuple[Path, int, int] | None = None
        self._artifact_refresh_reason: str | None = "artifact_missing"
        self._artifact_lock = threading.Lock()
        self._training_lock = asyncio.Lock()
        self._lock_strategy = lock_strategy or PostgresTrainingLock()

    def reset(self) -> None:
        """Discard the trained model and learned metadata."""
        self._model = None
        self._direct_models = {}
        self._passive_direct_models = {}
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

        if not self.is_trained or not self._passive_direct_models:
            return {"ready": False, "reason": "passive_model_unavailable"}
        horizon = int(forecast_horizon_minutes)
        if horizon not in self._passive_direct_models:
            return {
                "ready": False,
                "reason": "direct_forecast_horizon_unavailable",
                "horizon_minutes": horizon,
            }
        passive_metrics = self._metrics.get("passive_horizons", {})
        metrics = passive_metrics.get(str(horizon), {}) if isinstance(passive_metrics, dict) else {}
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
        max_change_c_per_hour: float = 0.5,
    ) -> tuple[float | None, dict[str, Any]]:
        """Direct weather-aware indoor forecast with no space heating input."""

        readiness = self.passive_forecast_readiness(forecast_horizon_minutes)
        if not readiness["ready"]:
            return None, readiness
        features = self._passive_window_features(
            self._make_features(
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
            )
        )
        horizon = int(readiness["horizon_minutes"])
        model = self._passive_direct_models[horizon]
        delta = self._project_prediction(model, features, indoor_temp, indoor_feature_index=10)
        if delta is None:
            return None, {"ready": False, "reason": "projection_failed"}
        elapsed_hours = horizon / 60.0
        delta = self._clamp_passive_change(delta, elapsed_hours, max_change_c_per_hour)
        predicted = indoor_temp + delta
        return predicted, readiness

    @property
    def metrics(self) -> dict[str, Any]:
        return dict(self._metrics)

    def record_forecast_quality_gate(
        self,
        gate: dict[str, object],
        *,
        schema: str,
        required_horizons: tuple[int, ...],
        passes_required: int,
        failures_required: int,
        evaluation_id: str | None = None,
        evaluation_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Persist the fail-closed planning gate beside validated model metrics."""
        from packages.ml.forecast_quality import apply_control_gate_hysteresis

        if (
            evaluation_id is not None
            and self._metrics.get("forecast_quality_gate_evaluation_id") == evaluation_id
            and isinstance(self._metrics.get("forecast_quality_gate"), dict)
        ):
            return dict(self._metrics["forecast_quality_gate"])
        state = apply_control_gate_hysteresis(
            gate,
            self._metrics,
            schema=schema,
            required_horizons=required_horizons,
            passes_required=passes_required,
            failures_required=failures_required,
            evaluation_context=evaluation_context,
        )
        self._metrics["forecast_quality_gate"] = state
        if evaluation_id is not None:
            self._metrics["forecast_quality_gate_evaluation_id"] = evaluation_id
        return state

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
        active_input_buckets = self._metrics.get("active_input_buckets")
        active_input_range_c = self._metrics.get("active_input_range_c")
        if (
            not isinstance(active_input_buckets, int)
            or active_input_buckets < MIN_CONTROL_ACTIVE_INPUT_BUCKETS
            or not isinstance(active_input_range_c, (float, int))
            or active_input_range_c < MIN_CONTROL_ACTIVE_INPUT_RANGE_C
        ):
            return {
                "ready": False,
                "reason": "insufficient_heat_input_variance",
                "active_input_buckets": active_input_buckets or 0,
                "minimum_active_input_buckets": MIN_CONTROL_ACTIVE_INPUT_BUCKETS,
                "active_input_range_c": active_input_range_c or 0.0,
                "minimum_active_input_range_c": MIN_CONTROL_ACTIVE_INPUT_RANGE_C,
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
        """Train once with process-local and PostgreSQL single-flight protection."""
        if self._training_lock.locked():
            return {"status": "training_in_progress"}
        async with self._training_lock:
            started = time.monotonic()
            lease: TrainingLockLease | None = None
            temp_path: Path | None = None
            published = False
            finalized = False
            try:
                lease = await self._lock_strategy.acquire()
            except BaseException:
                self._training_notice = "training_lock_unavailable"
                logger.warning("comfort_model_training_skipped", reason="training_lock_unavailable")
                return {"status": "training_skipped", "reason": "training_lock_unavailable"}
            if lease is None:
                reason = getattr(self._lock_strategy, "reason", "training_lock_unavailable")
                if reason == "training_in_progress":
                    return {"status": "training_in_progress"}
                self._training_notice = "training_lock_unavailable"
                logger.warning("comfort_model_training_skipped", reason="training_lock_unavailable")
                return {"status": "training_skipped", "reason": "training_lock_unavailable"}
            try:
                dataset = await self._materialize_window_dataset()
                candidate = await asyncio.to_thread(build_candidate, dataset, thermal_lag_minutes)
                if candidate is None:
                    if thermal_lag_minutes is not None:
                        self._thermal_lag_minutes = thermal_lag_minutes
                    return {
                        "status": "insufficient_data",
                        "rows": len(
                            dataset.controlled.get(DIRECT_FORECAST_HORIZONS_MINUTES[0], ((), ()))[1]
                        ),
                        "required": MIN_TRAINING_ROWS,
                    }
                prior_mae = read_mae_baseline("comfort")
                candidate_mae = candidate.metrics.get("mae")
                if (
                    self.is_trained
                    and isinstance(prior_mae, (float, int))
                    and isinstance(candidate_mae, (float, int))
                    and candidate_mae > prior_mae
                ):
                    return {
                        "status": "regressed",
                        "samples": candidate.samples,
                        "mae": candidate_mae,
                        "prior_deployed_mae": round(float(prior_mae), 3),
                    }
                artifact = self._candidate_artifact(candidate)
                artifact_path = self._new_artifact_path()
                serialization = asyncio.create_task(
                    asyncio.to_thread(self._write_candidate_temp, artifact, artifact_path)
                )
                try:
                    temp_path = await asyncio.shield(serialization)
                except asyncio.CancelledError:
                    # The worker cannot publish; await its only side effect so it can be removed.
                    temp_path = await asyncio.shield(serialization)
                    raise
                if not await lease.health_check():
                    self._training_notice = "training_lock_unavailable"
                    return {"status": "training_skipped", "reason": "training_lock_unavailable"}
                self._publish_candidate_temp(temp_path, artifact_path)
                published = True
                self._install_candidate(candidate, artifact_path)
                result = {
                    "status": "trained",
                    "samples": candidate.samples,
                    **self._metrics,
                    "training_notice": self._training_notice,
                }
                try:
                    await lease.commit()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    await lease.invalidate()
                    finalized = True
                    raise
                except Exception:
                    # The candidate is already atomically installed. Keep it, but expose recovery.
                    await lease.invalidate()
                    self._training_notice = "training_lock_finalize_recovered"
                    result = {**result, "training_notice": self._training_notice}
                finalized = True
                return result
            except BaseException as training_error:
                if finalized:
                    raise
                try:
                    await lease.rollback()
                except BaseException as rollback_error:
                    finalized = True
                    await lease.invalidate()
                    raise rollback_error from training_error
                finalized = True
                raise
            finally:
                if not finalized:
                    try:
                        await lease.rollback()
                    except BaseException:
                        await lease.invalidate()
                if temp_path is not None and not published:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("comfort_model_temp_cleanup_failed")
                logger.info(
                    "comfort_model_training_lock_released",
                    lock_held_seconds=round(time.monotonic() - started, 3),
                    phase="published" if published else "not_published",
                )
                await lease.close()

    async def _materialize_window_dataset(self) -> WindowDataset:
        """Read DB state once and return only plain inputs for off-loop fitting."""
        # Legacy unit tests override the old loader with already-materialized arrays.
        if "_build_dataset" in self.__dict__:
            features, targets, _ = await self._build_dataset()
            empty = (np.array([]), np.array([]))
            return WindowDataset(
                {
                    horizon: (features, targets)
                    if horizon == DIRECT_FORECAST_HORIZONS_MINUTES[0]
                    else empty
                    for horizon in DIRECT_FORECAST_HORIZONS_MINUTES
                },
                {horizon: empty for horizon in DIRECT_FORECAST_HORIZONS_MINUTES},
                {"active_heating_rows": 0},
            )
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        values = await get_all_settings()
        reference_sensor_id = str(values.get("comfort_reference_sensor_id") or "").strip()
        weather_provider = str(values.get("weather_provider") or "open-meteo")
        async with get_session() as session:
            reading_result = await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= cutoff)
                .where(IndoorTempReading.is_stale.is_(False))
                .order_by(IndoorTempReading.timestamp)  # noqa: E712
            )
            readings, strategy, sensor_count = self._select_indoor_observations(
                reading_result.scalars().all(), reference_sensor_id
            )
            if not readings:
                empty = {
                    horizon: (np.array([]), np.array([]))
                    for horizon in DIRECT_FORECAST_HORIZONS_MINUTES
                }
                return WindowDataset(
                    empty,
                    dict(empty),
                    {
                        "active_heating_rows": 0,
                        "sensor_strategy": strategy,
                        "source_sensor_count": sensor_count,
                    },
                )
            status_result = await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts >= readings[0].timestamp - dt.timedelta(hours=12))
                .where(DeviceStatusRecord.ts <= readings[-1].timestamp)
                .order_by(DeviceStatusRecord.ts)
            )
            weather_result = await session.execute(
                select(WeatherRecord)
                .where(WeatherRecord.ts >= readings[0].timestamp)
                .where(WeatherRecord.ts <= readings[-1].timestamp)
                .where(WeatherRecord.source == weather_provider)
                .order_by(WeatherRecord.ts)
            )
        dataset = build_window_dataset(
            readings,
            status_result.scalars().all(),
            weather_result.scalars().all(),
            DIRECT_FORECAST_HORIZONS_MINUTES,
        )
        return WindowDataset(
            dataset.controlled,
            dataset.passive,
            {**dataset.evidence, "sensor_strategy": strategy, "source_sensor_count": sensor_count},
        )

    @staticmethod
    def _write_candidate_temp(artifact: dict[str, Any], artifact_path: Path) -> Path:
        from packages.ml.safe_persistence import safe_write_temp

        return safe_write_temp(artifact, artifact_path)

    @staticmethod
    def _publish_candidate_temp(temp_path: Path, artifact_path: Path) -> None:
        from packages.ml.safe_persistence import safe_publish_temp

        safe_publish_temp(temp_path, artifact_path)

    @staticmethod
    def _new_artifact_path() -> Path:
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return MODEL_DIR / f"{COMFORT_MODEL_ARTIFACT_PREFIX}{timestamp}.pkl"

    def _candidate_artifact(self, candidate: ComfortModelCandidate) -> dict[str, Any]:
        return {
            "model": candidate.model,
            "direct_models": candidate.direct_models,
            "passive_direct_models": candidate.passive_direct_models,
            "metrics": candidate.metrics,
            "trained_at": dt.datetime.now(dt.timezone.utc),
            "samples": candidate.samples,
            "thermal_lag": candidate.thermal_lag_minutes,
            "feature_schema": COMFORT_MODEL_FEATURE_SCHEMA,
            "format_version": COMFORT_MODEL_FORMAT_VERSION,
            "target_kind": COMFORT_MODEL_TARGET_KIND,
            "feature_names": list(CONTROLLED_FEATURE_NAMES),
            "passive_feature_names": list(PASSIVE_FEATURE_NAMES),
            "passive_feature_schema": COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA,
            "controlled_constraints": _MONOTONIC_CST,
            "passive_constraints": _PASSIVE_MONOTONIC_CST,
            "constraint_version": COMFORT_MODEL_CONSTRAINT_VERSION,
            "direct_horizons_minutes": DIRECT_FORECAST_HORIZONS_MINUTES,
            "knot_range": COMFORT_MODEL_KNOT_RANGE,
            "projection_epsilon": COMFORT_MODEL_PROJECTION_EPSILON,
        }

    def _install_candidate(self, candidate: ComfortModelCandidate, artifact_path: Path) -> None:
        metrics = dict(candidate.metrics)
        prior_gate = self._metrics.get("forecast_quality_gate")
        if (
            self._metrics.get("forecast_quality_feature_schema") == COMFORT_MODEL_FEATURE_SCHEMA
            and self._metrics.get("forecast_quality_gate_schema")
            == FORECAST_QUALITY_GATE_SCHEMA_V4_DELTA_WINDOW_HEAT
            and isinstance(prior_gate, dict)
            and prior_gate.get("schema") == FORECAST_QUALITY_GATE_SCHEMA_V4_DELTA_WINDOW_HEAT
            and tuple(prior_gate.get("required_horizons", ())) == FORECAST_QUALITY_REQUIRED_HORIZONS
        ):
            metrics["forecast_quality_gate"] = prior_gate
            evaluation_id = self._metrics.get("forecast_quality_gate_evaluation_id")
            if isinstance(evaluation_id, str):
                metrics["forecast_quality_gate_evaluation_id"] = evaluation_id
        stat = artifact_path.stat()
        with self._artifact_lock:
            self._model = candidate.model
            self._direct_models = candidate.direct_models
            self._passive_direct_models = candidate.passive_direct_models
            self._metrics = metrics
            self._thermal_lag_minutes = candidate.thermal_lag_minutes
            self._last_dataset_evidence = dict(candidate.metrics)
            self._last_trained = dt.datetime.now(dt.timezone.utc)
            self._training_samples = candidate.samples
            self._training_notice = candidate.training_notice
            self._artifact_fingerprint = (artifact_path, stat.st_mtime_ns, stat.st_size)
            self._artifact_refresh_reason = None

    async def _train_unlocked(self, thermal_lag_minutes: int | None = None) -> dict[str, Any]:
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

        # v7 models are trained directly at the planner's supported anchors.
        self._thermal_lag_minutes = DIRECT_FORECAST_HORIZONS_MINUTES[0]
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

        bundle = getattr(self, "_window_candidate_bundle", None)
        if bundle is not None:
            self._window_candidate_bundle = None
            controlled_models = bundle["controlled_models"]
            if not controlled_models:
                return {
                    "status": "insufficient_data",
                    "rows": n_rows,
                    "required": MIN_TRAINING_ROWS,
                }
            self._model = controlled_models[DIRECT_FORECAST_HORIZONS_MINUTES[0]]
            self._direct_models = controlled_models
            self._passive_direct_models = bundle["passive_models"]
            self._metrics = {
                **bundle["metrics"],
                "mae": bundle["metrics"]["direct_horizons"]["60"].get("mae"),
                "r2": bundle["metrics"]["direct_horizons"]["60"].get("r2"),
                "validated": True,
                "delta_target": True,
                "forecast_quality_feature_schema": COMFORT_MODEL_FEATURE_SCHEMA,
                "forecast_quality_gate_schema": FORECAST_QUALITY_GATE_SCHEMA_V4_DELTA_WINDOW_HEAT,
            }
            self._training_notice = bundle["notice"]
            self._last_trained = dt.datetime.now(dt.timezone.utc)
            self._training_samples = n_rows
            if persist:
                self._save()
            return {
                "status": "trained",
                "samples": n_rows,
                **self._metrics,
                "training_notice": self._training_notice,
            }

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
        previous_metrics = self._metrics
        self._metrics = {
            "mae": round(float(mae), 3),
            "r2": round(float(r2), 3),
            "thermal_lag_min": self._thermal_lag_minutes,
            "training_horizon_minutes": self._thermal_lag_minutes,
            "validated": validated,
            "baseline_mae": round(float(baseline_mae), 3),
            "prior_deployed_mae": round(float(prior_mae), 3) if prior_mae is not None else None,
            "active_heating_rows": self._last_dataset_evidence.get("active_heating_rows", 0),
            "active_input_buckets": self._last_dataset_evidence.get("active_input_buckets", 0),
            "active_input_range_c": self._last_dataset_evidence.get("active_input_range_c", 0.0),
            "zone_water_temp_source": "Panasonic zoneStatus.temperatureNow",
            "flat_active_heating_rows_excluded": self._last_dataset_evidence.get(
                "flat_active_heating_rows_excluded", 0
            ),
            "sensor_strategy": self._last_dataset_evidence.get("sensor_strategy", "unknown"),
            "source_sensor_count": self._last_dataset_evidence.get("source_sensor_count", 0),
            "forecast_quality_feature_schema": COMFORT_MODEL_FEATURE_SCHEMA,
            "forecast_quality_gate_schema": FORECAST_QUALITY_GATE_SCHEMA,
        }
        previous_gate = previous_metrics.get("forecast_quality_gate")
        if (
            isinstance(previous_gate, dict)
            and previous_gate.get("schema") == FORECAST_QUALITY_GATE_SCHEMA
            and tuple(previous_gate.get("required_horizons", ()))
            == FORECAST_QUALITY_REQUIRED_HORIZONS
            and previous_metrics.get("forecast_quality_feature_schema")
            in (None, COMFORT_MODEL_FEATURE_SCHEMA)
        ):
            self._metrics["forecast_quality_gate"] = previous_gate
            evaluation_id = previous_metrics.get("forecast_quality_gate_evaluation_id")
            if isinstance(evaluation_id, str):
                self._metrics["forecast_quality_gate_evaluation_id"] = evaluation_id
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
        if forecast_horizon_minutes is not None:
            horizon = int(forecast_horizon_minutes)
            if horizon not in self._direct_models:
                return None
            model = self._direct_models[horizon]
        raw = float(model.predict(features.reshape(1, -1))[0])
        if not self._metrics.get("delta_target"):
            return raw
        issue = float(features[_INDOOR_TEMPERATURE_FEATURE_INDEX])
        projected = self._project_prediction(model, features, issue)
        return None if projected is None else issue + projected

    @classmethod
    def _project_prediction(
        cls,
        model: Any,
        features: np.ndarray,
        issue_temp: float,
        *,
        indoor_feature_index: int = _INDOOR_TEMPERATURE_FEATURE_INDEX,
    ) -> float | None:
        """Evaluate and project a delta curve once, then interpolate at issue temperature."""
        knots = np.arange(COMFORT_MODEL_KNOT_RANGE[0], COMFORT_MODEL_KNOT_RANGE[1] + 1.0)
        rows = np.tile(np.asarray(features, dtype=float), (len(knots), 1))
        rows[:, indoor_feature_index] = knots
        try:
            curve = cls.project_delta_curve(np.asarray(model.predict(rows), dtype=float))
        except (TypeError, ValueError):
            return None
        if curve is None:
            return None
        if issue_temp < knots[0]:
            return float(curve[0] + (issue_temp - knots[0]) * (curve[1] - curve[0]))
        if issue_temp > knots[-1]:
            return float(curve[-1] + (issue_temp - knots[-1]) * (curve[-1] - curve[-2]))
        return float(np.interp(issue_temp, knots, curve))

    @staticmethod
    def _clamp_passive_change(
        delta_c: float, elapsed_hours: float, limit_c_per_hour: float
    ) -> float:
        limit = max(0.0, float(limit_c_per_hour)) * max(0.0, float(elapsed_hours))
        return max(-limit, min(limit, float(delta_c)))

    @staticmethod
    def _pav(values: np.ndarray, *, increasing: bool) -> np.ndarray:
        """Euclidean isotonic projection using deterministic pool-adjacent violators."""
        work = np.asarray(values, dtype=float)
        if not increasing:
            work = -work
        blocks: list[list[float]] = []
        for value in work:
            blocks.append([float(value), 1.0])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                total, count = blocks.pop()
                blocks[-1][0] += total
                blocks[-1][1] += count
        projected = np.concatenate([np.full(int(count), total / count) for total, count in blocks])
        return projected if increasing else -projected

    @classmethod
    def project_delta_curve(cls, raw_deltas: np.ndarray) -> np.ndarray | None:
        """Project direct delta estimates onto the bounded monotone constraint set."""
        raw = np.asarray(raw_deltas, dtype=float)
        knots = np.arange(COMFORT_MODEL_KNOT_RANGE[0], COMFORT_MODEL_KNOT_RANGE[1] + 1.0)
        if raw.shape != knots.shape or not np.all(np.isfinite(raw)):
            return None
        residual_a = np.zeros_like(raw)
        residual_b = np.zeros_like(raw)
        current = raw.copy()
        slope = 1.0 - COMFORT_MODEL_PROJECTION_EPSILON
        for _ in range(1000):
            prior = current.copy()
            first = cls._pav(current + residual_a, increasing=False)
            residual_a = current + residual_a - first
            shifted = first + residual_b + slope * knots
            second = cls._pav(shifted, increasing=True) - slope * knots
            residual_b = first + residual_b - second
            current = second
            decreasing_violation = max(0.0, float(np.max(np.diff(current))))
            increasing_violation = max(0.0, float(-np.min(np.diff(current + slope * knots))))
            if (
                np.max(np.abs(current - prior)) <= 1e-10
                and decreasing_violation <= 1e-10
                and increasing_violation <= 1e-10
            ):
                return current if np.all(np.isfinite(current)) else None
        return None

    @staticmethod
    def _controlled_window_features(
        *,
        mean_supply_lift: float,
        mean_target_lift: float,
        window_heat_fraction: float,
        final_hour_heat_fraction: float,
        outdoor_temp: float,
        wind_speed: float,
        irradiance: float,
        precipitation: float,
        humidity: float,
        cloud_cover: float,
        hour: int,
        issue_indoor_temp: float,
        issue_indoor_trend: float,
    ) -> np.ndarray:
        return ComfortModel._make_features(
            mean_supply_lift,
            outdoor_temp,
            wind_speed,
            irradiance,
            hour,
            indoor_temp=issue_indoor_temp,
            precipitation=precipitation,
            humidity=humidity,
            cloud_cover=cloud_cover,
            zone_target_temp=mean_target_lift,
            space_heating_fraction=window_heat_fraction,
            recent_heat_fraction=final_hour_heat_fraction,
            indoor_trend_c_per_hour=issue_indoor_trend,
        )

    @staticmethod
    def _passive_window_features(controlled_features: np.ndarray) -> np.ndarray:
        return np.delete(np.asarray(controlled_features, dtype=float), [10, 11])

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

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = MODEL_DIR / f"{COMFORT_MODEL_ARTIFACT_PREFIX}{ts}.pkl"
        safe_dump(
            {
                "model": self._model,
                "direct_models": self._direct_models,
                "passive_direct_models": self._passive_direct_models,
                "metrics": self._metrics,
                "trained_at": self._last_trained,
                "samples": self._training_samples,
                "thermal_lag": self._thermal_lag_minutes,
                "feature_schema": COMFORT_MODEL_FEATURE_SCHEMA,
                "format_version": COMFORT_MODEL_FORMAT_VERSION,
                "target_kind": COMFORT_MODEL_TARGET_KIND,
                "feature_names": [
                    "mean_supply_lift",
                    "mean_target_lift",
                    "window_heat_fraction",
                    "final_hour_heat_fraction",
                    "outdoor_temp",
                    "wind_speed",
                    "irradiance",
                    "precipitation",
                    "humidity",
                    "cloud_cover",
                    "hour_sin",
                    "hour_cos",
                    "issue_indoor_temp",
                    "issue_indoor_trend",
                ],
                "passive_feature_names": list(PASSIVE_FEATURE_NAMES),
                "passive_feature_schema": COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA,
                "controlled_constraints": _MONOTONIC_CST,
                "passive_constraints": _PASSIVE_MONOTONIC_CST,
                "constraint_version": COMFORT_MODEL_CONSTRAINT_VERSION,
                "direct_horizons_minutes": DIRECT_FORECAST_HORIZONS_MINUTES,
                "knot_range": COMFORT_MODEL_KNOT_RANGE,
                "projection_epsilon": COMFORT_MODEL_PROJECTION_EPSILON,
            },
            path,
        )
        prune_old_models(COMFORT_MODEL_ARTIFACT_GLOB, model_dir=MODEL_DIR)
        stat = path.stat()
        self._artifact_fingerprint = (path, stat.st_mtime_ns, stat.st_size)
        self._artifact_refresh_reason = None
        logger.info("comfort_model_saved", path=str(path))

    def load_latest(self) -> bool:
        """Load the most recent saved model.  Returns True if loaded."""
        return self.refresh_if_changed(force=True)

    @property
    def artifact_refresh_reason(self) -> str | None:
        """The latest shared-artifact refresh outcome, safe for status endpoints."""

        return self._artifact_refresh_reason

    def refresh_if_changed(self, *, force: bool = False) -> bool:
        """Install a newer valid shared artifact without replacing a known-good model."""
        from packages.ml.safe_persistence import safe_load

        try:
            models = sorted(MODEL_DIR.glob(COMFORT_MODEL_ARTIFACT_GLOB), reverse=True)
        except OSError:
            models = []
        if not models:
            try:
                legacy_models = list(MODEL_DIR.glob("comfort_model_*.pkl"))
            except OSError:
                legacy_models = []
            with self._artifact_lock:
                self._artifact_refresh_reason = "artifact_missing"
                if legacy_models:
                    self._training_notice = (
                        "A previous comfort-model artifact uses an older feature schema and was "
                        "retired safely. Retrain to use confirmed heating evidence."
                    )
                return False

        for path in models:
            try:
                stat = path.stat()
                fingerprint = (path, stat.st_mtime_ns, stat.st_size)
            except (FileNotFoundError, OSError):
                continue
            with self._artifact_lock:
                if not force and fingerprint == self._artifact_fingerprint:
                    return False
            try:
                data = safe_load(path)
                candidate = data["model"]
                if (
                    data.get("format_version") != COMFORT_MODEL_FORMAT_VERSION
                    or data.get("target_kind") != COMFORT_MODEL_TARGET_KIND
                    or data.get("feature_schema") != COMFORT_MODEL_FEATURE_SCHEMA
                    or data.get("passive_feature_schema") != COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA
                    or tuple(data.get("feature_names", ())) != CONTROLLED_FEATURE_NAMES
                    or tuple(data.get("passive_feature_names", ())) != PASSIVE_FEATURE_NAMES
                    or data.get("controlled_constraints") != _MONOTONIC_CST
                    or data.get("passive_constraints") != _PASSIVE_MONOTONIC_CST
                    or data.get("constraint_version") != COMFORT_MODEL_CONSTRAINT_VERSION
                    or tuple(data.get("direct_horizons_minutes", ()))
                    != DIRECT_FORECAST_HORIZONS_MINUTES
                    or tuple(data.get("knot_range", ())) != COMFORT_MODEL_KNOT_RANGE
                    or data.get("projection_epsilon") != COMFORT_MODEL_PROJECTION_EPSILON
                ):
                    raise ValueError("incompatible comfort-model artifact")
                if getattr(candidate, "n_features_in_", None) != len(_MONOTONIC_CST):
                    continue
            except (
                AttributeError,
                EOFError,
                FileNotFoundError,
                ImportError,
                IndexError,
                KeyError,
                OSError,
                pickle.UnpicklingError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "comfort_model_integrity_failed",
                    artifact=path.name,
                    exception_type=type(exc).__name__,
                )
                with self._artifact_lock:
                    self._artifact_refresh_reason = "artifact_integrity_failed"
                continue

            with self._artifact_lock:
                try:
                    current_stat = path.stat()
                except (FileNotFoundError, OSError):
                    continue
                if (path, current_stat.st_mtime_ns, current_stat.st_size) != fingerprint:
                    continue
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
                passive_models = data.get("passive_direct_models", {})
                self._passive_direct_models = (
                    {
                        int(horizon): model
                        for horizon, model in passive_models.items()
                        if int(horizon) in DIRECT_FORECAST_HORIZONS_MINUTES
                        and getattr(model, "n_features_in_", None) == len(_PASSIVE_MONOTONIC_CST)
                    }
                    if isinstance(passive_models, dict)
                    else {}
                )
                self._metrics = data.get("metrics", {})
                self._last_trained = data.get("trained_at")
                self._training_samples = data.get("samples", 0)
                self._thermal_lag_minutes = data.get("thermal_lag", DEFAULT_THERMAL_LAG_MINUTES)
                self._training_notice = None
                self._artifact_fingerprint = fingerprint
                self._artifact_refresh_reason = None
                return True
        return False

    async def arefresh_if_changed(self, *, force: bool = False) -> bool:
        """Refresh a shared artifact without blocking the async event loop."""

        return await asyncio.to_thread(self.refresh_if_changed, force=force)

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
        """Load records, then delegate all v7 feature construction to a pure helper."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        values = await get_all_settings()
        reference_sensor_id = str(values.get("comfort_reference_sensor_id") or "").strip()
        weather_provider = str(values.get("weather_provider") or "open-meteo")
        async with get_session() as session:
            reading_result = await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= cutoff)
                .where(IndoorTempReading.is_stale.is_(False))
                .order_by(IndoorTempReading.timestamp)  # noqa: E712
            )
            raw_readings = reading_result.scalars().all()
            readings, strategy, sensor_count = self._select_indoor_observations(
                raw_readings, reference_sensor_id
            )
            if not readings:
                self._last_dataset_evidence = {
                    "active_heating_rows": 0,
                    "sensor_strategy": strategy,
                    "source_sensor_count": sensor_count,
                }
                return np.array([]), np.array([]), 0
            status_result = await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts >= readings[0].timestamp - dt.timedelta(hours=12))
                .where(DeviceStatusRecord.ts <= readings[-1].timestamp)
                .order_by(DeviceStatusRecord.ts)
            )
            weather_result = await session.execute(
                select(WeatherRecord)
                .where(WeatherRecord.ts >= readings[0].timestamp)
                .where(WeatherRecord.ts <= readings[-1].timestamp)
                .where(WeatherRecord.source == weather_provider)
                .order_by(WeatherRecord.ts)
            )
            dataset = build_window_dataset(
                readings,
                status_result.scalars().all(),
                weather_result.scalars().all(),
                DIRECT_FORECAST_HORIZONS_MINUTES,
            )
        features, targets = dataset.controlled.get(
            self._thermal_lag_minutes, (np.array([]), np.array([]))
        )
        self._last_dataset_evidence = {
            **dataset.evidence,
            "sensor_strategy": strategy,
            "source_sensor_count": sensor_count,
        }
        self._window_candidate_bundle = build_candidate_bundle(dataset) if HAS_SKLEARN else None
        return features, targets, len(targets)

    async def _build_dataset_legacy(self) -> tuple[np.ndarray, np.ndarray, int]:
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
        active_inputs: list[float] = []
        flat_active_status_times = self._flat_active_status_times(statuses)
        flat_active_heating_rows_excluded = 0

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

            evidence = classify_space_heating(
                operation_status=getattr(status, "operation_status", None),
                mode=getattr(status, "mode", None),
                direction=getattr(status, "direction", None),
                pump_duty=getattr(status, "pump_duty", None),
                device_action=getattr(status, "device_action", None),
                defrost_active=getattr(status, "defrost_active", None),
                zone1_operation_status=getattr(status, "zone1_operation_status", None),
                zone2_operation_status=getattr(status, "zone2_operation_status", None),
            )
            if evidence.code in {"domestic_hot_water", "cooling", "defrost"}:
                continue
            is_active_heating = evidence.active
            recent_heat_fraction = self._recent_heat_fraction(statuses, status_times, idx, t_sec)

            zone_water_temp = status.zone1_temp
            outdoor_temp = status.outdoor_temp
            if is_active_heating and status.ts in flat_active_status_times:
                flat_active_heating_rows_excluded += 1
                continue

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
            if outdoor_temp is None:
                continue
            if not is_active_heating:
                zone_water_temp = outdoor_temp

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
            zone_target_temp = (
                effective_zone_target_temperature(
                    status.zone1_target_temp,
                    outdoor_temp,
                    config=heat_curve,
                    fallback_c=zone_water_temp,
                )
                if is_active_heating
                else outdoor_temp
            )
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
                zone_target_temp=zone_target_temp,
                space_heating_fraction=1.0 if is_active_heating else 0.0,
                recent_heat_fraction=recent_heat_fraction,
                indoor_trend_c_per_hour=indoor_trend,
            )
            X_rows.append(features)
            y_rows.append(reading.temperature)
            if is_active_heating:
                active_heating_rows += 1
                active_inputs.append(float(zone_water_temp))

        n = len(X_rows)
        self._last_dataset_evidence = {
            "active_heating_rows": active_heating_rows,
            "active_input_buckets": len({round(value * 2) / 2 for value in active_inputs}),
            "active_input_range_c": round(max(active_inputs) - min(active_inputs), 3)
            if active_inputs
            else 0.0,
            "flat_active_heating_rows_excluded": flat_active_heating_rows_excluded,
            "sensor_strategy": sensor_strategy,
            "source_sensor_count": source_sensor_count,
        }
        if n == 0:
            return np.array([]), np.array([]), 0

        return np.array(X_rows), np.array(y_rows), n

    @staticmethod
    def _flat_active_status_times(statuses: list[DeviceStatusRecord]) -> set[dt.datetime]:
        """Identify six-hour, twelve-sample active runs with no useful heat input variance."""

        flat_times: set[dt.datetime] = set()
        start = 0
        while start < len(statuses):
            end = start
            values: list[float] = []
            while end < len(statuses):
                status = statuses[end]
                evidence = classify_space_heating(
                    operation_status=getattr(status, "operation_status", None),
                    mode=getattr(status, "mode", None),
                    direction=getattr(status, "direction", None),
                    pump_duty=getattr(status, "pump_duty", None),
                    device_action=getattr(status, "device_action", None),
                    defrost_active=getattr(status, "defrost_active", None),
                    zone1_operation_status=getattr(status, "zone1_operation_status", None),
                    zone2_operation_status=getattr(status, "zone2_operation_status", None),
                )
                if not evidence.active or status.zone1_temp is None:
                    break
                values.append(float(status.zone1_temp))
                if max(values) - min(values) > 0.1 + 1e-9:
                    break
                end += 1
            if end - start >= 12 and (statuses[end - 1].ts - statuses[start].ts) >= dt.timedelta(
                hours=6
            ):
                flat_times.update(status.ts for status in statuses[start:end])
            start = max(end, start + 1)
        return flat_times

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

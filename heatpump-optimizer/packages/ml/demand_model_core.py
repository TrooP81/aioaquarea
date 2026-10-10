"""Demand model implementation."""

from __future__ import annotations

import datetime as dt
import asyncio
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select

from packages.core.config import settings as app_settings
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, WeatherRecord
from packages.core.settings_service import get_user_tz
from packages.ml.lifecycle import LifecycleUnavailable
from packages.ml.models_common import (
    HAS_SKLEARN,
    MODEL_DIR as MODEL_DIR,
    COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS,
    _logger,
    chronological_fit_holdout,
    evaluate_regression,
    iter_confirmed_zero_intervals,
    iter_counter_change_intervals,
    make_monotonic_regressor,
    time_series_cv_mae,
    time_series_cv_naive_mae,
)

# Weather samples are typically hourly; only join a consumption interval to a
# weather observation within this gap, otherwise fall back to defaults.
MAX_WEATHER_GAP_SECONDS = 2 * 3600
MIN_TRAINING_SAMPLES = 168
# Required relative MAE improvement over predicting the historical mean.
MIN_SKILL_OVER_NAIVE = 0.05

# Physical monotonicity for demand features
# [outdoor_temp, wind_speed, irradiance, precipitation, humidity, cloud_cover,
#  hour_sin, hour_cos, dow_sin, dow_cos]:
# heating demand falls as it warms up outside (-1) and as solar gain rises (-1),
# and rises with wind-driven heat loss (+1). Time-of-day/week are unconstrained.
_DEMAND_MONOTONIC_CST = [-1, 1, -1, 0, 0, 0, 0, 0, 0, 0]
DEMAND_MODEL_ARTIFACT_PREFIX = "demand_model_weather_v3_"
DEMAND_MODEL_ARTIFACT_GLOB = f"{DEMAND_MODEL_ARTIFACT_PREFIX}*.pkl"
DEMAND_MAE_BASELINE = "demand_weather_v3"
DEMAND_COUNTER_CONFIRMATION_ENABLED = False
DEMAND_LABEL_EMBARGO = dt.timedelta(hours=4)
DEMAND_HISTORY_WINDOW = dt.timedelta(days=90)
DEMAND_MAX_MATERIALIZED_ROWS = 50_000
_PREPROCESSING_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="demand-preprocess")


class DemandPreparationBusy(LifecycleUnavailable):
    pass


@dataclass
class _PreparationFlight:
    task: asyncio.Task
    worker_started: asyncio.Event
    waiters: int = 0


_PREPARATION_FLIGHTS: WeakKeyDictionary = WeakKeyDictionary()


def build_demand_candidate(data: dict[str, object]) -> dict[str, object] | None:
    features = np.asarray(data["features"], dtype=float)
    targets = np.asarray(data["targets"], dtype=float)
    bounds = data["bounds"]
    if (
        len(bounds) != len(targets)
        or not np.isfinite(features).all()
        or not np.isfinite(targets).all()
    ):
        return None
    fit, holdout = chronological_fit_holdout(
        bounds, holdout_fraction=0.2, embargo=DEMAND_LABEL_EMBARGO
    )
    if len(fit) < MIN_TRAINING_SAMPLES or len(holdout) < 24:
        return None
    models = {}
    evaluated = {}
    for quantile, name in ((0.5, "median"), (0.1, "lower"), (0.9, "upper")):
        model = make_monotonic_regressor(
            _DEMAND_MONOTONIC_CST, max_iter=250, loss="quantile", quantile=quantile
        )
        model.fit(features[fit], targets[fit])
        models[name] = model
        evaluated[name] = model.predict(features[holdout])
    mae = float(np.mean(np.abs(evaluated["median"] - targets[holdout])))
    baseline = float(np.mean(targets[fit]))
    naive_mae = float(np.mean(np.abs(baseline - targets[holdout])))
    if naive_mae <= 0 or 1 - mae / naive_mae < MIN_SKILL_OVER_NAIVE:
        return None
    return {
        **models,
        "schema_version": "demand_lifecycle_v1",
        "metrics": {
            "mae": mae,
            "naive_mae": naive_mae,
            "skill_vs_naive": 1 - mae / naive_mae,
            "samples": len(targets),
            "fit_samples": len(fit),
            "holdout_samples": len(holdout),
            "unit": "kW",
            "validation_method": "chronological_embargo_exact_fit",
        },
        "baseline_kw": baseline,
        "device_id": data.get("device_id"),
        "evaluation": {
            "features": features[holdout],
            "predictions": evaluated,
            "targets": targets[holdout],
            "fit_label_end": bounds[int(fit[-1])][1].isoformat(),
            "holdout_issue_start": bounds[int(holdout[0])][0].isoformat(),
        },
    }


class DemandModel:
    # Quantile levels for the uncertainty band. The median (0.5) is the point
    # prediction; the optimizer can use the p10/p90 spread to price risk.
    QUANTILES = (0.1, 0.5, 0.9)

    def __init__(self):
        self._model = None  # median (p50) — the point predictor
        self._model_lower = None  # p10
        self._model_upper = None  # p90
        self._version: str = "untrained"
        self._metrics: dict[str, float | int | str | None] = {}
        self._last_data_quality: dict[str, object] = self._empty_data_quality()
        self._qualified_reference = None
        self._sample_bounds: list[tuple[dt.datetime, dt.datetime]] = []
        self._sample_devices: set[str] = set()

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

    @property
    def metrics(self) -> dict[str, float | int | str | None]:
        """Persisted forward-chaining validation evidence for the live model."""
        return dict(self._metrics)

    @property
    def version(self) -> str:
        """Version parsed from the currently loaded artifact filename."""
        return self._version

    def reset(self) -> None:
        """Discard the trained model and return to the untrained fallback state."""
        self._model = None
        self._model_lower = None
        self._model_upper = None
        self._version = "untrained"
        self._metrics = {}
        self._qualified_reference = None

    def _has_qualified_incumbent(self) -> bool:
        from packages.ml.lifecycle import reference_is_qualified

        return reference_is_qualified(self._qualified_reference)

    @property
    def is_trained(self) -> bool:
        return self._model is not None and self._has_qualified_incumbent()

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
        if not self._has_qualified_incumbent():
            self.reset()
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
        naive_mae = time_series_cv_naive_mae(y)
        skill = 1.0 - mae / naive_mae if naive_mae > 0 else 0.0
        if skill < MIN_SKILL_OVER_NAIVE:
            _logger.warning(
                "demand_model_no_skill_over_naive",
                mae=round(mae, 3),
                naive_mae=round(naive_mae, 3),
                samples=len(X),
            )
            return {
                "status": "no_skill",
                "mae": round(mae, 3),
                "naive_mae": round(naive_mae, 3),
                "skill_vs_naive": round(skill, 3),
                "samples": len(X),
            }

        has_prior = self._has_qualified_incumbent()
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

        return {
            "status": "maintenance_required",
            "samples": len(X),
            "mae": mae,
            "cv_std": cv_std,
            "baseline_mae": decision["baseline_mae"],
            "reason": "diagnostic_only_use_queued_lifecycle",
            "control_ready": False,
        }

    def predict_hourly(self, weather_forecast: list[dict], hours: int = 24) -> list[float]:
        predictions = []
        now = dt.datetime.now(dt.timezone.utc)
        for h in range(hours):
            weather = weather_forecast[h] if h < len(weather_forecast) else {}
            forecast_ts = weather.get("ts")
            ts = (
                forecast_ts if isinstance(forecast_ts, dt.datetime) else now + dt.timedelta(hours=h)
            )
            temp = weather.get("temperature", 5.0)
            wind = weather.get("wind_speed", 3.0)
            irradiance = weather.get("irradiance", 0.0)
            precipitation = weather.get("precipitation", 0.0)
            humidity = weather.get("humidity", 60.0)
            cloud_cover = weather.get("cloud_cover", 0.5)
            if self.is_trained:
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
            weather = weather_forecast[h] if h < len(weather_forecast) else {}
            forecast_ts = weather.get("ts")
            ts = (
                forecast_ts if isinstance(forecast_ts, dt.datetime) else now + dt.timedelta(hours=h)
            )
            temp = weather.get("temperature", 5.0)
            wind = weather.get("wind_speed", 3.0)
            irradiance = weather.get("irradiance", 0.0)
            precipitation = weather.get("precipitation", 0.0)
            humidity = weather.get("humidity", 60.0)
            cloud_cover = weather.get("cloud_cover", 0.5)
            if self.is_trained:
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
        try:
            await self._prepare_data()
        except DemandPreparationBusy:
            return {
                **self.last_data_quality,
                "ready_to_train": False,
                "training_blocker": "preparation_busy",
            }
        return self.last_data_quality

    async def _prepare_data(
        self,
        *,
        lifecycle_evidence: bool = False,
        cutoff: dt.datetime | None = None,
        device_id: str | None = None,
        weather_provider: str | None = None,
        worker=None,
    ) -> tuple[np.ndarray, np.ndarray]:
        loop = asyncio.get_running_loop()
        key = (lifecycle_evidence, cutoff, device_id, weather_provider, worker)
        current = _PREPARATION_FLIGHTS.get(loop)
        if current is not None and current[1].task.done():
            _PREPARATION_FLIGHTS.pop(loop)
            current = None
        if current is not None and current[0] != key:
            raise DemandPreparationBusy("demand_preparation_busy")
        if current is None:
            started = asyncio.Event()
            flight = _PreparationFlight(
                loop.create_task(
                    self._materialize_prepared(
                        lifecycle_evidence=lifecycle_evidence,
                        cutoff=cutoff,
                        device_id=device_id,
                        weather_provider=weather_provider,
                        worker=worker,
                        worker_started=started,
                    )
                ),
                started,
            )
            _PREPARATION_FLIGHTS[loop] = (key, flight)

            def finished(task):
                active = _PREPARATION_FLIGHTS.get(loop)
                if active is not None and active[1] is flight:
                    _PREPARATION_FLIGHTS.pop(loop)
                if not task.cancelled():
                    task.exception()

            flight.task.add_done_callback(finished)
        else:
            flight = current[1]
        flight.waiters += 1
        try:
            prepared = await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            if lifecycle_evidence and flight.worker_started.is_set():
                while not flight.task.done():
                    try:
                        await asyncio.shield(flight.task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            raise
        finally:
            flight.waiters -= 1
            if (
                flight.waiters == 0
                and not flight.worker_started.is_set()
                and not flight.task.done()
            ):
                flight.task.cancel()
                with suppress(asyncio.CancelledError):
                    await flight.task
        self._last_data_quality = prepared["quality"]
        self._sample_bounds = prepared["bounds"]
        self._sample_devices = prepared["devices"]
        return prepared["features"], prepared["targets"]

    async def _materialize_prepared(
        self,
        *,
        lifecycle_evidence: bool,
        cutoff: dt.datetime | None,
        device_id: str | None,
        weather_provider: str | None,
        worker,
        worker_started: asyncio.Event,
    ):
        timezone = ZoneInfo(await get_user_tz())
        consumption_query = select(
            ConsumptionRecord.device_id,
            ConsumptionRecord.ts,
            ConsumptionRecord.heat_kwh,
            ConsumptionRecord.cool_kwh,
            ConsumptionRecord.tank_kwh,
            ConsumptionRecord.outdoor_temp,
            ConsumptionRecord.source_date,
        )
        weather_query = select(
            WeatherRecord.ts,
            WeatherRecord.temperature,
            WeatherRecord.wind_speed,
            WeatherRecord.irradiance,
            WeatherRecord.precipitation,
            WeatherRecord.humidity,
            WeatherRecord.cloud_cover,
        )
        status_query = select(DeviceStatusRecord)
        cutoff = cutoff or dt.datetime.now(dt.timezone.utc)
        since = cutoff - DEMAND_HISTORY_WINDOW
        consumption_query = consumption_query.where(
            ConsumptionRecord.ts >= since, ConsumptionRecord.ts <= cutoff
        ).limit(DEMAND_MAX_MATERIALIZED_ROWS + 1)
        weather_query = weather_query.where(
            WeatherRecord.ts >= since - dt.timedelta(hours=2), WeatherRecord.ts <= cutoff
        ).limit(DEMAND_MAX_MATERIALIZED_ROWS + 1)
        status_query = status_query.where(
            DeviceStatusRecord.ts >= since - dt.timedelta(minutes=15),
            DeviceStatusRecord.ts <= cutoff,
        ).limit(DEMAND_MAX_MATERIALIZED_ROWS + 1)
        if device_id is not None:
            consumption_query = consumption_query.where(ConsumptionRecord.device_id == device_id)
            status_query = status_query.where(DeviceStatusRecord.device_id == device_id)
        if weather_provider is not None:
            weather_query = weather_query.where(WeatherRecord.source == weather_provider)
        async with get_session() as session:
            consumption_rows = (
                await session.execute(consumption_query.order_by(ConsumptionRecord.ts))
            ).all()
            weather_rows = (await session.execute(weather_query.order_by(WeatherRecord.ts))).all()
            statuses = (
                (await session.execute(status_query.order_by(DeviceStatusRecord.ts)))
                .scalars()
                .all()
                if lifecycle_evidence
                else []
            )
        if (
            max(len(consumption_rows), len(weather_rows), len(statuses))
            > DEMAND_MAX_MATERIALIZED_ROWS
        ):
            raise ValueError("Demand materialization overflow")
        data = {
            "consumption_rows": consumption_rows,
            "weather_rows": weather_rows,
            "statuses": statuses,
            "timezone": timezone,
            "lifecycle_evidence": lifecycle_evidence,
        }
        worker_started.set()
        if worker is not None:
            return await worker(self._preprocess_materialized, data)
        else:
            future = asyncio.get_running_loop().run_in_executor(
                _PREPROCESSING_EXECUTOR, self._preprocess_materialized, data
            )
            try:
                prepared = await asyncio.shield(future)
            except asyncio.CancelledError:
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if future.done() and not future.cancelled():
                    with suppress(Exception):
                        future.result()
                raise
            return prepared

    @staticmethod
    def _preprocess_materialized(data):
        from packages.ml.safe_persistence import fitting_worker_scope

        processor = DemandModel()
        with fitting_worker_scope():
            features, targets = processor._prepare_materialized(**data)
        return {
            "features": features,
            "targets": targets,
            "quality": processor.last_data_quality,
            "bounds": processor._sample_bounds,
            "devices": processor._sample_devices,
        }

    def _prepare_materialized(
        self, *, consumption_rows, weather_rows, statuses, timezone, lifecycle_evidence
    ):
        self._sample_bounds = []
        self._sample_devices = set()
        if not consumption_rows:
            self._last_data_quality = self._empty_data_quality()
            return np.array([]), np.array([])

        weather_rows = sorted(weather_rows, key=lambda row: row.ts)
        weather_seconds = [row.ts.timestamp() for row in weather_rows]

        def _nearest_weather(ts: dt.datetime):
            if not weather_seconds:
                return None
            position = bisect_left(weather_seconds, ts.timestamp())
            indexes = [
                index for index in (position - 1, position) if 0 <= index < len(weather_rows)
            ]
            idx = min(indexes, key=lambda index: abs(weather_seconds[index] - ts.timestamp()))
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
        counter_diagnostics: dict[str, int] = {}
        status_by_device = {}
        for status in statuses:
            status_by_device.setdefault(status.device_id, []).append(status)
        status_indexes = {}
        for device, records in status_by_device.items():
            ordered = sorted(records, key=lambda row: row.ts)
            status_indexes[device] = (ordered, [row.ts for row in ordered])
        positive_intervals = list(
            iter_counter_change_intervals(
                consumption_rows,
                "heat_kwh",
                timezone,
                max_interval_hours=4.0,
                diagnostics=counter_diagnostics,
                confirm_changes=DEMAND_COUNTER_CONFIRMATION_ENABLED,
            )
        )
        positives_by_device = {}
        for interval in positive_intervals:
            positives_by_device.setdefault(interval.device_id, []).append(interval)
        positive_indexes = {}
        for device, records in positives_by_device.items():
            ordered = sorted(records, key=lambda interval: interval.start_ts)
            latest_end = ordered[0].end_ts
            ends = []
            for interval in ordered:
                latest_end = max(latest_end, interval.end_ts)
                ends.append(latest_end)
            positive_indexes[device] = ([interval.start_ts for interval in ordered], ends)

        def positive_overlap(zero):
            starts, ends = positive_indexes.get(zero.device_id, ([], []))
            index = bisect_left(starts, zero.end_ts) - 1
            return index >= 0 and ends[index] > zero.start_ts

        zeros = [
            zero
            for zero in iter_confirmed_zero_intervals(
                consumption_rows, statuses, timezone, status_indexes=status_indexes
            )
            if not positive_overlap(zero)
        ]
        ordered_intervals = sorted([*positive_intervals, *zeros], key=lambda item: item.end_ts)
        for interval in ordered_intervals:
            intervals += 1
            # Target is *space-heating* electrical demand only. DHW and cooling
            # are excluded because they violate the model's monotonic physics
            # (DHW is outdoor-independent; cooling demand rises — not falls —
            # with outdoor temperature). Intervals with no space-heating draw
            # are naturally dropped by the rate<=0 guard below.
            rate = interval.energy_kwh / interval.elapsed_hours
            if rate < 0:
                rejected_nonpositive += 1
                continue
            if lifecycle_evidence:
                from packages.ml.thermal import ThermalModel

                records, times = status_indexes.get(interval.device_id, ([], []))
                state, _ = ThermalModel._continuous_indoor_state(
                    records, interval.start_ts, interval.end_ts, status_times=times
                )
                if state is None:
                    continue
            if rate > max_rate:
                skip_rate_bounds += 1
                continue

            weather = _nearest_weather(interval.end_ts)
            outdoor = interval.row.outdoor_temp
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
                    interval.end_ts.hour,
                    interval.end_ts.weekday(),
                    precipitation,
                    humidity,
                    cloud_cover,
                )
            )
            y_list.append(rate)
            self._sample_bounds.append((interval.start_ts, interval.end_ts))
            self._sample_devices.add(interval.device_id)

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
            "counter_zero_delta": counter_diagnostics.get("zero_delta", 0),
            "confirmed_zero_samples": len(zeros),
            "counter_midday_decrease": counter_diagnostics.get("midday_decrease", 0),
            "counter_source_day_reset": counter_diagnostics.get("source_day_reset", 0),
            "counter_correction_reanchor": counter_diagnostics.get("correction_reanchor", 0),
            "counter_outside_window": counter_diagnostics.get("outside_window", 0),
            **{
                f"counter_{key}": counter_diagnostics.get(key, 0)
                for key in COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS
            },
        }
        log_data_quality = dict(self._last_data_quality)
        for key in COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS:
            if not log_data_quality[f"counter_{key}"]:
                log_data_quality.pop(f"counter_{key}")
        _logger.info("demand_training_data_prepared", **log_data_quality)
        return np.array(X_list), np.array(y_list)

    async def materialize_candidate(
        self,
        *,
        cutoff: dt.datetime | None = None,
        device_id: str | None = None,
        weather_provider: str | None = None,
        worker=None,
    ) -> dict[str, object] | None:
        features, targets = await self._prepare_data(
            lifecycle_evidence=True,
            cutoff=cutoff,
            device_id=device_id,
            weather_provider=weather_provider,
            worker=worker,
        )
        if len(self._sample_devices) != 1:
            return None
        return {
            "features": features,
            "targets": targets,
            "bounds": self._sample_bounds,
            "device_id": next(iter(self._sample_devices)),
        }

    async def arefresh_committed(self, *, lifecycle=None, scope: dict | None = None) -> bool:
        from packages.ml.forecast_quality import (
            check_exact_fit,
            materialize_native_inputs,
            native_scope,
        )
        from packages.ml.lifecycle import Lifecycle, reference_is_qualified

        authority = lifecycle if lifecycle is not None else Lifecycle()
        try:
            state = await authority.read()
            reference = state.reference("demand")
            if not reference_is_qualified(reference):
                raise ValueError("demand_not_qualified")
            if scope is None:
                inputs = await materialize_native_inputs(
                    "demand",
                    now=dt.datetime.now(dt.timezone.utc),
                    device_id=reference.manifest["device_id"],
                )
                if inputs is None:
                    raise ValueError("current_demand_scope_unavailable")
                scope = native_scope(inputs)
            if not reference_is_qualified(reference, scope=scope):
                raise ValueError("demand_scope_mismatch")
            payload = await authority.load_committed(
                "demand", expected_hash=reference.artifact_hash
            )
            if payload.get("schema_version") != "demand_lifecycle_v1":
                raise ValueError("demand_schema_mismatch")
            await asyncio.to_thread(check_exact_fit, "demand", payload)
            current = (await authority.read()).reference("demand")
            if current != reference or not reference_is_qualified(current, scope=scope):
                raise ValueError("demand_authority_changed")
        except Exception as error:
            self.reset()
            _logger.warning("demand_committed_unavailable", error_type=type(error).__name__)
            return False
        self._model, self._model_lower, self._model_upper = (
            payload[name] for name in ("median", "lower", "upper")
        )
        self._metrics = dict(payload["metrics"])
        self._version = reference.artifact_hash
        self._qualified_reference = reference
        return True

    def load_latest(self) -> bool:
        if not self._has_qualified_incumbent():
            self.reset()
        return self.is_trained

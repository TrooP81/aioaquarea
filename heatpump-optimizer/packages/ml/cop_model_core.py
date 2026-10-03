"""COP model implementation."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select

from packages.core.config import settings as app_settings
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, WeatherRecord
from packages.core.settings_service import get_user_tz
from packages.ml.models_common import (
    HAS_SKLEARN,
    MODEL_DIR,
    COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS,
    _logger,
    evaluate_regression,
    iter_counter_change_intervals,
    make_monotonic_regressor,
    prune_old_models,
    time_series_cv_mae,
    write_mae_baseline,
)

# Physical monotonicity for COP features
# [outdoor_temp, tank_target, hour_sin, hour_cos, precipitation, humidity, cloud_cover]:
# COP rises as it gets warmer outside (+1) and falls as the tank target rises (-1).
_COP_MONOTONIC_CST = [1, -1, 0, 0, 0, 0, 0]

# Version the persisted artifact by its training-target definition.  Earlier
# checkpoints used total heating + DHW electricity while the target was based
# on a tank-only thermal measurement, so they must not be used after the
# denominator correction below.
COP_MODEL_ARTIFACT_PREFIX = "cop_model_weather_dhw_v5_"
COP_MODEL_ARTIFACT_GLOB = f"{COP_MODEL_ARTIFACT_PREFIX}*.pkl"
COP_MAE_BASELINE = "cop_weather_dhw_v5"
COP_COUNTER_CONFIRMATION_ENABLED = True


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
        self._last_data_quality: dict[str, int] = {}

    def reset(self) -> None:
        """Discard the trained model and return to the untrained fallback state."""
        self._model = None
        self._version = "untrained"
        self._metrics = {}

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def metrics(self) -> dict[str, float]:
        """Persisted forward-chaining validation evidence for the live model."""
        return dict(self._metrics)

    @property
    def last_data_quality(self) -> dict[str, int]:
        """Aggregate counter diagnostics from the latest preparation pass."""
        return dict(self._last_data_quality)

    @property
    def version(self) -> str:
        return self._version

    async def train(self) -> dict[str, object]:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required: pip install scikit-learn")

        X, y = await self._prepare_training_data()
        if len(X) < 50:
            return {"error": "Insufficient data", "samples": len(X)}

        model = make_monotonic_regressor(_COP_MONOTONIC_CST, max_iter=300)
        # Forward-chaining time-series CV (never trains on the future) gives an
        # honest MAE; plain KFold would leak later intervals into earlier folds.
        mae, cv_std = time_series_cv_mae(model, X, y)

        has_prior = bool(list(MODEL_DIR.glob(COP_MODEL_ARTIFACT_GLOB)))
        decision = evaluate_regression(COP_MAE_BASELINE, mae, has_prior)
        if not decision["deploy"]:
            _logger.warning(
                "cop_model_deploy_skipped",
                mae=round(mae, 3),
                baseline_mae=decision["baseline_mae"],
                samples=len(X),
            )
            return {
                "status": "regressed",
                "metrics": {
                    "mae": mae,
                    "baseline_mae": decision["baseline_mae"],
                    "samples": len(X),
                },
            }

        model.fit(X, y)

        self._model = model
        self._version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M")
        self._metrics = {
            "mae": round(float(mae), 3),
            "samples": int(len(X)),
            "cv_std": round(float(cv_std), 3),
            "baseline_mae": decision["baseline_mae"],
            "validation_method": "forward_chaining_time_series_cv",
        }

        _logger.info(
            "cop_model_trained",
            samples=len(X),
            mae=round(mae, 3),
            baseline_mae=decision["baseline_mae"],
            y_mean=round(float(np.mean(y)), 2),
            y_std=round(float(np.std(y)), 2),
        )

        model_path = MODEL_DIR / f"{COP_MODEL_ARTIFACT_PREFIX}{self._version}.pkl"
        from packages.ml.safe_persistence import safe_dump

        safe_dump({"model": model, "metrics": self._metrics}, model_path)
        prune_old_models(COP_MODEL_ARTIFACT_GLOB, model_dir=MODEL_DIR)
        write_mae_baseline(COP_MAE_BASELINE, mae)
        return {"version": self._version, "metrics": self._metrics, "model_path": str(model_path)}

    def predict(
        self,
        outdoor_temp: float,
        tank_target: int,
        hour: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> float:
        return self._estimate_thermal_output(outdoor_temp, tank_target) / self.predict_cop(
            outdoor_temp, tank_target, hour, precipitation, humidity, cloud_cover
        )

    def predict_cop(
        self,
        outdoor_temp: float,
        tank_target: int,
        hour: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> float:
        if self._model is None:
            return self._default_cop_curve(outdoor_temp)
        features = self._make_features(
            outdoor_temp, tank_target, hour, precipitation, humidity, cloud_cover
        )
        raw = float(self._model.predict(features.reshape(1, -1))[0])
        return max(self.COP_MIN, min(self.COP_MAX, raw))

    @staticmethod
    def _default_cop_curve(outdoor_temp: float) -> float:
        return max(1.5, min(6.0, 3.5 + 0.1 * outdoor_temp))

    @staticmethod
    def _estimate_thermal_output(outdoor_temp: float, tank_target: int) -> float:
        return 2.0 * max(0.5, 1.0 - outdoor_temp * 0.02)

    @staticmethod
    def _make_features(
        outdoor_temp: float,
        tank_target: int,
        hour: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> np.ndarray:
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        return np.array(
            [
                outdoor_temp,
                tank_target,
                hour_sin,
                hour_cos,
                max(0.0, precipitation),
                max(0.0, min(100.0, humidity)),
                max(0.0, min(1.0, cloud_cover)),
            ]
        )

    async def _prepare_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        timezone = ZoneInfo(await get_user_tz())
        async with get_session() as session:
            consumption_rows = (
                await session.execute(
                    select(
                        ConsumptionRecord.device_id,
                        ConsumptionRecord.ts,
                        ConsumptionRecord.heat_kwh,
                        ConsumptionRecord.tank_kwh,
                        ConsumptionRecord.outdoor_temp,
                    ).order_by(ConsumptionRecord.ts)
                )
            ).all()
            status_rows = (
                await session.execute(
                    select(
                        DeviceStatusRecord.device_id,
                        DeviceStatusRecord.ts,
                        DeviceStatusRecord.tank_target_temp,
                        DeviceStatusRecord.tank_temp,
                        DeviceStatusRecord.outdoor_temp,
                        DeviceStatusRecord.direction,
                        DeviceStatusRecord.device_action,
                        DeviceStatusRecord.zone1_temp,
                        DeviceStatusRecord.defrost_active,
                    ).order_by(DeviceStatusRecord.ts)
                )
            ).all()
            weather_rows = (
                await session.execute(
                    select(
                        WeatherRecord.ts,
                        WeatherRecord.temperature,
                        WeatherRecord.precipitation,
                        WeatherRecord.humidity,
                        WeatherRecord.cloud_cover,
                    ).order_by(WeatherRecord.ts)
                )
            ).all()

        if not consumption_rows or not status_rows:
            return np.array([]), np.array([])

        status_by_device: dict[str, list] = defaultdict(list)
        for status in status_rows:
            status_by_device[status.device_id].append(status)
        for statuses in status_by_device.values():
            statuses.sort(key=lambda status: status.ts)
        weather_sorted = sorted(weather_rows, key=lambda w: w.ts)
        weather_ts = [w.ts for w in weather_sorted]

        def _find_closest_status(statuses, ts):
            import bisect

            status_ts = [status.ts for status in statuses]
            idx = bisect.bisect_left(status_ts, ts)
            best = None
            best_gap = float("inf")
            for candidate_idx in (idx - 1, idx):
                if 0 <= candidate_idx < len(statuses):
                    gap = abs((statuses[candidate_idx].ts - ts).total_seconds())
                    if gap < best_gap:
                        best_gap = gap
                        best = statuses[candidate_idx]
            return best if best and best_gap <= 1200 else None

        def _weather_at(ts) -> tuple[float | None, float, float, float]:
            if not weather_sorted:
                return None, 0.0, 60.0, 0.5
            import bisect

            idx = bisect.bisect_left(weather_ts, ts)
            candidates = [weather_sorted[i] for i in (idx - 1, idx) if 0 <= i < len(weather_sorted)]
            if not candidates:
                return None, 0.0, 60.0, 0.5
            closest = min(candidates, key=lambda item: abs((item.ts - ts).total_seconds()))
            if abs((closest.ts - ts).total_seconds()) > 7200:
                return None, 0.0, 60.0, 0.5
            return (
                float(closest.temperature) if closest.temperature is not None else None,
                max(0.0, float(closest.precipitation or 0.0)),
                max(
                    0.0,
                    min(
                        100.0,
                        float(
                            60.0 if getattr(closest, "humidity", None) is None else closest.humidity
                        ),
                    ),
                ),
                max(
                    0.0,
                    min(
                        1.0,
                        float(
                            0.5
                            if getattr(closest, "cloud_cover", None) is None
                            else closest.cloud_cover
                        ),
                    ),
                ),
            )

        X_list = []
        y_list = []
        counter_diagnostics: dict[str, int] = {}
        skip_no_status = 0
        skip_defrost = 0
        skip_mixed_mode = 0
        skip_no_dhw_activity = 0
        skip_cop_low = 0
        skip_cop_high = 0
        used_status_path = 0
        skip_no_measured_thermal = 0

        for interval in iter_counter_change_intervals(
            consumption_rows,
            "tank_kwh",
            timezone,
            min_interval_hours=0.5,
            max_interval_hours=2.0,
            diagnostics=counter_diagnostics,
            confirm_changes=COP_COUNTER_CONFIRMATION_ENABLED,
        ):
            statuses = status_by_device.get(interval.device_id, [])
            prev_status_rec = _find_closest_status(statuses, interval.start_ts)
            curr_status_rec = _find_closest_status(statuses, interval.end_ts)
            window_statuses = [
                status for status in statuses if interval.start_ts <= status.ts <= interval.end_ts
            ]
            if not prev_status_rec or not curr_status_rec or not window_statuses:
                skip_no_status += 1
                continue
            if any(getattr(status, "defrost_active", False) for status in window_statuses):
                skip_defrost += 1
                continue
            if any(
                getattr(status, "device_action", None) in {"HEATING", "COOLING"}
                for status in window_statuses
            ):
                skip_mixed_mode += 1
                continue
            if not any(
                getattr(status, "device_action", None) == "HEATING_WATER"
                or getattr(status, "direction", None) == "WATER"
                for status in window_statuses
            ):
                skip_no_dhw_activity += 1
                continue

            used_status_path += 1
            tank_thermal = 0.0
            if prev_status_rec.tank_temp is not None and curr_status_rec.tank_temp is not None:
                tank_delta = curr_status_rec.tank_temp - prev_status_rec.tank_temp
                if tank_delta > 0:
                    tank_thermal = tank_delta * self._tank_kwh_per_degree()
            if tank_thermal <= 0:
                skip_no_measured_thermal += 1
                continue

            cop = tank_thermal / interval.energy_kwh
            if cop < self.COP_MIN:
                skip_cop_low += 1
                continue
            if cop > self.COP_MAX:
                skip_cop_high += 1
                continue

            weather_temperature, precipitation, humidity, cloud_cover = _weather_at(interval.end_ts)
            features = self._make_features(
                (
                    weather_temperature
                    if weather_temperature is not None
                    else interval.row.outdoor_temp
                    if interval.row.outdoor_temp is not None
                    else 5.0
                ),
                int(curr_status_rec.tank_target_temp or 50),
                interval.end_ts.hour,
                precipitation,
                humidity,
                cloud_cover,
            )
            X_list.append(features)
            y_list.append(cop)

        self._last_data_quality = {
            f"counter_{key}": counter_diagnostics.get(key, 0)
            for key in COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS
        }
        _logger.info(
            "cop_training_data_prepared",
            total_consumption_rows=len(consumption_rows),
            status_rows=len(status_rows),
            paired_samples=len(y_list),
            counter_zero_delta=counter_diagnostics.get("zero_delta", 0),
            counter_midday_decrease=counter_diagnostics.get("midday_decrease", 0),
            counter_source_day_reset=counter_diagnostics.get("source_day_reset", 0),
            counter_correction_reanchor=counter_diagnostics.get("correction_reanchor", 0),
            counter_outside_window=counter_diagnostics.get("outside_window", 0),
            **{key: value for key, value in self._last_data_quality.items() if value},
            skip_no_status=skip_no_status,
            skip_defrost=skip_defrost,
            skip_mixed_mode=skip_mixed_mode,
            skip_no_dhw_activity=skip_no_dhw_activity,
            skip_no_measured_thermal=skip_no_measured_thermal,
            skip_cop_low=skip_cop_low,
            skip_cop_high=skip_cop_high,
            used_status_path=used_status_path,
            kwh_per_degree=round(self._tank_kwh_per_degree(), 4),
        )
        return np.array(X_list), np.array(y_list)

    def load_latest(self) -> bool:
        from packages.ml.safe_persistence import safe_load

        models = sorted(MODEL_DIR.glob(COP_MODEL_ARTIFACT_GLOB))
        if not models:
            _logger.info("cop_model_load_skip", reason="no model files found", dir=str(MODEL_DIR))
            return False
        for path in reversed(models):
            try:
                payload = safe_load(path)
            except ValueError as exc:
                _logger.warning("cop_model_load_failed", path=str(path), error=str(exc))
                continue
            # ``v3`` checkpoints created before validation metadata was
            # persisted stored the estimator directly. Keep them loadable, but
            # expose that their live validation evidence is unavailable.
            candidate = payload.get("model") if isinstance(payload, dict) else payload
            if getattr(candidate, "n_features_in_", None) != len(_COP_MONOTONIC_CST):
                _logger.info(
                    "cop_model_load_skip", path=str(path), reason="obsolete_feature_schema"
                )
                continue
            self._model = candidate
            self._version = path.stem.replace(COP_MODEL_ARTIFACT_PREFIX, "")
            raw_metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            self._metrics = dict(raw_metrics) if isinstance(raw_metrics, dict) else {}
            _logger.info("cop_model_loaded", version=self._version, path=str(path))
            return True
        return False

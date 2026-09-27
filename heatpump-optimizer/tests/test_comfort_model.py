"""Tests for the comfort model (indoor air temperature prediction)."""

import asyncio
import datetime as dt
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import AsyncMock

import numpy as np
import pytest

from packages.core.heat_curve import HeatCurveConfig, effective_zone_target_temperature
from packages.ml.comfort_model import (
    COMFORT_MODEL_CONSTRAINT_VERSION,
    COMFORT_MODEL_FORMAT_VERSION,
    COMFORT_MODEL_KNOT_RANGE,
    COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA,
    COMFORT_MODEL_PROJECTION_EPSILON,
    COMFORT_MODEL_TARGET_KIND,
    DIRECT_FORECAST_HORIZONS_MINUTES,
    _MONOTONIC_CST,
    _PASSIVE_MONOTONIC_CST,
    PASSIVE_FEATURE_NAMES,
    CONTROLLED_FEATURE_NAMES,
    ComfortModelCandidate,
    WindowDataset,
    _IndoorObservation,
    build_candidate_bundle,
    build_window_dataset,
    ComfortModel,
    MIN_TRAINING_ROWS,
    _confirmed_absent,
)


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


@pytest.mark.parametrize("mode", [None, 0, 1, "1", "2", "3", "heat", "AUTO_COOL", "bad"])
def test_comfort_training_labels_unchanged_by_canonical_classifier(mode):
    status = SimpleNamespace(
        operation_status=0,
        mode=mode,
        direction="PUMP",
        pump_duty=1,
        device_action="OFF",
        defrost_active=False,
        zone1_operation_status=1,
        zone2_operation_status=None,
    )

    assert _confirmed_absent(status) is (str(mode) not in {"1", "3"})


def test_direct_prediction_rejects_nearest_horizon():
    model = ComfortModel()
    model._model = MagicMock()
    model._direct_models = {60: MagicMock()}

    prediction = model.predict_indoor_temp(
        zone_water_temp=35.0,
        outdoor_temp=5.0,
        indoor_temp=20.0,
        forecast_horizon_minutes=120,
    )

    assert prediction is None


class _TrainingLease:
    def __init__(self, *, health=True, commit_error=None, rollback_error=None):
        self.health = health
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.committed = False
        self.rolled_back = False
        self.invalidated = False

    async def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.committed = True

    async def rollback(self):
        self.rolled_back = True
        if self.rollback_error:
            raise self.rollback_error

    async def invalidate(self):
        self.invalidated = True

    async def health_check(self):
        return self.health

    async def close(self):
        return None


class _TrainingLock:
    reason = "acquired"

    async def acquire(self):
        return _TrainingLease()


class _FixedTrainingLock:
    reason = "acquired"

    def __init__(self, lease):
        self.lease = lease

    async def acquire(self):
        return self.lease


class _UnavailableTrainingLock:
    reason = "training_lock_unavailable"

    async def acquire(self):
        return None


@pytest.fixture(autouse=True)
def _db_free_training_lock(monkeypatch):
    monkeypatch.setattr(
        "packages.ml.comfort_model.PostgresTrainingLock.acquire", _TrainingLock().acquire
    )


class TestComfortModelUntrained:
    def test_not_trained_by_default(self):
        model = ComfortModel()
        assert model.is_trained is False

    def test_predict_returns_none_when_untrained(self):
        model = ComfortModel()
        result = model.predict_indoor_temp(35.0, 5.0)
        assert result is None

    def test_required_zone_temp_returns_none_when_untrained(self):
        model = ComfortModel()
        result = model.required_zone_temp(21.0, 5.0)
        assert result is None


class TestComfortArtifactRefresh:
    def test_AC8_3_missing_artifact_reports_reason(self, tmp_path, monkeypatch):
        model = ComfortModel()
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)

        assert model.refresh_if_changed() is False
        assert model.artifact_refresh_reason == "artifact_missing"

    def test_AC8_3_bad_newest_artifact_keeps_last_known_good(self, tmp_path, monkeypatch):
        model = ComfortModel()
        good = MagicMock(n_features_in_=14)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load",
            MagicMock(
                side_effect=[
                    {
                        "model": good,
                        "format_version": COMFORT_MODEL_FORMAT_VERSION,
                        "target_kind": COMFORT_MODEL_TARGET_KIND,
                        "feature_schema": "weather_delta_v7_window_heat_controlled",
                        "feature_names": list(CONTROLLED_FEATURE_NAMES),
                        "passive_feature_schema": COMFORT_MODEL_PASSIVE_FEATURE_SCHEMA,
                        "passive_feature_names": list(PASSIVE_FEATURE_NAMES),
                        "controlled_constraints": _MONOTONIC_CST,
                        "passive_constraints": _PASSIVE_MONOTONIC_CST,
                        "constraint_version": COMFORT_MODEL_CONSTRAINT_VERSION,
                        "direct_horizons_minutes": DIRECT_FORECAST_HORIZONS_MINUTES,
                        "knot_range": COMFORT_MODEL_KNOT_RANGE,
                        "projection_epsilon": COMFORT_MODEL_PROJECTION_EPSILON,
                    },
                    ValueError("bad HMAC"),
                ]
            ),
        )
        good_path = tmp_path / "comfort_model_weather_delta_v7_window_heat_1.pkl"
        bad_path = tmp_path / "comfort_model_weather_delta_v7_window_heat_2.pkl"
        good_path.write_bytes(b"good")

        assert model.refresh_if_changed() is True
        bad_path.write_bytes(b"partial")

        assert model.refresh_if_changed() is False
        assert model._model is good
        assert model.artifact_refresh_reason == "artifact_integrity_failed"


class TestPassiveDirectForecastReadiness:
    def test_direct_passive_forecast_can_be_ready_without_heating_control(self):
        model = ComfortModel()
        model._model = MagicMock()
        model._passive_direct_models = {60: MagicMock()}
        model._metrics = {
            "passive_horizons": {"60": {"status": "trained", "mae": 0.6}},
            # No active heating evidence: full heating control remains blocked.
            "active_heating_rows": 0,
            "active_input_buckets": 4,
            "active_input_range_c": 3.0,
        }

        assert model.is_ready_for_control is False
        assert model.passive_forecast_readiness(60) == {
            "ready": True,
            "horizon_minutes": 60,
            "mae": 0.6,
        }

    def test_direct_passive_forecast_rejects_poor_validation(self):
        model = ComfortModel()
        model._model = MagicMock()
        model._passive_direct_models = {60: MagicMock()}
        model._metrics = {"passive_horizons": {"60": {"status": "trained", "mae": 1.2}}}

        readiness = model.passive_forecast_readiness(60)
        assert readiness["ready"] is False
        assert readiness["reason"] == "direct_forecast_mae_above_threshold"


class TestComfortControlReadiness:
    def test_requires_active_heating_evidence_and_baseline_improvement(self):
        model = ComfortModel()
        model._model = object()
        model._metrics = {
            "validated": True,
            "mae": 0.3,
            "r2": 0.5,
            "active_heating_rows": 0,
            "baseline_mae": 0.5,
        }

        assert model.control_readiness["reason"] == "insufficient_active_heating_evidence"

        model._metrics["active_heating_rows"] = 20
        model._metrics["active_input_buckets"] = 4
        model._metrics["active_input_range_c"] = 3.0
        model._metrics["baseline_mae"] = 0.3
        assert model.control_readiness["reason"] == "not_better_than_persistence_baseline"

        model._metrics["baseline_mae"] = 0.4
        assert model.is_ready_for_control is True

    def test_control_margin_is_bounded_by_validated_mae(self):
        model = ComfortModel()
        model._model = object()
        model._metrics = {
            "validated": True,
            "mae": 0.6,
            "r2": 0.3,
            "active_heating_rows": 30,
            "active_input_buckets": 4,
            "active_input_range_c": 3.0,
            "baseline_mae": 0.8,
        }

        assert model.is_ready_for_control is True
        assert model.control_margin_c == 0.45

    def test_AC9_3_requires_heat_input_variance(self):
        model = ComfortModel()
        model._model = object()
        model._metrics = {
            "validated": True,
            "mae": 0.3,
            "r2": 0.5,
            "active_heating_rows": 20,
            "active_input_buckets": 3,
            "active_input_range_c": 3.0,
            "baseline_mae": 0.5,
        }

        assert model.control_readiness["reason"] == "insufficient_heat_input_variance"
        model._metrics["active_input_buckets"] = 4
        model._metrics["active_input_range_c"] = 2.9
        assert model.control_readiness["reason"] == "insufficient_heat_input_variance"
        model._metrics["active_input_range_c"] = 3.0
        assert model.is_ready_for_control is True


class TestComfortModelFeatures:
    def test_panasonic_sentinel_target_uses_weather_compensated_curve(self):
        curve = HeatCurveConfig()

        assert effective_zone_target_temperature(-5.0, 5.0, config=curve) == 47.0
        assert effective_zone_target_temperature(-5.0, 20.0, config=curve) == 23.0
        assert effective_zone_target_temperature(42.0, 5.0, config=curve) == 42.0

    def test_make_features_shape(self):
        features = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        assert features.shape == (14,)

    def test_make_features_indoor_temp(self):
        # When indoor_temp is provided, it should be used as the final feature.
        features = ComfortModel._make_features(
            35.0,
            5.0,
            3.0,
            100.0,
            12,
            indoor_temp=21.0,
            precipitation=1.5,
            humidity=82.0,
            cloud_cover=0.75,
        )
        assert features[12] == 21.0
        assert features[7] == 1.5
        assert features[8] == 82.0
        assert features[9] == 0.75

    def test_make_features_indoor_temp_fallback(self):
        # When indoor_temp is None, falls back to outdoor_temp
        features = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        assert features[12] == 5.0  # outdoor_temp

    def test_make_features_cyclical_hour(self):
        features_0 = ComfortModel._make_features(35.0, 5.0, 3.0, 0.0, 0)
        features_12 = ComfortModel._make_features(35.0, 5.0, 3.0, 0.0, 12)
        # Hour 0: sin=0, cos=1
        assert abs(features_0[10]) < 0.01  # sin(0) ≈ 0
        assert abs(features_0[11] - 1.0) < 0.01  # cos(0) ≈ 1
        # Hour 12: sin=0, cos=-1
        assert abs(features_12[10]) < 0.01  # sin(π) ≈ 0
        assert abs(features_12[11] + 1.0) < 0.01  # cos(π) ≈ -1

    def test_make_features_keeps_heating_evidence_separate_from_water_temp(self):
        no_heat = ComfortModel._make_features(
            40.0,
            5.0,
            3.0,
            0.0,
            12,
            zone_target_temp=45.0,
            space_heating_fraction=0.0,
            recent_heat_fraction=0.25,
            indoor_trend_c_per_hour=-0.4,
        )
        active = ComfortModel._make_features(
            40.0,
            5.0,
            3.0,
            0.0,
            12,
            zone_target_temp=45.0,
            space_heating_fraction=1.0,
            recent_heat_fraction=0.75,
            indoor_trend_c_per_hour=0.4,
        )

        assert no_heat[1] == active[1] == 45.0
        assert no_heat[2:4].tolist() == [0.0, 0.25]
        assert active[2:4].tolist() == [1.0, 0.75]
        assert no_heat[13] == -0.4
        assert active[13] == 0.4

    def test_recent_heat_fraction_uses_component_evidence(self):
        statuses = [
            MagicMock(
                space_heating_active=None,
                operation_status=0,
                mode="1",
                direction="PUMP",
                pump_duty=1,
                device_action="OFF",
                defrost_active=False,
                zone1_operation_status=1,
                zone2_operation_status=0,
            ),
            MagicMock(space_heating_active=False),
        ]

        fraction = ComfortModel._recent_heat_fraction(
            statuses, np.array([0.0, 300.0]), latest_index=1, feature_time=300.0
        )

        assert fraction == 0.5

    def test_dataset_summarizes_complete_heating_window(self):
        base_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        readings = [
            SimpleNamespace(timestamp=base_time, temperature=20.0, device_id="sensor-1"),
            SimpleNamespace(
                timestamp=base_time + dt.timedelta(hours=1),
                temperature=20.5,
                device_id="sensor-1",
            ),
        ]
        statuses = [
            SimpleNamespace(
                ts=base_time,
                space_heating_active=None,
                operation_status=1,
                mode="1",
                direction="PUMP",
                pump_duty=1,
                device_action="HEATING",
                defrost_active=False,
                zone1_operation_status=1,
                zone2_operation_status=0,
                zone1_temp=35.0,
                zone1_target_temp=40.0,
                outdoor_temp=5.0,
            ),
            SimpleNamespace(
                ts=base_time + dt.timedelta(minutes=15),
                space_heating_active=True,
                operation_status=1,
                mode="1",
                direction="PUMP",
                pump_duty=1,
                device_action="HEATING",
                defrost_active=False,
                zone1_operation_status=1,
                zone2_operation_status=0,
                zone1_temp=35.0,
                zone1_target_temp=40.0,
                outdoor_temp=5.0,
            ),
            SimpleNamespace(
                ts=base_time + dt.timedelta(minutes=30),
                space_heating_active=True,
                operation_status=1,
                mode="1",
                direction="PUMP",
                pump_duty=1,
                device_action="HEATING",
                defrost_active=False,
                zone1_operation_status=1,
                zone2_operation_status=0,
                zone1_temp=35.0,
                zone1_target_temp=40.0,
                outdoor_temp=5.0,
            ),
            SimpleNamespace(
                ts=base_time + dt.timedelta(minutes=45),
                space_heating_active=True,
                operation_status=1,
                mode="1",
                direction="PUMP",
                pump_duty=1,
                device_action="HEATING",
                defrost_active=False,
                zone1_operation_status=1,
                zone2_operation_status=0,
                zone1_temp=35.0,
                zone1_target_temp=40.0,
                outdoor_temp=5.0,
            ),
        ]

        weather = [
            SimpleNamespace(
                ts=base_time + dt.timedelta(hours=1),
                temperature=5.0,
                wind_speed=2.0,
                irradiance=100.0,
                precipitation=0.0,
                humidity=60.0,
                cloud_cover=0.5,
            )
        ]
        dataset = build_window_dataset(
            [_IndoorObservation(row.timestamp, row.temperature) for row in readings],
            statuses,
            weather,
            (60,),
        )
        features, targets = dataset.controlled[60]
        assert len(features) == len(targets) == 1
        assert targets.tolist() == [0.5]
        assert features[0, :4].tolist() == [15.0, 20.0, 1.0, 1.0]

    def test_incomplete_window_evidence_is_rejected(self):
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        readings = [
            _IndoorObservation(start, 20.0),
            _IndoorObservation(start + dt.timedelta(hours=1), 20.5),
        ]
        statuses = [SimpleNamespace(ts=start, space_heating_active=True)]
        weather = [SimpleNamespace(ts=start + dt.timedelta(hours=1), temperature=5.0)]
        dataset = build_window_dataset(readings, statuses, weather, (60,))
        assert len(dataset.controlled[60][1]) == 0
        assert dataset.evidence["rejected_incomplete_windows"] >= 1

    def test_passive_window_rejects_unconfirmed_status_gap(self):
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)
        readings = [
            _IndoorObservation(start, 20.0),
            _IndoorObservation(start + dt.timedelta(hours=3), 20.5),
        ]
        statuses = [
            SimpleNamespace(ts=start + dt.timedelta(minutes=minutes), operation_status=0)
            for minutes in range(0, 181, 12)
            if minutes != 72
        ]
        weather = [SimpleNamespace(ts=start + dt.timedelta(hours=3), temperature=5.0)]

        dataset = build_window_dataset(readings, statuses, weather, (180,))

        assert len(dataset.controlled[180][1]) == 1
        assert len(dataset.passive[180][1]) == 0

    def test_delta_constraint_vector_exact(self):
        assert _MONOTONIC_CST == [1, 1, 1, 1, 1, -1, 1, 0, 0, -1, 0, 0, -1, 0]

    def test_passive_schema_has_no_clock_features(self):
        assert "hour_sin" not in PASSIVE_FEATURE_NAMES
        assert "hour_cos" not in PASSIVE_FEATURE_NAMES

    def test_bounded_projection_satisfies_both_constraints(self):
        projected = ComfortModel.project_delta_curve(np.random.RandomState(4).normal(size=31))
        assert projected is not None
        assert np.all(np.diff(projected) <= 1e-10)
        assert np.all(np.diff(projected) >= -(1 - COMFORT_MODEL_PROJECTION_EPSILON) - 1e-10)

    def test_projection_is_deterministic(self):
        raw = np.random.RandomState(12).normal(size=31)
        np.testing.assert_allclose(
            ComfortModel.project_delta_curve(raw), ComfortModel.project_delta_curve(raw)
        )

    def test_projection_failure_falls_back(self):
        assert ComfortModel.project_delta_curve(np.array([np.nan] * 31)) is None

    def test_passive_hourly_change_uses_configured_limit(self):
        assert ComfortModel._clamp_passive_change(3.0, 3.0, 0.25) == 0.75
        assert ComfortModel._clamp_passive_change(-3.0, 3.0, 0.25) == -0.75

    def test_candidate_builder_does_not_mutate_model_instance(self):
        model = ComfortModel()
        dataset = build_window_dataset([], [], [], (60,))
        build_candidate_bundle(dataset)
        assert model.is_trained is False

    def test_passive_model_never_trains_keeps_physics_fallback_indefinitely(self):
        dataset = build_window_dataset([], [], [], (60, 180, 360, 720))
        bundle = build_candidate_bundle(dataset)
        model = ComfortModel()
        model._model = MagicMock()
        model._passive_direct_models = bundle["passive_models"]
        model._training_notice = bundle["notice"]
        assert model.passive_forecast_readiness(60)["reason"] == "passive_model_unavailable"
        assert (
            model.training_notice
            == "passive_model_unavailable:no_zero_heating_windows;passive_forecast=physics_fallback"
        )

    def test_projected_final_temperature_is_monotone_at_and_beyond_knots(self):
        class DeltaModel:
            def predict(self, features):
                return -np.asarray(features)[:, 12] * 3.0

        model = ComfortModel()
        model._model = DeltaModel()
        model._metrics = {"delta_target": True}
        predictions = [
            model.predict_indoor_temp(0.0, 5.0, indoor_temp=issue)
            for issue in (3.0, 20.0, 22.0, 23.8, 25.3, 27.0, 38.0)
        ]
        assert all(current is not None for current in predictions)
        assert all(current < following for current, following in zip(predictions, predictions[1:]))


class TestComfortModelTrained:
    """Tests with a synthetically trained model."""

    @pytest.fixture
    def trained_model(self):
        """
        Create a model trained on synthetic data where indoor temp ≈
        0.3 * water_temp + 0.2 * outdoor_temp + 10 + noise.
        """
        model = ComfortModel()
        rng = np.random.RandomState(42)
        n = 500

        water_temps = rng.uniform(25, 55, n)
        outdoor_temps = rng.uniform(-5, 25, n)
        wind = rng.uniform(0, 10, n)
        irradiance = rng.uniform(0, 500, n)
        precipitation = rng.uniform(0, 5, n)
        humidity = rng.uniform(30, 95, n)
        cloud_cover = rng.uniform(0, 1, n)
        hours = rng.randint(0, 24, n)
        # Previous indoor temp — slightly correlated with target
        prev_indoor = 0.3 * water_temps + 0.2 * outdoor_temps + 10.0 + rng.normal(0, 2.0, n)

        X = np.column_stack(
            [
                water_temps,
                water_temps,
                np.ones(n),
                np.ones(n),
                outdoor_temps,
                wind,
                irradiance,
                precipitation,
                humidity,
                cloud_cover,
                np.sin(2.0 * np.pi * hours / 24.0),
                np.cos(2.0 * np.pi * hours / 24.0),
                prev_indoor,
                np.zeros(n),
            ]
        )
        # Simplified thermal relationship
        y = 0.3 * water_temps + 0.2 * outdoor_temps + 10.0 + rng.normal(0, 0.5, n)

        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "gbr",
                    GradientBoostingRegressor(
                        n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
                    ),
                ),
            ]
        )
        pipeline.fit(X, y)

        model._model = pipeline
        model._last_trained = dt.datetime.now(dt.timezone.utc)
        model._training_samples = n
        model._metrics = {"mae": 0.5, "r2": 0.95}
        return model

    def test_is_trained(self, trained_model):
        assert trained_model.is_trained is True

    def test_predict_indoor_temp(self, trained_model):
        # water=35, outdoor=5 → expected ≈ 0.3*35 + 0.2*5 + 10 = 21.5
        result = trained_model.predict_indoor_temp(35.0, 5.0)
        assert result is not None
        assert 18.0 < result < 25.0  # reasonable range

    def test_predict_higher_water_temp_gives_higher_indoor(self, trained_model):
        low = trained_model.predict_indoor_temp(25.0, 5.0)
        high = trained_model.predict_indoor_temp(50.0, 5.0)
        assert high > low

    def test_predict_higher_outdoor_gives_higher_indoor(self, trained_model):
        cold = trained_model.predict_indoor_temp(35.0, -5.0)
        warm = trained_model.predict_indoor_temp(35.0, 20.0)
        assert warm > cold

    def test_required_zone_temp_inverse(self, trained_model):
        # Ask for 21 °C indoor, should return a water temp
        water = trained_model.required_zone_temp(21.0, 5.0)
        assert water is not None
        assert 25.0 <= water <= 55.0

        # Verify: predicting with that water temp should give ~21 °C
        predicted = trained_model.predict_indoor_temp(water, 5.0)
        assert abs(predicted - 21.0) < 1.0

    def test_required_zone_temp_clamps_at_max(self, trained_model):
        # Request impossibly high indoor temp → should return MAX_ZONE_WATER_TEMP
        water = trained_model.required_zone_temp(40.0, -10.0)
        assert water is not None
        assert water == 65.0  # MAX_ZONE_WATER_TEMP

    def test_required_zone_temp_clamps_at_min(self, trained_model):
        # Request very low indoor temp on warm day → should return MIN_ZONE_WATER_TEMP
        water = trained_model.required_zone_temp(10.0, 25.0)
        assert water is not None
        assert water == 20.0  # MIN_ZONE_WATER_TEMP


class TestComfortModelTraining:
    @pytest.mark.asyncio
    async def test_lock_unavailable_skips_without_starting_worker(self):
        model = ComfortModel(lock_strategy=_UnavailableTrainingLock())
        model._materialize_window_dataset = AsyncMock()

        result = await model.train()

        assert result == {"status": "training_skipped", "reason": "training_lock_unavailable"}
        model._materialize_window_dataset.assert_not_awaited()
        assert model.training_notice == "training_lock_unavailable"

    @pytest.mark.asyncio
    async def test_same_process_concurrent_train_returns_training_in_progress(self):
        started = asyncio.Event()
        release = asyncio.Event()
        model = ComfortModel(lock_strategy=_TrainingLock())

        async def slow_materialize():
            started.set()
            await release.wait()
            return WindowDataset({}, {}, {})

        model._materialize_window_dataset = slow_materialize
        first = asyncio.create_task(model.train())
        await started.wait()
        second = await model.train()
        release.set()

        assert second == {"status": "training_in_progress"}
        assert (await first)["status"] == "insufficient_data"

    @staticmethod
    def _candidate() -> ComfortModelCandidate:
        return ComfortModelCandidate(
            model=SimpleNamespace(name="new"),
            direct_models={60: SimpleNamespace(name="new")},
            passive_direct_models={},
            metrics={"direct_horizons": {"60": {"mae": 0.2, "r2": 0.4, "samples": 100}}},
            samples=100,
            thermal_lag_minutes=60,
            training_notice=None,
        )

    @staticmethod
    def _dataset() -> WindowDataset:
        empty = (np.array([]), np.array([]))
        return WindowDataset({60: empty, 180: empty, 360: empty, 720: empty}, {}, {})

    @staticmethod
    def _artifact_writer(tmp_path):
        def write(_artifact, _path):
            temp = tmp_path / "candidate.tmp"
            temp.write_bytes(b"candidate")
            return temp

        def publish(temp, path):
            temp.replace(path)

        return write, publish

    @pytest.mark.asyncio
    async def test_failed_candidate_does_not_mutate_active_state(self, monkeypatch):
        model = ComfortModel()
        active = object()
        model._model = active
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", MagicMock(side_effect=ValueError())
        )

        with pytest.raises(ValueError):
            await model.train()

        assert model._model is active

    @pytest.mark.asyncio
    async def test_commit_failure_keeps_candidate_and_marks_recovered(self, tmp_path, monkeypatch):
        lease = _TrainingLease(commit_error=RuntimeError("commit"))
        model = ComfortModel(lock_strategy=_FixedTrainingLock(lease))
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        write, publish = self._artifact_writer(tmp_path)
        model._write_candidate_temp = write
        model._publish_candidate_temp = publish
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", lambda *_: self._candidate()
        )
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)

        result = await model.train()

        assert result["training_notice"] == "training_lock_finalize_recovered"
        assert model._model.name == "new"
        assert lease.invalidated is True

    @pytest.mark.asyncio
    async def test_commit_cancellation_invalidates_and_propagates(self, tmp_path, monkeypatch):
        lease = _TrainingLease(commit_error=asyncio.CancelledError())
        model = ComfortModel(lock_strategy=_FixedTrainingLock(lease))
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        write, publish = self._artifact_writer(tmp_path)
        model._write_candidate_temp = write
        model._publish_candidate_temp = publish
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", lambda *_: self._candidate()
        )
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)

        with pytest.raises(asyncio.CancelledError):
            await model.train()

        assert lease.invalidated is True
        assert lease.rolled_back is False

    @pytest.mark.asyncio
    async def test_rollback_failure_invalidates_lock(self, monkeypatch):
        lease = _TrainingLease(rollback_error=RuntimeError("rollback"))
        model = ComfortModel(lock_strategy=_FixedTrainingLock(lease))
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", MagicMock(side_effect=ValueError())
        )

        with pytest.raises(RuntimeError, match="rollback"):
            await model.train()

        assert lease.invalidated is True

    @pytest.mark.asyncio
    async def test_health_failure_discards_temp_artifact(self, tmp_path, monkeypatch):
        lease = _TrainingLease(health=False)
        model = ComfortModel(lock_strategy=_FixedTrainingLock(lease))
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        write, publish = self._artifact_writer(tmp_path)
        model._write_candidate_temp = write
        model._publish_candidate_temp = publish
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", lambda *_: self._candidate()
        )
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)

        result = await model.train()

        assert result["status"] == "training_skipped"
        assert not (tmp_path / "candidate.tmp").exists()
        assert not list(tmp_path.glob("*.pkl"))

    @pytest.mark.asyncio
    async def test_cancellation_during_temp_write_rolls_back_without_publish(
        self, tmp_path, monkeypatch
    ):
        lease = _TrainingLease()
        model = ComfortModel(lock_strategy=_FixedTrainingLock(lease))
        active = object()
        model._model = active
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        started = threading.Event()
        release = threading.Event()

        def write(_artifact, _path):
            temp = tmp_path / "candidate.tmp"
            temp.write_bytes(b"candidate")
            started.set()
            release.wait(timeout=2)
            return temp

        model._write_candidate_temp = write
        model._publish_candidate_temp = MagicMock()
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", lambda *_: self._candidate()
        )
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        task = asyncio.create_task(model.train())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert lease.rolled_back is True
        assert model._model is active
        model._publish_candidate_temp.assert_not_called()
        assert not (tmp_path / "candidate.tmp").exists()

    @pytest.mark.asyncio
    async def test_slow_candidate_build_keeps_heartbeat_advancing(self, tmp_path, monkeypatch):
        model = ComfortModel()
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        write, publish = self._artifact_writer(tmp_path)
        model._write_candidate_temp = write
        model._publish_candidate_temp = publish
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)

        def slow_candidate(*_):
            time.sleep(0.08)
            return self._candidate()

        monkeypatch.setattr("packages.ml.comfort_model.build_candidate", slow_candidate)
        ticks = 0
        running = True

        async def heartbeat():
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.01)

        heartbeat_task = asyncio.create_task(heartbeat())
        await model.train()
        running = False
        await heartbeat_task

        assert ticks >= 3

    @pytest.mark.asyncio
    async def test_compatible_gate_is_carried_and_v7_schema_mismatch_resets(
        self, tmp_path, monkeypatch
    ):
        model = ComfortModel()
        model._materialize_window_dataset = AsyncMock(return_value=self._dataset())
        write, publish = self._artifact_writer(tmp_path)
        model._write_candidate_temp = write
        model._publish_candidate_temp = publish
        monkeypatch.setattr(
            "packages.ml.comfort_model.build_candidate", lambda *_: self._candidate()
        )
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        model._metrics = {
            "forecast_quality_feature_schema": "weather_delta_v7_window_heat_controlled",
            "forecast_quality_gate_schema": "indoor_forecast_v4_delta_window_heat",
            "forecast_quality_gate": {
                "schema": "indoor_forecast_v4_delta_window_heat",
                "required_horizons": [1, 3, 6, 12, 24],
                "pass_streak": 2,
            },
        }

        await model.train()
        assert model.metrics["forecast_quality_gate"]["pass_streak"] == 2
        model._metrics["forecast_quality_feature_schema"] = "older_schema"
        await model.train()
        assert "forecast_quality_gate" not in model.metrics

    @pytest.mark.asyncio
    async def test_train_insufficient_data(self):
        model = ComfortModel()

        # Mock the dataset builder to return too few rows
        async def mock_build(*args, **kwargs):
            return np.array([]), np.array([]), 0

        model._build_dataset = mock_build
        result = await model.train()

        assert result["status"] == "insufficient_data"
        assert result["required"] == MIN_TRAINING_ROWS
        assert model.is_trained is False

    @pytest.mark.asyncio
    async def test_retraining_never_replaces_a_worse_checkpoint(self, monkeypatch):
        model = ComfortModel()
        previous_checkpoint = object()
        model._model = previous_checkpoint
        X = np.tile(np.arange(14, dtype=float), (MIN_TRAINING_ROWS, 1))
        y = np.linspace(20.0, 30.0, MIN_TRAINING_ROWS)

        async def mock_build(*args, **kwargs):
            return X, y, len(X)

        model._build_dataset = mock_build
        monkeypatch.setattr("packages.ml.comfort_model.read_mae_baseline", lambda name: 0.0)

        result = await model.train(thermal_lag_minutes=60)

        assert result["status"] == "regressed"
        assert model._model is previous_checkpoint

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("feature_schema", "required_horizons", "preserved"),
        [
            ("causal_v4_hourly_heat_and_trend", [1, 3, 6, 12, 24], False),
            ("obsolete_schema", [1, 3, 6, 12, 24], False),
            ("causal_v4_hourly_heat_and_trend", [1, 3, 6, 12], False),
        ],
    )
    async def test_retrain_preserves_gate_only_for_matching_schema_and_horizons(
        self, monkeypatch, feature_schema, required_horizons, preserved
    ):
        X, y, n_rows = _inverted_dataset()
        model = ComfortModel()
        model._metrics = {
            "forecast_quality_feature_schema": feature_schema,
            "forecast_quality_gate": {
                "schema": "indoor_forecast_v3",
                "required_horizons": required_horizons,
                "pass_streak": 1,
                "failure_streak": 0,
                "status": "observing",
            },
            "forecast_quality_gate_evaluation_id": "scorecard-1",
        }

        async def fake_build():
            return X, y, n_rows

        model._build_dataset = fake_build
        model._train_direct_forecasts = AsyncMock(return_value=({}, {}))
        model._save = lambda: None
        monkeypatch.setattr("packages.ml.comfort_model.write_mae_baseline", lambda *_: None)

        await model._train_with_current_lag()

        if preserved:
            assert model.metrics["forecast_quality_gate"]["pass_streak"] == 1
            assert model.metrics["forecast_quality_gate_evaluation_id"] == "scorecard-1"
        else:
            assert "forecast_quality_gate" not in model.metrics
            assert "forecast_quality_gate_evaluation_id" not in model.metrics


class TestThermalLag:
    def test_default_thermal_lag(self):
        model = ComfortModel()
        assert model._thermal_lag_minutes == 60

    @pytest.mark.asyncio
    async def test_custom_thermal_lag_propagated(self):
        model = ComfortModel()

        # Mock dataset builder with known data to verify lag is used
        async def mock_build(*args, **kwargs):
            return np.array([]), np.array([]), 0

        model._build_dataset = mock_build
        await model.train(thermal_lag_minutes=45)

        assert model._thermal_lag_minutes == 45

    def test_make_features_consistent_across_lags(self):
        """Feature vector doesn't depend on lag — lag only affects data pairing."""
        features_a = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        features_b = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        np.testing.assert_array_equal(features_a, features_b)


class TestCausalFeatureLookup:
    def test_uses_latest_sample_at_or_before_feature_time(self):
        """Feature joins must never select the closer future sample."""
        times = np.array([0.0, 600.0, 1_200.0])

        assert ComfortModel._latest_index_at_or_before(times, 900.0) == 1
        assert ComfortModel._latest_index_at_or_before(times, 600.0) == 1
        assert ComfortModel._latest_index_at_or_before(times, -1.0) is None


def _inverted_dataset(n=300, seed=0):
    """Synthetic data where MORE water heat correlates with LOWER indoor temp.

    This reproduces the failure mode reported from the field (the comfort model
    trained on a mis-selected sensor) where a naive regressor learns that
    heating *lowers* indoor temperature.
    """
    rng = np.random.RandomState(seed)
    water = rng.uniform(25, 55, n)
    outdoor = rng.uniform(-5, 20, n)
    wind = rng.uniform(0, 8, n)
    irradiance = rng.uniform(0, 400, n)
    precipitation = rng.uniform(0, 5, n)
    humidity = rng.uniform(30, 95, n)
    cloud_cover = rng.uniform(0, 1, n)
    hours = rng.randint(0, 24, n)
    prev_indoor = rng.uniform(19, 27, n)
    X = np.column_stack(
        [
            water,
            water,
            np.ones(n),
            np.ones(n),
            outdoor,
            wind,
            irradiance,
            precipitation,
            humidity,
            cloud_cover,
            np.sin(2.0 * np.pi * hours / 24.0),
            np.cos(2.0 * np.pi * hours / 24.0),
            prev_indoor,
            np.zeros(n),
        ]
    )
    # Inverted relationship: higher water temp -> lower indoor temp.
    y = 30.0 - 0.2 * water + 0.05 * outdoor + rng.normal(0, 0.3, n)
    return X, y, n


class TestComfortModelMonotonicity:
    """The trained model must stay physically sensible even on bad data."""

    @pytest.mark.asyncio
    async def test_higher_water_never_lowers_indoor_on_inverted_data(self):
        X, y, n = _inverted_dataset()
        model = ComfortModel()

        async def fake_build():
            return X, y, n

        model._build_dataset = fake_build
        model._save = lambda: None

        result = await model.train(thermal_lag_minutes=30)
        assert result["status"] == "trained"

        # Despite the inverted training signal, the monotonic constraint must
        # guarantee predicted indoor is non-decreasing in water supply temp.
        prev = None
        for water in range(25, 56, 5):
            pred = model.predict_indoor_temp(float(water), 5.0, indoor_temp=22.0)
            if prev is not None:
                assert pred >= prev - 1e-6
            prev = pred

    @pytest.mark.asyncio
    async def test_higher_current_indoor_never_increases_delta_prediction(self):
        X, y, n = _inverted_dataset(seed=1)
        model = ComfortModel()

        async def fake_build():
            return X, y, n

        model._build_dataset = fake_build
        model._save = lambda: None
        await model.train(thermal_lag_minutes=30)

        low = model.predict_indoor_temp(40.0, 5.0, indoor_temp=20.0)
        high = model.predict_indoor_temp(40.0, 5.0, indoor_temp=26.0)
        assert high is not None and low is not None
        assert high - 26.0 <= low - 20.0 + 1e-6

    @pytest.mark.asyncio
    async def test_metrics_report_out_of_sample_validation(self):
        X, y, n = _inverted_dataset(seed=2)
        model = ComfortModel()

        async def fake_build():
            return X, y, n

        model._build_dataset = fake_build
        model._save = lambda: None
        result = await model.train(thermal_lag_minutes=30)
        assert result["validated"] is True
        assert "mae" in result and "r2" in result

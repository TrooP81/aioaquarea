"""Tests for the comfort model (indoor air temperature prediction)."""

import datetime as dt
from unittest.mock import MagicMock

import numpy as np
import pytest

from packages.core.heat_curve import HeatCurveConfig, effective_zone_target_temperature
from packages.ml.comfort_model import ComfortModel, MIN_TRAINING_ROWS


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


class TestPassiveDirectForecastReadiness:
    def test_direct_passive_forecast_can_be_ready_without_heating_control(self):
        model = ComfortModel()
        model._model = MagicMock()
        model._direct_models = {60: MagicMock()}
        model._metrics = {
            "direct_horizons": {"60": {"status": "trained", "mae": 0.6}},
            # No active heating evidence: full heating control remains blocked.
            "active_heating_rows": 0,
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
        model._direct_models = {60: MagicMock()}
        model._metrics = {"direct_horizons": {"60": {"status": "trained", "mae": 1.2}}}

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
            "baseline_mae": 0.8,
        }

        assert model.is_ready_for_control is True
        assert model.control_margin_c == 0.45


class TestComfortModelFeatures:
    def test_panasonic_sentinel_target_uses_weather_compensated_curve(self):
        curve = HeatCurveConfig()

        assert effective_zone_target_temperature(-5.0, 5.0, config=curve) == 47.0
        assert effective_zone_target_temperature(-5.0, 20.0, config=curve) == 20.0
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
    async def test_higher_current_indoor_never_lowers_prediction(self):
        X, y, n = _inverted_dataset(seed=1)
        model = ComfortModel()

        async def fake_build():
            return X, y, n

        model._build_dataset = fake_build
        model._save = lambda: None
        await model.train(thermal_lag_minutes=30)

        low = model.predict_indoor_temp(40.0, 5.0, indoor_temp=20.0)
        high = model.predict_indoor_temp(40.0, 5.0, indoor_temp=26.0)
        assert high >= low - 1e-6

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

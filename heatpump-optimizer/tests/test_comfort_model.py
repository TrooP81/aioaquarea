"""Tests for the comfort model (indoor air temperature prediction)."""

import datetime as dt

import numpy as np
import pytest

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


class TestComfortModelFeatures:
    def test_make_features_shape(self):
        features = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        assert features.shape == (7,)

    def test_make_features_indoor_temp(self):
        # When indoor_temp is provided, it should be used as the 7th feature
        features = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12, indoor_temp=21.0)
        assert features[6] == 21.0

    def test_make_features_indoor_temp_fallback(self):
        # When indoor_temp is None, falls back to outdoor_temp
        features = ComfortModel._make_features(35.0, 5.0, 3.0, 100.0, 12)
        assert features[6] == 5.0  # outdoor_temp

    def test_make_features_cyclical_hour(self):
        features_0 = ComfortModel._make_features(35.0, 5.0, 3.0, 0.0, 0)
        features_12 = ComfortModel._make_features(35.0, 5.0, 3.0, 0.0, 12)
        # Hour 0: sin=0, cos=1
        assert abs(features_0[4]) < 0.01  # sin(0) ≈ 0
        assert abs(features_0[5] - 1.0) < 0.01  # cos(0) ≈ 1
        # Hour 12: sin=0, cos=-1
        assert abs(features_12[4]) < 0.01  # sin(π) ≈ 0
        assert abs(features_12[5] + 1.0) < 0.01  # cos(π) ≈ -1


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
        hours = rng.randint(0, 24, n)
        # Previous indoor temp — slightly correlated with target
        prev_indoor = 0.3 * water_temps + 0.2 * outdoor_temps + 10.0 + rng.normal(0, 2.0, n)

        X = np.column_stack([
            water_temps,
            outdoor_temps,
            wind,
            irradiance,
            np.sin(2.0 * np.pi * hours / 24.0),
            np.cos(2.0 * np.pi * hours / 24.0),
            prev_indoor,
        ])
        # Simplified thermal relationship
        y = 0.3 * water_temps + 0.2 * outdoor_temps + 10.0 + rng.normal(0, 0.5, n)

        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("gbr", GradientBoostingRegressor(
                n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
            )),
        ])
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


class TestThermalLag:
    def test_default_thermal_lag(self):
        model = ComfortModel()
        assert model._thermal_lag_minutes == 30

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
    hours = rng.randint(0, 24, n)
    prev_indoor = rng.uniform(19, 27, n)
    X = np.column_stack([
        water,
        outdoor,
        wind,
        irradiance,
        np.sin(2.0 * np.pi * hours / 24.0),
        np.cos(2.0 * np.pi * hours / 24.0),
        prev_indoor,
    ])
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

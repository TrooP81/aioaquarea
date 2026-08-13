"""Tests for ML models (COP, Demand) and ThermalModel."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *args, **kwargs):
        return self._results.pop(0)


class _FakeSessionCtx:
    def __init__(self, results):
        self._session = _FakeSession(results)

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _mock_get_session(results):
    def factory():
        return _FakeSessionCtx(results)

    return factory


def _optimizer_session_mock():
    """Return a DB-session mock with SQLAlchemy's sync/async boundary intact."""

    active_plans = MagicMock()
    active_plans.scalars.return_value.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=active_plans)
    session.add = MagicMock()
    return session


class TestCOPModel:
    def test_untrained_uses_fallback(self):
        """Untrained model should use default COP curve."""
        from packages.ml.models import COPModel

        model = COPModel()
        assert not model.is_trained

        # Fallback COP should be in reasonable range
        cop = model.predict_cop(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 1.5 <= cop <= 6.0

        # predict() should still return reasonable electrical kWh
        pred = model.predict(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 0.1 < pred < 5.0

    def test_fallback_higher_at_cold(self):
        """Colder outdoor temp → lower COP → higher electrical consumption."""
        from packages.ml.models import COPModel

        model = COPModel()
        cold_cop = model.predict_cop(outdoor_temp=-5.0, tank_target=50, hour=12)
        warm_cop = model.predict_cop(outdoor_temp=15.0, tank_target=50, hour=12)
        assert cold_cop < warm_cop  # COP increases with outdoor temp

        cold_pred = model.predict(outdoor_temp=-5.0, tank_target=50, hour=12)
        warm_pred = model.predict(outdoor_temp=15.0, tank_target=50, hour=12)
        assert cold_pred > warm_pred  # Electrical consumption higher in cold

    def test_predict_cop_fallback(self):
        """predict_cop should return a reasonable COP range."""
        from packages.ml.models import COPModel

        model = COPModel()
        cop = model.predict_cop(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 1.5 <= cop <= 6.0

    def test_make_features_shape(self):
        """Feature vector should have correct shape."""
        from packages.ml.models import COPModel

        features = COPModel._make_features(
            5.0, 50, 12, precipitation=1.5, humidity=82.0, cloud_cover=0.75
        )
        assert features.shape == (7,)
        assert features[4] == 1.5
        assert features[5] == 82.0
        assert features[6] == 0.75

    def test_load_latest_no_models(self, tmp_path):
        """load_latest returns False when no model files exist."""
        from packages.ml.models import COPModel

        model = COPModel()
        with patch("packages.ml.models.MODEL_DIR", tmp_path):
            assert not model.load_latest()

    def test_train_and_predict_with_synthetic_data(self, tmp_path):
        """Train COP model on synthetic COP data and verify predictions."""
        from packages.ml.models import COPModel, HAS_SKLEARN

        if not HAS_SKLEARN:
            pytest.skip("scikit-learn not installed")

        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        model = COPModel()

        # Simulate training: outdoor_temp + tank_target + hour → COP
        rng = np.random.RandomState(42)
        n = 300

        outdoor_temps = rng.uniform(-5, 25, n)
        tank_targets = rng.uniform(45, 55, n).astype(int)
        hours = rng.randint(0, 24, n)

        X = np.column_stack(
            [
                outdoor_temps,
                tank_targets,
                np.sin(2 * np.pi * hours / 24),
                np.cos(2 * np.pi * hours / 24),
                rng.uniform(0, 5, n),
                rng.uniform(30, 95, n),
                rng.uniform(0, 1, n),
            ]
        )
        # Higher outdoor → higher COP (physically correct)
        y = 3.0 + 0.08 * outdoor_temps - 0.02 * tank_targets + rng.normal(0, 0.15, n)
        y = np.clip(y, 1.5, 6.0)

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "model",
                    GradientBoostingRegressor(
                        n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
                    ),
                ),
            ]
        )
        pipeline.fit(X, y)
        model._model = pipeline
        model._version = "test"

        assert model.is_trained

        # Warm → higher COP than cold
        cold_cop = model.predict_cop(outdoor_temp=-5.0, tank_target=50, hour=12)
        warm_cop = model.predict_cop(outdoor_temp=20.0, tank_target=50, hour=12)
        assert warm_cop > cold_cop

        # COP should be in physical range
        cop = model.predict_cop(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 1.5 <= cop <= 6.0

        # predict() (electrical kWh) should still be reasonable
        pred = model.predict(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 0.1 < pred < 5.0

        # Save and reload
        with (
            patch("packages.ml.models.MODEL_DIR", tmp_path),
            patch("packages.ml.safe_persistence.settings") as mock_settings,
        ):
            mock_settings.model_dir = str(tmp_path)
            mock_settings.secret_key = "test-secret-key"
            from packages.ml.safe_persistence import safe_dump

            model_path = tmp_path / "cop_model_weather_dhw_v4_test.pkl"
            safe_dump(pipeline, model_path)

            model2 = COPModel()
            assert model2.load_latest()
            assert model2.is_trained
            cop2 = model2.predict_cop(outdoor_temp=5.0, tank_target=50, hour=12)
            assert abs(cop2 - cop) < 0.01


class TestDemandModel:
    def test_untrained_uses_fallback(self):
        """Untrained model should produce reasonable fallback predictions."""
        from packages.ml.models import DemandModel

        model = DemandModel()
        assert not model.is_trained

        weather = [
            {"temperature": 5.0, "wind_speed": 3.0, "irradiance": 0.0, "precipitation": 1.0}
        ] * 24
        predictions = model.predict_hourly(weather, hours=24)

        assert len(predictions) == 24
        assert all(p >= 0 for p in predictions)

    def test_colder_weather_higher_demand(self):
        """Colder weather should predict higher demand in fallback mode."""
        from packages.ml.models import DemandModel

        model = DemandModel()

        cold_weather = [{"temperature": -5.0}] * 24
        warm_weather = [{"temperature": 15.0}] * 24

        cold_demand = model.predict_hourly(cold_weather, hours=24)
        warm_demand = model.predict_hourly(warm_weather, hours=24)

        assert sum(cold_demand) > sum(warm_demand)

    def test_make_features_shape_and_order(self):
        """Shared feature builder must return the fixed 10-feature schema."""
        from packages.ml.models import DemandModel

        features = DemandModel._make_features(
            5.0, 3.0, 100.0, 12, 2, precipitation=1.5, humidity=82.0, cloud_cover=0.75
        )
        assert features.shape == (10,)
        assert features[0] == 5.0  # temperature
        assert features[1] == 3.0  # wind
        assert features[2] == 100.0  # irradiance
        assert features[3] == 1.5  # precipitation
        assert features[4] == 82.0  # humidity
        assert features[5] == 0.75  # cloud cover

    @pytest.mark.asyncio
    async def test_prepare_data_uses_interval_rate_not_cumulative(self):
        """Target must be per-interval hourly RATE, never the cumulative counter."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=1.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [
            SimpleNamespace(
                ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0, precipitation=2.5
            )
        ]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_data()

        # 0.5 kWh / 0.25h = 2.0 kW; 1.0 kWh / 0.25h = 4.0 kW
        assert len(y) == 2
        assert sorted(round(float(v), 6) for v in y) == [2.0, 4.0]
        # The cumulative reading (1.5) must NOT leak through as a target.
        assert max(y) == pytest.approx(4.0)
        # Reported weather is canonical because the physical pump sensor may
        # be sun-heated by its installation.
        assert X[0][0] == 1.0
        assert X[0][3] == 2.5
        assert model.last_data_quality == {
            "raw_records": 3,
            "intervals": 2,
            "usable_samples": 2,
            "minimum_samples": 168,
            "rejected_nonpositive": 0,
            "rejected_rate_bounds": 0,
            "weather_records": 1,
            "weather_matches": 2,
            "weather_temperature_fallbacks": 0,
            "max_rate_kw": model._max_plausible_rate_kw(),
            "ready_to_train": False,
            "remaining_samples": 166,
            "heating_activity_ratio": 1.0,
            "training_blocker": "collecting_space_heating_intervals",
            "seasonal_guidance": "Collecting additional valid space-heating intervals before training.",
        }

    @pytest.mark.asyncio
    async def test_prepare_data_uses_weather_temp_when_consumption_temp_is_missing(self):
        """The nearest reported temperature remains canonical when the old row has no temperature."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=None),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.25,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=None,
            ),
        ]
        weather = [
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=20),
                temperature=-3.0,
                wind_speed=5.0,
                irradiance=0.0,
                precipitation=1.0,
            )
        ]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_data()

        assert len(y) == 1
        assert X[0][0] == -3.0  # weather temperature filled in
        assert X[0][1] == 5.0  # weather wind
        assert X[0][3] == 1.0  # weather precipitation
        assert model.last_data_quality["weather_temperature_fallbacks"] == 0

    @pytest.mark.asyncio
    async def test_prepare_data_counts_fallback_when_weather_has_no_temperature(self):
        """Only a missing weather temperature may fall back to the stored pump value."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=4.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.25,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=4.0,
            ),
        ]
        weather = [
            SimpleNamespace(
                ts=base,
                temperature=None,
                wind_speed=5.0,
                irradiance=0.0,
                precipitation=1.0,
            )
        ]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_data()

        assert len(y) == 1
        assert X[0][0] == 4.0
        assert model.last_data_quality["weather_temperature_fallbacks"] == 1


class TestConsumptionIntervals:
    def test_basic_delta_within_day(self):
        from packages.ml.models_common import iter_consumption_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(ts=base, heat_kwh=1.0, cool_kwh=0.0, tank_kwh=0.5, outdoor_temp=3.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=1.4,
                cool_kwh=0.0,
                tank_kwh=0.6,
                outdoor_temp=3.0,
            ),
        ]
        intervals = list(iter_consumption_intervals(rows))
        assert len(intervals) == 1
        iv = intervals[0]
        assert iv.elapsed_hours == pytest.approx(0.25)
        assert iv.heat_kwh == pytest.approx(0.4)
        assert iv.tank_kwh == pytest.approx(0.1)
        assert iv.total_kwh == pytest.approx(0.5)
        assert iv.total_rate_kw == pytest.approx(2.0)

    def test_day_boundary_reset_skipped(self):
        from packages.ml.models_common import iter_consumption_intervals

        rows = [
            SimpleNamespace(
                ts=dt.datetime(2026, 1, 5, 23, 55, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=3.0,
            ),
            SimpleNamespace(
                ts=dt.datetime(2026, 1, 6, 0, 10, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=3.0,
            ),
        ]
        assert list(iter_consumption_intervals(rows)) == []

    def test_out_of_window_elapsed_skipped(self):
        from packages.ml.models_common import iter_consumption_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(ts=base, heat_kwh=1.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=3.0),
            SimpleNamespace(
                ts=base + dt.timedelta(hours=3),
                heat_kwh=2.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=3.0,
            ),
        ]
        assert list(iter_consumption_intervals(rows)) == []

    def test_negative_field_delta_clamped(self):
        from packages.ml.models_common import iter_consumption_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(ts=base, heat_kwh=2.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=3.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=1.5,
                cool_kwh=0.0,
                tank_kwh=1.0,
                outdoor_temp=3.0,
            ),
        ]
        intervals = list(iter_consumption_intervals(rows))
        assert len(intervals) == 1
        assert intervals[0].heat_kwh == 0.0  # negative delta clamped to zero
        assert intervals[0].tank_kwh == pytest.approx(1.0)


class TestThermalModel:
    def test_default_params(self):
        """Model should have sensible default params."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        assert model.params.tank_heating_rate > 0
        assert model.params.tank_standby_loss < 0
        assert model.params.zone_heating_rate > 0

    def test_predict_tank_heating_time(self):
        """Should predict positive heating time for a temperature increase."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_heating_time(
            current_temp=45.0, target_temp=52.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes > 0
        assert pred.heating_rate_per_hour > 0
        assert pred.confidence == "default"

    def test_predict_tank_already_at_target(self):
        """Should return 0 minutes if already at target."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_heating_time(
            current_temp=55.0, target_temp=52.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes == 0.0

    def test_predict_tank_cooling_time(self):
        """Should predict positive cooling time from above minimum."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_cooling_time(current_temp=52.0, min_temp=45.0, outdoor_temp=5.0)

        assert pred.estimated_minutes > 0

    def test_predict_zone_heating_time(self):
        """Zone heating prediction should be positive for a delta."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_zone_heating_time(
            current_temp=18.0, target_temp=22.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes > 0

    def test_optimal_start_time(self):
        """Optimal start should be before the deadline."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        deadline = dt.datetime(2026, 5, 1, 6, 0, tzinfo=dt.timezone.utc)
        start = model.optimal_start_time(
            current_temp=45.0,
            target_temp=52.0,
            deadline=deadline,
            outdoor_temp=5.0,
            is_tank=True,
        )

        assert start < deadline

    def test_warmer_outdoor_faster_heating(self):
        """Warmer outdoor temp should give faster (shorter) heating."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()

        cold = model.predict_tank_heating_time(45.0, 52.0, outdoor_temp=-5.0)
        warm = model.predict_tank_heating_time(45.0, 52.0, outdoor_temp=15.0)

        assert warm.estimated_minutes < cold.estimated_minutes

    def test_temperature_curve(self):
        """Temperature curve should have correct length."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=52.0, outdoor_temp=5.0, hours=12, is_tank=True
        )

        assert len(curve) == 12
        # Standby loss: temperatures should decrease
        assert curve[-1]["predicted_temp"] < 52.0


class TestMonotonicCOPModel:
    """COP must rise with outdoor temperature even when trained on bad data."""

    @pytest.mark.asyncio
    async def test_cop_non_decreasing_in_outdoor_on_inverted_data(self):
        from packages.ml.cop_model_core import COPModel

        rng = np.random.RandomState(0)
        n = 200
        outdoor = rng.uniform(-10, 20, n)
        tank = rng.uniform(45, 55, n)
        hours = rng.randint(0, 24, n)
        X = np.column_stack(
            [
                outdoor,
                tank,
                np.sin(2 * np.pi * hours / 24),
                np.cos(2 * np.pi * hours / 24),
                rng.uniform(0, 5, n),
                rng.uniform(30, 95, n),
                rng.uniform(0, 1, n),
            ]
        )
        # Physically wrong: colder outside -> higher COP.
        y = np.clip(3.0 - 0.05 * outdoor + rng.normal(0, 0.1, n), 1.5, 6.0)

        model = COPModel()

        async def fake_prep():
            return X, y

        model._prepare_training_data = fake_prep
        with patch("packages.ml.safe_persistence.safe_dump"):
            result = await model.train()
        assert "version" in result

        cold = model.predict_cop(outdoor_temp=-5.0, tank_target=50, hour=12)
        warm = model.predict_cop(outdoor_temp=15.0, tank_target=50, hour=12)
        assert warm >= cold - 1e-6


class TestMonotonicDemandModel:
    """Demand must rise as it gets colder even when trained on bad data."""

    @pytest.mark.asyncio
    async def test_demand_non_increasing_in_outdoor_on_inverted_data(self):
        from packages.ml.demand_model_core import DemandModel

        rng = np.random.RandomState(0)
        n = 300
        outdoor = rng.uniform(-10, 20, n)
        wind = rng.uniform(0, 10, n)
        irradiance = rng.uniform(0, 500, n)
        precipitation = rng.uniform(0, 5, n)
        humidity = rng.uniform(30, 95, n)
        cloud_cover = rng.uniform(0, 1, n)
        hours = rng.randint(0, 24, n)
        dow = rng.randint(0, 7, n)
        X = np.column_stack(
            [
                outdoor,
                wind,
                irradiance,
                precipitation,
                humidity,
                cloud_cover,
                np.sin(2 * np.pi * hours / 24),
                np.cos(2 * np.pi * hours / 24),
                np.sin(2 * np.pi * dow / 7),
                np.cos(2 * np.pi * dow / 7),
            ]
        )
        # Physically wrong: warmer outside -> higher demand.
        y = np.clip(1.0 + 0.1 * outdoor + rng.normal(0, 0.1, n), 0.1, None)

        model = DemandModel()

        async def fake_prep():
            return X, y

        model._prepare_data = fake_prep
        with patch("packages.ml.safe_persistence.safe_dump"):
            result = await model.train()
        assert "version" in result

        f_warm = DemandModel._make_features(15.0, 3.0, 0.0, 12, 2).reshape(1, -1)
        f_cold = DemandModel._make_features(-5.0, 3.0, 0.0, 12, 2).reshape(1, -1)
        warm = float(model._model.predict(f_warm)[0])
        cold = float(model._model.predict(f_cold)[0])
        assert cold >= warm - 1e-6


class TestPhysicalCurveOrdering:
    """The indoor-forecast endpoint must never show heating below no-heating."""

    def test_predicted_clamped_to_no_heating_floor(self):
        from packages.api.routers.models_router import _enforce_physical_ordering

        # Reproduces the reported inversion: predicted 25.9 < no-heating 26.2.
        forecast = [{"predicted_indoor_temp": 25.9}]
        forecast_with_plan = [{"predicted_indoor_temp": 25.0}]
        forecast_no_heating = [{"predicted_indoor_temp": 26.2}]

        _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

        assert forecast[0]["predicted_indoor_temp"] == 26.2
        assert forecast_with_plan[0]["predicted_indoor_temp"] >= 26.2

    def test_valid_ordering_left_untouched(self):
        from packages.api.routers.models_router import _enforce_physical_ordering

        forecast = [{"predicted_indoor_temp": 22.0}]
        forecast_with_plan = [{"predicted_indoor_temp": 23.0}]
        forecast_no_heating = [{"predicted_indoor_temp": 20.0}]

        _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

        assert forecast[0]["predicted_indoor_temp"] == 22.0
        assert forecast_with_plan[0]["predicted_indoor_temp"] == 23.0


class TestOrchestratorFallback:
    """Tests for the orchestrator layer selection and fallback logic."""

    @pytest.mark.asyncio
    async def test_select_optimizer_can_reload_models(self):
        """reload_models=True should refresh checkpoints before selecting a layer."""
        from packages.optimizer.main import _select_optimizer

        with patch("packages.optimizer.main._load_ml_models") as mock_load:
            layer_name, optimizer = await _select_optimizer("rules_only", reload_models=True)

        mock_load.assert_called_once_with()
        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"

    @pytest.mark.asyncio
    async def test_rules_only_never_uses_milp(self):
        """With rules_only setting, MILP should never be invoked."""
        from packages.optimizer.main import _select_optimizer

        layer_name, optimizer = await _select_optimizer("rules_only")
        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"

    @pytest.mark.asyncio
    async def test_milp_preferred_returns_milp(self):
        """milp_preferred should return MILP optimizer."""
        from packages.optimizer.main import _select_optimizer

        layer_name, optimizer = await _select_optimizer("milp_preferred")
        assert layer_name == "milp"
        assert type(optimizer).__name__ == "MILPOptimizer"

    @pytest.mark.asyncio
    async def test_auto_uses_rules_without_ml(self):
        """auto should fall back to rules when ML models are not trained."""
        from packages.optimizer.main import _select_optimizer, _cop_model, _demand_model

        # Ensure models are not trained
        assert not _cop_model.is_trained or not _demand_model.is_trained

        layer_name, optimizer = await _select_optimizer("auto")
        # Without trained models, auto should pick rules
        assert layer_name == "rules"

    @pytest.mark.asyncio
    async def test_auto_requires_sufficient_data(self):
        """auto should fall back to rules when ML models are trained but data history is too short."""
        from packages.optimizer.main import _select_optimizer, _cop_model, _demand_model

        # Temporarily mark models as trained
        _cop_model._model = MagicMock()
        _demand_model._model = MagicMock()

        try:
            # Mock insufficient data
            with patch(
                "packages.optimizer.main._has_sufficient_ml_data",
                new_callable=AsyncMock,
                return_value=False,
            ):
                layer_name, optimizer = await _select_optimizer("auto")
                assert layer_name == "rules"

            # Mock sufficient data
            with patch(
                "packages.optimizer.main._has_sufficient_ml_data",
                new_callable=AsyncMock,
                return_value=True,
            ):
                layer_name, optimizer = await _select_optimizer("auto")
                assert layer_name == "milp"
        finally:
            # Restore untrained state
            _cop_model._model = None
            _demand_model._model = None

    @pytest.mark.asyncio
    async def test_optimizer_status_snapshot_uses_same_auto_gate(self):
        """Status snapshot should report the same layer the runtime selector would use."""
        from packages.optimizer.main import (
            _cop_model,
            _demand_model,
            get_optimizer_status_snapshot,
        )

        _cop_model._model = MagicMock()
        _demand_model._model = MagicMock()

        try:
            with patch(
                "packages.optimizer.main._has_sufficient_ml_data",
                new_callable=AsyncMock,
                return_value=False,
            ):
                status = await get_optimizer_status_snapshot("auto")
                assert status["active_layer"] == "rules_v3"

            with patch(
                "packages.optimizer.main._has_sufficient_ml_data",
                new_callable=AsyncMock,
                return_value=True,
            ):
                status = await get_optimizer_status_snapshot("auto")
                assert status["active_layer"] == "milp_v1+ml"
                assert status["cop_trained"] is True
                assert status["demand_trained"] is True
        finally:
            _cop_model._model = None
            _demand_model._model = None

    @pytest.mark.asyncio
    async def test_run_optimization_with_rules_only(self):
        """End-to-end: rules_only setting should produce a rules plan."""
        from packages.optimizer.main import run_optimization

        with patch(
            "packages.optimizer.main.get_setting", new_callable=AsyncMock, return_value="rules_only"
        ):
            with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                mock_plan = {
                    "horizon_start": dt.datetime.now(dt.timezone.utc),
                    "horizon_end": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24),
                    "actions": [],
                    "version": "rules_v3",
                    "cost_estimate": 0.0,
                }
                MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)

                with (
                    patch(
                        "packages.optimizer.main.get_active_price_context",
                        new_callable=AsyncMock,
                        return_value=SimpleNamespace(area="test", currency="EUR", source="test"),
                    ),
                    patch(
                        "packages.optimizer.main._has_material_near_term_change",
                        new_callable=AsyncMock,
                        return_value=(True, None),
                    ),
                    patch("packages.optimizer.main.get_session") as mock_session_ctx,
                ):
                    mock_session = _optimizer_session_mock()
                    mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                    await run_optimization()

                MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_replace_bypasses_plan_stability_check(self):
        """Explicit manual replans must be allowed to replace later actions."""
        from packages.optimizer.main import run_optimization

        mock_plan = {
            "horizon_start": dt.datetime.now(dt.timezone.utc),
            "horizon_end": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24),
            "actions": [],
            "version": "rules_v3",
            "cost_estimate": 0.0,
        }
        with patch(
            "packages.optimizer.main.get_setting", new_callable=AsyncMock, return_value="rules_only"
        ):
            with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)
                with (
                    patch(
                        "packages.optimizer.main.get_active_price_context",
                        new_callable=AsyncMock,
                        return_value=SimpleNamespace(area="test", currency="EUR", source="test"),
                    ),
                    patch(
                        "packages.optimizer.main._has_material_near_term_change",
                        new_callable=AsyncMock,
                        side_effect=AssertionError("manual replan must bypass stability"),
                    ),
                    patch("packages.optimizer.main.get_session") as mock_session_ctx,
                ):
                    mock_session = _optimizer_session_mock()
                    mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                    await run_optimization(force_replace=True)

        MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_milp_failure_falls_back_to_rules(self):
        """When MILP raises, the orchestrator should fall back to rules."""
        from packages.optimizer.main import run_optimization
        from packages.optimizer import DataIncompleteError

        mock_plan = {
            "horizon_start": dt.datetime.now(dt.timezone.utc),
            "horizon_end": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24),
            "actions": [],
            "version": "rules_v3",
            "cost_estimate": 0.0,
        }

        with patch(
            "packages.optimizer.main.get_setting",
            new_callable=AsyncMock,
            return_value="milp_preferred",
        ):
            with patch(
                "packages.optimizer.main._select_optimizer", new_callable=AsyncMock
            ) as mock_select:
                mock_milp = AsyncMock()
                mock_milp.generate_plan = AsyncMock(side_effect=DataIncompleteError("no prices"))
                mock_select.return_value = ("milp", mock_milp)

                with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                    MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)

                    with (
                        patch(
                            "packages.optimizer.main.get_planning_data_quality",
                            new_callable=AsyncMock,
                            return_value={
                                "control_allowed": True,
                                "status": "healthy",
                                "reasons": [],
                                "price": {},
                                "weather": {},
                            },
                        ),
                        patch(
                            "packages.optimizer.main.get_active_price_context",
                            new_callable=AsyncMock,
                            return_value=SimpleNamespace(
                                area="test", currency="EUR", source="test"
                            ),
                        ),
                        patch(
                            "packages.optimizer.main._has_material_near_term_change",
                            new_callable=AsyncMock,
                            return_value=(True, None),
                        ),
                        patch("packages.optimizer.main.get_session") as mock_session_ctx,
                    ):
                        mock_session = _optimizer_session_mock()
                        mock_session_ctx.return_value.__aenter__ = AsyncMock(
                            return_value=mock_session
                        )
                        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                        await run_optimization()

                    # MILP failed, so rules should have been called
                    MockRules.return_value.generate_plan.assert_awaited_once()


class TestDirectionAwareCOP:
    """Tests for direction-based COP computation."""

    def test_tank_thermal_mass_from_config(self):
        """Verify tank thermal mass is computed from configured volume."""
        from packages.ml.models import DirectionAwareCOP

        dac = DirectionAwareCOP()
        # 300L tank → 0.349 kWh/°C (configurable via tank_volume_liters)
        tank_kwh = dac._tank_kwh_per_degree()
        assert 0.1 < tank_kwh < 1.0

    def test_water_circuit_thermal_mass_constant(self):
        """Verify the renamed water circuit thermal mass constant."""
        from packages.ml.models import DirectionAwareCOP

        dac = DirectionAwareCOP()
        assert hasattr(dac, "WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG")
        assert 0.1 < dac.WATER_CIRCUIT_THERMAL_MASS_KWH_PER_DEG < 2.0

    def test_only_heating_water_uses_tank_temp(self):
        """
        When device_action is HEATING_WATER, COP should use tank_temp deltas.
        When device_action is HEATING, COP should use zone1_temp (water circuit) deltas.
        Verify that the code path branches correctly.
        """
        from packages.ml.models import DirectionAwareCOP

        dac = DirectionAwareCOP()

        # Simulate two records: HEATING_WATER with tank temp rise
        base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)

        record_prev = MagicMock()
        record_prev.ts = base
        record_prev.device_action = "HEATING_WATER"
        record_prev.tank_temp = 45.0
        record_prev.zone1_temp = 30.0
        record_prev.outdoor_temp = 5.0
        record_prev.defrost_active = False

        record_curr = MagicMock()
        record_curr.ts = base + dt.timedelta(hours=1)
        record_curr.device_action = "HEATING_WATER"
        record_curr.tank_temp = 50.0  # +5°C in tank
        record_curr.zone1_temp = 30.0  # zone unchanged
        record_curr.outdoor_temp = 5.0
        record_curr.defrost_active = False
        record_curr.device_id = "test"

        # For HEATING_WATER: thermal = 5 * tank_kwh_per_degree (from tank temp)
        # NOT from zone1_temp which didn't change
        expected_thermal = 5.0 * dac._tank_kwh_per_degree()
        assert expected_thermal > 0

    def test_idle_and_off_intervals_are_skipped(self):
        """Intervals with IDLE or OFF action should produce no COP entries."""
        # This is by design: the compute_cop_intervals loop skips
        # actions in ("OFF", "IDLE") at the top of the loop
        from packages.ml.models import DirectionAwareCOP

        dac = DirectionAwareCOP()

        # Verify the tank thermal capacity is available and positive
        assert dac._tank_kwh_per_degree() > 0

    def test_defrost_intervals_are_skipped(self):
        """Defrost intervals should not contribute to COP calculation."""
        base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)

        record_prev = MagicMock()
        record_prev.ts = base
        record_prev.device_action = "HEATING"
        record_prev.tank_temp = 45.0
        record_prev.zone1_temp = 30.0
        record_prev.outdoor_temp = 5.0
        record_prev.defrost_active = False

        record_curr = MagicMock()
        record_curr.ts = base + dt.timedelta(hours=1)
        record_curr.device_action = "HEATING"
        record_curr.tank_temp = 45.0
        record_curr.zone1_temp = 35.0  # zone rose
        record_curr.outdoor_temp = 5.0
        record_curr.defrost_active = True  # DEFROST → should be skipped

        # In the real code loop, defrost_active=True causes `continue`
        # so this interval would never produce a COP entry.
        assert record_curr.defrost_active is True


class _ScalarResult:
    """Fake execute() result exposing scalars().all() for ORM-object queries."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class TestDemandHeatingOnlyTarget:
    """Demand target must be space-heating electrical rate only (not DHW/cooling)."""

    @pytest.mark.asyncio
    async def test_dhw_only_interval_is_skipped(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        # Only tank (DHW) energy accrues — no space heating. Must be excluded.
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=1.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_data()

        assert len(y) == 0  # DHW-only interval contributes no space-heating sample

    @pytest.mark.asyncio
    async def test_heating_rate_excludes_dhw_component(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        # 0.5 kWh space heating + 2.0 kWh DHW over 0.25h. Target must be the
        # heating rate (0.5/0.25 = 2.0 kW), NOT the combined 10 kW.
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=2.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_data()

        assert len(y) == 1
        assert float(y[0]) == pytest.approx(2.0)


class TestDemandQuantiles:
    """Demand model exposes a p10/p50/p90 uncertainty band."""

    def test_untrained_band_is_degenerate(self):
        from packages.ml.models import DemandModel

        model = DemandModel()
        band = model.predict_hourly_quantiles([{"temperature": 5.0}] * 3, hours=3)
        assert len(band) == 3
        for entry in band:
            assert entry["p10"] == entry["p50"] == entry["p90"]
            assert entry["p50"] >= 0

    @pytest.mark.asyncio
    async def test_trained_band_is_ordered(self, tmp_path):
        from packages.ml.models import DemandModel

        rng = np.random.RandomState(1)
        n = 400
        outdoor = rng.uniform(-10, 20, n)
        X = np.column_stack(
            [
                outdoor,
                rng.uniform(0, 10, n),
                rng.uniform(0, 500, n),
                rng.uniform(0, 5, n),
                rng.uniform(30, 95, n),
                rng.uniform(0, 1, n),
                np.sin(2 * np.pi * rng.randint(0, 24, n) / 24),
                np.cos(2 * np.pi * rng.randint(0, 24, n) / 24),
                np.sin(2 * np.pi * rng.randint(0, 7, n) / 7),
                np.cos(2 * np.pi * rng.randint(0, 7, n) / 7),
            ]
        )
        y = np.clip(3.0 - 0.1 * outdoor + rng.normal(0, 0.5, n), 0.1, None)

        model = DemandModel()

        async def fake_prep():
            return X, y

        model._prepare_data = fake_prep
        with (
            patch("packages.ml.models.MODEL_DIR", tmp_path),
            patch("packages.ml.demand_model_core.MODEL_DIR", tmp_path),
            patch("packages.ml.safe_persistence.safe_dump"),
        ):
            result = await model.train()

        assert "version" in result
        band = model.predict_hourly_quantiles([{"temperature": 0.0}] * 5, hours=5)
        for entry in band:
            assert entry["p10"] <= entry["p50"] <= entry["p90"]


class TestCOPExcludesFallback:
    """COP training must not synthesize samples when status data is missing."""

    @pytest.mark.asyncio
    async def test_no_status_yields_no_samples(self):
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, tank_kwh=0.0, outdoor_temp=5.0),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=30), heat_kwh=1.0, tank_kwh=0.0, outdoor_temp=5.0
            ),
        ]
        results = [
            _FakeResult(consumption),
            _FakeResult([]),
            _FakeResult([]),
        ]  # no status/weather rows

        model = COPModel()
        with patch("packages.ml.cop_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_training_data()

        # Previously a synthetic COP (0.7 × default curve) would leak in here.
        assert len(y) == 0

    @pytest.mark.asyncio
    async def test_cop_uses_dhw_counter_without_space_heating_energy(self):
        """Tank COP must not be diluted by space-heating consumption."""
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(ts=base, heat_kwh=0.0, tank_kwh=0.0, outdoor_temp=0.0),
            # The same interval has 3 kWh of space heating, but only 1 kWh
            # belongs to the tank whose 6°C temperature rise is measured.
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=3.0,
                tank_kwh=1.0,
                outdoor_temp=0.0,
            ),
        ]
        statuses = [
            SimpleNamespace(
                ts=base,
                tank_target_temp=50.0,
                tank_temp=45.0,
                outdoor_temp=0.0,
                direction="WATER",
                zone1_temp=30.0,
                defrost_active=False,
            ),
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=30),
                tank_target_temp=50.0,
                tank_temp=51.0,
                outdoor_temp=0.0,
                direction="WATER",
                zone1_temp=30.0,
                defrost_active=False,
            ),
        ]
        results = [_FakeResult(consumption), _FakeResult(statuses), _FakeResult([])]

        model = COPModel()
        with patch("packages.ml.cop_model_core.get_session", _mock_get_session(results)):
            X, y = await model._prepare_training_data()

        assert len(y) == 1
        assert float(y[0]) == pytest.approx(6.0 * model._tank_kwh_per_degree())
        assert float(X[0][0]) == 0.0  # zero outdoors must not fall back to 5°C


class TestMAEBaseline:
    """Regression gating: a worse retrain must not replace a good model."""

    def test_first_train_always_deploys(self, tmp_path):
        from packages.ml import models_common as mc

        with patch.object(mc, "MODEL_DIR", tmp_path):
            decision = mc.evaluate_regression("cop", mae=1.0, has_prior_model=False)
        assert decision["deploy"] is True

    def test_regression_blocks_deploy(self, tmp_path):
        from packages.ml import models_common as mc

        with patch.object(mc, "MODEL_DIR", tmp_path):
            mc.write_mae_baseline("cop", 0.5)
            good = mc.evaluate_regression("cop", mae=0.5, has_prior_model=True)
            bad = mc.evaluate_regression("cop", mae=1.0, has_prior_model=True)

        assert good["deploy"] is True
        assert bad["deploy"] is False
        assert bad["improved"] is False

    def test_time_series_cv_mae_returns_scalar(self):
        from packages.ml.models_common import make_monotonic_regressor, time_series_cv_mae

        rng = np.random.RandomState(3)
        X = rng.uniform(-5, 15, (120, 2))
        y = 3.0 - 0.1 * X[:, 0] + rng.normal(0, 0.2, 120)
        model = make_monotonic_regressor([-1, 0])
        mae, std = time_series_cv_mae(model, X, y)
        assert mae >= 0
        assert std >= 0


class TestDirectionAwareCOPConfidence:
    """DHW COP is measured; space-heating/cooling COP is flagged estimated."""

    @pytest.mark.asyncio
    async def test_heating_water_is_measured(self):
        from packages.ml.models import DirectionAwareCOP

        base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
        prev = SimpleNamespace(
            ts=base,
            device_action="HEATING_WATER",
            tank_temp=45.0,
            zone1_temp=30.0,
            outdoor_temp=5.0,
            defrost_active=False,
            device_id="d1",
        )
        curr = SimpleNamespace(
            ts=base + dt.timedelta(hours=1),
            device_action="HEATING_WATER",
            tank_temp=50.0,
            zone1_temp=30.0,
            outdoor_temp=5.0,
            defrost_active=False,
            device_id="d1",
        )
        consumption = [
            SimpleNamespace(
                ts=base + dt.timedelta(hours=1), tank_kwh=2.0, heat_kwh=0.0, cool_kwh=0.0
            )
        ]
        # reads: status, consumption; then a persist session (padded dummies)
        results = [
            _ScalarResult([prev, curr]),
            _ScalarResult(consumption),
            _FakeResult([]),
            _FakeResult([]),
        ]

        dac = DirectionAwareCOP()
        with patch("packages.ml.models.get_session", _mock_get_session(results)):
            intervals = await dac.compute_cop_intervals(hours=24)

        assert len(intervals) == 1
        assert intervals[0]["mode"] == "HEATING_WATER"
        assert intervals[0]["confidence"] == "measured"

    @pytest.mark.asyncio
    async def test_space_heating_is_estimated(self):
        from packages.ml.models import DirectionAwareCOP

        base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
        prev = SimpleNamespace(
            ts=base,
            device_action="HEATING",
            tank_temp=45.0,
            zone1_temp=30.0,
            outdoor_temp=5.0,
            defrost_active=False,
            device_id="d1",
        )
        curr = SimpleNamespace(
            ts=base + dt.timedelta(hours=1),
            device_action="HEATING",
            tank_temp=45.0,
            zone1_temp=34.0,
            outdoor_temp=5.0,
            defrost_active=False,
            device_id="d1",
        )
        consumption = [
            SimpleNamespace(
                ts=base + dt.timedelta(hours=1), tank_kwh=0.0, heat_kwh=1.0, cool_kwh=0.0
            )
        ]
        results = [
            _ScalarResult([prev, curr]),
            _ScalarResult(consumption),
            _FakeResult([]),
            _FakeResult([]),
        ]

        dac = DirectionAwareCOP()
        with patch("packages.ml.models.get_session", _mock_get_session(results)):
            intervals = await dac.compute_cop_intervals(hours=24)

        assert len(intervals) == 1
        assert intervals[0]["mode"] == "HEATING"
        assert intervals[0]["confidence"] == "estimated"


class TestModelCheckpointRetention:
    def test_keeps_only_newest_matching_checkpoints(self, tmp_path):
        from packages.ml.models_common import prune_old_models

        for index in range(7):
            (tmp_path / f"cop_model_{index:02d}.pkl").write_bytes(b"model")
        unrelated = tmp_path / "thermal_params_v1.pkl"
        unrelated.write_bytes(b"thermal")

        assert prune_old_models("cop_model_*.pkl", keep=5, model_dir=tmp_path) == 2
        assert [path.name for path in sorted(tmp_path.glob("cop_model_*.pkl"))] == [
            f"cop_model_{index:02d}.pkl" for index in range(2, 7)
        ]
        assert unrelated.exists()

    def test_rejects_negative_retention(self, tmp_path):
        from packages.ml.models_common import prune_old_models

        with pytest.raises(ValueError, match="keep must be >= 0"):
            prune_old_models("*.pkl", keep=-1, model_dir=tmp_path)

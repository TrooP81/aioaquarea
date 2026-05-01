"""Tests for ML models (COP, Demand) and ThermalModel."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


class TestCOPModel:
    def test_untrained_uses_fallback(self):
        """Untrained model should use fallback prediction."""
        from packages.ml.models import COPModel

        model = COPModel()
        assert not model.is_trained

        # Fallback should return reasonable values
        pred = model.predict(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 0.5 < pred < 5.0

    def test_fallback_higher_at_cold(self):
        """Colder outdoor temp → higher electrical consumption (lower COP)."""
        from packages.ml.models import COPModel

        model = COPModel()
        cold_pred = model.predict(outdoor_temp=-5.0, tank_target=50, hour=12)
        warm_pred = model.predict(outdoor_temp=15.0, tank_target=50, hour=12)
        assert cold_pred > warm_pred

    def test_predict_cop_fallback(self):
        """predict_cop should return a reasonable COP range."""
        from packages.ml.models import COPModel

        model = COPModel()
        cop = model.predict_cop(outdoor_temp=5.0, tank_target=50, hour=12)
        assert 1.0 < cop < 8.0

    def test_make_features_shape(self):
        """Feature vector should have correct shape."""
        from packages.ml.models import COPModel

        features = COPModel._make_features(5.0, 50, 12)
        assert features.shape == (4,)

    def test_load_latest_no_models(self, tmp_path):
        """load_latest returns False when no model files exist."""
        from packages.ml.models import COPModel

        model = COPModel()
        with patch("packages.ml.models.MODEL_DIR", tmp_path):
            assert not model.load_latest()


class TestDemandModel:
    def test_untrained_uses_fallback(self):
        """Untrained model should produce reasonable fallback predictions."""
        from packages.ml.models import DemandModel

        model = DemandModel()
        assert not model.is_trained

        weather = [{"temperature": 5.0, "wind_speed": 3.0, "irradiance": 0.0}] * 24
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
        pred = model.predict_tank_cooling_time(
            current_temp=52.0, min_temp=45.0, outdoor_temp=5.0
        )

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


class TestOrchestratorFallback:
    """Tests for the orchestrator layer selection and fallback logic."""

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

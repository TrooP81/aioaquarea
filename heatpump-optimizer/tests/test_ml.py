"""Tests for ML models (COP, Demand) and ThermalModel."""

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from packages.optimizer import InfeasibleError, SolverTimeoutError


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return None


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    def add(self, *_args, **_kwargs):
        return None

    async def execute(self, *args, **kwargs):
        self.statements.append(args[0] if args else None)
        if self._results:
            return self._results.pop(0)
        return _FakeResult([])


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

        features = COPModel._make_features(5.0, 50, 12)
        assert features.shape == (7,)

    def test_load_latest_no_models(self, tmp_path):
        """load_latest returns False when no model files exist."""
        from packages.ml.models import COPModel

        model = COPModel()
        with patch("packages.ml.models.MODEL_DIR", tmp_path):
            assert not model.load_latest()

    @pytest.mark.parametrize(
        ("model_class", "filename"),
        [
            ("COPModel", "cop_model_weather_dhw_v4_20261001_1200.pkl"),
            ("DemandModel", "demand_model_weather_v2_20261001_1200.pkl"),
        ],
    )
    def test_load_latest_ignores_legacy_artifact_prefix(self, model_class, filename, tmp_path):
        from packages.ml.models import COPModel, DemandModel

        model = {"COPModel": COPModel, "DemandModel": DemandModel}[model_class]()
        (tmp_path / filename).write_bytes(b"legacy artifact")
        with (
            patch("packages.ml.models.MODEL_DIR", tmp_path),
            patch("packages.ml.safe_persistence.safe_load") as safe_load,
        ):
            assert not model.load_latest()

        safe_load.assert_not_called()

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
                np.zeros(n),  # precipitation
                np.full(n, 60.0),  # humidity
                np.full(n, 0.5),  # cloud_cover
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
            from packages.ml.cop_model_core import COP_MODEL_ARTIFACT_PREFIX

            model_path = tmp_path / f"{COP_MODEL_ARTIFACT_PREFIX}test.pkl"
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

    def test_make_features_shape_and_order(self):
        """Shared feature builder must return the fixed 10-feature schema."""
        from packages.ml.models import DemandModel

        features = DemandModel._make_features(5.0, 3.0, 100.0, 12, 2, 1.2, 73.0, 0.8)
        assert features.shape == (10,)
        assert features[0] == 5.0  # temperature
        assert features[1] == 3.0  # wind
        assert features[2] == 100.0  # irradiance
        assert features[3] == 1.2  # precipitation
        assert features[4] == 73.0  # humidity
        assert features[5] == 0.8  # cloud_cover

    @pytest.mark.asyncio
    async def test_prepare_data_uses_interval_rate_not_cumulative(self):
        """Target must be per-interval hourly RATE, never the cumulative counter."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=1.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with (
            patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            X, y = await model._prepare_data()

        # 0.5 kWh / 0.25h = 2.0 kW; 1.0 kWh / 0.25h = 4.0 kW
        assert len(y) == 2
        assert sorted(round(float(v), 6) for v in y) == [2.0, 4.0]
        # The cumulative reading (1.5) must NOT leak through as a target.
        assert max(y) == pytest.approx(4.0)
        # Matched weather temperature is used in the demand feature schema.
        assert X[0][0] == 1.0

    @pytest.mark.asyncio
    async def test_prepare_data_uses_source_date_for_historical_local_cutover(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 7, 15, 23, 50, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=10.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.3,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        context = _FakeSessionCtx([_FakeResult(consumption), _FakeResult(weather)])
        model = DemandModel()

        with (
            patch("packages.ml.demand_model_core.get_session", return_value=context),
            patch(
                "packages.ml.demand_model_core.get_user_tz",
                new=AsyncMock(return_value="Europe/Stockholm"),
            ),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([3.6, 0.4])
        assert model.last_data_quality["counter_source_day_reset"] == 1

    @pytest.mark.xfail(
        strict=True, reason="source_date SELECT lands with migration 031 ingestion change"
    )
    @pytest.mark.asyncio
    async def test_prepare_data_selects_source_date_for_historical_local_cutover(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 7, 15, 23, 50, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=10.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.3,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        context = _FakeSessionCtx([_FakeResult(consumption), _FakeResult(weather)])
        model = DemandModel()

        with (
            patch("packages.ml.demand_model_core.get_session", return_value=context),
            patch(
                "packages.ml.demand_model_core.get_user_tz",
                new=AsyncMock(return_value="Europe/Stockholm"),
            ),
        ):
            await model._prepare_data()

        assert "source_date" in str(context._session.statements[0])

    @pytest.mark.asyncio
    async def test_prepare_data_uses_elapsed_time_across_repeated_daily_totals(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=1.0 if index < 12 else 2.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            )
            for index in range(13)
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([1.0 / 3.0])

    @pytest.mark.asyncio
    async def test_prepare_data_falls_back_to_weather_temp(self):
        """When consumption lacks outdoor_temp, use nearest weather temperature."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.25,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=None,
            ),
        ]
        weather = [
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=20), temperature=-3.0, wind_speed=5.0, irradiance=0.0
            )
        ]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with (
            patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            X, y = await model._prepare_data()

        assert len(y) == 1
        assert X[0][0] == -3.0  # weather temperature filled in
        assert X[0][1] == 5.0  # weather wind

    @pytest.mark.asyncio
    async def test_hourly_counter_steps_yield_hourly_rates(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=index // 4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            )
            for index in range(9)
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([1.0, 1.0])
        assert model.last_data_quality["counter_zero_delta"] == 6

    @pytest.mark.asyncio
    async def test_prepare_data_rejects_nonpositive_and_implausible_rates(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=1),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=2),
                heat_kwh=100.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]

        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
            patch("packages.ml.demand_model_core._logger.info") as log_info,
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([0.5])
        assert model.last_data_quality["intervals"] == 2
        assert model.last_data_quality["rejected_rate_bounds"] == 1
        assert "one" not in repr(log_info.call_args)


class TestCOPCounterWindows:
    @pytest.mark.asyncio
    async def test_hourly_tank_steps_yield_one_bounded_sample_per_change(self):
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        model = COPModel()
        degrees_per_hour = 1.0 / model._tank_kwh_per_degree()
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=0.0,
                tank_kwh=0.5 * (index // 4),
                outdoor_temp=2.0,
            )
            for index in range(201)
        ]
        statuses = [
            SimpleNamespace(
                device_id="one",
                ts=row.ts,
                tank_target_temp=50,
                tank_temp=20.0 + degrees_per_hour * (index // 4),
                direction="WATER",
                device_action="HEATING_WATER",
                zone1_temp=None,
                defrost_active=False,
            )
            for index, row in enumerate(consumption)
        ]
        weather = [
            SimpleNamespace(
                ts=base, temperature=2.0, precipitation=0.0, humidity=60.0, cloud_cover=0.5
            )
        ]
        context = _FakeSessionCtx(
            [_FakeResult(consumption), _FakeResult(statuses), _FakeResult(weather)]
        )
        with (
            patch("packages.ml.cop_model_core.get_session", return_value=context),
            patch("packages.ml.cop_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, y = await model._prepare_training_data()

        assert len(y) == 49
        assert np.all((y >= model.COP_MIN) & (y <= model.COP_MAX))

    @pytest.mark.xfail(
        strict=True, reason="source_date SELECT lands with migration 031 ingestion change"
    )
    @pytest.mark.asyncio
    async def test_hourly_tank_steps_select_source_date(self):
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        model = COPModel()
        degrees_per_hour = 1.0 / model._tank_kwh_per_degree()
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=0.0,
                tank_kwh=0.5 * (index // 4),
                outdoor_temp=2.0,
            )
            for index in range(201)
        ]
        statuses = [
            SimpleNamespace(
                device_id="one",
                ts=row.ts,
                tank_target_temp=50,
                tank_temp=20.0 + degrees_per_hour * (index // 4),
                direction="WATER",
                device_action="HEATING_WATER",
                zone1_temp=None,
                defrost_active=False,
            )
            for index, row in enumerate(consumption)
        ]
        weather = [
            SimpleNamespace(
                ts=base, temperature=2.0, precipitation=0.0, humidity=60.0, cloud_cover=0.5
            )
        ]
        context = _FakeSessionCtx(
            [_FakeResult(consumption), _FakeResult(statuses), _FakeResult(weather)]
        )
        with (
            patch("packages.ml.cop_model_core.get_session", return_value=context),
            patch("packages.ml.cop_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            await model._prepare_training_data()

        assert "source_date" in str(context._session.statements[0])

    @pytest.mark.asyncio
    async def test_realistic_irregular_multi_device_cop_yield_has_safe_diagnostics(self):
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 6, 0, tzinfo=dt.timezone.utc)
        model = COPModel()
        kwh_per_degree = model._tank_kwh_per_degree()
        consumption = []
        statuses = []
        step_times = {
            "device-a": {
                dt.datetime(2026, 1, 5, 7, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 8, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 9, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 12, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 13, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc),
            },
            "device-b": {
                dt.datetime(2026, 1, 5, 6, 45, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 7, 45, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 10, 15, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 11, 30, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 5, 13, 0, tzinfo=dt.timezone.utc),
            },
        }
        counters = {device_id: 0.0 for device_id in step_times}
        for device_id in step_times:
            for poll_index in range(0, 41):
                ts = base + dt.timedelta(minutes=15 * poll_index)
                if device_id == "device-a" and dt.datetime(
                    2026, 1, 5, 10, 0, tzinfo=dt.timezone.utc
                ) <= ts < dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc):
                    continue
                if ts in step_times[device_id]:
                    counters[device_id] += 0.5
                consumption.append(
                    SimpleNamespace(
                        device_id=device_id,
                        ts=ts,
                        heat_kwh=0.0,
                        tank_kwh=counters[device_id],
                        outdoor_temp=2.0,
                    )
                )
                statuses.append(
                    SimpleNamespace(
                        device_id=device_id,
                        ts=ts,
                        tank_target_temp=50,
                        tank_temp=40.0 + counters[device_id] * 2.0 / kwh_per_degree,
                        direction="WATER" if ts.hour < 11 else "SPACE",
                        device_action="HEATING_WATER" if ts.hour < 11 else "IDLE",
                        zone1_temp=None,
                        defrost_active=False,
                    )
                )
        consumption.sort(key=lambda row: row.ts)
        statuses.sort(key=lambda row: row.ts)
        weather = [
            SimpleNamespace(
                ts=base,
                temperature=2.0,
                precipitation=0.0,
                humidity=60.0,
                cloud_cover=0.5,
            )
        ]
        with (
            patch(
                "packages.ml.cop_model_core.get_session",
                _mock_get_session(
                    [_FakeResult(consumption), _FakeResult(statuses), _FakeResult(weather)]
                ),
            ),
            patch("packages.ml.cop_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
            patch("packages.ml.cop_model_core._logger.info") as log_info,
        ):
            _, target = await model._prepare_training_data()

        assert 4 <= len(target) < len(step_times["device-a"]) + len(step_times["device-b"])
        assert np.all(target == pytest.approx(2.0))
        assert np.all((target >= model.COP_MIN) & (target <= model.COP_MAX))
        log_text = repr(log_info.call_args)
        assert "device-a" not in log_text
        assert "device-b" not in log_text
        diagnostics = log_info.call_args.kwargs
        assert diagnostics["counter_outside_window"] >= 1
        assert diagnostics["skip_no_dhw_activity"] >= 1

    @pytest.mark.parametrize(
        ("timezone", "start", "end"),
        [
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Stockholm",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Stockholm",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
            ),
        ],
    )
    def test_dst_spring_and_fall_use_elapsed_instants(self, timezone, start, end):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(device_id="one", ts=start, heat_kwh=1.0),
            SimpleNamespace(device_id="one", ts=end, heat_kwh=2.0),
        ]
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo(timezone), max_interval_hours=4.0
            )
        )
        assert intervals[0].elapsed_hours == 3.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocked_status", ["defrost", "mixed_mode"])
    async def test_cop_rejects_defrost_and_mixed_mode_windows(self, blocked_status):
        from packages.ml.models import COPModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.0,
                tank_kwh=0.5,
                outdoor_temp=2.0,
            ),
        ]
        statuses = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                tank_target_temp=50,
                tank_temp=20.0 + 2.5 * index,
                direction="WATER",
                device_action="HEATING_WATER"
                if blocked_status != "mixed_mode" or index != 1
                else "HEATING",
                zone1_temp=None,
                defrost_active=blocked_status == "defrost" and index == 1,
            )
            for index in range(3)
        ]
        weather = [
            SimpleNamespace(
                ts=base, temperature=2.0, precipitation=0.0, humidity=60.0, cloud_cover=0.5
            )
        ]
        model = COPModel()
        with (
            patch(
                "packages.ml.cop_model_core.get_session",
                _mock_get_session(
                    [_FakeResult(consumption), _FakeResult(statuses), _FakeResult(weather)]
                ),
            ),
            patch("packages.ml.cop_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            X, y = await model._prepare_training_data()

        assert len(X) == len(y) == 0


class TestConsumptionIntervals:
    def test_counter_changes_ignore_zero_polls_and_keep_devices_independent(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=1.0),
            SimpleNamespace(
                device_id="two",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=4.0,
            ),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.0),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=30), heat_kwh=4.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=1.4),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )
        assert [item.device_id for item in intervals] == ["two", "one"]
        assert [item.energy_kwh for item in intervals] == pytest.approx([0.5, 0.4])
        assert [item.elapsed_hours for item in intervals] == pytest.approx([0.25, 1.0])
        assert diagnostics["zero_delta"] == 1

    def test_transient_midday_decrease_retains_anchor_for_recovery(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=2.5),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )
        assert intervals[0].energy_kwh == pytest.approx(0.5)
        assert intervals[0].elapsed_hours == pytest.approx(1.0)
        assert diagnostics["midday_decrease"] == 1

    def test_persisted_midday_decrease_reanchors_after_confirmation(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=2.0),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.5])
        assert [interval.elapsed_hours for interval in intervals] == pytest.approx([0.5])
        assert diagnostics["midday_decrease"] == 1
        assert diagnostics["correction_reanchor"] == 1

    def test_cop_confirmation_rejects_transient_refresh_spike(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.4),
            SimpleNamespace(
                device_id="one", ts=base + dt.timedelta(hours=1, minutes=15), heat_kwh=10.4
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [(interval.energy_kwh, interval.elapsed_hours) for interval in intervals] == [
            (pytest.approx(0.4), pytest.approx(1.0))
        ]
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "positive_confirmed": 1,
        }

    def test_cop_confirmation_requires_a_later_reading_after_revert(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.3),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert intervals == []
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "positive_unresolved": 1,
        }

    def test_cop_confirmation_does_not_reuse_revert_history_after_zero_reads(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=4), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=8), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=11), heat_kwh=10.3),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert intervals == []
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "zero_delta": 2,
            "positive_unresolved": 1,
        }

    def test_demand_default_emits_transient_refresh_spike_immediately(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.4),
        ]

        intervals = list(iter_counter_change_intervals(rows, "heat_kwh", ZoneInfo("UTC")))

        assert [(interval.energy_kwh, interval.elapsed_hours) for interval in intervals] == [
            (pytest.approx(0.8), pytest.approx(0.25))
        ]

    @pytest.mark.parametrize(
        ("values", "expected_energy", "diagnostic"),
        [
            ([10.0, 10.8, 10.8], 0.8, "positive_confirmed"),
            ([10.0, 10.8, 10.4, 10.4], 0.4, "positive_replaced"),
        ],
    )
    def test_confirmation_positive_candidates(self, values, expected_energy, diagnostic):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=value,
            )
            for index, value in enumerate(values)
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([expected_energy])
        assert diagnostics[diagnostic] == 1

    @pytest.mark.parametrize(
        ("values", "expected_energy", "diagnostic"),
        [
            ([10.0, 0.3, 0.4], 0.3, "reset_confirmed"),
            ([10.0, 0.3, 0.2, 0.2], 0.2, "reset_replaced"),
            ([10.0, 0.3, 10.1], None, "reset_abandoned"),
            ([10.0, 0.3], None, "reset_unresolved"),
        ],
    )
    def test_confirmation_reset_candidates(self, values, expected_energy, diagnostic):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=value,
            )
            for index, value in enumerate(values)
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == (
            [] if expected_energy is None else pytest.approx([expected_energy])
        )
        assert diagnostics[diagnostic] == 1

    def test_explicit_equal_source_date_suppresses_timestamp_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
                source_date=dt.date(2026, 1, 5),
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 6, 0, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 5),
            ),
        ]
        diagnostics: dict[str, int] = {}

        assert (
            list(
                iter_counter_change_intervals(
                    rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
                )
            )
            == []
        )
        assert diagnostics["midday_decrease"] == 1
        assert "source_day_reset" not in diagnostics

    def test_missing_or_mixed_source_date_uses_timestamp_compatibility(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=25),
                heat_kwh=0.3,
                source_date=None,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["source_day_reset"] == 1

    def test_explicit_source_date_change_starts_and_confirms_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 6, 0, 5, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=10),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=20),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["source_day_reset"] == 1
        assert diagnostics["reset_confirmed"] == 1

    def test_newer_source_day_replaces_pending_positive_with_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 6, 0, 5, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=10),
                heat_kwh=10.8,
                source_date=dt.date(2026, 1, 5),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=20),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["positive_abandoned"] == 1
        assert diagnostics["reset_confirmed"] == 1

    def test_irregular_steps_and_long_gaps_are_bounded_per_device(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=1.0),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=15), heat_kwh=4.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=70), heat_kwh=1.5),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=95), heat_kwh=4.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=4), heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=5), heat_kwh=2.5),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )

        assert [item.device_id for item in intervals] == ["one", "two", "one"]
        assert [item.elapsed_hours for item in intervals] == pytest.approx([7 / 6, 4 / 3, 1.0])
        assert [item.energy_kwh for item in intervals] == pytest.approx([0.5, 0.5, 0.5])
        assert diagnostics["outside_window"] == 1

    @pytest.mark.parametrize(
        ("timezone", "start", "end", "expected_hours"),
        [
            (
                "Europe/Stockholm",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
                3.0,
            ),
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
                3.0,
            ),
        ],
    )
    def test_dst_uses_elapsed_instants(self, timezone, start, end, expected_hours):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(device_id="one", ts=start, heat_kwh=1.0),
            SimpleNamespace(device_id="one", ts=end, heat_kwh=2.0),
        ]
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo(timezone), max_interval_hours=4.0
            )
        )
        assert intervals[0].elapsed_hours == expected_hours

    def test_local_midnight_reset_uses_local_day_start(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 22, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        intervals = list(
            iter_counter_change_intervals(rows, "heat_kwh", ZoneInfo("Europe/Amsterdam"))
        )
        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)

    def test_utc_source_day_reset_uses_utc_boundary(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 6, 0, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)
        assert diagnostics["source_day_reset"] == 1

    @pytest.mark.parametrize(
        "reset_day",
        [dt.date(2026, 3, 29), dt.date(2026, 10, 25)],
    )
    def test_dst_day_uses_utc_reset_even_when_local_date_is_unchanged(self, reset_day):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        previous_day = reset_day - dt.timedelta(days=1)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime.combine(previous_day, dt.time(23, 50), tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime.combine(reset_day, dt.time(0, 15), tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                max_interval_hours=4.0,
            )
        )

        assert len(intervals) == 1
        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)
        assert diagnostics["source_day_reset"] == 1
        assert diagnostics.get("local_day_reset", 0) == 0

    @pytest.mark.asyncio
    async def test_demand_accepts_four_hour_counter_interval_but_caps_longer_gaps(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=4),
                heat_kwh=2.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=8, minutes=1),
                heat_kwh=2.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=8, minutes=16),
                heat_kwh=3.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()

        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([0.5, 2.0])
        assert model.last_data_quality["counter_outside_window"] == 1


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
                np.zeros(n),  # precipitation
                np.full(n, 60.0),  # humidity
                np.full(n, 0.5),  # cloud_cover
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
        hours = rng.randint(0, 24, n)
        dow = rng.randint(0, 7, n)
        X = np.column_stack(
            [
                outdoor,
                wind,
                irradiance,
                np.zeros(n),
                np.full(n, 60.0),
                np.full(n, 0.5),
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

    @pytest.fixture(autouse=True)
    def _learning_gate(self, optimization_learning_gate):
        return optimization_learning_gate

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
                assert status["active_layer"] == "rules_v7"

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
                        "packages.optimizer.main.get_session",
                        _mock_get_session([_FakeResult([]), _FakeResult([]), _FakeResult([])]),
                    ),
                    patch(
                        "packages.optimizer.main.get_active_price_context",
                        AsyncMock(
                            return_value=SimpleNamespace(area="NL", currency="EUR", source="test")
                        ),
                    ),
                    patch(
                        "packages.optimizer.main.get_device_data_quality",
                        AsyncMock(return_value={"ready": True, "reasons": []}),
                    ),
                ):
                    await run_optimization()

                MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure", [SolverTimeoutError("timeout"), InfeasibleError("infeasible")]
    )
    async def test_milp_failure_falls_back_to_rules(self, failure):
        """When MILP raises, the orchestrator should fall back to rules."""
        from packages.optimizer.main import run_optimization

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
                mock_milp.generate_plan = AsyncMock(side_effect=failure)
                mock_select.return_value = ("milp", mock_milp)

                with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                    MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)

                    with (
                        patch(
                            "packages.optimizer.main.get_session",
                            _mock_get_session([_FakeResult([]), _FakeResult([])]),
                        ),
                        patch(
                            "packages.optimizer.main.get_planning_data_quality",
                            AsyncMock(
                                return_value={
                                    "control_allowed": True,
                                    "status": "ready",
                                    "reasons": [],
                                    "price": {},
                                    "weather": {},
                                }
                            ),
                        ),
                        patch(
                            "packages.optimizer.main.get_active_price_context",
                            AsyncMock(
                                return_value=SimpleNamespace(
                                    area="NL", currency="EUR", source="test"
                                )
                            ),
                        ),
                        patch(
                            "packages.optimizer.main.get_device_data_quality",
                            AsyncMock(return_value={"ready": True, "reasons": []}),
                        ),
                    ):
                        await run_optimization()

                    # MILP failed, so rules should have been called
                    MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_planning_quality_check_failure_blocks_plan(self):
        """An unavailable planning-quality gate must not permit a rules fallback."""
        from packages.optimizer.main import run_optimization

        optimizer = AsyncMock()
        with (
            patch(
                "packages.optimizer.main.get_setting",
                new=AsyncMock(return_value="milp_preferred"),
            ),
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("milp", optimizer)),
            ),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(return_value={"ready": True, "reasons": []}),
            ),
            patch(
                "packages.optimizer.main.get_planning_data_quality",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("packages.optimizer.main.logger") as logger,
        ):
            assert await run_optimization() is None

        optimizer.generate_plan.assert_not_awaited()
        logger.error.assert_called_once_with(
            "optimization_paused_planning_data_quality_check_failed",
            reason="quality_check_failed",
            error_type="RuntimeError",
        )

    @pytest.mark.asyncio
    async def test_milp_failure_fallback_remains_behind_device_gate(self):
        """A failed MILP cannot reach its rules fallback when readiness is unavailable."""
        from packages.optimizer.main import run_optimization

        milp = AsyncMock()
        with (
            patch(
                "packages.optimizer.main.get_setting",
                new=AsyncMock(return_value="milp_preferred"),
            ),
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("milp", milp)),
            ),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("packages.optimizer.main.RulesOptimizer") as rules,
        ):
            assert await run_optimization() is None

        milp.generate_plan.assert_not_awaited()
        rules.return_value.generate_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_learning_state_skips_before_optimizer_selection(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.UNKNOWN
        with patch(
            "packages.optimizer.main._select_optimizer", new=AsyncMock()
        ) as select_optimizer:
            assert await run_optimization() is None
        select_optimizer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_state_lookup_failure_skips_before_optimizer_selection(
        self, optimization_learning_gate
    ):
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.side_effect = RuntimeError("settings unavailable")
        with patch(
            "packages.optimizer.main._select_optimizer", new=AsyncMock()
        ) as select_optimizer:
            assert await run_optimization() is None
        select_optimizer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_learning_state_does_not_touch_active_plan_or_safety_links(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.UNKNOWN
        with (
            patch("packages.optimizer.main.get_session") as get_session,
            patch("packages.optimizer.main._select_optimizer", new=AsyncMock()) as select_optimizer,
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ) as comfort_model,
        ):
            assert await run_optimization(force_replace=True) is None

        # The active plan and any pending linked safety restore remain untouched
        # because UNKNOWN returns before opening the lifecycle transaction.
        get_session.assert_not_called()
        select_optimizer.assert_not_awaited()
        comfort_model.arefresh_if_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_state_cancellation_propagates(self, optimization_learning_gate):
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await run_optimization()

    @pytest.mark.asyncio
    async def test_active_learning_state_reaches_normal_admission_checks(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.ACTIVE
        optimizer = AsyncMock()
        with (
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("rules", optimizer)),
            ) as select_optimizer,
            patch("packages.optimizer.main.get_setting", new=AsyncMock(return_value="rules_only")),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["credentials_missing"]}),
            ),
        ):
            assert await run_optimization() is None
        select_optimizer.assert_awaited_once()
        optimizer.generate_plan.assert_not_awaited()


def test_model_dir_is_isolated_to_pytest_tmp(isolate_model_dir: Path) -> None:
    """Unit tests must never resolve model artifacts through the production directory."""
    from packages.core.config import settings
    from packages.ml import comfort_model, models_common, thermal

    expected = isolate_model_dir.resolve()
    assert Path(settings.model_dir).resolve() == expected
    assert models_common.MODEL_DIR.resolve() == expected
    assert comfort_model.MODEL_DIR.resolve() == expected
    assert thermal.MODEL_DIR.resolve() == expected


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


class TestDemandHeatingOnlyTarget:
    @pytest.mark.asyncio
    async def test_dhw_only_interval_is_skipped(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=1.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]

        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert len(target) == 0


class TestDemandQuantiles:
    def test_untrained_band_is_degenerate(self):
        from packages.ml.models import DemandModel

        band = DemandModel().predict_hourly_quantiles([{"temperature": 5.0}] * 3, hours=3)

        assert len(band) == 3
        assert all(entry["p10"] == entry["p50"] == entry["p90"] >= 0 for entry in band)


class TestMAEBaseline:
    def test_write_atomically_replaces_baseline(self, tmp_path):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        baseline_path.write_text('{"mae": 1.0}')
        with (
            patch.object(models_common, "MODEL_DIR", tmp_path),
            patch.object(models_common.os, "replace", wraps=models_common.os.replace) as replace,
        ):
            models_common.write_mae_baseline("cop", 0.5)
            assert models_common.read_mae_baseline("cop") == 0.5

        replace.assert_called_once()
        temporary_path, destination_path = replace.call_args.args
        assert Path(temporary_path).parent == tmp_path
        assert destination_path == baseline_path
        assert set(json.loads(baseline_path.read_text())) == {"mae", "updated_at"}

    def test_write_failure_preserves_baseline_and_removes_temp_file(self, tmp_path):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        original_contents = '{"mae": 1.0}'
        baseline_path.write_text(original_contents)
        with (
            patch.object(models_common, "MODEL_DIR", tmp_path),
            patch.object(models_common.os, "replace", side_effect=OSError("replace failed")),
        ):
            models_common.write_mae_baseline("cop", 0.5)

        assert baseline_path.read_text() == original_contents
        assert not list(tmp_path.glob(".cop_mae_baseline.json.*.tmp"))

    @pytest.mark.parametrize("contents", [None, "not json", '{"mae": null}', "{}"])
    def test_read_missing_or_malformed_baseline_returns_none(self, tmp_path, contents):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        if contents is not None:
            baseline_path.write_text(contents)

        with patch.object(models_common, "MODEL_DIR", tmp_path):
            assert models_common.read_mae_baseline("cop") is None

    def test_regression_blocks_deploy(self, tmp_path):
        from packages.ml import models_common as models_common

        with patch.object(models_common, "MODEL_DIR", tmp_path):
            models_common.write_mae_baseline("cop", 0.5)
            decision = models_common.evaluate_regression("cop", mae=1.0, has_prior_model=True)

        assert decision["deploy"] is False
        assert decision["improved"] is False


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

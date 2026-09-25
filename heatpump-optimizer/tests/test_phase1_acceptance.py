"""Acceptance coverage for the Phase 1 comfort and stale-data contracts."""

from __future__ import annotations

import datetime as dt
import inspect
import json
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from packages.api.routers import models_router
from packages.api.routers.models_router import readiness
from packages.core.device_data_quality import get_device_data_quality
from packages.core.heating_evidence import classify_space_heating
from packages.core.operational_alerts import device_status_is_fresh
from packages.ml.comfort_model import ComfortModel
from packages.optimizer.actions import ActionType
from packages.optimizer.executor_core import PlanExecutor
from packages.optimizer.rules import RulesOptimizer


def _context(value):
    class Context:
        async def __aenter__(self):
            return value

        async def __aexit__(self, *args):
            return False

    return Context()


def _quality_session(timestamp=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = timestamp
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


class TestAC8ArtifactLifecycle:
    @pytest.mark.asyncio
    async def test_AC8_1_status_predict_and_train_refresh_before_use(self):
        model = MagicMock()
        events = []
        model.is_trained = True
        model.is_ready_for_control = False
        model.control_readiness = {"ready": False}
        model.last_trained = None
        model.training_samples = 0
        model.metrics = {}
        model.training_notice = None
        model.artifact_refresh_reason = None
        model.control_margin_c = 0.0
        model.passive_forecast_readiness.return_value = {"ready": False}
        model.arefresh_if_changed = AsyncMock(side_effect=lambda: events.append("refresh"))
        model.predict_indoor_temp.side_effect = lambda **kwargs: events.append("predict") or 20.0
        model.required_zone_temp.side_effect = lambda **kwargs: events.append("required") or 35.0
        model.train = AsyncMock(
            side_effect=lambda **kwargs: events.append("train") or {"status": "trained"}
        )

        with patch("packages.ml.comfort_model.comfort_model", model):
            await models_router.get_comfort_model_status()
            await models_router.predict_indoor_temp(water_temp=35.0, outdoor_temp=5.0)
            with (
                patch(
                    "packages.core.settings_service.get_setting",
                    new=AsyncMock(return_value="60"),
                ),
                patch(
                    "packages.api.routers.models_router.get_session",
                    return_value=_context(MagicMock(add=MagicMock())),
                ),
            ):
                await models_router.trigger_comfort_model_training()

        assert events == ["refresh", "refresh", "predict", "required", "refresh", "train"]
        assert model.arefresh_if_changed.await_count == 3

    def test_AC8_2_fingerprint_change_loads_newest_and_ignores_old_prefix(
        self, tmp_path, monkeypatch
    ):
        model = ComfortModel()
        candidate = MagicMock(n_features_in_=14)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load", MagicMock(return_value={"model": candidate})
        )
        first = tmp_path / "comfort_model_weather_causal_v7_component_evidence_1.pkl"
        second = tmp_path / "comfort_model_weather_causal_v7_component_evidence_2.pkl"
        first.write_bytes(b"valid")

        assert model.refresh_if_changed() is True
        assert model.refresh_if_changed() is False
        second.write_bytes(b"new")
        assert model.refresh_if_changed() is True
        assert model._artifact_fingerprint[0] == second

        legacy = tmp_path / "comfort_model_old_schema.pkl"
        legacy.write_bytes(b"legacy")
        empty_model = ComfortModel()
        empty_model.refresh_if_changed()
        assert empty_model.artifact_refresh_reason is None

    @pytest.mark.asyncio
    async def test_AC8_2_optimizer_and_poller_refresh_call_sites(self):
        import importlib

        optimizer_main = importlib.import_module("packages.optimizer.main")
        poller_main = importlib.import_module("packages.poller.main")

        optimizer = AsyncMock()
        refresh = AsyncMock()
        quality = {"ready": False, "reasons": ["credentials_missing"]}
        with (
            patch.object(
                optimizer_main,
                "_select_optimizer",
                new=AsyncMock(return_value=("rules", optimizer)),
            ),
            patch.object(optimizer_main, "get_setting", new=AsyncMock(return_value="rules_only")),
            patch.object(
                optimizer_main, "comfort_model", SimpleNamespace(arefresh_if_changed=refresh)
            ),
            patch.object(
                optimizer_main, "get_device_data_quality", new=AsyncMock(return_value=quality)
            ),
        ):
            assert await optimizer_main.run_optimization() is None
        refresh.assert_awaited_once_with()
        optimizer.generate_plan.assert_not_awaited()

        events = []
        poller_model = SimpleNamespace(
            arefresh_if_changed=AsyncMock(side_effect=lambda: events.append("refresh")),
            train=AsyncMock(),
        )
        with (
            patch.object(poller_main, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch(
                "packages.core.device_data_quality.get_device_data_quality",
                new=AsyncMock(side_effect=lambda: events.append("quality") or quality),
            ),
            patch("packages.ml.comfort_model.comfort_model", poller_model),
        ):
            await poller_main.retrain_comfort_model()
        assert events == ["refresh", "quality"]
        poller_model.arefresh_if_changed.assert_awaited_once_with()
        poller_model.train.assert_not_awaited()

    def test_AC8_3_torn_newest_artifact_keeps_last_known_good(self, tmp_path, monkeypatch):
        model = ComfortModel()
        good = MagicMock(n_features_in_=14)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load",
            MagicMock(side_effect=[{"model": good}, ValueError("partial HMAC")]),
        )
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_1.pkl").write_bytes(b"good")
        assert model.refresh_if_changed() is True
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_2.pkl").write_bytes(
            b"partial"
        )
        assert model.refresh_if_changed() is False
        assert model._model is good
        assert model.artifact_refresh_reason == "artifact_integrity_failed"

    @pytest.mark.parametrize(
        "exception_type",
        [
            pickle.UnpicklingError,
            EOFError,
            AttributeError,
            ImportError,
            ModuleNotFoundError,
            IndexError,
        ],
    )
    def test_AC8_3_valid_signed_load_failures_keep_last_known_good_and_log_safe_fields(
        self, tmp_path, monkeypatch, exception_type
    ):
        model = ComfortModel()
        good = MagicMock(n_features_in_=14)
        sentinel = "AC8_3_EXCEPTION_MESSAGE_SENTINEL"
        load_error = exception_type(sentinel)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load",
            MagicMock(side_effect=[{"model": good}, load_error]),
        )
        good_path = tmp_path / "comfort_model_weather_causal_v7_component_evidence_1.pkl"
        bad_path = tmp_path / "comfort_model_weather_causal_v7_component_evidence_2.pkl"
        good_path.write_bytes(b"valid-signed-artifact")
        assert model.refresh_if_changed() is True
        bad_path.write_bytes(b"valid-signed-artifact")

        with capture_logs() as logs:
            assert model.refresh_if_changed() is False
        assert model._model is good
        assert model.artifact_refresh_reason == "artifact_integrity_failed"
        assert len(logs) == 1
        log = logs[0]
        assert log["artifact"] == bad_path.name
        assert log["exception_type"] == exception_type.__name__
        rendered_log = repr(log)
        assert str(tmp_path) not in rendered_log
        assert sentinel not in rendered_log

    @pytest.mark.asyncio
    async def test_AC8_3_async_refresh_keeps_last_known_good_after_load_failure(
        self, tmp_path, monkeypatch
    ):
        model = ComfortModel()
        good = MagicMock(n_features_in_=14)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load",
            MagicMock(
                side_effect=[
                    {"model": good},
                    ModuleNotFoundError("AC8_3_ASYNC_EXCEPTION_MESSAGE_SENTINEL"),
                ]
            ),
        )
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_1.pkl").write_bytes(
            b"valid-signed-artifact"
        )
        assert await model.arefresh_if_changed() is True
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_2.pkl").write_bytes(
            b"valid-signed-artifact"
        )

        assert await model.arefresh_if_changed() is False
        assert model._model is good
        assert model.artifact_refresh_reason == "artifact_integrity_failed"

    @pytest.mark.asyncio
    async def test_AC8_3_status_returns_200_after_unpickle_integrity_failure(
        self, tmp_path, monkeypatch
    ):
        model = ComfortModel()
        good = MagicMock(n_features_in_=14)
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr(
            "packages.ml.safe_persistence.safe_load",
            MagicMock(side_effect=[{"model": good}, pickle.UnpicklingError("corrupt")]),
        )
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_1.pkl").write_bytes(
            b"valid-signed-artifact"
        )
        assert model.refresh_if_changed() is True
        (tmp_path / "comfort_model_weather_causal_v7_component_evidence_2.pkl").write_bytes(
            b"valid-signed-artifact"
        )
        app = FastAPI()
        app.include_router(models_router.router)

        with patch("packages.ml.comfort_model.comfort_model", model):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/comfort-model/status")

        assert response.status_code == 200
        assert response.json()["artifact_refresh_reason"] == "artifact_integrity_failed"
        assert model._model is good

    def test_AC8_4_two_saves_in_one_second_have_distinct_artifact_names(
        self, tmp_path, monkeypatch
    ):
        model = ComfortModel()
        model._model = object()
        paths = []
        real_datetime = dt.datetime

        class FixedClock:
            timezone = dt.timezone

            @classmethod
            def now(cls, timezone=None):
                return real_datetime(
                    2026, 9, 24, 12, 0, 0, 100000 if not paths else 200000, tzinfo=timezone
                )

        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        monkeypatch.setattr("packages.ml.comfort_model.dt.datetime", FixedClock)

        def dump(_payload, path):
            paths.append(path)
            path.write_bytes(b"artifact")

        monkeypatch.setattr("packages.ml.safe_persistence.safe_dump", dump)
        monkeypatch.setattr("packages.ml.comfort_model.prune_old_models", lambda *args, **kwargs: 0)
        model._save()
        model._save()
        assert len(paths) == 2
        assert paths[0].name != paths[1].name


class TestAC9ComfortEvidence:
    @pytest.mark.parametrize(
        ("elapsed", "samples", "spread", "expected"),
        [
            (dt.timedelta(hours=5, minutes=59), 12, 0.0, False),
            (dt.timedelta(hours=6), 11, 0.0, False),
            (dt.timedelta(hours=6), 12, 0.10, True),
            (dt.timedelta(hours=6), 12, 0.11, False),
        ],
    )
    def test_AC9_2_flat_run_boundaries(self, elapsed, samples, spread, expected):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        rows = []
        for index in range(samples):
            fraction = index / max(1, samples - 1)
            rows.append(
                SimpleNamespace(
                    ts=start + elapsed * fraction,
                    zone1_temp=20.0 + spread * fraction,
                    space_heating_active=True,
                    operation_status=1,
                    mode="1",
                    direction="PUMP",
                    pump_duty=1,
                    device_action="HEATING",
                    defrost_active=False,
                    zone1_operation_status=1,
                    zone2_operation_status=0,
                )
            )
        result = ComfortModel._flat_active_status_times(rows)
        assert bool(result) is expected

    @pytest.mark.parametrize(
        ("active_rows", "buckets", "spread", "ready"),
        [(19, 4, 3.0, False), (20, 3, 3.0, False), (20, 4, 2.9, False), (20, 4, 3.0, True)],
    )
    def test_AC9_3_variance_readiness_boundaries(self, active_rows, buckets, spread, ready):
        model = ComfortModel()
        model._model = object()
        model._metrics = {
            "validated": True,
            "mae": 0.3,
            "r2": 0.5,
            "active_heating_rows": active_rows,
            "active_input_buckets": buckets,
            "active_input_range_c": spread,
            "baseline_mae": 0.5,
        }
        assert model.is_ready_for_control is ready

    def test_AC9_3_variance_gated_model_uses_untrained_planner_fallback(self):
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=hour), 0.10) for hour in range(3)]
        weather = [
            (base, 0.0),
            (base + dt.timedelta(hours=1), -5.0),
            (base + dt.timedelta(hours=2), -5.0),
        ]
        passive = {
            base: 21.0,
            base + dt.timedelta(hours=1): 19.0,
            base + dt.timedelta(hours=2): 19.0,
        }
        variance_gated = ComfortModel()
        variance_gated._model = object()
        variance_gated._metrics = {
            "validated": True,
            "mae": 0.3,
            "r2": 0.5,
            "active_heating_rows": 20,
            "active_input_buckets": 3,
            "active_input_range_c": 3.0,
            "baseline_mae": 0.5,
        }
        assert variance_gated.control_readiness["reason"] == "insufficient_heat_input_variance"

        plans = []
        for model in (ComfortModel(), variance_gated):
            optimizer = RulesOptimizer()
            with (
                patch.object(optimizer, "_passive_indoor_forecast", return_value=passive),
                patch("packages.optimizer.rule_mixins.comfort_model", model),
                patch(
                    "packages.optimizer.rule_mixins.thermal_model.predict_zone_heating_time",
                    return_value=SimpleNamespace(
                        estimated_hours=0.5,
                        estimated_minutes=30.0,
                        heating_rate_per_hour=2.0,
                    ),
                ),
            ):
                plans.append(
                    optimizer._plan_preheat(
                        prices,
                        weather,
                        base,
                        current_indoor_temp=20.0,
                        current_outdoor_temp=-5.0,
                        current_water_temp=30.0,
                        current_zone_target_temp=36.0,
                        current_zone_heat_min=20,
                        current_zone_heat_max=65,
                    )
                )

        assert plans[0] == plans[1]
        assert any(action["type"] == "zone_temp_boost" for action in plans[1])

    @pytest.mark.asyncio
    async def test_AC9_1_source_label_and_AC9_2_excluded_count_are_persisted(self):
        model = ComfortModel()
        model._last_dataset_evidence = {
            "active_heating_rows": 20,
            "active_input_buckets": 4,
            "active_input_range_c": 3.0,
            "flat_active_heating_rows_excluded": 7,
        }

        class Regressor:
            def fit(self, X, y):
                return self

            def predict(self, X):
                return np.zeros(len(X))

        async def direct():
            return {}, {}

        model._build_dataset = AsyncMock(return_value=(np.zeros((100, 14)), np.ones(100), 100))
        model._build_regressor = staticmethod(lambda: Regressor())
        model._train_direct_forecasts = direct
        model._save = lambda: None
        with (
            patch("packages.ml.comfort_model.read_mae_baseline", return_value=None),
            patch("packages.ml.comfort_model.write_mae_baseline"),
        ):
            result = await model._train_with_current_lag()
        assert result["status"] == "trained"
        assert model.metrics["zone_water_temp_source"] == "Panasonic zoneStatus.temperatureNow"
        assert model.metrics["flat_active_heating_rows_excluded"] == 7

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"device_action": "HEATING_WATER", "direction": "PUMP"}, "domestic_hot_water"),
            ({"device_action": "COOLING", "direction": "PUMP"}, "cooling"),
            ({"defrost_active": True, "direction": "PUMP"}, "defrost"),
            ({"device_action": "IDLE", "direction": "PUMP"}, "idle"),
            ({"operation_status": 0, "direction": "PUMP"}, "device_off"),
            ({"operation_status": 1, "direction": "PUMP"}, "not_confirmed"),
        ],
    )
    def test_AC9_4_excluded_modes_are_normalized(self, kwargs, code):
        evidence = classify_space_heating(
            operation_status=kwargs.get("operation_status", 1),
            direction=kwargs.get("direction"),
            device_action=kwargs.get("device_action"),
            defrost_active=kwargs.get("defrost_active", False),
            mode="1",
            pump_duty=0,
        )
        assert evidence.code == code
        assert evidence.active is False

    @pytest.mark.asyncio
    async def test_AC9_4_passive_rows_use_ambient_water_and_target_temperatures(self):
        model = ComfortModel()
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        passive_rows = [
            ("idle", 5.0, {"device_action": "IDLE"}),
            ("idle", 20.0, {"device_action": "IDLE"}),
            ("device_off", 5.0, {"operation_status": 0}),
            ("device_off", 20.0, {"operation_status": 0}),
            ("not_confirmed", 5.0, {}),
            ("not_confirmed", 20.0, {}),
        ]
        excluded_rows = [
            {"device_action": "HEATING_WATER"},
            {"device_action": "COOLING"},
            {"defrost_active": True},
        ]
        statuses = []
        readings = []
        for index, (_, outdoor_temp, overrides) in enumerate(passive_rows):
            timestamp = start + dt.timedelta(minutes=15 * index)
            statuses.append(
                SimpleNamespace(
                    **(
                        {
                            "ts": timestamp,
                            "operation_status": 1,
                            "mode": "1",
                            "direction": "PUMP",
                            "pump_duty": 0,
                            "device_action": "HEATING",
                            "defrost_active": False,
                            "zone1_operation_status": 0,
                            "zone2_operation_status": 0,
                            "zone1_temp": 45.0,
                            "zone1_target_temp": -5.0,
                            "outdoor_temp": outdoor_temp,
                        }
                        | overrides
                    )
                )
            )
            readings.append(
                SimpleNamespace(
                    timestamp=timestamp + dt.timedelta(hours=1),
                    temperature=20.0 + index,
                    device_id="indoor",
                )
            )
        for index, overrides in enumerate(excluded_rows, start=len(passive_rows)):
            timestamp = start + dt.timedelta(minutes=15 * index)
            statuses.append(
                SimpleNamespace(
                    **(
                        {
                            "ts": timestamp,
                            "operation_status": 1,
                            "mode": "1",
                            "direction": "PUMP",
                            "pump_duty": 0,
                            "device_action": "HEATING",
                            "defrost_active": False,
                            "zone1_operation_status": 0,
                            "zone2_operation_status": 0,
                            "zone1_temp": 45.0,
                            "zone1_target_temp": -5.0,
                            "outdoor_temp": 5.0,
                        }
                        | overrides
                    )
                )
            )
            readings.append(
                SimpleNamespace(
                    timestamp=timestamp + dt.timedelta(hours=1),
                    temperature=20.0 + index,
                    device_id="indoor",
                )
            )

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def scalars(self):
                return self

            def all(self):
                return self.rows

        session = MagicMock()
        session.execute = AsyncMock(side_effect=[Result(readings), Result(statuses), Result([])])
        with (
            patch("packages.ml.comfort_model.get_all_settings", new=AsyncMock(return_value={})),
            patch("packages.ml.comfort_model.get_session", return_value=_context(session)),
        ):
            features, _, row_count = await model._build_dataset()

        expected_outdoor_temps = [row[1] for row in passive_rows]
        assert row_count == len(expected_outdoor_temps)
        assert features[:, 0].tolist() == expected_outdoor_temps
        assert features[:, 1].tolist() == expected_outdoor_temps

    def test_AC9_5_old_prefix_is_ignored(self, tmp_path, monkeypatch):
        model = ComfortModel()
        monkeypatch.setattr("packages.ml.comfort_model.MODEL_DIR", tmp_path)
        (tmp_path / "comfort_model_old_schema.pkl").write_bytes(b"legacy")
        assert model.refresh_if_changed() is False
        assert model.artifact_refresh_reason == "artifact_missing"


class TestAC10FreshnessSafety:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured", "poll", "expected"),
        [
            ("4", "300", 900),
            ("61", "300", 3600),
            ("not-an-int", "300", 900),
            ("5", "1000", 3000),
        ],
    )
    async def test_AC10_2_clamps_and_uses_effective_threshold(self, configured, poll, expected):
        now = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
        settings = {
            "aquarea_username": "user",
            "aquarea_password": "password",
            "device_status_max_age_minutes": configured,
            "poll_interval_seconds": poll,
        }
        session = _quality_session(now)
        with (
            patch(
                "packages.core.device_data_quality.get_setting",
                new=AsyncMock(side_effect=lambda key: settings[key]),
            ),
            patch(
                "packages.core.device_data_quality.get_session",
                return_value=_context(session),
            ),
        ):
            quality = await get_device_data_quality(now=now)
        assert quality["threshold_seconds"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("username", "password", "timestamp", "reason"),
        [
            ("", "password", None, "credentials_missing"),
            ("user", "password", None, "device_status_missing"),
            (
                "user",
                "password",
                dt.datetime(2026, 9, 24, 0, tzinfo=dt.timezone.utc),
                "device_status_stale",
            ),
        ],
    )
    async def test_AC10_2_each_quality_reason_code(self, username, password, timestamp, reason):
        now = dt.datetime(2026, 9, 24, 12, tzinfo=dt.timezone.utc)
        settings = {
            "aquarea_username": username,
            "aquarea_password": password,
            "device_status_max_age_minutes": "15",
            "poll_interval_seconds": "300",
        }
        with (
            patch(
                "packages.core.device_data_quality.get_setting",
                new=AsyncMock(side_effect=lambda key: settings[key]),
            ),
            patch(
                "packages.core.device_data_quality.get_session",
                return_value=_context(_quality_session(timestamp)),
            ),
        ):
            quality = await get_device_data_quality(now=now)
        assert reason in quality["reasons"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("layer", ["rules_only", "milp_preferred", "auto"])
    async def test_AC10_2_missing_credentials_block_rules_milp_and_fallback(self, layer):
        import importlib

        optimizer_main = importlib.import_module("packages.optimizer.main")

        optimizer = AsyncMock()
        with (
            patch.object(
                optimizer_main, "_select_optimizer", new=AsyncMock(return_value=(layer, optimizer))
            ),
            patch.object(optimizer_main, "get_setting", new=AsyncMock(return_value=layer)),
            patch.object(
                optimizer_main, "comfort_model", SimpleNamespace(arefresh_if_changed=AsyncMock())
            ),
            patch.object(
                optimizer_main,
                "get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["credentials_missing"]}),
            ),
        ):
            assert await optimizer_main.run_optimization() is None
        optimizer.generate_plan.assert_not_awaited()

    def test_AC10_2_planner_and_executor_use_exact_freshness_boundary(self):
        now = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
        threshold = 900
        assert device_status_is_fresh(
            now - dt.timedelta(seconds=threshold), now=now, threshold_seconds=threshold
        )
        assert not device_status_is_fresh(
            now - dt.timedelta(seconds=threshold + 1), now=now, threshold_seconds=threshold
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("age_seconds", "expected_fresh"), [(900, True), (901, False)])
    async def test_AC10_2_planner_gate_matches_executor_at_shared_threshold(
        self, age_seconds, expected_fresh
    ):
        import importlib

        frozen_now = dt.datetime(2026, 9, 24, 12, tzinfo=dt.timezone.utc)
        timestamp = frozen_now - dt.timedelta(seconds=age_seconds)
        settings = {
            "aquarea_username": "user",
            "aquarea_password": "password",
            "device_status_max_age_minutes": "15",
            "poll_interval_seconds": "300",
        }
        with (
            patch(
                "packages.core.device_data_quality.get_setting",
                new=AsyncMock(side_effect=lambda key: settings[key]),
            ),
            patch(
                "packages.core.device_data_quality.get_session",
                return_value=_context(_quality_session(timestamp)),
            ),
        ):
            quality = await get_device_data_quality(now=frozen_now)
        assert quality["ready"] is expected_fresh
        assert quality["threshold_seconds"] == 900

        optimizer_main = importlib.import_module("packages.optimizer.main")
        planner = AsyncMock()
        with (
            patch.object(
                optimizer_main, "_select_optimizer", new=AsyncMock(return_value=("rules", planner))
            ),
            patch.object(optimizer_main, "get_setting", new=AsyncMock(return_value="rules_only")),
            patch.object(
                optimizer_main, "comfort_model", SimpleNamespace(arefresh_if_changed=AsyncMock())
            ),
            patch.object(
                optimizer_main, "get_device_data_quality", new=AsyncMock(return_value=quality)
            ),
        ):
            await optimizer_main.run_optimization()
        assert planner.generate_plan.await_count == int(expected_fresh)

        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace()
        session = MagicMock(execute=AsyncMock(return_value=result))
        wrapper = AsyncMock()
        wrapper.get_selected_device_id.return_value = "device-a"
        executor = PlanExecutor(
            wrapper,
            session_factory=lambda: _context(session),
            learning_check=AsyncMock(return_value=False),
            device_quality_check=AsyncMock(return_value=quality),
        )

        class FrozenClock:
            @classmethod
            def now(cls, timezone=None):
                return frozen_now

        with patch("packages.optimizer.executor_core.dt.datetime", FrozenClock):
            precondition = await executor._dispatch_precondition(
                SimpleNamespace(device_id="device-a"), ActionType.FORCE_DHW_OFF, {}
            )

        if expected_fresh:
            assert precondition is None
            query = session.execute.await_args.args[0]
            assert (
                frozen_now - dt.timedelta(seconds=quality["threshold_seconds"])
                in query.compile().params.values()
            )
        else:
            assert precondition == {"reason": "device_status_stale", "device_id": "device-a"}
            session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_AC10_2_initial_and_periodic_training_skip_when_degraded(self):
        import importlib

        poller_main = importlib.import_module("packages.poller.main")

        model = SimpleNamespace(arefresh_if_changed=AsyncMock(), train=AsyncMock())
        with (
            patch.object(poller_main, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch(
                "packages.core.device_data_quality.get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["credentials_missing"]}),
            ),
            patch("packages.ml.comfort_model.comfort_model", model),
        ):
            await poller_main.retrain_comfort_model()
        model.train.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_AC10_2_periodic_training_skips_when_quality_check_fails(self):
        import importlib

        poller_main = importlib.import_module("packages.poller.main")
        model = SimpleNamespace(arefresh_if_changed=AsyncMock(), train=AsyncMock())
        with (
            patch.object(poller_main, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch(
                "packages.core.device_data_quality.get_device_data_quality",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("packages.ml.comfort_model.comfort_model", model),
        ):
            await poller_main.retrain_comfort_model()
        model.train.assert_not_awaited()

    def test_AC10_2_initial_training_is_after_device_quality_gate(self):
        import importlib

        source = inspect.getsource(importlib.import_module("packages.optimizer.main").main)
        assert source.index("device_quality = await get_device_data_quality()") < source.index(
            "comfort_model.train()"
        )
        assert 'if not device_quality["ready"]' in source

    @pytest.mark.asyncio
    async def test_AC10_3_freshness_blocked_action_returns_pending_without_rescheduling(self):
        action = SimpleNamespace(
            id=1,
            action_type=str(ActionType.SET_TANK_TEMP),
            scheduled_ts=dt.datetime(2026, 9, 24, 10, tzinfo=dt.timezone.utc),
        )
        update_session = MagicMock()
        update_session.execute = AsyncMock()
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=lambda: _context(update_session),
            learning_check=AsyncMock(return_value=False),
            device_quality_check=AsyncMock(),
        )
        await executor._defer_action(action, {"reason": "device_status_stale"})
        statement = update_session.execute.await_args.args[0]
        values = {key.key: value.value for key, value in statement._values.items()}
        assert values["status"] == "pending"
        assert "scheduled_ts" not in values

    @pytest.mark.asyncio
    async def test_AC10_2_quality_check_failure_defers_action_without_device_write(self):
        action = SimpleNamespace(
            id=1,
            action_type=str(ActionType.SET_TANK_TEMP),
            device_id="device-a",
            payload_json="{}",
        )
        session = MagicMock()
        session.execute = AsyncMock()
        wrapper = AsyncMock()
        wrapper.get_selected_device_id = AsyncMock(return_value="device-a")
        executor = PlanExecutor(
            wrapper,
            session_factory=lambda: _context(session),
            learning_check=AsyncMock(return_value=False),
            device_quality_check=AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        with patch("packages.optimizer.executor_core.get_action_handler") as get_handler:
            await executor._execute_action(action)

        get_handler.assert_not_called()
        statement = session.execute.await_args.args[0]
        values = {key.key: value.value for key, value in statement._values.items()}
        assert values["status"] == "pending"
        assert json.loads(values["result_json"])["reason"] == "quality_check_failed"

    def test_P2_AC13_degraded_expiry_preserves_only_linked_restores(self):
        from packages.core.safety_reverts import is_restorative_action

        assert is_restorative_action(
            SimpleNamespace(action_type=str(ActionType.FORCE_DHW_OFF), reverts_action_id=1)
        )
        assert not is_restorative_action(
            SimpleNamespace(action_type=str(ActionType.FORCE_DHW_OFF), reverts_action_id=None)
        )
        assert not is_restorative_action(
            SimpleNamespace(action_type="unknown_action", reverts_action_id=None)
        )

    @pytest.mark.asyncio
    async def test_P2_AC13_expiry_keeps_linked_restore_but_expires_untagged_actions(self):
        actions = [
            SimpleNamespace(
                id=index,
                action_type=action_type,
                reverts_action_id=1 if action_type == str(ActionType.FORCE_DHW_OFF) else None,
                scheduled_ts=dt.datetime(2026, 9, 24, 10, tzinfo=dt.timezone.utc),
            )
            for index, action_type in enumerate(
                [
                    str(ActionType.FORCE_DHW_OFF),
                    str(ActionType.FORCE_DHW_ON),
                    str(ActionType.SET_TANK_TEMP),
                    "unknown_action",
                ],
                start=1,
            )
        ]
        stale_result = MagicMock()
        stale_result.scalars.return_value.all.return_value = actions
        latest_result = MagicMock()
        latest_result.scalar_one_or_none.return_value = 1
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[stale_result, latest_result, *[MagicMock()] * 4])
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=lambda: _context(session),
            learning_check=AsyncMock(return_value=False),
            device_quality_check=AsyncMock(
                return_value={"ready": False, "reasons": ["device_status_stale"]}
            ),
        )
        with patch.object(
            PlanExecutor,
            "_diagnose_missed",
            new=AsyncMock(return_value={"reason": "overdue"}),
        ):
            await executor.expire_stale_actions()

        assert session.execute.await_count == 5
        update_calls = session.execute.await_args_list[2:]
        assert len(update_calls) == 3

    @pytest.mark.asyncio
    async def test_AC10_3_unresolvable_device_skips_before_quality_check(self):
        wrapper = AsyncMock()
        wrapper.get_selected_device_id.side_effect = RuntimeError("no device")
        quality = AsyncMock()
        executor = PlanExecutor(
            wrapper,
            session_factory=MagicMock(),
            learning_check=AsyncMock(return_value=False),
            device_quality_check=quality,
        )
        result = await executor._dispatch_precondition(
            SimpleNamespace(device_id="device-a"), ActionType.FORCE_DHW_ON, {}
        )
        assert result["reason"] == "action_device_unresolvable"
        quality.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_AC10_2_initial_training_skipped_when_quality_check_raises(self):
        import importlib

        optimizer_main = importlib.import_module("packages.optimizer.main")
        sentinel = "AC10_2_QUALITY_EXCEPTION_SENTINEL"
        comfort_model = SimpleNamespace(is_trained=False, train=AsyncMock())
        wrapper = AsyncMock()
        scheduler = MagicMock()
        shutdown_event = MagicMock()
        shutdown_event.wait = AsyncMock()

        with (
            patch.object(optimizer_main, "_load_ml_models"),
            patch("packages.core.logging.configure_logging"),
            patch.object(
                optimizer_main,
                "get_device_data_quality",
                new=AsyncMock(side_effect=RuntimeError(sentinel)),
            ),
            patch.object(optimizer_main, "comfort_model", comfort_model),
            patch.object(optimizer_main, "AquareaWrapper", return_value=wrapper),
            patch.object(optimizer_main, "create_scheduler", return_value=scheduler),
            patch.object(optimizer_main, "utc_after", return_value=object()),
            patch(
                "packages.core.service_health.record_service_heartbeat",
                new=AsyncMock(),
            ),
            patch.object(optimizer_main, "_shutdown_runtime", new=AsyncMock()),
            patch.object(optimizer_main.asyncio, "Event", return_value=shutdown_event),
        ):
            with capture_logs() as logs:
                await optimizer_main.main()

        comfort_model.train.assert_not_awaited()
        assert any(
            log.get("event") == "comfort_model_initial_training_quality_check_failed"
            and log.get("reason") == "quality_check_failed"
            for log in logs
        )
        assert sentinel not in repr(logs)
        wrapper.start.assert_awaited_once()
        scheduler.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_AC10_3_degraded_optimizer_does_not_supersede_a_plan(self):
        import importlib

        optimizer_main = importlib.import_module("packages.optimizer.main")

        with (
            patch.object(
                optimizer_main,
                "_select_optimizer",
                new=AsyncMock(return_value=("rules", AsyncMock())),
            ),
            patch.object(optimizer_main, "get_setting", new=AsyncMock(return_value="rules_only")),
            patch.object(
                optimizer_main, "comfort_model", SimpleNamespace(arefresh_if_changed=AsyncMock())
            ),
            patch.object(
                optimizer_main,
                "get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["device_status_stale"]}),
            ),
            patch.object(optimizer_main, "activate_plan") as activate_plan,
        ):
            assert await optimizer_main.run_optimization() is None
        activate_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_AC10_4_readiness_has_fields_and_never_returns_credential_values(self):
        now = dt.datetime.now(dt.timezone.utc)
        rows = [
            SimpleNamespace(service=service, updated_at=now)
            for service in ("poller", "optimizer", "backup")
        ]
        session_result = MagicMock()
        session_result.scalars.return_value.all.return_value = rows
        quality = {
            "ready": True,
            "timestamp": now,
            "age_seconds": 0,
            "threshold_seconds": 900,
            "credentials_configured": True,
            "reasons": [],
        }
        planning = {"control_allowed": True}
        with (
            patch(
                "packages.api.routers.models_router.get_session",
                return_value=_context(MagicMock(execute=AsyncMock(return_value=session_result))),
            ),
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                new=AsyncMock(return_value=planning),
            ),
            patch(
                "packages.core.device_data_quality.get_device_data_quality",
                new=AsyncMock(return_value=quality),
            ),
        ):
            body = await readiness()
        encoded = json.dumps(body, default=str)
        assert body["status"] == "ready"
        assert {"services", "data", "backup", "planning_data_quality"} <= body.keys()
        assert "aquarea_username" not in encoded
        assert "aquarea_password" not in encoded
        assert "never-print-this-panasonic-secret" not in encoded

    def test_AC10_5_refresh_is_integrated_without_a_new_scheduler_job_or_queue(self):
        import importlib

        optimizer_main = importlib.import_module("packages.optimizer.main")
        poller_main = importlib.import_module("packages.poller.main")

        optimizer_source = inspect.getsource(optimizer_main.main)
        poller_source = inspect.getsource(poller_main.main)
        assert "artifact_refresh" not in optimizer_source
        assert "artifact_refresh" not in poller_source
        assert "comfort_model_refresh" not in optimizer_source
        assert "comfort_model_refresh" not in poller_source

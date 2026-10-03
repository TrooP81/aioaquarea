"""Focused API contract tests for dashboard plan and optimization endpoints."""

from __future__ import annotations

import datetime as dt
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.schemas import DashboardResponse
from packages.api.routers.dashboard import get_dashboard
from packages.api.routers.dashboard import get_stats
from packages.api.routers.models_router import get_heat_curve_advice, get_thermal_curve
from packages.core.heat_curve import HeatCurveConfig
from packages.core.space_heating_gate import HeatingGateConfig
from packages.api.routers.optimizer import (
    get_optimizer_status,
    get_plan_detail,
    get_plans,
    get_optimization_request,
    optimize_now,
)
from packages.ml.model_status import clear_artifact_status_cache
from packages.ml.safe_persistence import safe_dump


def _session_context(session):
    class _AsyncContextManager:
        def __init__(self, value):
            self._value = value

        async def __aenter__(self):
            return self._value

        async def __aexit__(self, *args):
            return False

    return _AsyncContextManager(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("learning_state", "expected_learning_mode", "expected_reliable", "expected_plan_driven"),
    [
        ("active", True, True, False),
        ("inactive", False, True, True),
        ("unknown", False, False, False),
    ],
)
async def test_thermal_curve_projects_tri_state_learning_contract(
    learning_state, expected_learning_mode, expected_reliable, expected_plan_driven
):
    from packages.optimizer.executor_core import LearningModeState

    status = SimpleNamespace(tank_temp=48.0, tank_target_temp=52, outdoor_temp=7.0, zone1_temp=20.0)
    active_plan = SimpleNamespace(id=42)
    action = SimpleNamespace(
        action_type="force_dhw_on",
        scheduled_ts=dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0),
        payload_json=json.dumps({"dhw_minutes": 30}),
    )
    status_result = SimpleNamespace(scalar_one_or_none=lambda: status)
    plan_result = SimpleNamespace(scalar_one_or_none=lambda: active_plan)
    actions_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [action]))
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[status_result, plan_result, actions_result])
    )
    thermal_model = SimpleNamespace(
        load_latest=MagicMock(),
        predict_temperature_curve=MagicMock(return_value=[]),
        predict_planned_tank_curve=MagicMock(return_value=[]),
        predict_managed_tank_curve=MagicMock(return_value=[]),
    )
    with (
        patch(
            "packages.api.routers.models_router.get_session", return_value=_session_context(session)
        ),
        patch(
            "packages.core.settings_service.get_comfort_schedule", new=AsyncMock(return_value={})
        ),
        patch("packages.core.settings_service.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value=None)),
        patch(
            "packages.optimizer.executor_core.resolve_learning_mode_state",
            new=AsyncMock(return_value=LearningModeState(learning_state)),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
    ):
        response = await get_thermal_curve(hours=1)

    assert response["current"] == {
        "tank_temp": 48.0,
        "tank_target": 52,
        "outdoor_temp": 7.0,
        "zone1_temp": 20.0,
        "tank_min_temp": response["current"]["tank_min_temp"],
        "tank_min_temp_offpeak": response["current"]["tank_min_temp_offpeak"],
        "plan_driven": expected_plan_driven,
        "learning_mode": expected_learning_mode,
        "learning_mode_state": learning_state,
        "learning_mode_reliable": expected_reliable,
        "plan_id": 42,
    }


@pytest.mark.asyncio
async def test_thermal_curve_lookup_failure_returns_unknown_metadata():
    status = SimpleNamespace(
        id=1,
        tank_temp=48.0,
        tank_target_temp=52,
        outdoor_temp=7.0,
        zone1_temp=20.0,
    )
    status_result = SimpleNamespace(scalar_one_or_none=lambda: status)
    session = SimpleNamespace(execute=AsyncMock(return_value=status_result))
    thermal_model = SimpleNamespace(
        load_latest=MagicMock(),
        predict_temperature_curve=MagicMock(return_value=[]),
        predict_managed_tank_curve=MagicMock(return_value=[]),
    )
    with (
        patch(
            "packages.api.routers.models_router.get_session", return_value=_session_context(session)
        ),
        patch(
            "packages.core.settings_service.get_comfort_schedule", new=AsyncMock(return_value={})
        ),
        patch("packages.core.settings_service.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value=None)),
        patch(
            "packages.optimizer.executor_core.resolve_learning_mode_state",
            new=AsyncMock(side_effect=RuntimeError("unavailable")),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
    ):
        response = await get_thermal_curve(hours=1)

    assert response["current"]["learning_mode_state"] == "unknown"
    assert response["current"]["learning_mode"] is False
    assert response["current"]["learning_mode_reliable"] is False


def test_dashboard_response_serializes_space_heating_gate_evidence() -> None:
    response = DashboardResponse(
        space_heating_gate={
            "state": "BLOCKED",
            "reason": "above_off_threshold",
            "profile_id": "WH_MXC12J9E8_J_DEFAULT",
            "on_threshold_c": 13.0,
            "off_threshold_c": 15.0,
            "fingerprint_matches": True,
        }
    )

    assert response.model_dump(mode="json")["space_heating_gate"] == {
        "state": "BLOCKED",
        "reason": "above_off_threshold",
        "profile_id": "WH_MXC12J9E8_J_DEFAULT",
        "on_threshold_c": 13.0,
        "off_threshold_c": 15.0,
        "fingerprint_matches": True,
    }


@pytest.mark.asyncio
async def test_dashboard_uses_data_quality_threshold_for_status_freshness() -> None:
    empty_status_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    empty_price_result = SimpleNamespace(one_or_none=lambda: None)
    empty_consumption_result = SimpleNamespace(one_or_none=lambda: None)
    empty_records_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    empty_prices_result = SimpleNamespace(all=lambda: [])
    empty_plan_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    empty_override_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                empty_status_result,
                empty_price_result,
                empty_consumption_result,
                empty_records_result,
                empty_prices_result,
                empty_plan_result,
                empty_override_result,
            ]
        )
    )
    freshness_check = MagicMock(return_value=False)

    with (
        patch(
            "packages.api.routers.dashboard.get_device_data_quality",
            new=AsyncMock(return_value={"threshold_seconds": 900}),
        ) as get_device_data_quality,
        patch("packages.api.routers.dashboard.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.api.routers.dashboard.get_price_area", new=AsyncMock(return_value="FI")),
        patch(
            "packages.api.routers.dashboard.get_space_heating_gate_config",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "packages.api.routers.dashboard.resolve_effective_gate",
            return_value=SimpleNamespace(
                state="UNKNOWN",
                reason_code="no_data",
                profile_id=None,
                on_operator=None,
                off_operator=None,
                base_c=None,
                on_threshold_c=None,
                off_threshold_c=None,
                last_raw_outdoor_c=None,
                fingerprint_matches=None,
            ),
        ),
        patch("packages.api.routers.dashboard.get_session", return_value=_session_context(session)),
        patch(
            "packages.api.routers.dashboard.device_status_is_fresh",
            freshness_check,
        ),
    ):
        await get_dashboard()

    get_device_data_quality.assert_awaited_once()
    assert freshness_check.call_args.kwargs["threshold_seconds"] == 900
    dashboard_sql = [
        str(call.args[0])
        for call in session.execute.call_args_list
        if "source_date" in str(call.args[0])
    ]
    assert any("consumption.ts >=" in sql and "consumption.ts <=" in sql for sql in dashboard_sql)


@pytest.mark.asyncio
async def test_stats_source_date_grouping_keeps_timestamp_bounds() -> None:
    consumption_result = SimpleNamespace(one=lambda: (1.0, 2.0, 3.0))
    price_result = SimpleNamespace(scalar=lambda: 0.1)
    session = SimpleNamespace(execute=AsyncMock(side_effect=[consumption_result, price_result]))

    with patch(
        "packages.api.routers.dashboard.get_session", return_value=_session_context(session)
    ):
        response = await get_stats("day")

    assert response.total_kwh == 6.0
    stats_sql = str(session.execute.call_args_list[0].args[0])
    assert "source_date" in stats_sql
    assert "consumption.ts >=" in stats_sql
    assert "consumption.ts <=" in stats_sql


@pytest.mark.asyncio
async def test_optimizer_status_uses_filtered_demand_training_quality(tmp_path) -> None:
    quality = {
        "raw_records": 188,
        "intervals": 187,
        "usable_samples": 19,
        "minimum_samples": 168,
        "remaining_samples": 149,
        "ready_to_train": False,
    }
    count_result = MagicMock()
    count_result.scalar.return_value = 188
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = SimpleNamespace(
        params=SimpleNamespace(last_calibrated=None, tank_heating_rate=2.5)
    )

    with (
        patch(
            "packages.core.settings_service.get_setting",
            new=AsyncMock(return_value="rules_only"),
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(
                return_value={
                    "active_layer": "rules_v3",
                    "cop_trained": False,
                    "demand_trained": False,
                }
            ),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value=quality),
        ) as training_data_quality,
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch(
            "packages.api.routers.optimizer.get_session",
            return_value=_session_context(session),
        ),
    ):
        response = await get_optimizer_status()

    training_data_quality.assert_awaited_once_with()
    assert response["demand_model"]["data_quality"] == quality
    assert response["demand_model"]["samples"] == 0
    assert response["demand_model"]["source_records"] == 188


def _write_signed_artifact(path, payload) -> None:
    with patch(
        "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
    ):
        safe_dump(payload, path)


@pytest.mark.asyncio
async def test_optimizer_status_returns_consumption_evidence_and_artifact_metrics(tmp_path) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 42
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = SimpleNamespace(
        params=SimpleNamespace(last_calibrated=None, tank_heating_rate=2.5)
    )
    artifact_status = {
        "trained": True,
        "last_trained": "2026-10-01T12:00:00+00:00",
        "samples": 36,
        "metrics": {"mae": 0.123, "samples": 36},
        "unavailable_reason": None,
    }

    with (
        patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(return_value="rules_only")
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(return_value={"active_layer": "rules_v3"}),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch(
            "packages.ml.model_status._inspect_model_artifact",
            return_value=artifact_status,
        ),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value={"usable_samples": 2}),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
    ):
        response = await get_optimizer_status()

    assert response["cop_model"]["source_records"] == 42
    assert response["demand_model"]["source_records"] == 42
    assert response["cop_model"]["trained"] is True
    assert response["cop_model"]["last_trained"] == "2026-10-01T12:00:00+00:00"
    assert response["cop_model"]["metrics"] == {"mae": 0.123, "samples": 36}
    count_statement = session.execute.await_args.args[0]
    assert "consumption" in str(count_statement)


@pytest.mark.parametrize(
    ("model_kind", "filename", "payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v5_20261001_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"mae": 0.2, "samples": 17}},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"mae": 0.3, "samples": 23}},
        ),
    ],
)
def test_model_status_reads_newest_signed_artifact(model_kind, filename, payload, tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, payload)
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status == {
        "trained": True,
        "last_trained": "2026-10-01T12:00:00+00:00",
        "samples": payload["metrics"]["samples"],
        "metrics": payload["metrics"],
        "unavailable_reason": None,
    }


@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v5_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v3_20261001_1200.pkl"),
    ],
)
def test_model_status_reports_signature_failure_without_details(
    model_kind, filename, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    (tmp_path / filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    with patch("packages.ml.models.MODEL_DIR", tmp_path):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == "integrity_check_failed"
    assert str(tmp_path) not in str(status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v5_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v3_20261001_1200.pkl"),
    ],
)
async def test_optimizer_status_contains_safe_failure_for_bad_artifact(
    model_kind, filename, tmp_path
) -> None:
    (tmp_path / filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    count_result = MagicMock()
    count_result.scalar.return_value = 42
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = SimpleNamespace(
        params=SimpleNamespace(last_calibrated=None, tank_heating_rate=2.5)
    )

    with (
        patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(return_value="rules_only")
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(return_value={"active_layer": "rules_v3"}),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value={"usable_samples": 0}),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
    ):
        response = await get_optimizer_status()

    model_status = response[f"{model_kind}_model"]
    assert model_status["trained"] is False
    assert model_status["unavailable_reason"] == "integrity_check_failed"
    assert str(tmp_path) not in json.dumps(response)


@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v3_20261001_1200.pkl"),
        ("cop", "cop_model_weather_dhw_v4_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v2_20261001_1200.pkl"),
    ],
)
def test_model_status_ignores_pre_current_version_artifacts(model_kind, filename, tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, {"model": SimpleNamespace(n_features_in_=7)})
    clear_artifact_status_cache()
    status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == "not_found"


@pytest.mark.parametrize(
    ("model_kind", "older_filename", "newer_filename", "older_payload", "newer_payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v5_20261001_1200.pkl",
            "cop_model_weather_dhw_v5_20261002_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"samples": 17}},
            {"model": SimpleNamespace(n_features_in_=1)},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            "demand_model_weather_v3_20261002_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"samples": 23}},
            {"median": SimpleNamespace(n_features_in_=1)},
        ),
    ],
)
def test_model_status_uses_older_compatible_artifact_when_newer_is_incompatible(
    model_kind, older_filename, newer_filename, older_payload, newer_payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / older_filename, older_payload)
    _write_signed_artifact(tmp_path / newer_filename, newer_payload)
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status == {
        "trained": True,
        "last_trained": "2026-10-01T12:00:00+00:00",
        "samples": older_payload["metrics"]["samples"],
        "metrics": older_payload["metrics"],
        "unavailable_reason": None,
    }


@pytest.mark.parametrize(
    ("model_kind", "older_filename", "newer_filename", "older_payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v5_20261001_1200.pkl",
            "cop_model_weather_dhw_v5_20261002_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"samples": 17}},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            "demand_model_weather_v3_20261002_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"samples": 23}},
        ),
    ],
)
def test_model_status_uses_older_valid_artifact_when_newer_fails_integrity(
    model_kind, older_filename, newer_filename, older_payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / older_filename, older_payload)
    (tmp_path / newer_filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is True
    assert status["last_trained"] == "2026-10-01T12:00:00+00:00"
    assert status["metrics"] == older_payload["metrics"]
    assert status["unavailable_reason"] is None


@pytest.mark.parametrize(
    ("model_kind", "filename", "payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v5_20261001_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=1)},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=1)},
        ),
    ],
)
def test_model_status_reports_only_incompatible_artifact(
    model_kind, filename, payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, payload)
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == "incompatible_artifact"


def test_model_status_cache_reuses_snapshot_until_artifact_changes(tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    path = tmp_path / "cop_model_weather_dhw_v5_20261001_1200.pkl"
    path.write_bytes(b"first")
    clear_artifact_status_cache()
    loaded_metrics = iter(({"samples": 4}, {"samples": 9}))
    load_latest = MagicMock(return_value=True)
    fake_model = SimpleNamespace(
        load_latest=load_latest, version="20261001_1200", metrics={"samples": 0}
    )

    def load_changed_metrics():
        fake_model.metrics = next(loaded_metrics)
        return True

    load_latest.side_effect = load_changed_metrics
    with patch("packages.ml.models.COPModel", return_value=fake_model):
        first = _inspect_model_artifact("cop", tmp_path)
        second = _inspect_model_artifact("cop", tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
        third = _inspect_model_artifact("cop", tmp_path)

    assert first["metrics"] == {"samples": 4}
    assert second == first
    assert third["metrics"] == {"samples": 9}
    assert load_latest.call_count == 2


def test_model_status_cache_invalidates_when_an_artifact_is_added(tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    first_path = tmp_path / "cop_model_weather_dhw_v5_20261001_1200.pkl"
    first_path.write_bytes(b"first")
    clear_artifact_status_cache()
    fake_model = SimpleNamespace(version="20261001_1200", metrics={"samples": 4})
    fake_model.load_latest = MagicMock(return_value=True)

    with patch("packages.ml.models.COPModel", return_value=fake_model):
        first = _inspect_model_artifact("cop", tmp_path)
        (tmp_path / "cop_model_weather_dhw_v5_20261002_1200.pkl").write_bytes(b"second")
        fake_model.metrics = {"samples": 9}
        second = _inspect_model_artifact("cop", tmp_path)

    assert first["metrics"] == {"samples": 4}
    assert second["metrics"] == {"samples": 9}
    assert fake_model.load_latest.call_count == 2


@pytest.mark.asyncio
async def test_plan_list_preserves_lifecycle_and_price_context() -> None:
    plan = SimpleNamespace(
        id=7,
        created_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        horizon_start=dt.datetime(2026, 9, 14, 1, tzinfo=dt.timezone.utc),
        horizon_end=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
        optimizer_version="milp_v1+ml",
        cost_estimate_eur=3.21,
        price_currency="GBP",
        price_source="octopus",
        status="superseded",
        status_reason="replaced_by_manual_replan",
        superseded_at=dt.datetime(2026, 9, 14, 2, tzinfo=dt.timezone.utc),
        superseded_by_plan_id=8,
    )
    session = SimpleNamespace()
    result = MagicMock()
    result.all.return_value = [(plan, 4)]
    session.execute = AsyncMock(return_value=result)

    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        [response] = await get_plans(limit=10)

    assert response.model_dump() == {
        "id": 7,
        "created_at": plan.created_at,
        "horizon_start": plan.horizon_start,
        "horizon_end": plan.horizon_end,
        "optimizer_version": "milp_v1+ml",
        "cost_estimate_eur": 3.21,
        "price_currency": "GBP",
        "price_source": "octopus",
        "actions_count": 4,
        "status": "superseded",
        "status_reason": "replaced_by_manual_replan",
        "superseded_at": plan.superseded_at,
        "superseded_by_plan_id": 8,
    }


@pytest.mark.asyncio
async def test_plan_detail_preserves_provenance_and_change_summary() -> None:
    plan = SimpleNamespace(
        id=7,
        created_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        horizon_start=dt.datetime(2026, 9, 14, 1, tzinfo=dt.timezone.utc),
        horizon_end=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
        optimizer_version="rules_v3",
        cost_estimate_eur=2.5,
        price_currency="EUR",
        price_source="entsoe",
        status="active",
        status_reason=None,
        superseded_at=None,
        superseded_by_plan_id=None,
        plan_json=json.dumps({"change_summary": json.dumps({"reason": "price_shift"})}),
        input_provenance_json=json.dumps({"input_quality": {"price": {"fresh": True}}}),
    )
    action = SimpleNamespace(
        id=10,
        scheduled_ts=dt.datetime(2026, 9, 14, 3, tzinfo=dt.timezone.utc),
        action_type="quiet_mode_on",
        payload_json="{}",
        status="pending",
        executed_at=None,
        result_json=None,
    )
    session = SimpleNamespace()
    plan_result = SimpleNamespace(scalar_one_or_none=lambda: plan)
    actions_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [action]))
    session.execute = AsyncMock(side_effect=[plan_result, actions_result])

    with (
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
        patch(
            "packages.api.routers.optimizer.plan_measurement",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("packages.api._helpers.get_price_area", new_callable=AsyncMock, return_value="NL"),
        patch(
            "packages.core.settings_service.get_float_setting",
            new_callable=AsyncMock,
            return_value=18.0,
        ),
        patch(
            "packages.core.settings_service.get_user_tz", new_callable=AsyncMock, return_value="UTC"
        ),
    ):
        response = await get_plan_detail(7)

    assert response.change_summary == {"reason": "price_shift"}
    assert response.provenance == {"input_quality": {"price": {"fresh": True}}}
    assert response.price_source == "entsoe"


@pytest.mark.asyncio
async def test_optimize_now_enqueues_and_status_reports_durable_record() -> None:
    session = SimpleNamespace()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock()
    added = {}

    def assign_id(request):
        added["request"] = request

    async def flush():
        added["request"].id = 42

    session.add.side_effect = assign_id
    session.flush.side_effect = flush
    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        queued = await optimize_now()

    assert queued == {"status": "queued", "request_id": 42}
    request = added["request"]
    assert request.requested_by == "api"

    session.get.return_value = SimpleNamespace(
        id=42,
        status="completed",
        requested_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        started_at=dt.datetime(2026, 9, 14, 0, 1, tzinfo=dt.timezone.utc),
        completed_at=dt.datetime(2026, 9, 14, 0, 2, tzinfo=dt.timezone.utc),
        plan_id=9,
        error=None,
    )
    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        status = await get_optimization_request(42)

    assert status["status"] == "completed"
    assert status["plan_id"] == 9
    assert status["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "fingerprint_matches", "suggestion_available"),
    [
        ("ALLOWED", True, True),
        ("BLOCKED", True, False),
        ("UNKNOWN", True, False),
        ("ALLOWED", False, False),
    ],
    ids=["allowed", "blocked", "unknown", "fingerprint_mismatch"],
)
async def test_heat_curve_advice_uses_current_device_gate_and_fails_closed(
    state, fingerprint_matches, suggestion_available
) -> None:
    config = HeatingGateConfig()
    gate_row = SimpleNamespace(
        device_id="device-a",
        state=state,
        reason_code="test_gate_state",
        config_fingerprint=config.fingerprint if fingerprint_matches else "obsolete",
        last_raw_outdoor_c=5.0,
    )
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = SimpleNamespace(
        device_id="device-a", outdoor_temp=5.0
    )
    indoor_result = MagicMock()
    indoor_result.scalar.return_value = 18.0
    gate_result = MagicMock()
    gate_result.scalar_one_or_none.return_value = gate_row
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[status_result, indoor_result, gate_result])
    )

    with (
        patch(
            "packages.api.routers.models_router.get_session",
            return_value=_session_context(session),
        ),
        patch(
            "packages.core.settings_service.get_heat_curve_config",
            new=AsyncMock(return_value=HeatCurveConfig()),
        ),
        patch(
            "packages.core.settings_service.get_space_heating_gate_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "packages.core.settings_service.get_float_setting",
            new=AsyncMock(return_value=21.5),
        ),
        patch(
            "packages.core.settings_service.get_heat_curve_verification_state",
            new=AsyncMock(return_value={"status": "verified"}),
        ),
    ):
        response = await get_heat_curve_advice()

    gate_statement = session.execute.await_args_list[2].args[0]
    assert "device-a" in gate_statement.compile().params.values()
    assert (response["suggested"] is not None) is suggestion_available
    if suggestion_available:
        assert response["status"] == "too_cold"
        assert response["controllability"] == "heat_curve_effective"
    else:
        assert response["suggested"] is None
        assert response["status"] == "not_controllable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cop_count", "consumption_count", "expected"),
    [(49, 50, False), (50, 49, False), (50, 50, True)],
)
async def test_ml_data_gate_requires_both_current_thresholds(
    cop_count, consumption_count, expected
):
    from packages.optimizer.main import _has_sufficient_ml_data

    cop_result = MagicMock()
    cop_result.scalar.return_value = cop_count
    consumption_result = MagicMock()
    consumption_result.scalar.return_value = consumption_count
    session = SimpleNamespace(execute=AsyncMock(side_effect=[cop_result, consumption_result]))

    with patch("packages.optimizer.main.get_session", return_value=_session_context(session)):
        assert await _has_sufficient_ml_data() is expected

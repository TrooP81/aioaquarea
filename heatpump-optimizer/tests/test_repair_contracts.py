"""Localized observation-only API and utility repair contracts."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import NoScriptError


def test_R24_empty_installation_unavailable_forecast_is_schema_valid():
    from packages.api.routers.models_router import _numeric_forecast_unavailable
    from packages.api.schemas import IndoorForecastResponse

    payload = _numeric_forecast_unavailable("no_device_data")
    parsed = IndoorForecastResponse.model_validate(payload)
    assert parsed.forecast_status == "unavailable"
    assert parsed.forecast_unavailable_reason == "no_device_data"
    assert parsed.forecast == []
    assert payload["forecast_quality"]["control_allowed"] is False


async def test_R24_indoor_forecast_route_without_device_status_is_schema_valid(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from packages.api.routers import models_router
    from packages.api.schemas import IndoorForecastResponse
    from packages.core.control_temperature import ControlTemperature
    from tests.test_optimizer_api_contracts import _session_context

    async def execute(statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )

    thermal = SimpleNamespace(arefresh_if_changed=AsyncMock(return_value=False))
    monkeypatch.setattr("packages.ml.thermal.thermal_model", thermal)
    monkeypatch.setattr(
        models_router,
        "_read_live_forecast_quality",
        AsyncMock(return_value={"status": "fallback", "control_allowed": False}),
    )
    monkeypatch.setattr(models_router, "get_price_area", AsyncMock(return_value="SE3"))
    monkeypatch.setattr(
        models_router, "get_session", lambda: _session_context(SimpleNamespace(execute=execute))
    )
    monkeypatch.setattr(
        "packages.core.control_temperature.get_control_temperature",
        AsyncMock(
            return_value=ControlTemperature(
                value=None, confidence="none", sensor_count=0, sample_count=0, latest_reading=None
            )
        ),
    )
    app = FastAPI()
    app.include_router(models_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://r24.invalid",
    ) as client:
        response = await client.get("/api/thermal/indoor-forecast?hours=24")

    assert response.status_code == 200
    payload = response.json()
    assert "error" not in payload
    body = IndoorForecastResponse.model_validate(payload)
    for field in (
        "forecast",
        "forecast_with_plan",
        "forecast_no_heating",
        "target_schedule",
        "weather_forecast",
        "price_forecast",
        "planned_actions",
    ):
        assert payload[field] == [] and getattr(body, field) == []
    assert body.forecast_source == "unavailable"
    assert body.forecast_status == "unavailable"
    assert body.forecast_unavailable_reason == "no_device_data"
    assert payload["forecast_quality"]["control_allowed"] is False
    thermal.arefresh_if_changed.assert_awaited_once()


def test_migration035_is_nullable_additive_without_historical_backfill(monkeypatch):
    path = Path(__file__).parents[1] / "migrations" / "versions" / "035_feed_provenance.py"
    spec = importlib.util.spec_from_file_location("feed_provenance_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = SimpleNamespace(execute=Mock(), add_column=Mock(), drop_column=Mock())
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert migration.revision == "035"
    assert migration.down_revision == "034"
    operations.execute.assert_called_once_with("SET LOCAL lock_timeout = '5s'")
    assert [(call.args[0], call.args[1].name) for call in operations.add_column.call_args_list] == [
        ("consumption", "source_timezone"),
        ("weather", "units_schema"),
        ("prices", "interval_schema"),
    ]
    assert all(call.args[1].nullable for call in operations.add_column.call_args_list)
    assert all(call.args[1].server_default is None for call in operations.add_column.call_args_list)
    migration.downgrade()
    assert [call.args for call in operations.drop_column.call_args_list] == [
        ("prices", "interval_schema"),
        ("weather", "units_schema"),
        ("consumption", "source_timezone"),
    ]


@pytest.mark.parametrize("repeat_failure", [False, True])
async def test_R26_typed_noscript_reload_is_bounded(repeat_failure):
    from packages.core.resilience import DistributedReadQuota, ReadQuotaCategory

    results = [NoScriptError("No matching script. Please use EVAL.")]
    results.append(NoScriptError("still missing") if repeat_failure else [1, 20, 0])
    client = SimpleNamespace(
        script_load=AsyncMock(return_value="new-script"),
        evalsha=AsyncMock(side_effect=results),
    )
    quota = DistributedReadQuota(client)
    quota._script_sha = "old-script"
    if repeat_failure:
        with pytest.raises(NoScriptError):
            await quota._run("offline", 1, 0, ReadQuotaCategory.STATUS, snapshot=False)
    else:
        assert await quota._run("offline", 1, 0, ReadQuotaCategory.STATUS, snapshot=False) == [
            1,
            20,
            0,
        ]
    assert client.evalsha.await_count == 2
    client.script_load.assert_awaited_once()


def test_R31_signatures_compare_utc_instants():
    from packages.optimizer.main import _planned_action_signature, _stored_action_signature

    first = {"type": "force_dhw_on", "ts": "2026-10-12T10:00:00+02:00", "payload": {}}
    second = {**first, "ts": "2026-10-12T08:00:00Z"}
    stored = SimpleNamespace(
        action_type="force_dhw_on",
        scheduled_ts=dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc),
        payload_json="{}",
    )
    assert _planned_action_signature(first) == _planned_action_signature(second)
    assert _planned_action_signature(first) == _stored_action_signature(stored)


def test_R31_normalized_actions_are_ordered_and_deduplicated_by_utc():
    from packages.optimizer.rules_engine import RulesOptimizer

    actions = [
        {"type": "quiet_mode_on", "ts": "2026-10-12T09:30:00Z", "payload": {"level": 1}},
        {"type": "quiet_mode_off", "ts": "2026-10-12T10:00:00+02:00", "payload": {}},
        {"type": "force_dhw_on", "ts": "2026-10-12T10:00:00+02:00", "payload": {}},
        {"type": "force_dhw_on", "ts": "2026-10-12T08:00:00Z", "payload": {}},
    ]
    result = RulesOptimizer._normalise_actions(actions)
    assert len(result) == 3
    assert [action["type"] for action in result] == [
        "force_dhw_on",
        "quiet_mode_off",
        "quiet_mode_on",
    ]
    instants = [RulesOptimizer._action_timestamp(action) for action in result]
    assert instants == sorted(instants)


def test_R25_hardware_support_does_not_advertise_ordinary_automation():
    from packages.core.panasonic_capabilities import build_panasonic_capabilities

    now = dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc)
    status = SimpleNamespace(
        ts=now,
        device_id="offline",
        tank_temp=50,
        tank_heat_min=40,
        tank_heat_max=60,
        mode="HEAT",
    )
    capabilities = build_panasonic_capabilities(
        latest_status=status,
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )
    command = capabilities["commands"]["set_tank_temperature"]
    assert command["device_supported"] is True
    assert command["available"] is False
    assert command["policy"] == "observe_only_build"
    assert capabilities["availability"]["ordinary_automation_allowed"] is False


async def test_R22_usage_uses_configured_timezone(monkeypatch):
    from packages.core import settings_service

    timestamp = dt.datetime(2026, 10, 11, 22, tzinfo=dt.timezone.utc)

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(all=lambda: [(timestamp, "HEATING")])
                )
            )

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(settings_service, "get_session", Context)
    monkeypatch.setattr(settings_service, "get_user_tz", AsyncMock(return_value="Asia/Tokyo"))
    result = await settings_service.get_learned_usage()
    assert result["weekday"] == {7: 1.0}
    assert result["weekend"] == {}


@pytest.mark.parametrize(
    "deadline_hours,priced_hours,expected",
    [
        (2, 2, True),
        (1, 2, False),
        (2, 1, False),
    ],
)
def test_R27_ceil_runtime_requires_final_deadline_and_full_price_window(
    monkeypatch, deadline_hours, priced_hours, expected
):
    from packages.optimizer import rule_mixins
    from packages.optimizer.rules_engine import RulesOptimizer

    start = dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc)
    prediction = SimpleNamespace(
        estimated_hours=1.01,
        estimated_minutes=60.6,
        heating_rate_per_hour=5.0,
        confidence="assumed",
    )
    monkeypatch.setattr(
        rule_mixins.thermal_model, "predict_tank_heating_time", Mock(return_value=prediction)
    )
    monkeypatch.setattr(rule_mixins.thermal_model, "optimal_start_time", Mock(return_value=start))
    monkeypatch.setattr(rule_mixins.thermal_model, "_tank_loss_rate", Mock(return_value=0.0))
    optimizer = RulesOptimizer()
    monkeypatch.setattr(
        optimizer,
        "_local_dhw_deadlines_in_horizon",
        lambda *args, **kwargs: [(start + dt.timedelta(hours=deadline_hours), 10)],
    )
    prices = [(start + dt.timedelta(hours=h), 0.1) for h in range(priced_hours)]
    actions = optimizer._plan_dhw(
        prices, [(ts, 5.0) for ts, _ in prices], start, 40.0, 50, 5.0, {}, tz_name="UTC"
    )
    assert bool(actions) is expected
    if expected:
        assert len(actions) == 2
        assert dt.datetime.fromisoformat(actions[1]["ts"]) - dt.datetime.fromisoformat(
            actions[0]["ts"]
        ) == dt.timedelta(hours=2)


@pytest.mark.parametrize(
    "reason,with_context",
    [
        ("readiness_input_budget_exceeded", True),
        ("readiness_unavailable", True),
        ("readiness_busy", False),
    ],
)
async def test_RC_M1_unavailable_readiness_projects_full_reporting_status(reason, with_context):
    from packages.core.settings_service import SETTING_SPECS
    from packages.ml import model_status
    from packages.ml.seasonal_learning import get_seasonal_calibration_progress

    as_of = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    context = (
        {
            "settings": {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()},
            "as_of": as_of,
        }
        if with_context
        else None
    )
    snapshot = model_status._unavailable_readiness(
        reason, as_of=as_of, generation="rc-m1", context=context
    )
    for component in snapshot["readiness"]["components"].values():
        reporting = component["reporting"]
        assert reporting["status"] == "unavailable"
        assert reporting["reason"] == reason
        assert reporting["counts"] is None
        assert {
            "missing_counts",
            "exclusion_counts",
            "pending_outcomes",
            "evidence_reason",
            "artifact_hash",
            "scope",
            "current_scope_match",
        } <= set(reporting)

    progress = await get_seasonal_calibration_progress(snapshot=snapshot)

    assert progress["reporting_ready"] is False
    assert progress["control_ready"] is False
    assert progress["next_step"] == "readiness_unavailable"
    assert progress["demand"]["usable_samples"] is None
    assert progress["demand"]["trained"] is False
    assert progress["indoor_heating"]["samples"] is None
    assert progress["readiness"]["availability"] == "unavailable"
    assert progress["readiness"]["reason"] == reason
    if with_context:
        assert progress["reason"] == "seasonal_reporting_unavailable"
        assert progress["heating_season_detected"] is None
    else:
        assert progress["enabled"] is None
        assert progress["observe_only_active"] is None

"""Focused API contract tests for dashboard plan and optimization endpoints."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.schemas import DashboardResponse
from packages.api.routers.dashboard import get_dashboard
from packages.api.routers.models_router import get_heat_curve_advice
from packages.core.heat_curve import HeatCurveConfig
from packages.core.space_heating_gate import HeatingGateConfig
from packages.api.routers.optimizer import (
    get_optimizer_status,
    get_plan_detail,
    get_plans,
    get_optimization_request,
    optimize_now,
)


def _session_context(session):
    class _AsyncContextManager:
        def __init__(self, value):
            self._value = value

        async def __aenter__(self):
            return self._value

        async def __aexit__(self, *args):
            return False

    return _AsyncContextManager(session)


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
    assert response["demand_model"]["samples"] == 19


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

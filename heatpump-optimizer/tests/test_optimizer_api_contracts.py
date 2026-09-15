"""Focused API contract tests for dashboard plan and optimization endpoints."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.routers.optimizer import (
    get_plan_detail,
    get_plans,
    get_optimization_request,
    optimize_now,
)


def _session_context(session):
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


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
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [(plan, 4)]
    session.execute.return_value = result

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
    session = AsyncMock()
    plan_result = SimpleNamespace(scalar_one_or_none=lambda: plan)
    actions_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [action]))
    session.execute.side_effect = [plan_result, actions_result]

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
    session = MagicMock()
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

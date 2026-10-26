from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from packages.core.comfort_schedule import sample_control_target
from packages.core.house_contract import (
    HousePlanningContract,
    _contract_hash,
    snapshot_from_revision,
    validate_plan_contract_snapshot,
)

UTC = dt.timezone.utc


@pytest.mark.parametrize(
    ("zone", "instant", "hours"),
    [
        ("Asia/Kolkata", dt.datetime(2026, 10, 18, 23, 10, tzinfo=UTC), [6]),
        ("Asia/Kathmandu", dt.datetime(2026, 10, 18, 23, 10, tzinfo=UTC), [6]),
        ("Australia/Lord_Howe", dt.datetime(2026, 4, 4, 13, 10, tzinfo=UTC), [1]),
    ],
)
@pytest.mark.parametrize("saved_plan_status", ["active", "diagnostic"])
async def test_fractional_policy_api_prediction_assessment_and_saved_window(
    monkeypatch, zone, instant, hours, saved_plan_status
):
    from packages.core.comfort_assessment import build_comfort_assessment
    from packages.core.heat_curve import HeatCurveConfig
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason

    projection = HousePlanningContract.from_settings(
        {
            "timezone": zone,
            "comfort_schedule": json.dumps({"weekday": hours, "weekend": hours}),
            "comfort_temp_min": "20.05",
            "comfort_temp_target": "20.55",
            "comfort_temp_max": "25",
        },
        UUID(int=1),
        UUID(int=2),
    ).to_projection()
    call = TestIndoorForecastApiLimitationReason.call_api
    live = await call(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=17,
        contract=projection,
        instant=instant,
    )
    assert live["forecast_source"] == "live_estimate"
    assert len(live["forecast_with_plan"]) == 2
    policy = live["target_schedule"]
    assert len(policy) > 2  # Exact policy is not the prediction/control grid.
    expected = [
        sample_control_target(policy, dt.datetime.fromisoformat(point["interval_start_utc"]))
        for point in live["forecast_with_plan"]
    ]
    assert [point["control_target_c"] for point in live["forecast_with_plan"]] == [
        row["target"] for row in expected
    ]
    assert live["comfort_assessment"]["misses"]
    for miss in live["comfort_assessment"]["misses"]:
        assert miss["target_c"] == round(expected[miss["forecast_index"]]["target"], 1)
    snapshot = {
        **live,
        "version": "indoor_forecast_v5",
        "forecast_status": "available",
    }
    cached = await call(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=17,
        contract=projection,
        instant=instant,
        saved_snapshot=snapshot,
        saved_plan_status=saved_plan_status,
    )
    assert cached["forecast_source"] == (
        "diagnostic_plan" if saved_plan_status == "diagnostic" else "active_plan"
    )
    assert cached["target_schedule"] == policy
    for saved_miss, live_miss in zip(
        cached["comfort_assessment"]["misses"], live["comfort_assessment"]["misses"], strict=True
    ):
        assert dt.datetime.fromisoformat(saved_miss["ts"]) == dt.datetime.fromisoformat(
            live_miss["ts"]
        )
        assert {k: v for k, v in saved_miss.items() if k != "ts"} == {
            k: v for k, v in live_miss.items() if k != "ts"
        }
    refused = await call(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=17,
        contract=projection,
        instant=instant,
        saved_snapshot=snapshot,
        saved_plan_status=saved_plan_status,
        thermal_context_current=False,
    )
    assert refused["forecast_source"] == "unavailable"
    assert refused["forecast_unavailable_reason"] == ("thermal_dependency_changed_during_forecast")
    # Old saved producers label an hour by its end, without explicit slot fields.
    legacy_points = deepcopy(live["forecast_with_plan"])
    for point in legacy_points:
        point.pop("interval_start_utc")
        point.pop("interval_end_utc")
    assessment = build_comfort_assessment(
        forecast=legacy_points,
        targets=policy,
        weather=live["weather_forecast"],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
    )
    for miss in assessment["misses"]:
        assert miss["target_c"] == round(expected[miss["forecast_index"]]["target"], 1)
    from packages.api.routers.models_router import _plan_forecast_window

    nullable_snapshot = deepcopy(snapshot)
    for name in ("forecast", "forecast_with_plan", "forecast_no_heating"):
        for point in nullable_snapshot[name]:
            point["interval_start_utc"] = None
            point["interval_end_utc"] = None
    start = instant.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    revision = SimpleNamespace(
        id=1,
        contract_hash=_contract_hash(projection),
        contract_json=projection,
        effective_at=instant,
        source="fixture",
    )
    authority = await snapshot_from_revision(
        SimpleNamespace(get=AsyncMock(return_value=revision)),
        1,
        start,
        start + dt.timedelta(hours=2),
    )
    plan = SimpleNamespace(
        plan_json=json.dumps(
            {"forecast_snapshot": nullable_snapshot, "house_contract_snapshot": authority}
        ),
        contract_revision_id=1,
        contract_hash=revision.contract_hash,
        horizon_start=start,
        horizon_end=start + dt.timedelta(hours=2),
    )
    window = _plan_forecast_window(plan, start, 2)
    assert window is not None and window["target_schedule"] == policy


async def contract_plan():
    from packages.core.models import PlanRecord

    start = dt.datetime(2026, 10, 25, tzinfo=UTC)
    projection = HousePlanningContract.from_settings(
        {
            "timezone": "Europe/Stockholm",
            "comfort_schedule": '{"weekday":[2],"weekend":[2]}',
            "comfort_temp_min": "18",
            "comfort_temp_target": "21",
            "comfort_temp_max": "25",
        },
        UUID(int=1),
        UUID(int=2),
    ).to_projection()
    revision = SimpleNamespace(
        id=4,
        contract_hash=_contract_hash(projection),
        contract_json=projection,
        effective_at=start,
        source="repair6_test",
    )
    plan = PlanRecord(
        id=71,
        created_at=start,
        horizon_start=start,
        horizon_end=start + dt.timedelta(hours=3),
        optimizer_version="rules_v8",
        price_source="entsoe",
        price_currency="EUR",
        input_provenance_json="{}",
        status="diagnostic",
        contract_revision_id=4,
        contract_hash=revision.contract_hash,
        contract_state="stale",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=revision))
    snapshot = await snapshot_from_revision(session, 4, plan.horizon_start, plan.horizon_end)
    plan.plan_json = json.dumps({"house_contract_snapshot": snapshot})
    return plan, snapshot, revision


@pytest.mark.parametrize(
    "mutation", ["coherent_offset", "fold", "membership", "local_end", "boundary"]
)
async def test_full_projection_corruption_fails_detail_diagnostic_history_and_measurement(
    monkeypatch, mutation
):
    from packages.api.routers import optimizer as routes
    from packages.core import house_contract
    from packages.core.plan_outcome import plan_measurement

    plan, snapshot, revision = await contract_plan()
    assert validate_plan_contract_snapshot(plan, snapshot)
    interval = snapshot["schedule_projection"][0]
    if mutation == "coherent_offset":
        # A self-consistent +03 timezone interpretation is still not Stockholm.
        for row in snapshot["schedule_projection"]:
            for field in ("local_start", "local_end"):
                row[field] = (
                    dt.datetime.fromisoformat(row[field])
                    .astimezone(dt.timezone(dt.timedelta(hours=3)))
                    .isoformat()
                )
            local = dt.datetime.fromisoformat(row["local_start"])
            row.update(
                local_hour=local.hour,
                local_date=local.date().isoformat(),
                utc_offset_minutes=180,
                fold=0,
                scheduled=local.hour == 2,
                preferred_comfort=local.hour == 2,
                target_c=21.0 if local.hour == 2 else 18.0,
            )
    elif mutation == "fold":
        snapshot["schedule_projection"][1]["fold"] = 0
    elif mutation == "membership":
        interval.update(scheduled=False, preferred_comfort=False, target_c=18.0)
    elif mutation == "local_end":
        interval["local_end"] = (
            dt.datetime.fromisoformat(interval["local_end"])
            .astimezone(dt.timezone(dt.timedelta(hours=3)))
            .isoformat()
        )
    else:
        snapshot["schedule_projection"][0]["end_utc"] = (
            plan.horizon_start + dt.timedelta(minutes=30)
        ).isoformat()
        snapshot["schedule_projection"][1]["start_utc"] = snapshot["schedule_projection"][0][
            "end_utc"
        ]
    assert not validate_plan_contract_snapshot(plan, snapshot)
    plan.plan_json = json.dumps({"house_contract_snapshot": snapshot})
    measurement = AsyncMock(side_effect=AssertionError("corrupt projection must not be measured"))
    session = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(house_contract, "ensure_current_contract", AsyncMock(return_value=revision))
    monkeypatch.setattr(routes, "get_session", transaction)
    monkeypatch.setattr(routes, "plan_measurement", measurement)
    for status in ("diagnostic", "active"):
        plan.status = status
        session.execute = AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: plan, scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        )
        response = await routes.get_plan_detail(71, include_diagnostics=True)
        assert response.outcome["state"] == "unknown"
        assert response.outcome["contract_snapshot_state"] == "corrupt"
    session.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [(plan, 0)], scalars=lambda: SimpleNamespace(all=lambda: [plan])
        )
    )
    history = await routes.get_plans(limit=10, include_diagnostics=True)
    assert history[0].contract_snapshot_state == "unknown"
    assert history[0].is_current is False
    measurement.assert_not_awaited()
    result = await plan_measurement(
        SimpleNamespace(),
        plan=plan,
        price_area="",
        comfort_min_c=29,
        comfort_max_c=30,
        timezone_name="UTC",
    )
    assert result["state"] == "unknown"


async def test_saved_tzdata_version_never_reinterpreted_with_current_rules(monkeypatch):
    from packages.core import house_contract

    plan, snapshot, _ = await contract_plan()
    monkeypatch.setattr(house_contract, "timezone_data_version", lambda: "system:future-test")
    resolver = AsyncMock(side_effect=AssertionError("must not reinterpret historical policy"))
    monkeypatch.setattr(house_contract, "resolve_hourly_targets", resolver)
    assert not validate_plan_contract_snapshot(plan, snapshot)
    resolver.assert_not_called()


async def test_historical_unavailable_tzdata_is_unknown_not_corrupt(monkeypatch):
    from packages.api.routers import optimizer as routes
    from packages.core import house_contract, comfort_schedule

    plan, snapshot, revision = await contract_plan()
    monkeypatch.setattr(house_contract, "timezone_data_version", lambda: "system:future-test")
    monkeypatch.setattr(comfort_schedule, "timezone_data_version", lambda: "system:future-test")
    monkeypatch.setattr(house_contract, "ensure_current_contract", AsyncMock(return_value=revision))
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: plan))
    )

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(routes, "get_session", transaction)
    result = await routes.get_plan_detail(plan.id, include_diagnostics=True)
    assert result.outcome["state"] == "unknown"
    assert result.outcome["reason"] == "historical_timezone_data_unavailable"
    assert result.contract_snapshot_state == "timezone_data_unavailable"

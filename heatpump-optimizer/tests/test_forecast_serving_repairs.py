from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json

import pytest

from packages.api.routers.models_router import _plan_forecast_window
from packages.core.house_contract import canonical_target_schedule
from tests.test_p21_repair6 import contract_plan


async def saved_plan():
    plan, authority, _ = await contract_plan()
    start = plan.horizon_start
    points = [
        {"ts": (start + dt.timedelta(hours=i + 1)).isoformat(), "predicted_indoor_temp": 20.0}
        for i in range(3)
    ]
    snapshot = {
        "version": "indoor_forecast_v5",
        "forecast_status": "available",
        "current_indoor": 20.0,
        "target_schedule": canonical_target_schedule(authority),
        **{
            key: deepcopy(points)
            for key in ("forecast", "forecast_with_plan", "forecast_no_heating")
        },
        **{
            key: [{"ts": (start + dt.timedelta(hours=i)).isoformat()} for i in range(3)]
            for key in ("weather_forecast", "price_forecast")
        },
    }
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    return plan, authority, snapshot


async def test_advanced_window_uses_interval_start_and_requires_tail():
    plan, authority, snapshot = await saved_plan()
    start = plan.horizon_start + dt.timedelta(hours=1)
    window = _plan_forecast_window(plan, start, 2)
    assert window is not None
    assert window["forecast_with_plan"][0]["ts"] == (start + dt.timedelta(hours=1)).isoformat()
    for key in (
        "forecast",
        "forecast_with_plan",
        "forecast_no_heating",
        "weather_forecast",
        "price_forecast",
    ):
        snapshot[key].pop()
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    assert _plan_forecast_window(plan, start, 2) is None


@pytest.mark.parametrize(
    "corruption",
    [
        "owner",
        "projection",
        "target",
        "reported",
        "gap",
        "duplicate",
        "weather",
        "partial",
        "unavailable",
    ],
)
async def test_corrupt_saved_forecast_is_rejected(corruption):
    plan, authority, snapshot = await saved_plan()
    if corruption == "owner":
        plan.contract_hash = "f" * 64
    elif corruption == "projection":
        authority["schedule_projection"][0]["target_c"] += 1
    elif corruption == "target":
        snapshot["target_schedule"][0]["target"] += 1
    elif corruption == "reported":
        snapshot["forecast"][0]["control_target_c"] = 29
    elif corruption == "gap":
        snapshot["forecast_with_plan"].pop(1)
    elif corruption == "duplicate":
        snapshot["forecast_with_plan"][1] = snapshot["forecast_with_plan"][0]
    elif corruption == "weather":
        snapshot["weather_forecast"][0]["ts"] = snapshot["weather_forecast"][1]["ts"]
    elif corruption == "partial":
        snapshot["forecast"][0]["interval_start_utc"] = plan.horizon_start.isoformat()
    else:
        snapshot["forecast_status"] = "unavailable"
        authority.clear()
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    assert _plan_forecast_window(plan, plan.horizon_start, 2) is None


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_provider_isolation_in_live_and_rejected_plan_fallback(
    monkeypatch, missing, rejected, reverse
):
    from types import SimpleNamespace
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    now = dt.datetime(2026, 10, 23, 12, 10, tzinfo=dt.timezone.utc)
    start = now.replace(minute=0) + dt.timedelta(hours=1)

    def weather(source, temp):
        return [
            SimpleNamespace(
                source=source,
                ts=start + dt.timedelta(hours=i),
                temperature=temp,
                wind_speed=2,
                irradiance=0,
                precipitation=0,
            )
            for i in range(2)
        ]

    selected = [] if missing else weather("smhi", -10)
    kwargs = dict(
        quality={"control_allowed": False, "reason": "stale_weather"},
        indoor_value=20,
        instant=now,
        provider="smhi",
    )
    baseline = await Fixture.call_api(monkeypatch, weather_rows=selected, **kwargs)
    rows = selected + weather("open-meteo", 40)
    if reverse:
        rows.reverse()
    result = await Fixture.call_api(
        monkeypatch,
        weather_rows=rows,
        saved_snapshot={**baseline, "version": "indoor_forecast_v5"} if rejected else None,
        corrupt_owner=rejected,
        **kwargs,
    )
    assert result["forecast_source"] == "live_estimate"
    assert result["weather_forecast"] == baseline["weather_forecast"]
    assert result["forecast_with_plan"] == baseline["forecast_with_plan"]
    assert result["forecast_no_heating"] == baseline["forecast_no_heating"]
    assert result["comfort_assessment"] == baseline["comfort_assessment"]
    assert result["forecast_quality"]["reason"] == "stale_weather"
    assert result["weather_forecast"][0]["input_status"] == ("imputed" if missing else "observed")
    assert result["weather_forecast"][0]["outdoor_temp"] == (5 if missing else -10)


@pytest.mark.parametrize("learned", [False, True])
@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("change", ["sensor", "aba", "same", "missing"])
@pytest.mark.parametrize("indoor", [20, None])
async def test_held_default_forecast_fenced_by_persisted_generation(
    monkeypatch, saved, change, indoor, learned
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import UUID
    from packages.core.control_temperature import ControlTemperature
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    now = dt.datetime(2026, 10, 23, 12, 10, tzinfo=dt.timezone.utc)
    baseline = await Fixture.call_api(
        monkeypatch, quality={"control_allowed": True}, indoor_value=20, instant=now
    )
    held, release = asyncio.Event(), asyncio.Event()
    state = {"generation": 1}

    async def installation(*args):
        return SimpleNamespace(
            installation_id=UUID(int=1), indoor_dependency_generation=state["generation"]
        )

    async def read_sensor(**kwargs):
        # Initial generation must have been captured before old sensor inputs.
        assert getter.await_count == 1
        held.set()
        await asyncio.wait_for(release.wait(), 5)
        return ControlTemperature(
            value=indoor, confidence="high", sensor_count=1, sample_count=1, latest_reading=now
        )

    getter = AsyncMock(side_effect=installation)
    task = asyncio.create_task(
        Fixture.call_api(
            monkeypatch,
            quality={"control_allowed": True},
            indoor_value=indoor,
            instant=now,
            saved_snapshot={**baseline, "version": "indoor_forecast_v5"} if saved else None,
            input_hook=read_sensor,
            installation_get=getter,
            learned_context=learned,
        )
    )
    try:
        await asyncio.wait_for(held.wait(), 5)
        state["generation"] = {"sensor": 2, "aba": 3, "same": 1, "missing": None}[change]
        release.set()
        result = await asyncio.wait_for(task, 5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (
        getter.await_count == 2
    )  # Fresh persisted read, even for default model / no trusted sensor.
    if change == "same":
        assert result["forecast_unavailable_reason"] != "indoor_dependency_changed_during_forecast"
    else:
        assert result["forecast_unavailable_reason"] == "indoor_dependency_changed_during_forecast"
        assert result["forecast"] == []
        assert result["weather_forecast"] == []
        assert result["current_indoor"] is None


@pytest.mark.parametrize("mutation", ["owner", "nested", "reported", "schema"])
async def test_corrupt_forecast_endpoint_returns_qualified_fallback(monkeypatch, mutation):
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    now = dt.datetime(2026, 10, 23, 12, 10, tzinfo=dt.timezone.utc)
    baseline = await Fixture.call_api(
        monkeypatch, quality={"control_allowed": True}, indoor_value=20, instant=now
    )
    snapshot = {**deepcopy(baseline), "version": "indoor_forecast_v5"}
    if mutation == "nested":
        snapshot["target_schedule"][0]["contract_hash"] = "f" * 64
    elif mutation == "reported":
        snapshot["forecast_with_plan"][0]["control_target_c"] = 29
    elif mutation == "schema":
        snapshot["weather_forecast"][0]["imputed_fields"] = 12
    result = await Fixture.call_api(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=20,
        instant=now,
        saved_snapshot=snapshot,
        corrupt_owner=mutation == "owner",
    )
    assert result["forecast_source"] == "live_estimate"
    assert result["target_schedule"] == baseline["target_schedule"]
    assert result["forecast_with_plan"] == baseline["forecast_with_plan"]


@pytest.mark.parametrize("missing_final", [False, True])
async def test_clock_advanced_endpoint_requires_exact_contiguous_tail(monkeypatch, missing_final):
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    now = dt.datetime(2026, 10, 23, 12, 10, tzinfo=dt.timezone.utc)
    start = now.replace(minute=0) + dt.timedelta(hours=1)
    baseline = await Fixture.call_api(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=20,
        instant=now,
        hours=2 if missing_final else 3,
    )
    result = await Fixture.call_api(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=20,
        instant=now + dt.timedelta(hours=1),
        owner_start=start,
        saved_snapshot={**baseline, "version": "indoor_forecast_v5"},
    )
    assert result["forecast_source"] == ("live_estimate" if missing_final else "active_plan")
    assert dt.datetime.fromisoformat(result["forecast_with_plan"][0]["ts"]) == start + dt.timedelta(
        hours=2
    )
    assert len(result["forecast_with_plan"]) == 2


async def test_cancelled_request_releases_held_sensor_task(monkeypatch):
    import asyncio
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    held, cancelled = asyncio.Event(), asyncio.Event()

    async def sensor(**kwargs):
        held.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        Fixture.call_api(
            monkeypatch, quality={"control_allowed": False}, indoor_value=20, input_hook=sensor
        )
    )
    try:
        await asyncio.wait_for(held.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert cancelled.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "policy,valid",
    [("slot_start_utc", True), ("utc-hour-slot-start-v1", True), ("slot_end_utc", False)],
)
async def test_reported_sampling_must_mean_authoritative_slot_start(policy, valid):
    plan, authority, snapshot = await saved_plan()
    from packages.core.comfort_schedule import sample_control_target

    targets = canonical_target_schedule(authority)
    for key in ("forecast", "forecast_with_plan", "forecast_no_heating"):
        for point in snapshot[key]:
            start = dt.datetime.fromisoformat(point["ts"]) - dt.timedelta(hours=1)
            point["control_target_c"] = sample_control_target(targets, start)["target"]
            point["control_sampling_policy"] = policy
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    assert (_plan_forecast_window(plan, plan.horizon_start, 2) is not None) is valid


@pytest.mark.parametrize("generation", [None, True, -1, "1"])
async def test_missing_or_invalid_initial_generation_never_reads_sensor(monkeypatch, generation):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import UUID
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    sensor = AsyncMock(side_effect=AssertionError("unqualified input read"))
    result = await Fixture.call_api(
        monkeypatch,
        quality={},
        indoor_value=20,
        input_hook=sensor,
        installation_get=AsyncMock(
            return_value=SimpleNamespace(
                installation_id=UUID(int=1), indoor_dependency_generation=generation
            )
        ),
    )
    assert result["forecast_unavailable_reason"] == "indoor_dependency_changed_during_forecast"
    sensor.assert_not_awaited()


async def test_unreadable_final_generation_fails_closed(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import UUID
    from sqlalchemy.exc import SQLAlchemyError
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    result = await Fixture.call_api(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=20,
        installation_get=AsyncMock(
            side_effect=[
                SimpleNamespace(installation_id=UUID(int=1), indoor_dependency_generation=1),
                SQLAlchemyError("synthetic read failure"),
            ]
        ),
    )
    assert result["forecast_unavailable_reason"] == "indoor_dependency_changed_during_forecast"
    assert result["forecast"] == []


async def test_partial_nested_target_bounds_fail_closed():
    plan, authority, snapshot = await saved_plan()
    for row in snapshot["target_schedule"]:
        row.pop("interval_start_utc")
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    assert _plan_forecast_window(plan, plan.horizon_start, 2) is None

"""Independent AC-F02/F03 boundary regressions; offline ASGI, no device or DB."""

from copy import deepcopy
import datetime as dt
import json

import pytest

from packages.api.routers.models_router import _plan_forecast_window
from tests.test_forecast_serving_repairs import saved_plan


@pytest.mark.parametrize("explicit", [False, True])
async def test_one_slot_window_excludes_interval_ending_at_start(explicit):
    plan, authority, snapshot = await saved_plan()
    if explicit:
        for key in ("forecast", "forecast_with_plan", "forecast_no_heating"):
            for point in snapshot[key]:
                end = dt.datetime.fromisoformat(point["ts"])
                point["interval_start_utc"] = (end - dt.timedelta(hours=1)).isoformat()
                point["interval_end_utc"] = end.isoformat().replace("+00:00", "Z")
    plan.plan_json = json.dumps(
        {"house_contract_snapshot": authority, "forecast_snapshot": snapshot}
    )
    start = plan.horizon_start + dt.timedelta(hours=1)
    result = _plan_forecast_window(plan, start, 1)
    assert result is not None
    assert len(result["forecast_with_plan"]) == 1
    assert dt.datetime.fromisoformat(result["forecast_with_plan"][0]["ts"]) == (
        start + dt.timedelta(hours=1)
    )
    assert dt.datetime.fromisoformat(result["weather_forecast"][0]["ts"]) == start


@pytest.mark.parametrize("corrupt_ts", ["0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00+01:00"])
async def test_corrupt_saved_timestamp_boundary_returns_fallback(monkeypatch, corrupt_ts):
    from tests.test_comfort_assessment import TestIndoorForecastApiLimitationReason as Fixture

    now = dt.datetime(2026, 10, 23, 12, 10, tzinfo=dt.timezone.utc)
    baseline = await Fixture.call_api(
        monkeypatch, quality={"control_allowed": True}, indoor_value=20, instant=now
    )
    snapshot = {**deepcopy(baseline), "version": "indoor_forecast_v5"}
    point = snapshot["forecast"][0]
    point["ts"] = corrupt_ts
    point.pop("interval_start_utc", None)
    point.pop("interval_end_utc", None)
    result = await Fixture.call_api(
        monkeypatch,
        quality={"control_allowed": True},
        indoor_value=20,
        instant=now,
        saved_snapshot=snapshot,
    )
    assert result["forecast_source"] == "live_estimate"
    assert result["forecast_with_plan"] == baseline["forecast_with_plan"]
    assert result["target_schedule"] == baseline["target_schedule"]
    assert result["ordinary_writes_allowed"] is False

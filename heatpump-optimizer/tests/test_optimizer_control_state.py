from __future__ import annotations

import datetime as dt
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.learning_state import LearningStateSnapshot
from packages.core.optimizer_control_state import (
    ControlStateOverrideUnavailableError,
    _next_action_is_embargoed,
    _next_pending_action_embargoed,
    get_controlling_override,
    resolve_control_state,
)
from packages.api.schemas import ControlStateResponse


@pytest.fixture(autouse=True)
def reset_dashboard_comfort_assessment_cache(monkeypatch):
    from packages.api.routers import dashboard

    monkeypatch.setattr(dashboard, "_comfort_assessment_cache", None)
    monkeypatch.setattr(dashboard, "_comfort_assessment_lock", asyncio.Lock())


class _Session:
    def __init__(self, results):
        self.execute = AsyncMock(side_effect=results)


class _Context:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


def _result(*, one=None, values=()):
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalars.return_value.all.return_value = list(values)
    return result


@pytest.mark.asyncio
async def test_shared_override_query_selects_highest_id_not_latest_start():
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    selected = SimpleNamespace(id=12, ts_from=now - dt.timedelta(hours=4), ts_to=now)
    session = _Session([_result(one=selected)])

    override = await get_controlling_override(session, now=now)

    assert override is selected
    statement = session.execute.await_args.args[0]
    assert "ORDER BY overrides.id DESC" in str(statement)


@pytest.mark.asyncio
async def test_control_state_raises_when_controlling_override_lookup_fails(monkeypatch):
    session = _Session([])
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_controlling_override",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(ControlStateOverrideUnavailableError):
        await resolve_control_state()


@pytest.mark.asyncio
async def test_control_state_pause_has_precedence_and_observing_notice(monkeypatch):
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    override = SimpleNamespace(
        id=7, ts_from=now - dt.timedelta(hours=2), ts_to=now + dt.timedelta(hours=2)
    )
    session = _Session([_result(one=override), _result(values=[7, 6])])
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_learning_state",
        AsyncMock(return_value=LearningStateSnapshot(True, False, True)),
    )

    result = await resolve_control_state(now=now)

    assert result.state == "paused_by_user"
    assert result.override_id == 7
    assert result.active_override_count == 2
    assert result.notices[0].code == "observing_suppresses_safety_reverts"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comfort_state, expected",
    [
        ("degraded", "automatic"),
        ("unavailable", "automatic"),
        ("conflict", "automatic"),
        ("room_overheat_suppression", "automatic"),
        ("on_target", "automatic"),
        ("at_risk", "comfort_at_risk"),
    ],
)
async def test_control_state_only_selects_comfort_risk_for_at_risk(
    monkeypatch, comfort_state, expected
):
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    session = _Session([_result(), _result(values=[]), _result(), _result()])
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_learning_state",
        AsyncMock(return_value=LearningStateSnapshot(False, False, True)),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_device_data_quality",
        AsyncMock(return_value={"ready": True, "reasons": []}),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_planning_data_quality",
        AsyncMock(return_value={"control_allowed": True}),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state._next_pending_action_embargoed",
        AsyncMock(return_value=False),
    )

    result = await resolve_control_state(now=now, comfort_assessment={"state": comfort_state})

    assert result.state == expected


@pytest.mark.asyncio
async def test_control_state_forecast_failure_falls_through_to_automatic(monkeypatch):
    from packages.api.routers import dashboard

    monkeypatch.setattr(
        "packages.api.routers.models_router.get_indoor_forecast",
        AsyncMock(side_effect=RuntimeError("forecast unavailable")),
    )
    resolve = AsyncMock(return_value=SimpleNamespace(state="automatic"))
    monkeypatch.setattr(dashboard, "resolve_control_state", resolve)

    result = await dashboard.get_control_state()

    assert result.state == "automatic"
    assert resolve.await_args.kwargs["comfort_assessment"] is None


@pytest.mark.asyncio
async def test_control_state_caches_successful_comfort_assessment(monkeypatch):
    from packages.api.routers import dashboard

    forecast = AsyncMock(return_value={"comfort_assessment": {"state": "at_risk"}})
    resolve = AsyncMock(return_value=SimpleNamespace(state="automatic"))
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", forecast)
    monkeypatch.setattr(dashboard, "resolve_control_state", resolve)

    await dashboard.get_control_state()
    await dashboard.get_control_state()

    assert forecast.await_count == 1
    assert resolve.await_args.kwargs["comfort_assessment"] == {"state": "at_risk"}

    monkeypatch.setattr(
        dashboard,
        "_comfort_assessment_cache",
        (time.monotonic() - 31, {"state": "at_risk"}),
    )
    await dashboard.get_control_state()

    assert forecast.await_count == 2


@pytest.mark.asyncio
async def test_control_state_coalesces_concurrent_comfort_assessment_refreshes(monkeypatch):
    from packages.api.routers import dashboard

    release_forecast = asyncio.Event()
    forecast_started = asyncio.Event()

    async def forecast_call(*, hours):
        forecast_started.set()
        await release_forecast.wait()
        return {"comfort_assessment": {"state": "at_risk"}}

    forecast = AsyncMock(side_effect=forecast_call)
    resolve = AsyncMock(return_value=SimpleNamespace(state="automatic"))
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", forecast)
    monkeypatch.setattr(dashboard, "resolve_control_state", resolve)

    first = asyncio.create_task(dashboard.get_control_state())
    await asyncio.wait_for(forecast_started.wait(), timeout=1)
    second = asyncio.create_task(dashboard.get_control_state())
    await asyncio.sleep(0)
    release_forecast.set()

    await asyncio.gather(first, second)

    assert forecast.await_count == 1


@pytest.mark.asyncio
async def test_control_state_does_not_cache_comfort_assessment_failure(monkeypatch):
    from packages.api.routers import dashboard

    forecast = AsyncMock(
        side_effect=[RuntimeError("forecast unavailable"), {"comfort_assessment": {}}]
    )
    resolve = AsyncMock(return_value=SimpleNamespace(state="automatic"))
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", forecast)
    monkeypatch.setattr(dashboard, "resolve_control_state", resolve)

    await dashboard.get_control_state()
    await dashboard.get_control_state()

    assert forecast.await_count == 2


@pytest.mark.asyncio
async def test_control_state_failure_log_omits_raw_error_and_device_id(monkeypatch):
    session = _Session([_result(one=None), _result(values=[])])
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_learning_state",
        AsyncMock(side_effect=RuntimeError("secret device-123 details")),
    )

    with patch("packages.core.optimizer_control_state.logger.warning") as warning:
        result = await resolve_control_state()

    assert result.state == "holding"
    assert warning.call_args.kwargs == {
        "dependency": "control_state_resolution",
        "error_type": "RuntimeError",
    }
    assert "secret device-123 details" not in repr(warning.call_args)


def _patch_control_state_dependencies(
    monkeypatch,
    *,
    learning: LearningStateSnapshot,
    device_ready: bool = True,
    device_reason: str = "device_status_stale",
    next_action_embargoed: bool = False,
    unresolved_revert: bool = False,
    control_allowed: bool = True,
    override=None,
):
    session = _Session(
        [
            _result(values=[override.id] if override is not None else []),
            _result(one=1 if unresolved_revert else None),
        ]
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_controlling_override",
        AsyncMock(return_value=override),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_learning_state",
        AsyncMock(return_value=learning),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_device_data_quality",
        AsyncMock(
            return_value={
                "ready": device_ready,
                "reasons": [device_reason] if not device_ready else [],
            }
        ),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state._next_pending_action_embargoed",
        AsyncMock(return_value=next_action_embargoed),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_planning_data_quality",
        AsyncMock(return_value={"control_allowed": control_allowed}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override, learning, device_ready, next_action_embargoed, comfort_state, expected",
    [
        (
            SimpleNamespace(id=4, ts_from=None, ts_to=None),
            LearningStateSnapshot(True, False, True),
            True,
            False,
            None,
            "paused_by_user",
        ),
        (None, LearningStateSnapshot(True, True, True), True, False, None, "observing"),
        (None, LearningStateSnapshot(False, False, False), True, False, None, "holding"),
        (None, LearningStateSnapshot(False, False, True), False, False, None, "holding"),
        (None, LearningStateSnapshot(False, False, True), True, True, None, "holding"),
        (
            None,
            LearningStateSnapshot(False, False, True),
            True,
            False,
            "at_risk",
            "comfort_at_risk",
        ),
        (None, LearningStateSnapshot(False, False, True), True, False, "on_target", "automatic"),
    ],
    ids=[
        "pause-wins",
        "observing-wins",
        "unreliable-wins",
        "quality-wins",
        "revert-wins",
        "comfort-risk",
        "automatic-fallback",
    ],
)
async def test_control_state_pairwise_precedence_table(
    monkeypatch,
    override,
    learning,
    device_ready,
    next_action_embargoed,
    comfort_state,
    expected,
):
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    if override is not None:
        override.ts_from = now - dt.timedelta(hours=1)
        override.ts_to = now + dt.timedelta(hours=1)
    _patch_control_state_dependencies(
        monkeypatch,
        learning=learning,
        device_ready=device_ready,
        next_action_embargoed=next_action_embargoed,
        override=override,
    )

    result = await resolve_control_state(
        now=now,
        comfort_assessment={"state": comfort_state} if comfort_state else None,
    )

    assert result.state == expected


@pytest.mark.asyncio
async def test_control_state_holds_when_next_action_is_embargoed(monkeypatch):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(False, False, True),
        next_action_embargoed=True,
    )

    result = await resolve_control_state()

    assert result.state == "holding"
    assert result.reason_code == "unresolved_safety_revert"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manual_enabled, manual_error, expected_executor, expected_state",
    [
        (True, False, "active", "observing"),
        (False, False, "unknown", "holding"),
        (False, True, "unknown", "holding"),
    ],
    ids=["manual-survives-seasonal-error", "seasonal-error-holds", "manual-error-holds"],
)
async def test_real_learning_state_controls_executor_and_resolver(
    monkeypatch, manual_enabled, manual_error, expected_executor, expected_state
):
    from packages.optimizer.executor_core import resolve_learning_mode_state

    session = _Session([_result(values=[])])
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_session", lambda: _Context(session)
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_controlling_override",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_device_data_quality",
        AsyncMock(return_value={"ready": True, "reasons": []}),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state._next_pending_action_embargoed",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "packages.core.optimizer_control_state.get_planning_data_quality",
        AsyncMock(return_value={"control_allowed": True}),
    )
    monkeypatch.setattr(
        "packages.core.settings_service.get_bool_setting",
        AsyncMock(
            side_effect=RuntimeError("settings unavailable") if manual_error else None,
            return_value=manual_enabled,
        ),
    )
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_status",
        AsyncMock(side_effect=RuntimeError("seasonal unavailable")),
    )

    executor_state = await resolve_learning_mode_state()
    result = await resolve_control_state()

    assert executor_state.value == expected_executor
    assert result.state == expected_state


def test_matching_unresolved_revert_embargoes_next_dhw_action():
    next_action = SimpleNamespace(
        reverts_action_id=None,
        action_type="force_dhw_on",
        payload_json="{}",
        device_id="device-a",
    )
    revert = SimpleNamespace(
        reverts_action_id=42,
        action_type="force_dhw_off",
        payload_json="{}",
        device_id="device-a",
        status="pending",
    )

    assert _next_action_is_embargoed(next_action, [revert], SimpleNamespace()) is True


@pytest.mark.asyncio
async def test_control_state_stays_automatic_for_unrelated_revert(monkeypatch):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(False, False, True),
        next_action_embargoed=False,
        unresolved_revert=True,
    )

    result = await resolve_control_state()

    assert result.state == "automatic"
    assert [notice.code for notice in result.notices] == ["safety_restore_pending"]


def test_unrelated_unresolved_revert_does_not_embargo_next_dhw_action():
    next_action = SimpleNamespace(
        reverts_action_id=None,
        action_type="force_dhw_on",
        payload_json="{}",
        device_id="device-a",
    )
    revert = SimpleNamespace(
        reverts_action_id=42,
        action_type="force_dhw_off",
        payload_json="{}",
        device_id="device-b",
        status="pending",
    )

    assert _next_action_is_embargoed(next_action, [revert], SimpleNamespace()) is False


@pytest.mark.asyncio
async def test_control_state_without_next_action_reports_pending_revert_as_notice(monkeypatch):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(False, False, True),
        next_action_embargoed=False,
        unresolved_revert=True,
    )

    result = await resolve_control_state()

    assert result.state == "automatic"
    assert [notice.code for notice in result.notices] == ["safety_restore_pending"]


@pytest.mark.asyncio
async def test_no_next_action_is_not_embargoed():
    session = _Session([_result(one=None)])

    assert await _next_pending_action_embargoed(session) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revert_device_id, expected",
    [("device-a", True), ("device-b", False)],
    ids=["matching-revert-embargoes-next-action", "unrelated-revert-does-not-embargo"],
)
async def test_next_pending_action_embargo_query_uses_real_session_path(revert_device_id, expected):
    next_action = SimpleNamespace(
        id=10,
        plan_id=1,
        reverts_action_id=None,
        action_type="force_dhw_on",
        payload_json="{}",
        device_id="device-a",
        status="pending",
    )
    status = SimpleNamespace(device_id="device-a")
    unresolved_revert = SimpleNamespace(
        id=11,
        reverts_action_id=10,
        action_type="force_dhw_off",
        payload_json="{}",
        device_id=revert_device_id,
        status="pending",
    )
    session = _Session(
        [
            _result(one=next_action),
            _result(one=status),
            _result(values=[unresolved_revert]),
        ]
    )

    assert await _next_pending_action_embargoed(session) is expected
    assert session.execute.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manual_enabled, seasonal_active, reliable, expected",
    [
        (False, False, True, "automatic"),
        (True, False, True, "observing"),
        (False, True, True, "observing"),
        (True, True, True, "observing"),
        (False, False, False, "holding"),
        (True, False, False, "holding"),
        (False, True, False, "holding"),
        (True, True, False, "holding"),
    ],
    ids=[
        "inactive",
        "manual",
        "seasonal",
        "manual-and-seasonal",
        "unreliable-inactive",
        "unreliable-manual",
        "unreliable-seasonal",
        "unreliable-both",
    ],
)
async def test_control_state_maps_all_learning_combinations(
    monkeypatch, manual_enabled, seasonal_active, reliable, expected
):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(manual_enabled, seasonal_active, reliable),
    )

    result = await resolve_control_state()

    assert result.state == expected


@pytest.mark.asyncio
async def test_control_allowed_false_stays_automatic_with_new_plans_notice(monkeypatch):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(False, False, True),
        control_allowed=False,
    )

    result = await resolve_control_state()

    assert result.state == "automatic"
    assert [(notice.code, notice.severity) for notice in result.notices] == [
        ("new_plans_paused", "warning")
    ]
    assert "already-scheduled actions still run" in result.notices[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["credentials_missing", "status_missing", "status_stale"])
async def test_device_quality_failures_hold_control(monkeypatch, reason):
    _patch_control_state_dependencies(
        monkeypatch,
        learning=LearningStateSnapshot(False, False, True),
        device_ready=False,
        device_reason=reason,
    )

    result = await resolve_control_state()

    assert result.state == "holding"
    assert result.reason_code == reason


def test_control_state_schema_preserves_utc_actions_and_notices():
    resolved_at = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    response = ControlStateResponse.model_validate(
        {
            "state": "automatic",
            "headline": "Scheduled control remains active",
            "detail": "Automatic dispatch remains active.",
            "reason_code": "new_plans_paused",
            "since": "2026-09-28T10:00:00Z",
            "until": None,
            "override_id": None,
            "active_override_count": 0,
            "primary_action": {"kind": "link", "label": "View plan", "href": "/?view=timeline"},
            "notices": [
                {
                    "code": "new_plans_paused",
                    "severity": "warning",
                    "detail": "Already-scheduled actions still run.",
                }
            ],
            "resolved_at": resolved_at,
        }
    )

    assert response.since is not None and response.since.tzinfo is not None
    assert response.resolved_at == resolved_at
    assert response.primary_action is not None
    assert response.primary_action.kind == "link"
    assert response.notices[0].severity == "warning"

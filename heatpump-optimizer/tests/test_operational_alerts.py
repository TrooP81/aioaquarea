"""Focused operational alert projection tests."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from packages.core.operational_alerts import _panasonic_adapter_alert, device_status_is_fresh
from packages.core.operational_alerts import get_operational_alerts


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


def test_fresh_adapter_outage_builds_actionable_alert() -> None:
    alert = _panasonic_adapter_alert(
        {
            "status": "backoff",
            "state_fresh": True,
            "consecutive_failures": 3,
            "retry_at": "2026-08-14T09:20:00+00:00",
        }
    )

    assert alert is not None
    assert alert["id"] == "panasonic_adapter_unavailable"
    assert "3 consecutive" in alert["detail"]
    assert "automatic commands remain paused" in alert["action"]


def test_stale_adapter_outage_does_not_raise_alert() -> None:
    assert (
        _panasonic_adapter_alert(
            {"status": "unavailable", "state_fresh": False, "consecutive_failures": 4}
        )
        is None
    )


def test_adapter_alert_requires_three_consecutive_failures() -> None:
    assert (
        _panasonic_adapter_alert(
            {"status": "unavailable", "state_fresh": True, "consecutive_failures": 2}
        )
        is None
    )


@pytest.mark.asyncio
async def test_gate_failure_and_unknown_alerts_are_actionable() -> None:
    now = dt.datetime(2026, 8, 19, 10, tzinfo=dt.timezone.utc)
    heartbeat_rows = [
        SimpleNamespace(service="poller", updated_at=now),
        SimpleNamespace(service="optimizer", updated_at=now),
    ]
    gate_row = SimpleNamespace(
        device_id="device-a",
        consecutive_evaluation_failures=3,
        state="UNKNOWN",
        evaluated_at=now - dt.timedelta(hours=2),
        transitioned_at=None,
    )
    session = SimpleNamespace()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: heartbeat_rows)),
            SimpleNamespace(scalar_one_or_none=lambda: now),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [gate_row])),
        ]
    )

    with (
        patch("packages.core.operational_alerts.get_bool_setting", AsyncMock(return_value=True)),
        patch("packages.core.operational_alerts.get_int_setting", AsyncMock(return_value=60)),
        patch(
            "packages.core.operational_alerts.get_planning_data_quality",
            AsyncMock(return_value={"control_allowed": True}),
        ),
        patch("packages.ml.forecast_quality.get_forecast_scorecard", AsyncMock(return_value={})),
        patch(
            "packages.core.operational_alerts.service_heartbeat_details",
            return_value={"panasonic_adapter": {}},
        ),
        patch(
            "packages.core.operational_alerts.project_panasonic_adapter_state",
            return_value={"state_fresh": True, "status": "available"},
        ),
        patch("packages.core.operational_alerts.get_session") as mock_get_session,
    ):
        mock_get_session.return_value = _AsyncContextManager(session)
        result = await get_operational_alerts(now=now)

    alert_ids = {alert["id"] for alert in result["alerts"]}
    assert "space_heating_gate_failures_device-a" in alert_ids
    assert "space_heating_gate_unknown_device-a" in alert_ids


def test_device_status_freshness_uses_shared_polling_threshold() -> None:
    now = dt.datetime(2026, 8, 19, 10, tzinfo=dt.timezone.utc)

    assert device_status_is_fresh(now - dt.timedelta(minutes=14), now=now, poll_interval_seconds=60)
    assert not device_status_is_fresh(
        now - dt.timedelta(minutes=16), now=now, poll_interval_seconds=60
    )


@pytest.mark.asyncio
async def test_cancelled_actions_are_not_treated_as_failed_or_expired_alerts() -> None:
    session = SimpleNamespace()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
            SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
        ]
    )

    with (
        patch("packages.core.operational_alerts.get_bool_setting", AsyncMock(return_value=True)),
        patch("packages.core.operational_alerts.get_int_setting", AsyncMock(return_value=60)),
        patch(
            "packages.core.operational_alerts.get_planning_data_quality",
            AsyncMock(return_value={"control_allowed": True}),
        ),
        patch("packages.ml.forecast_quality.get_forecast_scorecard", AsyncMock(return_value={})),
        patch(
            "packages.core.operational_alerts.service_heartbeat_details",
            return_value={"panasonic_adapter": {}},
        ),
        patch(
            "packages.core.operational_alerts.project_panasonic_adapter_state",
            return_value={"state_fresh": False, "status": "available"},
        ),
        patch("packages.core.operational_alerts.get_session") as mock_get_session,
    ):
        mock_get_session.return_value = _AsyncContextManager(session)

        result = await get_operational_alerts(
            now=dt.datetime(2026, 8, 19, 10, tzinfo=dt.timezone.utc)
        )

    failed_actions_stmt = session.execute.await_args_list[2].args[0]
    compiled = failed_actions_stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    status_values = {
        value
        for key, value in compiled.params.items()
        if key.startswith("status") and isinstance(value, str)
    }
    assert {"failed", "expired"}.issubset(status_values)
    assert "cancelled" not in status_values
    assert all(alert["id"] != "plan_actions_failed" for alert in result["alerts"])

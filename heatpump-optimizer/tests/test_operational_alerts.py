"""Focused operational alert projection tests."""

import datetime as dt

from packages.core.operational_alerts import _panasonic_adapter_alert, device_status_is_fresh


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


def test_device_status_freshness_uses_shared_polling_threshold() -> None:
    now = dt.datetime(2026, 8, 19, 10, tzinfo=dt.timezone.utc)

    assert device_status_is_fresh(
        now - dt.timedelta(minutes=14), now=now, poll_interval_seconds=60
    )
    assert not device_status_is_fresh(
        now - dt.timedelta(minutes=16), now=now, poll_interval_seconds=60
    )

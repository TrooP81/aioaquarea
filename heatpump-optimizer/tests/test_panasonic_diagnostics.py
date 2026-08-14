"""Panasonic adaptor diagnostic-state tests."""

import datetime as dt

import pytest

from packages.core.panasonic_diagnostics import (
    build_panasonic_adapter_state,
    classify_panasonic_adapter_reason,
    project_panasonic_adapter_state,
)


def test_adapter_state_includes_absolute_retry_time() -> None:
    observed_at = dt.datetime(2026, 8, 14, 8, tzinfo=dt.timezone.utc)

    state = build_panasonic_adapter_state(
        status="unavailable",
        device_id="device-1",
        reason="offline",
        consecutive_failures=2,
        retry_after_seconds=600,
        observed_at=observed_at,
    )

    assert state["observed_at"] == "2026-08-14T08:00:00+00:00"
    assert state["retry_at"] == "2026-08-14T08:10:00+00:00"


def test_projected_state_marks_old_observation_stale() -> None:
    now = dt.datetime(2026, 8, 14, 9, tzinfo=dt.timezone.utc)
    state = build_panasonic_adapter_state(
        status="backoff",
        device_id="device-1",
        retry_after_seconds=300,
        observed_at=now - dt.timedelta(minutes=20),
    )

    projected = project_panasonic_adapter_state(state, now=now, stale_after_seconds=900)

    assert projected["status"] == "backoff"
    assert projected["state_fresh"] is False
    assert projected["age_seconds"] == 1200


def test_malformed_state_degrades_to_unknown() -> None:
    projected = project_panasonic_adapter_state({"status": "broken", "observed_at": "nope"})

    assert projected["status"] == "unknown"
    assert projected["state_fresh"] is False
    assert projected["consecutive_failures"] == 0


def test_far_future_observation_is_not_trusted_as_fresh() -> None:
    now = dt.datetime(2026, 8, 14, 9, tzinfo=dt.timezone.utc)
    state = build_panasonic_adapter_state(
        status="unavailable",
        device_id="device-1",
        observed_at=now + dt.timedelta(minutes=10),
    )

    assert project_panasonic_adapter_state(state, now=now)["state_fresh"] is False


def test_provider_error_text_is_reduced_to_stable_reason_code() -> None:
    assert (
        classify_panasonic_adapter_reason(
            "API error: unknown_error_code - Failed communication with adaptor"
        )
        == "adaptor_communication_failed"
    )
    assert classify_panasonic_adapter_reason("unexpected raw provider text") == (
        "live_status_request_failed"
    )


def test_builder_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        build_panasonic_adapter_state(status="broken", device_id=None)

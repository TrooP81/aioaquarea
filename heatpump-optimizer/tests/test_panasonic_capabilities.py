"""Panasonic capability projection tests."""

import datetime as dt
from types import SimpleNamespace

import pytest

from packages.core.panasonic_capabilities import build_panasonic_capabilities

_MANUAL_ONLY_COMMANDS = (
    "set_powerful_time",
    "set_force_heater",
    "set_holiday_timer",
    "request_defrost",
)
_OBSERVE_ONLY_COMMANDS = (
    "set_operation_mode",
    "set_zone_heat_temperature",
    "set_tank_temperature",
    "set_quiet_mode",
    "force_dhw",
    "set_special_status",
)


def _status(now: dt.datetime, **overrides):
    values = {
        "ts": now - dt.timedelta(seconds=30),
        "device_id": "device-1",
        "tank_temp": 48,
        "tank_target_temp": 50,
        "tank_operation_status": 1,
        "tank_heat_min": 40,
        "tank_heat_max": 65,
        "zone1_temp": 21,
        "zone1_target_temp": 22,
        "zone1_operation_status": 1,
        "zone1_heat_min": 18,
        "zone1_heat_max": 45,
        "zone2_temp": None,
        "zone2_target_temp": None,
        "zone2_operation_status": None,
        "zone2_heat_min": None,
        "zone2_heat_max": None,
        "mode": "heat",
        "quiet_mode": 0,
        "powerful_mode": 0,
        "force_heater": 0,
        "holiday_mode": 0,
        "defrost_active": False,
        "special_status": None,
        "special_status_supported": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fresh_observation_advertises_available_observed_commands() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now),
        poller_heartbeat=SimpleNamespace(updated_at=now - dt.timedelta(seconds=20)),
        device_status_threshold_seconds=900,
        now=now,
    )

    assert result["availability"]["commands_allowed"] is False
    assert result["availability"]["observation_available"] is True
    assert result["availability"]["reason"] == "available"
    assert result["device"] == {
        "id": "device-1",
        "observed": True,
        "has_tank": True,
        "zones": [1],
    }
    force_dhw = result["commands"]["force_dhw"]
    assert force_dhw["device_supported"] is True
    assert force_dhw["policy"] == "observe_only_build"
    assert force_dhw["available"] is False
    assert force_dhw["ordinary_automation_allowed"] is False
    assert force_dhw["values"] == ["off", "on"]
    assert result["commands"]["set_tank_temperature"]["constraints"] == {
        "observed_range": {"minimum_celsius": 40, "maximum_celsius": 65},
        "whole_degrees_only": True,
    }
    assert result["commands"]["set_zone_heat_temperature"]["constraints"] == {
        "observed_ranges": {"1": {"minimum": 18, "maximum": 45}},
        "whole_degrees_only": True,
    }
    for name in _OBSERVE_ONLY_COMMANDS:
        command = result["commands"][name]
        assert command["device_supported"] is True, name
        assert command["policy"] == "observe_only_build", name
        assert command["available"] is False, name
        assert command["ordinary_automation_allowed"] is False, name
    for name in _MANUAL_ONLY_COMMANDS:
        command = result["commands"][name]
        assert command["device_supported"] is True, name
        assert command["policy"] == "manual_only", name
        assert command["available"] is True, name
        assert command["ordinary_automation_allowed"] is False, name
    assert result["commands"]["request_defrost"]["policy"] == "manual_only"
    assert result["commands"]["set_special_status"]["device_supported"] is True
    assert result["commands"]["set_special_status"]["available"] is False
    assert result["api"]["live_probe_performed"] is False


@pytest.mark.parametrize(
    ("status_age", "heartbeat_age", "latest", "reason"),
    [
        (dt.timedelta(seconds=901), dt.timedelta(seconds=20), True, "live_status_stale"),
        (dt.timedelta(seconds=30), dt.timedelta(minutes=4), True, "poller_heartbeat_stale"),
        (None, dt.timedelta(seconds=20), False, "no_live_status"),
    ],
    ids=["stale_status", "stale_heartbeat", "unavailable_status"],
)
def test_stale_or_unavailable_observation_blocks_manual_and_observe_only_commands(
    status_age, heartbeat_age, latest, reason
) -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, ts=now - status_age) if latest else None,
        poller_heartbeat=SimpleNamespace(updated_at=now - heartbeat_age),
        device_status_threshold_seconds=900,
        now=now,
    )

    assert result["availability"]["reason"] == reason
    assert result["availability"]["commands_allowed"] is False
    assert result["commands"]["force_dhw"]["policy"] == "observe_only_build"
    for name in (*_MANUAL_ONLY_COMMANDS, *_OBSERVE_ONLY_COMMANDS):
        assert result["commands"][name]["available"] is False, name
    assert result["commands"]["set_tank_temperature"]["constraints"]["whole_degrees_only"] is True


def test_stale_status_blocks_every_command_without_hiding_capabilities() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, ts=now - dt.timedelta(minutes=16)),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )

    assert result["availability"]["reason"] == "live_status_stale"
    assert result["commands"]["force_dhw"]["device_supported"] is True
    assert all(not command["available"] for command in result["commands"].values())


def test_missing_tank_limits_blocks_target_write_but_keeps_tank_support() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, tank_heat_min=None, tank_heat_max=None),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )

    command = result["commands"]["set_tank_temperature"]
    assert result["availability"]["commands_allowed"] is False
    assert result["availability"]["observation_available"] is True
    assert command["device_supported"] is True
    assert command["available"] is False
    assert command["constraints"]["observed_range"] is None


def test_missing_zone_limits_blocks_target_write_but_keeps_zone_support() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, zone1_heat_min=None, zone1_heat_max=None),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )

    command = result["commands"]["set_zone_heat_temperature"]
    assert command["device_supported"] is True
    assert command["available"] is False
    assert command["constraints"]["observed_ranges"] == {}


def test_unsafe_special_status_metadata_blocks_optimizer_command() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, special_status_supported=False),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )

    command = result["commands"]["set_special_status"]
    assert command["device_supported"] is False
    assert command["available"] is False


def test_missing_observation_reports_unknown_device_support() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=None,
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        now=now,
    )

    assert result["availability"]["reason"] == "no_live_status"
    assert result["device"]["has_tank"] is None
    assert result["commands"]["set_force_heater"]["device_supported"] is None
    assert result["commands"]["set_force_heater"]["available"] is False
    assert result["commands"]["set_tank_temperature"]["constraints"] == {
        "observed_range": None,
        "whole_degrees_only": True,
    }


def test_stale_poller_heartbeat_blocks_otherwise_fresh_device() -> None:
    now = dt.datetime(2026, 8, 13, 12, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now),
        poller_heartbeat=SimpleNamespace(updated_at=now - dt.timedelta(minutes=4)),
        device_status_threshold_seconds=900,
        now=now,
    )

    assert result["availability"]["reason"] == "poller_heartbeat_stale"
    assert result["availability"]["commands_allowed"] is False
    assert all(not command["available"] for command in result["commands"].values())


def test_fresh_adapter_outage_is_exposed_and_blocks_commands() -> None:
    now = dt.datetime(2026, 8, 14, 9, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now, ts=now - dt.timedelta(minutes=20)),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        adapter_state={
            "status": "backoff",
            "device_id": "device-1",
            "reason": "offline",
            "consecutive_failures": 3,
            "retry_after_seconds": 1200,
            "observed_at": now.isoformat(),
            "retry_at": (now + dt.timedelta(minutes=20)).isoformat(),
        },
        now=now,
    )

    assert result["availability"]["reason"] == "adapter_backoff"
    assert result["availability"]["commands_allowed"] is False
    assert result["adapter"]["consecutive_failures"] == 3
    assert result["adapter"]["retry_at"] == "2026-08-14T09:20:00+00:00"


def test_stale_adapter_outage_does_not_override_live_availability() -> None:
    now = dt.datetime(2026, 8, 14, 9, tzinfo=dt.timezone.utc)
    result = build_panasonic_capabilities(
        latest_status=_status(now),
        poller_heartbeat=SimpleNamespace(updated_at=now),
        device_status_threshold_seconds=900,
        adapter_state={
            "status": "unavailable",
            "observed_at": (now - dt.timedelta(hours=1)).isoformat(),
        },
        now=now,
    )

    assert result["availability"]["reason"] == "available"
    assert result["adapter"]["state_fresh"] is False

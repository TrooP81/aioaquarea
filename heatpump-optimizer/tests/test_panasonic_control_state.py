"""Tests for optimizer permissions derived from Panasonic device state."""

from types import SimpleNamespace

import pytest

from packages.core.panasonic_control_state import (
    panasonic_tank_heating_available,
    panasonic_zone_heating_available,
)


def _status(**overrides):
    values = {
        "operation_status": 1,
        "mode": "1",
        "device_action": "HEATING",
        "tank_temp": 48.0,
        "tank_target_temp": 52,
        "tank_operation_status": 1,
        "zone1_temp": 35.0,
        "zone1_target_temp": 0.0,
        "zone1_heat_min": -5,
        "zone1_heat_max": 5,
        "zone1_operation_status": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_observed_panasonic_circuits_are_available():
    status = _status()

    assert panasonic_tank_heating_available(status) is True
    assert panasonic_zone_heating_available(status) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_status": 0},
        {"tank_operation_status": 0},
        {"tank_temp": None},
        {"tank_target_temp": None},
    ],
)
def test_tank_heating_rejects_unavailable_panasonic_state(overrides):
    assert panasonic_tank_heating_available(_status(**overrides)) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_status": 0},
        {"zone1_operation_status": 0},
        {"device_action": "COOLING"},
        {"mode": "2"},
        {"mode": "AUTO_COOL"},
        {"zone1_temp": None},
        {"zone1_target_temp": None},
        {"zone1_heat_min": 1, "zone1_target_temp": 0},
    ],
)
def test_zone_heating_rejects_non_heating_or_incomplete_panasonic_state(overrides):
    assert panasonic_zone_heating_available(_status(**overrides)) is False


def test_legacy_missing_status_flags_keep_complete_heat_circuit_available():
    status = _status(operation_status=None, zone1_operation_status=None, device_action=None, mode=None)

    assert panasonic_zone_heating_available(status) is True


def test_missing_device_status_disables_all_panasonic_load_control():
    assert panasonic_tank_heating_available(None) is False
    assert panasonic_zone_heating_available(None) is False

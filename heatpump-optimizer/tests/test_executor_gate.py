from types import SimpleNamespace

import pytest

from packages.optimizer.actions import ActionType
from packages.optimizer.executor_gate import is_room_heating_increase


@pytest.mark.parametrize("action_type", [ActionType.NORMAL_MODE_ON, ActionType.ECO_MODE_OFF])
def test_mode_actions_that_enable_room_heating_are_increases(action_type):
    assert is_room_heating_increase(action_type, {}, SimpleNamespace())


@pytest.mark.parametrize("zone_id", [pytest.param("absent", id="absent"), None, 0, 1])
def test_set_zone_temperature_normalizes_zone_one_identifiers(zone_id):
    payload = {"temperature": 21}
    if zone_id != "absent":
        payload["zone_id"] = zone_id

    assert is_room_heating_increase(
        ActionType.SET_ZONE_HEAT_TEMPERATURE,
        payload,
        SimpleNamespace(zone1_target_temp=20, zone2_target_temp=30),
    )


def test_set_zone_temperature_uses_zone_two_baseline():
    assert is_room_heating_increase(
        ActionType.SET_ZONE_HEAT_TEMPERATURE,
        {"zone_id": 2, "temperature": 29},
        SimpleNamespace(zone1_target_temp=20, zone2_target_temp=28),
    )


@pytest.mark.parametrize("zone_id", [True, False, "2", float("nan"), float("inf"), 1.5, 3, -1])
def test_set_zone_temperature_fails_closed_for_invalid_zone_identifiers(zone_id):
    assert is_room_heating_increase(
        ActionType.SET_ZONE_HEAT_TEMPERATURE,
        {"zone_id": zone_id, "temperature": 19},
        SimpleNamespace(zone1_target_temp=20, zone2_target_temp=20),
    )

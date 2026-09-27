from enum import IntEnum
from types import SimpleNamespace

import pytest

from packages.core.heating_evidence import classify_space_heating, has_confirmed_space_heating


class ExtendedOperationMode(IntEnum):
    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO_HEAT = 3
    AUTO_COOL = 4


def test_component_evidence_confirms_room_heating_when_global_status_is_off():
    evidence = classify_space_heating(
        operation_status=0,
        mode="1",
        direction="PUMP",
        pump_duty=1,
        device_action="OFF",
        defrost_active=False,
        zone1_operation_status=1,
        zone2_operation_status=0,
    )

    assert evidence.active is True
    assert evidence.code == "component_space_heating"


def test_component_evidence_requires_active_heating_mode_pump_and_zone():
    base = {
        "operation_status": 0,
        "mode": "1",
        "direction": "PUMP",
        "pump_duty": 1,
        "device_action": "OFF",
        "defrost_active": False,
        "zone1_operation_status": 1,
        "zone2_operation_status": 0,
    }
    negative_cases = [
        {"defrost_active": True},
        {"direction": "WATER"},
        {"device_action": "HEATING_WATER"},
        {"mode": "2", "device_action": "COOLING"},
        {"direction": "IDLE", "device_action": "IDLE"},
        {"pump_duty": 0},
        {"zone1_operation_status": 0},
    ]

    for override in negative_cases:
        evidence = classify_space_heating(**(base | override))
        assert evidence.active is False


@pytest.mark.parametrize(
    "override",
    [
        {"mode": None},
        {"direction": None},
        {"pump_duty": None},
        {"zone1_operation_status": None, "zone2_operation_status": None},
    ],
)
def test_component_evidence_rejects_missing_required_fields(override):
    base = {
        "operation_status": 0,
        "mode": "1",
        "direction": "PUMP",
        "pump_duty": 1,
        "device_action": "OFF",
        "defrost_active": False,
        "zone1_operation_status": 1,
        "zone2_operation_status": 0,
    }

    evidence = classify_space_heating(**(base | override))

    assert evidence.active is False


def test_legacy_missing_activity_fields_are_not_treated_as_heating():
    status = SimpleNamespace(
        operation_status=None,
        mode="1",
        direction="PUMP",
        pump_duty=None,
        device_action=None,
        defrost_active=None,
        zone1_operation_status=1,
        zone2_operation_status=None,
    )

    assert has_confirmed_space_heating(status) is False


def test_complete_legacy_component_evidence_is_promoted():
    status = SimpleNamespace(
        operation_status=0,
        mode="3",
        direction="PUMP",
        pump_duty=1,
        device_action="OFF",
        defrost_active=False,
        zone1_operation_status=0,
        zone2_operation_status=1,
    )

    assert has_confirmed_space_heating(status) is True


def test_persisted_evidence_is_authoritative():
    status = SimpleNamespace(space_heating_active=True)

    assert has_confirmed_space_heating(status) is True


def _old_classify_space_heating(**values):
    if values["defrost_active"]:
        return False, "defrost"
    if values["device_action"] == "HEATING_WATER" or values["direction"] == "WATER":
        return False, "domestic_hot_water"
    if values["device_action"] == "COOLING":
        return False, "cooling"
    if values["device_action"] == "IDLE" or values["direction"] == "IDLE":
        return False, "idle"
    active_zone = values["zone1_operation_status"] == 1 or values["zone2_operation_status"] == 1
    if (
        str(values["mode"]) in {"1", "3"}
        and values["direction"] == "PUMP"
        and values["pump_duty"] == 1
        and active_zone
    ):
        return True, "component_space_heating"
    if values["operation_status"] == 0:
        return False, "device_off"
    return False, "not_confirmed"


@pytest.mark.parametrize(
    "mode",
    [
        None,
        0,
        1,
        "1",
        "2",
        "3",
        "heat",
        "AUTO_COOL",
        ExtendedOperationMode.OFF,
        ExtendedOperationMode.HEAT,
        ExtendedOperationMode.COOL,
        ExtendedOperationMode.AUTO_HEAT,
        ExtendedOperationMode.AUTO_COOL,
        "ExtendedOperationMode.HEAT",
        "ExtendedOperationMode.AUTO_COOL",
        "malformed",
    ],
)
@pytest.mark.parametrize("operation_status", [None, 0, 1])
def test_classifier_parity_for_repository_mode_operation_corpus(mode, operation_status):
    values = {
        "operation_status": operation_status,
        "mode": mode,
        "direction": "PUMP",
        "pump_duty": 1,
        "device_action": "OFF",
        "defrost_active": False,
        "zone1_operation_status": 1,
        "zone2_operation_status": None,
    }

    evidence = classify_space_heating(**values)

    assert (evidence.active, evidence.code) == _old_classify_space_heating(**values)

from types import SimpleNamespace

import pytest

from packages.core.heating_evidence import classify_space_heating, has_confirmed_space_heating


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

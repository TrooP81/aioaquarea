from types import SimpleNamespace

from packages.core.heating_evidence import classify_space_heating, has_confirmed_space_heating


def test_pump_direction_while_device_is_off_is_not_space_heating():
    evidence = classify_space_heating(
        operation_status=0,
        direction="PUMP",
        device_action="OFF",
        defrost_active=False,
    )

    assert evidence.active is False
    assert evidence.code == "device_off"


def test_reported_heating_requires_active_device_and_pump_direction():
    evidence = classify_space_heating(
        operation_status=1,
        direction="PUMP",
        device_action="HEATING",
        defrost_active=False,
    )

    assert evidence.active is True
    assert evidence.code == "reported_space_heating"


def test_legacy_missing_activity_fields_are_not_treated_as_heating():
    status = SimpleNamespace(
        operation_status=None,
        direction="PUMP",
        device_action=None,
        defrost_active=None,
    )

    assert has_confirmed_space_heating(status) is False


def test_persisted_evidence_is_authoritative():
    status = SimpleNamespace(space_heating_active=True)

    assert has_confirmed_space_heating(status) is True

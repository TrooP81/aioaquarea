from types import SimpleNamespace

import pytest

from packages.api.routers.polling import poll_now
from packages.core.device_status_snapshot import build_device_status_record
from packages.poller.main import poll_device_status


def _value(value, name=None):
    return SimpleNamespace(value=value, name=name or str(value))


def test_snapshot_persists_component_evidence_and_operation_status_provenance():
    zone = SimpleNamespace(
        operation_status=_value(1),
        temperature=35.0,
        heat_target_temperature=40.0,
        heat_min=25,
        heat_max=45,
    )
    device = SimpleNamespace(
        zones={1: zone},
        current_direction=_value(1, "PUMP"),
        current_action=_value(0, "OFF"),
        device_mode_status=_value(0, "NORMAL"),
        operation_status=_value(0),
        mode=1,
        pump_duty=1,
        long_id="device-1",
        tank=None,
        quiet_mode=_value(0),
        powerful_time=_value(0),
        special_status=None,
        force_dhw=_value(0),
        force_heater=_value(0),
        holiday_timer=_value(0),
        operation_status_present=True,
        operation_status_valid=True,
    )

    record = build_device_status_record(device)

    assert record.space_heating_active is True
    assert record.space_heating_evidence == "component_space_heating"
    assert record.operation_status_present is True
    assert record.operation_status_valid is True


@pytest.mark.parametrize(
    ("provenance", "expected_present", "expected_valid"),
    [
        (
            {"operation_status_present": True, "operation_status_valid": False},
            True,
            False,
        ),
        (
            {"operation_status_present": False, "operation_status_valid": False},
            False,
            False,
        ),
        ({}, None, None),
    ],
)
def test_snapshot_preserves_operation_status_provenance_or_legacy_none(
    provenance, expected_present, expected_valid
):
    zone = SimpleNamespace(
        operation_status=_value(1),
        temperature=35.0,
        heat_target_temperature=40.0,
        heat_min=25,
        heat_max=45,
    )
    device = SimpleNamespace(
        zones={1: zone},
        current_direction=_value(1, "PUMP"),
        current_action=_value(0, "OFF"),
        device_mode_status=_value(0, "NORMAL"),
        operation_status=_value(0),
        mode=1,
        pump_duty=1,
        long_id="device-1",
        tank=None,
        quiet_mode=_value(0),
        powerful_time=_value(0),
        special_status=None,
        force_dhw=_value(0),
        force_heater=_value(0),
        holiday_timer=_value(0),
        **provenance,
    )

    record = build_device_status_record(device)

    assert record.operation_status_present is expected_present
    assert record.operation_status_valid is expected_valid
    assert record.space_heating_active is True
    assert record.space_heating_evidence == "component_space_heating"


def test_manual_and_scheduled_polling_share_the_snapshot_evidence_path():
    assert poll_now.__globals__["build_device_status_record"] is build_device_status_record
    assert (
        poll_device_status.__globals__["build_device_status_record"] is build_device_status_record
    )

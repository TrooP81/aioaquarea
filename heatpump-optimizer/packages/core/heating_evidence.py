"""Conservative evidence for actual space heating.

The Aquarea direction field describes hydraulic direction, not a guarantee that
the compressor is currently producing room heat.  In particular, a device can
retain ``PUMP`` while the global operation status is OFF.  This module keeps
the distinction explicit so ML training never treats configured plumbing as
delivered heat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpaceHeatingEvidence:
    """A persisted, explainable observation of room-heating activity."""

    active: bool
    code: str


def classify_space_heating(
    *,
    operation_status: int | None,
    direction: str | None,
    device_action: str | None,
    defrost_active: bool | None,
    mode: str | int | None = None,
    pump_duty: int | None = None,
    zone1_operation_status: int | None = None,
    zone2_operation_status: int | None = None,
) -> SpaceHeatingEvidence:
    """Classify a live Aquarea status without inferring heat from PUMP alone."""

    if defrost_active:
        return SpaceHeatingEvidence(False, "defrost")
    if device_action == "HEATING_WATER" or direction == "WATER":
        return SpaceHeatingEvidence(False, "domestic_hot_water")
    if device_action == "COOLING":
        return SpaceHeatingEvidence(False, "cooling")
    if device_action == "IDLE" or direction == "IDLE":
        return SpaceHeatingEvidence(False, "idle")
    active_zone = zone1_operation_status == 1 or zone2_operation_status == 1
    if str(mode) in {"1", "3"} and direction == "PUMP" and pump_duty == 1 and active_zone:
        return SpaceHeatingEvidence(True, "component_space_heating")
    if operation_status == 0:
        return SpaceHeatingEvidence(False, "device_off")
    return SpaceHeatingEvidence(False, "not_confirmed")


def has_confirmed_space_heating(status: Any) -> bool:
    """Return only positively confirmed room-heating observations.

    New rows persist the classifier result.  Legacy rows are accepted only if
    they include the complete reported HEATING/PUMP state; missing fields are
    deliberately not promoted to evidence.
    """

    persisted = getattr(status, "space_heating_active", None)
    if persisted is not None:
        return bool(persisted)
    evidence = classify_space_heating(
        operation_status=getattr(status, "operation_status", None),
        mode=getattr(status, "mode", None),
        direction=getattr(status, "direction", None),
        pump_duty=getattr(status, "pump_duty", None),
        device_action=getattr(status, "device_action", None),
        defrost_active=getattr(status, "defrost_active", None),
        zone1_operation_status=getattr(status, "zone1_operation_status", None),
        zone2_operation_status=getattr(status, "zone2_operation_status", None),
    )
    return evidence.active

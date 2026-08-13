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
) -> SpaceHeatingEvidence:
    """Classify a live Aquarea status without inferring heat from PUMP alone."""

    if defrost_active:
        return SpaceHeatingEvidence(False, "defrost")
    if operation_status == 0:
        return SpaceHeatingEvidence(False, "device_off")
    if device_action == "HEATING" and direction == "PUMP":
        return SpaceHeatingEvidence(True, "reported_space_heating")
    if device_action == "HEATING_WATER" or direction == "WATER":
        return SpaceHeatingEvidence(False, "domestic_hot_water")
    if device_action == "COOLING":
        return SpaceHeatingEvidence(False, "cooling")
    if device_action == "IDLE" or direction == "IDLE":
        return SpaceHeatingEvidence(False, "idle")
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
        direction=getattr(status, "direction", None),
        device_action=getattr(status, "device_action", None),
        defrost_active=getattr(status, "defrost_active", None),
    )
    return evidence.active

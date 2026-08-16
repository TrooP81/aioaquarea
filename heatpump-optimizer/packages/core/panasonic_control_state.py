"""Fail-closed optimizer permissions derived from live Panasonic state."""

from __future__ import annotations

import math
from typing import Any


_COOLING_MODES = {
    "2",
    "4",
    "cool",
    "auto_cool",
    "extendedoperationmode.cool",
    "extendedoperationmode.auto_cool",
}


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def panasonic_tank_heating_available(status: Any | None) -> bool:
    """Return whether the observed installation can accept forced DHW control."""

    if status is None:
        return False
    if getattr(status, "operation_status", None) == 0:
        return False
    if getattr(status, "tank_operation_status", None) == 0:
        return False
    return _finite_number(getattr(status, "tank_temp", None)) and _finite_number(
        getattr(status, "tank_target_temp", None)
    )


def panasonic_zone_heating_available(status: Any | None, zone_id: int = 1) -> bool:
    """Return whether a zone can execute optimizer heat-target commands.

    Missing legacy status flags are tolerated only when the complete live
    temperature target and range are available. Explicit OFF or cooling state
    always wins so optimizer writes cannot override a user-disabled circuit.
    """

    if status is None or zone_id not in (1, 2):
        return False
    if getattr(status, "operation_status", None) == 0:
        return False
    if getattr(status, f"zone{zone_id}_operation_status", None) == 0:
        return False
    if str(getattr(status, "device_action", "")).upper() == "COOLING":
        return False

    mode = str(getattr(status, "mode", "")).strip().lower()
    if mode in _COOLING_MODES:
        return False

    water_temp = getattr(status, f"zone{zone_id}_temp", None)
    target = getattr(status, f"zone{zone_id}_target_temp", None)
    minimum = getattr(status, f"zone{zone_id}_heat_min", None)
    maximum = getattr(status, f"zone{zone_id}_heat_max", None)
    if not all(_finite_number(value) for value in (water_temp, target, minimum, maximum)):
        return False

    target_value = float(target)
    minimum_value = float(minimum)
    maximum_value = float(maximum)
    return minimum_value <= target_value <= maximum_value

"""Safety checks for optimizer-controlled Panasonic ECO/COMFORT modes."""

from __future__ import annotations

from typing import Any

from aioaquarea.data import SpecialStatus


def optimizer_special_status_supported(device: Any) -> bool:
    """Return whether special status is safe and useful for heating control.

    Aioaquarea updates every zone target when special status changes. Require
    complete modifiers for every writable zone and ensure ECO lowers while
    COMFORT raises the heating target. This prevents inverted or partial
    device metadata from turning an energy-saving action into extra heat.
    """

    if getattr(device, "support_special_status", False) is not True:
        return False

    zones = list(getattr(device, "zones", {}).values())
    if not zones:
        return False

    has_heating_target = False
    for zone in zones:
        if getattr(zone, "supports_special_status", False) is not True:
            return False

        modifiers = getattr(zone, "temperature_modifiers", None)
        if not isinstance(modifiers, dict):
            return False
        eco = modifiers.get(SpecialStatus.ECO)
        comfort = modifiers.get(SpecialStatus.COMFORT)

        heat_target = getattr(zone, "heat_target_temperature", None)
        if heat_target is not None:
            has_heating_target = True
            eco_heat = getattr(eco, "heat", None)
            comfort_heat = getattr(comfort, "heat", None)
            if (
                isinstance(eco_heat, bool)
                or not isinstance(eco_heat, (int, float))
                or eco_heat >= 0
                or isinstance(comfort_heat, bool)
                or not isinstance(comfort_heat, (int, float))
                or comfort_heat <= 0
            ):
                return False

        cool_target = getattr(zone, "cool_target_temperature", None)
        if cool_target is not None:
            eco_cool = getattr(eco, "cool", None)
            comfort_cool = getattr(comfort, "cool", None)
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (eco_cool, comfort_cool)
            ):
                return False

    return has_heating_target

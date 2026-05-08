"""Shared action typing and verification result models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    FORCE_DHW_ON = "force_dhw_on"
    FORCE_DHW_OFF = "force_dhw_off"
    QUIET_MODE_ON = "quiet_mode_on"
    QUIET_MODE_OFF = "quiet_mode_off"
    ZONE_TEMP_BOOST = "zone_temp_boost"
    ZONE_TEMP_RESTORE = "zone_temp_restore"
    SET_TANK_TEMP = "set_tank_temp"
    SET_ZONE_HEAT_TEMPERATURE = "set_zone_heat_temperature"
    ECO_MODE_ON = "eco_mode_on"
    ECO_MODE_OFF = "eco_mode_off"
    NORMAL_MODE_ON = "normal_mode_on"
    COMFORT_MODE_ON = "comfort_mode_on"


@dataclass(slots=True)
class VerifyResult:
    ok: bool
    observed_value: Any = None
    expected_value: Any = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "observed_value": self.observed_value,
            "expected_value": self.expected_value,
            "reason": self.reason,
        }

"""Pure room-heating action direction classification for executor safety checks."""

from __future__ import annotations

import math

from packages.optimizer.actions import ActionType


def is_room_heating_increase(action_type: ActionType, payload: dict, status) -> bool:
    if action_type in {
        ActionType.ZONE_TEMP_BOOST,
        ActionType.COMFORT_MODE_ON,
        ActionType.NORMAL_MODE_ON,
        ActionType.ECO_MODE_OFF,
    }:
        return True
    if action_type is not ActionType.SET_ZONE_HEAT_TEMPERATURE:
        return False
    zone_id = payload.get("zone_id", 1)
    if isinstance(zone_id, bool):
        return True
    if zone_id is None or zone_id in (0, 1):
        normalized_zone_id = 1
    elif (
        not isinstance(zone_id, (int, float))
        or not math.isfinite(float(zone_id))
        or not float(zone_id).is_integer()
        or int(zone_id) != 2
    ):
        return True
    else:
        normalized_zone_id = 2
    target = payload.get("temperature")
    baseline = status.zone1_target_temp if normalized_zone_id == 1 else status.zone2_target_temp
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return True
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
        return True
    if not math.isfinite(float(target)) or not math.isfinite(float(baseline)):
        return True
    return float(target) > float(baseline)

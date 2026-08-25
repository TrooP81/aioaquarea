"""Central registry for optimizer action dispatch and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from packages.core.panasonic_special_status import optimizer_special_status_supported

from .types import ActionType, VerifyResult

DispatchFn = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any] | None]]
RedispatchFn = Callable[[Any, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None]]
VerifyFn = Callable[[Any, dict[str, Any], dict[str, Any] | None], VerifyResult]


@dataclass(frozen=True, slots=True)
class ActionHandler:
    dispatch: DispatchFn
    verify: VerifyFn
    redispatch: RedispatchFn | None = None

    async def redispatch_expected(
        self,
        wrapper: Any,
        payload: dict[str, Any],
        expected: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Repeat a command without changing its already-calculated target."""
        if self.redispatch is not None:
            return await self.redispatch(wrapper, payload, expected)
        return await self.dispatch(wrapper, payload)


def _zone_from_device(device: Any, zone_id: int = 0):
    zones = getattr(device, "zones", None)
    if not zones:
        return None
    if zone_id:
        return zones.get(zone_id)
    return zones.get(0) or zones.get(1) or next(iter(zones.values()), None)


def _is_whole_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value).is_integer()
    )


def _zone_temperature_range(zone: Any) -> tuple[int, int] | None:
    minimum = getattr(zone, "heat_min", None)
    maximum = getattr(zone, "heat_max", None)
    if not _is_whole_number(minimum) or not _is_whole_number(maximum):
        return None
    minimum = int(minimum)
    maximum = int(maximum)
    return (minimum, maximum) if minimum <= maximum else None


def _is_valid_zone_target(value: Any, zone: Any) -> bool:
    limits = _zone_temperature_range(zone)
    return bool(limits and _is_whole_number(value) and limits[0] <= value <= limits[1])


def _special_status_name(device: Any) -> str | None:
    status = getattr(device, "special_status", None)
    return getattr(status, "name", None)


def _tank_target_reached(device: Any) -> tuple[bool, Any, Any]:
    tank = getattr(device, "tank", None)
    current_temp = getattr(tank, "temperature", None)
    target_temp = getattr(tank, "target_temperature", None)
    reached = (
        isinstance(current_temp, (int, float))
        and isinstance(target_temp, (int, float))
        and current_temp >= target_temp - 0.5
    )
    return reached, current_temp, target_temp


async def _active_weekly_timer_conflict(wrapper: Any, zone_id: int) -> dict[str, Any] | None:
    slots = await wrapper.get_active_weekly_timer_slots()
    if not isinstance(slots, (list, tuple)):
        return None
    requested_zone = zone_id or 1
    matching = [slot for slot in slots if getattr(slot, "zone_id", None) == requested_zone]
    if not matching:
        return None
    return {
        "skip": True,
        "reason": "panasonic_weekly_timer_active",
        "timer_zone_id": requested_zone,
        "timer_slot_count": len(matching),
    }


async def _dispatch_force_dhw_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import ForceDHW

    device = await wrapper.get_device()
    reached, current_temp, target_temp = _tank_target_reached(device)
    if reached:
        return {
            "skip": True,
            "reason": "tank_at_target",
            "current_tank_temp": current_temp,
            "tank_target_temp": target_temp,
        }

    changed = await wrapper.force_dhw(ForceDHW.ON)
    if changed is False:
        return {"skip": True, "reason": "force_dhw_already_on"}
    return {"force_dhw": "ON"}


async def _redispatch_force_dhw_on(
    wrapper: Any,
    _payload: dict[str, Any],
    _expected: dict[str, Any],
) -> dict[str, Any]:
    """Avoid re-forcing DHW after Panasonic has already reached the tank target."""
    from aioaquarea import ForceDHW

    device = await wrapper.get_device()
    reached, _, _ = _tank_target_reached(device)
    if not reached:
        await wrapper.force_dhw(ForceDHW.ON)
    return {"force_dhw": "ON"}


async def _dispatch_force_dhw_off(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import ForceDHW

    changed = await wrapper.force_dhw(ForceDHW.OFF)
    if changed is False:
        return {"skip": True, "reason": "force_dhw_already_off"}
    return {"force_dhw": "OFF"}


async def _dispatch_quiet_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import QuietMode

    level = payload.get("level", 1)
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 3:
        return {
            "skip": True,
            "reason": "quiet_mode_level_invalid",
            "requested_quiet_level": level,
        }

    mode = QuietMode(level)
    changed = await wrapper.set_quiet_mode(mode)
    if changed is False:
        return {
            "skip": True,
            "reason": "quiet_mode_already_active",
            "observed_quiet_level": level,
        }
    return {"quiet_mode": mode.name}


async def _dispatch_quiet_mode_off(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import QuietMode

    changed = await wrapper.set_quiet_mode(QuietMode.OFF)
    if changed is False:
        return {"skip": True, "reason": "quiet_mode_already_off"}
    return {"quiet_mode": "OFF"}


async def _dispatch_zone_temp_boost(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    offset = int(payload.get("offset", 2))
    zone_id = int(payload.get("zone_id", 0))
    if conflict := await _active_weekly_timer_conflict(wrapper, zone_id):
        return conflict
    device = await wrapper.get_device()
    zone = _zone_from_device(device, zone_id)
    current_target = getattr(zone, "heat_target_temperature", None)
    if not _is_valid_zone_target(current_target, zone):
        return {
            "skip": True,
            "reason": "zone_target_or_range_unavailable",
            "observed_zone_target": current_target,
        }

    planned_baseline = payload.get("baseline_temperature")
    planned_target = payload.get("temperature")
    has_frozen_plan = planned_baseline is not None or planned_target is not None
    if has_frozen_plan:
        if not _is_valid_zone_target(planned_baseline, zone) or not _is_valid_zone_target(
            planned_target, zone
        ):
            return {
                "skip": True,
                "reason": "zone_boost_plan_outside_live_range",
                "observed_zone_target": current_target,
                "planned_baseline": planned_baseline,
                "requested_zone_target": planned_target,
            }
        if current_target != planned_baseline:
            return {
                "skip": True,
                "reason": "zone_target_changed_since_plan",
                "observed_zone_target": current_target,
                "planned_baseline": planned_baseline,
                "requested_zone_target": planned_target,
            }
        new_temp = int(planned_target)
    else:
        # Backward compatibility for boosts saved before plans froze their
        # absolute baseline and target. Restore actions intentionally do not
        # have an equivalent fallback because guessing a water target is unsafe.
        new_temp = int(current_target + offset)

    if not _is_valid_zone_target(new_temp, zone):
        return {
            "skip": True,
            "reason": "zone_boost_outside_live_range",
            "observed_zone_target": current_target,
            "requested_zone_target": new_temp,
        }
    await wrapper.set_zone_heat_temperature(zone_id, new_temp)
    return {"zone_id": zone_id, "temperature": new_temp}


async def _redispatch_zone_temp_boost(
    wrapper: Any,
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Retry the first absolute boost target instead of compounding its offset."""
    zone_id = int(expected.get("zone_id", payload.get("zone_id", 0)))
    target = expected.get("temperature")
    if not _is_whole_number(target):
        raise ValueError("Zone boost retry is missing a whole-degree absolute target")
    target = int(target)
    if await _active_weekly_timer_conflict(wrapper, zone_id):
        return expected
    await wrapper.set_zone_heat_temperature(zone_id, target)
    return {"zone_id": zone_id, "temperature": target}


async def _dispatch_zone_temp_restore(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    zone_id = int(payload.get("zone_id", 0))
    target = payload.get("temperature")
    if target is None:
        return {
            "skip": True,
            "reason": "zone_restore_target_missing",
            "requested_zone_target": None,
        }

    device = await wrapper.get_device()
    zone = _zone_from_device(device, zone_id)
    observed_target = getattr(zone, "heat_target_temperature", None)
    if not _is_valid_zone_target(observed_target, zone) or not _is_valid_zone_target(target, zone):
        return {
            "skip": True,
            "reason": "zone_restore_outside_live_range",
            "observed_zone_target": observed_target,
            "requested_zone_target": target,
        }
    boost_target = payload.get("boost_temperature")
    if boost_target is not None and (
        not _is_valid_zone_target(boost_target, zone) or observed_target != boost_target
    ):
        return {
            "skip": True,
            "reason": "zone_restore_target_changed_since_boost",
            "observed_zone_target": observed_target,
            "expected_boost_target": boost_target,
            "requested_zone_target": target,
        }
    target = int(target)
    await wrapper.set_zone_heat_temperature(zone_id, target)
    return {"zone_id": zone_id, "temperature": target}


async def _dispatch_set_tank_temp(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    target = int(payload.get("temperature", 50))
    await wrapper.set_tank_temperature(target)
    return {"temperature": target}


async def _dispatch_set_zone_heat_temperature(
    wrapper: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    zone_id = int(payload.get("zone_id", 0))
    if conflict := await _active_weekly_timer_conflict(wrapper, zone_id):
        return conflict
    target = payload["temperature"]
    device = await wrapper.get_device()
    zone = _zone_from_device(device, zone_id)
    if not _is_valid_zone_target(target, zone):
        return {
            "skip": True,
            "reason": "zone_target_outside_live_range",
            "requested_zone_target": target,
        }
    target = int(target)
    await wrapper.set_zone_heat_temperature(zone_id, target)
    return {"zone_id": zone_id, "temperature": target}


async def _dispatch_eco_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not optimizer_special_status_supported(await wrapper.get_device()):
        return {"skip": True, "reason": "special_status_not_safely_supported"}
    await wrapper.set_special_status("ECO")
    return {"special_status": "ECO"}


async def _dispatch_clear_special_status(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not optimizer_special_status_supported(await wrapper.get_device()):
        return {"skip": True, "reason": "special_status_not_safely_supported"}
    await wrapper.clear_special_status()
    return {"special_status": None}


async def _dispatch_comfort_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not optimizer_special_status_supported(await wrapper.get_device()):
        return {"skip": True, "reason": "special_status_not_safely_supported"}
    await wrapper.set_special_status("COMFORT")
    return {"special_status": "COMFORT"}


def _verify_force_dhw(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    observed = getattr(getattr(device, "force_dhw", None), "value", None)
    expected_value = 1 if expected and expected.get("force_dhw") == "ON" else 0
    if expected_value == 1 and observed != expected_value:
        reached, _, _ = _tank_target_reached(device)
        if reached:
            return VerifyResult(
                ok=True,
                observed_value=observed,
                expected_value=expected_value,
                reason="tank_target_reached",
            )
    ok = observed == expected_value
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=expected_value,
        reason=None if ok else "force_dhw_mismatch",
    )


def _verify_quiet_mode(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    observed = getattr(getattr(device, "quiet_mode", None), "value", None)
    target = expected.get("quiet_mode") if expected else None
    expected_value = {"OFF": 0, "LEVEL1": 1, "LEVEL2": 2, "LEVEL3": 3}.get(target)
    ok = expected_value is not None and observed == expected_value
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=expected_value,
        reason=None if ok else "quiet_mode_mismatch",
    )


def _verify_special_status(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    observed = _special_status_name(device)
    target = expected.get("special_status") if expected else None

    ok = observed == target
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=target,
        reason=None if ok else "special_status_mismatch",
    )


def _verify_tank_temp(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    observed = getattr(getattr(device, "tank", None), "target_temperature", None)
    target = expected.get("temperature") if expected else None
    ok = observed == target
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=target,
        reason=None if ok else "tank_target_mismatch",
    )


def _verify_zone_temp(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    zone_id = int((expected or {}).get("zone_id", payload.get("zone_id", 0)))
    zone = _zone_from_device(device, zone_id)
    observed = getattr(zone, "heat_target_temperature", None)
    target = expected.get("temperature") if expected else None
    ok = observed == target
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=target,
        reason=None if ok else "zone_target_mismatch",
    )


ACTION_REGISTRY: dict[ActionType, ActionHandler] = {
    ActionType.FORCE_DHW_ON: ActionHandler(
        _dispatch_force_dhw_on,
        _verify_force_dhw,
        redispatch=_redispatch_force_dhw_on,
    ),
    ActionType.FORCE_DHW_OFF: ActionHandler(_dispatch_force_dhw_off, _verify_force_dhw),
    ActionType.QUIET_MODE_ON: ActionHandler(_dispatch_quiet_mode_on, _verify_quiet_mode),
    ActionType.QUIET_MODE_OFF: ActionHandler(_dispatch_quiet_mode_off, _verify_quiet_mode),
    ActionType.ZONE_TEMP_BOOST: ActionHandler(
        _dispatch_zone_temp_boost,
        _verify_zone_temp,
        redispatch=_redispatch_zone_temp_boost,
    ),
    ActionType.ZONE_TEMP_RESTORE: ActionHandler(_dispatch_zone_temp_restore, _verify_zone_temp),
    ActionType.SET_TANK_TEMP: ActionHandler(_dispatch_set_tank_temp, _verify_tank_temp),
    ActionType.SET_ZONE_HEAT_TEMPERATURE: ActionHandler(
        _dispatch_set_zone_heat_temperature, _verify_zone_temp
    ),
    ActionType.ECO_MODE_ON: ActionHandler(_dispatch_eco_mode_on, _verify_special_status),
    ActionType.ECO_MODE_OFF: ActionHandler(_dispatch_clear_special_status, _verify_special_status),
    ActionType.NORMAL_MODE_ON: ActionHandler(
        _dispatch_clear_special_status, _verify_special_status
    ),
    ActionType.COMFORT_MODE_ON: ActionHandler(_dispatch_comfort_mode_on, _verify_special_status),
}


def get_action_handler(action_type: str | ActionType) -> ActionHandler:
    return ACTION_REGISTRY[ActionType(action_type)]

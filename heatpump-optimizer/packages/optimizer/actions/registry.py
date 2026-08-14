"""Central registry for optimizer action dispatch and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .types import ActionType, VerifyResult

ZONE_WATER_TARGET_MIN_C = 20
ZONE_WATER_TARGET_MAX_C = 65

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


def _zone_from_device(device: Any):
    if not getattr(device, "zones", None):
        return None
    return device.zones.get(0) or device.zones.get(1) or next(iter(device.zones.values()), None)


def _is_valid_zone_water_target(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and ZONE_WATER_TARGET_MIN_C <= value <= ZONE_WATER_TARGET_MAX_C
    )


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

    await wrapper.force_dhw(ForceDHW.ON)
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

    await wrapper.force_dhw(ForceDHW.OFF)
    return {"force_dhw": "OFF"}


async def _dispatch_quiet_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import QuietMode

    await wrapper.set_quiet_mode(QuietMode.LEVEL1)
    return {"quiet_mode": "LEVEL1"}


async def _dispatch_quiet_mode_off(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import QuietMode

    await wrapper.set_quiet_mode(QuietMode.OFF)
    return {"quiet_mode": "OFF"}


async def _dispatch_zone_temp_boost(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    offset = int(payload.get("offset", 2))
    zone_id = int(payload.get("zone_id", 0))
    device = await wrapper.get_device()
    zone = _zone_from_device(device)
    current_target = getattr(zone, "heat_target_temperature", None)
    if not _is_valid_zone_water_target(current_target):
        return {
            "skip": True,
            "reason": "zone_target_not_a_safe_water_setpoint",
            "observed_zone_target": current_target,
        }

    planned_baseline = payload.get("baseline_temperature")
    planned_target = payload.get("temperature")
    has_frozen_plan = planned_baseline is not None or planned_target is not None
    if has_frozen_plan:
        if not _is_valid_zone_water_target(planned_baseline) or not _is_valid_zone_water_target(
            planned_target
        ):
            return {
                "skip": True,
                "reason": "zone_boost_plan_not_safe",
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

    if not _is_valid_zone_water_target(new_temp):
        return {
            "skip": True,
            "reason": "zone_boost_outside_safe_water_range",
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
    if not _is_valid_zone_water_target(target):
        raise ValueError("Zone boost retry is missing a safe absolute target")
    target = int(target)
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
    zone = _zone_from_device(device)
    observed_target = getattr(zone, "heat_target_temperature", None)
    if not _is_valid_zone_water_target(observed_target) or not _is_valid_zone_water_target(target):
        return {
            "skip": True,
            "reason": "zone_restore_not_safe_in_curve_mode",
            "observed_zone_target": observed_target,
            "requested_zone_target": target,
        }
    boost_target = payload.get("boost_temperature")
    if boost_target is not None and (
        not _is_valid_zone_water_target(boost_target) or observed_target != boost_target
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
    target = int(payload["temperature"])
    if not _is_valid_zone_water_target(target):
        return {
            "skip": True,
            "reason": "zone_target_outside_safe_water_range",
            "requested_zone_target": target,
        }
    await wrapper.set_zone_heat_temperature(zone_id, target)
    return {"zone_id": zone_id, "temperature": target}


async def _dispatch_eco_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    await wrapper.set_special_status("ECO")
    return {"special_status": "ECO"}


async def _dispatch_clear_special_status(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    await wrapper.clear_special_status()
    return {"special_status": None}


async def _dispatch_comfort_mode_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
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
    ok = (target == "OFF" and observed == 0) or (
        target == "LEVEL1" and observed is not None and observed >= 1
    )
    return VerifyResult(
        ok=ok,
        observed_value=observed,
        expected_value=target,
        reason=None if ok else "quiet_mode_mismatch",
    )


def _verify_special_status(
    device: Any, payload: dict[str, Any], expected: dict[str, Any] | None
) -> VerifyResult:
    observed = _special_status_name(device)
    target = expected.get("special_status") if expected else None

    # The aioaquarea library does not surface the device's active special status
    # (DeviceStatus.special_status is hard-coded to None), so ECO/COMFORT can
    # never be read back via a refresh. When a special mode was requested and the
    # device reports nothing, accept the successful dispatch as applied instead
    # of failing verification forever — which otherwise spams
    # `action_verification_failed` errors and burns API read budget on 12 futile
    # polls. This stays forward-compatible: if the library ever populates
    # special_status, a real mismatch (observed non-None) still fails below.
    if target is not None and observed is None:
        return VerifyResult(
            ok=True,
            observed_value=observed,
            expected_value=target,
            reason="special_status_unverifiable",
        )

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
    zone = _zone_from_device(device)
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

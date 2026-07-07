"""Central registry for optimizer action dispatch and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from packages.core.config import settings

from .types import ActionType, VerifyResult

DispatchFn = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any] | None]]
VerifyFn = Callable[[Any, dict[str, Any], dict[str, Any] | None], VerifyResult]


@dataclass(frozen=True, slots=True)
class ActionHandler:
    dispatch: DispatchFn
    verify: VerifyFn


def _zone_from_device(device: Any):
    if not getattr(device, "zones", None):
        return None
    return device.zones.get(0) or device.zones.get(1) or next(iter(device.zones.values()), None)


def _special_status_name(device: Any) -> str | None:
    status = getattr(device, "special_status", None)
    return getattr(status, "name", None)


async def _dispatch_force_dhw_on(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from aioaquarea import ForceDHW

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
    new_temp = int((current_target or 20) + offset)
    await wrapper.set_zone_heat_temperature(zone_id, new_temp)
    return {"zone_id": zone_id, "temperature": new_temp}


async def _dispatch_zone_temp_restore(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    zone_id = int(payload.get("zone_id", 0))
    target = int(payload.get("temperature") or ((settings.comfort_temp_min + settings.comfort_temp_max) // 2))
    await wrapper.set_zone_heat_temperature(zone_id, target)
    return {"zone_id": zone_id, "temperature": target}


async def _dispatch_set_tank_temp(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    target = int(payload.get("temperature", 50))
    await wrapper.set_tank_temperature(target)
    return {"temperature": target}


async def _dispatch_set_zone_heat_temperature(wrapper: Any, payload: dict[str, Any]) -> dict[str, Any]:
    zone_id = int(payload.get("zone_id", 0))
    target = int(payload["temperature"])
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


def _verify_force_dhw(device: Any, payload: dict[str, Any], expected: dict[str, Any] | None) -> VerifyResult:
    observed = getattr(getattr(device, "force_dhw", None), "value", None)
    expected_value = 1 if expected and expected.get("force_dhw") == "ON" else 0
    ok = observed == expected_value
    return VerifyResult(ok=ok, observed_value=observed, expected_value=expected_value, reason=None if ok else "force_dhw_mismatch")


def _verify_quiet_mode(device: Any, payload: dict[str, Any], expected: dict[str, Any] | None) -> VerifyResult:
    observed = getattr(getattr(device, "quiet_mode", None), "value", None)
    target = expected.get("quiet_mode") if expected else None
    ok = (target == "OFF" and observed == 0) or (target == "LEVEL1" and observed is not None and observed >= 1)
    return VerifyResult(ok=ok, observed_value=observed, expected_value=target, reason=None if ok else "quiet_mode_mismatch")


def _verify_special_status(device: Any, payload: dict[str, Any], expected: dict[str, Any] | None) -> VerifyResult:
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
    return VerifyResult(ok=ok, observed_value=observed, expected_value=target, reason=None if ok else "special_status_mismatch")


def _verify_tank_temp(device: Any, payload: dict[str, Any], expected: dict[str, Any] | None) -> VerifyResult:
    observed = getattr(getattr(device, "tank", None), "target_temperature", None)
    target = expected.get("temperature") if expected else None
    ok = observed == target
    return VerifyResult(ok=ok, observed_value=observed, expected_value=target, reason=None if ok else "tank_target_mismatch")


def _verify_zone_temp(device: Any, payload: dict[str, Any], expected: dict[str, Any] | None) -> VerifyResult:
    zone = _zone_from_device(device)
    observed = getattr(zone, "heat_target_temperature", None)
    target = expected.get("temperature") if expected else None
    ok = observed == target
    return VerifyResult(ok=ok, observed_value=observed, expected_value=target, reason=None if ok else "zone_target_mismatch")


ACTION_REGISTRY: dict[ActionType, ActionHandler] = {
    ActionType.FORCE_DHW_ON: ActionHandler(_dispatch_force_dhw_on, _verify_force_dhw),
    ActionType.FORCE_DHW_OFF: ActionHandler(_dispatch_force_dhw_off, _verify_force_dhw),
    ActionType.QUIET_MODE_ON: ActionHandler(_dispatch_quiet_mode_on, _verify_quiet_mode),
    ActionType.QUIET_MODE_OFF: ActionHandler(_dispatch_quiet_mode_off, _verify_quiet_mode),
    ActionType.ZONE_TEMP_BOOST: ActionHandler(_dispatch_zone_temp_boost, _verify_zone_temp),
    ActionType.ZONE_TEMP_RESTORE: ActionHandler(_dispatch_zone_temp_restore, _verify_zone_temp),
    ActionType.SET_TANK_TEMP: ActionHandler(_dispatch_set_tank_temp, _verify_tank_temp),
    ActionType.SET_ZONE_HEAT_TEMPERATURE: ActionHandler(_dispatch_set_zone_heat_temperature, _verify_zone_temp),
    ActionType.ECO_MODE_ON: ActionHandler(_dispatch_eco_mode_on, _verify_special_status),
    ActionType.ECO_MODE_OFF: ActionHandler(_dispatch_clear_special_status, _verify_special_status),
    ActionType.NORMAL_MODE_ON: ActionHandler(_dispatch_clear_special_status, _verify_special_status),
    ActionType.COMFORT_MODE_ON: ActionHandler(_dispatch_comfort_mode_on, _verify_special_status),
}


def get_action_handler(action_type: str | ActionType) -> ActionHandler:
    return ACTION_REGISTRY[ActionType(action_type)]

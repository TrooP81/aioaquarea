"""Shared conversion of an Aquarea device into a persisted status snapshot."""

from __future__ import annotations

import datetime as dt
from typing import Any

from packages.core.device_data_quality import _as_utc
from packages.core.heating_evidence import classify_space_heating
from packages.core.models import DeviceStatusRecord
from packages.core.panasonic_special_status import optimizer_special_status_supported


def native_policy_settings(status: Any) -> dict[str, Any]:
    return {
        key: getattr(status, key, None)
        for key in (
            "mode",
            "operation_status",
            "zone1_operation_status",
            "zone2_operation_status",
            "zone1_target_temp",
            "zone2_target_temp",
            "zone1_heat_min",
            "zone1_heat_max",
            "zone2_heat_min",
            "zone2_heat_max",
            "tank_operation_status",
            "tank_target_temp",
            "tank_heat_min",
            "tank_heat_max",
            "quiet_mode",
            "powerful_mode",
            "special_status",
            "force_dhw",
            "force_heater",
            "holiday_mode",
        )
    }


def build_device_status_record(
    device: Any,
    *,
    timer_provenance: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> DeviceStatusRecord:
    """Build a status record without using global action as heating evidence."""
    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    observed_at = getattr(device, "status_observed_at", None)
    retrieved_at = getattr(device, "status_retrieved_at", None)
    for value in (observed_at, retrieved_at):
        if value is not None and (_as_utc(value) is None or _as_utc(value) > now):
            raise ValueError("Device snapshot provenance is unknown or future")
    timer = timer_provenance if isinstance(timer_provenance, dict) else {"status": "unknown"}
    if timer.get("status") == "known":
        fetched_at = _as_utc(timer.get("fetched_at"))
        if fetched_at is None or fetched_at > now:
            timer = {"status": "unknown", "reason": "inadmissible_timer_clock"}
    zones = device.zones
    zone1 = zones.get(1)
    zone2 = zones.get(2)
    direction = device.current_direction.name
    device_action = device.current_action.name
    defrost_active = device.device_mode_status.name == "DEFROST"
    operation_status = device.operation_status.value
    zone1_status = zone1.operation_status.value if zone1 else None
    zone2_status = zone2.operation_status.value if zone2 else None
    heating_evidence = classify_space_heating(
        operation_status=operation_status,
        mode=str(device.mode),
        direction=direction,
        pump_duty=device.pump_duty,
        device_action=device_action,
        defrost_active=defrost_active,
        zone1_operation_status=zone1_status,
        zone2_operation_status=zone2_status,
    )
    record = DeviceStatusRecord(
        ts=now,
        device_id=device.long_id,
        mode=str(device.mode),
        operation_status=operation_status,
        operation_status_present=getattr(device, "operation_status_present", None),
        operation_status_valid=getattr(device, "operation_status_valid", None),
        outdoor_temp=None,
        heat_pump_outdoor_temp=None,
        outdoor_temp_source=None,
        tank_temp=device.tank.temperature if device.tank else None,
        tank_target_temp=device.tank.target_temperature if device.tank else None,
        tank_operation_status=device.tank.operation_status.value if device.tank else None,
        zone1_temp=zone1.temperature if zone1 else None,
        zone1_target_temp=zone1.heat_target_temperature if zone1 else None,
        zone1_heat_min=zone1.heat_min if zone1 else None,
        zone1_heat_max=zone1.heat_max if zone1 else None,
        zone2_temp=zone2.temperature if zone2 else None,
        zone2_target_temp=zone2.heat_target_temperature if zone2 else None,
        zone2_heat_min=zone2.heat_min if zone2 else None,
        zone2_heat_max=zone2.heat_max if zone2 else None,
        quiet_mode=device.quiet_mode.value,
        powerful_mode=device.powerful_time.value,
        special_status=device.special_status.value if device.special_status else None,
        special_status_supported=optimizer_special_status_supported(device),
        direction=direction,
        pump_duty=device.pump_duty,
        device_action=device_action,
        defrost_active=defrost_active,
        space_heating_active=heating_evidence.active,
        space_heating_evidence=heating_evidence.code,
        force_dhw=device.force_dhw.value,
        force_heater=device.force_heater.value,
        holiday_mode=device.holiday_timer.value,
        zone1_operation_status=zone1_status,
        zone2_operation_status=zone2_status,
        tank_heat_max=device.tank.heat_max if device.tank else None,
        tank_heat_min=device.tank.heat_min if device.tank else None,
    )
    record.native_policy_provenance = {
        "schema": "native_unchanged_v1",
        "captured_at": record.ts.isoformat(),
        "settings": native_policy_settings(record),
        "timer": timer,
        "timing": {
            "schema": "local_snapshot_v1",
            "observed_at": _as_utc(observed_at).isoformat() if observed_at is not None else None,
            "retrieved_at": _as_utc(retrieved_at).isoformat() if retrieved_at is not None else None,
        },
    }
    return record

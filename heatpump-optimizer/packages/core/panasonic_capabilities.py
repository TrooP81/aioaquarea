"""Read-only Panasonic command capability and availability projection."""

from __future__ import annotations

import datetime as dt
from typing import Any

_POLLER_MAX_AGE = dt.timedelta(minutes=3)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _observed(value: Any, *names: str) -> bool:
    return any(getattr(value, name, None) is not None for name in names)


def build_panasonic_capabilities(
    *,
    latest_status: Any | None,
    poller_heartbeat: Any | None,
    poll_interval_seconds: int,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Describe mapped commands without opening another Panasonic session."""

    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    stale_after_seconds = max(int(poll_interval_seconds), 60)
    status_at = _as_utc(getattr(latest_status, "ts", None))
    heartbeat_at = _as_utc(getattr(poller_heartbeat, "updated_at", None))

    if status_at is None:
        reason = "no_live_status"
    elif status_at < now - dt.timedelta(seconds=stale_after_seconds):
        reason = "live_status_stale"
    elif heartbeat_at is None:
        reason = "poller_heartbeat_missing"
    elif heartbeat_at < now - _POLLER_MAX_AGE:
        reason = "poller_heartbeat_stale"
    else:
        reason = "available"

    commands_allowed = reason == "available"
    observed_device = latest_status is not None
    has_tank = (
        _observed(
            latest_status,
            "tank_temp",
            "tank_target_temp",
            "tank_operation_status",
            "tank_heat_min",
            "tank_heat_max",
        )
        if observed_device
        else None
    )
    zones = []
    if observed_device and _observed(
        latest_status, "zone1_temp", "zone1_target_temp", "zone1_operation_status"
    ):
        zones.append(1)
    if observed_device and _observed(
        latest_status, "zone2_temp", "zone2_target_temp", "zone2_operation_status"
    ):
        zones.append(2)

    def command(
        *,
        policy: str,
        values: list[object] | None = None,
        device_supported: bool | None = None,
    ) -> dict[str, object]:
        return {
            "integration_supported": True,
            "device_supported": device_supported,
            "available": commands_allowed and device_supported is True,
            "policy": policy,
            "values": values or [],
        }

    def observed_support(*names: str) -> bool | None:
        return _observed(latest_status, *names) if observed_device else None

    return {
        "api": {
            "provider": "Panasonic Aquarea Smart Cloud",
            "contract": "private_unofficial",
            "live_probe_performed": False,
            "write_preflight_required": True,
        },
        "availability": {
            "commands_allowed": commands_allowed,
            "reason": reason,
            "last_live_status_at": status_at.isoformat() if status_at else None,
            "poller_heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
            "age_seconds": (round((now - status_at).total_seconds()) if status_at else None),
            "stale_after_seconds": stale_after_seconds,
        },
        "device": {
            "id": getattr(latest_status, "device_id", None),
            "observed": observed_device,
            "has_tank": has_tank,
            "zones": zones,
        },
        "commands": {
            "set_operation_mode": command(
                policy="automatic_with_plan",
                values=["off", "heat", "cool", "auto"],
                device_supported=observed_support("mode"),
            ),
            "set_zone_heat_temperature": command(
                policy="automatic_with_plan",
                values=zones,
                device_supported=bool(zones) if observed_device else None,
            ),
            "set_tank_temperature": command(
                policy="automatic_with_plan",
                device_supported=has_tank,
            ),
            "set_quiet_mode": command(
                policy="automatic_with_plan",
                values=[0, 1, 2, 3],
                device_supported=observed_support("quiet_mode"),
            ),
            "force_dhw": command(
                policy="automatic_with_plan",
                values=["off", "on"],
                device_supported=has_tank,
            ),
            "set_powerful_time": command(
                policy="manual_only",
                values=["off", "30m", "60m", "90m"],
                device_supported=observed_support("powerful_mode"),
            ),
            "set_force_heater": command(
                policy="manual_only",
                values=["off", "on"],
                device_supported=observed_support("force_heater"),
            ),
            "set_holiday_timer": command(
                policy="manual_only",
                values=["off", "on"],
                device_supported=observed_support("holiday_mode"),
            ),
            "request_defrost": command(
                policy="manual_only",
                device_supported=observed_support("defrost_active"),
            ),
            "set_special_status": command(
                policy="automatic_with_plan",
                values=["normal", "eco", "comfort"],
                device_supported=None,
            ),
        },
    }

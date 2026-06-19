"""Settings service: DB-backed configuration with env-var fallback and manual mode support."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from packages.core.config import settings as env_settings
from packages.core.database import get_session
from packages.core.models import SettingRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: str
    description: str
    default: str = ""
    default_env: str | None = None
    options: tuple[str, ...] | None = None

    def serialize_default(self) -> str:
        return self.default

    def parse(self, value: str) -> Any:
        if self.value_type in {"str", "secret", "json"}:
            return value
        if self.value_type == "int":
            return int(value)
        if self.value_type == "float":
            return float(value)
        if self.value_type == "bool":
            return _parse_bool(value)
        raise ValueError(f"Unsupported setting type: {self.value_type}")


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")

# Defines which settings are configurable, their types, and env fallbacks.
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    # --- Optimizer layer ---
    "optimizer_layer": {
        "type": "str",
        "default": "rules_only",
        "description": "Which optimizer layer to use",
        "options": ["rules_only", "milp_preferred", "auto"],
    },
    # --- Learning mode ---
    "learning_mode_enabled": {
        "type": "bool",
        "default": "false",
        "description": (
            "Observe-only training mode: the optimizer still plans but the executor "
            "dispatches no device commands, so the heat pump runs naturally while data "
            "is collected for ML training. Toggle off to let the optimizer act."
        ),
    },
    "learning_mode_since": {
        "type": "str",
        "default": "",
        "description": "ISO timestamp when learning mode was last enabled (internal)",
    },
    # --- Service modes ---
    "price_provider": {
        "type": "str",
        "default_env": "price_provider",
        "description": "Price data source",
        "options": ["entsoe", "tibber", "manual"],
    },
    "weather_provider": {
        "type": "str",
        "default_env": None,
        "default": "open-meteo",
        "description": "Weather data source",
        "options": ["open-meteo", "manual"],
    },
    # --- API keys ---
    "entsoe_api_token": {
        "type": "secret",
        "default_env": "entsoe_api_token",
        "description": "ENTSO-E API token",
    },
    "entsoe_area": {
        "type": "str",
        "default_env": "entsoe_area",
        "description": "ENTSO-E bidding zone",
    },
    "tibber_api_token": {
        "type": "secret",
        "default_env": "tibber_api_token",
        "description": "Tibber API token",
    },
    # --- Panasonic credentials ---
    "aquarea_username": {
        "type": "secret",
        "default_env": "aquarea_username",
        "description": "Panasonic Aquarea username",
    },
    "aquarea_password": {
        "type": "secret",
        "default_env": "aquarea_password",
        "description": "Panasonic Aquarea password",
    },
    # --- Location ---
    "latitude": {
        "type": "float",
        "default_env": "latitude",
        "description": "Location latitude",
    },
    "longitude": {
        "type": "float",
        "default_env": "longitude",
        "description": "Location longitude",
    },
    "timezone": {
        "type": "str",
        "default": "Europe/Amsterdam",
        "description": "IANA timezone (e.g. Europe/Amsterdam, Europe/Stockholm)",
    },
    # --- Optimizer constraints ---
    "tank_min_temp": {
        "type": "int",
        "default_env": "tank_min_temp",
        "description": "Minimum tank temperature during comfort hours (°C)",
    },
    "tank_min_temp_offpeak": {
        "type": "int",
        "default_env": "tank_min_temp_offpeak",
        "description": "Minimum tank temperature during off-peak/sleep hours (°C)",
    },
    "tank_max_temp": {
        "type": "int",
        "default_env": "tank_max_temp",
        "description": "Maximum tank temperature (°C)",
    },
    "tank_volume_liters": {
        "type": "int",
        "default_env": "tank_volume_liters",
        "description": "DHW tank volume (liters)",
    },
    "sh_max_power_kw": {
        "type": "float",
        "default_env": "sh_max_power_kw",
        "description": "Heat pump max electrical input for space heating (kW)",
    },
    "comfort_temp_min": {
        "type": "float",
        "default_env": "comfort_temp_min",
        "description": "Comfort zone minimum (°C)",
    },
    "comfort_temp_max": {
        "type": "float",
        "default_env": "comfort_temp_max",
        "description": "Comfort zone maximum (°C)",
    },
    # --- Quiet mode ---
    "quiet_mode_start": {
        "type": "int",
        "default": "22",
        "description": "Quiet mode start hour (0-23)",
    },
    "quiet_mode_end": {
        "type": "int",
        "default": "6",
        "description": "Quiet mode end hour (0-23)",
    },
    # --- Price sensitivity ---
    "price_comfort_override_pct": {
        "type": "int",
        "default": "90",
        "description": "Skip comfort when price is above this percentile (0-100)",
    },
    "price_eco_upgrade_pct": {
        "type": "int",
        "default": "25",
        "description": "Upgrade eco to normal when price is below this percentile (0-100)",
    },
    # --- Adaptive learning ---
    "learned_schedule_threshold": {
        "type": "float",
        "default": "0.3",
        "description": "Min heating activity score (0-1) to auto-add comfort hour",
    },
    # --- Polling ---
    "poll_interval_seconds": {
        "type": "int",
        "default_env": "poll_interval_seconds",
        "description": "Device poll interval (seconds)",
    },
    # --- Display ---
    "currency": {
        "type": "str",
        "default": "EUR",
        "description": "Display currency",
        "options": ["EUR", "GBP", "USD", "SEK", "NOK", "DKK", "CHF", "PLN", "CZK", "HUF"],
    },
    "time_format": {
        "type": "str",
        "default": "24h",
        "description": "Time display format",
        "options": ["24h", "12h"],
    },
    # --- Manual static values ---
    "manual_price_eur_per_kwh": {
        "type": "float",
        "default": "0.25",
        "description": "Static electricity price per kWh when provider is 'manual'",
    },
    "manual_outdoor_temp": {
        "type": "float",
        "default": "10.0",
        "description": "Static outdoor temperature (°C) when weather provider is 'manual'",
    },
    "manual_wind_speed": {
        "type": "float",
        "default": "5.0",
        "description": "Static wind speed (m/s) when weather provider is 'manual'",
    },
    "manual_humidity": {
        "type": "float",
        "default": "60.0",
        "description": "Static humidity (%) when weather provider is 'manual'",
    },
    "manual_irradiance": {
        "type": "float",
        "default": "200.0",
        "description": "Static solar irradiance (W/m²) when weather provider is 'manual'",
    },
    # --- SmartThings indoor temperature ---
    "smartthings_enabled": {
        "type": "str",
        "default": "false",
        "description": "Enable SmartThings indoor temperature polling",
        "options": ["true", "false"],
    },
    "smartthings_client_id": {
        "type": "secret",
        "default_env": "smartthings_client_id",
        "description": "SmartThings OAuth client ID (from smartthings apps:create)",
    },
    "smartthings_client_secret": {
        "type": "secret",
        "default_env": "smartthings_client_secret",
        "description": "SmartThings OAuth client secret",
    },
    "smartthings_redirect_uri": {
        "type": "str",
        "default_env": "smartthings_redirect_uri",
        "description": "OAuth redirect URI (must match SmartThings app registration exactly)",
    },
    "smartthings_pat": {
        "type": "secret",
        "default_env": "smartthings_pat",
        "description": "Legacy: SmartThings Personal Access Token (use OAuth instead)",
    },
    "smartthings_device_ids": {
        "type": "str",
        "default": "",
        "description": "Comma-separated SmartThings device IDs (empty = auto-discover)",
    },
    "smartthings_poll_interval": {
        "type": "int",
        "default": "300",
        "description": "SmartThings poll interval in seconds (default 300 = 5 min)",
    },
    "_smartthings_oauth_state": {
        "type": "secret",
        "default": "",
        "description": "Transient CSRF state for SmartThings OAuth flow (internal)",
    },
    # --- Comfort model ---
    "use_comfort_model": {
        "type": "str",
        "default": "false",
        "description": "Enable ML comfort model for indoor temp prediction",
        "options": ["true", "false"],
    },
    "comfort_temp_target": {
        "type": "float",
        "default": "20.5",
        "description": "Target indoor air temperature (°C) for comfort model optimization",
    },
    "thermal_lag_minutes": {
        "type": "str",
        "default": "",
        "description": "Thermal lag override (minutes). Leave empty to auto-detect from data",
    },
    # --- Comfort schedule ---
    "comfort_schedule": {
        "type": "json",
        "default": '{"weekday":[7,8,9,17,18,19,20,21],"weekend":[8,9,10,11,12,13,14,15,16,17,18,19,20,21]}',
        "description": "Hours (0-23) when comfort mode is preferred, by day type (JSON)",
    },
    # --- Shower mode (reactive DHW boost) ---
    "shower_mode_enabled": {
        "type": "str",
        "default": "false",
        "description": "Enable reactive DHW boost on rapid tank temperature drop (shower detection)",
        "options": ["true", "false"],
    },
    "shower_drop_threshold": {
        "type": "int",
        "default": "10",
        "description": "Tank temp drop (°C) between polls to trigger shower mode",
    },
    "shower_max_duration_minutes": {
        "type": "int",
        "default": "60",
        "description": "Max shower boost duration before timeout (minutes)",
    },
}

SETTING_SPECS: dict[str, SettingSpec] = {
    key: SettingSpec(
        key=key,
        value_type=str(schema["type"]),
        description=str(schema.get("description", "")),
        default=str(schema.get("default", "")),
        default_env=schema.get("default_env"),
        options=tuple(schema["options"]) if schema.get("options") else None,
    )
    for key, schema in SETTINGS_SCHEMA.items()
}


def get_setting_spec(key: str) -> SettingSpec:
    try:
        return SETTING_SPECS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown setting: {key}") from exc


def _default_setting_value(spec: SettingSpec) -> str:
    if spec.default_env and hasattr(env_settings, spec.default_env):
        return str(getattr(env_settings, spec.default_env))
    return spec.serialize_default()


async def get_all_settings() -> dict[str, str]:
    """Load all settings from DB, falling back to env vars."""
    result: dict[str, str] = {}

    # Start with env fallbacks / defaults
    for key, spec in SETTING_SPECS.items():
        result[key] = _default_setting_value(spec)

    # Override with DB values
    async with get_session() as session:
        rows = await session.execute(select(SettingRecord))
        for row in rows.scalars().all():
            if row.key in SETTINGS_SCHEMA:
                result[row.key] = row.value

    return result


async def get_setting(key: str) -> str:
    """Get a single setting value (DB first, then env fallback)."""
    async with get_session() as session:
        row = await session.execute(
            select(SettingRecord).where(SettingRecord.key == key)
        )
        record = row.scalar_one_or_none()
        if record is not None:
            return record.value

    # Fallback to env
    spec = get_setting_spec(key)
    return _default_setting_value(spec)


async def get_typed_setting(key: str) -> Any:
    spec = get_setting_spec(key)
    raw_value = await get_setting(key)
    if raw_value == "" and spec.value_type not in {"str", "secret", "json"}:
        return spec.parse(spec.serialize_default()) if spec.serialize_default() else None
    return spec.parse(raw_value)


async def get_int_setting(key: str) -> int:
    return int(await get_typed_setting(key))


async def get_float_setting(key: str) -> float:
    return float(await get_typed_setting(key))


async def get_bool_setting(key: str) -> bool:
    return bool(await get_typed_setting(key))


async def get_string_setting(key: str) -> str:
    return str(await get_setting(key))


async def set_setting(key: str, value: str) -> None:
    """Upsert a setting in the DB."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with get_session() as session:
        stmt = pg_insert(SettingRecord).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value},
        )
        await session.execute(stmt)


async def set_settings_bulk(updates: dict[str, str]) -> None:
    """Upsert multiple settings."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with get_session() as session:
        for key, value in updates.items():
            if key not in SETTINGS_SCHEMA:
                continue
            stmt = pg_insert(SettingRecord).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value},
            )
            await session.execute(stmt)


async def get_comfort_schedule() -> dict[str, list[int]]:
    """Get the parsed comfort schedule. Returns {"weekday": [...hours], "weekend": [...hours]}."""
    raw = await get_setting("comfort_schedule")
    try:
        schedule = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        schedule = json.loads(SETTINGS_SCHEMA["comfort_schedule"]["default"])

    # Validate structure
    if not isinstance(schedule, dict):
        schedule = json.loads(SETTINGS_SCHEMA["comfort_schedule"]["default"])
    for key in ("weekday", "weekend"):
        if key not in schedule or not isinstance(schedule[key], list):
            schedule[key] = []
        schedule[key] = [h for h in schedule[key] if isinstance(h, int) and 0 <= h <= 23]

    return schedule


def _to_local(ts: dt.datetime, tz_name: str | None = None) -> dt.datetime:
    """Convert a (possibly UTC) timestamp to the user's local timezone."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("Europe/Amsterdam")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(tz)


async def get_user_tz() -> str:
    """Return the user's configured IANA timezone string."""
    return await get_setting("timezone") or "Europe/Amsterdam"


def is_comfort_hour(schedule: dict[str, list[int]], ts: dt.datetime, tz_name: str | None = None) -> bool:
    """Check if a given timestamp falls within a comfort hour (in local time)."""
    local = _to_local(ts, tz_name)
    day_type = "weekday" if local.weekday() < 5 else "weekend"
    return local.hour in schedule.get(day_type, [])


def dhw_deadlines_from_schedule(schedule: dict[str, list[int]], ts: dt.datetime, tz_name: str | None = None) -> list[int]:
    """
    Derive DHW ready-by hours from the comfort schedule.

    Returns the first hour of each contiguous comfort block for the day type
    of `ts` (in local time). The tank must be at temperature by the start of each comfort block.
    """
    local = _to_local(ts, tz_name)
    day_type = "weekday" if local.weekday() < 5 else "weekend"
    hours = sorted(set(schedule.get(day_type, [])))
    if not hours:
        return []

    deadlines = [hours[0]]
    for i in range(1, len(hours)):
        if hours[i] - hours[i - 1] > 1:
            deadlines.append(hours[i])
    return deadlines


async def get_learned_usage(days: int = 14) -> dict[str, dict[int, float]]:
    """Analyze recent device_status records to get per-hour heating activity scores."""
    import datetime as dt
    from collections import defaultdict
    from packages.core.models import DeviceStatusRecord

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(DeviceStatusRecord.ts, DeviceStatusRecord.device_action)
            .where(DeviceStatusRecord.ts >= since)
            .order_by(DeviceStatusRecord.ts)
        )
        rows = result.all()

    heating_counts: dict[str, dict[int, int]] = {"weekday": defaultdict(int), "weekend": defaultdict(int)}
    total_counts: dict[str, dict[int, int]] = {"weekday": defaultdict(int), "weekend": defaultdict(int)}

    for ts, action in rows:
        local = _to_local(ts)
        day_type = "weekday" if local.weekday() < 5 else "weekend"
        hour = local.hour
        total_counts[day_type][hour] += 1
        if action and action.upper() in ("HEATING", "HEATING_WATER"):
            heating_counts[day_type][hour] += 1

    learned: dict[str, dict[int, float]] = {"weekday": {}, "weekend": {}}
    for day_type in ("weekday", "weekend"):
        for hour in range(24):
            total = total_counts[day_type].get(hour, 0)
            if total > 0:
                score = heating_counts[day_type].get(hour, 0) / total
                if score > 0.05:
                    learned[day_type][hour] = round(score, 3)

    return learned


async def get_effective_schedule(learned_threshold: float = 0.3) -> dict[str, list[int]]:
    """Get the comfort schedule merged with learned usage patterns."""
    base = await get_comfort_schedule()
    learned = await get_learned_usage()

    merged = {"weekday": list(base["weekday"]), "weekend": list(base["weekend"])}
    for day_type in ("weekday", "weekend"):
        for hour, score in learned[day_type].items():
            if score >= learned_threshold and hour not in merged[day_type]:
                merged[day_type].append(hour)
        merged[day_type] = sorted(merged[day_type])

    return merged

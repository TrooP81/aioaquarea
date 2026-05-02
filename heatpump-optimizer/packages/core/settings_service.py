"""Settings service: DB-backed configuration with env-var fallback and manual mode support."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from packages.core.config import settings as env_settings
from packages.core.database import get_session
from packages.core.models import SettingRecord

logger = logging.getLogger(__name__)

# Defines which settings are configurable, their types, and env fallbacks.
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    # --- Optimizer layer ---
    "optimizer_layer": {
        "type": "str",
        "default": "rules_only",
        "description": "Which optimizer layer to use",
        "options": ["rules_only", "milp_preferred", "auto"],
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
    # --- Optimizer constraints ---
    "tank_min_temp": {
        "type": "int",
        "default_env": "tank_min_temp",
        "description": "Minimum tank temperature (°C)",
    },
    "tank_max_temp": {
        "type": "int",
        "default_env": "tank_max_temp",
        "description": "Maximum tank temperature (°C)",
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
}


async def get_all_settings() -> dict[str, str]:
    """Load all settings from DB, falling back to env vars."""
    result: dict[str, str] = {}

    # Start with env fallbacks / defaults
    for key, schema in SETTINGS_SCHEMA.items():
        env_attr = schema.get("default_env")
        if env_attr and hasattr(env_settings, env_attr):
            result[key] = str(getattr(env_settings, env_attr))
        elif "default" in schema:
            result[key] = str(schema["default"])
        else:
            result[key] = ""

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
    schema = SETTINGS_SCHEMA.get(key, {})
    env_attr = schema.get("default_env")
    if env_attr and hasattr(env_settings, env_attr):
        return str(getattr(env_settings, env_attr))
    return str(schema.get("default", ""))


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


def is_comfort_hour(schedule: dict[str, list[int]], ts: "dt.datetime") -> bool:
    """Check if a given timestamp falls within a comfort hour."""
    # Monday=0 ... Sunday=6; weekday = Mon-Fri
    day_type = "weekday" if ts.weekday() < 5 else "weekend"
    return ts.hour in schedule.get(day_type, [])


def dhw_deadlines_from_schedule(schedule: dict[str, list[int]], ts: "dt.datetime") -> list[int]:
    """
    Derive DHW ready-by hours from the comfort schedule.

    Returns the first hour of each contiguous comfort block for the day type
    of `ts`. The tank must be at temperature by the start of each comfort block.
    """
    day_type = "weekday" if ts.weekday() < 5 else "weekend"
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
        day_type = "weekday" if ts.weekday() < 5 else "weekend"
        hour = ts.hour
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

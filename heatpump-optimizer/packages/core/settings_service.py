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
    "dhw_ready_by_hours": {
        "type": "str",
        "default_env": "dhw_ready_by_hours",
        "description": "DHW ready-by hours (comma-separated)",
    },
    # --- Polling ---
    "poll_interval_seconds": {
        "type": "int",
        "default_env": "poll_interval_seconds",
        "description": "Device poll interval (seconds)",
    },
    # --- Manual static values ---
    "manual_price_eur_per_kwh": {
        "type": "float",
        "default": "0.25",
        "description": "Static electricity price (EUR/kWh) when price provider is 'manual'",
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

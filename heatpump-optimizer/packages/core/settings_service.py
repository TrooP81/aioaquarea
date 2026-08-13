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
from packages.core.heat_curve import HeatCurveConfig
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
    "seasonal_calibration_enabled": {
        "type": "bool",
        "default": "false",
        "description": (
            "Opt-in, observe-only seasonal calibration. During detected cold weather, "
            "plans are recorded but no heat-pump commands are sent so natural heating "
            "data can improve the demand and indoor thermal models."
        ),
    },
    "seasonal_calibration_max_outdoor_c": {
        "type": "float",
        "default": "12",
        "description": "Activate seasonal observation only when recent average outdoor temperature is at or below this value (°C)",
    },
    "seasonal_calibration_window_days": {
        "type": "int",
        "default": "7",
        "description": "Days of recent outdoor data used to decide whether heating season has started",
    },
    "seasonal_calibration_auto_train": {
        "type": "bool",
        "default": "true",
        "description": "When seasonal evidence is sufficient, train demand and thermal models automatically (still observe-only)",
    },
    "seasonal_calibration_auto_exit": {
        "type": "bool",
        "default": "true",
        "description": "End seasonal observe-only mode after demand and indoor-heating evidence have both trained successfully",
    },
    "outcome_experiments_enabled": {
        "type": "bool",
        "default": "false",
        "description": "Enable optional manual heat-curve trials. Suggestions require your review and never send heat-pump commands.",
    },
    "outcome_experiment_max_curve_step_c": {
        "type": "float",
        "default": "0.5",
        "description": "Largest heat-curve adjustment suggested for one manual trial (°C)",
    },
    "operational_alerts_enabled": {
        "type": "bool",
        "default": "true",
        "description": "Show in-app alerts for stale data, failed plan actions, and degraded forecast or planning quality",
    },
    "operational_alert_webhook_url": {
        "type": "secret",
        "default": "",
        "description": "Optional HTTPS webhook for operational alerts; leave empty for in-app alerts only",
    },
    "_operational_alert_delivery_state": {
        "type": "json",
        "default": "{}",
        "description": "Internal webhook delivery throttle state",
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
        "options": ["open-meteo", "smhi", "manual"],
    },
    "outdoor_temperature_source": {
        "type": "str",
        "default": "weather",
        "description": "Outdoor temperature used by planning and ML; weather uses the configured weather report and falls back to the heat-pump sensor only when needed",
        "options": ["weather", "heat_pump"],
    },
    "outdoor_temperature_weather_offset_c": {
        "type": "float",
        "default": "0.0",
        "description": "Optional local adjustment added to the reported weather temperature (°C)",
    },
    "outdoor_temperature_weather_max_age_minutes": {
        "type": "int",
        "default": "180",
        "description": "Maximum age of a weather report before the system temporarily falls back to the heat-pump sensor (minutes)",
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
    # --- Controller heat curve (recorded from Panasonic controller) ---
    "heat_curve_outdoor_cold_c": {
        "type": "float",
        "default": "5",
        "description": "Controller curve: outdoor cold point (°C); Värme PÅ: Vattentemp",
    },
    "heat_curve_supply_cold_c": {
        "type": "float",
        "default": "47",
        "description": "Controller curve: supply water at cold outdoor point (°C)",
    },
    "heat_curve_outdoor_warm_c": {
        "type": "float",
        "default": "15",
        "description": "Controller curve: outdoor warm point (°C); Värme PÅ: Vattentemp",
    },
    "heat_curve_supply_warm_c": {
        "type": "float",
        "default": "23",
        "description": "Controller curve: supply water at warm outdoor point (°C)",
    },
    "heat_curve_heating_off_outdoor_c": {
        "type": "float",
        "default": "13",
        "description": "Controller: Värme AV above this outdoor temperature (°C)",
    },
    "heat_curve_delta_t_c": {
        "type": "float",
        "default": "4",
        "description": "Controller: Värme PÅ ΔT (°C); recorded hydraulic setting",
    },
    "_heat_curve_verification_state": {
        "type": "json",
        "default": "{}",
        "description": "Internal evidence window for the latest heat-curve change",
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
        "description": "Preferred display currency (the price source currency is used when no FX conversion is configured)",
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
        "description": "Static electricity price per kWh when provider is 'manual' (in manual price currency)",
    },
    "manual_price_currency": {
        "type": "str",
        "default": "EUR",
        "description": "Currency of the manual electricity price",
        "options": ["EUR", "SEK", "NOK", "DKK", "GBP", "USD", "CHF", "PLN", "CZK", "HUF"],
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
    "manual_precipitation": {
        "type": "float",
        "default": "0.0",
        "description": "Static precipitation (mm/h) when weather provider is 'manual'",
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
        "description": "Indoor temperature sensors to poll (none selected = poll all discovered)",
    },
    "comfort_reference_sensor_id": {
        "type": "str",
        "default": "",
        "description": "Optional reference room sensor for comfort control (empty = robust median of selected sensors)",
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


# Marker used by the GET /api/settings response to mask secret values.
SECRET_MASK_MARKER = "***"


def is_masked_secret(spec: SettingSpec, value: str) -> bool:
    """True when a secret value still carries the display mask.

    The settings GET endpoint returns secrets masked (e.g. ``a***z``). If a
    client echoes that masked value back on PUT we must NOT overwrite the real
    secret with the mask. The frontend already guards this; this is the
    matching server-side guard so a direct API client can't clobber a secret.
    """
    return spec.value_type == "secret" and SECRET_MASK_MARKER in value


def validate_setting_value(key: str, value: str) -> None:
    """Validate a raw string value against its setting's declared type/options.

    Raises ``KeyError`` for an unknown key and ``ValueError`` for a value that
    does not match the setting's options or type (so invalid input is rejected
    at the API boundary instead of crashing later in the optimizer/poller).
    """
    spec = get_setting_spec(key)

    if spec.options is not None:
        if value not in spec.options:
            raise ValueError(
                f"{key} must be one of {list(spec.options)} (got {value!r})"
            )
        return

    if spec.value_type == "json":
        try:
            json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"{key} must be valid JSON ({exc})") from exc
        return

    # Empty string clears a value (falls back to env/default); allowed for the
    # free-form types but never needs numeric/bool parsing.
    if value == "":
        return

    if spec.value_type in {"int", "float", "bool"}:
        try:
            spec.parse(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a valid {spec.value_type} ({exc})") from exc


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
        row = await session.execute(select(SettingRecord).where(SettingRecord.key == key))
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


async def get_heat_curve_config() -> HeatCurveConfig:
    """Load and validate the controller heat curve recorded in Settings."""
    return HeatCurveConfig.from_settings(await get_all_settings())


async def get_heat_curve_verification_state() -> dict[str, Any]:
    """Load the internal, persisted heat-curve evidence window safely."""
    raw = await get_setting("_heat_curve_verification_state")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def set_heat_curve_verification_state(state: dict[str, Any]) -> None:
    """Persist verification progress without exposing it as a user-editable setting."""
    await set_setting("_heat_curve_verification_state", json.dumps(state, sort_keys=True))


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


def is_comfort_hour(
    schedule: dict[str, list[int]], ts: dt.datetime, tz_name: str | None = None
) -> bool:
    """Check if a given timestamp falls within a comfort hour (in local time)."""
    local = _to_local(ts, tz_name)
    day_type = "weekday" if local.weekday() < 5 else "weekend"
    return local.hour in schedule.get(day_type, [])


def dhw_deadlines_from_schedule(
    schedule: dict[str, list[int]], ts: dt.datetime, tz_name: str | None = None
) -> list[int]:
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

    heating_counts: dict[str, dict[int, int]] = {
        "weekday": defaultdict(int),
        "weekend": defaultdict(int),
    }
    total_counts: dict[str, dict[int, int]] = {
        "weekday": defaultdict(int),
        "weekend": defaultdict(int),
    }

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

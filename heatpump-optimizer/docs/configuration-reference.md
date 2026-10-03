# Heat Pump Optimizer configuration reference

The optimizer reads configuration from two layers:

1. **Environment variables** – read once at process start by
   `packages/core/config.py` (`pydantic-settings`, `.env` file supported).
2. **Runtime settings** – stored in the `settings` database table, defined in
   `SETTINGS_SCHEMA` in `packages/core/settings_service.py`, and edited through
   the dashboard Settings page or `GET`/`PUT /api/settings`.

For runtime settings, a stored value wins. When no value is stored, the
setting's *env fallback* (if any) is used, then its schema default.

Values are stored as strings. `secret` values are masked in `GET /api/settings`;
a `PUT` that echoes a masked value (containing `***`) does not overwrite the
stored secret. A value outside a setting's options, type, or range is rejected
with an error. An empty string clears a free-form value.

## Environment variables

### Application (`packages/core/config.py`)

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://heatpump:changeme_in_production@db:5432/heatpump` | Async SQLAlchemy URL. Docker Compose builds it from `POSTGRES_*`. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis; stores the Panasonic auth circuit-breaker state. |
| `APP_ENVIRONMENT` | `development` | `development`, `staging`, or `production`. Outside `development`, startup fails unless `SECRET_KEY` and `API_TOKEN` are set. |
| `SECRET_KEY` | `change-this-to-a-random-string` | HMAC key for persisted ML models. Changing it invalidates saved models. A warning is emitted while the default is used. |
| `API_TOKEN` | `disabled` | Bearer token for the API. `disabled` or empty turns authentication off. |
| `CORS_ORIGINS` | `http://localhost:4444` | Comma-separated allowed origins. |
| `MODEL_DIR` | `/app/models` | ML model directory; shared by `api`, `poller`, `optimizer` via the `modeldata` volume. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `POLL_INTERVAL_SECONDS` | `300` | Device status poll interval used by the poller scheduler. Also the env fallback for `poll_interval_seconds`. |
| `PANASONIC_DISTRIBUTED_READ_QUOTA_ENABLED` | `false` | Enables the Redis-backed, per-account Panasonic logical-read quota. Manual refresh reserves two reads and requires ten available reads before admission; background reads wait and fall back to the local limiter when Redis is unavailable. |
| `EXECUTOR_SAFETY_READ_RESERVE` | `2` | Startup-only count of the five hourly executor verification reads reserved for safety restores. Valid range: 1–4. |
| `PRICE_PROVIDER` | `entsoe` | Env fallback for `price_provider`. |
| `ENTSOE_API_TOKEN` | _(empty)_ | Env fallback for `entsoe_api_token`. |
| `ENTSOE_AREA` | `10YNL----------L` | Env fallback for `entsoe_area`. |
| `TIBBER_API_TOKEN` | _(empty)_ | Env fallback for `tibber_api_token`. |
| `LATITUDE` / `LONGITUDE` | `52.37` / `4.89` | Env fallbacks for `latitude` / `longitude`. |
| `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET` | _(empty)_ | Env fallbacks for SmartThings OAuth. |
| `SMARTTHINGS_REDIRECT_URI` | _(empty)_ | Env fallback for `smartthings_redirect_uri`. |
| `SMARTTHINGS_PAT` | _(empty)_ | Env fallback for the legacy Personal Access Token. |
| `TANK_MIN_TEMP` | `45` | Env fallback for `tank_min_temp`. |
| `TANK_MIN_TEMP_OFFPEAK` | `41` | Env fallback for `tank_min_temp_offpeak`. |
| `TANK_MAX_TEMP` | `55` | Env fallback for `tank_max_temp`. |
| `COMFORT_TEMP_MIN` / `COMFORT_TEMP_MAX` | `20.0` / `22.0` | Env fallbacks for comfort bounds. |
| `TANK_VOLUME_LITERS` | `300` | Env fallback for `tank_volume_liters`. |
| `SH_MAX_POWER_KW` | `12.0` | Env fallback for `sh_max_power_kw`. |

Panasonic credentials are **not** read from the environment. Set
`aquarea_username` and `aquarea_password` on the Settings page.

### Panasonic distributed read-quota canary and rollback

Keep `PANASONIC_DISTRIBUTED_READ_QUOTA_ENABLED=false` for the legacy local
limiter. During a canary, enable it on one isolated deployment with Redis
available, verify `/api/panasonic/read-quota` reports `enabled: true` and
`reliable: true`, and confirm manual refresh succeeds above the displayed
ten-read threshold. The dashboard disables manual refresh when the quota is
unreliable or below that threshold. To roll back, set the flag to `false` and
restart the API and poller; no migration or Redis cleanup is required.

### Docker Compose

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `heatpump` / `changeme_in_production` / `heatpump` | `db`, app services, backups | Database credentials. |
| `DB_PORT` | `5434` | `db` | Loopback host port for PostgreSQL. |
| `API_PORT` | `8500` | `api` | Loopback host port for the API. |
| `WEB_PORT` | `4444` | `web` | Loopback host port for the dashboard. |
| `COMPOSE_ENV_FILE` | `.env` | all | Alternative env file for services. |
| `APP_VERSION` / `BUILD_REVISION` | `0.14.0` / `unknown` | image builds | Build metadata. |
| `TIMESCALE_IMAGE` / `REDIS_IMAGE` | pinned digests | `db`, backups / `redis` | Image overrides. |

### Backups

| Variable | Default | Description |
| --- | --- | --- |
| `BACKUP_INTERVAL_SECONDS` | `86400` | Interval between archives. |
| `BACKUP_RETENTION_DAYS` | `14` | Days completed archives are kept. |
| `BACKUP_VERIFY_AFTER_DUMP` | `false` | Restore each archive into a disposable database before accepting it. |
| `BACKUP_REPLICA_ENABLED` | `false` | Write an encrypted copy to the replica directory. |
| `BACKUP_REPLICA_REQUIRED` | `false` | Treat a failed replica as a failed backup. |
| `BACKUP_REPLICA_ENCRYPTION_KEY` | _(empty)_ | Required when replicas are enabled. |
| `BACKUP_REPLICA_HOST_DIR` | `./backups-replica` | Host directory mounted as the replica target. |
| `BACKUP_MAX_AGE_SECONDS` | `93600` (26 h) | Read by the API readiness check; an older newest backup is reported stale. |

### Web (`web` service)

| Variable | Default | Description |
| --- | --- | --- |
| `INTERNAL_API_URL` | `http://api:8500` | Upstream for the server-side `/api/*` proxy route. |
| `INTERNAL_API_TOKEN` | `API_TOKEN` | Bearer token added by the proxy. Never exposed to the browser. |

### Playwright live suite

| Variable | Default | Description |
| --- | --- | --- |
| `E2E_BASE_URL` | `http://localhost:4444` | Dashboard under test. |
| `E2E_API_URL` | `http://localhost:8500` | API under test. |

## Runtime settings

Types: `str`, `int`, `float`, `bool` (`true/1/yes/on`, `false/0/no/off`),
`secret` (masked), `json`. Keys starting with `_` are internal state and are
not meant to be edited.

### Optimizer control

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `optimizer_layer` | str | `rules_only` | `rules_only`, `milp_preferred`, or `auto`. |
| `learning_mode_enabled` | bool | `false` | Observe-only: plans are generated, no device commands are sent. |
| `learning_mode_since` | str | _(empty)_ | Internal: when learning mode was last enabled. |
| `shower_mode_enabled` | str | `false` | `true`/`false`. Reactive DHW boost on a rapid tank temperature drop. |
| `shower_drop_threshold` | int | `10` | Tank drop (°C) between polls that triggers shower mode. |
| `shower_max_duration_minutes` | int | `60` | Maximum shower boost duration. |
| `quiet_mode_start` | int | `22` | Night quiet-mode start hour (0–23). |
| `quiet_mode_end` | int | `6` | Night quiet-mode end hour (0–23). |
| `price_comfort_override_pct` | int | `90` | Skip comfort above this price percentile. |
| `price_eco_upgrade_pct` | int | `25` | Upgrade eco to normal below this price percentile. |
| `comfort_schedule` | json | weekday `7–9, 17–21`; weekend `8–21` | Comfort hours by day type: `{"weekday": [...], "weekend": [...]}`. |
| `learned_schedule_threshold` | float | `0.3` | Minimum heating activity score (0–1) to auto-add a comfort hour. |

### Seasonal calibration

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `seasonal_calibration_enabled` | bool | `false` | Opt-in observe-only mode during detected cold weather. |
| `seasonal_calibration_max_outdoor_c` | float | `12` | Activate only when the recent average outdoor temperature is at or below this (°C). |
| `seasonal_calibration_window_days` | int | `7` | Days of outdoor data used for the season decision. |
| `seasonal_calibration_auto_train` | bool | `true` | Train demand and thermal models when evidence is sufficient. |
| `seasonal_calibration_auto_exit` | bool | `true` | Leave seasonal observe-only mode after both models train successfully. |
| `_seasonal_calibration_safety_deferred_since` | str | _(empty)_ | Internal. |

### Equipment and comfort bounds

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `tank_min_temp` | int | env `TANK_MIN_TEMP` | Tank minimum during comfort hours (°C). |
| `tank_min_temp_offpeak` | int | env `TANK_MIN_TEMP_OFFPEAK` | Tank minimum outside comfort hours (°C). |
| `tank_max_temp` | int | env `TANK_MAX_TEMP` | Tank maximum (°C). |
| `tank_volume_liters` | int | env `TANK_VOLUME_LITERS` | DHW tank volume (L). |
| `sh_max_power_kw` | float | env `SH_MAX_POWER_KW` | Max electrical input for space heating (kW). |
| `comfort_temp_min` | float | env `COMFORT_TEMP_MIN` | Comfort minimum (°C). |
| `comfort_temp_max` | float | env `COMFORT_TEMP_MAX` | Comfort maximum (°C). |
| `comfort_temp_target` | float | `20.5` | Indoor target for the comfort model (°C). |

### Controller heat curve and room-heating gate

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `heat_curve_outdoor_cold_c` | float | `5` | Curve cold outdoor point (°C). |
| `heat_curve_supply_cold_c` | float | `47` | Supply water at the cold point (°C). |
| `heat_curve_outdoor_warm_c` | float | `15` | Curve warm outdoor point (°C). |
| `heat_curve_supply_warm_c` | float | `23` | Supply water at the warm point (°C). |
| `heat_curve_heating_off_outdoor_c` | float | `12` | Controller heating-off outdoor temperature (°C). |
| `heat_curve_delta_t_c` | float | `4` | Controller heating ΔT (°C). |
| `space_heating_behavior_profile` | str | `WH_MXC12J9E8_J_DEFAULT` | Room-heating eligibility profile (only option). |
| `space_heating_baseline_mode` | str | `shadow` | `off`, `shadow`, or `on`. |
| `space_heating_default_fraction` | float | `0.35` | Fallback room-heating duty fraction (0–1). |
| `space_heating_gate_on_offset_c` | float | `1` | Gate ON offset above heating-off temperature (°C). |
| `space_heating_gate_off_offset_c` | float | `3` | Gate OFF offset above heating-off temperature (°C). |
| `_heat_curve_verification_state` | json | `{}` | Internal. |

### Indoor forecast and comfort model

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `use_comfort_model` | str | `false` | `true`/`false`. Use the ML comfort model for indoor prediction. |
| `thermal_lag_minutes` | str | _(empty)_ | Thermal lag override; empty = auto-detect. |
| `indoor_forecast_max_passive_change_c_per_hour` | float | `0.5` | Maximum learned passive indoor change (°C/h). |
| `indoor_forecast_min_r2` | float | `0.15` | Minimum R² for learned indoor control. |
| `indoor_forecast_min_persistence_improvement_c` | float | `0.1` | Minimum improvement over persistence (°C). |
| `indoor_forecast_max_abs_bias_c` | float | `0.5` | Maximum absolute bias (°C). |
| `indoor_forecast_gate_passes_required` | int | `2` | Consecutive passing scorecards to enable learned control. |
| `indoor_forecast_gate_failures_required` | int | `1` | Consecutive failed scorecards to disable learned control. |

### Data sources

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `price_provider` | str | env `PRICE_PROVIDER` | `entsoe`, `tibber`, or `manual`. |
| `entsoe_api_token` | secret | env `ENTSOE_API_TOKEN` | |
| `entsoe_area` | str | env `ENTSOE_AREA` | ENTSO-E bidding zone. |
| `tibber_api_token` | secret | env `TIBBER_API_TOKEN` | |
| `manual_price_eur_per_kwh` | float | `0.25` | Static price per kWh for `manual` (in `manual_price_currency`). |
| `manual_price_currency` | str | `EUR` | `EUR`, `SEK`, `NOK`, `DKK`, `GBP`, `USD`, `CHF`, `PLN`, `CZK`, `HUF`. |
| `weather_provider` | str | `open-meteo` | `open-meteo`, `smhi`, or `manual`. |
| `manual_outdoor_temp` | float | `10.0` | °C, for `manual` weather. |
| `manual_wind_speed` | float | `5.0` | m/s. |
| `manual_humidity` | float | `60.0` | %. |
| `manual_irradiance` | float | `200.0` | W/m². |
| `manual_precipitation` | float | `0.0` | mm/h. |
| `outdoor_temperature_source` | str | `weather` | `weather` or `heat_pump`. `weather` falls back to the heat-pump sensor when the report is too old. |
| `outdoor_temperature_weather_offset_c` | float | `0.0` | Added to the weather temperature (°C). |
| `outdoor_temperature_weather_max_age_minutes` | int | `180` | Weather age before falling back to the heat-pump sensor. |
| `latitude` / `longitude` | float | env `LATITUDE` / `LONGITUDE` | Weather location. |
| `timezone` | str | `Europe/Amsterdam` | IANA timezone for schedules and local hours. |

### Panasonic

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `aquarea_username` | secret | _(none)_ | Panasonic ID username. |
| `aquarea_password` | secret | _(none)_ | Panasonic ID password. |
| `poll_interval_seconds` | int | env `POLL_INTERVAL_SECONDS` | Expected poll interval used by data-freshness checks and alerts. The poller schedule itself uses the `POLL_INTERVAL_SECONDS` environment variable. |
| `device_status_max_age_minutes` | int | `15` | Maximum device-status age for control readiness (5–60). |

### SmartThings

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `smartthings_enabled` | str | `false` | `true`/`false`. |
| `smartthings_client_id` | secret | env `SMARTTHINGS_CLIENT_ID` | OAuth client ID. |
| `smartthings_client_secret` | secret | env `SMARTTHINGS_CLIENT_SECRET` | OAuth client secret. |
| `smartthings_redirect_uri` | str | env `SMARTTHINGS_REDIRECT_URI` | Must match the SmartApp registration exactly. |
| `smartthings_pat` | secret | env `SMARTTHINGS_PAT` | Legacy Personal Access Token. |
| `smartthings_device_ids` | str | _(empty)_ | Sensors to poll; empty = all discovered. |
| `comfort_reference_sensor_id` | str | _(empty)_ | Reference sensor; empty = robust median of selected sensors. |
| `smartthings_poll_interval` | int | `300` | Seconds. Read by the poller at start-up. |
| `smartthings_device_max_age_minutes` | int | `180` | Readings older than this are excluded (30–1440). |
| `_smartthings_oauth_state` | secret | _(empty)_ | Internal OAuth CSRF state. |

### Alerts, experiments, display

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `operational_alerts_enabled` | bool | `true` | In-app alerts for stale data, failed actions, degraded quality. |
| `operational_alert_webhook_url` | secret | _(empty)_ | Optional HTTPS webhook for alerts. |
| `_operational_alert_delivery_state` | json | `{}` | Internal throttle state. |
| `outcome_experiments_enabled` | bool | `false` | Manual heat-curve trial suggestions; never sends commands. |
| `outcome_experiment_max_curve_step_c` | float | `0.5` | Largest suggested curve step (°C). |
| `currency` | str | `EUR` | Display currency: `EUR`, `GBP`, `USD`, `SEK`, `NOK`, `DKK`, `CHF`, `PLN`, `CZK`, `HUF`. |
| `time_format` | str | `24h` | `24h` or `12h`. |

## Adding a runtime setting

Register the key in `SETTINGS_SCHEMA` with `type`, `description`, and either
`default` or `default_env` (the `Settings` attribute name). Add `options` or
`min_value`/`max_value` to have the API validate input. Then add a row to this
reference.

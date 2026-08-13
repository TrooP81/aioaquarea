# Heat Pump Optimizer

Cost-optimizing controller for Panasonic Aquarea heat pumps. Monitors electricity prices, weather forecasts, and device state to automatically minimize your heating costs while maintaining comfort.

## Architecture

```
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ poller  │──▶│ postgres │◀──│ api      │◀──│ web ui   │
│         │   │+timescale│   │(fastapi) │   │(next.js) │
└─────────┘   └──────────┘   └──────────┘   └──────────┘
     │              ▲              │
     ▼              │              ▼
 Panasonic     ┌──────────┐   ┌──────────┐
 Cloud API     │  redis   │   │optimizer │
(aioaquarea)   └──────────┘   └──────────┘
```

## Features

- **Real-time monitoring**: Device status, temperatures, compressor direction, device action, consumption
- **Hourly-accurate cost tracking**: Today's cost is computed as Σ(per-interval Δ kWh × that hour's spot price), not a flat-rate estimate
- **Fault detection**: Automatic detection and logging of device faults
- **Electricity price integration**: ENTSO-E day-ahead prices or Tibber subscription prices
- **Weather-aware**: Open-Meteo (default) or SMHI forecast for COP estimation and pre-heating
- **SmartThings indoor temperature**: OAuth or PAT integration for real indoor sensor readings (multi-sensor averaging), with an in-app sensor selector to choose which discovered sensors to poll
- **Rules-based optimizer (v3)**: DHW shifting, pre-heating, peak avoidance, schedule-driven eco/comfort, quiet mode, action verification
- **MILP optimizer**: Optimal 24h scheduling via linear programming, with schedule-aware off-peak tank floor
- **Comfort schedule**: Weekday/weekend comfort hours with adaptive learning from actual usage
- **Direction-aware COP**: Real COP computation from compressor direction and consumption data
- **ML models**: COP prediction, demand forecasting, and indoor comfort model (train on your own data)
- **Action verification**: Confirms commands took effect by polling device after execution
- **Manual overrides**: Always-wins pause button for the optimizer; survives optimizer reruns
- **Learning mode**: Manually toggleable observe-only mode — the optimizer keeps planning but sends no device commands, so the heat pump runs naturally while clean training data is collected over a long period
- **Condition-aware forecast safety**: Forecast quality is evaluated separately for rain, cold, and mild weather. Unobserved adverse conditions add a comfort reserve; failed conditions fall back to rules only when forecast.
- **Seasonal calibration**: Optional, observe-only collection activates only during detected heating weather; it never changes heat-pump settings autonomously.
- **On-demand actions**: "Optimize now" and "Poll now" buttons trigger an immediate plan or device refresh
- **Configurable settings UI**: Tank/comfort bounds, quiet mode hours, price sensitivity, learning thresholds — editable from the dashboard
- **Application log viewer**: Live, filterable view of structured logs from all services on the settings page
- **Audit log**: Every executed action is recorded

## Quick Start

1. **Copy environment config:**
   ```bash
   cp .env.example .env
   # Edit .env with your Panasonic credentials and ENTSO-E token
   ```

2. **Start all services:**
   ```bash
   docker compose up -d
   ```

3. **Access the dashboard:**
   - Web UI: http://localhost:3500
   - API docs: http://localhost:8500/docs

## Configuration

Settings can be supplied via environment variables (typically through `.env`) and most are also editable at runtime from the **Settings** page in the dashboard. UI changes take precedence over the env defaults.

### Credentials & data sources

| Variable | Description |
|----------|-------------|
| `AQUAREA_USERNAME` | Panasonic Comfort Cloud email |
| `AQUAREA_PASSWORD` | Panasonic Comfort Cloud password |
| `PRICE_PROVIDER` | Price source: `entsoe` (default) or `tibber` |
| `ENTSOE_API_TOKEN` | Free token from [ENTSO-E](https://transparency.entsoe.eu/) |
| `ENTSOE_AREA` | Your bidding zone (e.g., `10Y1001A1001A46L` for SE3, `10YNL----------L` for NL) |
| `TIBBER_API_TOKEN` | Token from [developer.tibber.com](https://developer.tibber.com/) |
| `LATITUDE` / `LONGITUDE` | Your location for weather forecasts |
| `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET` | SmartThings OAuth app credentials (preferred) |
| `SMARTTHINGS_REDIRECT_URI` | OAuth callback URL, e.g. `http://localhost:8500/api/smartthings/oauth/callback` |
| `SMARTTHINGS_PAT` | Personal Access Token (legacy fallback if OAuth isn't configured) |

### Optimizer & system

| Variable | Default | Description |
|----------|---------|-------------|
| `TANK_MIN_TEMP` | `45` | Hot water tank minimum (°C) during comfort hours |
| `TANK_MIN_TEMP_OFFPEAK` | `41` | Lower tank floor allowed during sleep/away hours from the comfort schedule |
| `TANK_MAX_TEMP` | `55` | Hot water tank maximum (°C) |
| `COMFORT_TEMP_MIN` / `COMFORT_TEMP_MAX` | `20.0` / `22.0` | Room temperature bounds |
| `TANK_VOLUME_LITERS` | `300` | DHW tank volume — used to derive thermal capacity |
| `SH_MAX_POWER_KW` | `12.0` | Heat pump max electrical input for space heating |
| `POLL_INTERVAL_SECONDS` | `300` | Device polling interval |

### App & deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | _(insecure default)_ | Required: set to a random string in production (used for HMAC-signed model files) |
| `API_TOKEN` | `disabled` | Set to a strong token to protect FastAPI. The web container forwards it server-to-server and never exposes it to the browser. |
| `CORS_ORIGINS` | `http://localhost:3500` | Comma-separated allowed origins |
| `MODEL_DIR` | `/app/models` | Where ML models are persisted |
| `LOG_LEVEL` | `INFO` | Standard log level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | — | Database credentials (used by `docker-compose`) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `DB_PORT` / `API_PORT` / `WEB_PORT` | `5434` / `8500` / `3500` | Host-side port mappings. DB and API bind to `127.0.0.1`; web remains the user-facing service. |
| `BACKUP_INTERVAL_SECONDS` / `BACKUP_RETENTION_DAYS` | `86400` / `14` | Automated PostgreSQL backup cadence and local archive retention |
| `BACKUP_VERIFY_AFTER_DUMP` | `false` | Restore each new backup into a disposable database before accepting it |
| `BACKUP_REPLICA_ENABLED` / `BACKUP_REPLICA_HOST_DIR` | `false` / `./backups-replica` | Opt-in encrypted replica; point the host directory at a mounted NAS/share |
| `BACKUP_REPLICA_ENCRYPTION_KEY` | — | Required when replica is enabled; use a long secret managed outside source control |

## Services

| Service | Default host port | Description |
|---------|-------------------|-------------|
| `web` | `WEB_PORT` (3500) | Dashboard UI |
| `api` | `API_PORT` (8500) | REST API + Swagger docs (`/docs`) |
| `poller` | — | Data collection (device + prices + weather + SmartThings) |
| `optimizer` | — | Plan generation + action execution |
| `db` | `DB_PORT` (5434 → container 5432) | TimescaleDB |
| `redis` | (internal only) | Cache + token persistence |
| `backup` | — | Scheduled PostgreSQL custom-format backups in `./backups` |

## API Endpoints

The full, always-current OpenAPI spec is available at `http://localhost:8500/docs`. Highlights:

**Overview & history**
- `GET /api/dashboard` — Overview (status, current price, today's kWh, hourly-priced today's cost, active plan, override flag)
- `GET /api/status/history?hours=24` — Device status history
- `GET /api/device/settings` — Last-known device-reported settings
- `GET /api/consumption/history?hours=24` — Per-interval energy deltas
- `GET /api/prices?hours=48` — Electricity prices (past + future)
- `GET /api/weather?hours=48` — Weather forecast
- `GET /api/stats?period=day|week|month` — Aggregated stats
- `GET /api/audit` — Audit log
- `GET /api/logs?minutes=30&level=&service=` — Application log entries (last up to 24h)
- `GET /api/currency` / `GET /api/time-format` — UI display preferences
- `GET /health` — Liveness probe

**Optimizer & overrides**
- `GET /api/plans` / `GET /api/plans/{id}` — Plans and plan details with actions
- `GET /api/optimizer/status` — Current optimizer mode, ML model readiness, and learning-mode state
- `GET /api/learning-mode` / `POST /api/learning-mode` — Read or toggle observe-only learning mode (`{"enabled": true|false}`)
- `POST /api/optimize-now` — Force an immediate optimizer run
- `POST /api/poll-now` — Force an immediate device poll
- `POST /api/overrides` / `DELETE /api/overrides/{id}` — Create or cancel a manual override

**Comfort schedule & indoor temp**
- `GET /api/comfort-schedule` / `PUT /api/comfort-schedule` — Read/write the schedule
- `GET /api/comfort-schedule/learned` — Auto-detected usage patterns
- `POST /api/comfort-schedule/apply-learned` — Merge learned patterns into the schedule
- `GET /api/indoor-temp?hours=24` / `GET /api/indoor-temp/latest` — Indoor sensor history/latest

**SmartThings integration**
- `GET /api/smartthings/devices` — List discoverable temperature sensors (powers the Settings sensor selector)
- `GET /api/smartthings/oauth/authorize` / `GET /api/smartthings/oauth/callback` — OAuth flow
- `GET /api/smartthings/oauth/status` — Current connection status
- `DELETE /api/smartthings/oauth/disconnect` — Revoke stored OAuth tokens

The Settings page → **SmartThings Integration** section includes a sensor selector: it discovers your temperature sensors and lets you tick which ones to poll for indoor temperature. Selecting none falls back to polling all discovered sensors. The choice is saved as `smartthings_device_ids`.

**Faults, COP & compressor**
- `GET /api/faults` / `POST /api/faults/{id}/resolve`
- `GET /api/cop/history` / `GET /api/cop/stats` / `POST /api/cop/compute`
- `GET /api/compressor/activity`

**Models, thermal & calibration**
- `POST /api/ml/train` — Retrain COP and demand models
- `GET /api/comfort-model/status` / `POST /api/comfort-model/train` / `GET /api/comfort-model/predict`
- `GET /api/thermal/status` / `POST /api/thermal/calibrate` / `GET /api/thermal/curve` / `GET /api/thermal/indoor-forecast`

**Settings & connectivity**
- `GET /api/settings` / `PUT /api/settings`
- `POST /api/test-connection` — Probe Panasonic/price/weather/SmartThings connectivity

**Admin / data reset**
- `POST /api/admin/reset` — Permanently delete selected data scopes so models can train from scratch (`{"scopes": ["indoor_temp", "energy", ...], "reset_models": true}`). Available scopes: `indoor_temp`, `energy`, `device_status`, `weather`, `prices`, `plans`, `logs`. Settings, credentials, and the SmartThings connection are always preserved. Clearing any model-feeding scope (`indoor_temp`, `energy`, `device_status`, `weather`) also resets the trained ML models (COP, demand, comfort, thermal) so predictions don't drift from deleted data.

The Settings page → **Danger Zone — Reset Data** card exposes this: tick the data categories to wipe (or *Start everything fresh* to select all), confirm, and the collected time-series data — plus the affected trained models — is cleared. Use it to recover after training on bad data (e.g. a misconfigured indoor-temperature sensor).

## How the Optimizer Works

### Rules Engine (v3)
1. **DHW Shifting**: Uses the thermal model to find the cheapest slot before each deadline, with direction-aware heating time estimation
2. **Pre-heating**: Boosts zone temperature during cheap hours before forecast cold spells
3. **Peak avoidance**: Activates quiet mode during the 5% most expensive hours (if outdoor temp allows)
4. **Comfort schedule**: Switches between eco and comfort modes based on weekday/weekend schedule with configurable price overrides
5. **Quiet mode**: Reduces compressor speed during configurable night hours (default 22:00–06:00)
6. **Adaptive learning**: Automatically detects regular heating patterns and merges them into the comfort schedule
7. **Holiday mode**: Suspends all optimization when device is in holiday mode
8. **Action verification**: Polls device after each command to confirm it took effect

### MILP Optimizer (v2)
Solves a 24h cost-minimization problem with:
- Decision: when to run DHW, how much space heating per hour
- Objective: minimize Σ(price × kWh_electrical)
- Constraints: per-hour tank floor (uses `tank_min_temp_offpeak` during sleep/away hours, normal `tank_min_temp` during comfort hours), tank max, comfort bounds, COP curve, hardware rate limits, and the comfort model's predicted indoor response when available

The MILP path is selected automatically when ≥14 days of training data and trained ML models are available; otherwise the rules engine runs. A partial price horizon produces a shorter, explicitly marked plan and a full newly published horizon queues one safe re-plan.

### ML Models
- **COP Model**: Predicts COP directly from real thermal data using compressor direction-aware sample pairing
- **Demand Model**: Forecasts thermal demand from weather + usage patterns
- **Comfort Model**: Predicts indoor temperature response, used by both rules and MILP for accurate pre-heating
- Models are persisted with HMAC-signed pickles (`SECRET_KEY`-bound) and shared between containers via `MODEL_DIR`
- Auto-retrain weekly on accumulated data; manual retrain via `POST /api/ml/train` or the settings page

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run a single service locally
python -m packages.poller.main
```

## Backups and data retention

Timescale retention policies keep raw device, weather, price, consumption, and
indoor-temperature history bounded. Plans and audit history are retained for
traceability. The `backup` service creates a PostgreSQL custom-format archive
immediately at startup and then on the configured interval in `./backups`.

Run a real restore verification at any time; it restores the newest archive to
a disposable database and removes that database afterwards:

```bash
docker compose --profile maintenance run --rm backup-verify
```

Set `BACKUP_VERIFY_AFTER_DUMP=true` to perform that restore check after every
scheduled backup. For machine-loss protection, set
`BACKUP_REPLICA_ENABLED=true`, mount a NAS/share through
`BACKUP_REPLICA_HOST_DIR`, and set `BACKUP_REPLICA_ENCRYPTION_KEY`. The replica
is AES-256-CBC encrypted with PBKDF2 and stored with a SHA-256 checksum. It is
off by default; use a secret manager rather than committing the key to `.env`.

### Frontend end-to-end tests

The dashboard has two Playwright suites under `web/`:

```bash
cd web

# Mocked UI tests — every API call is stubbed and Playwright starts its own
# dev server. Fast, deterministic, good for CI. (web/e2e/)
npm run test:e2e

# Live-stack tests — drive the real running system (web :4444 + API :8500 +
# DB + optimizer) with NO mocking, asserting real data and physical invariants
# (e.g. the indoor forecast must drift gradually toward outdoor, never snap).
# Requires the Docker stack to be up first (`docker compose up -d`). (web/e2e-live/)
npm run test:e2e:live

# Override targets when not on the default ports:
#   E2E_BASE_URL=http://host:4444 E2E_API_URL=http://host:8500 npm run test:e2e:live
```

## Safety

- Manual override **always wins** over the optimizer
- **Learning mode** suppresses all device commands while enabled (optimizer observes only) — useful for safely collecting training data
- Rate limiter prevents API abuse (30 reads/h, 20 writes/h)
- Circuit breaker disables auth for 15 min after 3 failures
- All actions are audit-logged
- Emergency: set any override via the API to immediately pause everything

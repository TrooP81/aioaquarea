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
- **Weather-aware**: Open-Meteo forecast for COP estimation and pre-heating
- **SmartThings indoor temperature**: OAuth or PAT integration for real indoor sensor readings (multi-sensor averaging)
- **Rules-based optimizer (v3)**: DHW shifting, pre-heating, peak avoidance, schedule-driven eco/comfort, quiet mode, action verification
- **MILP optimizer**: Optimal 24h scheduling via linear programming, with schedule-aware off-peak tank floor
- **Comfort schedule**: Weekday/weekend comfort hours with adaptive learning from actual usage
- **Direction-aware COP**: Real COP computation from compressor direction and consumption data
- **ML models**: COP prediction, demand forecasting, and indoor comfort model (train on your own data)
- **Action verification**: Confirms commands took effect by polling device after execution
- **Manual overrides**: Always-wins pause button for the optimizer; survives optimizer reruns
- **Learning mode**: Manually toggleable observe-only mode — the optimizer keeps planning but sends no device commands, so the heat pump runs naturally while clean training data is collected over a long period
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
| `API_TOKEN` | `disabled` | Set to a strong token to enable bearer-token auth on `/api/*` |
| `CORS_ORIGINS` | `http://localhost:3500` | Comma-separated allowed origins |
| `MODEL_DIR` | `/app/models` | Where ML models are persisted |
| `LOG_LEVEL` | `INFO` | Standard log level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | — | Database credentials (used by `docker-compose`) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `DB_PORT` / `API_PORT` / `WEB_PORT` | `5434` / `8500` / `3500` | Host-side port mappings |

## Services

| Service | Default host port | Description |
|---------|-------------------|-------------|
| `web` | `WEB_PORT` (3500) | Dashboard UI |
| `api` | `API_PORT` (8500) | REST API + Swagger docs (`/docs`) |
| `poller` | — | Data collection (device + prices + weather + SmartThings) |
| `optimizer` | — | Plan generation + action execution |
| `db` | `DB_PORT` (5434 → container 5432) | TimescaleDB |
| `redis` | (internal only) | Cache + token persistence |

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
- `GET /api/smartthings/devices` — List discoverable temperature sensors
- `GET /api/smartthings/oauth/authorize` / `GET /api/smartthings/oauth/callback` — OAuth flow
- `GET /api/smartthings/oauth/status` — Current connection status
- `DELETE /api/smartthings/oauth/disconnect` — Revoke stored OAuth tokens

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

The MILP path is selected automatically when ≥14 days of training data and trained ML models are available; otherwise the rules engine runs.

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

## Safety

- Manual override **always wins** over the optimizer
- **Learning mode** suppresses all device commands while enabled (optimizer observes only) — useful for safely collecting training data
- Rate limiter prevents API abuse (30 reads/h, 20 writes/h)
- Circuit breaker disables auth for 15 min after 3 failures
- All actions are audit-logged
- Emergency: set any override via the API to immediately pause everything

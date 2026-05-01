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
- **Fault detection**: Automatic detection and logging of device faults
- **Electricity price integration**: ENTSO-E day-ahead prices or Tibber subscription prices
- **Weather-aware**: Open-Meteo forecast for COP estimation and pre-heating
- **Rules-based optimizer (v3)**: DHW shifting, pre-heating, peak avoidance, schedule-driven eco/comfort, quiet mode, action verification
- **Comfort schedule**: Weekday/weekend comfort hours with adaptive learning from actual usage
- **Direction-aware COP**: Real COP computation from compressor direction and consumption data
- **MILP optimizer**: Optimal scheduling via linear programming
- **ML models**: COP prediction and demand forecasting (trains on your data)
- **Action verification**: Confirms commands took effect by polling device after execution
- **Manual overrides**: Always-wins pause button for the optimizer
- **Configurable settings**: Quiet mode hours, price sensitivity, learning thresholds — all editable via UI
- **Audit log**: Every action is logged

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

| Variable | Description |
|----------|-------------|
| `AQUAREA_USERNAME` | Panasonic Comfort Cloud email |
| `AQUAREA_PASSWORD` | Panasonic Comfort Cloud password |
| `PRICE_PROVIDER` | Price source: `entsoe` (default) or `tibber` |
| `ENTSOE_API_TOKEN` | Free token from [ENTSO-E](https://transparency.entsoe.eu/) |
| `ENTSOE_AREA` | Your bidding zone (e.g., `10Y1001A1001A46L` for SE3) |
| `TIBBER_API_TOKEN` | Token from [developer.tibber.com](https://developer.tibber.com/) |
| `LATITUDE` / `LONGITUDE` | Your location for weather |
| `TANK_MIN_TEMP` / `TANK_MAX_TEMP` | Hot water tank bounds |
| `COMFORT_TEMP_MIN` / `COMFORT_TEMP_MAX` | Room temperature bounds |
| `DHW_READY_BY_HOURS` | Hours when tank must be hot (e.g., `6,18`) |
| `POLL_INTERVAL_SECONDS` | Device polling interval (default: 300) |

## Services

| Service | Port | Description |
|---------|------|-------------|
| `web` | 3500 | Dashboard UI |
| `api` | 8500 | REST API + Swagger docs |
| `poller` | — | Data collection (device + prices + weather) |
| `optimizer` | — | Plan generation + action execution |
| `db` | 5432 | TimescaleDB |
| `redis` | 6379 | Cache + token persistence |

## API Endpoints

- `GET /api/dashboard` — Overview data (includes direction, device action, defrost, fault indicators)
- `GET /api/status/history?hours=24` — Device history
- `GET /api/consumption/history?hours=24` — Energy data
- `GET /api/prices?hours=48` — Electricity prices
- `GET /api/weather?hours=48` — Weather forecast
- `GET /api/plans` — Optimizer plans
- `GET /api/plans/{id}` — Plan details with actions
- `GET /api/stats?period=day|week|month` — Aggregated stats
- `POST /api/overrides` — Create manual override
- `DELETE /api/overrides/{id}` — Cancel override
- `GET /api/audit` — Audit log
- `GET /api/comfort-schedule` — Current comfort schedule
- `PUT /api/comfort-schedule` — Update comfort schedule
- `GET /api/comfort-schedule/learned` — Learned usage patterns
- `POST /api/comfort-schedule/apply-learned` — Merge learned patterns into schedule
- `GET /api/faults` — Device fault history
- `POST /api/faults/{id}/resolve` — Resolve a fault
- `GET /api/cop/history` — COP values over time
- `GET /api/cop/stats` — COP statistics by mode
- `POST /api/cop/compute` — Trigger COP computation
- `GET /api/compressor/activity` — Compressor direction/action history
- `GET /api/settings` — All configurable settings
- `PUT /api/settings` — Update settings

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
- Constraints: tank temp deadlines, comfort bounds, COP curve, rate limits

### ML Models
- **COP Model**: Predicts electrical consumption given conditions (gradient boosting)
- **Demand Model**: Forecasts thermal demand from weather + patterns
- Auto-retrains weekly on accumulated data

## Documentation

Full documentation is available in the [`docs/`](../../docs/) folder:

- [Getting Started Tutorial](../../docs/tutorials/getting-started.md)
- [Deploy with Docker](../../docs/how-to/deploy-with-docker.md)
- [Configuration Reference](../../docs/reference/configuration.md)
- [API Reference](../../docs/reference/api.md)
- [How the Optimizer Works](../../docs/explanation/optimizer-design.md)

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
- Rate limiter prevents API abuse (30 reads/h, 20 writes/h)
- Circuit breaker disables auth for 15 min after 3 failures
- All actions are audit-logged
- Emergency: set any override via the API to immediately pause everything

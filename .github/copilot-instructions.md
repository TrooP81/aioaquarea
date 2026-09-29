# Copilot Instructions

## Project Overview

This repository contains two related Python projects:

1. **`aioaquarea/`** – An async Python library for controlling Panasonic Aquarea heat pump devices via the Panasonic Smart Cloud API. Published as a pip package. Primary consumer is [home-assistant-aquarea](https://github.com/cjaliaga/home-assistant-aquarea).

2. **`heatpump-optimizer/`** – A cost-optimizing controller application built on top of `aioaquarea`. Uses FastAPI, PostgreSQL (asyncpg/SQLAlchemy), Redis, APScheduler, and a Next.js web frontend.

## Architecture

### aioaquarea library

The library follows a layered design:

- **`core.py`** (`AquareaClient`) – Main entry point. Exported as `Client` via `__init__.py`. Orchestrates auth, device discovery, and device interaction.
- **`auth.py`** (`Authenticator`) – Handles Panasonic's OAuth2/Auth0 flow (PKCE, token refresh). Uses `PanasonicSettings` for token state and `CCAppVersion` for app version tracking.
- **`api_client.py`** (`AquareaAPIClient`) – Low-level HTTP client wrapping `aiohttp`. Handles request signing, error mapping, and base URL switching (production vs demo).
- **`device_manager.py`** (`DeviceManager`) – Device discovery, grouping, and status parsing from API responses.
- **`device_control.py`** (`AquareaDeviceControl`) – Sends control commands (operation mode, temperature, quiet mode, holiday timer, etc.).
- **`entities.py`** (`DeviceImpl`, `TankImpl`) – Concrete implementations that wire data models to the API client for state mutations.
- **`data.py`** – Dataclasses and enums representing device state (zones, tanks, operation modes, sensors).
- **`decorators.py`** – `@auth_required` decorator: logs in first when `is_logged` is `False`, then re-raises API/auth errors without retrying. Token refresh + single retry on auth failures is handled centrally in `AquareaAPIClient`.
- **`consumption_manager.py`** / **`statistics.py`** – Energy consumption data retrieval and types.
- **`weekly_timer.py`** / **`weekly_timer_manager.py`** – Read-only weekly timer parsing (`WeeklyTimerSettings`, `WeeklyTimerSlot`, `DayOfWeek`). Writes are intentionally unsupported.
- **`command_result.py`** – `PanasonicCommandResult` returned by write commands (HTTP status, response code, request ID).
- **`data_enums.py`** / **`data_models.py`** – Enums and dataclasses re-exported through `data.py` for backwards compatibility.

Public API reference: `docs/library-reference.md`. Wire-level endpoints: `docs/panasonic-aquarea-api.md`.

Key pattern: `TYPE_CHECKING` imports are used throughout to avoid circular dependencies between `core.py` and the manager/entity modules. Use string literal type annotations (e.g., `"AquareaClient"`) when referencing these types at runtime.

### heatpump-optimizer

- **`packages/core/`** – Config (`pydantic-settings`), database (async SQLAlchemy 2.0), domain models, `settings_service.py` (runtime-editable settings persisted to DB), `log_sink.py` (structlog → DB), `resilience.py` (`RateLimiter`, `CircuitBreaker`, Redis-backed `RedisCircuitBreaker`), and `services/` (`AquareaWrapper` implemented in `services/aquarea.py` and re-exported from `services/__init__.py`).
- **`packages/api/`** – FastAPI application. `main.py` creates the app and includes nine `APIRouter` modules from `routers/` (`admin`, `dashboard`, `feeds`, `models_router`, `optimizer`, `panasonic`, `polling`, `settings`, `smartthings`); ~62 routes. Global auth via `dependencies=[Depends(require_auth)]` on the `FastAPI(...)` constructor. `auth.py` defines `require_auth` (bearer token gated by `API_TOKEN`; public paths: `/health`, `/health/ready`, `/api/smartthings/oauth/callback`).
- **`packages/optimizer/`** – Dual-layer optimization: rules engine (`rules_engine.py` + `rule_mixins.py`, version `rules_v7`; `rules.py` is a compatibility re-export) and `milp.py` (PuLP/CBC, `milp_v1`). MILP always falls back to rules on solver error. `executor.py` / `executor_core.py` / `executor_gate.py` dispatch plan actions with verification delay and override checks. `shower_mode.py` handles temporary DHW boost. `data_access.py` reads inputs (prices, status, weather). The package `__init__.py` defines the `Optimizer` Protocol (`generate_plan() -> dict | None`) and three exception types (`InfeasibleError`, `DataIncompleteError`, `SolverTimeoutError`).
- **`packages/ml/`** – COP, demand, comfort, and thermal models (scikit-learn/LightGBM). Model files use HMAC-signed pickle via `safe_persistence.py` — changing `SECRET_KEY` invalidates saved models. `packages/ml/main.py` (weekly retrain) is not a Compose service.
- **`packages/poller/`** – APScheduler-based polling for device status, consumption, prices (ENTSO-E/Tibber/manual), weather (Open-Meteo/SMHI/manual), and SmartThings indoor temps; also comfort-model retraining, seasonal calibration, and alert delivery.
- **`web/`** – Next.js 16 (App Router, React 19) dashboard with Recharts and Lucide icons. Proxies `/api/*` to the Python API through the server-side route handler `app/api/[...path]/route.ts` (`INTERNAL_API_URL`, `INTERNAL_API_TOKEN`). Playwright suites: mocked `e2e/` (`playwright.config.ts`) and live-stack `e2e-live/` (`playwright.live.config.ts`).
- **`migrations/`** – Alembic with async engine. TimescaleDB hypertables are created in migrations (not model definitions). Run by the one-shot `migrate` Compose service.

## Build & Test Commands

### aioaquarea library

```bash
# Install dev dependencies (Python 3.10+)
python -m pip install -e ".[dev]"   # or: pipenv install --dev

# Tests
python -m pytest tests -q

# Lint (matches .github/workflows/library-checks.yml)
python -m black --check aioaquarea tests
python -m isort --check-only aioaquarea tests
python -m pylint --errors-only aioaquarea

# Format
black aioaquarea/ tests/
isort aioaquarea/ tests/
```

The library test suite lives in `tests/` (root) and uses `asyncio_mode = "auto"` from `pyproject.toml`. The library is consumed by `heatpump-optimizer` via `aioaquarea @ git+https://github.com/TrooP81/aioaquarea.git@<full commit SHA>` (declared in `heatpump-optimizer/pyproject.toml`); bump the SHA to pick up library changes.

### heatpump-optimizer

```bash
cd heatpump-optimizer

# Install with all extras (locked)
python -m pip install -c constraints.txt -e ".[all,dev]"

# Run unit tests (tests/e2e needs the Docker test stack)
python -m pytest --ignore=tests/e2e -q

# Run a single test
pytest tests/test_file.py::test_function -v

# Run a single test class
pytest tests/test_file.py::TestClassName -v

# Lint
ruff check packages/ tests/

# Format
ruff format packages/ tests/

# Run database migrations
alembic upgrade head

# Start API server
uvicorn packages.api.main:app --reload

# Backend E2E tests (Windows): spins up docker-compose.test.yml, sets env overrides, runs tests/e2e/
run-tests.bat

# Web frontend
cd web && npm install
npm run dev          # Next.js dev server
npm run build        # production build
npm run lint         # eslint, zero warnings
npm run typecheck    # tsc --noEmit
npm run test:e2e     # Playwright mocked suite (web/e2e, starts its own dev server)
npm run test:e2e:live  # Playwright live suite (web/e2e-live, needs the Docker stack)
npm run test:e2e:ui  # Playwright with UI mode
```

Full environment-variable and runtime-setting reference: `heatpump-optimizer/docs/configuration-reference.md`.

E2E backend tests require a separate test database (Postgres on port 5433, Redis on port 6380, see `docker-compose.test.yml`) and set environment overrides **before** importing app modules. Unit tests are self-contained with no DB dependency. Both `pytest.ini` and `pyproject.toml` set `asyncio_mode = "auto"` (with `asyncio_default_fixture_loop_scope = session` in `pytest.ini`).

## Key Conventions

- Python 3.10+ for `aioaquarea`, Python 3.11+ for `heatpump-optimizer`.
- `from __future__ import annotations` is used consistently throughout both projects.
- All IO is async (`aiohttp` in the library, `httpx`/`asyncpg` in the optimizer).
- Formatting: `black` + `isort` (profile: black) for the library; `ruff` (line-length 100, target py311) for the optimizer.
- The library uses `StrEnum` with a compatibility shim for Python <3.11.
- Enums in `data.py` map directly to Panasonic API integer values — do not change enum values without verifying against the API.
- The `@auth_required` decorator on `AquareaClient` methods handles automatic re-authentication; new authenticated methods should use it.
- `heatpump-optimizer` uses `structlog` for logging (not stdlib `logging`), `pydantic-settings` for configuration, and async SQLAlchemy 2.0 `Mapped`/`mapped_column` patterns.
- The Panasonic API has strict rate limits (30 reads/hr, 20 writes/hr). `AquareaWrapper` in `packages/core/services/` enforces token-bucket rate limiting and a circuit breaker (3 auth failures → 15min cooldown).
- The settings singleton (`settings = Settings()`) is created at module import time. In E2E tests, environment variables must be set **before** importing any app modules.
- The optimizer Protocol (`packages/optimizer/__init__.py`) defines `generate_plan() -> dict | None`. The `optimizer_layer` setting is `rules_only` (default), `milp_preferred`, or `auto`; `auto` uses MILP only when COP and demand models are trained and ≥14 days of COP/consumption data exist, otherwise rules.
- Tests use `asyncio_mode = "auto"` so `@pytest.mark.asyncio` is usually not needed. Tests are class-based (e.g., `class TestRulesOptimizer`) with inline `@pytest.fixture` data helpers.
- DB access in the optimizer uses `async with get_session() as session:` which auto-commits on success and auto-rollbacks on exception.
- Runtime-editable settings live in the DB and are read via `packages.core.settings_service` (`get_setting`, `set_setting`, `get_all_settings`). UI changes via `PUT /api/settings` take precedence over `.env` defaults — when adding a new tunable, register it in `SETTINGS_SCHEMA`.
- Default host ports are non-standard: web `4444`, API `8500`, Postgres `5434` (test DB `5433`, test Redis `6380`). Don't hardcode `localhost:8000` / `5432`.
- The web frontend reaches the API via the server-side proxy route `web/app/api/[...path]/route.ts` (`/api/*` → `INTERNAL_API_URL`, bearer token added server-side); never hardcode the API origin or token in client code.
- `AquareaWrapper.start()` must be called before any device call; it creates the `aiohttp.ClientSession`, opens Redis, and authenticates. Always pair with `stop()` on shutdown.

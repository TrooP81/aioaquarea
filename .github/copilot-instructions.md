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
- **`decorators.py`** – `@auth_required` decorator that auto-retries on token expiration.
- **`consumption_manager.py`** / **`statistics.py`** – Energy consumption data retrieval and types.

Key pattern: `TYPE_CHECKING` imports are used throughout to avoid circular dependencies between `core.py` and the manager/entity modules. Use string literal type annotations (e.g., `"AquareaClient"`) when referencing these types at runtime.

### heatpump-optimizer

- **`packages/core/`** – Config (`pydantic-settings`), database (async SQLAlchemy 2.0), domain models, `settings_service.py` (runtime-editable settings persisted to DB), `log_sink.py` (structlog → DB), and `services/` (`AquareaWrapper` in `services/__init__.py` with rate limiting + circuit breaker, plus token persistence in Redis).
- **`packages/api/`** – FastAPI application. Single-file API (`main.py`, ~50 routes) with no router separation — all routes are flat on `app`. Global auth via `dependencies=[Depends(require_auth)]` on the `FastAPI(...)` constructor. `auth.py` defines the `require_auth` dependency (bearer token gated by `API_TOKEN`).
- **`packages/optimizer/`** – Dual-layer optimization: `rules.py` (v3, deterministic) and `milp.py` (PuLP/CBC). MILP always falls back to rules on error. `executor.py` dispatches plan actions with verification delay and override checks. `shower_mode.py` handles temporary DHW boost. `data_access.py` reads inputs (prices, status, weather). The package `__init__.py` defines the `Optimizer` Protocol (`generate_plan() -> dict | None`) and three exception types (`InfeasibleError`, `DataIncompleteError`, `SolverTimeoutError`).
- **`packages/ml/`** – ML models for COP prediction and demand forecasting (scikit-learn/LightGBM). Model files use HMAC-signed pickle via `safe_persistence.py` — changing `SECRET_KEY` invalidates saved models.
- **`packages/poller/`** – APScheduler-based polling for device status, prices (ENTSO-E/Tibber), weather (Open-Meteo), and SmartThings indoor temps.
- **`web/`** – Next.js 14 (App Router) dashboard with Recharts and Lucide icons. Proxies `/api/*` to the Python API via `next.config.js` rewrites. Playwright for E2E (`playwright.config.ts`).
- **`migrations/`** – Alembic with async engine. TimescaleDB hypertables are created in migrations (not model definitions).

## Build & Test Commands

### aioaquarea library

```bash
# Install dev dependencies (Pipfile pins python_version = 3.10, but project requires-python = ">=3.9")
pipenv install --dev

# Lint
black --check aioaquarea/
isort --check aioaquarea/
pylint aioaquarea/

# Format
black aioaquarea/
isort aioaquarea/
```

No test suite exists for the library currently. The library is consumed by `heatpump-optimizer` via `aioaquarea @ git+https://github.com/cjaliaga/aioaquarea.git@main` (declared in `heatpump-optimizer/pyproject.toml`).

### heatpump-optimizer

```bash
cd heatpump-optimizer

# Install with all extras
pip install -e ".[all,dev]"

# Run all tests
pytest

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
npm run test:e2e     # Playwright E2E (requires API + web running)
npm run test:e2e:ui  # Playwright with UI mode
```

E2E backend tests require a separate test database (Postgres on port 5433, Redis on port 6380, see `docker-compose.test.yml`) and set environment overrides **before** importing app modules. Unit tests are self-contained with no DB dependency. Both `pytest.ini` and `pyproject.toml` set `asyncio_mode = "auto"` (with `asyncio_default_fixture_loop_scope = session` in `pytest.ini`).

## Key Conventions

- Python 3.9+ for `aioaquarea`, Python 3.11+ for `heatpump-optimizer`.
- `from __future__ import annotations` is used consistently throughout both projects.
- All IO is async (`aiohttp` in the library, `httpx`/`asyncpg` in the optimizer).
- Formatting: `black` + `isort` (profile: black) for the library; `ruff` (line-length 100, target py311) for the optimizer.
- The library uses `StrEnum` with a compatibility shim for Python <3.11.
- Enums in `data.py` map directly to Panasonic API integer values — do not change enum values without verifying against the API.
- The `@auth_required` decorator on `AquareaClient` methods handles automatic re-authentication; new authenticated methods should use it.
- `heatpump-optimizer` uses `structlog` for logging (not stdlib `logging`), `pydantic-settings` for configuration, and async SQLAlchemy 2.0 `Mapped`/`mapped_column` patterns.
- The Panasonic API has strict rate limits (30 reads/hr, 20 writes/hr). `AquareaWrapper` in `packages/core/services/` enforces token-bucket rate limiting and a circuit breaker (3 auth failures → 15min cooldown).
- The settings singleton (`settings = Settings()`) is created at module import time. In E2E tests, environment variables must be set **before** importing any app modules.
- The optimizer Protocol (`packages/optimizer/__init__.py`) defines `generate_plan() -> dict | None`. The `auto` mode uses MILP only when ML models are trained with ≥14 days of data; otherwise falls back to rules.
- Tests use `asyncio_mode = "auto"` so `@pytest.mark.asyncio` is usually not needed. Tests are class-based (e.g., `class TestRulesOptimizer`) with inline `@pytest.fixture` data helpers.
- DB access in the optimizer uses `async with get_session() as session:` which auto-commits on success and auto-rollbacks on exception.
- Runtime-editable settings live in the DB and are read via `packages.core.settings_service` (`get_setting`, `set_setting`, `get_all_settings`). UI changes via `PUT /api/settings` take precedence over `.env` defaults — when adding a new tunable, register it in `SETTINGS_SCHEMA`.
- Default host ports are non-standard: web `3500`, API `8500`, Postgres `5434` (test DB `5433`, test Redis `6380`). Don't hardcode `localhost:8000` / `5432`.
- The web frontend reaches the API via Next.js rewrites (`/api/*` → API service); never hardcode the API origin in client code.
- `AquareaWrapper.start()` must be called before any device call; it creates the `aiohttp.ClientSession`, opens Redis, and authenticates. Always pair with `stop()` on shutdown.

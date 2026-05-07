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

- **`packages/core/`** – Config (`pydantic-settings`), database (async SQLAlchemy 2.0), domain models, and services (`AquareaWrapper` with rate limiting + circuit breaker).
- **`packages/api/`** – FastAPI application. Single-file API (`main.py`, ~50 routes) with no router separation — all routes are flat on `app`. Global auth via `dependencies=[Depends(require_auth)]`.
- **`packages/optimizer/`** – Dual-layer optimization: rules engine (v3, deterministic) and MILP solver (PuLP/CBC). MILP always falls back to rules on error. Executor dispatches plan actions with verification delay and override checks.
- **`packages/ml/`** – ML models for COP prediction and demand forecasting (scikit-learn/LightGBM). Model files use HMAC-signed pickle via `safe_persistence.py` — changing `SECRET_KEY` invalidates saved models.
- **`packages/poller/`** – APScheduler-based polling for device status, prices (ENTSO-E/Tibber), weather (Open-Meteo), and SmartThings indoor temps.
- **`web/`** – Next.js 14 (App Router) dashboard with Recharts. Proxies `/api/*` to the Python API via `next.config.js` rewrites.
- **`migrations/`** – Alembic with async engine. TimescaleDB hypertables are created in migrations (not model definitions).

## Build & Test Commands

### aioaquarea library

```bash
# Install dev dependencies
pipenv install --dev

# Lint
black --check aioaquarea/
isort --check aioaquarea/
pylint aioaquarea/

# Format
black aioaquarea/
isort aioaquarea/
```

No test suite exists for the library currently.

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

# Start web frontend
cd web && npm install && npm run dev
```

E2E tests require a separate test database (port 5433) and set environment overrides before importing app modules. Unit tests are self-contained with no DB dependency.

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

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

Key pattern: `TYPE_CHECKING` imports are used throughout to avoid circular dependencies between `core.py` and the manager/entity modules.

### heatpump-optimizer

- **`packages/core/`** – Config, database (SQLAlchemy async), domain models and services.
- **`packages/api/`** – FastAPI application.
- **`packages/optimizer/`** – MILP-based scheduling optimizer (PuLP) and rule-based fallback.
- **`packages/ml/`** – ML models for demand/price forecasting (scikit-learn, LightGBM).
- **`packages/poller/`** – Background data polling from heat pump and external APIs.
- **`web/`** – Next.js frontend.
- **`migrations/`** – Alembic database migrations.

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

## Key Conventions

- Python 3.9+ for `aioaquarea`, Python 3.11+ for `heatpump-optimizer`.
- All IO is async (`aiohttp` in the library, `httpx`/`asyncpg` in the optimizer).
- Formatting: `black` + `isort` (profile: black) for the library; `ruff` for the optimizer.
- The library uses `StrEnum` with a compatibility shim for Python <3.11.
- Enums in `data.py` map directly to Panasonic API integer values – do not change enum values without verifying against the API.
- The `@auth_required` decorator on `AquareaClient` methods handles automatic re-authentication; new authenticated methods should use it.
- `heatpump-optimizer` uses `structlog` for logging, `pydantic-settings` for configuration, and async SQLAlchemy 2.0 patterns.

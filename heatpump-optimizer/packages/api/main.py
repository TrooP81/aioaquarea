"""FastAPI application: REST API for the heat pump optimizer dashboard."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from packages.api.auth import is_auth_enabled, require_auth
from packages.api.routers.admin import router as admin_router
from packages.api.routers.dashboard import router as dashboard_router
from packages.api.routers.feeds import router as feeds_router
from packages.api.routers.models_router import router as models_router
from packages.api.routers.optimizer import router as optimizer_router
from packages.api.routers.panasonic import router as panasonic_router
from packages.api.routers.polling import router as polling_router
from packages.api.routers.settings import router as settings_router
from packages.api.routers.smartthings import router as smartthings_router
from packages.core.config import settings
from packages.core.logging import configure_logging
from packages.core.services import AquareaWrapper
from packages.core.version import API_CONTRACT_VERSION, APP_VERSION

configure_logging("api")
logger = logging.getLogger(__name__)

if not is_auth_enabled():
    import structlog

    structlog.get_logger().warning(
        "api_auth_disabled",
        detail=(
            "API_TOKEN is unset/'disabled' - the API accepts unauthenticated "
            "requests. Set API_TOKEN to a strong secret to require Bearer auth, "
            "especially if the API is reachable beyond localhost."
        ),
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    wrapper: AquareaWrapper | None = None
    if settings.panasonic_distributed_read_quota_enabled:
        wrapper = AquareaWrapper(read_only=True)
        try:
            await wrapper.start()
        except Exception:
            logger.exception("Panasonic quota wrapper startup failed")
            try:
                await wrapper.stop()
            except Exception:
                logger.exception("Panasonic quota wrapper partial shutdown failed")
        else:
            application.state.aquarea_wrapper = wrapper
    try:
        yield
    finally:
        active_wrapper = getattr(application.state, "aquarea_wrapper", None)
        if active_wrapper is not None:
            await active_wrapper.stop()
            del application.state.aquarea_wrapper


app = FastAPI(
    title="Heat Pump Optimizer API",
    version=APP_VERSION,
    description="API for monitoring and optimizing Panasonic Aquarea heat pump costs",
    dependencies=[Depends(require_auth)],
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/api/version")
async def version():
    """Return the version of the running API service."""
    return {"version": APP_VERSION, "api_contract": API_CONTRACT_VERSION}


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a unique request ID to each request/response."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


for router in (
    dashboard_router,
    feeds_router,
    optimizer_router,
    panasonic_router,
    settings_router,
    smartthings_router,
    models_router,
    polling_router,
    admin_router,
):
    app.include_router(router)

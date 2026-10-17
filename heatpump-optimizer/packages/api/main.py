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
    from packages.core.runtime_ownership import close_service_resources, finish_owned_cleanup

    wrapper: AquareaWrapper | None = None
    failed_wrapper_stopped = True
    try:
        configure_logging("api")
        if settings.panasonic_distributed_read_quota_enabled:
            wrapper = AquareaWrapper(read_only=True)
            try:
                await wrapper.start()
            except Exception:
                logger.exception("Panasonic quota wrapper startup failed")
                # Transfer disposal ownership before awaiting so final cleanup cannot
                # stop the failed wrapper a second time, including after cancellation.
                failed_wrapper, wrapper = wrapper, None
                failed_wrapper_stopped = False

                async def stop_failed_wrapper():
                    nonlocal failed_wrapper_stopped
                    await failed_wrapper.stop()
                    failed_wrapper_stopped = True

                await finish_owned_cleanup(stop_failed_wrapper)
            else:
                application.state.aquarea_wrapper = wrapper
        yield
    finally:

        async def cleanup():
            from packages.core.database import engine

            await close_service_resources(
                wrapper=wrapper, engine=engine if failed_wrapper_stopped else None
            )
            if hasattr(application.state, "aquarea_wrapper"):
                del application.state.aquarea_wrapper

        await finish_owned_cleanup(cleanup)


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

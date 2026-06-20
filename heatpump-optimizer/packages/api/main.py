"""FastAPI application: REST API for the heat pump optimizer dashboard."""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from packages.api.auth import require_auth
from packages.api.routers.admin import router as admin_router
from packages.api.routers.dashboard import router as dashboard_router
from packages.api.routers.feeds import router as feeds_router
from packages.api.routers.models_router import router as models_router
from packages.api.routers.optimizer import router as optimizer_router
from packages.api.routers.polling import router as polling_router
from packages.api.routers.settings import router as settings_router
from packages.api.routers.smartthings import router as smartthings_router
from packages.core.config import settings
from packages.core.logging import configure_logging

configure_logging("api")

app = FastAPI(
    title="Heat Pump Optimizer API",
    version="0.1.0",
    description="API for monitoring and optimizing Panasonic Aquarea heat pump costs",
    dependencies=[Depends(require_auth)],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


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
    settings_router,
    smartthings_router,
    models_router,
    polling_router,
    admin_router,
):
    app.include_router(router)

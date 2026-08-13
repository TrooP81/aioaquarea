"""API authentication via Bearer token."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.core.config import settings

_bearer_scheme = HTTPBearer(auto_error=False)

# Paths that skip auth. Health endpoints deliberately reveal only liveness and
# readiness, and must remain reachable by Docker's unauthenticated healthcheck.
# All control and data APIs continue to require the configured bearer token.
_PUBLIC_PATHS: set[str] = {
    "/api/smartthings/oauth/callback",
    "/health",
    "/health/ready",
}


def is_auth_enabled() -> bool:
    """Auth is enabled when api_token is set to a non-placeholder value."""
    token = settings.api_token
    return bool(token) and token != "disabled"


# Backwards-compatible private alias.
_is_auth_enabled = is_auth_enabled


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """
    Dependency that enforces Bearer token auth on protected routes.

    Auth is skipped when api_token is not configured (dev/test convenience),
    and for public paths such as OAuth callbacks.
    """
    if not _is_auth_enabled():
        return

    if request.url.path in _PUBLIC_PATHS:
        return

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(credentials.credentials, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication token",
        )

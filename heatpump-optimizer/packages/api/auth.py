"""API authentication via Bearer token."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.core.config import settings

_bearer_scheme = HTTPBearer(auto_error=False)


def _is_auth_enabled() -> bool:
    """Auth is enabled when api_token is set to a non-placeholder value."""
    token = settings.api_token
    return bool(token) and token != "disabled"


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """
    Dependency that enforces Bearer token auth on protected routes.

    Auth is skipped when api_token is not configured (dev/test convenience).
    """
    if not _is_auth_enabled():
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

"""SmartThings OAuth 2.0 Authorization Code flow — token management with auto-refresh.

Implements RFC 6749 §1.3.1 for SmartThings Connected Services:
  https://developer.smartthings.com/docs/connected-services/oauth-integrations

Tokens are stored in the ``smartthings_oauth_token`` DB table and refreshed
automatically before they expire.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import httpx
import structlog

from packages.core.database import get_session
from packages.core.settings_service import get_setting

logger = structlog.get_logger(__name__)

# SmartThings OAuth endpoints
AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/oauth/token"

# Scopes needed for temperature-sensor read access
DEFAULT_SCOPES = "r:devices:*"

# Refresh tokens before they actually expire (5-minute safety margin)
EXPIRY_MARGIN = dt.timedelta(minutes=5)


class SmartThingsOAuthError(Exception):
    """OAuth token exchange or refresh failed."""


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    *,
    scopes: str = DEFAULT_SCOPES,
) -> tuple[str, str]:
    """Return ``(authorize_url, state)`` for the browser redirect.

    The ``state`` value is a CSRF token that must be verified on callback
    (RFC 6749 §10.12).
    """
    state = secrets.token_urlsafe(32)
    params = httpx.QueryParams(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{params}", state


async def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange the authorization code for access + refresh tokens.

    Returns the raw JSON response from SmartThings::

        {
            "access_token": "...",
            "token_type": "bearer",
            "refresh_token": "...",
            "expires_in": 86400,
            "scope": "r:devices:*"
        }
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )

    if resp.status_code != 200:
        logger.error(
            "smartthings_oauth_code_exchange_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise SmartThingsOAuthError(
            f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )

    return resp.json()


async def refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Use a refresh token to obtain a new access token.

    Returns the same shape as :func:`exchange_code_for_tokens`.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )

    if resp.status_code != 200:
        logger.error(
            "smartthings_oauth_refresh_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise SmartThingsOAuthError(
            f"Token refresh failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )

    return resp.json()


# ------------------------------------------------------------------
# Persisted token CRUD
# ------------------------------------------------------------------


async def save_tokens(token_data: dict[str, Any]) -> None:
    """Persist OAuth tokens to the database (upsert — single row with id=1)."""
    from packages.core.models import SmartThingsToken

    now = dt.datetime.now(dt.timezone.utc)
    expires_in = int(token_data.get("expires_in", 86400))
    expires_at = now + dt.timedelta(seconds=expires_in)

    async with get_session() as session:
        row = await session.get(SmartThingsToken, 1)
        if row is None:
            row = SmartThingsToken(id=1)
            session.add(row)

        row.access_token = token_data["access_token"]
        row.refresh_token = token_data["refresh_token"]
        row.token_type = token_data.get("token_type", "bearer")
        row.scope = token_data.get("scope", "")
        row.expires_at = expires_at
        row.updated_at = now


async def load_tokens() -> dict[str, Any] | None:
    """Load persisted tokens.  Returns ``None`` if not connected."""
    from packages.core.models import SmartThingsToken

    async with get_session() as session:
        row = await session.get(SmartThingsToken, 1)
        if row is None:
            return None
        return {
            "access_token": row.access_token,
            "refresh_token": row.refresh_token,
            "token_type": row.token_type,
            "scope": row.scope,
            "expires_at": row.expires_at,
        }


async def delete_tokens() -> None:
    """Remove persisted tokens (disconnect)."""
    from packages.core.models import SmartThingsToken

    async with get_session() as session:
        row = await session.get(SmartThingsToken, 1)
        if row is not None:
            await session.delete(row)


# ------------------------------------------------------------------
# Token resolver — returns a valid access token (refreshing if needed)
# ------------------------------------------------------------------


async def get_valid_access_token() -> str | None:
    """Return a valid access token, refreshing if needed.

    Falls back to the legacy PAT setting when no OAuth tokens are stored.
    Returns ``None`` when no credentials are available at all.
    """
    tokens = await load_tokens()

    if tokens is None:
        # Fallback: legacy PAT
        pat = await get_setting("smartthings_pat")
        return pat if pat else None

    now = dt.datetime.now(dt.timezone.utc)
    expires_at: dt.datetime = tokens["expires_at"]

    if now + EXPIRY_MARGIN < expires_at:
        # Token is still valid
        return tokens["access_token"]

    # Token expired or about to expire — refresh
    client_id = await get_setting("smartthings_client_id")
    client_secret = await get_setting("smartthings_client_secret")

    if not client_id or not client_secret:
        logger.error("smartthings_oauth_refresh_missing_credentials")
        return None

    try:
        new_tokens = await refresh_access_token(tokens["refresh_token"], client_id, client_secret)
        await save_tokens(new_tokens)
        logger.info("smartthings_oauth_token_refreshed")
        return new_tokens["access_token"]
    except SmartThingsOAuthError:
        logger.exception("smartthings_oauth_refresh_failed")
        return None

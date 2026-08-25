"""Regression tests for UTC-safe client login and token lifetime handling."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from aioaquarea.const import AquareaEnvironment
from aioaquarea.core import AquareaClient
from aioaquarea.decorators import auth_required
from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes


def _client(environment=AquareaEnvironment.PRODUCTION) -> AquareaClient:
    return AquareaClient(
        session=AsyncMock(),
        username=(
            "user@example.test"
            if environment is AquareaEnvironment.PRODUCTION
            else None
        ),
        password="secret" if environment is AquareaEnvironment.PRODUCTION else None,
        environment=environment,
    )


def test_naive_token_expiration_is_normalized_to_utc() -> None:
    client = _client()
    client._api_client.access_token = "token"
    client._api_client.token_expiration = dt.datetime(2099, 1, 1, 12, 0)

    assert client.token_expiration == dt.datetime(
        2099, 1, 1, 12, 0, tzinfo=dt.timezone.utc
    )
    assert client.is_logged is True


def test_non_utc_token_expiration_is_converted_to_utc() -> None:
    client = _client()
    client._api_client.token_expiration = dt.datetime(
        2026, 1, 1, 13, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))
    )

    assert client.token_expiration == dt.datetime(
        2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc
    )


def test_consumption_manager_uses_client_timezone() -> None:
    timezone = ZoneInfo("Europe/Stockholm")
    client = AquareaClient(
        session=AsyncMock(),
        username="user@example.test",
        password="secret",
        timezone=timezone,
    )

    assert client._consumption_manager._timezone is timezone


def test_production_client_registers_serialized_login_for_api_reauthentication() -> None:
    client = _client()

    assert client._api_client._refresh_authentication == client.refresh_login
    assert client._api_client._reauthenticate == client.login


def test_demo_client_does_not_register_recursive_api_reauthentication() -> None:
    client = _client(AquareaEnvironment.DEMO)

    assert client._api_client._refresh_authentication is None
    assert client._api_client._reauthenticate is None


@pytest.mark.asyncio
async def test_refresh_login_publishes_refreshed_token_to_api_requests() -> None:
    client = _client()

    async def refresh_token():
        client._settings.set_token(
            "refreshed-token",
            "next-refresh-token",
            dt.datetime.now(dt.timezone.utc).timestamp() + 3600,
            "scope",
        )

    client._authenticator.refresh_token = AsyncMock(side_effect=refresh_token)

    await client.refresh_login()

    client._authenticator.refresh_token.assert_awaited_once()
    assert client._api_client.access_token == "refreshed-token"
    assert client.token_expiration is not None
    assert client.is_logged is True


@pytest.mark.asyncio
async def test_auth_decorator_does_not_duplicate_api_token_recovery() -> None:
    login = AsyncMock()
    fake_client = SimpleNamespace(is_logged=True, login=login, logger=MagicMock())

    @auth_required
    async def operation(client):
        raise AuthenticationError(
            AuthenticationErrorCodes.TOKEN_EXPIRED,
            "Token expires",
        )

    with pytest.raises(AuthenticationError):
        await operation(fake_client)

    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_login_preserves_token_from_api_response() -> None:
    client = _client(AquareaEnvironment.DEMO)
    client._app_version.init = AsyncMock()

    async def demo_request(*args, **kwargs):
        client._api_client.access_token = "demo-token"
        return object()

    client._api_client.request = AsyncMock(side_effect=demo_request)

    await client.login()

    assert client._api_client.access_token == "demo-token"
    assert client.token_expiration is not None
    assert client.token_expiration.tzinfo is dt.timezone.utc


@pytest.mark.asyncio
async def test_concurrent_login_attempts_share_one_authentication() -> None:
    client = _client()
    client._app_version.init = AsyncMock()

    async def authenticate(*args, **kwargs):
        await asyncio.sleep(0)
        client._settings.set_token(
            "token",
            "refresh",
            dt.datetime.now(dt.timezone.utc).timestamp() + 3600,
            "scope",
        )

    client._authenticator.authenticate = AsyncMock(side_effect=authenticate)

    # Hold the lock until both calls have captured their intent timestamp and
    # are waiting. This exercises true contention rather than two sequential
    # login calls scheduled by the event loop.
    await client._login_lock.acquire()
    first = asyncio.create_task(client.login())
    second = asyncio.create_task(client.login())
    for _ in range(10):
        waiters = client._login_lock._waiters  # noqa: SLF001 - contention regression
        if waiters is not None and len(waiters) == 2:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("Both login attempts did not reach the lock")
    client._login_lock.release()
    await asyncio.gather(first, second)

    client._authenticator.authenticate.assert_awaited_once()
    assert client._last_login.tzinfo is dt.timezone.utc

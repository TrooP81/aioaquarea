import json
import logging
from unittest.mock import AsyncMock

import pytest

from aioaquarea.auth import (
    Authenticator,
    CCAppVersion,
    PanasonicRequestHeader,
    PanasonicSettings,
    check_response,
    generate_random_string,
    get_querystring_parameter_from_header_entry_url,
    has_new_version_been_published,
)
from aioaquarea.const import AquareaEnvironment
from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes


class DummyResponse:
    def __init__(self, *, status=200, text_data="", json_data=None, headers=None):
        self.status = status
        self._text_data = text_data
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}

    async def text(self):
        return self._text_data

    async def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_panasonic_request_header_get_requires_access_token():
    settings = PanasonicSettings()
    app_version = CCAppVersion()

    with pytest.raises(AuthenticationError) as exc:
        await PanasonicRequestHeader.get(settings, app_version)

    assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR


@pytest.mark.asyncio
async def test_panasonic_request_header_includes_expected_fields():
    settings = PanasonicSettings()
    settings.access_token = "token-123"
    settings.clientId = "client-abc"
    app_version = CCAppVersion()
    app_version.version = "9.9.9"

    headers = await PanasonicRequestHeader.get(settings, app_version)

    assert headers["x-app-version"] == "9.9.9"
    assert headers["x-user-authorization-v2"] == "Bearer token-123"
    assert headers["x-client-id"] == "client-abc"
    assert headers["x-cfc-api-key"]


def test_generate_random_string_uses_requested_length():
    value = generate_random_string(43)

    assert len(value) == 43
    assert value.isalnum()


def test_get_querystring_parameter_from_header_entry_url_extracts_value():
    response = DummyResponse(
        headers={"Location": "https://example.test/callback?code=abc123&state=xyz"}
    )

    code = get_querystring_parameter_from_header_entry_url(response, "Location", "code")

    assert code == "abc123"


@pytest.mark.asyncio
async def test_check_response_raises_authentication_error_on_unexpected_status():
    response = DummyResponse(status=500, text_data="server-error")

    with pytest.raises(AuthenticationError) as exc:
        await check_response(response, "login", 200)

    assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
    assert "Unexpected status code 500" in exc.value.error_message


@pytest.mark.asyncio
async def test_has_new_version_been_published_detects_special_401_code():
    response = DummyResponse(status=401, json_data={"code": 4106})

    assert await has_new_version_been_published(response) is True


@pytest.mark.asyncio
async def test_has_new_version_been_published_ignores_other_responses():
    response = DummyResponse(status=401, json_data={"code": 9999})

    assert await has_new_version_been_published(response) is False


@pytest.mark.asyncio
async def test_refresh_token_sends_token_and_preserves_it_when_not_rotated(monkeypatch):
    settings = PanasonicSettings()
    settings.access_token = "old-access-token"
    settings.refresh_token = "stable-refresh-token"
    settings.scope = "openid offline_access"
    session = AsyncMock()
    session.post.return_value = DummyResponse(
        status=200,
        text_data=json.dumps(
            {
                "access_token": "new-access-token",
                "expires_in": 3600,
            }
        ),
    )
    monkeypatch.setattr("aioaquarea.auth.time.time", lambda: 1_700_000_000.0)
    authenticator = Authenticator(
        session,
        settings,
        CCAppVersion(),
        AquareaEnvironment.PRODUCTION,
        logging.getLogger(__name__),
    )

    await authenticator.refresh_token()

    request = session.post.await_args
    assert request.kwargs["json"]["refresh_token"] == "stable-refresh-token"
    assert settings.access_token == "new-access-token"
    assert settings.refresh_token == "stable-refresh-token"
    assert settings.scope == "openid offline_access"
    assert settings.expires_at == 1_700_003_600.0


@pytest.mark.asyncio
async def test_refresh_token_rejects_missing_token_before_request():
    settings = PanasonicSettings()
    session = AsyncMock()
    authenticator = Authenticator(
        session,
        settings,
        CCAppVersion(),
        AquareaEnvironment.PRODUCTION,
        logging.getLogger(__name__),
    )

    with pytest.raises(AuthenticationError) as exc:
        await authenticator.refresh_token()

    assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
    session.post.assert_not_awaited()

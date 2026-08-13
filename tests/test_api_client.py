import datetime as dt
from unittest.mock import AsyncMock

import pytest

from aioaquarea.api_client import AquareaAPIClient
from aioaquarea.auth import CCAppVersion, PanasonicSettings
from aioaquarea.const import AquareaEnvironment
from aioaquarea.errors import ApiError, AuthenticationError, AuthenticationErrorCodes


class FakeResponse:
    def __init__(self, payload, content_type="application/json"):
        self._payload = payload
        self.content_type = content_type

    async def json(self):
        return self._payload


@pytest.fixture
def api_client():
    session = AsyncMock()
    settings = PanasonicSettings()
    settings.access_token = "access-token"
    app_version = CCAppVersion()
    app_version.version = "1.2.3"
    return AquareaAPIClient(
        session=session,
        settings=settings,
        app_version=app_version,
        environment=AquareaEnvironment.PRODUCTION,
    )


@pytest.mark.asyncio
async def test_request_updates_stored_access_token_and_expiration(api_client):
    response = FakeResponse(
        {
            "accessToken": {
                "token": "new-token",
                "expires": "2026-05-08T12:34:56+0000",
            },
            "message": [],
        }
    )
    api_client.access_token = "old-token"
    api_client._sess.request = AsyncMock(return_value=response)

    returned = await api_client.request("GET", url="/test")

    assert returned is response
    assert api_client.access_token == "new-token"
    assert api_client.token_expiration == dt.datetime(
        2026, 5, 8, 12, 34, 56, tzinfo=dt.timezone.utc
    )


@pytest.mark.asyncio
async def test_request_raises_api_error_for_regular_error_messages(api_client):
    response = FakeResponse(
        {
            "message": [{"errorCode": "1234", "errorMessage": "something went wrong"}],
        }
    )
    api_client._sess.request = AsyncMock(return_value=response)

    with pytest.raises(ApiError) as exc:
        await api_client.request("GET", url="/test")

    assert exc.value.error_code == "1234"


@pytest.mark.asyncio
async def test_request_raises_authentication_error_for_token_expiration(api_client):
    response = FakeResponse({"message": ["Token expires soon"]})
    api_client._sess.request = AsyncMock(return_value=response)

    with pytest.raises(AuthenticationError) as exc:
        await api_client.request("GET", url="/test")

    assert exc.value.error_code == AuthenticationErrorCodes.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_request_uses_external_url_when_absolute(api_client):
    response = FakeResponse({"message": []})
    api_client._sess.request = AsyncMock(return_value=response)

    await api_client.request("GET", external_url="https://example.test/path")

    called_url = api_client._sess.request.await_args.args[1]
    assert called_url == "https://example.test/path"


@pytest.mark.asyncio
async def test_look_for_errors_wraps_string_message_as_api_error(api_client):
    errors = await api_client.look_for_errors({"message": ["plain error"]})

    assert len(errors) == 1
    assert isinstance(errors[0], ApiError)
    assert errors[0].error_code == "unknown_error_code"

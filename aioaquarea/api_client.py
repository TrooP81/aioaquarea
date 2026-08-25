import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Optional
import urllib.parse

import aiohttp

from .auth import CCAppVersion, PanasonicRequestHeader, PanasonicSettings
from .const import AQUAREA_SERVICE_BASE, AQUAREA_SERVICE_DEMO_BASE, AquareaEnvironment
from .errors import ApiError, AuthenticationError, AuthenticationErrorCodes

_LOGGER = logging.getLogger(__name__)


class AquareaAPIClient:
    """Base API client for Aquarea."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: PanasonicSettings,
        app_version: CCAppVersion,
        environment: AquareaEnvironment,
        logger: Optional[logging.Logger] = None,
    ):
        self._sess = session
        self._settings = settings
        self._app_version = app_version
        self._environment = environment
        self._logger = logger or logging.getLogger("aioaquarea")
        self._base_url = (
            AQUAREA_SERVICE_BASE
            if environment == AquareaEnvironment.PRODUCTION
            else AQUAREA_SERVICE_DEMO_BASE
        )
        self._access_token: Optional[str] = None
        self._token_expiration: Optional[dt.datetime] = None
        self._refresh_authentication: Callable[[], Awaitable[None]] | None = None
        self._reauthenticate: Callable[[], Awaitable[None]] | None = None

    def set_refresh_authentication_callback(
        self, callback: Callable[[], Awaitable[None]] | None
    ) -> None:
        """Register the owning client's serialized refresh-token callback."""

        self._refresh_authentication = callback

    def set_reauthenticate_callback(
        self, callback: Callable[[], Awaitable[None]] | None
    ) -> None:
        """Register the owning client's serialized production login callback."""

        self._reauthenticate = callback

    @staticmethod
    def _requires_reauthentication(error: ApiError) -> bool:
        return (
            isinstance(error, AuthenticationError)
            and error.error_code == AuthenticationErrorCodes.TOKEN_EXPIRED
        ) or "Missing Authentication Token" in str(error)

    async def request(
        self,
        method: str,
        url: str = None,
        external_url: str = None,
        referer: str = AQUAREA_SERVICE_BASE,
        throw_on_error=True,
        content_type: str = "application/json",
        headers: Optional[dict] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """Make a request with bounded refresh-token and full-login recovery."""

        explicit_headers = dict(headers or {})
        kwarg_headers = dict(kwargs.pop("headers", {}))

        if external_url is not None:
            # If external_url is provided, use it directly or join with base_url if relative
            parsed_external_url = urllib.parse.urlparse(external_url)
            if not parsed_external_url.scheme or not parsed_external_url.netloc:
                url = urllib.parse.urljoin(self._base_url, external_url)
            else:
                url = external_url
        else:
            # If external_url is not provided, join the base_url with the given url
            url = urllib.parse.urljoin(self._base_url, url)
        recovery_callbacks = tuple(
            callback
            for callback in (self._refresh_authentication, self._reauthenticate)
            if callback is not None
        )
        recovery_index = 0

        while True:
            base_headers = await PanasonicRequestHeader.get(
                self._settings, self._app_version
            )
            request_headers = {
                **base_headers,
                **explicit_headers,
                **kwarg_headers,
            }
            resp = await self._sess.request(
                method,
                url,
                **{**kwargs, "headers": request_headers},
            )

            if resp.content_type != "application/json":
                return resp

            data = await resp.json()
            if self._access_token and self.__contains_valid_token(data):
                access_token = data["accessToken"]["token"]
                token_expiration = dt.datetime.strptime(
                    data["accessToken"]["expires"], "%Y-%m-%dT%H:%M:%S%z"
                )
                self.access_token = access_token
                self.token_expiration = token_expiration

                # Panasonic may rotate the bearer token in an ordinary API
                # response. Request headers are built from PanasonicSettings,
                # so keeping only AquareaAPIClient in sync makes the next
                # request reuse the expired token and enter a login loop.
                self._settings.access_token = access_token
                self._settings.expires_at = token_expiration.timestamp()

            if not throw_on_error:
                return resp

            errors = await self.look_for_errors(data)
            if not errors:
                return resp

            error = errors[0]
            if self._requires_reauthentication(error):
                while recovery_index < len(recovery_callbacks):
                    callback = recovery_callbacks[recovery_index]
                    recovery_index += 1
                    recovery_name = (
                        "refresh token"
                        if callback is self._refresh_authentication
                        else "full login"
                    )
                    self._logger.warning(
                        "Panasonic access token expired during API request; "
                        "%s recovery (%d/%d)",
                        recovery_name,
                        recovery_index,
                        len(recovery_callbacks),
                    )
                    try:
                        await callback()
                    except Exception as exc:  # noqa: BLE001 - fall through to full login
                        self._logger.warning(
                            "Panasonic %s recovery failed: %s",
                            recovery_name,
                            exc,
                        )
                        continue
                    break
                else:
                    raise AuthenticationError(error.error_code, error.error_message)
                continue

            if error.error_code in list(AuthenticationErrorCodes):
                raise AuthenticationError(error.error_code, error.error_message)
            raise ApiError(error.error_code, error.error_message)

    def __contains_valid_token(self, data: dict) -> bool:
        """Check if the data contains a valid token."""
        return (
            "accessToken" in data
            and "token" in data["accessToken"]
            and "expires" in data["accessToken"]
        )

    async def look_for_errors(
        self, data: dict
    ) -> list[ApiError]:  # Changed return type to ApiError
        """Look for errors in the response and return them as a list of ApiError objects."""
        if not isinstance(data, dict):
            return []

        messages = data.get("message", [])
        if not isinstance(messages, list):
            messages = [messages]  # Wrap single string message in a list

        api_errors = []
        for error_item in messages:
            if (
                isinstance(error_item, dict)
                and "errorMessage" in error_item
                and "errorCode" in error_item
            ):
                # Check for token expiration message
                if "Token expires" in error_item["errorMessage"]:
                    api_errors.append(
                        AuthenticationError(
                            AuthenticationErrorCodes.TOKEN_EXPIRED,
                            error_item["errorMessage"],
                        )
                    )
                else:
                    api_errors.append(
                        ApiError(error_item["errorCode"], error_item["errorMessage"])
                    )
            elif isinstance(error_item, str):
                # Check for token expiration message in string errors
                if "Token expires" in error_item:
                    api_errors.append(
                        AuthenticationError(
                            AuthenticationErrorCodes.TOKEN_EXPIRED, error_item
                        )
                    )
                else:
                    api_errors.append(ApiError("unknown_error_code", error_item))
        return api_errors

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @access_token.setter
    def access_token(self, value: Optional[str]):
        self._access_token = value

    @property
    def token_expiration(self) -> Optional[dt.datetime]:
        return self._token_expiration

    @token_expiration.setter
    def token_expiration(self, value: Optional[dt.datetime]):
        # Older callers may still pass a naive UTC timestamp. Normalize it at
        # the boundary so comparisons in AquareaClient are always aware/aware.
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            else:
                value = value.astimezone(dt.timezone.utc)
        self._token_expiration = value

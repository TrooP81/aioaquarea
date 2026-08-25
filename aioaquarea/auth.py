import base64
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import string
import time
import urllib.parse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    APP_CLIENT_ID,
    AUTH_0_CLIENT,
    AUTH_API_USER_AGENT,
    AUTH_BROWSER_USER_AGENT,
    BASE_PATH_ACC,
    BASE_PATH_AUTH,
    DEFAULT_X_APP_VERSION,
    REDIRECT_URI,
    AquareaEnvironment,
)
from .errors import AuthenticationError, AuthenticationErrorCodes

_LOGGER = logging.getLogger(__name__)


class PanasonicSettings:
    def __init__(self):
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None
        self.scope = None
        self.clientId = None
        self.username = None
        self.password = None

    def set_token(self, access_token, refresh_token, expires_at, scope):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.scope = scope


class CCAppVersion:
    def __init__(self):
        self.version = DEFAULT_X_APP_VERSION  # Default version

    async def init(self):
        # Try to fetch version on initialization
        await self.refresh()

    async def refresh(self):
        # Fetch the latest app version from App Store
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://apps.apple.com/de/app/panasonic-comfort-cloud/id1348640525"
                ) as response:
                    if response.status == 200:
                        html_content = await response.text()
                        # Look for version pattern in the HTML
                        version_match = re.search(
                            r"Version\s+(\d+\.\d+\.\d+)", html_content
                        )
                        if version_match:
                            new_version = version_match.group(1)
                            _LOGGER.debug(f"Found new app version: {new_version}")
                            self.version = new_version
                        else:
                            _LOGGER.warning(
                                f"Could not parse version from App Store page, keeping current version: {self.version}"
                            )
                    else:
                        _LOGGER.error(
                            f"Failed to fetch App Store page: {response.status}, keeping current version: {self.version}"
                        )
        except Exception as e:
            _LOGGER.error(
                f"Error fetching app version: {e}, keeping current version: {self.version}"
            )

    async def get(self):
        return self.version


class PanasonicRequestHeader:
    @staticmethod
    async def get(
        settings: PanasonicSettings, app_version: CCAppVersion, include_client_id=True
    ):
        if settings.access_token is None:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Access token is missing from settings.",
            )

        now = dt.datetime.now()  # Use dt for datetime
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        api_key = PanasonicRequestHeader._get_api_key(timestamp, settings.access_token)
        _LOGGER.debug(f"Request Timestamp: {timestamp} key: {api_key}")
        headers = {
            "accept": "application/json; charset=utf-8",
            "content-type": "application/json",
            "user-agent": "G-RAC",
            "x-app-name": "Comfort Cloud",
            "x-app-timestamp": timestamp,
            "x-app-type": "1",
            "x-app-version": await app_version.get(),
            "x-cfc-api-key": api_key,
            "x-user-authorization-v2": "Bearer " + settings.access_token,
        }
        if include_client_id and settings.clientId:
            headers["x-client-id"] = settings.clientId
        return headers

    @staticmethod
    def get_aqua_headers(
        content_type: str = "application/x-www-form-urlencoded",
        referer: str = "https://aquarea-smart.panasonic.com/",
        user_agent: str = AUTH_BROWSER_USER_AGENT,
        accept: str | None = None,
    ):
        if accept is None:
            if content_type == "application/json":
                accept = "application/json"
            else:
                accept = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"

        headers = {
            "Cache-Control": "max-age=0",
            "Accept": accept,
            "Accept-Encoding": "deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": user_agent,
            "content-type": content_type,
            "referer": referer,
        }
        return headers

    @staticmethod
    def _get_api_key(timestamp, token):
        try:
            date = dt.datetime.strptime(
                timestamp, "%Y-%m-%d %H:%M:%S"
            )  # Use dt for datetime
            timestamp_ms = str(
                int(date.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            )  # Use dt for datetime

            components = [
                "Comfort Cloud".encode("utf-8"),
                "521325fb2dd486bf4831b47644317fca".encode("utf-8"),
                timestamp_ms.encode("utf-8"),
                "Bearer ".encode("utf-8"),
                token.encode("utf-8"),
            ]

            input_buffer = b"".join(components)
            hash_obj = hashlib.sha256()
            hash_obj.update(input_buffer)
            hash_str = hash_obj.hexdigest()

            result = hash_str[:9] + "cfc" + hash_str[9:]
            return result
        except Exception:
            _LOGGER.error("Failed to generate API key")


# Helper functions from the provided code
def generate_random_string(length: int) -> str:
    return "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(length)
    )


def _decode_json_object(payload: str, context: str) -> dict:
    """Decode an authentication response without leaking its contents."""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthenticationError(
            AuthenticationErrorCodes.API_ERROR,
            f"Panasonic {context} response is not valid JSON.",
        ) from exc
    if not isinstance(decoded, dict):
        raise AuthenticationError(
            AuthenticationErrorCodes.API_ERROR,
            f"Panasonic {context} response is not a JSON object.",
        )
    return decoded


def get_querystring_parameter_from_header_entry_url(
    response: aiohttp.ClientResponse, header_entry, querystring_parameter
):
    header_entry_value = response.headers.get(header_entry)
    if not header_entry_value:
        raise AuthenticationError(
            AuthenticationErrorCodes.API_ERROR,
            f"Authentication response is missing the {header_entry!r} header.",
        )
    parsed_url = urllib.parse.urlparse(header_entry_value)
    params = urllib.parse.parse_qs(parsed_url.query)
    value = params.get(querystring_parameter, [None])[0]
    if value is None:
        raise AuthenticationError(
            AuthenticationErrorCodes.API_ERROR,
            f"Authentication redirect is missing the {querystring_parameter!r} parameter.",
        )
    return value


async def check_response(
    response: aiohttp.ClientResponse, step_name: str, expected_status: int
):
    if response.status != expected_status:
        _LOGGER.error(
            "Error in %s: expected status %s, got %s",
            step_name,
            expected_status,
            response.status,
        )
        raise AuthenticationError(
            AuthenticationErrorCodes.API_ERROR,
            f"Error in {step_name}: Unexpected status code {response.status}",
        )


async def has_new_version_been_published(response: aiohttp.ClientResponse) -> bool:
    if response.status == 401:
        try:
            response_json = await response.json()
        except (aiohttp.ClientError, json.JSONDecodeError, TypeError, ValueError):
            return False
        return isinstance(response_json, dict) and response_json.get("code") == 4106
    return False


class Authenticator:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: PanasonicSettings,
        app_version: CCAppVersion,
        environment: AquareaEnvironment,
        logger: logging.Logger,
    ):
        self._sess = session
        self._settings = settings
        self._app_version = app_version
        self._environment = environment
        self._logger = logger

    async def authenticate(self, username: str, password: str):
        self._sess.cookie_jar.clear_domain("authglb.digital.panasonic.com")
        # generate initial state and code_challenge
        code_verifier = generate_random_string(43)

        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("utf-8")).digest()
            )
            .split("=".encode("utf-8"))[0]
            .decode("utf-8")
        )

        authorization_response = await self._authorize(code_challenge)
        authorization_redirect = authorization_response.headers.get("Location")
        if not authorization_redirect:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Authorization response is missing the redirect location.",
            )

        # check if the user can skip the authentication workflows - in that case,
        # the location is directly pointing to the redirect url with the "code"
        # query parameter included
        if authorization_redirect.startswith(REDIRECT_URI):
            code = get_querystring_parameter_from_header_entry_url(
                authorization_response, "Location", "code"
            )
        else:
            code = await self._login(authorization_response, username, password)

        await self._request_new_token(code, code_verifier)
        await self._retrieve_client_acc()

    async def refresh_token(self):
        self._logger.debug("Refreshing token")
        if not self._settings.refresh_token:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Refresh token is missing from settings.",
            )

        # Capture an absolute instant before the request. ``mktime`` interprets
        # naive local time and can shift expiration across DST transitions.
        unix_time_token_received = time.time()

        response = await self._sess.post(
            f"{BASE_PATH_AUTH}/oauth/token",
            headers={
                "Auth0-Client": AUTH_0_CLIENT,
                "user-agent": AUTH_API_USER_AGENT,
            },
            json={
                "scope": self._settings.scope,
                "client_id": APP_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._settings.refresh_token,
            },
            allow_redirects=False,
        )
        await check_response(response, "refresh_token", 200)
        token_response = _decode_json_object(await response.text(), "refresh token")
        self._set_token(token_response, unix_time_token_received)

    async def _authorize(self, challenge) -> aiohttp.ClientResponse:
        # --------------------------------------------------------------------
        # AUTHORIZE
        # --------------------------------------------------------------------
        state = generate_random_string(20)
        self._logger.debug(
            "Requesting authorization, %s",
            json.dumps({"challenge": challenge, "state": state}),
        )

        response = await self._sess.get(
            f"{BASE_PATH_AUTH}/authorize",
            headers={
                "user-agent": AUTH_API_USER_AGENT,
            },
            params={
                "scope": "openid offline_access comfortcloud.control a2w.control",
                "audience": f"https://digital.panasonic.com/{APP_CLIENT_ID}/api/v1/",
                "protocol": "oauth2",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "auth0Client": AUTH_0_CLIENT,
                "client_id": APP_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": state,
            },
            allow_redirects=False,
        )
        if await has_new_version_been_published(response):
            await self._app_version.refresh()
        await check_response(response, "authorize", 302)
        return response

    async def _login(
        self, authorization_response: aiohttp.ClientResponse, username, password
    ):
        state = get_querystring_parameter_from_header_entry_url(
            authorization_response, "Location", "state"
        )
        location = authorization_response.headers.get("Location")
        if not location:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Authorization redirect is missing its location.",
            )
        self._logger.debug(
            "Following authorization redirect, %s",
            json.dumps({"url": f"{BASE_PATH_AUTH}/{location}", "state": state}),
        )
        response = await self._sess.get(
            f"{BASE_PATH_AUTH}/{location}", allow_redirects=False
        )
        await check_response(response, "authorize_redirect", 200)

        # get the "_csrf" cookie
        csrf_cookie = response.cookies.get("_csrf")
        if csrf_cookie is None:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Authorization response is missing the CSRF cookie.",
            )
        csrf_value = getattr(csrf_cookie, "value", str(csrf_cookie))

        # -------------------------------------------------------------------
        # LOGIN
        # -------------------------------------------------------------------
        self._logger.debug(
            "Authenticating with username and password, %s",
            json.dumps({"csrf_present": True, "state": state}),
        )
        response = await self._sess.post(
            f"{BASE_PATH_AUTH}/usernamepassword/login",
            headers={
                "Auth0-Client": AUTH_0_CLIENT,
                "user-agent": AUTH_API_USER_AGENT,
            },
            json={
                "client_id": APP_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "tenant": "pdpauthglb-a1",
                "response_type": "code",
                "scope": "openid offline_access comfortcloud.control a2w.control",
                "audience": f"https://digital.panasonic.com/{APP_CLIENT_ID}/api/v1/",
                "_csrf": csrf_value,
                "state": state,
                "_intstate": "deprecated",
                "username": username,
                "password": password,
                "lang": "en",
                "connection": "PanasonicID-Authentication",
            },
            allow_redirects=False,
        )
        await check_response(response, "login", 200)

        # -------------------------------------------------------------------
        # CALLBACK
        # -------------------------------------------------------------------

        # get wa, wresult, wctx from body
        response_text = await response.text()
        self._logger.debug("Panasonic authentication form received")
        soup = BeautifulSoup(response_text, "html.parser")
        input_lines = soup.find_all("input", {"type": "hidden"})
        parameters = dict()
        for input_line in input_lines:
            name = input_line.get("name")
            if name:
                parameters[name] = input_line.get("value")

        if not parameters:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic login response contains no callback parameters.",
            )

        self._logger.debug(
            "Panasonic callback parameters found: %s", sorted(parameters)
        )
        response = await self._sess.post(
            url=f"{BASE_PATH_AUTH}/login/callback",
            data=parameters,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": AUTH_BROWSER_USER_AGENT,
            },
            allow_redirects=False,
        )
        await check_response(response, "login_callback", 302)

        # ------------------------------------------------------------------
        # FOLLOW REDIRECT
        # ------------------------------------------------------------------

        location = response.headers.get("Location")
        if not location:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic login callback is missing its redirect location.",
            )
        self._logger.debug(
            "Callback response, %s",
            json.dumps({"redirect": location}),
        )

        response = await self._sess.get(
            f"{BASE_PATH_AUTH}/{location}", allow_redirects=False
        )
        await check_response(response, "login_redirect", 302)
        location = response.headers.get("Location")
        if not location:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic login redirect is missing its callback location.",
            )
        self._logger.debug(
            "Callback redirect, %s",
            json.dumps({"redirect": location}),
        )

        return get_querystring_parameter_from_header_entry_url(
            response, "Location", "code"
        )

    async def _request_new_token(self, code, code_verifier):
        self._logger.debug("Requesting a new token")
        # Capture an absolute instant before the request so token lifetime is
        # independent of the host's timezone and daylight-saving rules.
        unix_time_token_received = time.time()

        response = await self._sess.post(
            f"{BASE_PATH_AUTH}/oauth/token",
            headers={
                "Auth0-Client": AUTH_0_CLIENT,
                "user-agent": AUTH_API_USER_AGENT,
            },
            json={
                "scope": "openid",
                "client_id": APP_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            allow_redirects=False,
        )
        await check_response(response, "get_token", 200)

        token_response = _decode_json_object(await response.text(), "access token")
        self._set_token(token_response, unix_time_token_received)

    def _set_token(self, token_response, unix_time_token_received):
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic token response is missing a valid access token.",
            )
        expires_in = token_response.get("expires_in")
        if isinstance(expires_in, bool):
            expires_in = None
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic token response is missing a valid expiration time.",
            )
        self._settings.set_token(
            access_token,
            token_response.get("refresh_token") or self._settings.refresh_token,
            unix_time_token_received + expires_in,
            token_response.get("scope") or self._settings.scope or "openid",
        )

    async def _retrieve_client_acc(self):
        # ------------------------------------------------------------------
        # RETRIEVE ACC_CLIENT_ID
        # ------------------------------------------------------------------
        headers = await PanasonicRequestHeader.get(
            self._settings, self._app_version, include_client_id=False
        )

        response = await self._sess.post(
            f"{BASE_PATH_ACC}/auth/v2/login", headers=headers, json={"language": 0}
        )
        if await has_new_version_been_published(response):
            self._logger.info("New version of acc client id has been published")
            await self._app_version.refresh()
            response = await self._sess.post(
                f"{BASE_PATH_ACC}/auth/v2/login",
                headers=await PanasonicRequestHeader.get(
                    self._settings, self._app_version, include_client_id=False
                ),
                json={"language": 0},
            )

        await check_response(response, "get_acc_client_id", 200)

        json_body = _decode_json_object(await response.text(), "ACC login")
        client_id = json_body.get("clientId")
        if not isinstance(client_id, str) or not client_id:
            raise AuthenticationError(
                AuthenticationErrorCodes.API_ERROR,
                "Panasonic ACC login response is missing a valid clientId.",
            )
        self._settings.clientId = client_id
        return

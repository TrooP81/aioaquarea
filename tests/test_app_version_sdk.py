"""SDK lifecycle tests for the validated Android app version (offline)."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from aioaquarea import _android_version
from aioaquarea._android_version import AndroidVersionLookupError
from aioaquarea.auth import (
    Authenticator,
    CCAppVersion,
    PanasonicRequestHeader,
    PanasonicSettings,
)
from aioaquarea.const import DEFAULT_X_APP_VERSION, AquareaEnvironment
from aioaquarea.core import AquareaClient
from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes


class Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body if body is not None else {}
        self.released = 0

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    def release(self):
        self.released += 1


VERSION_REJECTED = {"code": 4106, "message": "new version"}


def fetch_mock(monkeypatch, *results):
    fetch = AsyncMock(side_effect=list(results))
    monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
    return fetch


def forbid_fetch(monkeypatch):
    async def fail():
        raise AssertionError("external version lookup must not run")

    monkeypatch.setattr(_android_version, "fetch_android_app_version", fail)


def authenticator(session, app_version):
    settings = PanasonicSettings()
    settings.access_token = "access-token"
    return (
        Authenticator(
            session,
            settings,
            app_version,
            AquareaEnvironment.PRODUCTION,
            logging.getLogger(__name__),
        ),
        settings,
    )


def validated(version="5.1.0"):
    app_version = CCAppVersion()
    app_version.inject_validated(version)
    return app_version


class TestProductionState:
    async def test_unvalidated_production_get_is_refused(self, monkeypatch):
        forbid_fetch(monkeypatch)
        app_version = CCAppVersion()
        assert app_version.version is None
        with pytest.raises(AuthenticationError) as exc:
            await app_version.get()
        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR

    async def test_init_failure_raises_api_error_without_default(self, monkeypatch):
        fetch_mock(monkeypatch, AndroidVersionLookupError("version_not_found"))
        app_version = CCAppVersion()
        with pytest.raises(AuthenticationError) as exc:
            await app_version.init()
        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert app_version.version is None
        with pytest.raises(AuthenticationError):
            await app_version.get()

    async def test_refresh_invalidates_before_fetch(self, monkeypatch):
        app_version = validated("5.1.0")
        observed = {}

        async def fetch():
            observed["validated"] = app_version.is_validated
            observed["version"] = app_version.version
            raise AndroidVersionLookupError("timeout")

        monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
        assert await app_version.refresh() is False
        assert observed == {"validated": False, "version": None}
        with pytest.raises(AuthenticationError):
            await app_version.get()

    async def test_refresh_success_replaces_version(self, monkeypatch):
        fetch_mock(monkeypatch, "5.1.1")
        app_version = validated("5.1.0")
        assert await app_version.refresh() is True
        assert await app_version.get() == "5.1.1"

    async def test_refresh_cancellation_propagates_and_leaves_invalid(
        self, monkeypatch
    ):
        started = asyncio.Event()

        async def fetch():
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
        app_version = validated("5.1.0")
        task = asyncio.ensure_future(app_version.refresh())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app_version.is_validated is False
        assert app_version.version is None

    async def test_init_cancellation_propagates(self, monkeypatch):
        async def fetch():
            raise asyncio.CancelledError()

        monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
        with pytest.raises(asyncio.CancelledError):
            await CCAppVersion().init()

    async def test_superseded_concurrent_refresh_cannot_restore_stale_value(
        self, monkeypatch
    ):
        first_release = asyncio.Event()
        calls = []

        async def fetch():
            calls.append(len(calls))
            if len(calls) == 1:
                await first_release.wait()
                return "5.1.0"
            return "5.1.1"

        monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
        app_version = CCAppVersion()
        first = asyncio.ensure_future(app_version.refresh())
        await asyncio.sleep(0)
        assert await app_version.refresh() is True
        first_release.set()
        assert await first is False
        assert await app_version.get() == "5.1.1"

    async def test_injection_supersedes_in_flight_refresh(self, monkeypatch):
        release = asyncio.Event()

        async def fetch():
            await release.wait()
            return "5.1.0"

        monkeypatch.setattr(_android_version, "fetch_android_app_version", fetch)
        app_version = CCAppVersion()
        task = asyncio.ensure_future(app_version.refresh())
        await asyncio.sleep(0)
        app_version.inject_validated("5.2.0")
        release.set()
        assert await task is False
        assert await app_version.get() == "5.2.0"

    @pytest.mark.parametrize("value", ["", "0.0.0", "5.1", "\uff15.1.0", None])
    async def test_injection_uses_same_validator(self, monkeypatch, value):
        forbid_fetch(monkeypatch)
        app_version = validated("5.1.0")
        with pytest.raises(AuthenticationError) as exc:
            app_version.inject_validated(value)
        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert app_version.is_validated is False
        with pytest.raises(AuthenticationError):
            await app_version.get()

    async def test_validated_init_does_not_look_up_again(self, monkeypatch):
        forbid_fetch(monkeypatch)
        app_version = validated("5.1.0")
        await app_version.init()
        assert await app_version.get() == "5.1.0"

    async def test_header_keeps_android_type_with_validated_version(self):
        settings = PanasonicSettings()
        settings.access_token = "access-token"
        headers = await PanasonicRequestHeader.get(settings, validated("5.1.0"))
        assert headers["x-app-type"] == "1"
        assert headers["x-app-version"] == "5.1.0"

    async def test_production_never_selects_demo_baseline(self, monkeypatch):
        fetch_mock(monkeypatch, AndroidVersionLookupError("transport_error"))
        app_version = CCAppVersion(AquareaEnvironment.PRODUCTION)
        assert await app_version.refresh() is False
        assert app_version.version != DEFAULT_X_APP_VERSION
        with pytest.raises(AuthenticationError):
            await app_version.get()


class TestDemo:
    async def test_demo_uses_diagnostic_baseline_without_lookup(self, monkeypatch):
        forbid_fetch(monkeypatch)
        app_version = CCAppVersion(AquareaEnvironment.DEMO)
        await app_version.init()
        assert await app_version.refresh() is True
        assert await app_version.get() == DEFAULT_X_APP_VERSION

    async def test_demo_rejects_production_injection(self):
        with pytest.raises(AuthenticationError):
            CCAppVersion(AquareaEnvironment.DEMO).inject_validated("5.1.0")

    async def test_demo_real_header_chain_keeps_known_missing_token_error(
        self, monkeypatch
    ):
        """Pre-existing DEMO defect is preserved, not repaired (deferred)."""
        forbid_fetch(monkeypatch)
        session = AsyncMock()
        client = AquareaClient(session=session, environment=AquareaEnvironment.DEMO)
        with pytest.raises(AuthenticationError) as exc:
            await client.login()
        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert exc.value.error_message == "Access token is missing from settings."
        assert session.mock_calls == []


class TestClientLogin:
    async def test_lookup_failure_sends_zero_panasonic_requests(self, monkeypatch):
        fetch = fetch_mock(monkeypatch, AndroidVersionLookupError("timeout"))
        session = AsyncMock()
        client = AquareaClient(
            session=session, username="user@example.test", password="secret"
        )
        with pytest.raises(AuthenticationError) as exc:
            await client.login()
        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert fetch.await_count == 1
        session.get.assert_not_called()
        session.post.assert_not_called()
        session.request.assert_not_called()

    async def test_injected_client_login_performs_no_lookup(self, monkeypatch):
        forbid_fetch(monkeypatch)
        client = AquareaClient(
            session=AsyncMock(), username="user@example.test", password="secret"
        )
        client._app_version.inject_validated("5.1.0")
        client._authenticator.authenticate = AsyncMock()
        await client.login()
        client._authenticator.authenticate.assert_awaited_once()


class TestAccRetry:
    async def test_successful_refresh_allows_single_retry(self, monkeypatch):
        fetch = fetch_mock(monkeypatch, "5.1.1")
        first = Response(401, VERSION_REJECTED)
        second = Response(200, {"clientId": "client-1"})
        session = AsyncMock()
        session.post.side_effect = [first, second]
        auth, settings = authenticator(session, validated("5.1.0"))

        await auth._retrieve_client_acc()

        assert settings.clientId == "client-1"
        assert session.post.await_count == 2
        assert first.released == 1
        assert fetch.await_count == 1
        retry_headers = session.post.await_args_list[1].kwargs["headers"]
        first_headers = session.post.await_args_list[0].kwargs["headers"]
        assert first_headers["x-app-version"] == "5.1.0"
        assert retry_headers["x-app-version"] == "5.1.1"
        assert retry_headers["x-app-type"] == "1"

    async def test_failed_refresh_sends_no_second_acc(self, monkeypatch):
        fetch_mock(monkeypatch, AndroidVersionLookupError("version_not_found"))
        first = Response(401, VERSION_REJECTED)
        session = AsyncMock()
        session.post.side_effect = [first]
        auth, settings = authenticator(session, validated("5.1.0"))

        with pytest.raises(AuthenticationError) as exc:
            await auth._retrieve_client_acc()

        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert session.post.await_count == 1
        assert first.released == 1
        assert settings.clientId is None

    async def test_second_version_rejection_stops(self, monkeypatch):
        fetch = fetch_mock(monkeypatch, "5.1.1", "5.1.2")
        first = Response(401, VERSION_REJECTED)
        second = Response(401, VERSION_REJECTED)
        session = AsyncMock()
        session.post.side_effect = [first, second]
        auth, settings = authenticator(session, validated("5.1.0"))

        with pytest.raises(AuthenticationError) as exc:
            await auth._retrieve_client_acc()

        assert exc.value.error_code == AuthenticationErrorCodes.API_ERROR
        assert session.post.await_count == 2
        assert fetch.await_count == 1
        assert settings.clientId is None

    async def test_refresh_cancellation_propagates_without_second_acc(
        self, monkeypatch
    ):
        fetch_mock(monkeypatch, asyncio.CancelledError())
        first = Response(401, VERSION_REJECTED)
        session = AsyncMock()
        session.post.side_effect = [first]
        auth, _ = authenticator(session, validated("5.1.0"))

        with pytest.raises(asyncio.CancelledError):
            await auth._retrieve_client_acc()

        assert session.post.await_count == 1
        assert first.released == 1

    async def test_other_401_does_not_refresh(self, monkeypatch):
        forbid_fetch(monkeypatch)
        session = AsyncMock()
        session.post.side_effect = [Response(401, {"code": 4100})]
        auth, _ = authenticator(session, validated("5.1.0"))
        with pytest.raises(AuthenticationError):
            await auth._retrieve_client_acc()
        assert session.post.await_count == 1

    async def test_authorize_version_rejection_invalidates_and_raises(
        self, monkeypatch
    ):
        fetch = fetch_mock(monkeypatch, AndroidVersionLookupError("timeout"))
        session = AsyncMock()
        session.get.side_effect = [Response(401, VERSION_REJECTED)]
        app_version = validated("5.1.0")
        auth, _ = authenticator(session, app_version)

        with pytest.raises(AuthenticationError):
            await auth._authorize("challenge")

        assert session.get.await_count == 1
        assert fetch.await_count == 1
        assert app_version.is_validated is False

"""Tests for SmartThings OAuth 2.0 token management."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.poller.smartthings_oauth import (
    build_authorize_url,
    exchange_code_for_tokens,
    refresh_access_token,
    get_valid_access_token,
    save_tokens,
    SmartThingsOAuthError,
    AUTHORIZE_URL,
)


# ------------------------------------------------------------------
# build_authorize_url
# ------------------------------------------------------------------


class TestBuildAuthorizeUrl:
    def test_returns_url_and_state(self):
        url, state = build_authorize_url("my-client-id", "https://example.com/callback")

        assert url.startswith(AUTHORIZE_URL)
        assert "client_id=my-client-id" in url
        assert "redirect_uri=" in url
        assert "response_type=code" in url
        assert "scope=" in url
        assert f"state={state}" in url

    def test_state_is_unique(self):
        _, s1 = build_authorize_url("c", "https://x.com/cb")
        _, s2 = build_authorize_url("c", "https://x.com/cb")
        assert s1 != s2

    def test_custom_scopes(self):
        url, _ = build_authorize_url("c", "https://x.com/cb", scopes="r:devices:* w:devices:*")
        assert "r%3Adevices%3A" in url or "r:devices:" in url


# ------------------------------------------------------------------
# exchange_code_for_tokens
# ------------------------------------------------------------------


class TestExchangeCodeForTokens:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "acc-123",
            "refresh_token": "ref-456",
            "token_type": "bearer",
            "expires_in": 86400,
            "scope": "r:devices:*",
        }

        with patch("packages.poller.smartthings_oauth.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            tokens = await exchange_code_for_tokens(
                "auth-code", "client-id", "client-secret", "https://x.com/cb"
            )

        assert tokens["access_token"] == "acc-123"
        assert tokens["refresh_token"] == "ref-456"

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "invalid_grant"

        with patch("packages.poller.smartthings_oauth.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(SmartThingsOAuthError, match="Token exchange failed"):
                await exchange_code_for_tokens(
                    "bad-code", "client-id", "client-secret", "https://x.com/cb"
                )


# ------------------------------------------------------------------
# refresh_access_token
# ------------------------------------------------------------------


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-acc",
            "refresh_token": "new-ref",
            "token_type": "bearer",
            "expires_in": 86400,
        }

        with patch("packages.poller.smartthings_oauth.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            tokens = await refresh_access_token("old-ref", "client-id", "client-secret")

        assert tokens["access_token"] == "new-acc"

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "invalid_token"

        with patch("packages.poller.smartthings_oauth.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(SmartThingsOAuthError, match="Token refresh failed"):
                await refresh_access_token("bad-ref", "client-id", "client-secret")


class TestSaveTokens:
    @pytest.mark.asyncio
    async def test_preserves_refresh_token_when_refresh_response_omits_replacement(self):
        row = SimpleNamespace(refresh_token="working-refresh-token")
        session = AsyncMock()
        session.get = AsyncMock(return_value=row)
        session_context = AsyncMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "packages.poller.smartthings_oauth.get_session",
            return_value=session_context,
        ):
            await save_tokens(
                {
                    "access_token": "new-access-token",
                    "expires_in": 86400,
                }
            )

        assert row.access_token == "new-access-token"
        assert row.refresh_token == "working-refresh-token"


# ------------------------------------------------------------------
# get_valid_access_token
# ------------------------------------------------------------------


class TestGetValidAccessToken:
    @pytest.mark.asyncio
    async def test_no_oauth_tokens_falls_back_to_pat(self):
        """When no OAuth tokens exist, fall back to legacy PAT."""
        with patch(
            "packages.poller.smartthings_oauth.load_tokens",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                return_value="my-legacy-pat",
            ):
                token = await get_valid_access_token()

        assert token == "my-legacy-pat"

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self):
        """When no OAuth tokens and no PAT, return None."""
        with patch(
            "packages.poller.smartthings_oauth.load_tokens",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                return_value="",
            ):
                token = await get_valid_access_token()

        assert token is None

    @pytest.mark.asyncio
    async def test_valid_token_returned_directly(self):
        """A non-expired token is returned without refresh."""
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12)
        with patch(
            "packages.poller.smartthings_oauth.load_tokens",
            new_callable=AsyncMock,
            return_value={
                "access_token": "still-valid",
                "refresh_token": "ref",
                "expires_at": future,
            },
        ):
            token = await get_valid_access_token()

        assert token == "still-valid"

    @pytest.mark.asyncio
    async def test_expired_token_triggers_refresh(self):
        """An expired token triggers automatic refresh."""
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        with patch(
            "packages.poller.smartthings_oauth.load_tokens",
            new_callable=AsyncMock,
            return_value={
                "access_token": "expired",
                "refresh_token": "ref-tok",
                "expires_at": past,
            },
        ):
            with patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                side_effect=lambda k: {
                    "smartthings_client_id": "cid",
                    "smartthings_client_secret": "csec",
                }.get(k, ""),
            ):
                with patch(
                    "packages.poller.smartthings_oauth.refresh_access_token",
                    new_callable=AsyncMock,
                    return_value={
                        "access_token": "refreshed-token",
                        "refresh_token": "new-ref",
                        "expires_in": 86400,
                    },
                ) as mock_refresh:
                    with patch(
                        "packages.poller.smartthings_oauth.save_tokens",
                        new_callable=AsyncMock,
                    ) as mock_save:
                        token = await get_valid_access_token()

        assert token == "refreshed-token"
        mock_refresh.assert_awaited_once_with("ref-tok", "cid", "csec")
        mock_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_returns_none(self):
        """If refresh fails, return None rather than crash."""
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        with patch(
            "packages.poller.smartthings_oauth.load_tokens",
            new_callable=AsyncMock,
            return_value={
                "access_token": "expired",
                "refresh_token": "ref",
                "expires_at": past,
            },
        ):
            with patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                side_effect=lambda k: {
                    "smartthings_client_id": "cid",
                    "smartthings_client_secret": "csec",
                }.get(k, ""),
            ):
                with patch(
                    "packages.poller.smartthings_oauth.refresh_access_token",
                    new_callable=AsyncMock,
                    side_effect=SmartThingsOAuthError("token revoked"),
                ):
                    token = await get_valid_access_token()

        assert token is None

    @pytest.mark.asyncio
    async def test_server_rejected_unexpired_oauth_token_triggers_refresh(self):
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12)
        with (
            patch(
                "packages.poller.smartthings_oauth.load_tokens",
                new_callable=AsyncMock,
                return_value={
                    "access_token": "rejected-token",
                    "refresh_token": "ref-tok",
                    "expires_at": future,
                },
            ),
            patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                side_effect=lambda key: {
                    "smartthings_client_id": "cid",
                    "smartthings_client_secret": "csec",
                }.get(key, ""),
            ),
            patch(
                "packages.poller.smartthings_oauth.refresh_access_token",
                new_callable=AsyncMock,
                return_value={
                    "access_token": "fresh-token",
                    "refresh_token": "new-ref",
                    "expires_in": 86400,
                },
            ) as refresh,
            patch(
                "packages.poller.smartthings_oauth.save_tokens",
                new_callable=AsyncMock,
            ),
        ):
            token = await get_valid_access_token(rejected_access_token="rejected-token")

        assert token == "fresh-token"
        refresh.assert_awaited_once_with("ref-tok", "cid", "csec")

    @pytest.mark.asyncio
    async def test_concurrently_replaced_token_is_reused_without_refresh(self):
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12)
        with (
            patch(
                "packages.poller.smartthings_oauth.load_tokens",
                new_callable=AsyncMock,
                return_value={
                    "access_token": "already-refreshed",
                    "refresh_token": "new-ref",
                    "expires_at": future,
                },
            ),
            patch(
                "packages.poller.smartthings_oauth.refresh_access_token",
                new_callable=AsyncMock,
            ) as refresh,
        ):
            token = await get_valid_access_token(rejected_access_token="rejected-token")

        assert token == "already-refreshed"
        refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_legacy_pat_is_not_retried(self):
        with (
            patch(
                "packages.poller.smartthings_oauth.load_tokens",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "packages.poller.smartthings_oauth.get_setting",
                new_callable=AsyncMock,
                return_value="rejected-pat",
            ),
        ):
            token = await get_valid_access_token(rejected_access_token="rejected-pat")

        assert token is None

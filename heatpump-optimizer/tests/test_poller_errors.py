"""Tests for poller error handling paths."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from packages.core.services import (
    PanasonicAdapterBackoffError,
    PanasonicAdapterUnavailableError,
)
from packages.poller.feeds import FetchContext, PriceFeed, ProviderPriceList
from packages.poller.main import (
    _record_panasonic_adapter_state,
    poll_device_status,
    poll_indoor_temp,
    poll_prices,
    poll_weather,
)


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_adapter_heartbeat_uses_sanitized_reason_code():
    with patch(
        "packages.poller.main.record_service_heartbeat", new_callable=AsyncMock
    ) as record_heartbeat:
        await _record_panasonic_adapter_state(
            status="unavailable",
            device_id="device-1",
            reason="API error: unknown_error_code - Failed communication with adaptor",
            consecutive_failures=1,
            retry_after_seconds=300,
        )

    adapter = record_heartbeat.await_args.kwargs["panasonic_adapter"]
    assert adapter["reason"] == "adaptor_communication_failed"
    assert adapter["retry_at"] is not None


class TestPollDeviceStatusErrors:
    @pytest.mark.asyncio
    async def test_adapter_outage_has_structured_warning(self):
        async def refresh_device():
            raise PanasonicAdapterUnavailableError(
                device_id="device-1",
                reason="adaptor offline",
                consecutive_failures=2,
                retry_after_seconds=600,
            )

        wrapper = MagicMock(refresh_device=refresh_device)

        with (
            patch("packages.poller.main.logger") as mock_logger,
            patch(
                "packages.poller.main._record_panasonic_adapter_state",
                new_callable=AsyncMock,
            ) as record_state,
        ):
            await poll_device_status(wrapper)

        record_state.assert_awaited_once_with(
            status="unavailable",
            device_id="device-1",
            reason="adaptor offline",
            consecutive_failures=2,
            retry_after_seconds=600,
        )
        mock_logger.warning.assert_called_once_with(
            "panasonic_adapter_unavailable",
            device_id="device-1",
            consecutive_failures=2,
            retry_after_seconds=600,
            reason="adaptor offline",
        )
        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_adapter_backoff_has_structured_info(self):
        async def refresh_device():
            raise PanasonicAdapterBackoffError(
                device_id="device-1",
                reason="adaptor offline",
                consecutive_failures=2,
                retry_after_seconds=299,
            )

        wrapper = MagicMock(refresh_device=refresh_device)

        with (
            patch("packages.poller.main.logger") as mock_logger,
            patch(
                "packages.poller.main._record_panasonic_adapter_state",
                new_callable=AsyncMock,
            ) as record_state,
        ):
            await poll_device_status(wrapper)

        record_state.assert_awaited_once_with(
            status="backoff",
            device_id="device-1",
            reason="adaptor offline",
            consecutive_failures=2,
            retry_after_seconds=299,
        )
        mock_logger.info.assert_called_once_with(
            "panasonic_adapter_backoff",
            device_id="device-1",
            consecutive_failures=2,
            retry_after_seconds=299,
            reason="adaptor offline",
        )
        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_connection_failure_is_caught(self):
        async def refresh_device():
            raise RuntimeError("Auth expired")

        wrapper = MagicMock(refresh_device=refresh_device)

        # Should not raise
        await poll_device_status(wrapper)

    @pytest.mark.asyncio
    async def test_attribute_error_on_device(self):
        async def refresh_device():
            return None

        wrapper = MagicMock(refresh_device=refresh_device)

        await poll_device_status(wrapper)


class TestPollPricesErrors:
    @pytest.mark.asyncio
    async def test_fetch_prices_network_error(self):
        with patch("packages.poller.main.fetch_price_feed", new_callable=AsyncMock) as mock_fp:
            mock_fp.side_effect = Exception("Network timeout")
            # Should not raise
            await poll_prices()

    @pytest.mark.asyncio
    async def test_fetch_prices_empty_result(self):
        async def fetch_price_feed():
            return PriceFeed([], "EUR", "entsoe")

        with patch("packages.poller.main.fetch_price_feed", new=fetch_price_feed):
            with patch("packages.poller.main.get_session") as mock_session:
                mock_ctx = MagicMock()
                mock_session.return_value = _AsyncContextManager(mock_ctx)
                await poll_prices()

    @pytest.mark.parametrize("schema", ["hourly_duration_weighted_v1", None])
    async def test_S3_R16_persists_fetch_context_and_transports_schema(self, schema):
        clock = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        feed = PriceFeed(
            [(clock, -0.2)],
            "SEK",
            "entsoe",
            FetchContext("entsoe", clock, area="original-area"),
            schema,
        )
        session = AsyncMock()
        with (
            patch("packages.poller.main.fetch_price_feed", AsyncMock(return_value=feed)),
            patch(
                "packages.core.settings_service.get_string_setting",
                AsyncMock(return_value="changed-after-fetch"),
            ) as settings_reader,
            patch(
                "packages.poller.main.get_session",
                return_value=_AsyncContextManager(session),
            ),
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                AsyncMock(return_value={}),
            ),
            patch("packages.poller.main._queue_price_horizon_reoptimization", AsyncMock()),
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_prices()

        session.execute.assert_awaited_once()
        values = session.execute.await_args.args[0].compile().params
        assert values["area_m0"] == "original-area"
        assert values["price_source_m0"] == "entsoe"
        assert values["price_currency_m0"] == "SEK"
        assert values["price_eur_per_kwh_m0"] == -0.2
        assert values["fetched_at_m0"] == clock
        assert values["interval_schema_m0"] == schema
        settings_reader.assert_not_awaited()
        logger.error.assert_not_called()
        assert logger.info.call_args.kwargs["provider"] == "entsoe"

    @pytest.mark.parametrize(
        "context",
        [
            None,
            FetchContext(
                "manual",
                dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                area="original-area",
            ),
        ],
    )
    async def test_S3_R16_missing_or_conflicting_context_fails_without_persistence(self, context):
        clock = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        feed = PriceFeed([(clock, 0.2)], "EUR", "entsoe", context)
        with (
            patch("packages.poller.main.fetch_price_feed", AsyncMock(return_value=feed)),
            patch("packages.poller.main.get_session") as session_factory,
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                AsyncMock(),
            ) as quality,
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_prices()

        session_factory.assert_not_called()
        quality.assert_not_awaited()
        logger.error.assert_called_once_with("price_fetch_failed", error="ValueError")

    @pytest.mark.parametrize(
        "reason",
        [
            "currency_conflict",
            "duplicate_timestamp",
            "invalid_price_or_timestamp",
            "non_hourly_timestamp",
            "graphql_error",
            "malformed_response",
            None,
        ],
    )
    async def test_S3_R16_rejected_feed_logs_failure_not_success(self, reason):
        # Exercise provider-list -> PriceFeed -> poll boundary transport, offline.
        provider_result = ProviderPriceList(
            [], currency="", source="tibber", rejection_reason=reason
        )
        session = AsyncMock()
        with (
            patch("packages.poller.feeds.get_setting", AsyncMock(return_value="tibber")),
            patch(
                "packages.poller.feeds._fetch_prices_tibber",
                AsyncMock(return_value=provider_result),
            ),
            patch(
                "packages.poller.main.get_session",
                return_value=_AsyncContextManager(session),
            ) as session_factory,
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                AsyncMock(),
            ) as quality,
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_prices()

        quality.assert_not_awaited()
        session.execute.assert_not_awaited()
        if reason is not None:
            session_factory.assert_not_called()
            logger.error.assert_called_once_with(
                "price_fetch_failed",
                error="provider_validation_failed",
                provider="tibber",
                reason=reason,
            )
            logger.info.assert_not_called()
        else:
            session_factory.assert_called_once_with()
            logger.error.assert_not_called()
            logger.info.assert_called_once_with(
                "prices_fetched", count=0, provider="tibber", currency=""
            )

    @pytest.mark.parametrize("field", ["today", "tomorrow", "currentSubscription", "priceInfo"])
    @pytest.mark.parametrize(
        "value",
        [{}, False, 0, "", [], None, "absent"],
        ids=["object", "false", "zero", "empty-string", "array", "null", "absent"],
    )
    async def test_S3_R16_original_structures_reach_poller_boundary(self, field, value):
        home = {"currentSubscription": {"priceInfo": {"today": [], "tomorrow": []}}}
        parent = (
            home
            if field == "currentSubscription"
            else home["currentSubscription"]
            if field == "priceInfo"
            else home["currentSubscription"]["priceInfo"]
        )
        if value == "absent":
            parent.pop(field, None)
        else:
            parent[field] = value
        response = MagicMock()
        response.json.return_value = {"data": {"viewer": {"homes": [home]}}}
        client = MagicMock(post=AsyncMock(return_value=response))
        session = AsyncMock()
        config = {"price_provider": "tibber", "tibber_api_token": "offline-token"}
        with (
            patch(
                "packages.poller.feeds.get_setting",
                AsyncMock(side_effect=lambda key: config.get(key, "")),
            ),
            patch(
                "packages.poller.feeds.httpx.AsyncClient",
                return_value=_AsyncContextManager(client),
            ),
            patch(
                "packages.poller.main.get_session",
                return_value=_AsyncContextManager(session),
            ) as session_factory,
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                AsyncMock(),
            ) as quality,
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_prices()

        client.post.assert_awaited_once()
        quality.assert_not_awaited()
        session.execute.assert_not_awaited()
        list_field = field in ("today", "tomorrow")
        wrong_type = (
            value is not None
            and value != "absent"
            and not isinstance(value, list if list_field else dict)
        )
        if wrong_type:
            session_factory.assert_not_called()
            logger.error.assert_called_once_with(
                "price_fetch_failed",
                error="provider_validation_failed",
                provider="tibber",
                reason="malformed_price_entries" if list_field else "malformed_response",
            )
            logger.info.assert_not_called()
        else:
            session_factory.assert_called_once_with()
            logger.error.assert_not_called()
            logger.info.assert_called_once_with(
                "prices_fetched", count=0, provider="tibber", currency=""
            )

    @pytest.mark.parametrize("failure", ["http_status", "request", "unexpected"])
    async def test_S3_R16_price_failure_logs_never_include_credentials(self, failure):
        dummy_token = "dummy-S3-token-never-log"
        request = httpx.Request(
            "GET",
            "https://web-api.tp.entsoe.eu/api",
            params={"securityToken": dummy_token, "documentType": "A44"},
        )
        if failure == "http_status":
            response = httpx.Response(401, request=request)
            error = httpx.HTTPStatusError(
                f"untrusted {request.url}", request=request, response=response
            )
            expected = {"error": "http_status_error", "status_code": 401}
        elif failure == "request":
            error = httpx.ConnectError(f"untrusted {request.url}", request=request)
            expected = {"error": "ConnectError"}
        else:
            error = ValueError(f"untrusted {request.url}")
            expected = {"error": "ValueError"}
        with (
            patch("packages.poller.main.fetch_price_feed", AsyncMock(side_effect=error)),
            patch("packages.poller.main.get_session") as session_factory,
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_prices()

        session_factory.assert_not_called()
        logger.error.assert_called_once_with("price_fetch_failed", **expected)
        logger.info.assert_not_called()
        logged = repr(logger.mock_calls)
        assert dummy_token not in logged
        assert "securityToken" not in logged
        assert "documentType" not in logged
        assert "https://" not in logged


class TestPollWeatherErrors:
    @pytest.mark.asyncio
    async def test_fetch_weather_network_error(self):
        with patch("packages.poller.main.fetch_weather", new_callable=AsyncMock) as mock_fw:
            mock_fw.side_effect = Exception("DNS lookup failed")
            # Should not raise
            await poll_weather()

    @pytest.mark.parametrize("schema", ["weather_c_mps_wm2_mm_v1", None])
    async def test_S3_R17_weather_persistence_preserves_provenance(self, schema):
        clock = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        row = {
            "ts": clock,
            "source": "smhi",
            "temperature": 0.0,
            "wind_speed": 0.0,
            "irradiance": 0.0,
            "forecast_issued_at": clock,
            "units_schema": schema,
        }
        session = AsyncMock()
        with (
            patch("packages.poller.main.fetch_weather", AsyncMock(return_value=[row])),
            patch(
                "packages.poller.main.get_session",
                return_value=_AsyncContextManager(session),
            ),
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_weather()

        session.execute.assert_awaited_once()
        values = session.execute.await_args.args[0].compile().params
        assert values["source_m0"] == "smhi"
        assert values["forecast_issued_at_m0"] == clock
        assert values["units_schema_m0"] == schema
        assert values["temperature_m0"] == 0.0
        logger.error.assert_not_called()

    async def test_S3_R17_missing_source_is_not_defaulted_to_open_meteo(self):
        row = {"ts": dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), "temperature": 0.0}
        session = AsyncMock()
        with (
            patch("packages.poller.main.fetch_weather", AsyncMock(return_value=[row])),
            patch(
                "packages.poller.main.get_session",
                return_value=_AsyncContextManager(session),
            ),
            patch("packages.poller.main.logger") as logger,
        ):
            await poll_weather()

        session.execute.assert_not_awaited()
        assert logger.error.call_args.args == ("weather_fetch_failed",)


class TestPollIndoorTempErrors:
    @pytest.mark.asyncio
    async def test_smartthings_disabled(self):
        with patch("packages.poller.main.get_bool_setting", new_callable=AsyncMock) as mock_gs:
            mock_gs.return_value = False
            await poll_indoor_temp()

    @pytest.mark.asyncio
    async def test_smartthings_error_caught(self):
        with patch("packages.poller.main.get_bool_setting", new_callable=AsyncMock) as mock_gs:
            mock_gs.return_value = True
            with patch("packages.poller.main.get_session") as mock_session:
                mock_session.side_effect = Exception("DB down")
                await poll_indoor_temp()

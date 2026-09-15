"""Tests for poller error handling paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.services import (
    PanasonicAdapterBackoffError,
    PanasonicAdapterUnavailableError,
)
from packages.poller.feeds import PriceFeed
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

        async def get_string_setting(key):
            return {"price_provider": "entsoe"}.get(key, "")

        with patch("packages.poller.main.fetch_price_feed", new=fetch_price_feed):
            with patch("packages.poller.main.get_string_setting", new=get_string_setting):
                with patch("packages.poller.main.get_session") as mock_session:
                    mock_ctx = MagicMock()
                    mock_session.return_value = _AsyncContextManager(mock_ctx)
                    await poll_prices()


class TestPollWeatherErrors:
    @pytest.mark.asyncio
    async def test_fetch_weather_network_error(self):
        with patch("packages.poller.main.fetch_weather", new_callable=AsyncMock) as mock_fw:
            mock_fw.side_effect = Exception("DNS lookup failed")
            # Should not raise
            await poll_weather()


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

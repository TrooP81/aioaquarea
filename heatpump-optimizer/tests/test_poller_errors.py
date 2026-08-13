"""Tests for poller error handling paths."""

from unittest.mock import AsyncMock, patch

import pytest

from packages.core.services import (
    PanasonicAdapterBackoffError,
    PanasonicAdapterUnavailableError,
)
from packages.poller.feeds import PriceFeed
from packages.poller.main import poll_device_status, poll_indoor_temp, poll_prices, poll_weather


class TestPollDeviceStatusErrors:
    @pytest.mark.asyncio
    async def test_adapter_outage_has_structured_warning(self):
        wrapper = AsyncMock()
        wrapper.refresh_device.side_effect = PanasonicAdapterUnavailableError(
            device_id="device-1",
            reason="adaptor offline",
            consecutive_failures=2,
            retry_after_seconds=600,
        )

        with patch("packages.poller.main.logger") as mock_logger:
            await poll_device_status(wrapper)

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
        wrapper = AsyncMock()
        wrapper.refresh_device.side_effect = PanasonicAdapterBackoffError(
            device_id="device-1",
            reason="adaptor offline",
            consecutive_failures=2,
            retry_after_seconds=299,
        )

        with patch("packages.poller.main.logger") as mock_logger:
            await poll_device_status(wrapper)

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
        wrapper = AsyncMock()
        wrapper.refresh_device.side_effect = RuntimeError("Auth expired")

        # Should not raise
        await poll_device_status(wrapper)

    @pytest.mark.asyncio
    async def test_attribute_error_on_device(self):
        wrapper = AsyncMock()
        wrapper.refresh_device.return_value = None  # Will cause AttributeError

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
        with patch("packages.poller.main.fetch_price_feed", new_callable=AsyncMock) as mock_fp:
            mock_fp.return_value = PriceFeed([], "EUR", "entsoe")
            with patch(
                "packages.poller.main.get_string_setting", new_callable=AsyncMock
            ) as mock_gs:
                mock_gs.return_value = "entsoe"
                with patch("packages.poller.main.get_session") as mock_session:
                    mock_ctx = AsyncMock()
                    mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                    mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
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

"""Tests for SmartThings API client and poller integration."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.poller.smartthings import (
    SmartThingsClient,
    SmartThingsAuthError,
    SmartThingsError,
    SmartThingsRateLimited,
    STALE_READING_THRESHOLD,
    _to_celsius,
    _check_response,
)


# ------------------------------------------------------------------
# Unit: temperature conversion
# ------------------------------------------------------------------


class TestToCelsius:
    def test_celsius_passthrough(self):
        assert _to_celsius(21.5, "C") == 21.5

    def test_fahrenheit_conversion(self):
        assert abs(_to_celsius(72.0, "F") - 22.222) < 0.01

    def test_fahrenheit_uppercase(self):
        assert abs(_to_celsius(32.0, "FAHRENHEIT") - 0.0) < 0.01


# ------------------------------------------------------------------
# Unit: response checking
# ------------------------------------------------------------------


class TestCheckResponse:
    def test_401_raises_auth_error(self):
        resp = MagicMock()
        resp.status_code = 401
        with pytest.raises(SmartThingsAuthError):
            _check_response(resp)

    def test_403_raises_auth_error(self):
        resp = MagicMock()
        resp.status_code = 403
        with pytest.raises(SmartThingsAuthError):
            _check_response(resp)

    def test_429_raises_rate_limited(self):
        resp = MagicMock()
        resp.status_code = 429
        with pytest.raises(SmartThingsRateLimited):
            _check_response(resp)


# ------------------------------------------------------------------
# SmartThingsClient
# ------------------------------------------------------------------


class TestSmartThingsClient:
    @pytest.mark.asyncio
    async def test_discover_temp_sensors(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "deviceId": "abc-123",
                    "label": "Living Room Sensor",
                    "roomId": "room-1",
                },
                {
                    "deviceId": "def-456",
                    "name": "Bedroom",
                    "roomId": "room-2",
                },
            ]
        }

        with patch("packages.poller.smartthings.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = SmartThingsClient("test-pat")
            devices = await client.discover_temp_sensors()

        assert len(devices) == 2
        assert devices[0]["device_id"] == "abc-123"
        assert devices[0]["label"] == "Living Room Sensor"
        assert devices[1]["label"] == "Bedroom"

    @pytest.mark.asyncio
    async def test_get_temperature_celsius(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "temperature": {
                "value": 21.3,
                "unit": "C",
                "timestamp": "2026-06-01T10:00:00Z",
            }
        }

        with patch("packages.poller.smartthings.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = SmartThingsClient("test-pat")
            reading = await client.get_temperature("device-1")

        assert reading is not None
        assert reading["value"] == 21.3
        assert reading["timestamp"] == "2026-06-01T10:00:00Z"

    @pytest.mark.asyncio
    async def test_get_temperature_fahrenheit_converted(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "temperature": {
                "value": 72.0,
                "unit": "F",
                "timestamp": "2026-06-01T10:00:00Z",
            }
        }

        with patch("packages.poller.smartthings.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = SmartThingsClient("test-pat")
            reading = await client.get_temperature("device-1")

        assert reading is not None
        assert abs(reading["value"] - 22.222) < 0.01

    @pytest.mark.asyncio
    async def test_get_temperature_out_of_range_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "temperature": {"value": 999.0, "unit": "C"}
        }

        with patch("packages.poller.smartthings.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = SmartThingsClient("test-pat")
            reading = await client.get_temperature("device-1")

        assert reading is None

    @pytest.mark.asyncio
    async def test_get_temperature_404_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("packages.poller.smartthings.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = SmartThingsClient("test-pat")
            reading = await client.get_temperature("nonexistent-device")

        assert reading is None

    @pytest.mark.asyncio
    async def test_batch_stops_on_rate_limit(self):
        call_count = 0

        async def mock_get_temp(device_id):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise SmartThingsRateLimited("rate limited")
            return {"value": 21.0, "timestamp": "2026-06-01T10:00:00Z"}

        client = SmartThingsClient("test-pat")
        client.get_temperature = mock_get_temp

        results = await client.get_temperatures_batch(["d1", "d2", "d3"])

        # Should get 1 result (first succeeded, second rate limited, third skipped)
        assert len(results) == 1
        assert call_count == 2


# ------------------------------------------------------------------
# Network retry
# ------------------------------------------------------------------


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        """Verify that transient network errors trigger retries."""
        import httpx as _httpx
        from packages.poller.smartthings import _request_with_retry

        call_count = 0
        good_response = MagicMock()
        good_response.status_code = 200
        good_response.json.return_value = {"temperature": {"value": 21.0, "unit": "C"}}

        async def flaky_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _httpx.ConnectError("connection refused")
            return good_response

        mock_client = AsyncMock()
        mock_client.get = flaky_get

        with patch("packages.poller.smartthings.BASE_BACKOFF_SECONDS", 0.01):
            resp = await _request_with_retry(mock_client, "http://test")

        assert call_count == 3
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        """All retries exhausted → SmartThingsError."""
        import httpx as _httpx
        from packages.poller.smartthings import _request_with_retry

        async def always_fail(url, **kwargs):
            raise _httpx.ConnectError("down")

        mock_client = AsyncMock()
        mock_client.get = always_fail

        with patch("packages.poller.smartthings.BASE_BACKOFF_SECONDS", 0.01):
            with pytest.raises(SmartThingsError, match="retries"):
                await _request_with_retry(mock_client, "http://test")

    @pytest.mark.asyncio
    async def test_429_respects_retry_after_header(self):
        """Verify Retry-After header is read on 429."""
        from packages.poller.smartthings import _request_with_retry

        call_count = 0
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "0.01"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {}

        async def rate_then_ok(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return rate_resp
            return ok_resp

        mock_client = AsyncMock()
        mock_client.get = rate_then_ok

        with patch("packages.poller.smartthings.BASE_BACKOFF_SECONDS", 0.01):
            resp = await _request_with_retry(mock_client, "http://test")

        assert call_count == 2
        assert resp.status_code == 200


# ------------------------------------------------------------------
# Stale reading exclusion
# ------------------------------------------------------------------


class TestStaleReadingExclusion:
    @pytest.mark.asyncio
    async def test_stale_reading_is_skipped(self):
        """Readings older than STALE_READING_THRESHOLD are not written."""
        from packages.poller.smartthings import poll_smartthings_temps, invalidate_device_cache

        invalidate_device_cache()

        stale_ts = (
            dt.datetime.now(dt.timezone.utc) - STALE_READING_THRESHOLD - dt.timedelta(minutes=5)
        ).isoformat()

        mock_session = MagicMock()
        mock_session.add = MagicMock()

        with patch(
            "packages.poller.smartthings.get_setting",
            new_callable=AsyncMock,
            side_effect=lambda k: {
                "smartthings_pat": "test-pat",
                "smartthings_device_ids": "d1",
            }.get(k),
        ):
            with patch.object(
                SmartThingsClient,
                "get_temperatures_batch",
                new_callable=AsyncMock,
                return_value=[
                    {"device_id": "d1", "value": 21.0, "timestamp": stale_ts},
                ],
            ):
                count = await poll_smartthings_temps(mock_session)

        assert count == 0
        mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_reading_is_written(self):
        """Recent readings are written normally."""
        from packages.poller.smartthings import poll_smartthings_temps, invalidate_device_cache

        invalidate_device_cache()

        fresh_ts = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
        ).isoformat()

        mock_session = MagicMock()
        mock_session.add = MagicMock()

        with patch(
            "packages.poller.smartthings.get_setting",
            new_callable=AsyncMock,
            side_effect=lambda k: {
                "smartthings_pat": "test-pat",
                "smartthings_device_ids": "d1",
            }.get(k),
        ):
            with patch.object(
                SmartThingsClient,
                "get_temperatures_batch",
                new_callable=AsyncMock,
                return_value=[
                    {"device_id": "d1", "value": 21.0, "timestamp": fresh_ts},
                ],
            ):
                count = await poll_smartthings_temps(mock_session)

        assert count == 1
        mock_session.add.assert_called_once()

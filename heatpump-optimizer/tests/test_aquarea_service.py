"""Panasonic wrapper request-budget regression tests."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services.aquarea import AquareaWrapper


def _wrapper() -> AquareaWrapper:
    wrapper = AquareaWrapper()
    wrapper._client = AsyncMock()
    wrapper._read_limiter = SimpleNamespace(acquire=AsyncMock())
    wrapper._write_limiter = SimpleNamespace(acquire=AsyncMock())
    return wrapper


@pytest.mark.asyncio
async def test_cached_device_does_not_consume_read_budget() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace()
    wrapper._device = device

    assert await wrapper.get_device() is device

    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_device_load_consumes_one_read_token() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace()
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    assert await wrapper.get_device() is device

    wrapper._read_limiter.acquire.assert_awaited_once()
    wrapper._client.get_devices.assert_awaited_once()
    wrapper._client.get_device.assert_awaited_once_with(
        device_info=device_info,
        consumption_refresh_interval=dt.timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_first_refresh_reuses_status_fetched_during_device_load() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(refresh_data=AsyncMock())
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    assert await wrapper.refresh_device() is device

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_refresh_consumes_one_read_token() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(refresh_data=AsyncMock())
    wrapper._device = device

    assert await wrapper.refresh_device() is device

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once()

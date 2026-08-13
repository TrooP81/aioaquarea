"""Panasonic wrapper request-budget regression tests."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioaquarea import ForceHeater, HolidayTimer, PowerfulTime
from aioaquarea.data import StatusDataMode

from packages.core.services.aquarea import AquareaWrapper, PanasonicCachedStatusError


def _wrapper() -> AquareaWrapper:
    wrapper = AquareaWrapper()
    wrapper._client = AsyncMock()
    wrapper._read_limiter = SimpleNamespace(acquire=AsyncMock())
    wrapper._write_limiter = SimpleNamespace(acquire=AsyncMock())
    return wrapper


@pytest.mark.asyncio
async def test_cached_device_does_not_consume_read_budget() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device

    assert await wrapper.get_device() is device

    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_device_load_consumes_one_read_token() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(status_data_mode=StatusDataMode.LIVE)
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
async def test_concurrent_device_loads_share_one_panasonic_request() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(status_data_mode=StatusDataMode.LIVE)
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def slow_get_devices():
        request_started.set()
        await release_request.wait()
        return [device_info]

    wrapper._client.get_devices.side_effect = slow_get_devices
    wrapper._client.get_device.return_value = device

    first = asyncio.create_task(wrapper.get_device())
    await request_started.wait()
    second = asyncio.create_task(wrapper.get_device())
    release_request.set()

    first_device, second_device = await asyncio.gather(first, second)

    assert first_device is second_device is device
    wrapper._read_limiter.acquire.assert_awaited_once()
    wrapper._client.get_devices.assert_awaited_once()
    wrapper._client.get_device.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_refresh_reuses_status_fetched_during_device_load() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(refresh_data=AsyncMock(), status_data_mode=StatusDataMode.LIVE)
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    assert await wrapper.refresh_device() is device

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_refresh_consumes_one_read_token() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(refresh_data=AsyncMock(), status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device

    assert await wrapper.refresh_device() is device

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)


@pytest.mark.asyncio
async def test_cached_initial_status_is_not_returned_as_fresh() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(status_data_mode=StatusDataMode.CACHED)
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached"):
        await wrapper.refresh_device()


@pytest.mark.asyncio
async def test_cached_refresh_is_not_returned_as_fresh() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(refresh_data=AsyncMock(), status_data_mode=StatusDataMode.CACHED)
    wrapper._device = device

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached"):
        await wrapper.refresh_device()

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)


@pytest.mark.parametrize(
    ("method_name", "device_method", "value"),
    [
        ("set_powerful_time", "set_powerful_time", PowerfulTime.ON_60MIN),
        ("set_force_heater", "set_force_heater", ForceHeater.ON),
        ("set_holiday_timer", "set_holiday_timer", HolidayTimer.ON),
    ],
)
@pytest.mark.asyncio
async def test_extended_panasonic_commands_share_write_budget(
    method_name: str,
    device_method: str,
    value: PowerfulTime | ForceHeater | HolidayTimer,
) -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(**{device_method: AsyncMock()})
    wrapper._device = device

    await getattr(wrapper, method_name)(value)

    wrapper._write_limiter.acquire.assert_awaited_once()
    getattr(device, device_method).assert_awaited_once_with(value)


@pytest.mark.asyncio
async def test_defrost_command_uses_write_budget_and_device_guardrail() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(request_defrost=AsyncMock())
    wrapper._device = device

    await wrapper.request_defrost()

    wrapper._write_limiter.acquire.assert_awaited_once()
    device.request_defrost.assert_awaited_once()

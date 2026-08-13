"""Panasonic wrapper request-budget regression tests."""

import asyncio
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioaquarea import ForceHeater, HolidayTimer, PowerfulTime
from aioaquarea.data import StatusDataMode

from packages.core.services.aquarea import (
    AquareaWrapper,
    PanasonicCachedStatusError,
    PanasonicCommandValidationError,
)


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
    device = SimpleNamespace(
        status_data_mode=StatusDataMode.LIVE,
        **{device_method: AsyncMock()},
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await getattr(wrapper, method_name)(value)

    wrapper._write_limiter.acquire.assert_awaited_once()
    getattr(device, device_method).assert_awaited_once_with(value)


@pytest.mark.asyncio
async def test_defrost_command_uses_write_budget_and_device_guardrail() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(request_defrost=AsyncMock(), status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await wrapper.request_defrost()

    wrapper._write_limiter.acquire.assert_awaited_once()
    device.request_defrost.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_command_preflight_refreshes_live_status() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.LIVE,
        set_force_heater=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = 0.0

    await wrapper.set_force_heater(ForceHeater.OFF)

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
    wrapper._write_limiter.acquire.assert_awaited_once()
    device.set_force_heater.assert_awaited_once_with(ForceHeater.OFF)


@pytest.mark.asyncio
async def test_cached_command_preflight_does_not_spend_write_budget() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.CACHED,
        set_force_heater=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached"):
        await wrapper.set_force_heater(ForceHeater.ON)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_force_heater.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_wait_rechecks_status_before_command() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.LIVE,
        set_force_heater=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    async def age_status_during_wait() -> None:
        wrapper._last_live_status_at = 0.0

    wrapper._write_limiter.acquire.side_effect = age_status_during_wait

    await wrapper.set_force_heater(ForceHeater.OFF)

    wrapper._write_limiter.acquire.assert_awaited_once()
    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
    device.set_force_heater.assert_awaited_once_with(ForceHeater.OFF)


@pytest.mark.asyncio
async def test_tank_temperature_uses_tank_entity_api() -> None:
    wrapper = _wrapper()
    tank = SimpleNamespace(
        heat_min=40,
        heat_max=65,
        target_temperature=50,
        set_target_temperature=AsyncMock(),
    )
    device = SimpleNamespace(tank=tank, status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await wrapper.set_tank_temperature(52)

    wrapper._write_limiter.acquire.assert_awaited_once()
    tank.set_target_temperature.assert_awaited_once_with(52)


@pytest.mark.asyncio
async def test_tank_temperature_skips_already_applied_target() -> None:
    wrapper = _wrapper()
    tank = SimpleNamespace(
        heat_min=40,
        heat_max=65,
        target_temperature=52,
        set_target_temperature=AsyncMock(),
    )
    wrapper._device = SimpleNamespace(tank=tank, status_data_mode=StatusDataMode.LIVE)
    wrapper._last_live_status_at = time.monotonic()

    await wrapper.set_tank_temperature(52)

    wrapper._write_limiter.acquire.assert_not_awaited()
    tank.set_target_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_tank_temperature_rejects_missing_tank_before_write_budget() -> None:
    wrapper = _wrapper()
    wrapper._device = SimpleNamespace(tank=None, status_data_mode=StatusDataMode.LIVE)
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="no writable"):
        await wrapper.set_tank_temperature(52)

    wrapper._write_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_tank_temperature_rejects_live_out_of_range_target() -> None:
    wrapper = _wrapper()
    tank = SimpleNamespace(
        heat_min=40,
        heat_max=60,
        target_temperature=50,
        set_target_temperature=AsyncMock(),
    )
    wrapper._device = SimpleNamespace(tank=tank, status_data_mode=StatusDataMode.LIVE)
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="outside.*40-60"):
        await wrapper.set_tank_temperature(65)

    wrapper._write_limiter.acquire.assert_not_awaited()
    tank.set_target_temperature.assert_not_awaited()

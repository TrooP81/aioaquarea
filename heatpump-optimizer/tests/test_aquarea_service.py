"""Panasonic wrapper request-budget regression tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioaquarea import (
    DataNotAvailableError,
    DeviceUnavailableError,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    PowerfulTime,
    QuietMode,
)
from aioaquarea.data import SpecialStatus, StatusDataMode

from packages.core.services.aquarea import (
    AquareaWrapper,
    PanasonicAdapterBackoffError,
    PanasonicAdapterUnavailableError,
    PanasonicCachedStatusError,
    PanasonicCommandValidationError,
    PanasonicCredentialsMissingError,
)
from packages.core.resilience import RedisCircuitBreaker
from packages.core.models import PlanActionRecord


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def ttl(self, key):
        return 900 if key in self.values else -2

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        return True

    async def set(self, key, value, ex):
        self.values[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


def _wrapper() -> AquareaWrapper:
    wrapper = AquareaWrapper()
    wrapper._client = AsyncMock()
    wrapper._authenticated = True
    wrapper._read_limiter = SimpleNamespace(acquire=AsyncMock())
    wrapper._write_limiter = SimpleNamespace(acquire=AsyncMock())
    return wrapper


async def _dispatch_restore(wrapper, *, zone_id=None, temperature=None):
    payload = {} if zone_id is None else {"zone_id": zone_id, "temperature": temperature}
    source_payload = (
        {} if zone_id is None else {"zone_id": zone_id, "baseline_temperature": temperature}
    )
    source = PlanActionRecord(
        id=1,
        device_id="device-a",
        action_type="force_dhw_on" if zone_id is None else "zone_temp_boost",
        payload_json=json.dumps(source_payload),
    )
    restore = PlanActionRecord(
        id=2,
        device_id="device-a",
        action_type="force_dhw_off" if zone_id is None else "zone_temp_restore",
        payload_json=json.dumps(payload),
        reverts_action_id=source.id,
        status="executing",
        safety_claimed_at=dt.datetime.now(dt.timezone.utc),
    )
    return await wrapper._dispatch_linked_restore(restore, source)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize(
    "method,args",
    [
        ("set_mode", (None,)),
        ("set_tank_temperature", (45,)),
        ("set_quiet_mode", (None,)),
        ("force_dhw", (True,)),
        ("set_powerful_time", (None,)),
        ("set_force_heater", (None,)),
        ("set_holiday_timer", (None,)),
        ("request_defrost", ()),
        ("set_zone_heat_temperature", (1, 30)),
        ("set_special_status", ("ECO",)),
        ("clear_special_status", ()),
    ],
)
async def test_read_only_wrapper_rejects_all_mutating_methods_without_io(
    method, args, read_only
) -> None:
    wrapper = AquareaWrapper(read_only=read_only)
    wrapper._client = AsyncMock()
    wrapper._read_limiter = SimpleNamespace(acquire=AsyncMock())
    wrapper._write_limiter = SimpleNamespace(acquire=AsyncMock())

    with wrapper.safety_write(), pytest.raises(PermissionError, match="read-only|observe-only"):
        await getattr(wrapper, method)(*args)

    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._write_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.parametrize("attempt", ["ordinary_command", "inherited_task"])
async def test_restore_scope_cannot_authorize_ordinary_commands_or_child_tasks(attempt):
    wrapper = _wrapper()
    wrapper.get_device = AsyncMock()

    async def dispatch(client, _payload):
        if attempt == "inherited_task":
            return await asyncio.create_task(client.force_dhw(ForceDHW.OFF))
        return await client.force_dhw(ForceDHW.ON)

    handler = SimpleNamespace(dispatch=dispatch)
    with patch("packages.optimizer.actions.get_action_handler", return_value=handler):
        with pytest.raises(PermissionError, match="observe-only build"):
            await _dispatch_restore(wrapper)
    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.force_dhw(ForceDHW.OFF)
    wrapper.get_device.assert_not_awaited()
    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._write_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_device_does_not_consume_read_budget() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device

    assert await wrapper.get_device() is device

    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_breaker_persists_across_wrapper_instances() -> None:
    redis = FakeRedis()
    failing_wrapper = _wrapper()
    failing_wrapper._client.login.side_effect = RuntimeError("invalid credentials")
    failing_wrapper._redis_circuit_breaker = RedisCircuitBreaker(redis)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="invalid credentials"):
            await failing_wrapper._authenticate()

    restarted_wrapper = _wrapper()
    restarted_wrapper._redis_circuit_breaker = RedisCircuitBreaker(redis)

    with pytest.raises(RuntimeError, match="900s remaining"):
        await restarted_wrapper._authenticate()

    restarted_wrapper._client.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_breaker_uses_memory_when_redis_errors() -> None:
    class FailingRedis:
        async def ttl(self, key):
            raise ConnectionError("redis unavailable")

        async def incr(self, key):
            raise ConnectionError("redis unavailable")

    wrapper = _wrapper()
    wrapper._client.login.side_effect = RuntimeError("invalid credentials")
    wrapper._redis_circuit_breaker = RedisCircuitBreaker(FailingRedis())

    for _ in range(3):
        with pytest.raises(RuntimeError, match="invalid credentials"):
            await wrapper._authenticate()

    with pytest.raises(RuntimeError, match="auth disabled temporarily"):
        await wrapper._authenticate()

    assert wrapper._client.login.await_count == 3


def _settings_lookup(values: dict[str, str]):
    async def lookup(key: str) -> str:
        return values.get(key, "")

    return lookup


class TestPanasonicCredentialsFromSettings:
    async def test_start_without_credentials_does_not_raise_or_login(self) -> None:
        wrapper = AquareaWrapper()
        with (
            patch("packages.core.settings_service.get_setting", _settings_lookup({})),
            patch("packages.core.settings_service.get_user_tz", AsyncMock(return_value="UTC")),
            patch("packages.core.services.aquarea.aiohttp.ClientSession", MagicMock()),
            patch("packages.core.services.aquarea.redis.from_url", MagicMock()),
            patch("packages.core.services.aquarea.Client") as client_cls,
        ):
            await wrapper.start()

        client_cls.assert_not_called()
        assert wrapper._authenticated is False

    async def test_start_survives_login_failure(self) -> None:
        wrapper = AquareaWrapper()
        client = AsyncMock()
        client.login.side_effect = RuntimeError("401")
        creds = {"aquarea_username": "user@example.com", "aquarea_password": "pw"}
        with (
            patch("packages.core.settings_service.get_setting", _settings_lookup(creds)),
            patch("packages.core.settings_service.get_user_tz", AsyncMock(return_value="UTC")),
            patch("packages.core.services.aquarea.aiohttp.ClientSession", MagicMock()),
            patch("packages.core.services.aquarea.redis.from_url", MagicMock()),
            patch("packages.core.services.aquarea.Client", return_value=client),
        ):
            await wrapper.start()

        client.login.assert_awaited_once()
        assert wrapper._authenticated is False

    async def test_ensure_authenticated_uses_settings_credentials(self) -> None:
        wrapper = AquareaWrapper()
        client = AsyncMock()
        creds = {"aquarea_username": " user@example.com ", "aquarea_password": "pw"}
        with (
            patch("packages.core.settings_service.get_setting", _settings_lookup(creds)),
            patch("packages.core.services.aquarea.Client", return_value=client) as client_cls,
        ):
            await wrapper._ensure_authenticated()

        assert client_cls.call_args.kwargs["username"] == "user@example.com"
        assert client_cls.call_args.kwargs["password"] == "pw"
        client.login.assert_awaited_once()
        assert wrapper._authenticated is True

    async def test_get_device_without_credentials_raises_clear_error(self) -> None:
        wrapper = AquareaWrapper()
        with patch("packages.core.settings_service.get_setting", _settings_lookup({})):
            with pytest.raises(PanasonicCredentialsMissingError, match="Settings tab"):
                await wrapper.get_device()


@pytest.mark.asyncio
async def test_weekly_timer_read_is_cached_and_evaluated_in_user_timezone() -> None:
    wrapper = _wrapper()
    slots = (SimpleNamespace(zone_id=1),)
    timer = SimpleNamespace(active_slots=MagicMock(return_value=slots))
    device = SimpleNamespace(
        status_data_mode=StatusDataMode.LIVE,
        get_weekly_timer=AsyncMock(return_value=timer),
    )
    wrapper._device = device
    instant = dt.datetime(2026, 8, 24, 20, tzinfo=dt.timezone.utc)

    assert await wrapper.get_active_weekly_timer_slots(instant) == slots
    assert await wrapper.get_active_weekly_timer_slots(instant) == slots

    device.get_weekly_timer.assert_awaited_once()
    wrapper._read_limiter.acquire.assert_awaited_once()
    timer.active_slots.assert_called_with(instant, wrapper._timezone)


async def test_failed_timer_read_is_unknown_and_cached_for_one_hour():
    wrapper = _wrapper()
    device = SimpleNamespace(
        status_data_mode=StatusDataMode.LIVE,
        get_weekly_timer=AsyncMock(side_effect=RuntimeError("offline timer")),
    )
    wrapper._device = device
    assert await wrapper.get_active_weekly_timer_slots() is None
    assert await wrapper.get_active_weekly_timer_slots() is None
    assert wrapper.cached_native_timer_provenance() == {"status": "unknown"}
    device.get_weekly_timer.assert_awaited_once()
    wrapper._read_limiter.acquire.assert_awaited_once()


async def test_known_disabled_timer_has_explicit_native_provenance():
    wrapper = _wrapper()
    wrapper._device = SimpleNamespace(
        status_data_mode=StatusDataMode.LIVE,
        get_weekly_timer=AsyncMock(
            return_value=SimpleNamespace(
                enabled=False,
                slots=(),
                active_slots=MagicMock(return_value=()),
            )
        ),
    )
    assert await wrapper.get_active_weekly_timer_slots() == ()
    provenance = wrapper.cached_native_timer_provenance()
    assert provenance["status"] == "known"
    assert provenance["enabled"] is False
    assert provenance["slots"] == []
    assert provenance["fetched_at"]


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
        timezone=wrapper._timezone,
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
async def test_refresh_null_device_fallback_charges_one_token_before_lock() -> None:
    wrapper = _wrapper()
    cached_device = SimpleNamespace(refresh_data=AsyncMock(), status_data_mode=StatusDataMode.LIVE)
    initialized_device = SimpleNamespace(status_data_mode=StatusDataMode.LIVE)
    wrapper._device = cached_device
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    wrapper._client.get_device.return_value = initialized_device

    async def acquire_then_clear_device():
        wrapper._device = None

    wrapper._read_limiter.acquire.side_effect = acquire_then_clear_device

    assert await wrapper.refresh_device() is initialized_device

    wrapper._read_limiter.acquire.assert_awaited_once()
    cached_device.refresh_data.assert_not_awaited()
    wrapper._client.get_devices.assert_awaited_once()
    wrapper._client.get_device.assert_awaited_once()


@pytest.mark.asyncio
async def test_cached_initial_status_is_not_returned_as_fresh() -> None:
    wrapper = _wrapper()
    device_info = SimpleNamespace(device_id="device-1")
    device = SimpleNamespace(status_data_mode=StatusDataMode.CACHED)
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached") as exc:
        await wrapper.refresh_device()

    assert exc.value.consecutive_failures == 1
    assert exc.value.retry_after_seconds == 300


@pytest.mark.asyncio
async def test_cached_refresh_is_not_returned_as_fresh() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(refresh_data=AsyncMock(), status_data_mode=StatusDataMode.CACHED)
    wrapper._device = device

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached"):
        await wrapper.refresh_device()

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)


@pytest.mark.asyncio
async def test_adapter_outage_defers_next_live_refresh_without_spending_read_budget() -> None:
    wrapper = _wrapper()
    failure = DeviceUnavailableError("device-1", "adaptor offline")
    device = SimpleNamespace(
        long_id="device-1",
        refresh_data=AsyncMock(side_effect=failure),
        status_data_mode=StatusDataMode.LIVE,
    )
    wrapper._device = device

    with patch("packages.core.services.aquarea.time.monotonic", return_value=100.0):
        with pytest.raises(PanasonicAdapterUnavailableError) as unavailable:
            await wrapper.refresh_device()
        with pytest.raises(PanasonicAdapterBackoffError) as deferred:
            await wrapper.refresh_device()

    assert unavailable.value.device_id == "device-1"
    assert unavailable.value.consecutive_failures == 1
    assert unavailable.value.retry_after_seconds == 300
    assert deferred.value.consecutive_failures == 1
    assert deferred.value.retry_after_seconds == 300
    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)


@pytest.mark.asyncio
async def test_repeated_adapter_outages_back_off_exponentially() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        long_id="device-1",
        refresh_data=AsyncMock(
            side_effect=[
                DeviceUnavailableError("device-1", "offline"),
                DeviceUnavailableError("device-1", "offline"),
            ]
        ),
        status_data_mode=StatusDataMode.LIVE,
    )
    wrapper._device = device

    with patch("packages.core.services.aquarea.time.monotonic") as monotonic:
        monotonic.return_value = 100.0
        with pytest.raises(PanasonicAdapterUnavailableError) as first:
            await wrapper.refresh_device()
        monotonic.return_value = 401.0
        with pytest.raises(PanasonicAdapterUnavailableError) as second:
            await wrapper.refresh_device()

    assert first.value.retry_after_seconds == 300
    assert second.value.consecutive_failures == 2
    assert second.value.retry_after_seconds == 600
    assert wrapper._read_limiter.acquire.await_count == 2


def test_adapter_backoff_is_capped_at_thirty_minutes() -> None:
    wrapper = _wrapper()

    with patch("packages.core.services.aquarea.time.monotonic", return_value=100.0):
        delays = [
            wrapper._register_adapter_failure(device_id="device-1", reason="offline")[1]
            for _ in range(5)
        ]

    assert delays == [300, 600, 1200, 1800, 1800]


@pytest.mark.asyncio
async def test_successful_live_refresh_resets_adapter_backoff() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        long_id="device-1",
        refresh_data=AsyncMock(
            side_effect=[DeviceUnavailableError("device-1", "offline"), None, None]
        ),
        status_data_mode=StatusDataMode.LIVE,
    )
    wrapper._device = device

    with patch("packages.core.services.aquarea.time.monotonic") as monotonic:
        monotonic.return_value = 100.0
        with pytest.raises(PanasonicAdapterUnavailableError):
            await wrapper.refresh_device()
        monotonic.return_value = 401.0
        assert await wrapper.refresh_device() is device
        assert await wrapper.refresh_device() is device

    assert wrapper._adapter_failure_count == 0
    assert wrapper._adapter_retry_at is None
    assert wrapper._read_limiter.acquire.await_count == 3


@pytest.mark.parametrize(
    ("method_name", "device_method", "value"),
    [
        ("set_powerful_time", "set_powerful_time", PowerfulTime.ON_60MIN),
        ("set_force_heater", "set_force_heater", ForceHeater.ON),
        ("set_holiday_timer", "set_holiday_timer", HolidayTimer.ON),
    ],
)
@pytest.mark.asyncio
async def test_extended_panasonic_commands_are_refused_without_write_budget(
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

    with pytest.raises(PermissionError, match="observe-only build"):
        await getattr(wrapper, method_name)(value)

    wrapper._write_limiter.acquire.assert_not_awaited()
    wrapper._read_limiter.acquire.assert_not_awaited()
    getattr(device, device_method).assert_not_awaited()


@pytest.mark.asyncio
async def test_defrost_command_is_refused_before_budget_or_device_access() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(request_defrost=AsyncMock(), status_data_mode=StatusDataMode.LIVE)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.request_defrost()

    wrapper._write_limiter.acquire.assert_not_awaited()
    wrapper._read_limiter.acquire.assert_not_awaited()
    device.request_defrost.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_command_preflight_refreshes_live_status() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.LIVE,
        force_dhw=ForceDHW.ON,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic() - 61.0

    await _dispatch_restore(wrapper)

    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
    wrapper._write_limiter.acquire.assert_awaited_once()
    device.set_force_dhw.assert_awaited_once_with(ForceDHW.OFF)


@pytest.mark.asyncio
async def test_cached_command_preflight_does_not_spend_write_budget() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.CACHED,
        force_dhw=ForceDHW.ON,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCachedStatusError, match="cloud-cached"):
        await _dispatch_restore(wrapper)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_force_dhw.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_wait_rechecks_status_before_command() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        refresh_data=AsyncMock(),
        status_data_mode=StatusDataMode.LIVE,
        force_dhw=ForceDHW.ON,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    async def age_status_during_wait() -> None:
        wrapper._last_live_status_at = time.monotonic() - 61.0

    wrapper._write_limiter.acquire.side_effect = age_status_during_wait

    await _dispatch_restore(wrapper)

    wrapper._write_limiter.acquire.assert_awaited_once()
    wrapper._read_limiter.acquire.assert_awaited_once()
    device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
    device.set_force_dhw.assert_awaited_once_with(ForceDHW.OFF)


@pytest.mark.asyncio
async def test_force_dhw_skips_already_applied_state() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        force_dhw=ForceDHW.OFF,
        status_data_mode=StatusDataMode.LIVE,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    result = await _dispatch_restore(wrapper)

    assert result == {"skip": True, "reason": "force_dhw_already_off"}
    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_force_dhw.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_dhw_writes_changed_state_once() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        force_dhw=ForceDHW.ON,
        status_data_mode=StatusDataMode.LIVE,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    result = await _dispatch_restore(wrapper)

    assert result["force_dhw"] == "OFF"
    wrapper._write_limiter.acquire.assert_awaited_once()
    device.set_force_dhw.assert_awaited_once_with(ForceDHW.OFF)


@pytest.mark.asyncio
async def test_force_dhw_rechecks_state_after_write_wait() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        force_dhw=ForceDHW.ON,
        status_data_mode=StatusDataMode.LIVE,
        set_force_dhw=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    async def apply_state_during_wait() -> None:
        device.force_dhw = ForceDHW.OFF

    wrapper._write_limiter.acquire.side_effect = apply_state_during_wait

    result = await _dispatch_restore(wrapper)

    assert result == {"skip": True, "reason": "force_dhw_already_off"}
    wrapper._write_limiter.acquire.assert_awaited_once()
    device.set_force_dhw.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_mode_is_refused_even_at_already_applied_level() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        quiet_mode=QuietMode.LEVEL2,
        status_data_mode=StatusDataMode.LIVE,
        set_quiet_mode=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_quiet_mode(QuietMode.LEVEL2)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_quiet_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_mode_is_refused_at_changed_level() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        quiet_mode=QuietMode.LEVEL1,
        status_data_mode=StatusDataMode.LIVE,
        set_quiet_mode=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_quiet_mode(QuietMode.LEVEL2)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_quiet_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_mode_never_enters_write_wait() -> None:
    wrapper = _wrapper()
    device = SimpleNamespace(
        quiet_mode=QuietMode.LEVEL1,
        status_data_mode=StatusDataMode.LIVE,
        set_quiet_mode=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    async def apply_level_during_wait() -> None:
        device.quiet_mode = QuietMode.LEVEL2

    wrapper._write_limiter.acquire.side_effect = apply_level_during_wait

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_quiet_mode(QuietMode.LEVEL2)

    wrapper._write_limiter.acquire.assert_not_awaited()
    assert device.quiet_mode == QuietMode.LEVEL1
    device.set_quiet_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_tank_temperature_never_reaches_tank_entity_api() -> None:
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

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_tank_temperature(52)

    wrapper._write_limiter.acquire.assert_not_awaited()
    tank.set_target_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_tank_temperature_is_refused_even_at_already_applied_target() -> None:
    wrapper = _wrapper()
    tank = SimpleNamespace(
        heat_min=40,
        heat_max=65,
        target_temperature=52,
        set_target_temperature=AsyncMock(),
    )
    wrapper._device = SimpleNamespace(tank=tank, status_data_mode=StatusDataMode.LIVE)
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_tank_temperature(52)

    wrapper._write_limiter.acquire.assert_not_awaited()
    tank.set_target_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_tank_temperature_rejects_missing_tank_before_write_budget() -> None:
    wrapper = _wrapper()
    wrapper._device = SimpleNamespace(tank=None, status_data_mode=StatusDataMode.LIVE)
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="no writable"):
        wrapper._validate_tank_temperature(wrapper._device, 52)
    with pytest.raises(PermissionError, match="observe-only build"):
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
        wrapper._validate_tank_temperature(wrapper._device, 65)
    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_tank_temperature(65)

    wrapper._write_limiter.acquire.assert_not_awaited()
    tank.set_target_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_zone_temperature_skips_already_applied_target() -> None:
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=37, heat_min=20, heat_max=65)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await _dispatch_restore(wrapper, zone_id=1, temperature=37)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_zone_temperature_writes_changed_target_once() -> None:
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=37, heat_min=20, heat_max=65)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await _dispatch_restore(wrapper, zone_id=1, temperature=35)

    wrapper._write_limiter.acquire.assert_awaited_once()
    device.set_temperature.assert_awaited_once_with(35, zone_id=1)


@pytest.mark.asyncio
async def test_zone_temperature_normalizes_zero_based_optimizer_zone_to_panasonic_zone_one() -> (
    None
):
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=37, heat_min=20, heat_max=65)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await _dispatch_restore(wrapper, zone_id=0, temperature=35)

    device.set_temperature.assert_awaited_once_with(35, zone_id=1)


@pytest.mark.asyncio
async def test_zone_temperature_accepts_live_curve_shift_range() -> None:
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=-3, heat_min=-5, heat_max=5)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    await _dispatch_restore(wrapper, zone_id=1, temperature=-5)

    device.set_temperature.assert_awaited_once_with(-5, zone_id=1)


@pytest.mark.asyncio
async def test_zone_temperature_rejects_target_outside_live_range() -> None:
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=-5, heat_min=-5, heat_max=5)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="outside.*-5-5"):
        wrapper._validate_zone_heat_temperature(device, 1, 6)
    result = await _dispatch_restore(wrapper, zone_id=1, temperature=6)
    assert result["skip"] is True
    assert result["reason"] == "zone_restore_outside_live_range"

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_zone_temperature_rejects_missing_live_range() -> None:
    wrapper = _wrapper()
    zone = SimpleNamespace(heat_target_temperature=-5, heat_min=None, heat_max=None)
    device = SimpleNamespace(
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_temperature=AsyncMock(),
    )
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="did not report"):
        wrapper._validate_zone_heat_temperature(device, 1, -3)
    result = await _dispatch_restore(wrapper, zone_id=1, temperature=-3)
    assert result["skip"] is True
    assert result["reason"] == "zone_restore_outside_live_range"

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_temperature.assert_not_awaited()


def _special_status_device(status=None, *, supports: bool = True, eco_heat=-2):
    zone = SimpleNamespace(
        supports_special_status=True,
        heat_target_temperature=-5,
        cool_target_temperature=None,
        temperature_modifiers={
            SpecialStatus.ECO: SimpleNamespace(heat=eco_heat, cool=None),
            SpecialStatus.COMFORT: SimpleNamespace(heat=2, cool=None),
        },
    )
    return SimpleNamespace(
        support_special_status=supports,
        special_status=status,
        zones={1: zone},
        status_data_mode=StatusDataMode.LIVE,
        set_special_status=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_special_status_is_refused_even_at_already_applied_mode() -> None:
    wrapper = _wrapper()
    device = _special_status_device(SpecialStatus.ECO)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_special_status("ECO")

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_special_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_special_status_is_refused_at_valid_changed_mode() -> None:
    wrapper = _wrapper()
    device = _special_status_device(None)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.set_special_status("COMFORT")

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_special_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_special_status_rejects_unsafe_or_unknown_mode() -> None:
    wrapper = _wrapper()
    device = _special_status_device(None, eco_heat=2)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PanasonicCommandValidationError, match="safe ECO/COMFORT"):
        wrapper._validate_special_status_support(device)
    for mode in ("ECO", "BOOST"):
        with pytest.raises(PermissionError, match="observe-only build"):
            await wrapper.set_special_status(mode)

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_special_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_special_status_is_refused_even_when_already_normal() -> None:
    wrapper = _wrapper()
    device = _special_status_device(None)
    wrapper._device = device
    wrapper._last_live_status_at = time.monotonic()

    with pytest.raises(PermissionError, match="observe-only build"):
        await wrapper.clear_special_status()

    wrapper._write_limiter.acquire.assert_not_awaited()
    device.set_special_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_manual_refresh_keeps_status_when_consumption_is_unavailable(
    monkeypatch,
) -> None:
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    device = SimpleNamespace(long_id="device-1")
    monkeypatch.setattr(wrapper, "_distributed_quota_enabled", lambda: True)
    wrapper._refresh_device_unreserved = AsyncMock(return_value=device)
    wrapper._fetch_consumption_unreserved = AsyncMock(
        side_effect=DataNotAvailableError("Panasonic consumption data is unavailable")
    )

    refreshed, snapshot = await wrapper.refresh_status_and_consumption(
        dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    )

    assert refreshed is device
    assert snapshot is None
    wrapper._read_quota.reserve.assert_awaited_once()

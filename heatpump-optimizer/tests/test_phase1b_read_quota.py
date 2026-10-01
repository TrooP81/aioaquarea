from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import unicodedata
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioaquarea import DataNotAvailableError
from aioaquarea.data import StatusDataMode
from fastapi import HTTPException

from packages.api import main as api_main
from packages.api.routers import polling as polling_router
from packages.api.routers.panasonic import panasonic_read_quota
from packages.api.routers.polling import _poll_now_with_wrapper, get_polling_wrapper, poll_now
from packages.core.config import Settings, settings
from packages.core.resilience import (
    DistributedReadQuotaExhausted,
    READ_QUOTA_MANUAL_REQUIRED,
    ReadQuotaCategory,
    ReadQuotaReservation,
    ReadQuotaStatus,
    RateLimiter,
)
from packages.core.services.aquarea import (
    AquareaWrapper,
    ConsumptionSnapshot,
    EXECUTOR_VERIFICATION_QUOTA_STARVED_AFTER_SECONDS,
    PanasonicQuotaAccountMismatchError,
    ReadQuotaContext,
)


def _wrapper() -> AquareaWrapper:
    wrapper = AquareaWrapper()
    wrapper._client = AsyncMock()
    wrapper._authenticated = True
    wrapper._read_limiter = SimpleNamespace(acquire=AsyncMock())
    wrapper._write_limiter = SimpleNamespace(acquire=AsyncMock())
    return wrapper


def _live_device(device_id: str = "device-1") -> SimpleNamespace:
    return SimpleNamespace(
        long_id=device_id,
        status_data_mode=StatusDataMode.LIVE,
        refresh_data=AsyncMock(),
    )


def _quota_status(remaining: float = 0, retry_after_seconds: int = 1) -> ReadQuotaStatus:
    return ReadQuotaStatus(
        enabled=True,
        reliable=True,
        remaining=remaining,
        capacity=30,
        manual_required=READ_QUOTA_MANUAL_REQUIRED,
        retry_after_seconds=retry_after_seconds,
        counters={category: 0 for category in ReadQuotaCategory},
        observed_at=dt.datetime.now(dt.timezone.utc),
    )


class _SessionContext:
    def __init__(self):
        self.added = []
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    def add(self, value):
        self.added.append(value)

    async def execute(self, value):
        self.executed.append(value)


class _PollDevice:
    def __init__(
        self, *, consumption_error: Exception | None = None, action_error: Exception | None = None
    ):
        self.long_id = "device-1"
        self.temperature_outdoor = 4.0
        self.tank_temp = 48.0
        self.refresh_data = AsyncMock()
        self._consumption_error = consumption_error
        self._action_error = action_error
        self._consumption_index = 0

    @property
    def current_action(self):
        if self._action_error is not None:
            raise self._action_error
        return SimpleNamespace(name="HEAT")

    async def get_and_refresh_consumption(self, *_args):
        if self._consumption_error is not None:
            raise self._consumption_error
        values = (1.0, 2.0, 3.0)
        value = values[self._consumption_index]
        self._consumption_index += 1
        return value


def test_quota_flag_defaults_off_and_is_not_runtime_editable() -> None:
    assert Settings(_env_file=None).panasonic_distributed_read_quota_enabled is False

    from packages.core.settings_service import SETTINGS_SCHEMA

    assert "panasonic_distributed_read_quota_enabled" not in SETTINGS_SCHEMA


def test_account_key_uses_nfkc_trim_and_casefold() -> None:
    username = "  USER@EXAMPLE.COM  "
    equivalent = "\uff35ser@example.com"
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()

    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    assert AquareaWrapper._account_key_for_username(username) == expected
    assert AquareaWrapper._account_key_for_username(equivalent) == expected
    assert len(expected) == 64


@pytest.mark.asyncio
async def test_quota_response_contains_no_account_or_device_identity() -> None:
    wrapper = _wrapper()
    wrapper.get_rate_limit_status = AsyncMock(return_value=_quota_status(24))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(aquarea_wrapper=wrapper)))

    response = await panasonic_read_quota(request)
    rendered = repr(response)

    assert response["remaining"] == 24
    assert "account" not in rendered.lower()
    assert "device" not in rendered.lower()
    assert "USER@EXAMPLE.COM" not in rendered


@pytest.mark.asyncio
async def test_enabled_poll_now_dependency_requires_lifespan_wrapper_without_io(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(Exception) as raised:
        await get_polling_wrapper(request)

    assert raised.value.status_code == 503
    assert raised.value.headers["Retry-After"] == "30"
    assert raised.value.detail["code"] == "panasonic_read_quota_unavailable"


@pytest.mark.asyncio
async def test_enabled_poll_now_delegates_to_lifespan_wrapper_once(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = SimpleNamespace()
    delegated = AsyncMock(return_value={"status": "ok", "results": {}})
    monkeypatch.setattr(polling_router, "_poll_now_with_wrapper", delegated)

    response = await poll_now(wrapper)

    assert response == {"status": "ok", "results": {}}
    delegated.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_manual_quota_rejection_returns_integer_retry_after_before_feed_io(
    monkeypatch,
) -> None:
    exhausted = DistributedReadQuotaExhausted(_quota_status(7, retry_after_seconds=120))
    wrapper = SimpleNamespace(refresh_status_and_consumption=AsyncMock(side_effect=exhausted))
    price_feed = AsyncMock(side_effect=AssertionError("price feed must not run"))
    weather_feed = AsyncMock(side_effect=AssertionError("weather feed must not run"))
    monkeypatch.setattr("packages.poller.feeds.fetch_price_feed", price_feed)
    monkeypatch.setattr("packages.poller.feeds.fetch_weather", weather_feed)

    with pytest.raises(HTTPException) as raised:
        await poll_now(wrapper)

    assert raised.value.status_code == 429
    assert raised.value.headers["Retry-After"] == "120"
    assert 1 <= int(raised.value.headers["Retry-After"]) <= 3600
    assert raised.value.detail["code"] == "panasonic_read_quota_exhausted"
    price_feed.assert_not_awaited()
    weather_feed.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_quota_redis_failure_returns_503_before_feed_io(monkeypatch) -> None:
    wrapper = SimpleNamespace(
        refresh_status_and_consumption=AsyncMock(side_effect=ConnectionError("redis unavailable"))
    )
    price_feed = AsyncMock(side_effect=AssertionError("price feed must not run"))
    weather_feed = AsyncMock(side_effect=AssertionError("weather feed must not run"))
    monkeypatch.setattr("packages.poller.feeds.fetch_price_feed", price_feed)
    monkeypatch.setattr("packages.poller.feeds.fetch_weather", weather_feed)

    with pytest.raises(HTTPException) as raised:
        await poll_now(wrapper)

    assert raised.value.status_code == 503
    assert raised.value.headers["Retry-After"] == "30"
    assert raised.value.detail["code"] == "panasonic_read_quota_unavailable"
    price_feed.assert_not_awaited()
    weather_feed.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_lifespan_start_failure_leaves_wrapper_unset_and_stops_partial_resource(
    monkeypatch,
):
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)

    class FailingWrapper:
        instances = []

        def __init__(self, *, read_only):
            self.read_only = read_only
            self.stop = AsyncMock()
            self.instances.append(self)

        async def start(self):
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(api_main, "AquareaWrapper", FailingWrapper)
    application = SimpleNamespace(state=SimpleNamespace())

    async with api_main.lifespan(application):
        assert not hasattr(application.state, "aquarea_wrapper")

    assert FailingWrapper.instances[0].read_only is True
    FailingWrapper.instances[0].stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_cold_get_device_reserves_one_status_token_before_io(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    device_info = SimpleNamespace(device_id="device-1")
    device = _live_device()
    wrapper._client.get_devices.return_value = [device_info]
    wrapper._client.get_device.return_value = device

    result = await wrapper.get_device()

    assert result is device
    wrapper._read_quota.reserve.assert_awaited_once_with("account-key", 1, ReadQuotaCategory.STATUS)
    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_cached_device_and_identity_are_free(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._device = _live_device()
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())

    assert await wrapper.get_device() is wrapper._device
    assert await wrapper.get_selected_device_id() == "device-1"

    wrapper._read_quota.reserve.assert_not_awaited()
    wrapper._read_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_refresh_uses_one_reservation_and_never_double_charges(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    device = _live_device()
    wrapper._client.get_device.return_value = device

    assert await wrapper.refresh_device() is device

    wrapper._read_quota.reserve.assert_awaited_once_with("account-key", 1, ReadQuotaCategory.STATUS)
    wrapper._read_limiter.acquire.assert_not_awaited()
    device.refresh_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_losing_cold_racer_retains_token_and_performs_no_panasonic_io(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    first_device = _live_device()

    async def first_get_devices():
        first_started.set()
        await release_first.wait()
        return [SimpleNamespace(device_id="device-1")]

    wrapper._client.get_devices.side_effect = first_get_devices
    wrapper._client.get_device.return_value = first_device
    first = asyncio.create_task(wrapper.get_device())
    await first_started.wait()
    second = asyncio.create_task(wrapper.get_device())
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first, second)

    assert wrapper._read_quota.reserve.await_count == 2
    assert wrapper._client.get_devices.await_count == 1
    assert wrapper._client.get_device.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_before_reservation_consumes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await wrapper.get_device()

    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_reservation_retains_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._client.get_devices.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await wrapper.get_device()

    wrapper._read_quota.reserve.assert_awaited_once()
    wrapper._client.get_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_failure_uses_one_local_background_acquire(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=ConnectionError()))
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    wrapper._client.get_device.return_value = _live_device()

    await wrapper.get_device()

    wrapper._read_limiter.acquire.assert_awaited_once()
    assert wrapper.cached_rate_limit_reliability() is False
    wrapper._client.get_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_mismatch_fails_closed_before_discovery(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = None
    wrapper._resolve_account_key = AsyncMock(return_value="reserved-account")
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())

    async def authenticate_as_other_account():
        wrapper._account_key = "different-account"

    wrapper._ensure_authenticated = authenticate_as_other_account

    with pytest.raises(PanasonicQuotaAccountMismatchError):
        await wrapper.get_device()

    wrapper._read_quota.reserve.assert_awaited_once()
    wrapper._client.get_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumption_snapshot_selects_exact_day_and_preserves_missing_components(
    monkeypatch,
):
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._device = _live_device()
    target = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
    wrapper._client.get_device_consumption.return_value = [
        SimpleNamespace(
            data_time="20260930", heat_consumption=9.0, cool_consumption=9.0, tank_consumption=9.0
        ),
        SimpleNamespace(
            data_time="20261001", heat_consumption=1.5, cool_consumption=None, tank_consumption=0.5
        ),
    ]

    snapshot = await wrapper.refresh_consumption(target)

    assert isinstance(snapshot, ConsumptionSnapshot)
    assert snapshot.date == target.date()
    assert (snapshot.heat_kwh, snapshot.cool_kwh, snapshot.tank_kwh) == (1.5, None, 0.5)
    assert snapshot.total_kwh == 2.0
    wrapper._read_quota.reserve.assert_awaited_once_with(
        "account-key", 1, ReadQuotaCategory.CONSUMPTION
    )
    assert wrapper._client.get_device_consumption.await_count == 1


@pytest.mark.asyncio
async def test_consumption_snapshot_rejects_malformed_or_missing_exact_day(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._device = _live_device()
    wrapper._client.get_device_consumption.return_value = [
        SimpleNamespace(
            data_time="not-a-date", heat_consumption=1.0, cool_consumption=2.0, tank_consumption=3.0
        ),
        SimpleNamespace(
            data_time="20260930", heat_consumption=4.0, cool_consumption=5.0, tank_consumption=6.0
        ),
    ]

    with pytest.raises(DataNotAvailableError):
        await wrapper.refresh_consumption(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))


@pytest.mark.asyncio
async def test_manual_refresh_reserves_two_tokens_and_does_not_nest_reads(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._device = _live_device()
    wrapper._client.get_device_consumption.return_value = [
        SimpleNamespace(
            data_time="20261001", heat_consumption=1.0, cool_consumption=2.0, tank_consumption=3.0
        )
    ]

    device, snapshot = await wrapper.refresh_status_and_consumption(
        dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    )

    assert device is wrapper._device
    assert snapshot.total_kwh == 6.0
    wrapper._read_quota.reserve.assert_awaited_once_with(
        "account-key",
        2,
        ReadQuotaCategory.MANUAL,
        minimum_remaining=READ_QUOTA_MANUAL_REQUIRED - 2,
    )
    wrapper._read_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_refresh_keeps_device_success_when_consumption_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._device = _live_device()
    wrapper._client.get_device_consumption.return_value = []

    device, snapshot = await wrapper.refresh_status_and_consumption(
        dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    )

    assert device is wrapper._device
    assert snapshot is None
    wrapper._read_quota.reserve.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_poll_now_returns_stable_success_envelope_and_fetches_feeds_after_admission(
    monkeypatch,
):
    wrapper = _wrapper()
    device = SimpleNamespace(
        long_id="device-1",
        current_action=SimpleNamespace(name="HEAT"),
        temperature_outdoor=4.0,
        tank_temp=48.0,
    )
    snapshot = ConsumptionSnapshot(
        date=dt.date(2026, 10, 1),
        heat_kwh=1.0,
        cool_kwh=2.0,
        tank_kwh=3.0,
        fetched_at=dt.datetime.now(dt.timezone.utc),
    )
    wrapper.refresh_status_and_consumption = AsyncMock(return_value=(device, snapshot))
    session_contexts = [_SessionContext(), _SessionContext()]
    monkeypatch.setattr(polling_router, "get_session", lambda: session_contexts.pop(0))
    monkeypatch.setattr(
        polling_router,
        "build_device_status_record",
        lambda _device: SimpleNamespace(
            device_id="device-1",
            outdoor_temp=None,
            heat_pump_outdoor_temp=None,
            outdoor_temp_source=None,
            tank_temp=48.0,
        ),
    )
    monkeypatch.setattr(
        polling_router,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )
    monkeypatch.setattr(polling_router, "ingest_device_status", AsyncMock())

    async def populate_feeds(results):
        results["prices"] = {"success": True, "message": "prices"}
        results["weather"] = {"success": True, "message": "weather"}

    feeds = AsyncMock(side_effect=populate_feeds)
    monkeypatch.setattr(polling_router, "_poll_prices_and_weather_legacy", feeds)

    response = await _poll_now_with_wrapper(wrapper)

    assert response["status"] == "ok"
    assert set(response["results"]) == {"device", "prices", "weather"}
    assert response["results"]["device"]["success"] is True
    wrapper.refresh_status_and_consumption.assert_awaited_once()
    feeds.assert_awaited_once()


async def _run_poll_now_parity_branch(monkeypatch, *, enabled: bool, scenario: str):
    device = _PollDevice(
        consumption_error=(
            DataNotAvailableError("Panasonic consumption data is unavailable")
            if scenario == "consumption_unavailable"
            else None
        ),
        action_error=(
            RuntimeError("Panasonic device failure")
            if scenario == "partial_panasonic_failure"
            else None
        ),
    )
    snapshot = (
        None
        if scenario == "consumption_unavailable"
        else ConsumptionSnapshot(
            date=dt.date(2026, 10, 1),
            heat_kwh=1.0,
            cool_kwh=2.0,
            tank_kwh=3.0,
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    feed = SimpleNamespace(
        prices=[(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), 0.1)],
        currency="EUR",
        source="test",
    )
    weather = [{"ts": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), "temperature": 4.0}]

    with monkeypatch.context() as patches:
        patches.setattr(settings, "panasonic_distributed_read_quota_enabled", enabled)
        patches.setattr(polling_router, "get_session", lambda: _SessionContext())
        patches.setattr(
            polling_router,
            "build_device_status_record",
            lambda _device: SimpleNamespace(
                device_id="device-1",
                outdoor_temp=None,
                heat_pump_outdoor_temp=None,
                outdoor_temp_source=None,
                tank_temp=48.0,
            ),
        )
        patches.setattr(
            polling_router,
            "resolve_outdoor_temperature",
            AsyncMock(
                return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
            ),
        )
        patches.setattr(polling_router, "ingest_device_status", AsyncMock())
        patches.setattr("packages.poller.feeds.fetch_price_feed", AsyncMock(return_value=feed))
        patches.setattr("packages.poller.feeds.fetch_weather", AsyncMock(return_value=weather))
        patches.setattr(polling_router, "get_price_area", AsyncMock(return_value="SE1"))

        if enabled:
            wrapper = _wrapper()
            wrapper.refresh_status_and_consumption = AsyncMock(return_value=(device, snapshot))
            return await _poll_now_with_wrapper(wrapper)

        patches.setattr(
            "packages.core.settings_service.get_setting",
            AsyncMock(side_effect=["user@example.com", "password"]),
        )
        client = SimpleNamespace(
            login=AsyncMock(),
            get_devices=AsyncMock(return_value=[SimpleNamespace(device_id="device-1")]),
            get_device=AsyncMock(return_value=device),
        )
        patches.setattr("aiohttp.ClientSession", lambda: _SessionContext())
        patches.setattr("aioaquarea.Client", MagicMock(return_value=client))
        return await poll_now(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ("success", "partial_panasonic_failure", "consumption_unavailable"),
)
async def test_enabled_and_disabled_poll_now_have_identical_envelopes(
    monkeypatch, scenario
) -> None:
    enabled = await _run_poll_now_parity_branch(monkeypatch, enabled=True, scenario=scenario)
    disabled = await _run_poll_now_parity_branch(monkeypatch, enabled=False, scenario=scenario)

    assert set(enabled) == set(disabled) == {"status", "results"}
    assert enabled["status"] == disabled["status"]
    assert (
        set(enabled["results"])
        == set(disabled["results"])
        == {
            "device",
            "prices",
            "weather",
        }
    )
    for result_name in enabled["results"]:
        enabled_result = enabled["results"][result_name]
        disabled_result = disabled["results"][result_name]
        assert enabled_result.keys() == disabled_result.keys()
        assert enabled_result["success"] == disabled_result["success"]
        assert enabled_result["message"] == disabled_result["message"]


@pytest.mark.asyncio
async def test_two_plan_executors_share_wrapper_and_pass_only_public_verification_context():
    from packages.optimizer.actions import VerifyResult
    from packages.optimizer.executor_core import PlanExecutor

    wrapper = SimpleNamespace(refresh_device=AsyncMock(return_value=SimpleNamespace()))
    executors = [PlanExecutor(wrapper), PlanExecutor(wrapper)]
    handler = SimpleNamespace(
        verify=lambda *_args: VerifyResult(ok=True, expected_value={}, reason="verified")
    )
    action = SimpleNamespace(id=1, device_id="device-1", action_type="force_dhw_on")
    for executor in executors:
        executor._sleep = AsyncMock()
        executor._load_persisted_observation = AsyncMock(return_value=None)
        executor._reserve_verification_read = AsyncMock(return_value=True)
        executor._store_verification_progress = AsyncMock()
        result, _attempts = await executor._poll_until_verified(
            action=action,
            handler=handler,
            payload={},
            expected_state={},
            attempts=0,
            lane="ordinary",
            phase="initial",
            checkpoints=(0,),
            live_checkpoints=frozenset({0}),
            evidence_after=dt.datetime.now(dt.timezone.utc),
        )
        assert result.ok is True

    assert executors[0]._wrapper is executors[1]._wrapper is wrapper
    assert wrapper.refresh_device.await_count == 2
    assert all(
        call.kwargs["quota_context"] is ReadQuotaContext.EXECUTOR_VERIFICATION
        for call in wrapper.refresh_device.await_args_list
    )


@pytest.mark.asyncio
async def test_executor_context_preserves_local_fallback_acquisition(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=ConnectionError()))
    wrapper._device = _live_device()

    await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    wrapper._read_limiter.acquire.assert_awaited_once()
    wrapper._read_quota.reserve.assert_awaited_once_with("account-key", 1, ReadQuotaCategory.STATUS)


@pytest.mark.asyncio
async def test_executor_starvation_logs_once_then_recovery_resets(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._device = _live_device()
    exhausted = DistributedReadQuotaExhausted(_quota_status(7))
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=[exhausted, None]))
    monotonic_calls = 0

    def monotonic():
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 100.0 if monotonic_calls == 1 else 161.0

    monkeypatch.setattr("packages.core.services.aquarea.time.monotonic", monotonic)
    monkeypatch.setattr("packages.core.services.aquarea.asyncio.sleep", AsyncMock())

    with caplog.at_level(logging.WARNING, logger="packages.core.services.aquarea"):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        wrapper._read_quota.reserve.side_effect = [None]
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    assert EXECUTOR_VERIFICATION_QUOTA_STARVED_AFTER_SECONDS == 60
    assert [record.message for record in caplog.records].count(
        "executor_verification_quota_starved"
    ) == 1
    assert wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION].active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("cancellation", "local_fallback", "redis_failure"))
async def test_starved_verification_failures_do_not_reset_starvation_state(monkeypatch, outcome):
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    state = wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION]
    state.active = True
    state.consecutive_starved_verifications = 1
    exhausted = DistributedReadQuotaExhausted(_quota_status(0))
    failure = (
        asyncio.CancelledError()
        if outcome == "cancellation"
        else ConnectionError("redis unavailable")
    )
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=[exhausted, failure]))
    monkeypatch.setattr("packages.core.services.aquarea.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("packages.core.services.aquarea.asyncio.sleep", AsyncMock())

    reservation = None
    if outcome == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await wrapper._reserve_background_read(
                ReadQuotaCategory.STATUS,
                quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION,
            )
    else:
        reservation = await wrapper._reserve_background_read(
            ReadQuotaCategory.STATUS,
            quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION,
        )

    assert state.active is True
    assert state.consecutive_starved_verifications == 1
    assert state.verification_wait_started_at == 100.0
    if outcome == "cancellation":
        wrapper._read_limiter.acquire.assert_not_awaited()
    else:
        assert reservation.source == "local"
        wrapper._read_limiter.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_starved_then_distributed_success_recovers_once_and_relapse_emits_new_edge(
    monkeypatch, caplog
):
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._device = _live_device()
    exhausted = DistributedReadQuotaExhausted(_quota_status(0))
    wrapper._read_quota = SimpleNamespace(
        reserve=AsyncMock(side_effect=[exhausted, None, None, exhausted, None])
    )
    monkeypatch.setattr("packages.core.services.aquarea.asyncio.sleep", AsyncMock())
    monotonic_values = iter([100.0, 161.0, 200.0, 300.0, 361.0, 422.0, 500.0])
    monkeypatch.setattr(
        "packages.core.services.aquarea.time.monotonic",
        lambda: next(monotonic_values, 500.0),
    )

    with caplog.at_level(logging.INFO, logger="packages.core.services.aquarea"):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    messages = [record.message for record in caplog.records]
    assert messages.count("executor_verification_quota_starved") == 2
    assert messages.count("executor_verification_quota_recovered") == 1
    state = wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION]
    assert state.active is True
    assert state.consecutive_starved_verifications == 1
    assert state.verification_wait_started_at is None


@pytest.mark.asyncio
async def test_consecutive_long_waits_do_not_recover_until_fast_distributed_success(
    monkeypatch, caplog
):
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._device = _live_device()
    wrapper._record_live_status = MagicMock()
    exhausted = DistributedReadQuotaExhausted(_quota_status(0, retry_after_seconds=120))
    wrapper._read_quota = SimpleNamespace(
        reserve=AsyncMock(side_effect=[exhausted, None, exhausted, None, None, exhausted, None])
    )
    sleep = AsyncMock()
    monkeypatch.setattr("packages.core.services.aquarea.asyncio.sleep", sleep)
    monotonic_values = iter([100.0, 161.0, 200.0, 261.0, 300.0, 361.0])
    monkeypatch.setattr(
        "packages.core.services.aquarea.time.monotonic",
        lambda: next(monotonic_values, 361.0),
    )

    with caplog.at_level(logging.INFO, logger="packages.core.services.aquarea"):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        state = wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION]
        assert state.active is True
        assert state.consecutive_starved_verifications == 2
        assert state.ledger_slots_committed_since_recovery == 2
        assert state.accumulated_wait_seconds_since_recovery == 122.0

        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)
        assert state.active is False
        assert state.consecutive_starved_verifications == 0
        assert state.ledger_slots_committed_since_recovery == 0
        assert state.accumulated_wait_seconds_since_recovery == 0.0

        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    messages = [record.message for record in caplog.records]
    assert messages.count("executor_verification_quota_starved") == 2
    assert messages.count("executor_verification_quota_recovered") == 1
    assert sleep.await_args_list == [((120,), {}), ((120,), {}), ((120,), {})]


@pytest.mark.asyncio
async def test_disabled_wrapper_start_keeps_legacy_redis_without_distributed_quota(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", False)
    session = SimpleNamespace(close=AsyncMock())
    redis_client = SimpleNamespace(aclose=AsyncMock(), script_load=AsyncMock(), evalsha=AsyncMock())
    redis_factory = MagicMock(return_value=redis_client)
    redis_breaker = MagicMock()
    distributed_quota = MagicMock()
    monkeypatch.setattr("packages.core.services.aquarea.aiohttp.ClientSession", lambda: session)
    monkeypatch.setattr("packages.core.services.aquarea.redis.from_url", redis_factory)
    monkeypatch.setattr("packages.core.services.aquarea.RedisCircuitBreaker", redis_breaker)
    monkeypatch.setattr("packages.core.services.aquarea.DistributedReadQuota", distributed_quota)
    monkeypatch.setattr("packages.core.settings_service.get_user_tz", AsyncMock(return_value="UTC"))

    wrapper = AquareaWrapper()
    wrapper._ensure_authenticated = AsyncMock()
    await wrapper.start()
    await wrapper.stop()

    redis_factory.assert_called_once_with(settings.redis_url)
    redis_breaker.assert_called_once_with(redis_client)
    distributed_quota.assert_not_called()
    redis_client.script_load.assert_not_awaited()
    redis_client.evalsha.assert_not_awaited()
    assert wrapper._read_quota is None
    assert isinstance(wrapper._read_limiter, RateLimiter)


@pytest.mark.asyncio
async def test_disabled_poller_keeps_legacy_consumption_calls(monkeypatch) -> None:
    monkeypatch.setattr(polling_router.settings, "panasonic_distributed_read_quota_enabled", False)
    monkeypatch.setattr(polling_router, "get_session", lambda: _SessionContext())
    monkeypatch.setattr(
        polling_router,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )
    device = SimpleNamespace(
        long_id="device-1",
        temperature_outdoor=4.0,
        get_and_refresh_consumption=AsyncMock(side_effect=[1.0, 2.0, 3.0]),
    )
    wrapper = SimpleNamespace(get_device=AsyncMock(return_value=device))

    from packages.poller import main as poller_main

    monkeypatch.setattr(poller_main.settings, "panasonic_distributed_read_quota_enabled", False)
    monkeypatch.setattr(poller_main, "get_session", lambda: _SessionContext())
    monkeypatch.setattr(
        poller_main,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )

    await poller_main.poll_consumption(wrapper)

    wrapper.get_device.assert_awaited_once()
    assert device.get_and_refresh_consumption.await_count == 3


@pytest.mark.asyncio
async def test_enabled_poll_now_does_not_construct_legacy_clients(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = SimpleNamespace()
    delegated = AsyncMock(return_value={"status": "ok", "results": {}})
    monkeypatch.setattr(polling_router, "_poll_now_with_wrapper", delegated)

    with (
        patch("aiohttp.ClientSession") as client_session,
        patch("aioaquarea.Client") as client,
    ):
        response = await poll_now(wrapper)

    assert response == {"status": "ok", "results": {}}
    delegated.assert_awaited_once_with(wrapper)
    client_session.assert_not_called()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_poll_now_uses_legacy_client_and_three_consumption_reads(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", False)
    device = SimpleNamespace(
        long_id="device-1",
        current_action=SimpleNamespace(name="HEAT"),
        temperature_outdoor=4.0,
        refresh_data=AsyncMock(),
        get_and_refresh_consumption=AsyncMock(side_effect=[1.0, 2.0, 3.0]),
    )
    client = SimpleNamespace(
        login=AsyncMock(),
        get_devices=AsyncMock(return_value=[SimpleNamespace(device_id="device-1")]),
        get_device=AsyncMock(return_value=device),
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    feeds = SimpleNamespace(prices=[], currency="EUR", source="test")
    monkeypatch.setattr(
        "packages.core.settings_service.get_setting",
        AsyncMock(side_effect=["user@example.com", "password"]),
    )
    monkeypatch.setattr("aiohttp.ClientSession", lambda: Session())
    monkeypatch.setattr("aioaquarea.Client", MagicMock(return_value=client))
    monkeypatch.setattr(polling_router, "build_device_status_record", MagicMock(tank_temp=48.0))
    monkeypatch.setattr(
        polling_router,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )
    monkeypatch.setattr(polling_router, "ingest_device_status", AsyncMock())
    monkeypatch.setattr(polling_router, "get_session", lambda: _SessionContext())
    monkeypatch.setattr(polling_router, "get_price_area", AsyncMock(return_value="SE1"))
    monkeypatch.setattr("packages.poller.feeds.fetch_price_feed", AsyncMock(return_value=feeds))
    monkeypatch.setattr("packages.poller.feeds.fetch_weather", AsyncMock(return_value=[]))

    response = await poll_now(None)

    assert response["status"] == "partial"
    client.login.assert_awaited_once()
    assert device.get_and_refresh_consumption.await_count == 3


@pytest.mark.asyncio
async def test_cold_selected_device_id_reserves_one_status_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]

    assert await wrapper.get_selected_device_id() == "device-1"

    wrapper._read_quota.reserve.assert_awaited_once_with("account-key", 1, ReadQuotaCategory.STATUS)
    wrapper._read_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_distributed_reservation_waits_outside_device_lock(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    started = asyncio.Event()
    release = asyncio.Event()

    async def reserve(*_args, **_kwargs):
        assert not wrapper._device_lock.locked()
        started.set()
        await release.wait()

    wrapper._read_quota = SimpleNamespace(reserve=reserve)
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    wrapper._client.get_device.return_value = _live_device()
    task = asyncio.create_task(wrapper.get_device())
    await started.wait()
    assert not wrapper._device_lock.locked()
    release.set()
    await task


@pytest.mark.asyncio
async def test_local_fallback_waits_outside_device_lock(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=ConnectionError()))
    local_started = asyncio.Event()

    async def acquire():
        assert not wrapper._device_lock.locked()
        local_started.set()

    wrapper._read_limiter.acquire = acquire
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    wrapper._client.get_device.return_value = _live_device()

    await wrapper.get_device()
    assert local_started.is_set()
    assert wrapper.cached_rate_limit_reliability() is False


@pytest.mark.asyncio
async def test_write_limiter_is_untouched_by_enabled_reads(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._device = _live_device()
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())

    await wrapper.get_device()
    await wrapper.refresh_device()

    wrapper._write_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_response_contains_all_fields_without_identifiers() -> None:
    wrapper = _wrapper()
    status = ReadQuotaStatus(
        enabled=True,
        reliable=True,
        remaining=24,
        capacity=30,
        manual_required=READ_QUOTA_MANUAL_REQUIRED,
        retry_after_seconds=7,
        counters={
            ReadQuotaCategory.STATUS: 18,
            ReadQuotaCategory.CONSUMPTION: 6,
            ReadQuotaCategory.WEEKLY_TIMER: 1,
            ReadQuotaCategory.MANUAL: 4,
        },
        observed_at=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
    )
    wrapper.get_rate_limit_status = AsyncMock(return_value=status)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(aquarea_wrapper=wrapper)))

    response = await panasonic_read_quota(request)

    assert response == {
        "enabled": True,
        "reliable": True,
        "remaining": 24,
        "capacity": 30,
        "manual_required": 10,
        "retry_after_seconds": 7,
        "counters": {"status": 18, "consumption": 6, "weekly_timer": 1, "manual": 4},
        "observed_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
    }
    rendered = repr(response)
    assert "device-1" not in rendered
    assert "account-key" not in rendered


@pytest.mark.asyncio
async def test_api_lifespan_success_exposes_then_removes_one_wrapper(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    constructor = MagicMock(return_value=wrapper)
    monkeypatch.setattr(api_main, "AquareaWrapper", constructor)
    application = SimpleNamespace(state=SimpleNamespace())

    async with api_main.lifespan(application):
        assert application.state.aquarea_wrapper is wrapper
        assert (await api_main.version())["api_contract"]

    constructor.assert_called_once_with(read_only=True)
    wrapper.start.assert_awaited_once()
    wrapper.stop.assert_awaited_once()
    assert not hasattr(application.state, "aquarea_wrapper")


@pytest.mark.asyncio
async def test_lifespan_start_failure_leaves_other_routes_available(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("startup failed")), stop=AsyncMock()
    )
    monkeypatch.setattr(api_main, "AquareaWrapper", MagicMock(return_value=wrapper))
    application = SimpleNamespace(state=SimpleNamespace())

    async with api_main.lifespan(application):
        assert (await api_main.version())["version"]
        assert not hasattr(application.state, "aquarea_wrapper")

    wrapper.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_identity_then_cold_refresh_reserves_two_status_tokens(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-key"
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    wrapper._client.get_devices.return_value = [SimpleNamespace(device_id="device-1")]
    wrapper._client.get_device.return_value = _live_device()

    assert await wrapper.get_selected_device_id() == "device-1"
    await wrapper.refresh_device()

    assert wrapper._read_quota.reserve.await_count == 2
    assert wrapper._client.get_devices.await_count == 2


@pytest.mark.asyncio
async def test_executor_ledger_context_closes_before_reservation_returns() -> None:
    class TrackingContext:
        def __init__(self, session):
            self.session = session
            self.exited = False

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            self.exited = True
            return False

    now = dt.datetime.now(dt.timezone.utc)
    clock_result = MagicMock()
    clock_result.scalar_one.return_value = now
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(),
            MagicMock(),
            clock_result,
            MagicMock(),
            count_result,
            count_result,
        ]
    )
    context = TrackingContext(session)
    from packages.optimizer.executor_core import PlanExecutor

    executor = PlanExecutor(SimpleNamespace(), session_factory=lambda: context)

    assert await executor._reserve_verification_read(
        SimpleNamespace(id=1, device_id="device-1"), "ordinary", "initial", 15
    )
    assert context.exited is True


@pytest.mark.asyncio
async def test_quota_wait_cancellation_does_not_reset_account_or_device_state(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-digest"
    wrapper._device = _live_device("device-sensitive")
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    assert wrapper._account_key == "account-digest"
    assert wrapper._device.long_id == "device-sensitive"
    wrapper._read_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_starved_then_account_mismatch_does_not_reset_starvation_state(monkeypatch) -> None:
    wrapper = _wrapper()
    state = wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION]
    state.active = True
    state.consecutive_starved_verifications = 2
    state.verification_wait_started_at = 10.0
    reservation = ReadQuotaReservation("other-account", ReadQuotaCategory.STATUS, 1, "distributed")

    with pytest.raises(PanasonicQuotaAccountMismatchError):
        wrapper._account_key = "account-digest"
        wrapper._assert_reservation_account(reservation)

    assert state.active is True
    assert state.consecutive_starved_verifications == 2
    assert state.verification_wait_started_at == 10.0


@pytest.mark.asyncio
async def test_refresh_account_mismatch_does_not_recover_active_starvation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-digest"
    wrapper._device = _live_device()
    wrapper._resolve_account_key = AsyncMock(return_value="other-account")
    wrapper._read_quota = SimpleNamespace(reserve=AsyncMock())
    state = wrapper._quota_starvation[ReadQuotaContext.EXECUTOR_VERIFICATION]
    state.active = True
    state.consecutive_starved_verifications = 2
    state.ledger_slots_committed_since_recovery = 1
    state.accumulated_wait_seconds_since_recovery = 120.0

    with pytest.raises(PanasonicQuotaAccountMismatchError):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    assert state.active is True
    assert state.consecutive_starved_verifications == 2
    assert state.ledger_slots_committed_since_recovery == 2
    assert state.accumulated_wait_seconds_since_recovery == 120.0


@pytest.mark.asyncio
async def test_starvation_logs_redact_username_digest_key_and_device(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = _wrapper()
    wrapper._account_key = "account-digest-secret"
    wrapper._device = _live_device("device-secret")
    wrapper._read_quota = SimpleNamespace(
        reserve=AsyncMock(side_effect=[DistributedReadQuotaExhausted(_quota_status(0)), None])
    )
    monkeypatch.setattr("packages.core.services.aquarea.asyncio.sleep", AsyncMock())
    monotonic_values = iter([100.0, 161.0, 200.0])
    monkeypatch.setattr(
        "packages.core.services.aquarea.time.monotonic", lambda: next(monotonic_values, 200.0)
    )

    with caplog.at_level(logging.WARNING, logger="packages.core.services.aquarea"):
        await wrapper.refresh_device(quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION)

    captured = caplog.text
    assert "user@example.com" not in captured
    assert "account-digest-secret" not in captured
    assert "heatpump:aquarea:read_quota:v1:account-digest-secret" not in captured
    assert "device-secret" not in captured

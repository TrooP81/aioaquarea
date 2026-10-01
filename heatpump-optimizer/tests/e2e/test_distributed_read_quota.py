from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import redis.asyncio as redis
from sqlalchemy import select

from packages.core.models import ConsumptionRecord
from packages.core.resilience import (
    DistributedReadQuota,
    DistributedReadQuotaExhausted,
    READ_QUOTA_MANUAL_REQUIRED,
    ReadQuotaCategory,
)
from packages.core.services.aquarea import ConsumptionSnapshot
from packages.poller import main as poller_main


@pytest.fixture
def quota_account() -> str:
    return hashlib.sha256(f"phase1b-{uuid.uuid4()}".encode()).hexdigest()


@pytest_asyncio.fixture
async def quota_redis(quota_account):
    client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6380/0"))
    key = DistributedReadQuota._key(quota_account)
    await client.delete(key)
    yield client
    await client.delete(key)
    await client.aclose()


@pytest.mark.asyncio(loop_scope="session")
async def test_manual_reservations_are_atomic_and_preserve_eight_token_floor(
    quota_redis, quota_account
):
    quota = DistributedReadQuota(quota_redis)

    async def reserve_manual():
        try:
            await quota.reserve(
                quota_account,
                2,
                ReadQuotaCategory.MANUAL,
                minimum_remaining=READ_QUOTA_MANUAL_REQUIRED - 2,
            )
        except DistributedReadQuotaExhausted:
            return False
        return True

    results = await asyncio.gather(*(reserve_manual() for _ in range(20)))
    assert sum(results) == 11

    status = await quota.snapshot(quota_account)
    assert 7.9 <= status.remaining <= 8.1
    assert status.counters[ReadQuotaCategory.MANUAL] == 22


@pytest.mark.asyncio(loop_scope="session")
async def test_rejected_reservation_does_not_change_counters_and_refreshes_ttl(
    quota_redis, quota_account
):
    quota = DistributedReadQuota(quota_redis)
    await quota.reserve(quota_account, 29, ReadQuotaCategory.STATUS)
    before = await quota.snapshot(quota_account)
    ttl_before = await quota_redis.ttl(quota._key(quota_account))

    with pytest.raises(DistributedReadQuotaExhausted) as raised:
        await quota.reserve(
            quota_account,
            2,
            ReadQuotaCategory.MANUAL,
            minimum_remaining=READ_QUOTA_MANUAL_REQUIRED - 2,
        )

    after = await quota.snapshot(quota_account)
    ttl_after = await quota_redis.ttl(quota._key(quota_account))
    assert 1 <= raised.value.status.retry_after_seconds <= 3600
    assert after.counters == before.counters
    assert after.remaining >= before.remaining
    assert 0 < ttl_before <= 7200
    assert 0 < ttl_after <= 7200


@pytest.mark.asyncio(loop_scope="session")
async def test_rejected_reservation_retry_after_is_seconds_for_concrete_deficits(
    quota_redis, quota_account
):
    quota = DistributedReadQuota(quota_redis)
    key = quota._key(quota_account)

    await quota.reserve(quota_account, 30, ReadQuotaCategory.STATUS)
    with pytest.raises(DistributedReadQuotaExhausted) as one_token:
        await quota.reserve(quota_account, 1, ReadQuotaCategory.STATUS)
    assert 119 <= one_token.value.status.retry_after_seconds <= 121

    await quota_redis.delete(key)
    await quota.reserve(quota_account, 30, ReadQuotaCategory.STATUS)
    with pytest.raises(DistributedReadQuotaExhausted) as two_tokens:
        await quota.reserve(quota_account, 2, ReadQuotaCategory.STATUS)
    assert 239 <= two_tokens.value.status.retry_after_seconds <= 241
    assert two_tokens.value.status.retry_after_seconds <= 3600


@pytest.mark.asyncio(loop_scope="session")
async def test_snapshot_is_non_consuming_and_category_counters_are_distinct(
    quota_redis, quota_account
):
    quota = DistributedReadQuota(quota_redis)
    await quota.reserve(quota_account, 1, ReadQuotaCategory.STATUS)
    await quota.reserve(quota_account, 1, ReadQuotaCategory.CONSUMPTION)
    await quota.reserve(quota_account, 1, ReadQuotaCategory.WEEKLY_TIMER)
    before = await quota.snapshot(quota_account)
    after = await quota.snapshot(quota_account)

    assert after.remaining == pytest.approx(before.remaining, abs=0.01)
    assert after.counters[ReadQuotaCategory.STATUS] == 1
    assert after.counters[ReadQuotaCategory.CONSUMPTION] == 1
    assert after.counters[ReadQuotaCategory.WEEKLY_TIMER] == 1
    assert after.counters[ReadQuotaCategory.MANUAL] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_refill_math_caps_at_capacity(quota_redis, quota_account):
    quota = DistributedReadQuota(quota_redis)
    key = quota._key(quota_account)
    now_seconds, now_microseconds = await quota_redis.time()
    now_ms = now_seconds * 1000 + now_microseconds // 1000
    await quota_redis.hset(
        key,
        mapping={
            "tokens": 0,
            "updated_ms": now_ms - 3_600_000,
            "reserved_status_tokens": 0,
            "reserved_consumption_tokens": 0,
            "reserved_weekly_timer_tokens": 0,
            "reserved_manual_tokens": 0,
        },
    )

    status = await quota.reserve(quota_account, 1, ReadQuotaCategory.STATUS)

    assert status.remaining == pytest.approx(29, abs=0.01)


@pytest.mark.asyncio(loop_scope="session")
async def test_enabled_consumption_persists_all_non_zero_categories(db_session, monkeypatch):
    monkeypatch.setattr(poller_main.settings, "panasonic_distributed_read_quota_enabled", True)
    device = SimpleNamespace(long_id="fake-device", temperature_outdoor=4.0)
    wrapper = SimpleNamespace(
        get_device=AsyncMock(return_value=device),
        refresh_consumption=AsyncMock(
            return_value=ConsumptionSnapshot(
                date=dt.date.today(),
                heat_kwh=1.25,
                cool_kwh=2.5,
                tank_kwh=3.75,
                fetched_at=dt.datetime.now(dt.timezone.utc),
            )
        ),
    )
    outdoor = SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
    monkeypatch.setattr(poller_main, "resolve_outdoor_temperature", AsyncMock(return_value=outdoor))

    await poller_main.poll_consumption(wrapper)

    record = await db_session.scalar(
        select(ConsumptionRecord).where(ConsumptionRecord.device_id == "fake-device")
    )
    assert record is not None
    assert (record.heat_kwh, record.cool_kwh, record.tank_kwh) == (1.25, 2.5, 3.75)
    wrapper.get_device.assert_awaited_once()
    wrapper.refresh_consumption.assert_awaited_once()

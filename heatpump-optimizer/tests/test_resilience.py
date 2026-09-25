from __future__ import annotations

import asyncio

import pytest

from packages.core.resilience import (
    CircuitBreaker,
    RateLimiter,
    RedisCircuitBreaker,
    SafetyWriteCapacityError,
    safety_write_context,
)


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


class TestCircuitBreaker:
    def test_opens_after_failure_threshold(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)

        breaker.record_failure()
        assert breaker.is_open is False

        breaker.record_failure()
        assert breaker.is_open is True

    def test_success_resets_failures(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)

        breaker.record_failure()
        breaker.record_success()

        assert breaker.is_open is False


class TestRedisCircuitBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_three_failures_and_success_clears_state(self):
        redis = FakeRedis()
        breaker = RedisCircuitBreaker(redis)

        for _ in range(3):
            await breaker.record_failure()

        assert await breaker.is_open() == 900

        await breaker.record_success()

        assert await breaker.is_open() == 0


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_consumes_token_without_sleeping_when_available(self):
        limiter = RateLimiter(max_tokens=2, refill_per_second=1)

        await limiter.acquire()

        assert limiter._tokens < 2

    @pytest.mark.asyncio
    async def test_p2_ac10_ordinary_callers_cannot_take_reserved_tokens(self):
        limiter = RateLimiter(max_tokens=20, refill_per_second=20, reserve_tokens=2)
        limiter._tokens = 2

        acquisition = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        assert acquisition.done() is False
        acquisition.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquisition

    @pytest.mark.asyncio
    async def test_p2_ac10_safety_context_consumes_reserved_tokens_and_resets(self, monkeypatch):
        limiter = RateLimiter(max_tokens=20, refill_per_second=20, reserve_tokens=2)
        monkeypatch.setattr("packages.core.resilience.time.monotonic", lambda: 100.0)
        limiter._last_refill = 100.0
        limiter._tokens = 2
        token = safety_write_context.set(True)
        try:
            await limiter.acquire()
            await limiter.acquire()
        finally:
            safety_write_context.reset(token)

        assert limiter._tokens == 0
        assert safety_write_context.get() is False

    @pytest.mark.asyncio
    async def test_p2_ac10_safety_fails_promptly_when_no_token_exists(self):
        limiter = RateLimiter(max_tokens=20, refill_per_second=20, reserve_tokens=2)
        limiter._tokens = 0
        token = safety_write_context.set(True)
        try:
            with pytest.raises(SafetyWriteCapacityError):
                await limiter.acquire()
        finally:
            safety_write_context.reset(token)

    def test_p2_ac10_long_run_ordinary_throughput_is_bounded_by_safety_retries(self):
        # Four 15-minute safety retries consume four of the 20 hourly write tokens.
        assert 20 - 4 == 16

from __future__ import annotations

import pytest

from packages.core.resilience import CircuitBreaker, RateLimiter, RedisCircuitBreaker


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

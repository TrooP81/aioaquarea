from __future__ import annotations

import pytest

from packages.core.resilience import CircuitBreaker, RateLimiter


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


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_consumes_token_without_sleeping_when_available(self):
        limiter = RateLimiter(max_tokens=2, refill_per_second=1)

        await limiter.acquire()

        assert limiter._tokens < 2

"""Reusable resilience primitives for external integrations."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter."""

    max_tokens: int = 30
    refill_per_second: float = 30 / 3600
    _tokens: float = field(init=False, default=30)
    _last_refill: float = field(init=False, default_factory=time.monotonic)

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_per_second)
        self._last_refill = now

        if self._tokens < 1:
            wait = (1 - self._tokens) / self.refill_per_second
            logger.warning("Rate limit: waiting %.1fs before next API call", wait)
            await asyncio.sleep(wait)
            self._tokens = 0
        else:
            self._tokens -= 1


class CircuitBreaker:
    """Simple circuit breaker for auth failures."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 900):
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._last_failure_time: float = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        if self._open:
            if time.monotonic() - self._last_failure_time > self._recovery_timeout:
                logger.info("Circuit breaker: half-open, allowing retry")
                self._open = False
                self._failure_count = 0
                return False
            return True
        return False

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._open = True
            logger.error(
                "Circuit breaker OPEN after %s failures. Will retry in %ss",
                self._failure_count,
                self._recovery_timeout,
            )

    def record_success(self) -> None:
        self._failure_count = 0
        self._open = False

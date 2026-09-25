"""Reusable resilience primitives for external integrations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

safety_write_context: ContextVar[bool] = ContextVar("safety_write_context", default=False)


class SafetyWriteCapacityError(RuntimeError):
    """Raised when a safety write has no immediately available capacity."""


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter."""

    max_tokens: int = 30
    refill_per_second: float = 30 / 3600
    reserve_tokens: int = 0
    _tokens: float = field(init=False, default=30)
    _last_refill: float = field(init=False, default_factory=time.monotonic)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_per_second)
                self._last_refill = now
                floor = 0 if safety_write_context.get() else self.reserve_tokens
                if self._tokens >= floor + 1:
                    self._tokens -= 1
                    return
                if safety_write_context.get():
                    raise SafetyWriteCapacityError("safety write capacity exhausted")
                wait = (floor + 1 - self._tokens) / self.refill_per_second
            logger.warning("Rate limit: waiting %.1fs before next API call", wait)
            await asyncio.sleep(wait)


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


class RedisCircuitBreaker:
    """Persist authentication circuit-breaker state across process restarts."""

    def __init__(
        self,
        client: Any,
        failure_threshold: int = 3,
        recovery_timeout: int = 900,
        key_prefix: str = "heatpump:aquarea:auth_breaker",
    ) -> None:
        self._client = client
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures_key = f"{key_prefix}:failures"
        self._open_key = f"{key_prefix}:open"

    async def is_open(self) -> int:
        """Return remaining cooldown seconds, or zero when login is allowed."""
        return max(0, int(await self._client.ttl(self._open_key)))

    async def record_failure(self) -> None:
        failures = int(await self._client.incr(self._failures_key))
        await self._client.expire(self._failures_key, self._recovery_timeout)
        if failures >= self._failure_threshold:
            await self._client.set(self._open_key, "1", ex=self._recovery_timeout)
            logger.error(
                "Redis circuit breaker OPEN after %s failures. Will retry in %ss",
                failures,
                self._recovery_timeout,
            )

    async def record_success(self) -> None:
        await self._client.delete(self._failures_key, self._open_key)

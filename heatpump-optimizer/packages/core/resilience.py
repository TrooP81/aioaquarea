"""Reusable resilience primitives for external integrations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Mapping

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


class ReadQuotaCategory(StrEnum):
    STATUS = "status"
    CONSUMPTION = "consumption"
    WEEKLY_TIMER = "weekly_timer"
    MANUAL = "manual"


@dataclass(frozen=True)
class ReadQuotaStatus:
    enabled: bool
    reliable: bool
    remaining: float | None
    capacity: int
    manual_required: int
    retry_after_seconds: int
    counters: Mapping[ReadQuotaCategory, int]
    observed_at: datetime


@dataclass(frozen=True)
class ReadQuotaReservation:
    account_key: str
    category: ReadQuotaCategory
    tokens: int
    source: Literal["distributed", "local"]


class DistributedReadQuotaExhausted(RuntimeError):
    def __init__(self, status: ReadQuotaStatus) -> None:
        self.status = status
        super().__init__("Panasonic distributed read quota exhausted")


READ_QUOTA_CAPACITY = 30
READ_QUOTA_MANUAL_REQUIRED = 10


class DistributedReadQuota:
    """Atomic account-scoped logical-read bucket backed by Redis Lua."""

    _CAPACITY = READ_QUOTA_CAPACITY
    _MANUAL_REQUIRED = READ_QUOTA_MANUAL_REQUIRED
    _TTL_SECONDS = 7200
    _SCRIPT = """
local key = KEYS[1]
local requested = tonumber(ARGV[1])
local minimum = tonumber(ARGV[2])
local category = ARGV[3]
local snapshot = ARGV[4] == '1'
local capacity = tonumber(ARGV[5])
local refill_per_ms = tonumber(ARGV[6])
local ttl = tonumber(ARGV[7])
local now = redis.call('TIME')
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local values = redis.call('HMGET', key, 'tokens', 'updated_ms', 'reserved_status_tokens', 'reserved_consumption_tokens', 'reserved_weekly_timer_tokens', 'reserved_manual_tokens')
local tokens = tonumber(values[1]) or capacity
local updated_ms = tonumber(values[2]) or now_ms
tokens = math.min(capacity, tokens + math.max(0, now_ms - updated_ms) * refill_per_ms)
local admitted = 1
local retry_after = 0
if not snapshot and tokens < requested + minimum then
  admitted = 0
    retry_after = math.ceil((((requested + minimum) - tokens) / refill_per_ms) / 1000)
elseif not snapshot then
  tokens = tokens - requested
  local counter = 'reserved_' .. category .. '_tokens'
  redis.call('HINCRBYFLOAT', key, counter, requested)
end
redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now_ms)
redis.call('EXPIRE', key, ttl)
local counters = redis.call('HMGET', key, 'reserved_status_tokens', 'reserved_consumption_tokens', 'reserved_weekly_timer_tokens', 'reserved_manual_tokens')
return {admitted, tokens, retry_after, tonumber(counters[1]) or 0, tonumber(counters[2]) or 0, tonumber(counters[3]) or 0, tonumber(counters[4]) or 0, now_ms}
"""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script_sha: str | None = None

    @staticmethod
    def _key(account_key: str) -> str:
        return f"heatpump:aquarea:read_quota:v1:{account_key}"

    async def reserve(
        self,
        account_key: str,
        tokens: int,
        category: ReadQuotaCategory,
        *,
        minimum_remaining: int = 0,
    ) -> ReadQuotaStatus:
        result = await self._run(account_key, tokens, minimum_remaining, category, snapshot=False)
        status = self._status(result)
        if not int(result[0]):
            raise DistributedReadQuotaExhausted(status)
        return status

    async def snapshot(self, account_key: str) -> ReadQuotaStatus:
        result = await self._run(account_key, 0, 0, ReadQuotaCategory.STATUS, snapshot=True)
        return self._status(result)

    async def _run(
        self,
        account_key: str,
        tokens: int,
        minimum_remaining: int,
        category: ReadQuotaCategory,
        *,
        snapshot: bool,
    ) -> list[Any]:
        key = self._key(account_key)
        arguments = [
            tokens,
            minimum_remaining,
            category.value,
            int(snapshot),
            self._CAPACITY,
            self._CAPACITY / 3_600_000,
            self._TTL_SECONDS,
        ]
        if self._script_sha is None:
            self._script_sha = await self._client.script_load(self._SCRIPT)
        try:
            return await self._client.evalsha(self._script_sha, 1, key, *arguments)
        except Exception as exc:
            if "NOSCRIPT" not in str(exc).upper():
                raise
            self._script_sha = await self._client.script_load(self._SCRIPT)
            return await self._client.evalsha(self._script_sha, 1, key, *arguments)

    @classmethod
    def _status(cls, result: list[Any]) -> ReadQuotaStatus:
        observed_at = datetime.fromtimestamp(int(result[7]) / 1000, tz=timezone.utc)
        return ReadQuotaStatus(
            enabled=True,
            reliable=True,
            remaining=float(result[1]),
            capacity=cls._CAPACITY,
            manual_required=cls._MANUAL_REQUIRED,
            retry_after_seconds=max(0, int(result[2])),
            counters={
                ReadQuotaCategory.STATUS: int(float(result[3])),
                ReadQuotaCategory.CONSUMPTION: int(float(result[4])),
                ReadQuotaCategory.WEEKLY_TIMER: int(float(result[5])),
                ReadQuotaCategory.MANUAL: int(float(result[6])),
            },
            observed_at=observed_at,
        )

"""Aioaquarea wrapper with rate limiting, token persistence, and circuit breaker."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as redis

from aioaquarea import Client, AquareaEnvironment, DeviceInfo

from ..core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter."""

    max_tokens: int = 30
    refill_per_second: float = 30 / 3600  # 30 per hour
    _tokens: float = field(init=False, default=30)
    _last_refill: float = field(init=False, default_factory=time.monotonic)

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_per_second)
        self._last_refill = now

        if self._tokens < 1:
            wait = (1 - self._tokens) / self.refill_per_second
            logger.warning(f"Rate limit: waiting {wait:.1f}s before next API call")
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
                f"Circuit breaker OPEN after {self._failure_count} failures. "
                f"Will retry in {self._recovery_timeout}s"
            )

    def record_success(self) -> None:
        self._failure_count = 0
        self._open = False


class AquareaWrapper:
    """
    Wrapper around aioaquarea.Client with:
    - Token persistence in Redis
    - Rate limiting
    - Circuit breaker on auth failures
    - Singleton device reference
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._session: aiohttp.ClientSession | None = None
        self._redis: redis.Redis | None = None
        self._device = None
        self._device_info: DeviceInfo | None = None
        self._read_limiter = RateLimiter(max_tokens=30, refill_per_second=30 / 3600)
        self._write_limiter = RateLimiter(max_tokens=20, refill_per_second=20 / 3600)
        self._circuit_breaker = CircuitBreaker()
        self._authenticated = False

    async def start(self) -> None:
        """Initialize session, redis, and authenticate."""
        self._session = aiohttp.ClientSession()
        self._redis = redis.from_url(settings.redis_url)

        self._client = Client(
            session=self._session,
            username=settings.aquarea_username,
            password=settings.aquarea_password,
            device_direct=True,
            refresh_login=True,
            environment=AquareaEnvironment.PRODUCTION,
        )

        await self._authenticate()

    async def stop(self) -> None:
        """Cleanup."""
        if self._session:
            await self._session.close()
        if self._redis:
            await self._redis.aclose()

    async def _authenticate(self) -> None:
        """Authenticate, respecting circuit breaker."""
        if self._circuit_breaker.is_open:
            raise RuntimeError("Circuit breaker is open — auth disabled temporarily")

        try:
            await self._client.login()
            self._authenticated = True
            self._circuit_breaker.record_success()
            logger.info("Authenticated with Panasonic cloud")
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error(f"Authentication failed: {e}")
            raise

    async def get_device(self):
        """Get or refresh the device object."""
        await self._read_limiter.acquire()

        if self._device is None:
            devices = await self._client.get_devices(include_long_id=True)
            if not devices:
                raise RuntimeError("No devices found on account")
            self._device_info = devices[0]
            from datetime import timedelta

            self._device = await self._client.get_device(
                device_info=self._device_info,
                consumption_refresh_interval=timedelta(minutes=5),
            )
        return self._device

    async def refresh_device(self):
        """Refresh device data."""
        await self._read_limiter.acquire()
        device = await self.get_device()
        await device.refresh_data()
        return device

    async def set_mode(self, mode) -> None:
        """Set operation mode with rate limiting."""
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_mode(mode)
        logger.info(f"Set mode to {mode}")

    async def set_tank_temperature(self, temperature: int) -> None:
        """Set tank target temperature."""
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_tank_temperature(temperature)
        logger.info(f"Set tank target to {temperature}°C")

    async def set_quiet_mode(self, mode) -> None:
        """Set quiet mode."""
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_quiet_mode(mode)
        logger.info(f"Set quiet mode to {mode}")

    async def force_dhw(self, state) -> None:
        """Force DHW on/off."""
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_force_dhw(state)
        logger.info(f"Set force DHW to {state}")

    async def get_consumption(self, date_type="date"):
        """Get consumption data."""
        await self._read_limiter.acquire()
        device = await self.get_device()
        return device.consumption

"""Aioaquarea wrapper with rate limiting, token persistence, and circuit breaker."""

from __future__ import annotations

import logging

import aiohttp
import redis.asyncio as redis

from aioaquarea import AquareaEnvironment, Client, DeviceInfo

from ..config import settings
from ..resilience import CircuitBreaker, RateLimiter

logger = logging.getLogger(__name__)


class AquareaWrapper:
    """Wrapper around aioaquarea.Client for application use."""

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
            raise RuntimeError("Circuit breaker is open - auth disabled temporarily")

        try:
            await self._client.login()
            self._authenticated = True
            self._circuit_breaker.record_success()
            logger.info("Authenticated with Panasonic cloud")
        except Exception as exc:
            self._circuit_breaker.record_failure()
            logger.error("Authentication failed: %s", exc)
            raise

    async def get_device(self):
        """Return the cached device, loading it once when necessary.

        Merely retrieving the in-process object must not consume Panasonic's
        read budget. Initialisation does perform network I/O and therefore
        acquires exactly one logical read token.
        """
        if self._device is not None:
            return self._device

        await self._read_limiter.acquire()
        devices = await self._client.get_devices()
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
        """Refresh device data using one logical Panasonic read token.

        Creating a device already fetches its current status. Avoid an
        immediate second refresh on first use, which previously spent two
        limiter tokens and duplicated the cloud request.
        """
        if self._device is None:
            return await self.get_device()

        await self._read_limiter.acquire()
        await self._device.refresh_data()
        return self._device

    async def set_mode(self, mode) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_mode(mode)
        logger.info("Set mode to %s", mode)

    async def set_tank_temperature(self, temperature: int) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_tank_temperature(temperature)
        logger.info("Set tank target to %sC", temperature)

    async def set_quiet_mode(self, mode) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_quiet_mode(mode)
        logger.info("Set quiet mode to %s", mode)

    async def force_dhw(self, state) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_force_dhw(state)
        logger.info("Set force DHW to %s", state)

    async def set_zone_heat_temperature(self, zone_id: int, temperature: int) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_temperature(temperature, zone_id=zone_id or 1)
        logger.info("Set zone %s heat target to %sC", zone_id, temperature)

    async def set_special_status(self, status: str) -> None:
        from aioaquarea.data import SpecialStatus

        await self._write_limiter.acquire()
        device = await self.get_device()
        mode = SpecialStatus.ECO if status == "ECO" else SpecialStatus.COMFORT
        await device.set_special_status(mode)
        logger.info("Set special status to %s", status)

    async def clear_special_status(self) -> None:
        await self._write_limiter.acquire()
        device = await self.get_device()
        await device.set_special_status(None)
        logger.info("Cleared special status")

    async def get_consumption(self, date_type="date"):
        await self._read_limiter.acquire()
        device = await self.get_device()
        return device.consumption

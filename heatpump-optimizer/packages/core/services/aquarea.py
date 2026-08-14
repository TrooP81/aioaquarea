"""Aioaquarea wrapper with rate limiting, token persistence, and circuit breaker."""

from __future__ import annotations

import asyncio
import logging
import math
import time

import aiohttp
import redis.asyncio as redis

from aioaquarea import (
    AquareaEnvironment,
    Client,
    DeviceUnavailableError,
    DeviceInfo,
    ForceHeater,
    HolidayTimer,
    PowerfulTime,
)
from aioaquarea.data import StatusDataMode

from ..config import settings
from ..resilience import CircuitBreaker, RateLimiter

logger = logging.getLogger(__name__)

_COMMAND_STATUS_MAX_AGE_SECONDS = 60.0
_ADAPTER_RETRY_BASE_SECONDS = 300
_ADAPTER_RETRY_MAX_SECONDS = 1800


class PanasonicAdapterUnavailableError(RuntimeError):
    """Normalized live-adaptor outage with retry diagnostics."""

    def __init__(
        self,
        *,
        device_id: str | None,
        reason: str,
        consecutive_failures: int,
        retry_after_seconds: int,
        message: str | None = None,
    ) -> None:
        self.device_id = device_id
        self.reason = reason
        self.consecutive_failures = consecutive_failures
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or "Panasonic adaptor unavailable")


class PanasonicCachedStatusError(PanasonicAdapterUnavailableError):
    """Raised when Panasonic returns cloud cache instead of live adaptor data."""

    def __init__(
        self,
        *,
        device_id: str | None = None,
        reason: str = "cloud_cached_status",
        consecutive_failures: int = 0,
        retry_after_seconds: int = 0,
    ) -> None:
        super().__init__(
            device_id=device_id,
            reason=reason,
            consecutive_failures=consecutive_failures,
            retry_after_seconds=retry_after_seconds,
            message="Panasonic adaptor unavailable; refusing cloud-cached device status",
        )


class PanasonicAdapterBackoffError(RuntimeError):
    """Raised without network I/O while an adaptor retry delay is active."""

    def __init__(
        self,
        *,
        device_id: str | None,
        reason: str,
        consecutive_failures: int,
        retry_after_seconds: int,
    ) -> None:
        self.device_id = device_id
        self.reason = reason
        self.consecutive_failures = consecutive_failures
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Panasonic adaptor retry deferred for {retry_after_seconds} seconds")


class PanasonicCommandValidationError(ValueError):
    """Raised when a command is incompatible with the live device state."""


class AquareaWrapper:
    """Wrapper around aioaquarea.Client for application use."""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._session: aiohttp.ClientSession | None = None
        self._redis: redis.Redis | None = None
        self._device = None
        self._device_info: DeviceInfo | None = None
        self._device_lock = asyncio.Lock()
        self._last_live_status_at: float | None = None
        self._adapter_failure_count = 0
        self._adapter_retry_at: float | None = None
        self._adapter_failure_device_id: str | None = None
        self._adapter_failure_reason = "unknown"
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

        async with self._device_lock:
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
            self._record_live_status(self._device)
            return self._device

    async def refresh_device(self):
        """Refresh device data using one logical Panasonic read token.

        Creating a device already fetches its current status. Avoid an
        immediate second refresh on first use, which previously spent two
        limiter tokens and duplicated the cloud request.
        """
        self._raise_if_adapter_backoff_active()

        if self._device is None:
            return await self.get_device()

        await self._read_limiter.acquire()
        try:
            await self._device.refresh_data(allow_cached_fallback=False)
        except DeviceUnavailableError as exc:
            self._last_live_status_at = None
            failures, retry_after = self._register_adapter_failure(
                device_id=exc.device_id,
                reason=exc.reason or str(exc),
            )
            raise PanasonicAdapterUnavailableError(
                device_id=exc.device_id,
                reason=exc.reason or str(exc),
                consecutive_failures=failures,
                retry_after_seconds=retry_after,
            ) from exc
        self._record_live_status(self._device)
        return self._device

    def _record_live_status(self, device) -> None:
        try:
            self._require_live_status(device)
        except PanasonicCachedStatusError as exc:
            self._last_live_status_at = None
            device_id = getattr(device, "long_id", None) or getattr(
                self._device_info, "device_id", None
            )
            failures, retry_after = self._register_adapter_failure(
                device_id=device_id,
                reason=exc.reason,
            )
            raise PanasonicCachedStatusError(
                device_id=device_id,
                reason=exc.reason,
                consecutive_failures=failures,
                retry_after_seconds=retry_after,
            ) from exc
        self._last_live_status_at = time.monotonic()
        self._reset_adapter_backoff()

    def _register_adapter_failure(self, *, device_id: str | None, reason: str) -> tuple[int, int]:
        self._adapter_failure_count += 1
        exponent = min(self._adapter_failure_count - 1, 3)
        retry_after = min(
            _ADAPTER_RETRY_BASE_SECONDS * (2**exponent),
            _ADAPTER_RETRY_MAX_SECONDS,
        )
        self._adapter_retry_at = time.monotonic() + retry_after
        self._adapter_failure_device_id = device_id
        self._adapter_failure_reason = reason
        return self._adapter_failure_count, retry_after

    def _reset_adapter_backoff(self) -> None:
        self._adapter_failure_count = 0
        self._adapter_retry_at = None
        self._adapter_failure_device_id = None
        self._adapter_failure_reason = "unknown"

    def _raise_if_adapter_backoff_active(self) -> None:
        if self._adapter_retry_at is None:
            return
        remaining = self._adapter_retry_at - time.monotonic()
        if remaining <= 0:
            return
        raise PanasonicAdapterBackoffError(
            device_id=self._adapter_failure_device_id,
            reason=self._adapter_failure_reason,
            consecutive_failures=self._adapter_failure_count,
            retry_after_seconds=max(1, math.ceil(remaining)),
        )

    async def _get_writable_device(self):
        """Require a recent live adaptor response before any cloud write."""

        device = await self.get_device()
        if device.status_data_mode == StatusDataMode.CACHED:
            self._last_live_status_at = None
            self._require_live_status(device)

        if (
            self._last_live_status_at is None
            or time.monotonic() - self._last_live_status_at > _COMMAND_STATUS_MAX_AGE_SECONDS
        ):
            device = await self.refresh_device()
        return device

    async def _prepare_write(self):
        """Reserve write capacity without letting the live preflight go stale."""

        await self._get_writable_device()
        await self._write_limiter.acquire()
        return await self._get_writable_device()

    @staticmethod
    def _require_live_status(device) -> None:
        if device.status_data_mode == StatusDataMode.CACHED:
            raise PanasonicCachedStatusError()

    async def set_mode(self, mode) -> None:
        device = await self._prepare_write()
        await device.set_mode(mode)
        logger.info("Set mode to %s", mode)

    async def set_tank_temperature(self, temperature: int) -> None:
        device = await self._get_writable_device()
        tank = self._validate_tank_temperature(device, temperature)
        if tank.target_temperature == temperature:
            logger.info("Tank target already %sC; skipping Panasonic write", temperature)
            return

        await self._write_limiter.acquire()
        device = await self._get_writable_device()
        tank = self._validate_tank_temperature(device, temperature)
        if tank.target_temperature == temperature:
            logger.info("Tank target became %sC while waiting; skipping write", temperature)
            return

        await tank.set_target_temperature(temperature)
        logger.info("Set tank target to %sC", temperature)

    @staticmethod
    def _validate_tank_temperature(device, temperature: int):
        tank = getattr(device, "tank", None)
        if tank is None:
            raise PanasonicCommandValidationError("Panasonic device has no writable hot-water tank")

        minimum = getattr(tank, "heat_min", None)
        maximum = getattr(tank, "heat_max", None)
        if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
            raise PanasonicCommandValidationError(
                "Panasonic tank did not report writable temperature limits"
            )
        if isinstance(temperature, bool) or not isinstance(temperature, int):
            raise PanasonicCommandValidationError(
                "Panasonic tank target must be a whole number of degrees Celsius"
            )
        if not minimum <= temperature <= maximum:
            raise PanasonicCommandValidationError(
                f"Panasonic tank target {temperature}C is outside the live "
                f"device range {minimum}-{maximum}C"
            )
        return tank

    async def set_quiet_mode(self, mode) -> None:
        device = await self._get_writable_device()
        if getattr(device, "quiet_mode", None) == mode:
            logger.info("Quiet mode already %s; skipping Panasonic write", mode)
            return

        await self._write_limiter.acquire()
        device = await self._get_writable_device()
        if getattr(device, "quiet_mode", None) == mode:
            logger.info("Quiet mode became %s while waiting; skipping write", mode)
            return

        await device.set_quiet_mode(mode)
        logger.info("Set quiet mode to %s", mode)

    async def force_dhw(self, state) -> None:
        device = await self._get_writable_device()
        if getattr(device, "force_dhw", None) == state:
            logger.info("Force DHW already %s; skipping Panasonic write", state)
            return

        await self._write_limiter.acquire()
        device = await self._get_writable_device()
        if getattr(device, "force_dhw", None) == state:
            logger.info("Force DHW became %s while waiting; skipping write", state)
            return

        await device.set_force_dhw(state)
        logger.info("Set force DHW to %s", state)

    async def set_powerful_time(self, duration: PowerfulTime) -> None:
        """Set Panasonic's bounded 30/60/90 minute powerful mode."""
        device = await self._prepare_write()
        await device.set_powerful_time(duration)
        logger.info("Set powerful mode to %s", duration)

    async def set_force_heater(self, state: ForceHeater) -> None:
        """Enable or disable Panasonic's auxiliary-heater override."""
        device = await self._prepare_write()
        await device.set_force_heater(state)
        logger.info("Set force heater to %s", state)

    async def set_holiday_timer(self, state: HolidayTimer) -> None:
        """Enable or disable the Panasonic holiday timer."""
        device = await self._prepare_write()
        await device.set_holiday_timer(state)
        logger.info("Set holiday timer to %s", state)

    async def request_defrost(self) -> None:
        """Request defrost; the device entity suppresses an already-active request."""
        device = await self._prepare_write()
        await device.request_defrost()
        logger.info("Requested defrost")

    async def set_zone_heat_temperature(self, zone_id: int, temperature: int) -> None:
        resolved_zone_id = zone_id or 1
        device = await self._get_writable_device()
        zone = getattr(device, "zones", {}).get(resolved_zone_id)
        if zone is not None and zone.heat_target_temperature == temperature:
            logger.info(
                "Zone %s heat target already %sC; skipping Panasonic write",
                resolved_zone_id,
                temperature,
            )
            return

        await self._write_limiter.acquire()
        device = await self._get_writable_device()
        zone = getattr(device, "zones", {}).get(resolved_zone_id)
        if zone is not None and zone.heat_target_temperature == temperature:
            logger.info(
                "Zone %s heat target became %sC while waiting; skipping write",
                resolved_zone_id,
                temperature,
            )
            return

        await device.set_temperature(temperature, zone_id=resolved_zone_id)
        logger.info("Set zone %s heat target to %sC", resolved_zone_id, temperature)

    async def set_special_status(self, status: str) -> None:
        from aioaquarea.data import SpecialStatus

        device = await self._prepare_write()
        mode = SpecialStatus.ECO if status == "ECO" else SpecialStatus.COMFORT
        await device.set_special_status(mode)
        logger.info("Set special status to %s", status)

    async def clear_special_status(self) -> None:
        device = await self._prepare_write()
        await device.set_special_status(None)
        logger.info("Cleared special status")

    async def get_consumption(self, date_type="date"):
        await self._read_limiter.acquire()
        device = await self.get_device()
        return device.consumption

"""SmartThings API client — polls temperatureMeasurement devices for indoor air temp."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import httpx
import structlog

from packages.core.settings_service import get_setting

logger = structlog.get_logger(__name__)

SMARTTHINGS_API_BASE = "https://api.smartthings.com/v1"

# Stale reading threshold — ignore SmartThings readings older than this
STALE_READING_THRESHOLD = dt.timedelta(minutes=30)

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0


class SmartThingsClient:
    """Thin async wrapper around the SmartThings REST API."""

    def __init__(self, pat: str):
        self._pat = pat
        self._headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def discover_temp_sensors(self) -> list[dict[str, Any]]:
        """
        Return SmartThings devices that have the ``temperatureMeasurement`` capability.

        Each item: ``{"device_id", "label", "room_id"}``.
        """
        url = f"{SMARTTHINGS_API_BASE}/devices"
        params = {"capability": "temperatureMeasurement"}

        async with httpx.AsyncClient(timeout=30, headers=self._headers) as client:
            resp = await _request_with_retry(client, url, params=params)
            data = resp.json()

        devices: list[dict[str, Any]] = []
        for item in data.get("items", []):
            devices.append(
                {
                    "device_id": item["deviceId"],
                    "label": item.get("label", item.get("name", "")),
                    "room_id": item.get("roomId"),
                }
            )
        return devices

    # ------------------------------------------------------------------
    # Temperature reading
    # ------------------------------------------------------------------

    async def get_temperature(self, device_id: str) -> dict[str, Any] | None:
        """
        Read the current temperature from a single device.

        Returns ``{"value": float_celsius, "timestamp": str}`` or *None* on
        transient failure.
        """
        url = (
            f"{SMARTTHINGS_API_BASE}/devices/{device_id}"
            f"/components/main/capabilities/temperatureMeasurement/status"
        )

        async with httpx.AsyncClient(timeout=15, headers=self._headers) as client:
            resp = await _request_with_retry(client, url)
            if resp.status_code == 404:
                logger.warning("smartthings_device_not_found", device_id=device_id)
                return None
            data = resp.json()

        temp_attr = data.get("temperature", {})
        value = temp_attr.get("value")
        unit = temp_attr.get("unit", "C")
        timestamp = temp_attr.get("timestamp")

        if value is None:
            return None

        # Convert to Celsius if needed
        celsius = _to_celsius(float(value), unit)

        # Sanity bounds
        if celsius < -50.0 or celsius > 80.0:
            logger.warning(
                "smartthings_temp_out_of_range",
                device_id=device_id,
                value=celsius,
            )
            return None

        return {"value": celsius, "timestamp": timestamp}

    async def get_temperatures_batch(
        self, device_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Poll multiple devices, returning successful readings only."""
        results: list[dict[str, Any]] = []
        for did in device_ids:
            try:
                reading = await self.get_temperature(did)
                if reading is not None:
                    results.append({"device_id": did, **reading})
            except SmartThingsRateLimited:
                logger.warning("smartthings_rate_limited, stopping batch")
                break
            except SmartThingsError as exc:
                logger.error("smartthings_read_failed", device_id=did, error=str(exc))
        return results


# ------------------------------------------------------------------
# Device cache — avoid re-discovering on every poll
# ------------------------------------------------------------------

_device_cache: list[dict[str, Any]] = []
_device_cache_ts: float = 0.0
_DEVICE_CACHE_TTL = 3600.0  # 1 hour
_device_cache_lock = asyncio.Lock()


async def _get_cached_devices(client: SmartThingsClient) -> list[dict[str, Any]]:
    """Return discovered devices, refreshing cache hourly."""
    global _device_cache, _device_cache_ts
    async with _device_cache_lock:
        now = time.monotonic()
        if not _device_cache or (now - _device_cache_ts) > _DEVICE_CACHE_TTL:
            _device_cache = await client.discover_temp_sensors()
            _device_cache_ts = now
        return _device_cache


def invalidate_device_cache() -> None:
    """Force cache refresh on next call (e.g. after settings change)."""
    global _device_cache, _device_cache_ts
    _device_cache = []
    _device_cache_ts = 0.0


# ------------------------------------------------------------------
# Poller entry-point (called from packages/poller/main.py)
# ------------------------------------------------------------------


async def poll_smartthings_temps(session) -> int:
    """
    Fetch temperatures from all configured SmartThings sensors and
    write ``IndoorTempReading`` rows.  Returns the number of readings written.
    """
    from packages.core.models import IndoorTempReading

    pat = await get_setting("smartthings_pat")
    if not pat:
        return 0

    client = SmartThingsClient(pat)

    # Resolve which devices to poll
    device_ids_str = await get_setting("smartthings_device_ids")
    if device_ids_str:
        device_ids = [d.strip() for d in device_ids_str.split(",") if d.strip()]
        device_labels: dict[str, str] = {}
    else:
        # Auto-discover (cached for 1 hour)
        devices = await _get_cached_devices(client)
        device_ids = [d["device_id"] for d in devices]
        device_labels = {d["device_id"]: d["label"] for d in devices}

    if not device_ids:
        return 0

    readings = await client.get_temperatures_batch(device_ids)
    now = dt.datetime.now(dt.timezone.utc)

    count = 0
    for r in readings:
        # Exclude stale readings (SmartThings reports old timestamps when
        # a sensor hasn't reported recently)
        if r.get("timestamp"):
            try:
                reading_ts = dt.datetime.fromisoformat(
                    r["timestamp"].replace("+0000", "+00:00")
                )
                if (now - reading_ts) > STALE_READING_THRESHOLD:
                    logger.warning(
                        "smartthings_stale_reading",
                        device_id=r["device_id"],
                        age_minutes=round((now - reading_ts).total_seconds() / 60),
                    )
                    continue
            except (ValueError, TypeError):
                pass  # If timestamp parsing fails, accept the reading

        session.add(
            IndoorTempReading(
                timestamp=now,
                device_id=r["device_id"],
                device_label=device_labels.get(r["device_id"]),
                temperature=r["value"],
            )
        )
        count += 1

    return count


# ------------------------------------------------------------------
# Helpers / exceptions
# ------------------------------------------------------------------


class SmartThingsError(Exception):
    """Base exception for SmartThings API errors."""


class SmartThingsAuthError(SmartThingsError):
    """PAT is invalid or missing required scopes."""


class SmartThingsRateLimited(SmartThingsError):
    """HTTP 429 — too many requests."""


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
) -> httpx.Response:
    """
    GET *url* with exponential backoff on transient errors and 429s.

    Respects the ``Retry-After`` header when present.
    Raises on auth errors (401/403) immediately — no retry.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=params)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            last_exc = exc
            wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(
                "smartthings_network_error_retrying",
                attempt=attempt + 1,
                wait=wait,
                error=str(exc),
            )
            await asyncio.sleep(wait)
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** attempt)
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    "smartthings_rate_limited_retrying",
                    attempt=attempt + 1,
                    wait=wait,
                )
                await asyncio.sleep(wait)
                continue
            raise SmartThingsRateLimited("SmartThings rate limit exceeded after retries")

        # Non-retryable status codes
        if resp.status_code not in (404,):  # 404 handled by caller
            _check_response(resp)
        return resp

    # All retries exhausted on network errors
    raise SmartThingsError(f"Request failed after {MAX_RETRIES} retries: {last_exc}")


def _check_response(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise SmartThingsAuthError("Invalid or expired SmartThings PAT")
    if resp.status_code == 403:
        raise SmartThingsAuthError(
            "SmartThings PAT missing required scopes (need l:devices, r:devices:*)"
        )
    if resp.status_code == 429:
        raise SmartThingsRateLimited("SmartThings rate limit exceeded")
    resp.raise_for_status()


def _to_celsius(value: float, unit: str) -> float:
    if unit.upper() in ("F", "FAHRENHEIT"):
        return (value - 32.0) * 5.0 / 9.0
    return value

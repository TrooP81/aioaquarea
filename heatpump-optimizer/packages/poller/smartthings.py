"""SmartThings API client — polls temperatureMeasurement devices for indoor air temp.

Supports OAuth 2.0 Authorization Code tokens (auto-refreshed) with legacy PAT fallback.
"""

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

# Battery sensors commonly publish temperature only when it changes or at a
# periodic heartbeat. Three hours tolerates a quiet sensor without accepting a
# day-old value, while the independent poll-recency check still detects
# API/poller failures quickly.
STALE_READING_THRESHOLD = dt.timedelta(hours=3)
MIN_STALE_READING_MINUTES = 30
MAX_STALE_READING_MINUTES = 24 * 60

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0


class SmartThingsClient:
    """Thin async wrapper around the SmartThings REST API.

    Accepts either an explicit *access_token* (from OAuth or legacy PAT).
    """

    def __init__(self, access_token: str):
        self._access_token = access_token
        self._headers = {
            "Authorization": f"Bearer {access_token}",
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

    async def get_temperatures_batch(self, device_ids: list[str]) -> list[dict[str, Any]]:
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
_stale_devices: set[str] = set()


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
    _stale_devices.clear()


async def get_stale_reading_threshold() -> dt.timedelta:
    """Return the bounded device-reported timestamp age allowed for control."""

    raw = await get_setting("smartthings_device_max_age_minutes")
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = round(STALE_READING_THRESHOLD.total_seconds() / 60)
    minutes = max(MIN_STALE_READING_MINUTES, min(minutes, MAX_STALE_READING_MINUTES))
    return dt.timedelta(minutes=minutes)


def _record_staleness_transition(
    device_id: str, *, is_stale: bool, age_minutes: int | None, threshold_minutes: int
) -> None:
    """Log one warning per stale episode and one recovery event."""

    if is_stale and device_id not in _stale_devices:
        _stale_devices.add(device_id)
        logger.warning(
            "smartthings_stale_reading",
            device_id=device_id,
            age_minutes=age_minutes,
            stale_after_minutes=threshold_minutes,
        )
    elif not is_stale and device_id in _stale_devices:
        _stale_devices.remove(device_id)
        logger.info(
            "smartthings_reading_recovered",
            device_id=device_id,
            age_minutes=age_minutes,
        )


# ------------------------------------------------------------------
# Poller entry-point (called from packages/poller/main.py)
# ------------------------------------------------------------------


async def get_selected_device_ids() -> list[str]:
    """Return the user-selected SmartThings device IDs to poll.

    Reads the ``smartthings_device_ids`` setting. An empty value means "no
    explicit selection" — callers should treat that as "use all discovered
    sensors". Readers of indoor-temperature data should filter to this set so
    that lingering readings from previously-polled sensors do not leak into
    aggregations after the user narrows their selection.
    """
    raw = await get_setting("smartthings_device_ids")
    if not raw:
        return []
    return [d.strip() for d in raw.split(",") if d.strip()]


async def poll_smartthings_temps(session) -> int:
    """
    Fetch temperatures from all configured SmartThings sensors and
    write ``IndoorTempReading`` rows.  Returns the number of readings written.
    """
    from packages.core.models import IndoorTempReading
    from packages.poller.smartthings_oauth import get_valid_access_token

    access_token = await get_valid_access_token()
    if not access_token:
        return 0

    client = SmartThingsClient(access_token)

    # Keep labels and room identifiers with readings even when the user has
    # explicitly selected a subset.  Discovery is cached for an hour, so this
    # does not add a request to normal polling.
    selected_ids = await get_selected_device_ids()
    try:
        devices = await _get_cached_devices(client)
    except SmartThingsError as exc:
        # A configured sensor set remains pollable if discovery is temporarily
        # unavailable; room labels will be filled again on the next refresh.
        if not selected_ids:
            raise
        logger.warning("smartthings_discovery_metadata_unavailable", error=str(exc))
        devices = []
    device_meta = {device["device_id"]: device for device in devices}
    device_ids = selected_ids or list(device_meta)

    if not device_ids:
        return 0

    readings = await client.get_temperatures_batch(device_ids)
    now = dt.datetime.now(dt.timezone.utc)
    stale_threshold = await get_stale_reading_threshold()
    stale_after_minutes = round(stale_threshold.total_seconds() / 60)

    count = 0
    for r in readings:
        is_stale = False
        device_ts: dt.datetime | None = None

        # Parse the device-reported timestamp and check staleness. Poll time is
        # stored separately, so an API outage is still detected independently.
        if r.get("timestamp"):
            try:
                device_ts = dt.datetime.fromisoformat(r["timestamp"].replace("+0000", "+00:00"))
                if device_ts.tzinfo is None:
                    device_ts = device_ts.replace(tzinfo=dt.timezone.utc)
                else:
                    device_ts = device_ts.astimezone(dt.timezone.utc)
                age_minutes = max(0, round((now - device_ts).total_seconds() / 60))
                is_stale = (now - device_ts) > stale_threshold
                _record_staleness_transition(
                    r["device_id"],
                    is_stale=is_stale,
                    age_minutes=age_minutes,
                    threshold_minutes=stale_after_minutes,
                )
            except (AttributeError, ValueError, TypeError):
                is_stale = True
                _record_staleness_transition(
                    r["device_id"],
                    is_stale=True,
                    age_minutes=None,
                    threshold_minutes=stale_after_minutes,
                )
        else:
            is_stale = True
            _record_staleness_transition(
                r["device_id"],
                is_stale=True,
                age_minutes=None,
                threshold_minutes=stale_after_minutes,
            )

        session.add(
            IndoorTempReading(
                timestamp=now,
                device_id=r["device_id"],
                device_label=(device_meta.get(r["device_id"], {}).get("label") or None),
                room=(device_meta.get(r["device_id"], {}).get("room_id") or None),
                temperature=r["value"],
                device_timestamp=device_ts,
                is_stale=is_stale,
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
    """Access token is invalid or missing required scopes."""


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
            wait = BASE_BACKOFF_SECONDS * (2**attempt)
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
            wait = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2**attempt)
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
        raise SmartThingsAuthError("Invalid or expired SmartThings access token")
    if resp.status_code == 403:
        raise SmartThingsAuthError("SmartThings token missing required scopes (need r:devices:*)")
    if resp.status_code == 429:
        raise SmartThingsRateLimited("SmartThings rate limit exceeded")
    resp.raise_for_status()


def _to_celsius(value: float, unit: str) -> float:
    if unit.upper() in ("F", "FAHRENHEIT"):
        return (value - 32.0) * 5.0 / 9.0
    return value

"""Fail-closed Healthchecks.io reporting heartbeat with pinned outbound DNS."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp
import httpx
import redis.asyncio as redis
import structlog
from aiohttp.abc import AbstractResolver
from sqlalchemy import select

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.device_data_quality import get_device_data_quality
from packages.core.models import ServiceHeartbeatRecord
from packages.core.settings_service import get_all_settings

logger = structlog.get_logger(__name__)
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
_ATTEMPT_DEADLINE_SECONDS = 15
_NETWORK_TIMEOUT_SECONDS = 5
_DNS_TIMEOUT_SECONDS = 5
_SERVICE_MAX_AGE_SECONDS = 180


@dataclass(frozen=True)
class HeartbeatEndpoint:
    url: str
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] = ()


class HeartbeatValidationError(ValueError):
    """Validation failure whose reason is safe to log and never includes the URL."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _normalized_host(host: str) -> str:
    try:
        if host.endswith("."):
            raise ValueError
        normalized = host.encode("idna").decode("ascii").lower()
        labels = normalized.split(".")
        if (
            not normalized
            or len(normalized) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
                for label in labels
            )
        ):
            raise ValueError
        return normalized
    except (UnicodeError, ValueError) as exc:
        raise HeartbeatValidationError("invalid_authority") from exc


def _parse_authority(authority: str) -> tuple[str, int]:
    if not authority or any(char in authority for char in "@/%\\?#"):
        raise HeartbeatValidationError("invalid_allowlist")
    try:
        parsed = urlsplit("//" + authority)
        if parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
            raise ValueError
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
        if host is None or not 1 <= port <= 65535:
            raise ValueError
        if ":" in authority and authority.rsplit(":", 1)[1] != str(port):
            raise ValueError
    except ValueError as exc:
        raise HeartbeatValidationError("invalid_allowlist") from exc
    normalized = _normalized_host(host)
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise HeartbeatValidationError("invalid_allowlist")
    if ":" in host and not authority.startswith("["):
        raise HeartbeatValidationError("invalid_allowlist")
    return normalized, port


def _allowed_authorities(value: str) -> set[tuple[str, int]]:
    try:
        values = {_parse_authority(item.strip()) for item in value.split(",") if item.strip()}
    except (AttributeError, HeartbeatValidationError) as exc:
        raise HeartbeatValidationError("invalid_allowlist") from exc
    if not values:
        raise HeartbeatValidationError("invalid_allowlist")
    return values


def validate_heartbeat_url_structure(
    url: str, *, allowed_authorities: str | None = None
) -> HeartbeatEndpoint:
    """Enforce the exact HTTPS host[:port] allowlist without resolving DNS."""
    allowlist = (
        settings.external_heartbeat_allowed_authorities
        if allowed_authorities is None
        else allowed_authorities
    )
    if (
        not isinstance(url, str)
        or not url
        or url != url.strip()
        or any(ord(char) < 0x20 or char == "\\" for char in url)
    ):
        raise HeartbeatValidationError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.fragment
            or "#" in url
            or not parsed.path.startswith("/")
            or parsed.path == "/"
            or "%" in parsed.netloc
        ):
            raise ValueError
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
        if host is None or not 1 <= port <= 65535:
            raise ValueError
        if ":" in parsed.netloc and parsed.netloc.rsplit(":", 1)[1] != str(port):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise HeartbeatValidationError("invalid_url") from exc
    normalized = _normalized_host(host)
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        if all(char in "0123456789." for char in normalized):
            raise HeartbeatValidationError("ip_literal")
    else:
        raise HeartbeatValidationError("ip_literal")
    if ":" in host and not parsed.netloc.startswith("["):
        raise HeartbeatValidationError("invalid_url")
    if (normalized, port) not in _allowed_authorities(allowlist):
        raise HeartbeatValidationError("authority_not_allowed")
    return HeartbeatEndpoint(url=url, host=normalized, port=port)


async def _resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[..., Awaitable[list[tuple]]] | None = None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        lookup = resolver or asyncio.get_running_loop().getaddrinfo
        infos = await asyncio.wait_for(
            lookup(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HeartbeatValidationError("dns_failed") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        for info in infos:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                raise HeartbeatValidationError("unsafe_address")
            if not address.is_global or address.is_multicast or address.is_unspecified:
                raise HeartbeatValidationError("unsafe_address")
            addresses.add(address)
    except HeartbeatValidationError:
        raise
    except (ValueError, IndexError, TypeError) as exc:
        raise HeartbeatValidationError("dns_failed") from exc
    if not addresses:
        raise HeartbeatValidationError("dns_empty")
    return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


async def resolve_heartbeat_url(
    url: str, *, allowed_authorities: str | None = None
) -> HeartbeatEndpoint:
    endpoint = validate_heartbeat_url_structure(url, allowed_authorities=allowed_authorities)
    addresses = await _resolve_public_addresses(endpoint.host, endpoint.port)
    return HeartbeatEndpoint(endpoint.url, endpoint.host, endpoint.port, addresses)


async def validate_heartbeat_url(url: str) -> None:
    """Save-time structural and DNS validation with a sanitized API error."""
    try:
        await resolve_heartbeat_url(url)
    except HeartbeatValidationError:
        raise ValueError("external_heartbeat_url is not allowed") from None


class _PinnedResolver(AbstractResolver):
    def __init__(self, endpoint: HeartbeatEndpoint):
        self._endpoint = endpoint

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        if _normalized_host(host) != self._endpoint.host or port != self._endpoint.port:
            raise OSError("heartbeat destination mismatch")
        return [
            {
                "hostname": host,
                "host": str(address),
                "port": port,
                "family": socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in self._endpoint.addresses
        ]

    async def close(self) -> None:
        return None


async def _send_http_heartbeat(endpoint: HeartbeatEndpoint) -> int:
    """Send once through a DNS-pinned resolver, preserving HTTPS host/SNI checks."""
    resolver = _PinnedResolver(endpoint)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        ssl=True,
        family=socket.AF_UNSPEC,
    )
    timeout = aiohttp.ClientTimeout(
        total=_NETWORK_TIMEOUT_SECONDS,
        connect=_NETWORK_TIMEOUT_SECONDS,
        sock_connect=_NETWORK_TIMEOUT_SECONDS,
        sock_read=_NETWORK_TIMEOUT_SECONDS,
    )
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False
        ) as client:
            async with client.get(endpoint.url, allow_redirects=False) as response:
                return response.status
    finally:
        await resolver.close()


async def _probe_redis() -> bool:
    client = redis.from_url(
        settings.redis_url,
        socket_connect_timeout=_NETWORK_TIMEOUT_SECONDS,
        socket_timeout=_NETWORK_TIMEOUT_SECONDS,
    )
    try:
        response = await asyncio.wait_for(client.ping(), timeout=_NETWORK_TIMEOUT_SECONDS)
        return response is True or response in ("PONG", b"PONG")
    finally:
        await client.aclose()


async def _probe_internal_api() -> bool:
    url = settings.external_heartbeat_api_url.rstrip("/") + "/health"
    timeout = httpx.Timeout(_NETWORK_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.get(url)
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("db") == "connected"
    )


async def _read_service_heartbeats() -> dict[str, dt.datetime | None]:
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            select(ServiceHeartbeatRecord.service, ServiceHeartbeatRecord.updated_at).where(
                ServiceHeartbeatRecord.service.in_(("poller", "optimizer"))
            )
        )
        return {str(service): timestamp for service, timestamp in rows.all()}


def _fresh_heartbeat(timestamp: dt.datetime | None, now: dt.datetime) -> bool:
    if not isinstance(timestamp, dt.datetime):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    delta = (now - timestamp).total_seconds()
    return 0 <= delta <= _SERVICE_MAX_AGE_SECONDS


async def evaluate_reporting_conditions(settings_values: dict[str, str]) -> bool:
    """Return true only when all persisted reporting signals are fresh and healthy."""
    del settings_values
    now = dt.datetime.now(dt.timezone.utc)
    heartbeats = await _read_service_heartbeats()
    if not all(
        _fresh_heartbeat(heartbeats.get(service), now) for service in ("poller", "optimizer")
    ):
        return False
    if not await _probe_redis():
        return False
    if not await _probe_internal_api():
        return False
    quality = await get_device_data_quality(now=now)
    return isinstance(quality, dict) and quality.get("ready") is True


class ExternalHeartbeatRunner:
    """In-process due-time pacing for best-effort external reporting heartbeats."""

    def __init__(
        self,
        *,
        settings_reader: Callable[[], Awaitable[dict[str, str]]] = get_all_settings,
        condition_check: Callable[
            [dict[str, str]], Awaitable[bool]
        ] = evaluate_reporting_conditions,
        url_resolver: Callable[[str], Awaitable[HeartbeatEndpoint]] = resolve_heartbeat_url,
        sender: Callable[[HeartbeatEndpoint], Awaitable[int]] = _send_http_heartbeat,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_reader = settings_reader
        self._condition_check = condition_check
        self._url_resolver = url_resolver
        self._sender = sender
        self._monotonic = monotonic
        self._next_due: float | None = None
        self._interval_seconds = 60

    async def tick(self) -> bool:
        started = self._monotonic()
        if self._next_due is not None and started < self._next_due:
            return False
        self._next_due = started + self._interval_seconds
        try:
            values = await self._settings_reader()
            enabled = str(values.get("external_heartbeat_enabled", "false")).strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }
            interval = int(values.get("external_heartbeat_interval_seconds", "60"))
            if not 30 <= interval <= 300:
                raise ValueError("invalid interval")
            self._interval_seconds = interval
            self._next_due = started + interval
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("external_heartbeat_failed", reason="settings_unavailable")
            return False
        if not enabled:
            return False
        url = values.get("external_heartbeat_url", "")
        if not url:
            logger.warning("external_heartbeat_failed", reason="url_unconfigured")
            return False
        try:
            async with asyncio.timeout(_ATTEMPT_DEADLINE_SECONDS):
                if not await self._condition_check(values):
                    logger.warning("external_heartbeat_suppressed", reason="reporting_unhealthy")
                    return False
                endpoint = await self._url_resolver(url)
                status = await self._sender(endpoint)
                if status != 200:
                    logger.warning("external_heartbeat_failed", reason="http_status")
                    return False
            logger.info("external_heartbeat_succeeded")
            return True
        except asyncio.CancelledError:
            raise
        except HeartbeatValidationError as exc:
            logger.warning("external_heartbeat_failed", reason=exc.reason)
        except TimeoutError:
            logger.warning("external_heartbeat_failed", reason="attempt_timeout")
        except Exception:
            logger.warning("external_heartbeat_failed", reason="attempt_failed")
        return False

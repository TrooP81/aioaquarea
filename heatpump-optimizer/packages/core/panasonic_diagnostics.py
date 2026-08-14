"""Serializable Panasonic adaptor diagnostics shared between services."""

from __future__ import annotations

import datetime as dt
from typing import Any

_KNOWN_STATUSES = {"available", "unavailable", "backoff"}


def _as_utc(value: object) -> dt.datetime | None:
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def classify_panasonic_adapter_reason(reason: str | None) -> str | None:
    """Map provider text to a stable, non-sensitive diagnostic reason code."""

    if reason is None:
        return None
    normalized = reason.strip().lower()
    if normalized == "cloud_cached_status":
        return "cloud_cached_status"
    if "failed communication with adaptor" in normalized:
        return "adaptor_communication_failed"
    return "live_status_request_failed"


def build_panasonic_adapter_state(
    *,
    status: str,
    device_id: str | None,
    reason: str | None = None,
    consecutive_failures: int = 0,
    retry_after_seconds: int = 0,
    observed_at: dt.datetime | None = None,
) -> dict[str, object]:
    """Build the JSON-safe heartbeat payload written by the poller."""

    if status not in _KNOWN_STATUSES:
        raise ValueError(f"Unsupported Panasonic adaptor status: {status}")
    observed_at = _as_utc(observed_at) or dt.datetime.now(dt.timezone.utc)
    retry_after_seconds = _non_negative_int(retry_after_seconds)
    retry_at = (
        observed_at + dt.timedelta(seconds=retry_after_seconds) if retry_after_seconds else None
    )
    return {
        "status": status,
        "device_id": device_id,
        "reason": reason,
        "consecutive_failures": _non_negative_int(consecutive_failures),
        "retry_after_seconds": retry_after_seconds,
        "observed_at": observed_at.isoformat(),
        "retry_at": retry_at.isoformat() if retry_at else None,
    }


def project_panasonic_adapter_state(
    raw: Any,
    *,
    now: dt.datetime | None = None,
    stale_after_seconds: int = 900,
) -> dict[str, object]:
    """Normalize stored diagnostics and mark stale or malformed state safely."""

    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status")
    if status not in _KNOWN_STATUSES:
        status = "unknown"
    observed_at = _as_utc(raw.get("observed_at"))
    retry_at = _as_utc(raw.get("retry_at"))
    age_seconds = max(0, round((now - observed_at).total_seconds())) if observed_at else None
    future_skew_seconds = (
        (observed_at - now).total_seconds() if observed_at and observed_at > now else 0
    )
    state_fresh = (
        age_seconds is not None
        and age_seconds <= max(60, stale_after_seconds)
        and future_skew_seconds <= 60
    )
    device_id = raw.get("device_id")
    reason = raw.get("reason")
    return {
        "status": status,
        "state_fresh": state_fresh,
        "device_id": device_id if isinstance(device_id, str) else None,
        "reason": reason if isinstance(reason, str) else None,
        "consecutive_failures": _non_negative_int(raw.get("consecutive_failures")),
        "retry_after_seconds": _non_negative_int(raw.get("retry_after_seconds")),
        "observed_at": observed_at.isoformat() if observed_at else None,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "age_seconds": age_seconds,
        "stale_after_seconds": max(60, stale_after_seconds),
    }

"""Readiness of credentials and the latest persisted Panasonic device status."""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import nullcontext
from typing import Any

from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord
from packages.core.settings_service import get_setting


def _as_utc(value: dt.datetime | str | None) -> dt.datetime | None:
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


def observation_clock_issue(
    *,
    as_of: dt.datetime,
    timestamp: dt.datetime | str | None = None,
    captured_at: dt.datetime | str | None = None,
    observed_at: dt.datetime | str | None = None,
    retrieved_at: dt.datetime | str | None = None,
    max_age_seconds: float | None = None,
    evidence_after: dt.datetime | None = None,
    require_remote_clock: bool = False,
) -> str | None:
    as_of = _as_utc(as_of)
    values = (timestamp, captured_at, observed_at, retrieved_at)
    clocks = tuple(_as_utc(value) for value in values)
    if as_of is None or any(
        value is not None and clock is None for value, clock in zip(values, clocks)
    ):
        return "device_status_timing_unknown"
    timestamp, captured_at, observed_at, retrieved_at = clocks
    known = tuple(clock for clock in clocks if clock is not None)
    if any(clock > as_of for clock in known):
        return "device_status_future"
    local_finish = captured_at or timestamp
    if (observed_at is not None and retrieved_at is not None and observed_at > retrieved_at) or (
        local_finish is not None
        and any(clock is not None and clock > local_finish for clock in (observed_at, retrieved_at))
    ):
        return "device_status_clock_chronology"
    if max_age_seconds is not None and any(
        (as_of - clock).total_seconds() > max_age_seconds for clock in known
    ):
        return "device_status_stale"
    if require_remote_clock and observed_at is None and retrieved_at is None:
        return "device_status_timing_unknown"
    if evidence_after is not None:
        bound = _as_utc(evidence_after)
        if bound is None or bound > as_of:
            return "device_status_timing_unknown"
        if any(clock <= bound for clock in known):
            return "device_status_before_dispatch"
    return None


def _device_status_clock_values(status: Any) -> dict[str, Any] | None:
    timestamp = status if isinstance(status, dt.datetime) else getattr(status, "ts", None)
    provenance = getattr(status, "native_policy_provenance", None)
    if provenance is None:
        return {"timestamp": timestamp}
    if not isinstance(provenance, dict):
        return None
    timing = provenance.get("timing", {})
    if not isinstance(timing, dict):
        return None
    return {
        "timestamp": timestamp,
        "captured_at": provenance.get("captured_at"),
        "observed_at": timing.get("observed_at"),
        "retrieved_at": timing.get("retrieved_at"),
    }


def device_status_clock_issue(
    status: Any,
    *,
    as_of: dt.datetime,
    max_age_seconds: float | None = None,
    evidence_after: dt.datetime | None = None,
    require_remote_clock: bool = False,
) -> str | None:
    values = _device_status_clock_values(status)
    if values is None or _as_utc(values["timestamp"]) is None:
        return "device_status_timing_unknown"
    return observation_clock_issue(
        **values,
        as_of=as_of,
        max_age_seconds=max_age_seconds,
        evidence_after=evidence_after,
        require_remote_clock=require_remote_clock,
    )


_STATUS_UNSET = object()


async def get_device_data_quality(
    *, now: dt.datetime | None = None, session=None, values=None, status=_STATUS_UNSET
) -> dict[str, Any]:
    """Return explainable control readiness without exposing credentials."""

    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    keys = (
        "aquarea_username",
        "aquarea_password",
        "device_status_max_age_minutes",
        "poll_interval_seconds",
    )
    if values is None:
        if session is None:
            loaded = await asyncio.gather(*(get_setting(key) for key in keys))
        else:
            loaded = [await get_setting(key, session=session) for key in keys]
        values = dict(zip(keys, loaded, strict=True))
    if status is _STATUS_UNSET:
        async with get_session() if session is None else nullcontext(session) as borrowed:
            status = (
                await borrowed.execute(
                    select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
                )
            ).scalar_one_or_none()
    return device_data_quality_from_inputs(now=now, values=values, status=status)


def device_data_quality_from_inputs(*, now: dt.datetime, values: dict, status) -> dict[str, Any]:
    username, password, max_age_value, poll_interval_value = (
        values[key]
        for key in (
            "aquarea_username",
            "aquarea_password",
            "device_status_max_age_minutes",
            "poll_interval_seconds",
        )
    )
    credentials_configured = bool(username.strip() and password.strip())
    try:
        configured_minutes = int(max_age_value)
    except ValueError:
        configured_minutes = 15
    try:
        poll_interval_seconds = int(poll_interval_value)
    except ValueError:
        poll_interval_seconds = 300
    minutes = min(60, max(5, configured_minutes))
    threshold_seconds = max(minutes * 60, 3 * max(0, poll_interval_seconds))

    timestamp = _as_utc(status if isinstance(status, dt.datetime) else getattr(status, "ts", None))
    clock_issue = (
        device_status_clock_issue(status, as_of=now, max_age_seconds=threshold_seconds)
        if status is not None
        else None
    )
    values = _device_status_clock_values(status)
    known_clocks = tuple(_as_utc(value) for value in (values or {}).values() if value is not None)
    age_seconds = (
        round((now - min(known_clocks)).total_seconds())
        if known_clocks and clock_issue in (None, "device_status_stale")
        else None
    )
    reasons: list[str] = []
    if not credentials_configured:
        reasons.append("credentials_missing")
    if timestamp is None:
        reasons.append("device_status_missing")
    elif clock_issue:
        reasons.append(clock_issue)
    return {
        "timestamp": timestamp,
        "age_seconds": age_seconds,
        "threshold_seconds": threshold_seconds,
        "credentials_configured": credentials_configured,
        "ready": not reasons,
        "reasons": reasons,
    }

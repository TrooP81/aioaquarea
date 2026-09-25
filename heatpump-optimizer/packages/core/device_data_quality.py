"""Readiness of credentials and the latest persisted Panasonic device status."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord
from packages.core.settings_service import get_setting


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


async def get_device_data_quality(*, now: dt.datetime | None = None) -> dict[str, Any]:
    """Return explainable control readiness without exposing credentials."""

    now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
    username, password, max_age_value, poll_interval_value = await asyncio.gather(
        get_setting("aquarea_username"),
        get_setting("aquarea_password"),
        get_setting("device_status_max_age_minutes"),
        get_setting("poll_interval_seconds"),
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

    async with get_session() as session:
        timestamp = (
            await session.execute(
                select(DeviceStatusRecord.ts).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()
    timestamp = _as_utc(timestamp)
    age_seconds = max(0, round((now - timestamp).total_seconds())) if timestamp else None
    reasons: list[str] = []
    if not credentials_configured:
        reasons.append("credentials_missing")
    if timestamp is None:
        reasons.append("device_status_missing")
    elif age_seconds is not None and age_seconds > threshold_seconds:
        reasons.append("device_status_stale")
    return {
        "timestamp": timestamp,
        "age_seconds": age_seconds,
        "threshold_seconds": threshold_seconds,
        "credentials_configured": credentials_configured,
        "ready": not reasons,
        "reasons": reasons,
    }

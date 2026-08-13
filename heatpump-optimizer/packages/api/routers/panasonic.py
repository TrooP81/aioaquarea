"""Read-only Panasonic integration diagnostics."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord, ServiceHeartbeatRecord
from packages.core.panasonic_capabilities import build_panasonic_capabilities
from packages.core.settings_service import get_int_setting

router = APIRouter()


@router.get("/api/panasonic/capabilities")
async def panasonic_capabilities():
    """Return observed capabilities without creating a cloud connection."""

    async with get_session() as session:
        latest_status = (
            await session.execute(
                select(DeviceStatusRecord).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()
        poller_heartbeat = await session.get(ServiceHeartbeatRecord, "poller")

    return build_panasonic_capabilities(
        latest_status=latest_status,
        poller_heartbeat=poller_heartbeat,
        poll_interval_seconds=await get_int_setting("poll_interval_seconds"),
        now=dt.datetime.now(dt.timezone.utc),
    )

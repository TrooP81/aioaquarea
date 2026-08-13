"""Small, database-backed health signals for long-running services."""

from __future__ import annotations

import datetime as dt
import json

from packages.core.database import get_session
from packages.core.models import ServiceHeartbeatRecord


async def record_service_heartbeat(service: str, **details: object) -> None:
    """Record a successful service loop without making failures fatal."""

    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        row = await session.get(ServiceHeartbeatRecord, service)
        if row is None:
            row = ServiceHeartbeatRecord(service=service, updated_at=now)
            session.add(row)
        row.updated_at = now
        row.details_json = json.dumps(details, default=str) if details else None

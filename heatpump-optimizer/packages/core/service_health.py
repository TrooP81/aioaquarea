"""Small, database-backed health signals for long-running services."""

from __future__ import annotations

import datetime as dt
import json

from packages.core.database import get_session
from packages.core.models import ServiceHeartbeatRecord


def merge_service_heartbeat_details(
    existing_json: str | None, updates: dict[str, object]
) -> str | None:
    """Merge durable service diagnostics without trusting malformed old JSON."""

    existing: dict[str, object] = {}
    if existing_json:
        try:
            decoded = json.loads(existing_json)
            if isinstance(decoded, dict):
                existing = decoded
        except (TypeError, ValueError):
            pass
    existing.update(updates)
    return json.dumps(existing, default=str, sort_keys=True) if existing else None


def service_heartbeat_details(row: ServiceHeartbeatRecord | None) -> dict[str, object]:
    """Read heartbeat details defensively for API projections and alerts."""

    if row is None or not row.details_json:
        return {}
    try:
        decoded = json.loads(row.details_json)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def record_service_heartbeat(service: str, **details: object) -> None:
    """Record a successful service loop without making failures fatal."""

    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        row = await session.get(ServiceHeartbeatRecord, service)
        if row is None:
            row = ServiceHeartbeatRecord(service=service, updated_at=now)
            session.add(row)
        row.updated_at = now
        row.details_json = merge_service_heartbeat_details(row.details_json, details)

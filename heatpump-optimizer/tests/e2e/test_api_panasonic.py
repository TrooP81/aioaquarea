"""Panasonic read-only API diagnostics."""

import datetime as dt
import json

import pytest

from packages.core.models import ServiceHeartbeatRecord


@pytest.mark.asyncio(loop_scope="session")
async def test_capabilities_use_database_observation_without_cloud_probe(
    client, db_session, seed_device_status
):
    now = dt.datetime.now(dt.timezone.utc)
    db_session.add(
        ServiceHeartbeatRecord(
            service="poller",
            updated_at=now,
            details_json=json.dumps(
                {
                    "panasonic_adapter": {
                        "status": "available",
                        "device_id": seed_device_status.device_id,
                        "consecutive_failures": 0,
                        "retry_after_seconds": 0,
                        "observed_at": now.isoformat(),
                        "retry_at": None,
                    }
                }
            ),
        )
    )
    await db_session.commit()

    response = await client.get("/api/panasonic/capabilities")

    assert response.status_code == 200
    data = response.json()
    assert data["api"]["live_probe_performed"] is False
    assert data["api"]["write_preflight_required"] is True
    assert data["availability"]["commands_allowed"] is True
    assert data["adapter"]["status"] == "available"
    assert data["adapter"]["state_fresh"] is True
    assert data["device"]["id"] == seed_device_status.device_id
    assert data["device"]["has_tank"] is True
    assert data["commands"]["force_dhw"]["available"] is True

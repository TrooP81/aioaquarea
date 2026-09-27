from __future__ import annotations

import datetime as dt
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models import AuditLogRecord, PlanActionRecord, SettingRecord
from packages.core.operational_alerts import get_operational_alerts
from packages.optimizer.executor_core import SAFETY_ACTION_STUCK_AFTER


async def _seed_revert(db_session, *, now: dt.datetime, scheduled_ts: dt.datetime, claimed_at=None):
    source = PlanActionRecord(
        plan_id=1,
        scheduled_ts=scheduled_ts,
        action_type="force_dhw_on",
        payload_json="{}",
        device_id="test-device",
        status="executed",
    )
    db_session.add(source)
    await db_session.flush()
    restore = PlanActionRecord(
        plan_id=1,
        reverts_action_id=source.id,
        scheduled_ts=scheduled_ts,
        action_type="force_dhw_off",
        payload_json="{}",
        device_id="test-device",
        status="pending",
        safety_attempt_count=0,
        safety_claimed_at=claimed_at,
    )
    db_session.add(restore)
    await db_session.commit()
    return restore


@pytest.mark.asyncio(loop_scope="session")
async def test_learning_mode_api_rejects_then_force_enables_and_audits(
    client: AsyncClient, db_session
):
    now = dt.datetime.now(dt.timezone.utc)
    await _seed_revert(
        db_session,
        now=now,
        scheduled_ts=now - dt.timedelta(minutes=5),
    )

    rejected = await client.post("/api/learning-mode", json={"enabled": True})
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "unresolved_safety_reverts"
    assert (
        await db_session.scalar(
            select(SettingRecord.value).where(SettingRecord.key == "learning_mode_enabled")
        )
        is None
    )

    forced = await client.post("/api/learning-mode?force=true", json={"enabled": True})
    assert forced.status_code == 200
    assert forced.json()["enabled"] is True
    assert (
        await db_session.scalar(
            select(SettingRecord.value).where(SettingRecord.key == "learning_mode_enabled")
        )
        == "true"
    )
    audit = await db_session.scalar(
        select(AuditLogRecord).where(AuditLogRecord.action == "set_learning_mode")
    )
    assert audit is not None
    payload = json.loads(audit.payload_json)
    assert payload["force"] is True
    assert payload["obligations"]["count"] == 1


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("age_seconds", "claimed_at", "expected"),
    [
        (119, None, False),
        (121, None, True),
        (121, dt.timedelta(seconds=SAFETY_ACTION_STUCK_AFTER.total_seconds() + 1), True),
    ],
    ids=["below-120-seconds", "zero-attempt-unclaimed", "stale-claimed"],
)
async def test_pending_safety_alert_uses_due_age_and_stale_claim_boundary(
    db_session, age_seconds, claimed_at, expected
):
    now = dt.datetime.now(dt.timezone.utc)
    claim_value = now - claimed_at if isinstance(claimed_at, dt.timedelta) else claimed_at
    await _seed_revert(
        db_session,
        now=now,
        scheduled_ts=now - dt.timedelta(seconds=age_seconds),
        claimed_at=claim_value,
    )

    result = await get_operational_alerts(now=now)
    pending_alerts = [
        alert for alert in result["alerts"] if alert["id"] == "safety_revert_pending_unclaimed"
    ]
    assert bool(pending_alerts) is expected

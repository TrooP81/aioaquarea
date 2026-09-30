from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from packages.core.database import get_session
from packages.core.models import ExecutorVerificationReadRecord, PlanActionRecord
from packages.core.operational_alerts import get_operational_alerts
from packages.optimizer.executor_core import PlanExecutor


async def _seed_actions(db_session, count: int) -> list[PlanActionRecord]:
    now = dt.datetime.now(dt.timezone.utc)
    actions = [
        PlanActionRecord(
            plan_id=1,
            scheduled_ts=now,
            action_type="force_dhw_on",
            payload_json="{}",
            device_id="test-device",
            status="pending",
        )
        for _ in range(count)
    ]
    db_session.add_all(actions)
    await db_session.commit()
    return actions


async def _reserve(action: PlanActionRecord, lane: str) -> bool:
    executor = PlanExecutor(AsyncMock(), session_factory=get_session)
    return await executor._reserve_verification_read(
        SimpleNamespace(id=action.id, device_id=action.device_id),
        lane,
        "initial" if lane == "ordinary" else "safety",
        15,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_admission_never_exceeds_total_or_ordinary_caps(db_session):
    actions = await _seed_actions(db_session, 16)

    results = await asyncio.gather(
        *(
            _reserve(action, "ordinary" if index % 2 == 0 else "safety")
            for index, action in enumerate(actions)
        )
    )

    admitted = [action for action, admitted in zip(actions, results) if admitted]
    ordinary_count = sum(index % 2 == 0 for index, admitted in enumerate(results) if admitted)
    assert len(admitted) <= 5
    assert ordinary_count <= 3

    rows = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
    assert len(rows) == len(admitted)
    assert sum(row.lane == "ordinary" for row in rows) <= 3


@pytest.mark.asyncio(loop_scope="session")
async def test_safety_gets_reserved_capacity_after_ordinary_lane_is_exhausted(db_session):
    actions = await _seed_actions(db_session, 8)

    ordinary_results = [await _reserve(action, "ordinary") for action in actions[:4]]
    safety_results = await asyncio.gather(*(_reserve(action, "safety") for action in actions[4:]))

    assert sum(ordinary_results) == 3
    assert sum(safety_results) == 2

    rows = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
    assert len(rows) == 5
    assert sum(row.lane == "ordinary" for row in rows) == 3
    assert sum(row.lane == "safety" for row in rows) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_safety_borrows_unused_ordinary_capacity_up_to_total_limit(db_session):
    actions = await _seed_actions(db_session, 8)

    assert await _reserve(actions[0], "ordinary")
    safety_results = await asyncio.gather(*(_reserve(action, "safety") for action in actions[1:]))

    assert sum(safety_results) == 4
    rows = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
    assert len(rows) == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_admission_uses_database_time_and_cleans_rows_older_than_seven_days(db_session):
    actions = await _seed_actions(db_session, 3)
    inserted = await db_session.execute(
        text(
            "INSERT INTO executor_verification_reads "
            "(reserved_at, device_id, lane, action_id, phase, checkpoint_seconds) "
            "VALUES "
            "(clock_timestamp() - interval '1 hour', 'test-device', 'ordinary', :boundary, 'initial', 15), "
            "(clock_timestamp() - interval '59 minutes 59 seconds', 'test-device', 'ordinary', :recent, 'initial', 15), "
            "(clock_timestamp() - interval '7 days 1 second', 'test-device', 'ordinary', :retention, 'initial', 15) "
            "RETURNING id"
        ),
        {"boundary": actions[0].id, "recent": actions[1].id, "retention": actions[2].id},
    )
    boundary_id, recent_id, retention_id = [row.id for row in inserted]
    await db_session.commit()

    active_ids = set(
        (
            await db_session.execute(
                text(
                    "SELECT id FROM executor_verification_reads "
                    "WHERE reserved_at > clock_timestamp() - interval '1 hour'"
                )
            )
        ).scalars()
    )
    assert boundary_id not in active_ids
    assert recent_id in active_ids

    assert await _reserve(actions[2], "ordinary")

    rows = (await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all()
    row_ids = {row.id for row in rows}
    assert boundary_id in row_ids
    assert retention_id not in row_ids
    assert recent_id in row_ids


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_verification_keeps_its_committed_reservation(db_session):
    action = (await _seed_actions(db_session, 1))[0]
    wrapper = AsyncMock()
    wrapper.refresh_device = AsyncMock(side_effect=RuntimeError("Panasonic unavailable"))
    executor = PlanExecutor(
        wrapper, session_factory=get_session, sleep=lambda _delay: asyncio.sleep(0)
    )
    dispatched_at = dt.datetime.now(dt.timezone.utc)

    await executor._verify_with_retry(
        action,
        {},
        {"force_dhw": "ON"},
        dispatched_at=dispatched_at,
    )

    reservation = await db_session.scalar(
        select(ExecutorVerificationReadRecord).where(
            ExecutorVerificationReadRecord.action_id == action.id
        )
    )
    assert reservation is not None
    assert (
        await db_session.scalar(
            select(PlanActionRecord.status).where(PlanActionRecord.id == action.id)
        )
        == "executed_unverified"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_operational_alerts_include_failed_but_exclude_executed_unverified(db_session):
    now = dt.datetime.now(dt.timezone.utc)
    failed = PlanActionRecord(
        plan_id=1,
        scheduled_ts=now - dt.timedelta(minutes=5),
        action_type="force_dhw_on",
        payload_json="{}",
        device_id="test-device",
        status="failed",
        result_json='{"reason":"verification_mismatch_after_redispatch"}',
    )
    unverified = PlanActionRecord(
        plan_id=1,
        scheduled_ts=now - dt.timedelta(minutes=4),
        action_type="set_tank_temp",
        payload_json="{}",
        device_id="test-device",
        status="executed_unverified",
        result_json='{"reason":"verification_evidence_unavailable"}',
    )
    db_session.add_all([failed, unverified])
    await db_session.commit()

    result = await get_operational_alerts(now=now)
    alerts = {alert["id"]: alert for alert in result["alerts"]}

    assert "plan_actions_failed" in alerts
    assert alerts["plan_actions_failed"]["action_id"] == failed.id
    assert unverified.id != alerts["plan_actions_failed"]["action_id"]

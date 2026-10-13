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
from packages.optimizer.executor_core import PlanExecutor, _SafetyAuthorityLost


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


# At most this many contenders (each holding an owned safety attempt connection plus one
# transient session) run at once, so the real 5+10 pool is never exhausted.
MAX_CONCURRENT_CONTENDERS = 5


async def _reserve(action: PlanActionRecord, lane: str) -> bool:
    executor = PlanExecutor(AsyncMock(), session_factory=get_session)
    return await executor._reserve_verification_read(
        SimpleNamespace(id=action.id, device_id=action.device_id),
        lane,
        "initial" if lane == "ordinary" else "safety",
        15,
    )


async def _seed_restores(count: int, label: str) -> list[int]:
    """Seed settled source/restore pairs, one per distinct device, oldest first."""
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        sources = [
            PlanActionRecord(
                plan_id=1,
                scheduled_ts=now - dt.timedelta(minutes=10),
                action_type="force_dhw_on",
                payload_json="{}",
                device_id=f"quota-{label}-{index}",
                status="executed",
            )
            for index in range(count)
        ]
        session.add_all(sources)
        await session.flush()
        restores = [
            PlanActionRecord(
                plan_id=1,
                reverts_action_id=source.id,
                scheduled_ts=now - dt.timedelta(minutes=5) + dt.timedelta(seconds=index),
                action_type="force_dhw_off",
                payload_json="{}",
                device_id=source.device_id,
                status="pending",
            )
            for index, source in enumerate(sources)
        ]
        session.add_all(restores)
        await session.flush()
        return [restore.id for restore in restores]


async def _release_claim(executor: PlanExecutor, action: PlanActionRecord) -> None:
    attempt = executor._attempt_for(action)
    try:
        assert await attempt.drain()
        await executor._requeue_safety_action(action, "test_quota_cleanup")
    finally:
        assert await attempt.release()
        executor._safety_attempts.pop(attempt.token, None)


class _Contenders:
    """Run whole claim/reserve/requeue/drain/release lifetimes with bounded concurrency."""

    def __init__(self, restore_ids: list[int]):
        self.restore_ids = set(restore_ids)
        self.slots = asyncio.Semaphore(MAX_CONCURRENT_CONTENDERS)
        self.claim_lock = asyncio.Lock()
        self.active = 0
        self.peak = 0
        self.claimed: list[int] = []

    async def _enter(self):
        await self.slots.acquire()
        self.active += 1
        self.peak = max(self.peak, self.active)

    def _exit(self):
        self.active -= 1
        self.slots.release()

    async def ordinary(self, action: PlanActionRecord) -> bool:
        await self._enter()
        try:
            return await _reserve(action, "ordinary")
        finally:
            self._exit()

    async def safety(self) -> bool:
        await self._enter()
        try:
            executor = PlanExecutor(AsyncMock(), session_factory=get_session)
            # Only the claim step is ordered: the executor claims the OLDEST eligible
            # restore, so the reservation targets whichever restore was actually claimed.
            async with self.claim_lock:
                action = await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
            assert action is not None
            assert action.id in self.restore_ids and action.id not in self.claimed
            assert action.status == "executing" and action.safety_claim_token is not None
            self.claimed.append(action.id)
            try:
                return await executor._reserve_verification_read(action, "safety", "safety", 15)
            finally:
                await _release_claim(executor, action)
        finally:
            self._exit()


async def _reservation_rows(db_session) -> list[ExecutorVerificationReadRecord]:
    db_session.expire_all()
    return list((await db_session.execute(select(ExecutorVerificationReadRecord))).scalars().all())


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_admission_never_exceeds_total_or_ordinary_caps(db_session):
    actions = await _seed_actions(db_session, 8)
    restore_ids = await _seed_restores(8, "mixed")
    contenders = _Contenders(restore_ids)

    results = await asyncio.gather(
        *(
            contender
            for pair in zip(
                (contenders.ordinary(action) for action in actions),
                (contenders.safety() for _ in restore_ids),
            )
            for contender in pair
        )
    )

    assert len(results) == 16
    assert contenders.peak <= MAX_CONCURRENT_CONTENDERS
    assert sorted(contenders.claimed) == sorted(restore_ids)
    rows = await _reservation_rows(db_session)
    ordinary = [row for row in rows if row.lane == "ordinary"]
    safety = [row for row in rows if row.lane == "safety"]
    assert sum(results) == len(rows) == 5
    assert len(ordinary) <= 3
    assert {row.action_id for row in ordinary} <= {action.id for action in actions}
    assert {row.action_id for row in safety} <= set(restore_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_safety_gets_reserved_capacity_after_ordinary_lane_is_exhausted(db_session):
    actions = await _seed_actions(db_session, 4)
    restore_ids = await _seed_restores(4, "reserved")
    contenders = _Contenders(restore_ids)

    ordinary_results = [await _reserve(action, "ordinary") for action in actions]
    safety_results = await asyncio.gather(*(contenders.safety() for _ in restore_ids))

    assert ordinary_results == [True, True, True, False]
    assert sum(safety_results) == 2
    assert contenders.peak <= MAX_CONCURRENT_CONTENDERS
    assert sorted(contenders.claimed) == sorted(restore_ids)
    rows = await _reservation_rows(db_session)
    assert len(rows) == 5
    assert sum(row.lane == "ordinary" for row in rows) == 3
    assert sum(row.lane == "safety" for row in rows) == 2
    assert {row.action_id for row in rows if row.lane == "safety"} <= set(restore_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_safety_borrows_unused_ordinary_capacity_up_to_total_limit(db_session):
    actions = await _seed_actions(db_session, 1)
    restore_ids = await _seed_restores(7, "borrow")
    contenders = _Contenders(restore_ids)

    assert await _reserve(actions[0], "ordinary")
    safety_results = await asyncio.gather(*(contenders.safety() for _ in restore_ids))

    assert sum(safety_results) == 4
    assert contenders.peak <= MAX_CONCURRENT_CONTENDERS
    assert sorted(contenders.claimed) == sorted(restore_ids)
    rows = await _reservation_rows(db_session)
    assert len(rows) == 5
    assert sum(row.lane == "safety" for row in rows) == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_stale_or_unowned_safety_claim_writes_no_reservation(db_session):
    restore_ids = await _seed_restores(1, "unowned")
    owner = PlanExecutor(AsyncMock(), session_factory=get_session)
    action = await owner._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
    assert action is not None and action.id == restore_ids[0]
    claimed_result = action.result_json
    try:
        intruder = PlanExecutor(AsyncMock(), session_factory=get_session)
        with pytest.raises(_SafetyAuthorityLost):
            await intruder._reserve_verification_read(action, "safety", "safety", 15)
        with pytest.raises(_SafetyAuthorityLost):
            await owner._reserve_verification_read(
                SimpleNamespace(
                    id=action.id,
                    device_id=action.device_id,
                    reverts_action_id=action.reverts_action_id,
                ),
                "safety",
                "safety",
                15,
            )
        with pytest.raises(_SafetyAuthorityLost):
            await owner._reserve_verification_read(action, "ordinary", "initial", 15)
        assert await _reservation_rows(db_session) == []
        db_session.expire_all()
        stored = await db_session.get(PlanActionRecord, action.id)
        assert stored.result_json == claimed_result
    finally:
        await _release_claim(owner, action)

    with pytest.raises(_SafetyAuthorityLost):
        await owner._reserve_verification_read(action, "safety", "safety", 15)
    assert await _reservation_rows(db_session) == []


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

"""Database-backed tests for atomic optimizer plan replacement."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from packages.core.database import get_session
from packages.core.models import PlanActionRecord, PlanRecord
from packages.core.plan_lifecycle import activate_plan


def _plan(now: dt.datetime, version: str) -> PlanRecord:
    return PlanRecord(
        horizon_start=now,
        horizon_end=now + dt.timedelta(hours=24),
        plan_json="{}",
        optimizer_version=version,
        price_currency="EUR",
        price_source="test",
        status="active",
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_replacing_plan_cancels_only_pending_actions() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    first = _plan(now, "first")
    async with get_session() as session:
        await activate_plan(session, first)
        session.add_all(
            [
                PlanActionRecord(
                    plan_id=first.id,
                    scheduled_ts=now + dt.timedelta(hours=1),
                    action_type="quiet_mode_on",
                    payload_json="{}",
                    status="pending",
                ),
                PlanActionRecord(
                    plan_id=first.id,
                    scheduled_ts=now - dt.timedelta(hours=1),
                    action_type="quiet_mode_off",
                    payload_json="{}",
                    status="executed",
                ),
            ]
        )

    second = _plan(now, "second")
    async with get_session() as session:
        await activate_plan(session, second)

    async with get_session() as session:
        stored_first = await session.get(PlanRecord, first.id)
        actions = (
            (
                await session.execute(
                    select(PlanActionRecord)
                    .where(PlanActionRecord.plan_id == first.id)
                    .order_by(PlanActionRecord.id)
                )
            )
            .scalars()
            .all()
        )

    assert stored_first is not None
    assert stored_first.status == "superseded"
    assert stored_first.superseded_by_plan_id == second.id
    assert [action.status for action in actions] == ["cancelled", "executed"]


@pytest.mark.asyncio(loop_scope="session")
async def test_database_rejects_a_second_active_plan() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        session.add(_plan(now, "first"))

    with pytest.raises(IntegrityError):
        async with get_session() as session:
            session.add(_plan(now, "conflicting"))

    async with get_session() as session:
        active_count = await session.scalar(
            select(func.count()).select_from(PlanRecord).where(PlanRecord.status == "active")
        )

    assert active_count == 1

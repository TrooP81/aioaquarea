"""Atomic lifecycle helpers for executable optimizer plans."""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import PlanActionRecord, PlanRecord

ACTIVE_PLAN_STATUS = "active"
SUPERSEDED_PLAN_STATUS = "superseded"
CANCELLED_ACTION_STATUS = "cancelled"


async def activate_plan(
    session: AsyncSession,
    plan: PlanRecord,
    *,
    reason: str = "replaced_by_new_optimization",
) -> None:
    """Make *plan* the sole active plan and cancel older pending actions.

    The active-plan rows are locked before they are replaced.  A partial unique
    index (added by migration 015) is the final cross-process guard for the
    first-ever plan, when there is no existing row to lock.
    """

    now = dt.datetime.now(dt.timezone.utc)
    active_rows = (
        (
            await session.execute(
                select(PlanRecord).where(PlanRecord.status == ACTIVE_PLAN_STATUS).with_for_update()
            )
        )
        .scalars()
        .all()
    )
    active_ids = [row.id for row in active_rows]

    if active_ids:
        await session.execute(
            update(PlanActionRecord)
            .where(
                and_(
                    PlanActionRecord.plan_id.in_(active_ids),
                    PlanActionRecord.status == "pending",
                )
            )
            .values(
                status=CANCELLED_ACTION_STATUS,
                executed_at=now,
                result_json=json.dumps(
                    {"reason": "superseded", "detail": "Replaced by a newer active plan"}
                ),
            )
        )
        await session.execute(
            update(PlanRecord)
            .where(PlanRecord.id.in_(active_ids))
            .values(
                status=SUPERSEDED_PLAN_STATUS,
                status_reason=reason,
                superseded_at=now,
            )
        )

    plan.status = ACTIVE_PLAN_STATUS
    session.add(plan)
    await session.flush()

    if active_ids:
        await session.execute(
            update(PlanRecord)
            .where(PlanRecord.id.in_(active_ids))
            .values(superseded_by_plan_id=plan.id)
        )


def active_plan_query(now: dt.datetime):
    """Return the canonical query for the currently executable plan."""

    return (
        select(PlanRecord)
        .where(
            and_(
                PlanRecord.status == ACTIVE_PLAN_STATUS,
                PlanRecord.horizon_end > now,
            )
        )
        .order_by(PlanRecord.created_at.desc())
        .limit(1)
    )

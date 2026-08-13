"""Database-backed tests for durable optimizer request recovery."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import OptimizationRequestRecord
from packages.optimizer.main import (
    _ABANDONED_REQUEST_ERROR,
    _OPTIMIZATION_REQUEST_TIMEOUT,
    _fail_abandoned_optimization_requests,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_only_expired_running_requests_are_failed(db_session: AsyncSession) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    stale = OptimizationRequestRecord(
        requested_at=now - _OPTIMIZATION_REQUEST_TIMEOUT - dt.timedelta(minutes=2),
        started_at=now - _OPTIMIZATION_REQUEST_TIMEOUT - dt.timedelta(minutes=1),
        requested_by="api",
        status="running",
    )
    fresh = OptimizationRequestRecord(
        requested_at=now - dt.timedelta(minutes=2),
        started_at=now - dt.timedelta(minutes=1),
        requested_by="api",
        status="running",
    )
    pending = OptimizationRequestRecord(
        requested_at=now - _OPTIMIZATION_REQUEST_TIMEOUT - dt.timedelta(hours=1),
        requested_by="api",
        status="pending",
    )
    db_session.add_all([stale, fresh, pending])
    await db_session.flush()

    expired = await _fail_abandoned_optimization_requests(db_session, now)
    await db_session.commit()
    await db_session.refresh(stale)
    await db_session.refresh(fresh)
    await db_session.refresh(pending)

    assert expired == 1
    assert stale.status == "failed"
    assert stale.completed_at == now
    assert stale.error == _ABANDONED_REQUEST_ERROR
    assert fresh.status == "running"
    assert fresh.completed_at is None
    assert pending.status == "pending"

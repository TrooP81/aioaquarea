"""Tests for durable optimizer request recovery."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from packages.optimizer.main import (
    _ABANDONED_REQUEST_ERROR,
    _OPTIMIZATION_REQUEST_TIMEOUT,
    _fail_abandoned_optimization_requests,
)


@pytest.mark.asyncio
async def test_abandoned_running_requests_are_failed_after_timeout() -> None:
    now = dt.datetime(2026, 8, 12, 12, tzinfo=dt.timezone.utc)
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(rowcount=2)

    expired = await _fail_abandoned_optimization_requests(session, now)

    assert expired == 2
    statement = session.execute.await_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    assert "UPDATE optimization_requests" in str(compiled)
    assert "optimization_requests.status =" in str(compiled)
    assert (
        "coalesce(optimization_requests.started_at, optimization_requests.requested_at) <"
        in str(compiled)
    )
    assert compiled.params["status"] == "failed"
    assert compiled.params["completed_at"] == now
    assert compiled.params["error"] == _ABANDONED_REQUEST_ERROR
    assert now - compiled.params["coalesce_1"] == _OPTIMIZATION_REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_no_abandoned_request_is_a_noop() -> None:
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(rowcount=0)

    expired = await _fail_abandoned_optimization_requests(
        session,
        dt.datetime(2026, 8, 12, 12, tzinfo=dt.timezone.utc),
    )

    assert expired == 0

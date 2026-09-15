from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from packages.optimizer.main import (
    _executor_shutdown_tasks,
    _shutdown_runtime,
    get_optimizer_status_snapshot,
)


@pytest.mark.asyncio
async def test_status_snapshot_does_not_reload_models() -> None:
    with (
        patch("packages.optimizer.main._load_ml_models") as load_models,
        patch(
            "packages.optimizer.main.get_planning_data_quality",
            new_callable=AsyncMock,
            return_value={"control_allowed": True},
        ),
        patch(
            "packages.optimizer.main._select_optimizer",
            new_callable=AsyncMock,
            return_value=("rules", object()),
        ),
    ):
        snapshot = await get_optimizer_status_snapshot("rules_only")

    assert snapshot["active_layer"].startswith("rules_v")
    load_models.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_runtime_uses_non_blocking_scheduler_shutdown() -> None:
    scheduler = Mock()
    wrapper = AsyncMock()

    await _shutdown_runtime(scheduler, wrapper)

    scheduler.shutdown.assert_called_once_with(wait=False)
    wrapper.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_runtime_still_stops_wrapper_when_scheduler_shutdown_fails() -> None:
    scheduler = Mock()
    scheduler.shutdown.side_effect = RuntimeError("scheduler already stopped")
    wrapper = AsyncMock()

    await _shutdown_runtime(scheduler, wrapper)

    scheduler.shutdown.assert_called_once_with(wait=False)
    wrapper.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_runtime_cancels_inflight_executor_tasks() -> None:
    scheduler = Mock()
    wrapper = AsyncMock()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def inflight_task() -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(inflight_task())
    await started.wait()
    _executor_shutdown_tasks.add(task)

    await _shutdown_runtime(scheduler, wrapper)

    scheduler.shutdown.assert_called_once_with(wait=False)
    wrapper.stop.assert_awaited_once()
    assert cancelled.is_set()
    assert task.done()
    assert task.cancelled()

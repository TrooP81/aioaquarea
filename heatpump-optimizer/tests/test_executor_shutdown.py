from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.optimizer.actions import ActionType
from packages.optimizer.executor import PlanExecutor
from packages.optimizer.executor_core import LearningModeState


def _make_action(action_type: str, payload: dict | None = None):
    action = MagicMock()
    action.id = 1
    action.plan_id = 1
    action.action_type = action_type
    action.payload_json = json.dumps(payload or {})
    action.scheduled_ts = dt.datetime.now(dt.timezone.utc)
    action.status = "pending"
    action.reverts_action_id = None
    action.device_id = "device-a"
    return action


def _extract_stmt_values(stmt) -> dict[str, object]:
    if not hasattr(stmt, "_values"):
        return {}
    values: dict[str, object] = {}
    for key, value in stmt._values.items():
        normalized_key = key.key if hasattr(key, "key") else str(key)
        values[normalized_key] = value.value if hasattr(value, "value") else value
    return values


@pytest.mark.asyncio
async def test_legacy_inflight_cancel_reconciles_without_new_ordinary_dispatch() -> None:
    wrapper = AsyncMock()
    wrapper.get_selected_device_id = AsyncMock(return_value="device-a")
    executor = PlanExecutor(wrapper)
    action = _make_action(str(ActionType.FORCE_DHW_ON))
    action.status = "dispatched"
    fake_handler = MagicMock()
    fake_handler.dispatch = AsyncMock(return_value={"force_dhw": "ON"})

    with (
        patch("packages.optimizer.executor_core.get_action_handler", return_value=fake_handler),
        patch.object(executor, "_verify_with_retry", side_effect=asyncio.CancelledError),
        patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
        patch("packages.optimizer.executor.get_session") as mock_gs,
    ):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(PermissionError, match="observe-only build"):
            await executor._execute_action(action)
        assert action.status == "dispatched"
        await executor._mark_cancelled(action)

    fake_handler.dispatch.assert_not_awaited()
    wrapper.force_dhw.assert_not_awaited()

    status_updates: list[str] = []
    cancelled_payload: dict[str, object] | None = None
    for call in mock_session.execute.await_args_list:
        values = _extract_stmt_values(call.args[0])
        status = values.get("status")
        if isinstance(status, str):
            status_updates.append(status)
        if status == "cancelled" and isinstance(values.get("result_json"), str):
            cancelled_payload = json.loads(values["result_json"])

    assert status_updates == ["cancelled"]
    assert cancelled_payload == {
        "reason": "shutdown_cancelled",
        "detail": "Executor shutdown interrupted action verification",
    }
    mock_session.add.assert_called_once()
    added_audit = mock_session.add.call_args.args[0]
    assert added_audit.actor == "optimizer"
    assert added_audit.action == str(ActionType.FORCE_DHW_ON)
    assert added_audit.result == "cancelled"


@pytest.mark.asyncio
async def test_legacy_claimed_batch_cancellation_reconciles_without_new_dispatch() -> None:
    wrapper = AsyncMock()
    executor = PlanExecutor(wrapper)

    first_action = _make_action(str(ActionType.FORCE_DHW_ON))
    first_action.id = 101
    second_action = _make_action(str(ActionType.QUIET_MODE_ON), {"level": 2})
    second_action.id = 102
    first_action.status = "executing"
    second_action.status = "executing"

    with (
        patch("packages.optimizer.executor.get_session") as mock_gs,
        patch(
            "packages.optimizer.executor.resolve_learning_mode_state",
            new=AsyncMock(return_value=LearningModeState.INACTIVE),
        ),
        patch.object(
            executor,
            "_execute_action",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ) as mock_execute_action,
    ):
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        executing_result = MagicMock()
        executing_result.scalars.return_value.all.return_value = [101, 102]
        safety_result = MagicMock()
        safety_result.scalars.return_value.all.return_value = []

        mock_session.execute = AsyncMock(
            side_effect=[
                safety_result,
                executing_result,
                None,
            ]
        )

        await executor.execute_due_actions()
        assert mock_session.execute.await_count == 1
        assert first_action.status == second_action.status == "executing"
        await executor._reconcile_claimed_batch_cancelled([first_action, second_action])

    mock_execute_action.assert_not_awaited()
    wrapper.force_dhw.assert_not_awaited()
    wrapper.set_quiet_mode.assert_not_awaited()

    cancelled_payload: dict[str, object] | None = None
    cancelled_updates = 0
    for call in mock_session.execute.await_args_list:
        values = _extract_stmt_values(call.args[0])
        if values.get("status") == "cancelled":
            cancelled_updates += 1
            if isinstance(values.get("result_json"), str):
                cancelled_payload = json.loads(values["result_json"])

    assert cancelled_updates == 1
    assert cancelled_payload == {
        "reason": "shutdown_cancelled",
        "detail": "Executor shutdown interrupted action verification",
    }
    assert mock_session.add.call_count == 2
    audit_results = [call.args[0].result for call in mock_session.add.call_args_list]
    audit_actions = [call.args[0].action for call in mock_session.add.call_args_list]
    assert audit_results == ["cancelled", "cancelled"]
    assert sorted(audit_actions) == sorted([first_action.action_type, second_action.action_type])


async def test_restore_cancel_drains_before_reclaim():
    from tests.test_safety_reverts import _S1RestoreStore

    store = _S1RestoreStore()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owned_sdk_call(_temperature, *, zone_id):
        assert zone_id == 1
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()

    store.device.set_temperature = AsyncMock(side_effect=owned_sdk_call)
    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        old_token = store.restore.safety_claim_token
        worker.cancel()
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not worker.done()
        assert (
            await store.executor()._claim_due_safety_action(
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
            )
            is None
        )
        assert store.restore.safety_claim_token == old_token
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert store.restore.status == "pending"
    assert json.loads(store.restore.result_json)["safety_attempt"]["drain"] == "confirmed"
    store.restore.safety_next_retry_at = None
    replacement = store.executor()
    claimed = await replacement._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
    assert claimed is not None and claimed.safety_claim_token != old_token
    attempt = replacement._attempt_for(claimed)
    await attempt.drain()
    await replacement._requeue_safety_action(claimed, "test_cleanup_after_confirmed_drain")
    assert await attempt.release()


@pytest.mark.parametrize("failure", ["failed_drain", "lock_loss"])
async def test_restore_failed_drain_or_lock_loss_defers(monkeypatch, failure):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1RestoreStore

    monkeypatch.setattr(safety_attempts, "DRAIN_SECONDS", 0.1)
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    store = _S1RestoreStore()
    entered, release = asyncio.Event(), asyncio.Event()

    async def owned_sdk_call(_temperature, *, zone_id):
        assert zone_id == 1
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    store.device.set_temperature = AsyncMock(side_effect=owned_sdk_call)
    executor = store.executor()
    worker = asyncio.create_task(executor.execute_due_actions())
    attempt = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        old_token = store.restore.safety_claim_token
        attempt = executor._attempt_for(store.restore)
        if failure == "failed_drain":
            worker.cancel()
        else:
            store.connection.locked = False
        result = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2)
        if failure == "failed_drain":
            assert isinstance(result[0], asyncio.CancelledError)
        assert store.restore.status == "executing"
        assert store.restore.safety_claim_token == old_token
        assert not store.audits
        assert (
            await store.executor()._claim_due_safety_action(
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
            )
            is None
        )
        assert store.connection.invalidated
        assert store.connection.events[-1] == "exit"
        assert safety_attempts._DEVICE_OWNERS[store.device_id] is attempt
        assert not attempt._drained
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if attempt is not None:
            await asyncio.wait_for(asyncio.gather(*attempt._tasks, return_exceptions=True), 2)
            assert all(task.done() for task in attempt._tasks)


async def test_restore_cancel_during_monitor_query_confirms_drain_and_requeues():
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1RestoreStore

    store = _S1RestoreStore()
    quota_entered, monitor_entered = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    original_execute = store.connection.execute

    async def wait_for_quota():
        quota_entered.set()
        await release.wait()

    async def execute(statement, parameters):
        if (
            "SELECT EXISTS" in str(statement)
            and quota_entered.is_set()
            and not monitor_entered.is_set()
        ):
            monitor_entered.set()
            await release.wait()
        return await original_execute(statement, parameters)

    store.connection.execute = AsyncMock(side_effect=execute)
    store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_quota)
    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(quota_entered.wait(), 2)
        await asyncio.wait_for(monitor_entered.wait(), 3)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 3)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert store.restore.status == "pending"
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["drain"] == "confirmed" and metadata["fence_lost"] is False
    assert store.device_id not in safety_attempts._DEVICE_OWNERS
    store.device.set_temperature.assert_not_awaited()


async def test_restore_dedicated_session_serializes_fence_query_and_commit():
    import time
    from uuid import uuid4

    from packages.core.safety_attempts import SafetyAttempt
    from tests.test_safety_reverts import _S1FenceConnection

    connection = _S1FenceConnection()
    attempt = await SafetyAttempt.acquire("serialized-session-" + str(uuid4()), connection.context)
    assert attempt is not None
    commit_entered, release = asyncio.Event(), asyncio.Event()
    original_execute, original_commit = connection.execute, connection.commit
    committing = 0
    overlaps = 0

    async def execute(statement, parameters):
        nonlocal overlaps
        if committing:
            overlaps += 1
        return await original_execute(statement, parameters)

    async def commit():
        nonlocal committing
        committing += 1
        commit_entered.set()
        try:
            await release.wait()
            await original_commit()
        finally:
            committing -= 1

    connection.execute = AsyncMock(side_effect=execute)
    connection.commit = AsyncMock(side_effect=commit)
    deadline = time.monotonic() + 3
    first = asyncio.create_task(attempt._bounded(attempt.check_fence(), deadline))
    second = None
    try:
        await asyncio.wait_for(commit_entered.wait(), 2)
        second = asyncio.create_task(attempt._bounded(attempt.check_fence(), deadline))
        await asyncio.wait({second}, timeout=0.05)
        assert overlaps == 0
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []))
        assert await attempt.release()


async def test_repair2_acquisition_and_unlock_failure_still_invalidates_and_closes(monkeypatch):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1FenceConnection

    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    connection = _S1FenceConnection()
    original_execute = connection.execute

    async def fail_queries(statement, parameters):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            connection.locked = True
            connection.events.append("acquisition_query_failure")
            raise RuntimeError("acquisition query lost acknowledgement")
        if "pg_advisory_unlock" in sql:
            connection.events.append("unlock_transaction_aborted")
            raise RuntimeError("unlock transaction aborted")
        return await original_execute(statement, parameters)

    connection.execute = fail_queries
    device_id = "repair2-acquisition-failure"
    try:
        with pytest.raises(RuntimeError, match="acquisition query lost acknowledgement"):
            await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)

        assert connection.events == [
            "enter",
            "acquisition_query_failure",
            "unlock_transaction_aborted",
            "invalidate",
            "exit",
        ]
        assert device_id not in safety_attempts._DEVICE_OWNERS
        assert not safety_attempts._DEVICE_LOCKS[device_id].locked()
    finally:
        lingering = safety_attempts._DEVICE_OWNERS.get(device_id)
        if lingering is not None:
            await connection.invalidate()
            await lingering._connection_context.__aexit__(None, None, None)


def _repair2_cleanup_connection(safety_attempts, device_id, defect):
    from types import SimpleNamespace

    from tests.test_safety_reverts import _S1FenceConnection, _s1_result

    class CleanupConnection(_S1FenceConnection):
        def __init__(self):
            super().__init__()
            self.closed = False
            self.physical_closed = False
            self.pool_reusable = False
            self.detached = False
            self.attempt = None
            self.exit_entered = asyncio.Event()
            self.exit_release = asyncio.Event()
            self.pause_exit = False

        @asynccontextmanager
        async def context(self):
            self.events.append("enter")
            self.attempt = safety_attempts._DEVICE_OWNERS[device_id]
            try:
                yield self
            finally:
                if self.pause_exit:
                    self.events.append("exit_pending")
                    self.exit_entered.set()
                    await self.exit_release.wait()
                if defect == "context_exit_failure":
                    self.events.append("exit_failure")
                    raise RuntimeError("context exit failure")
                self.events.append("exit")
                self.closed = True
                self.pool_reusable = not self.detached
                self.invalidated = False

        async def execute(self, statement, parameters):
            sql = str(statement)
            if "pg_advisory_unlock" in sql:
                if defect == "unlock_aborted":
                    self.events.append("unlock_transaction_aborted")
                    raise RuntimeError("unlock transaction aborted")
                if defect in ("invalidation_failure", "invalidation_lost_ack", "unproven_release"):
                    self.events.append("unlock_unproven")
                    return _s1_result(False)
            result = await super().execute(statement, parameters)
            if "pg_try_advisory_lock" in sql and defect == "acquisition_query_failure":
                self.events.append("acquisition_lost_ack")
                raise RuntimeError("acquisition query lost acknowledgement")
            return result

        async def invalidate(self):
            if defect in ("invalidation_failure", "unproven_release"):
                self.events.append("invalidate_failure")
                raise RuntimeError("invalidation failure")
            await super().invalidate()
            self.locked = False
            self.physical_closed = True
            if defect == "invalidation_lost_ack":
                self.events.append("invalidate_lost_ack")
                raise RuntimeError("invalidation acknowledgement lost")

        async def run_sync(self, callback):
            detaching = "detach" in callback.__code__.co_names
            self.events.append("sync_detach" if detaching else "sync_invalidate")
            if defect == "unproven_release" and not detaching:
                raise RuntimeError("physical release remains unproven")

            def invalidate():
                self.locked = False
                self.physical_closed = True
                self.invalidated = True

            def detach():
                self.detached = True

            return callback(
                type(
                    "SyncConnection",
                    (),
                    {
                        "invalidate": staticmethod(invalidate),
                        "detach": staticmethod(detach),
                        "connection": SimpleNamespace(driver_connection=self),
                    },
                )()
            )

        def is_closed(self):
            return self.physical_closed

        async def close(self):
            self.events.append("close")
            self.closed = True
            self.invalidated = False

    return CleanupConnection()


@pytest.mark.parametrize(
    "defect",
    [
        "normal",
        "acquisition_query_failure",
        "unlock_aborted",
        "invalidation_failure",
        "invalidation_lost_ack",
        "context_exit_failure",
        "unproven_release",
    ],
)
async def test_repair2_cleanup_failures_require_release_and_close_proof(monkeypatch, defect):
    from packages.core import safety_attempts

    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    device_id = "repair2-cleanup-" + defect
    connection = _repair2_cleanup_connection(safety_attempts, device_id, defect)
    if defect == "acquisition_query_failure":
        with pytest.raises(RuntimeError, match="acquisition query lost acknowledgement"):
            await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)
        attempt = connection.attempt
    else:
        attempt = await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)
        assert attempt is not None
        assert await attempt.release() is (defect != "unproven_release")

    assert connection.closed
    assert attempt._drained and all(task.done() for task in attempt._tasks)
    blocked = defect == "unproven_release"
    assert attempt._keep_blocked is blocked
    assert attempt._released is not blocked
    assert attempt._lock.locked() is blocked
    assert (device_id in safety_attempts._DEVICE_OWNERS) is blocked
    if blocked:
        assert connection.locked and not connection.physical_closed
        assert not connection.pool_reusable
        assert connection.detached and attempt.metadata()["connection_pool_detached"]
        assert attempt.metadata()["connection_release_proof"] == "unproven"
        assert await safety_attempts.SafetyAttempt.acquire(device_id, connection.context) is None
    elif defect in ("unlock_aborted", "invalidation_failure", "invalidation_lost_ack"):
        assert connection.physical_closed
        assert attempt.metadata()["connection_release_proof"] == "invalidated"
    else:
        assert connection.events.count("unlock") == 1
        assert "invalidate" not in connection.events
        assert attempt.metadata()["connection_release_proof"] == "unlocked"
    if defect == "invalidation_failure":
        assert connection.events.index("invalidate_failure") < connection.events.index(
            "sync_invalidate"
        )
        assert connection.events.index("sync_invalidate") < connection.events.index("exit")
    if defect == "context_exit_failure":
        assert connection.events[-2:] == ["exit_failure", "close"]
    events = list(connection.events)
    assert await attempt.release() is not blocked
    assert connection.events == events or blocked


async def test_repair2_independent_cleanup_budget_preserves_original_acquisition_error(
    monkeypatch,
):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1Clock

    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    clock = _S1Clock(monkeypatch)
    device_id = "repair2-independent-cumulative-cleanup"
    connection = _repair2_cleanup_connection(safety_attempts, device_id, "normal")
    original_execute, original_invalidate = connection.execute, connection.invalidate

    async def failed_query(statement, parameters):
        if "pg_try_advisory_lock" in str(statement):
            connection.locked = True
            connection.events.append("original_acquisition_failure")
            raise RuntimeError("original acquisition error remains primary")
        if "pg_advisory_unlock" in str(statement):
            clock.advance(2)
            connection.events.append("unlock_failure")
            raise RuntimeError("aborted unlock")
        return await original_execute(statement, parameters)

    async def failed_invalidation():
        clock.advance(2)
        connection.events.append("invalidation_failure")
        raise RuntimeError("invalidation failure")

    async def failed_sync_invalidation(callback):
        assert "invalidate" in callback.__code__.co_names
        clock.advance(1)
        connection.events.append("sync_invalidation_failure")
        raise RuntimeError("sync invalidation failure")

    connection.execute = failed_query
    connection.invalidate = failed_invalidation
    connection.run_sync = failed_sync_invalidation
    try:
        with pytest.raises(RuntimeError, match="original acquisition error remains primary"):
            await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)

        attempt = connection.attempt
        assert safety_attempts.DRAIN_SECONDS == 5.0
        assert clock.elapsed == 5.0
        assert attempt.drain_deadline == attempt.started_monotonic + 5.0
        assert attempt.remaining(cleanup=True) == 0
        assert attempt._drained and all(task.done() for task in attempt._tasks)
        assert attempt._keep_blocked and not attempt._released and attempt._lock.locked()
        assert safety_attempts._DEVICE_OWNERS[device_id] is attempt
        assert not connection.closed and not connection.pool_reusable and not connection.detached
        assert attempt.metadata()["connection_release_proof"] == "unproven"
        assert connection.events == [
            "enter",
            "original_acquisition_failure",
            "unlock_failure",
            "invalidation_failure",
            "sync_invalidation_failure",
        ]
        assert await safety_attempts.SafetyAttempt.acquire(device_id, connection.context) is None
        events = list(connection.events)
        assert await attempt.release() is False
        assert connection.events == events
    finally:
        await original_invalidate()
        await connection.attempt._connection_context.__aexit__(None, None, None)
        assert connection.closed and connection.physical_closed


@pytest.mark.parametrize("phase", ["unlock", "invalidate", "context_exit"])
async def test_repair2_cancellation_during_cleanup_propagates_after_close(monkeypatch, phase):
    from packages.core import safety_attempts

    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    device_id = "repair2-cleanup-cancel-" + phase
    connection = _repair2_cleanup_connection(
        safety_attempts, device_id, "unlock_aborted" if phase == "invalidate" else "normal"
    )
    attempt = await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)
    assert attempt is not None
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "unlock":
        original = connection.execute

        async def wait_at_unlock(statement, parameters):
            if "pg_advisory_unlock" in str(statement):
                entered.set()
                await release.wait()
            return await original(statement, parameters)

        connection.execute = wait_at_unlock
    elif phase == "invalidate":
        original = connection.invalidate

        async def wait_at_invalidation():
            entered.set()
            await release.wait()
            await original()

        connection.invalidate = wait_at_invalidation
    else:
        connection.pause_exit = True
        entered, release = connection.exit_entered, connection.exit_release
    worker = asyncio.create_task(attempt.release())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert attempt._lock.locked() and safety_attempts._DEVICE_OWNERS[device_id] is attempt
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert connection.closed
    assert attempt._released and attempt._drained and not attempt._keep_blocked
    assert all(task.done() for task in attempt._tasks)
    assert device_id not in safety_attempts._DEVICE_OWNERS and not attempt._lock.locked()
    if phase != "context_exit":
        assert connection.physical_closed
        assert attempt.metadata()["connection_release_proof"] == "invalidated"
    else:
        assert connection.events.count("exit_pending") == connection.events.count("close") == 1


async def test_repair2_closed_connection_cannot_release_unsettled_actor(monkeypatch):
    from packages.core import safety_attempts

    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    monkeypatch.setattr(safety_attempts, "DRAIN_SECONDS", 0.1)
    device_id = "repair2-unsettled-cleanup"
    connection = _repair2_cleanup_connection(safety_attempts, device_id, "normal")
    attempt = await safety_attempts.SafetyAttempt.acquire(device_id, connection.context)
    assert attempt is not None
    entered, stop = asyncio.Event(), asyncio.Event()

    async def unsettled_actor():
        entered.set()
        while not stop.is_set():
            try:
                await stop.wait()
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(unsettled_actor())
    attempt._tasks.add(worker)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await attempt.release() is False
        assert connection.closed and connection.physical_closed
        assert not worker.done() and not attempt._drained
        assert attempt._keep_blocked and attempt._lock.locked()
        assert safety_attempts._DEVICE_OWNERS[device_id] is attempt
        assert await safety_attempts.SafetyAttempt.acquire(device_id, connection.context) is None
    finally:
        stop.set()
        await asyncio.wait_for(worker, 2)


async def test_restore_connect_handoff_cancel_keeps_fence_until_closed(monkeypatch):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1FenceConnection

    connection = _S1FenceConnection()
    device_id = "cancel-connect-handoff"
    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    bounded = safety_attempts.SafetyAttempt._bounded
    original_invalidate = connection.invalidate
    acquired, invalidating = asyncio.Event(), asyncio.Event()
    handoff_release, cleanup_release = asyncio.Event(), asyncio.Event()

    async def pause_handoff(attempt, awaitable, deadline):
        result = await bounded(attempt, awaitable, deadline)
        if not acquired.is_set():
            acquired.set()
            await handoff_release.wait()
        return result

    async def invalidate():
        invalidating.set()
        await cleanup_release.wait()
        await original_invalidate()

    monkeypatch.setattr(safety_attempts.SafetyAttempt, "_bounded", pause_handoff)
    connection.invalidate = AsyncMock(side_effect=invalidate)
    worker = asyncio.create_task(
        safety_attempts.SafetyAttempt.acquire(device_id, connection.context)
    )
    try:
        await asyncio.wait_for(acquired.wait(), 2)
        attempt = safety_attempts._DEVICE_OWNERS[device_id]
        assert attempt.connection is connection
        worker.cancel()
        await asyncio.wait_for(invalidating.wait(), 2)
        assert attempt._lock.locked() and not attempt._released
        assert safety_attempts._DEVICE_OWNERS[device_id] is attempt
        assert connection.events == ["enter"]
        assert await safety_attempts.SafetyAttempt.acquire(device_id, connection.context) is None
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
    finally:
        handoff_release.set()
        cleanup_release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert connection.events == ["enter", "invalidate", "exit"]
    connection.invalidate.assert_awaited_once()
    assert attempt._drained and attempt._released and not attempt._lock.locked()
    assert device_id not in safety_attempts._DEVICE_OWNERS
    assert all(task.done() for task in attempt._tasks)
    assert await attempt.release()
    connection.invalidate.assert_awaited_once()
    assert connection.events.count("exit") == 1


async def test_restore_queued_connection_cancel_releases_pool_waiter(monkeypatch):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1FenceConnection

    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    connection = _S1FenceConnection()
    pool_slot = asyncio.Semaphore(1)
    entered = asyncio.Event()
    await pool_slot.acquire()

    @asynccontextmanager
    async def queued_connect():
        entered.set()
        async with pool_slot:
            async with connection.context() as acquired:
                yield acquired

    device_id = "queued-connect-cancel"
    worker = asyncio.create_task(safety_attempts.SafetyAttempt.acquire(device_id, queued_connect))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        attempt = safety_attempts._DEVICE_OWNERS[device_id]
        assert attempt._connection_acquiring and attempt.connection is None
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        pool_slot.release()

    assert not attempt._connection_acquiring
    assert attempt._drained and attempt._released and not attempt._lock.locked()
    assert connection.events == [] and device_id not in safety_attempts._DEVICE_OWNERS
    assert all(task.done() for task in attempt._tasks)
    replacement = await safety_attempts.SafetyAttempt.acquire(device_id, queued_connect)
    assert replacement is not None
    assert await replacement.release()
    assert connection.events.count("enter") == connection.events.count("exit") == 1
    assert not pool_slot.locked()


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_restore_running_acquisition_owned_until_termination(monkeypatch, ending):
    from packages.core import safety_attempts
    from tests.test_safety_reverts import _S1FenceConnection

    monkeypatch.setattr(safety_attempts, "_DEVICE_LOCKS", {})
    monkeypatch.setattr(safety_attempts, "_DEVICE_OWNERS", {})
    if ending == "deadline":
        monkeypatch.setattr(safety_attempts, "ADMISSION_SECONDS", 0.05)
    connection = _S1FenceConnection()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def finishing_connect():
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        async with connection.context() as acquired:
            yield acquired

    device_id = "running-connect-" + ending
    worker = asyncio.create_task(
        safety_attempts.SafetyAttempt.acquire(device_id, finishing_connect)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        attempt = safety_attempts._DEVICE_OWNERS[device_id]
        if ending == "cancel":
            worker.cancel()
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not worker.done() and attempt.connection is None
        assert attempt._connection_acquiring and not attempt._drained and not attempt._released
        assert attempt._lock.locked() and safety_attempts._DEVICE_OWNERS[device_id] is attempt
        assert any(not task.done() for task in attempt._tasks)
        assert await safety_attempts.SafetyAttempt.acquire(device_id, connection.context) is None
        release.set()
        expected = (
            asyncio.CancelledError if ending == "cancel" else safety_attempts.SafetyAttemptDeadline
        )
        with pytest.raises(expected):
            await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert attempt.connection is connection and not attempt._connection_acquiring
    assert attempt._drained and attempt._released and not attempt._lock.locked()
    assert device_id not in safety_attempts._DEVICE_OWNERS
    assert connection.events == ["enter", "invalidate", "exit"]
    assert all(task.done() for task in attempt._tasks)

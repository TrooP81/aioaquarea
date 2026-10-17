"""Process-device and PostgreSQL session ownership of a bounded restore attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import datetime as dt
import hashlib
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

ATTEMPT_SECONDS = 110.0
WORK_SECONDS = 40.0
VERIFY_SECONDS = 60.0
DRAIN_SECONDS = 5.0
ADMISSION_SECONDS = 2.0
OWNER_BOOT_ID = str(uuid4())
_DEVICE_LOCKS: dict[str, asyncio.Lock] = {}
_DEVICE_OWNERS: dict[str, SafetyAttempt] = {}


class SafetyAttemptLost(PermissionError):
    """The attempt cannot establish live, scoped authority."""


class SafetyAttemptDeadline(TimeoutError):
    """No remaining attempt budget permits the next operation."""


def device_attempt_key(device_id: str) -> int:
    digest = hashlib.sha256(b"hpo-safety-attempt-v1\0" + device_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def safety_connection():
    from packages.core.database import engine

    return engine.connect()


class SafetyAttempt:
    def __init__(self, device_id: str, connection_factory: Callable, *, now=None):
        self.device_id = device_id
        self.key = device_attempt_key(device_id)
        self.started_at = now or dt.datetime.now(dt.timezone.utc)
        self.started_monotonic = time.monotonic()
        self.overall_deadline = self.started_monotonic + ATTEMPT_SECONDS
        self.work_deadline = self.started_monotonic + WORK_SECONDS
        self.verify_deadline: float | None = None
        self.drain_deadline: float | None = None
        self.action_id: int | None = None
        self.source_id: int | None = None
        self.token: UUID | None = None
        self.claimed_monotonic: float | None = None
        self.dispatch_started_monotonic: float | None = None
        self.dispatch_settled_monotonic: float | None = None
        self.source_settled_at: dt.datetime | None = None
        self.dispatch_settled_at: dt.datetime | None = None
        self.payload_json: str | None = None
        self.source_payload_json: str | None = None
        self.connection = None
        self._connection_context = connection_factory()
        self._connection_acquiring = False
        self._connection_handoff_complete = False
        self._connection_closed = False
        self._connection_release_proof = "unproven"
        self._connection_pool_detached = False
        self._detached_driver_connection = None
        self._lock = _DEVICE_LOCKS.setdefault(device_id, asyncio.Lock())
        self._connection_lock = asyncio.Lock()
        self._parent = asyncio.current_task()
        self._tasks: set[asyncio.Task] = set()
        self._monitor: asyncio.Task | None = None
        self._session_locked = False
        self._released = False
        self._revoked = False
        self._lost = False
        self._drained = False
        self._keep_blocked = False
        self._ownership_lost = asyncio.Event()
        self.authority_check: Callable | None = None
        self.close_owned_transport: Callable | None = None
        self.consumed_checkpoints: set[int] = set()
        self.outcome = "claimed"

    @classmethod
    async def acquire(cls, device_id: str, connection_factory=safety_connection, *, now=None):
        if not device_id or device_id in _DEVICE_OWNERS:
            return None
        attempt = cls(device_id, connection_factory, now=now)
        if attempt._lock.locked():
            return None
        await attempt._lock.acquire()
        _DEVICE_OWNERS[device_id] = attempt
        admission_deadline = min(
            attempt.work_deadline, attempt.started_monotonic + ADMISSION_SECONDS
        )
        try:
            await attempt._bounded(attempt._acquire_connection(), admission_deadline)
            attempt._connection_handoff_complete = True
            attempt._session_locked = True
            result = await attempt._query(
                "SELECT pg_try_advisory_lock(:key)", {"key": attempt.key}, admission_deadline
            )
            attempt._session_locked = bool(result.scalar_one())
            if not attempt._session_locked:
                await attempt.release()
                return None
            await attempt.check_fence()
            return attempt
        except BaseException:
            await attempt.release()
            raise

    async def _acquire_connection(self) -> None:
        self._connection_acquiring = True
        try:
            self.connection = await self._connection_context.__aenter__()
        finally:
            self._connection_acquiring = False

    def bind(
        self,
        action_id: int,
        source_id: int,
        token: UUID,
        *,
        payload_json=None,
        source_payload_json=None,
    ) -> None:
        if self.token is not None or not isinstance(token, UUID):
            raise SafetyAttemptLost("An attempt can bind exactly one new UUID claim")
        self.action_id, self.source_id, self.token = action_id, source_id, token
        self.claimed_monotonic = time.monotonic()
        self.payload_json = payload_json
        self.source_payload_json = source_payload_json

    def remaining(self, *, cleanup=False) -> float:
        if cleanup:
            if self.drain_deadline is None:
                self.drain_deadline = min(time.monotonic() + DRAIN_SECONDS, self.overall_deadline)
            deadline = self.drain_deadline
        else:
            deadline = self.verify_deadline or self.work_deadline
            deadline = min(deadline, self.overall_deadline - DRAIN_SECONDS)
        return max(0.0, deadline - time.monotonic())

    def assert_live(self, action: Any = None, *, cleanup=False) -> None:
        current = asyncio.current_task()
        if (
            _DEVICE_OWNERS.get(self.device_id) is not self
            or not self._lock.locked()
            or not self._session_locked
            or self._lost
            or self._released
            or (self._revoked and not cleanup)
            or (current is not self._parent and current not in self._tasks)
        ):
            raise SafetyAttemptLost("Restore attempt ownership is not live")
        if action is not None and (
            action.id != self.action_id
            or action.reverts_action_id != self.source_id
            or action.device_id != self.device_id
            or action.safety_claim_token != self.token
            or action.status not in ("executing", "dispatched")
            or (self.payload_json is not None and action.payload_json != self.payload_json)
        ):
            raise SafetyAttemptLost("Restore attempt scope does not match the claim")
        if self.remaining(cleanup=cleanup) <= 0:
            raise SafetyAttemptDeadline("Restore deadline exhausted before admission")

    async def _bounded(self, awaitable, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise SafetyAttemptDeadline("Restore deadline exhausted before work")
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        lost = asyncio.create_task(self._ownership_lost.wait()) if not self._revoked else None
        try:
            watched = {task, lost} if lost is not None else {task}
            done, _pending = await asyncio.wait(
                watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                self._revoked = True
                await self.drain()
                if self._lost:
                    raise SafetyAttemptLost("Restore fence was lost while work remained owned")
                raise SafetyAttemptDeadline("Restore work exceeded its remaining budget")
            return task.result()
        except asyncio.CancelledError:
            if not self._revoked:
                self._revoked = True
                await self.drain()
            raise
        finally:
            if lost is not None:
                lost.cancel()
                await asyncio.gather(lost, return_exceptions=True)
            if task.done():
                self._tasks.discard(task)

    def assert_dispatchable(self, action) -> None:
        self.assert_live(action)
        current = asyncio.current_task()
        if current not in self._tasks or current is self._monitor:
            raise SafetyAttemptLost("SDK restore work is not a registered bounded owned call")

    async def run(self, awaitable, *, verification=False):
        try:
            self.assert_live()
        except BaseException:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise
        if verification and self.verify_deadline is None:
            origin = self.dispatch_settled_monotonic
            if origin is None:
                origin = time.monotonic()
            self.verify_deadline = min(
                origin + VERIFY_SECONDS, self.overall_deadline - DRAIN_SECONDS
            )
        deadline = self.verify_deadline or self.work_deadline
        return await self._bounded(awaitable, min(deadline, self.overall_deadline - DRAIN_SECONDS))

    async def _query(self, statement: str, parameters: dict, deadline: float):
        async def execute():
            async with self._connection_lock:
                result = await self.connection.execute(text(statement), parameters)
                await self.connection.commit()
                return result

        return await self._bounded(execute(), deadline)

    async def check_fence(self, *, cleanup=False) -> None:
        self.assert_live(cleanup=cleanup)
        deadline = time.monotonic() + self.remaining(cleanup=cleanup)
        try:
            if cleanup and self._drained:
                await self._bounded(self.connection.rollback(), deadline)
            result = await self._query(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid::bigint = :high "
                "AND objid::bigint = :low AND objsubid = 1 AND granted)",
                {"high": (self.key >> 32) & 0xFFFFFFFF, "low": self.key & 0xFFFFFFFF},
                deadline,
            )
            if not result.scalar_one():
                raise SafetyAttemptLost("Dedicated PostgreSQL session fence was lost")
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError) and self._revoked and not self._lost:
                raise
            self._lost = True
            self._revoked = True
            self._keep_blocked = True
            self._ownership_lost.set()
            for task in self._tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            raise

    def start_monitor(self) -> None:
        async def monitor():
            try:
                while not self._revoked:
                    await asyncio.sleep(min(1.0, self.remaining()))
                    await self.check_fence()
            except asyncio.CancelledError:
                raise
            except SafetyAttemptDeadline:
                self._revoked = True
                self._ownership_lost.set()
                for task in self._tasks:
                    if task is not asyncio.current_task():
                        task.cancel()
            except Exception:
                self._lost = True
                self._revoked = True
                self._keep_blocked = True
                self._ownership_lost.set()
                for task in self._tasks:
                    if task is not asyncio.current_task():
                        task.cancel()

        self._monitor = asyncio.create_task(monitor())
        self._tasks.add(self._monitor)

    async def drain(self) -> bool:
        self._revoked = True
        current = asyncio.current_task()
        pending = {task for task in self._tasks if not task.done() and task is not current}
        for task in pending:
            task.cancel()
        remaining = self.remaining(cleanup=True)
        reserve = min(0.25, remaining / 2)
        if pending and remaining > 0:
            _done, pending = await asyncio.wait(pending, timeout=max(0.0, remaining - reserve))
        if pending and self.close_owned_transport is not None and self.remaining(cleanup=True) > 0:
            try:
                await self._bounded(
                    self.close_owned_transport(), time.monotonic() + self.remaining(cleanup=True)
                )
            except Exception:
                pass
            if self.remaining(cleanup=True) > 0:
                _done, pending = await asyncio.wait(
                    pending, timeout=max(0.0, self.remaining(cleanup=True) - reserve)
                )
        self._drained = not pending and current not in self._tasks
        self._keep_blocked |= not self._drained
        for task in tuple(self._tasks):
            if task.done():
                self._tasks.discard(task)
                if not task.cancelled():
                    task.exception()
        return self._drained

    def metadata(self, *, outcome: str | None = None) -> dict:
        return {
            "schema": "safety_attempt_v1",
            "boot_id": OWNER_BOOT_ID,
            "action_id": self.action_id,
            "source_id": self.source_id,
            "device_id": self.device_id,
            "claim_token": str(self.token) if self.token else None,
            "started_at": self.started_at.isoformat(),
            "bounds_seconds": {
                "overall": ATTEMPT_SECONDS,
                "work": WORK_SECONDS,
                "verification": VERIFY_SECONDS,
                "cancel_drain": DRAIN_SECONDS,
            },
            "drain": "confirmed" if self._drained and not self._lost else "unproven",
            "owned_tasks_done": self._drained,
            "fence_lost": self._lost,
            "connection_closed": self._connection_closed,
            "connection_release_proof": self._connection_release_proof,
            "connection_pool_detached": self._connection_pool_detached,
            "outcome": outcome or self.outcome,
            "checkpoints": sorted(self.consumed_checkpoints),
            "checkpoint_origin": "sdk_restore_call_entry",
            "dispatch_started_monotonic": self.dispatch_started_monotonic,
            "dispatch_settled_monotonic": self.dispatch_settled_monotonic,
            "verification_deadline_monotonic": self.verify_deadline,
        }

    async def release(self) -> bool:
        if self._released:
            return not self._keep_blocked
        interrupted = None

        async def cleanup(awaitable, deadline):
            nonlocal interrupted
            try:
                return True, await self._bounded(awaitable, deadline)
            except BaseException as error:
                if not isinstance(error, Exception):
                    interrupted = interrupted or error
                try:
                    await self.drain()
                except BaseException as drain_error:
                    if not isinstance(drain_error, Exception):
                        interrupted = interrupted or drain_error
                return False, None

        try:
            drained = await self.drain()
        except asyncio.CancelledError as error:
            interrupted = error
            drained = False
        released = False
        if self.connection is not None:
            deadline = time.monotonic() + self.remaining(cleanup=True)
            session_released = False
            if self._session_locked and not self._lost and drained:
                unlocked, result = await cleanup(
                    self._query("SELECT pg_advisory_unlock(:key)", {"key": self.key}, deadline),
                    deadline,
                )
                if unlocked and result.scalar_one():
                    absent, result = await cleanup(
                        self._query(
                            "SELECT NOT EXISTS (SELECT 1 FROM pg_locks "
                            "WHERE locktype = 'advisory' AND pid = pg_backend_pid())",
                            {},
                            deadline,
                        ),
                        deadline,
                    )
                    session_released = absent and bool(result.scalar_one())
                    if session_released:
                        self._connection_release_proof = "unlocked"
            elif not self._session_locked and self._connection_handoff_complete and drained:
                absent, result = await cleanup(
                    self._query(
                        "SELECT NOT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid())",
                        {},
                        deadline,
                    ),
                    deadline,
                )
                session_released = absent and bool(result.scalar_one())
                if session_released:
                    self._connection_release_proof = "no_advisory_locks"
            if not session_released:
                await cleanup(self.connection.invalidate(), deadline)
                session_released = bool(self.connection.invalidated)
                if not session_released and hasattr(self.connection, "run_sync"):
                    await cleanup(
                        self.connection.run_sync(lambda connection: connection.invalidate()),
                        deadline,
                    )
                    session_released = bool(self.connection.invalidated)
                if session_released:
                    self._connection_release_proof = "invalidated"
            if (
                not session_released
                and not self._connection_pool_detached
                and hasattr(self.connection, "run_sync")
            ):

                def detach(connection):
                    driver = connection.connection.driver_connection
                    connection.detach()
                    return driver

                detached, driver = await cleanup(self.connection.run_sync(detach), deadline)
                self._connection_pool_detached = detached
                if detached:
                    self._detached_driver_connection = driver
            if session_released or self._connection_pool_detached:
                exit_call = (
                    self.connection.close()
                    if self._connection_context is self.connection
                    else self._connection_context.__aexit__(None, None, None)
                )
                exited, _result = await cleanup(exit_call, deadline)
                self._connection_closed = exited and bool(getattr(self.connection, "closed", True))
                if not self._connection_closed and hasattr(self.connection, "close"):
                    closed, _result = await cleanup(self.connection.close(), deadline)
                    self._connection_closed = closed and bool(self.connection.closed)
            if not session_released and self._connection_pool_detached:
                driver = self._detached_driver_connection
                try:
                    session_released = bool(driver is not None and driver.is_closed())
                except Exception:
                    session_released = False
                if session_released:
                    self._connection_release_proof = "detached_closed"
            released = session_released and self._connection_closed
            if session_released:
                self._session_locked = False
        elif drained and not self._connection_acquiring:
            released = True
            self._connection_release_proof = "not_acquired"
        drained = self._drained and all(task.done() for task in self._tasks)
        self._released = released
        self._keep_blocked |= not drained or not released
        if not self._keep_blocked and _DEVICE_OWNERS.get(self.device_id) is self:
            del _DEVICE_OWNERS[self.device_id]
            self._lock.release()
        if interrupted is not None:
            raise interrupted
        return not self._keep_blocked

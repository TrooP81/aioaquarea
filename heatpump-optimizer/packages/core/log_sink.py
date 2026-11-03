"""Structlog processor that persists log entries to the app_logs table.

Usage in service entry points::

    from packages.core.log_sink import configure_structlog_with_db

    async def main():
        configure_structlog_with_db("optimizer")
        ...

Admission does not wait for capacity. The queue owns at most 1,000 rows,
including in-flight batches; encoded rows are limited to 8 KiB and batches
to 100 rows. Capacity is reserved before bounded traversal/encoding; unsupported
values, excessive nesting and unencodable rows use the oversize drop counter.
Failure diagnostics use at most one daemon writer, never the event loop.
A batch is acknowledged only after the transaction context commits.
Failed writes retain ownership for at most three attempts or
30 seconds, with bounded attempt waits. Drop counters are exposed by
``log_sink_status``. Entries older than 24 h are pruned on each write.

``shutdown_log_sink`` bounds drainage to five seconds without relinquishing
an unsettled write's ownership. Ordinary logs may duplicate after an ambiguous
commit or be lost on abrupt process death. Transactional safety audits must
never use this best-effort queue.

``bind_log_session_factory`` is an opt-in ownership lease for a strongly retained
async transaction-context factory (including commit on context exit), with no
fallback to the application database. Release only after shutdown and resource
cleanup. Explicit release leaves a closed fence until a fresh binding; callers
that never bind retain default startup/restart behavior. Lifecycle calls belong
to one loop/thread per generation; synchronous producers may use other threads.
An unsettled shutdown is permanently fenced, even after late worker completion.
Status counters are process-cumulative; ``admitted_loss`` excludes pre-admission
rejections. A settled shutdown does not mean a loss-free or durable audit phase.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import structlog

from packages.core.config import settings


# Buffer log entries for batch insert (avoids one DB round-trip per log line)
@dataclass
class _BufferedLog:
    row: dict[str, Any]
    admitted_at: float
    attempts: int = 0


_LOG_BUFFER: list[_BufferedLog] = []
_BUFFER_LOCK = asyncio.Lock()
_ADMISSION_LOCK = threading.Lock()
_ENCODING_ROWS = 0
_MAX_OWNED_ROWS = 1000
_BATCH_SIZE = 100
_MAX_ENCODED_ROW_BYTES = 8 * 1024
_MAX_ATTEMPTS = 3
_MAX_AGE_SECONDS = 30
_ATTEMPT_TIMEOUT_SECONDS = 2
_SHUTDOWN_TIMEOUT_SECONDS = 5
_DROP_COUNTS: Counter[str] = Counter()
_DROP_LOCK = threading.Lock()
_CLOSING = True
_FLUSH_INTERVAL = 2  # seconds
_SERVICE_NAME: str = "unknown"
_FLUSH_TASK: asyncio.Task[None] | None = None
_INFLIGHT_TASK: asyncio.Task[None] | None = None
_INFLIGHT_BATCH: list[_BufferedLog] = []
_DIAGNOSTIC_LOCK = threading.Lock()
_DIAGNOSTIC_THREAD: threading.Thread | None = None
_DIAGNOSTIC_DROPS = 0

# Retention: keep only the last 24 h of logs
_RETENTION_HOURS = 24


# Lifecycle calls belong to one loop/thread. Producers alone may cross threads.
# The metadata lock protects reservations, never callbacks, task factories or I/O.
@dataclass
class _Owner:
    generation: int = 0
    state: str = "IDLE"
    thread: int = 0
    loop: asyncio.AbstractEventLoop | None = None
    factory: Callable | None = None
    lease: object | None = None
    controller: asyncio.Task | None = None
    deadline: float | None = None
    failure: str | None = None
    creating: bool = False


_OWNER = _Owner()
_EXPLICIT_REQUIRED = False
_COUNTS: Counter[str] = Counter()
_ATTEMPT_TIMED_OUT = False
_WRITER_CANCEL_REQUESTED = False


def _count(reason: str, count: int = 1) -> None:
    with _DROP_LOCK:
        _COUNTS[reason] += count


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _check_boundary(owner, loop):
    if owner.thread and owner.thread != threading.get_ident():
        raise RuntimeError("log lifecycle belongs to another thread")
    if owner.loop is not None and owner.loop is not loop:
        raise RuntimeError("log lifecycle belongs to another loop")


class _BindingLease:
    """Retain the actual async transaction-context factory until quiescent release."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def release(self):
        global _CLOSING
        loop = _running_loop()
        with _ADMISSION_LOCK:
            _check_boundary(_OWNER, loop)
            if _OWNER.lease is not self:
                raise RuntimeError("log binding is not owned by this lease")
            if _OWNER.state not in ("BOUND_READY", "SETTLED"):
                raise RuntimeError("log binding still owns work; resource disposal forbidden")
            if (
                _ENCODING_ROWS
                or _LOG_BUFFER
                or any(
                    task is not None and not task.done() for task in (_FLUSH_TASK, _INFLIGHT_TASK)
                )
            ):
                raise RuntimeError("log binding is not quiescent; resource disposal forbidden")
            _OWNER.factory = None
            _OWNER.lease = None
            _OWNER.state = "SETTLED"
            _CLOSING = True


def bind_log_session_factory(factory: Callable) -> _BindingLease:
    """Opt into explicit ownership. No fallback; unbinding leaves a closed fence."""
    global _OWNER, _EXPLICIT_REQUIRED, _CLOSING
    if not callable(factory):
        raise TypeError("log factory must supply an async transaction context")
    loop = _running_loop()
    lease = _BindingLease()
    with _ADMISSION_LOCK:
        if _OWNER.state not in ("IDLE", "SETTLED") or _OWNER.lease is not None:
            raise RuntimeError("log sink must be quiescent and unbound before binding")
        if _ENCODING_ROWS or _LOG_BUFFER:
            raise RuntimeError("cannot adopt previously admitted logging work")
        _OWNER = _Owner(
            generation=_OWNER.generation + 1,
            state="BOUND_READY",
            thread=threading.get_ident(),
            loop=loop,
            factory=factory,
            lease=lease,
        )
        _EXPLICIT_REQUIRED = True
        _CLOSING = True
    return lease


def _record_drop(reason: str, count: int = 1) -> None:
    if not count:
        return
    with _DROP_LOCK:
        _DROP_COUNTS[reason] += count


def _bounded_json(value: Any, *, ensure_ascii=True, skip=frozenset()) -> str:
    """Encode at most 8 KiB, 4096 nodes and 16 levels, without copying input trees.

    Strings are escaped one character at a time so even a single enormous value
    cannot allocate an oversized JSON chunk. Arbitrary __str__/iterator hooks
    are not invoked by this best-effort path.
    """
    remaining_nodes = 4096
    encoded = bytearray()

    def chunks(item, depth=0):
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0 or depth > 16:
            raise ValueError("log traversal budget exceeded")
        kind = type(item)
        if kind in (dt.datetime, dt.date):
            item, kind = item.isoformat(), str
        if kind is str:
            yield '"'
            for char in item:
                yield json.dumps(char, ensure_ascii=ensure_ascii)[1:-1]
            yield '"'
        elif kind is dict:
            yield "{"
            separator = ""
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("unsupported log key")
                if depth == 0 and key in skip:
                    continue
                yield separator
                yield from chunks(key, depth + 1)
                yield ": "
                yield from chunks(child, depth + 1)
                separator = ", "
            yield "}"
        elif kind in (list, tuple):
            yield "["
            for index, child in enumerate(item):
                if index:
                    yield ", "
                yield from chunks(child, depth + 1)
            yield "]"
        elif item is None or kind is bool:
            yield json.dumps(item)
        elif kind is int and item.bit_length() <= 256:
            yield str(item)
        elif kind is float and math.isfinite(item):
            yield json.dumps(item)
        else:
            raise ValueError("unsupported log value")

    for chunk in chunks(value):
        part = chunk.encode("utf-8")
        if len(encoded) + len(part) > _MAX_ENCODED_ROW_BYTES:
            raise ValueError("encoded log budget exceeded")
        encoded.extend(part)
    return encoded.decode("utf-8")


def _log_text(value: Any, limit: int) -> str:
    if type(value) is str:
        return value[:limit]
    # Only bounded builtin conversion; never call application __str__ methods.
    return _bounded_json(value)[:limit]


def _encode_row(method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    skip = {"event", "logger", "level", "timestamp", "_record"}
    details_json = _bounded_json(event_dict, skip=skip)
    logger_name = event_dict.get("logger")
    logger_name = None if logger_name is None else _log_text(logger_name, 128)
    entry = {
        "level": _log_text(method_name, 32).upper(),
        "logger_name": logger_name,
        "event": _log_text(event_dict.get("event", ""), 256),
        "details_json": None if details_json == "{}" else details_json,
        "service": _SERVICE_NAME,
    }
    _bounded_json(entry, ensure_ascii=False)
    return entry


def _db_log_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Reserve ownership before inspecting the event; never wait for queue capacity."""
    global _ENCODING_ROWS
    if not _ADMISSION_LOCK.acquire(blocking=False):
        _record_drop("saturation")
        _count("rejected")
        return event_dict
    try:
        if _CLOSING:
            _record_drop("shutdown")
            _count("rejected")
            return event_dict
        if len(_LOG_BUFFER) + _ENCODING_ROWS >= _MAX_OWNED_ROWS:
            _record_drop("saturation")
            _count("rejected")
            return event_dict
        _ENCODING_ROWS += 1
        owner = _OWNER
    finally:
        _ADMISSION_LOCK.release()

    admitted_at = time.monotonic()
    entry = None
    try:
        entry = _encode_row(method_name, event_dict)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, RuntimeError):
        _record_drop("oversize")
        _count("rejected")
    finally:
        # Metadata-only critical section: no encoding, I/O or capacity wait.
        with _ADMISSION_LOCK:
            _ENCODING_ROWS -= 1
            if entry is not None:
                if _CLOSING or owner is not _OWNER:
                    _record_drop("shutdown")
                    _count("rejected")
                else:
                    _LOG_BUFFER.append(_BufferedLog(entry, admitted_at))
                    _count("admitted")
    return event_dict


async def _write_batch(batch: list[_BufferedLog]) -> None:
    """Acknowledgement is deliberately outside this transaction context."""
    from packages.core.database import get_session
    from packages.core.models import AppLogRecord
    from sqlalchemy import delete

    # Capture and retain the selected callable for the entire transaction exit.
    # An explicit factory exception MUST NOT retry through the application route.
    with _ADMISSION_LOCK:
        factory = _OWNER.factory
    async with (get_session if factory is None else factory)() as session:
        for entry in batch:
            session.add(AppLogRecord(**entry.row))
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=_RETENTION_HOURS)
        await session.execute(delete(AppLogRecord).where(AppLogRecord.ts < cutoff))


def _claim_batch() -> list[_BufferedLog]:
    with _ADMISSION_LOCK:
        now = time.monotonic()
        retained = []
        for entry in _LOG_BUFFER:
            if entry.attempts >= _MAX_ATTEMPTS or now - entry.admitted_at > _MAX_AGE_SECONDS:
                _record_drop("retry_age")
                _count("admitted_loss")
            else:
                retained.append(entry)
        _LOG_BUFFER[:] = retained
        batch = _LOG_BUFFER[:_BATCH_SIZE]
        for entry in batch:
            entry.attempts += 1
        return batch


def log_sink_status() -> dict[str, object]:
    with _ADMISSION_LOCK:
        with _DROP_LOCK:
            drops = dict(_DROP_COUNTS)
            counts = dict(_COUNTS)
        return {
            "owned_rows": len(_LOG_BUFFER) + _ENCODING_ROWS,
            "encoding_rows": _ENCODING_ROWS,
            "inflight_rows": len(_INFLIGHT_BATCH),
            "maximum_owned_rows": _MAX_OWNED_ROWS,
            "drops": drops,
            "accepting": not _CLOSING,
            "diagnostic_pending": _DIAGNOSTIC_LOCK.locked(),
            "diagnostic_drops": _DIAGNOSTIC_DROPS,
            "state": _OWNER.state,
            "generation": _OWNER.generation,
            "explicit_binding": _OWNER.lease is not None,
            "binding_required": _EXPLICIT_REQUIRED,
            "shutdown_deadline": _OWNER.deadline,
            "failure": _OWNER.failure,
            "counts": counts,
        }


def _report_flush_failure(exc: Exception) -> None:
    """At most one bounded message/writer; a stalled stderr cannot stall asyncio."""
    global _DIAGNOSTIC_THREAD, _DIAGNOSTIC_DROPS
    if not _DIAGNOSTIC_LOCK.acquire(blocking=False):
        with _DROP_LOCK:
            _DIAGNOSTIC_DROPS += 1
        return
    sink = sys.stderr
    message = f"log sink flush failed: {type(exc).__name__[:128]}\n"

    def write():
        global _DIAGNOSTIC_DROPS
        try:
            sink.write(message)
        except Exception:
            with _DROP_LOCK:
                _DIAGNOSTIC_DROPS += 1
        finally:
            _DIAGNOSTIC_LOCK.release()

    try:
        _DIAGNOSTIC_THREAD = threading.Thread(target=write, daemon=True)
        _DIAGNOSTIC_THREAD.start()
    except Exception:
        _DIAGNOSTIC_LOCK.release()
        with _DROP_LOCK:
            _DIAGNOSTIC_DROPS += 1


def _acknowledge_batch(batch: list[_BufferedLog]) -> None:
    with _ADMISSION_LOCK:
        if len(_LOG_BUFFER) < len(batch) or any(
            left is not right for left, right in zip(_LOG_BUFFER, batch)
        ):
            raise RuntimeError("log batch ownership changed before acknowledgement")
        del _LOG_BUFFER[: len(batch)]
        _count("acknowledged", len(batch))


def _settle_inflight() -> None:
    global _INFLIGHT_TASK, _INFLIGHT_BATCH
    if _INFLIGHT_TASK is None or not _INFLIGHT_TASK.done():
        return
    task, batch = _INFLIGHT_TASK, _INFLIGHT_BATCH
    _INFLIGHT_TASK, _INFLIGHT_BATCH = None, []
    try:
        task.result()
    except asyncio.CancelledError:
        _count("writer_cancellations")
        if _CLOSING:
            with _ADMISSION_LOCK:
                _record_drop("shutdown", len(batch))
                _count("admitted_loss", len(batch))
                del _LOG_BUFFER[: len(batch)]
    except Exception as exc:
        _count("batch_failures")
        _report_flush_failure(exc)
        if _CLOSING:
            with _ADMISSION_LOCK:
                _record_drop("shutdown", len(batch))
                _count("admitted_loss", len(batch))
                del _LOG_BUFFER[: len(batch)]
    else:
        _acknowledge_batch(batch)


def _cancel_writer():
    global _WRITER_CANCEL_REQUESTED
    if _INFLIGHT_TASK is not None and not _INFLIGHT_TASK.done() and not _WRITER_CANCEL_REQUESTED:
        _WRITER_CANCEL_REQUESTED = True
        _count("writer_cancel_requests")
        _INFLIGHT_TASK.cancel()


async def _run_write_batch(batch):
    _count("retried_rows", sum(entry.attempts > 1 for entry in batch))
    await _write_batch(batch)


async def _flush_locked(*, timeout, owner, shutdown=False):
    global _INFLIGHT_TASK, _INFLIGHT_BATCH, _ATTEMPT_TIMED_OUT, _WRITER_CANCEL_REQUESTED
    _settle_inflight()
    if _INFLIGHT_TASK is None:
        with _ADMISSION_LOCK:
            allowed = owner is _OWNER and (
                owner.state in ("PRELOOP", "RUNNING")
                or (shutdown and owner.state == "SHUTTING_DOWN")
            )
        if not allowed:
            return
        batch = _claim_batch()
        if not batch:
            return
        _INFLIGHT_BATCH = batch
        _ATTEMPT_TIMED_OUT = _WRITER_CANCEL_REQUESTED = False
        coroutine = _run_write_batch(batch)
        with _ADMISSION_LOCK:
            owner.creating = True
        try:
            # No await between reservation, creation and publication.
            task = asyncio.create_task(coroutine)
            with _ADMISSION_LOCK:
                _INFLIGHT_TASK = task
        except BaseException:
            coroutine.close()
            _INFLIGHT_BATCH = []
            _count("batch_failures")
            _count("task_creation_failures")
            raise
        finally:
            with _ADMISSION_LOCK:
                owner.creating = False
        _INFLIGHT_TASK.add_done_callback(
            lambda _task: _settle_inflight() if owner is _OWNER else None
        )
    task = _INFLIGHT_TASK
    done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
    if not done:
        if not _ATTEMPT_TIMED_OUT:
            _ATTEMPT_TIMED_OUT = True
            _count("attempt_timeouts")
        _cancel_writer()
    _settle_inflight()


async def _flush_once(*, timeout=_ATTEMPT_TIMEOUT_SECONDS) -> None:
    owner = _OWNER
    async with _BUFFER_LOCK:
        await _flush_locked(timeout=timeout, owner=owner)


async def _flush_loop() -> None:
    owner = _OWNER
    while owner is _OWNER and owner.state == "RUNNING":
        await asyncio.sleep(_FLUSH_INTERVAL)
        await _flush_once()


def _lock_available(lock):
    # Match asyncio.Lock.acquire's native fast path, INCLUDING queued waiters.
    # A released lock may be reserved for a waiter that has not resumed yet.
    return not lock.locked() and (
        not lock._waiters or all(waiter.cancelled() for waiter in lock._waiters)
    )


async def _shutdown_controller(owner):
    """One controller, one absolute budget. Never join cancellation via wait_for."""
    locked = False

    def remaining():
        return max(0, owner.deadline - time.monotonic())

    try:
        if _FLUSH_TASK is not None:
            if not _FLUSH_TASK.done():
                _count("flusher_cancel_requests")
                _FLUSH_TASK.cancel()
                await asyncio.wait({_FLUSH_TASK}, timeout=remaining())
            if not _FLUSH_TASK.done():
                raise RuntimeError("flusher-live")
            # Observe terminal exceptions (ordinary cancellation is expected).
            if not _FLUSH_TASK.cancelled():
                try:
                    _FLUSH_TASK.result()
                except Exception as exc:
                    _count("flusher_failures")
                    _report_flush_failure(exc)
        if not _lock_available(_BUFFER_LOCK):
            raise RuntimeError("drain-lock-unavailable")
        # Native uncontended acquire completes inline: no waiter/task/scheduling.
        await _BUFFER_LOCK.acquire()
        locked = True
        while remaining() and (_LOG_BUFFER or _INFLIGHT_TASK is not None):
            await _flush_locked(
                timeout=min(_ATTEMPT_TIMEOUT_SECONDS, remaining()), owner=owner, shutdown=True
            )
            if _INFLIGHT_TASK is not None:
                # A resistant writer retains ownership; no overlapping retry.
                await asyncio.wait({_INFLIGHT_TASK}, timeout=remaining())
                _settle_inflight()
        while _ENCODING_ROWS and remaining():
            await asyncio.sleep(min(0.001, remaining()))
        _settle_inflight()
        if _INFLIGHT_TASK is not None or _ENCODING_ROWS:
            raise RuntimeError("writer-or-encoding-live")
        with _ADMISSION_LOCK:
            _record_drop("shutdown", len(_LOG_BUFFER))
            _count("admitted_loss", len(_LOG_BUFFER))
            _LOG_BUFFER.clear()
            owner.state = "SETTLED"
    except BaseException as exc:
        owner.failure = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        with _ADMISSION_LOCK:
            owner.state = "FAILED_UNSETTLED"
        _count("unsettled_shutdowns")
    finally:
        if not remaining():
            _count("shutdown_expirations")
        if locked:
            _BUFFER_LOCK.release()
        _count("shutdown_outcomes")


def _controller_finished(owner, task):
    """Also fences cancellation before the controller's first coroutine turn."""
    try:
        task.result()
    except BaseException as exc:
        with _ADMISSION_LOCK:
            if owner.state in ("SETTLED", "FAILED_UNSETTLED"):
                return
            owner.failure = "controller-" + type(exc).__name__
            owner.state = "FAILED_UNSETTLED"
        _count("unsettled_shutdowns")
        _count("shutdown_outcomes")


async def shutdown_log_sink() -> None:
    """Join the strongly retained controller, even after repeated caller cancellation."""
    global _CLOSING
    loop = asyncio.get_running_loop()
    with _ADMISSION_LOCK:
        owner = _OWNER
        _check_boundary(owner, loop)
        if owner.creating:
            raise RuntimeError("log task creation owns lifecycle")
        if owner.state in ("IDLE", "BOUND_READY"):
            _CLOSING = True
            owner.state = "SETTLED"
            return
        if owner.state not in ("SHUTTING_DOWN", "SETTLED", "FAILED_UNSETTLED"):
            owner.loop = loop
            owner.state = "SHUTTING_DOWN"
            owner.deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
            _CLOSING = True
            owner.creating = True
    if owner.controller is None and owner.state == "SHUTTING_DOWN":
        coroutine = _shutdown_controller(owner)
        try:
            task = loop.create_task(coroutine)
            with _ADMISSION_LOCK:
                owner.controller = task
        except BaseException:
            coroutine.close()
            owner.state = "FAILED_UNSETTLED"
            owner.failure = "controller-creation-failed"
            _count("unsettled_shutdowns")
            _count("shutdown_outcomes")
            _count("task_creation_failures")
            raise
        finally:
            with _ADMISSION_LOCK:
                owner.creating = False
        owner.controller.add_done_callback(lambda task: _controller_finished(owner, task))
    cancelled = False
    if owner.controller is not None:
        while not owner.controller.done():
            try:
                await asyncio.shield(owner.controller)
            except asyncio.CancelledError:
                _count("caller_cancellations")
                cancelled = True
        _controller_finished(owner, owner.controller)
    if owner.state == "FAILED_UNSETTLED":
        raise RuntimeError(f"log sink {owner.failure}; resource disposal forbidden")
    if cancelled:
        raise asyncio.CancelledError


def configure_structlog_with_db(service_name: str) -> None:
    """Configure structlog with the DB-persistence processor.

    Call this once at service startup (after the event loop is running).
    """
    global _SERVICE_NAME, _CLOSING, _OWNER, _FLUSH_TASK, _BUFFER_LOCK
    loop = _running_loop()
    with _ADMISSION_LOCK:
        owner = _OWNER
        if owner.creating:
            raise RuntimeError("log task creation owns lifecycle")
        if owner.state in ("SHUTTING_DOWN", "FAILED_UNSETTLED"):
            raise RuntimeError("log sink shutdown owns lifecycle")
        if owner.state == "SETTLED" and owner.lease is not None:
            raise RuntimeError("release log binding before restarting")
        if owner.state in ("IDLE", "SETTLED"):
            if _EXPLICIT_REQUIRED:
                raise RuntimeError("log sink requires a fresh explicit binding")
            owner = _OWNER = _Owner(generation=owner.generation + 1)
        _check_boundary(owner, loop)
        if owner.state in ("PRELOOP", "RUNNING") and service_name != _SERVICE_NAME:
            raise RuntimeError("conflicting log service configuration")
        start = owner.state != "RUNNING" and loop is not None
        if owner.state == "RUNNING" and _FLUSH_TASK is not None and _FLUSH_TASK.done():
            raise RuntimeError("log flusher terminated; shutdown required")
        owner.thread = threading.get_ident()
        owner.loop = loop
        owner.state = "RUNNING" if loop is not None else "PRELOOP"
        _SERVICE_NAME = service_name
        _CLOSING = False
        owner.creating = start
    if start:
        _BUFFER_LOCK = asyncio.Lock()
        coroutine = _flush_loop()
        try:
            task = loop.create_task(coroutine)
            with _ADMISSION_LOCK:
                _FLUSH_TASK = task
        except BaseException:
            coroutine.close()
            with _ADMISSION_LOCK:
                _CLOSING = True
                owner.state = "PRELOOP"
            _count("task_creation_failures")
            raise
        finally:
            with _ADMISSION_LOCK:
                owner.creating = False

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _db_log_processor,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
    )

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
from typing import Any

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
_CLOSING = False
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


def _record_drop(reason: str, count: int = 1) -> None:
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
        return event_dict
    try:
        if _CLOSING:
            _record_drop("shutdown")
            return event_dict
        if len(_LOG_BUFFER) + _ENCODING_ROWS >= _MAX_OWNED_ROWS:
            _record_drop("saturation")
            return event_dict
        _ENCODING_ROWS += 1
    finally:
        _ADMISSION_LOCK.release()

    admitted_at = time.monotonic()
    entry = None
    try:
        entry = _encode_row(method_name, event_dict)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, RuntimeError):
        _record_drop("oversize")
    finally:
        # Metadata-only critical section: no encoding, I/O or capacity wait.
        with _ADMISSION_LOCK:
            _ENCODING_ROWS -= 1
            if entry is not None:
                if _CLOSING:
                    _record_drop("shutdown")
                else:
                    _LOG_BUFFER.append(_BufferedLog(entry, admitted_at))
    return event_dict


async def _write_batch(batch: list[_BufferedLog]) -> None:
    """Acknowledgement is deliberately outside this transaction context."""
    from packages.core.database import get_session
    from packages.core.models import AppLogRecord
    from sqlalchemy import delete

    async with get_session() as session:
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
        return {
            "owned_rows": len(_LOG_BUFFER) + _ENCODING_ROWS,
            "encoding_rows": _ENCODING_ROWS,
            "inflight_rows": len(_INFLIGHT_BATCH),
            "maximum_owned_rows": _MAX_OWNED_ROWS,
            "drops": drops,
            "accepting": not _CLOSING,
            "diagnostic_pending": _DIAGNOSTIC_LOCK.locked(),
            "diagnostic_drops": _DIAGNOSTIC_DROPS,
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


def _settle_inflight() -> None:
    global _INFLIGHT_TASK, _INFLIGHT_BATCH
    if _INFLIGHT_TASK is None or not _INFLIGHT_TASK.done():
        return
    task, batch = _INFLIGHT_TASK, _INFLIGHT_BATCH
    _INFLIGHT_TASK, _INFLIGHT_BATCH = None, []
    try:
        task.result()
    except asyncio.CancelledError:
        if _CLOSING:
            with _ADMISSION_LOCK:
                _record_drop("shutdown", len(batch))
                del _LOG_BUFFER[: len(batch)]
    except Exception as exc:
        _report_flush_failure(exc)
        if _CLOSING:
            with _ADMISSION_LOCK:
                _record_drop("shutdown", len(batch))
                del _LOG_BUFFER[: len(batch)]
    else:
        _acknowledge_batch(batch)


async def _flush_once(*, timeout=_ATTEMPT_TIMEOUT_SECONDS) -> None:
    global _INFLIGHT_TASK, _INFLIGHT_BATCH
    async with _BUFFER_LOCK:
        _settle_inflight()
        if _INFLIGHT_TASK is not None:
            await asyncio.wait({_INFLIGHT_TASK}, timeout=timeout)
            _settle_inflight()
            return
        batch = _claim_batch()
        if not batch:
            return
        _INFLIGHT_BATCH = batch
        _INFLIGHT_TASK = asyncio.create_task(_write_batch(batch))
        task = _INFLIGHT_TASK
        task.add_done_callback(lambda _task: _settle_inflight())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
        _settle_inflight()


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        await _flush_once()


async def shutdown_log_sink() -> None:
    """Stop admission and drain ordinary logs within the fixed shutdown budget."""
    global _CLOSING, _FLUSH_TASK
    _CLOSING = True

    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
    try:
        if _FLUSH_TASK is not None:
            _FLUSH_TASK.cancel()
            await asyncio.wait({_FLUSH_TASK}, timeout=max(0, deadline - time.monotonic()))
        while _LOG_BUFFER and (remaining := deadline - time.monotonic()) > 0:
            await _flush_once(timeout=min(_ATTEMPT_TIMEOUT_SECONDS, remaining))
    finally:
        _FLUSH_TASK = None
        if _INFLIGHT_TASK is not None and not _INFLIGHT_TASK.done():
            _INFLIGHT_TASK.cancel()
        _settle_inflight()
        with _ADMISSION_LOCK:
            retained_ids = {id(entry) for entry in _INFLIGHT_BATCH}
            _record_drop("shutdown", sum(id(entry) not in retained_ids for entry in _LOG_BUFFER))
            _LOG_BUFFER[:] = [entry for entry in _LOG_BUFFER if id(entry) in retained_ids]
        if _INFLIGHT_TASK is not None and not _INFLIGHT_TASK.done():
            raise RuntimeError("log writer unsettled; resource disposal forbidden")


def _ensure_flush_loop() -> None:
    """Start the flush task only when an event loop is available."""
    global _FLUSH_TASK

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    if _FLUSH_TASK is None or _FLUSH_TASK.done():
        _FLUSH_TASK = loop.create_task(_flush_loop())


def configure_structlog_with_db(service_name: str) -> None:
    """Configure structlog with the DB-persistence processor.

    Call this once at service startup (after the event loop is running).
    """
    global _SERVICE_NAME, _CLOSING
    _SERVICE_NAME = service_name
    _CLOSING = False

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

    _ensure_flush_loop()

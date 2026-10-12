"""Structlog processor that persists log entries to the app_logs table.

Usage in service entry points::

    from packages.core.log_sink import configure_structlog_with_db

    async def main():
        configure_structlog_with_db("optimizer")
        ...

Admission does not wait for capacity. The queue owns at most 1,000 rows,
including in-flight batches; encoded rows are limited to 8 KiB and batches
to 100 rows. A batch is acknowledged only after the transaction context
commits. Failed writes retain ownership for at most three attempts or
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

# Retention: keep only the last 24 h of logs
_RETENTION_HOURS = 24


def _record_drop(reason: str, count: int = 1) -> None:
    with _DROP_LOCK:
        _DROP_COUNTS[reason] += count


def _db_log_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor: enqueue a log row for async DB persistence."""
    # Extract the fields we care about
    level = method_name.upper()
    event = str(event_dict.get("event", ""))[:256]
    logger_name = event_dict.get("logger", None)
    if logger_name:
        logger_name = str(logger_name)[:128]

    # Everything except the standard keys goes into details_json
    skip = {"event", "logger", "level", "timestamp", "_record"}
    details = {k: v for k, v in event_dict.items() if k not in skip}
    # Ensure JSON-safe values
    for k, v in list(details.items()):
        if isinstance(v, (dt.datetime, dt.date)):
            details[k] = v.isoformat()
        elif not isinstance(v, (str, int, float, bool, list, dict, type(None))):
            details[k] = str(v)

    try:
        details_json = json.dumps(details, default=str) if details else None
    except (TypeError, ValueError, OverflowError):
        _record_drop("oversize")
        return event_dict

    entry = {
        "level": level,
        "logger_name": logger_name,
        "event": event,
        "details_json": details_json,
        "service": _SERVICE_NAME,
    }

    if len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) > _MAX_ENCODED_ROW_BYTES:
        _record_drop("oversize")
        return event_dict
    if not _ADMISSION_LOCK.acquire(blocking=False):
        _record_drop("saturation")
        return event_dict
    try:
        if _CLOSING:
            _record_drop("shutdown")
        elif len(_LOG_BUFFER) >= _MAX_OWNED_ROWS:
            _record_drop("saturation")
        else:
            _LOG_BUFFER.append(_BufferedLog(entry, time.monotonic()))
    finally:
        _ADMISSION_LOCK.release()

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
            "owned_rows": len(_LOG_BUFFER),
            "inflight_rows": len(_INFLIGHT_BATCH),
            "maximum_owned_rows": _MAX_OWNED_ROWS,
            "drops": drops,
            "accepting": not _CLOSING,
        }


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
        sys.stderr.write(f"log sink flush failed: {type(exc).__name__}\n")
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

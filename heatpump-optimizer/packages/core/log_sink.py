"""Structlog processor that persists log entries to the app_logs table.

Usage in service entry points::

    from packages.core.log_sink import configure_structlog_with_db

    async def main():
        configure_structlog_with_db("optimizer")
        ...

The processor is non-blocking: log rows are inserted via a background task
so they never slow down the calling code.  Entries older than 24 h are
pruned automatically on each write to keep the table small.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

import structlog

from packages.core.config import settings

# Buffer log entries for batch insert (avoids one DB round-trip per log line)
_LOG_BUFFER: list[dict[str, Any]] = []
_BUFFER_LOCK = asyncio.Lock()
_FLUSH_INTERVAL = 2  # seconds
_SERVICE_NAME: str = "unknown"
_FLUSH_TASK: asyncio.Task[None] | None = None

# Retention: keep only the last 24 h of logs
_RETENTION_HOURS = 24


def _db_log_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
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

    details_json = json.dumps(details) if details else None

    entry = {
        "level": level,
        "logger_name": logger_name,
        "event": event,
        "details_json": details_json,
        "service": _SERVICE_NAME,
    }

    # Thread-safe append (structlog processors can be called from threads)
    _LOG_BUFFER.append(entry)

    return event_dict


async def _flush_loop() -> None:
    """Background task that flushes buffered log entries to the DB."""
    # Import here to avoid circular imports at module level
    from packages.core.database import get_session
    from packages.core.models import AppLogRecord

    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        if not _LOG_BUFFER:
            continue

        async with _BUFFER_LOCK:
            batch = list(_LOG_BUFFER)
            _LOG_BUFFER.clear()

        if not batch:
            continue

        try:
            async with get_session() as session:
                for entry in batch:
                    session.add(AppLogRecord(**entry))

                # Prune old entries
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=_RETENTION_HOURS)
                from sqlalchemy import delete
                await session.execute(
                    delete(AppLogRecord).where(AppLogRecord.ts < cutoff)
                )
        except Exception:
            # Never crash the flush loop — losing a few log entries is fine
            pass


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
    global _SERVICE_NAME
    _SERVICE_NAME = service_name

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

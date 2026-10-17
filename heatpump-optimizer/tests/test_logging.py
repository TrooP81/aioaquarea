import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core import logging as logging_module
from packages.core import log_sink


def test_configure_logging_delegates_to_log_sink(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        logging_module,
        "configure_structlog_with_db",
        lambda service_name: calls.append(service_name),
    )

    logging_module.configure_logging("optimizer")

    assert calls == ["optimizer"]


class _FailingSessionContext:
    async def __aenter__(self):
        raise RuntimeError("database unavailable")

    async def __aexit__(self, *args):
        return False


class _SuccessfulSessionContext:
    async def __aenter__(self):
        return SimpleNamespace(add=lambda _row: None, execute=AsyncMock())

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_flush_loop_reports_failure_to_stderr_and_continues(monkeypatch, capsys):
    log_sink._LOG_BUFFER.clear()
    sleep_count = 0
    session_contexts = iter([_FailingSessionContext(), _SuccessfulSessionContext()])

    async def sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count <= 2:
            log_sink._db_log_processor(None, "info", {"event": f"entry-{sleep_count}"})
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr("packages.core.database.get_session", lambda: next(session_contexts))

    with pytest.raises(asyncio.CancelledError):
        await log_sink._flush_loop()

    if log_sink._DIAGNOSTIC_THREAD is not None:
        await asyncio.to_thread(log_sink._DIAGNOSTIC_THREAD.join, 1)
        assert not log_sink._DIAGNOSTIC_THREAD.is_alive()
    captured = capsys.readouterr().err
    assert "log sink flush failed: RuntimeError" in captured
    assert "database unavailable" not in captured
    assert sleep_count == 3


def test_R35_total_ownership_includes_claimed_rows_and_bounds_batch(monkeypatch):
    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    for index in range(1000):
        log_sink._db_log_processor(None, "info", {"event": str(index)})
    batch = log_sink._claim_batch()
    assert len(batch) == 100
    assert len(log_sink._LOG_BUFFER) == 1000
    log_sink._db_log_processor(None, "info", {"event": "excess"})
    assert len(log_sink._LOG_BUFFER) == 1000
    assert log_sink._DROP_COUNTS["saturation"] == 1
    log_sink._acknowledge_batch(batch)
    assert len(log_sink._LOG_BUFFER) == 900
    log_sink._db_log_processor(None, "info", {"event": "new"})
    assert len(log_sink._LOG_BUFFER) == 901


def test_R35_oversize_and_unencodable_rows_do_not_enter_queue(monkeypatch):
    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    cyclic = {}
    cyclic["cycle"] = cyclic
    for details in ("large" * 8192, cyclic):
        log_sink._db_log_processor(None, "info", {"event": "invalid", "details": details})
    assert log_sink._LOG_BUFFER == []
    assert log_sink._DROP_COUNTS["oversize"] == 2


@pytest.mark.parametrize("field", ["event", "logger"])
def test_R35_invalid_utf8_is_counted_without_interrupting_producer(monkeypatch, field):
    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    event = {"event": "invalid", field: "\ud800"}

    assert log_sink._db_log_processor(None, "info", event) is event
    assert log_sink._LOG_BUFFER == []
    assert log_sink._DROP_COUNTS["oversize"] == 1

    valid = {"event": "valid"}
    assert log_sink._db_log_processor(None, "info", valid) is valid
    assert len(log_sink._LOG_BUFFER) == 1


def test_R35_retry_and_age_loss_are_explicit(monkeypatch):
    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    monkeypatch.setattr(log_sink.time, "monotonic", lambda: 100.0)
    log_sink._db_log_processor(None, "info", {"event": "retry"})
    for _ in range(3):
        assert len(log_sink._claim_batch()) == 1
    assert log_sink._claim_batch() == []
    assert log_sink._DROP_COUNTS["retry_age"] == 1
    log_sink._db_log_processor(None, "info", {"event": "aged"})
    monkeypatch.setattr(log_sink.time, "monotonic", lambda: 131.0)
    assert log_sink._claim_batch() == []
    assert log_sink._DROP_COUNTS["retry_age"] == 2


async def test_R35_transaction_exit_failure_cannot_acknowledge_batch(monkeypatch):
    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    log_sink._db_log_processor(None, "info", {"event": "retained"})
    batch = log_sink._claim_batch()

    class CommitFailure(_SuccessfulSessionContext):
        async def __aexit__(self, *args):
            raise RuntimeError("commit failed")

    monkeypatch.setattr("packages.core.database.get_session", CommitFailure)
    with pytest.raises(RuntimeError, match="commit failed"):
        await log_sink._write_batch(batch)
    assert len(log_sink._LOG_BUFFER) == 1
    monkeypatch.setattr("packages.core.database.get_session", _SuccessfulSessionContext)
    await log_sink._write_batch(batch)
    assert len(log_sink._LOG_BUFFER) == 1
    log_sink._acknowledge_batch(batch)
    assert log_sink._LOG_BUFFER == []

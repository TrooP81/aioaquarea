import asyncio

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
        return object()

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
            log_sink._LOG_BUFFER.append({"event": f"entry-{sleep_count}"})
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr("packages.core.database.get_session", lambda: next(session_contexts))

    with pytest.raises(asyncio.CancelledError):
        await log_sink._flush_loop()

    assert "log sink flush failed: RuntimeError: database unavailable" in capsys.readouterr().err
    assert sleep_count == 3

"""Shared pytest compatibility shims for backend tests."""

from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import warnings
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio

TEST_API_TOKEN = "test-token"

os.environ.setdefault("API_TOKEN", TEST_API_TOKEN)

import aioaquarea  # noqa: E402


def pytest_configure() -> None:
    """Force test temp files into a writable repo-local path on Windows."""
    if os.name != "nt":
        return

    local_tmp = Path(__file__).resolve().parents[1] / ".pytest_tmp"
    local_tmp.mkdir(parents=True, exist_ok=True)

    os.environ["TMPDIR"] = str(local_tmp)
    os.environ["TEMP"] = str(local_tmp)
    os.environ["TMP"] = str(local_tmp)
    tempfile.tempdir = str(local_tmp)
    warnings.filterwarnings(
        "ignore",
        message=r"Exception ignored in: <coroutine object Connection\._cancel .*",
        category=pytest.PytestUnraisableExceptionWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"coroutine 'Connection\._cancel' was never awaited",
        category=RuntimeWarning,
    )


def pytest_sessionfinish(session, exitstatus):
    """Reclaim lingering async resources before pytest processes unraisables."""
    gc.collect()


@pytest.hookimpl(tryfirst=True)
def pytest_unconfigure(config):
    """Run a final GC pass before pytest collects unraisable exceptions."""
    gc.collect()


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _shutdown_db_log_sink():
    """Stop the background DB log flusher before pytest tears down the loop."""
    yield

    from packages.core import log_sink

    task = getattr(log_sink, "_FLUSH_TASK", None)
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    log_sink._FLUSH_TASK = None


# The optimizer environment currently installs an aioaquarea release that
# predates DeviceUnavailableError; retain this isolated test compatibility shim.
if not hasattr(aioaquarea, "DeviceUnavailableError") and hasattr(
    aioaquarea, "DataNotAvailableError"
):

    class DeviceUnavailableErrorCompat(aioaquarea.DataNotAvailableError):
        """Backfill missing aioaquarea.DeviceUnavailableError for older versions."""

        def __init__(self, device_id: str, reason: str | None = None) -> None:
            self.device_id = device_id
            self.reason = reason
            super().__init__(
                "Failed to retrieve live device status: "
                f"Panasonic adaptor unavailable for device {device_id}"
            )

    aioaquarea.DeviceUnavailableError = DeviceUnavailableErrorCompat

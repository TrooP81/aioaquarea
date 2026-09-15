"""Shared pytest compatibility shims for backend tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import aioaquarea


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

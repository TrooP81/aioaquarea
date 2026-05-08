"""Shared logging bootstrap helpers for service entrypoints."""

from __future__ import annotations

from packages.core.log_sink import configure_structlog_with_db


def configure_logging(service_name: str) -> None:
    """Configure application logging for a named service."""
    configure_structlog_with_db(service_name)

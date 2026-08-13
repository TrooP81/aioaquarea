"""Shared UTC and overlap-safe scheduler configuration."""

from __future__ import annotations

import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler


UTC = dt.timezone.utc

# A stalled event loop must not launch a burst of catch-up jobs, and a slow
# poll or solver must never overlap another instance of itself.
DEFAULT_JOB_DEFAULTS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 60,
}


def utc_now() -> dt.datetime:
    """Return an aware UTC timestamp for scheduler boundaries."""

    return dt.datetime.now(UTC)


def utc_after(**delta: float) -> dt.datetime:
    """Return an aware UTC timestamp offset by a ``timedelta``."""

    return utc_now() + dt.timedelta(**delta)


def create_scheduler() -> AsyncIOScheduler:
    """Create the common scheduler used by all long-running services."""

    return AsyncIOScheduler(
        timezone=UTC,
        job_defaults=DEFAULT_JOB_DEFAULTS,
    )

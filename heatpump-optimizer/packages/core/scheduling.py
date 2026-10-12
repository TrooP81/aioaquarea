"""Shared UTC and overlap-safe scheduler configuration."""

from __future__ import annotations

import datetime as dt
import asyncio
import functools
import inspect

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


class OwnedScheduler(AsyncIOScheduler):
    """Track every coroutine job until its cancellation cleanup actually terminates."""

    def __init__(self, **options):
        super().__init__(**options)
        self.owned_tasks = set()
        self.closing = False

    def add_job(self, func, *args, **kwargs):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("Service scheduler jobs must be async owned coroutines")

        @functools.wraps(func)
        async def owned_job(*job_args, **job_kwargs):
            if self.closing:
                return None
            task = asyncio.current_task()
            self.owned_tasks.add(task)
            try:
                return await func(*job_args, **job_kwargs)
            finally:
                self.owned_tasks.discard(task)

        return super().add_job(owned_job, *args, **kwargs)

    async def close(self):
        self.closing = True
        if self.running:
            self.pause()
        pending = [task for task in self.owned_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.running:
            self.shutdown(wait=False)


def create_scheduler() -> OwnedScheduler:
    """Create the common scheduler used by all long-running services."""

    return OwnedScheduler(
        timezone=UTC,
        job_defaults=DEFAULT_JOB_DEFAULTS,
    )

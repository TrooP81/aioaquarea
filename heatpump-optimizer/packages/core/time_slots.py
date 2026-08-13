"""Helpers for scheduling work into complete market-price intervals."""

from __future__ import annotations

import datetime as dt


def next_hour_boundary(now: dt.datetime) -> dt.datetime:
    """Return the first whole UTC hour that has not already started.

    Price and weather inputs are hourly. Starting a plan at ``HH:00`` after
    that hour has begun creates actions in the past, which an executor may
    otherwise attempt to catch up on. At an exact boundary that boundary is
    still valid; at every other instant the next hour is the first executable
    interval.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)

    hour = now.replace(minute=0, second=0, microsecond=0)
    return hour if now == hour else hour + dt.timedelta(hours=1)

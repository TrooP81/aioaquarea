"""Clock-independent timestamps for the seeded E2E consumption fixture.

The dashboard counts only rows whose timestamp lies in the user's current
local day and whose source date equals that local date. Seeding at
``now - h hours`` without a source date fails shortly after local midnight,
because the UTC fallback date and the earlier samples then belong to the
previous day. These helpers keep every sample inside ``[local midnight, now]``.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# Seeding and the subsequent API request must happen on the same local day.
MIDNIGHT_GUARD = dt.timedelta(seconds=60)


def _aware_utc(now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(dt.timezone.utc)


def local_day_bounds(now: dt.datetime, tz_name: str) -> tuple[dt.datetime, dt.datetime]:
    """Return the UTC start of the current local day and of the next one."""

    local = _aware_utc(now).astimezone(ZoneInfo(tz_name))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_start = dt.datetime.combine(
        local.date() + dt.timedelta(days=1), dt.time.min, tzinfo=local.tzinfo
    )
    return start.astimezone(dt.timezone.utc), next_start.astimezone(dt.timezone.utc)


def time_until_local_midnight(now: dt.datetime, tz_name: str) -> dt.timedelta:
    return local_day_bounds(now, tz_name)[1] - _aware_utc(now)


def same_day_seed_timestamps(
    now: dt.datetime, tz_name: str, count: int
) -> list[tuple[dt.datetime, dt.date]]:
    """Return ``count`` (ts, source_date) pairs, newest first, within the local day.

    Samples are hourly when the elapsed part of the day allows it and are
    compressed evenly between local midnight and ``now`` otherwise.
    """

    if count < 1:
        raise ValueError("count must be positive")
    now_utc = _aware_utc(now)
    day_start, _ = local_day_bounds(now_utc, tz_name)
    source_date = now_utc.astimezone(ZoneInfo(tz_name)).date()
    step = dt.timedelta(hours=1)
    if count > 1:
        # Floor division keeps the oldest sample at or after local midnight.
        step = min(step, (now_utc - day_start) // (count - 1))
    return [(now_utc - index * step, source_date) for index in range(count)]

"""Offline checks that the E2E consumption seed is independent of time of day."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from packages.core.plan_outcome import effective_source_date
from packages.core.settings_service import local_day_start_utc
from tests.e2e import consumption_seed

STOCKHOLM = "Europe/Stockholm"
UTC = dt.timezone.utc


def _local(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ZoneInfo(STOCKHOLM)).astimezone(UTC)


def _dashboard_today_rows(rows, now: dt.datetime, tz_name: str):
    """Mirror the /api/dashboard consumption filter for today's rows."""

    today_start = local_day_start_utc(now, tz_name)
    today = now.astimezone(ZoneInfo(tz_name)).date()
    return [
        row for row in rows if today_start <= row.ts <= now and effective_source_date(row) == today
    ]


def _seed_rows(now: dt.datetime, tz_name: str):
    points = consumption_seed.same_day_seed_timestamps(now, tz_name, 6)
    return [
        SimpleNamespace(ts=ts, source_date=source_date, heat_kwh=1.7 - h * 0.1, tank_kwh=0.5)
        for h, (ts, source_date) in enumerate(points)
    ]


@pytest.mark.parametrize(
    "now",
    [
        # PR #35 CI failure window: 22:00-22:02 UTC == 00:00-00:02 CEST.
        _local(2026, 10, 17, 0, 0, 0),
        _local(2026, 10, 17, 0, 1, 0),
        _local(2026, 10, 17, 0, 2, 0),
        _local(2026, 10, 17, 1, 59, 0),
        _local(2026, 10, 17, 12, 0, 0),
        _local(2026, 10, 17, 23, 59, 0),
        _local(2026, 10, 17, 23, 59, 59, 999999),
        # Winter time and both DST transition days.
        _local(2026, 1, 15, 0, 1, 0),
        _local(2026, 3, 29, 0, 1, 0),
        _local(2026, 3, 29, 3, 30, 0),
        _local(2026, 10, 25, 0, 1, 0),
        _local(2026, 10, 25, 2, 30, 0),
    ],
)
def test_seed_stays_within_local_today(now: dt.datetime) -> None:
    rows = _seed_rows(now, STOCKHOLM)

    today_rows = _dashboard_today_rows(rows, now, STOCKHOLM)

    assert len(today_rows) == len(rows) == 6
    newest = max(today_rows, key=lambda row: row.ts)
    assert newest.ts == now
    assert newest.heat_kwh + newest.tank_kwh > 0
    timestamps = [row.ts for row in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_seed_keeps_hourly_spacing_when_the_day_allows_it() -> None:
    now = _local(2026, 10, 17, 12, 0, 0)

    points = consumption_seed.same_day_seed_timestamps(now, STOCKHOLM, 6)

    assert [ts for ts, _ in points] == [now - dt.timedelta(hours=h) for h in range(6)]


def test_seed_compresses_just_after_midnight() -> None:
    now = _local(2026, 10, 17, 0, 1, 0)

    points = consumption_seed.same_day_seed_timestamps(now, STOCKHOLM, 6)

    assert points[-1][0] == _local(2026, 10, 17, 0, 0, 0)
    assert {source_date for _, source_date in points} == {dt.date(2026, 10, 17)}


def test_legacy_utc_seed_reproduces_the_midnight_failure() -> None:
    now = _local(2026, 10, 17, 0, 1, 0)
    legacy = [SimpleNamespace(ts=now - dt.timedelta(hours=h), source_date=None) for h in range(6)]

    assert _dashboard_today_rows(legacy, now, STOCKHOLM) == []


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_local(2026, 10, 17, 23, 59, 30), dt.timedelta(seconds=30)),
        (_local(2026, 10, 17, 0, 0, 0), dt.timedelta(hours=24)),
        # The DST fall-back day lasts 25 hours.
        (_local(2026, 10, 25, 0, 0, 0), dt.timedelta(hours=25)),
    ],
)
def test_time_until_local_midnight(now: dt.datetime, expected: dt.timedelta) -> None:
    assert consumption_seed.time_until_local_midnight(now, STOCKHOLM) == expected


def test_midnight_guard_covers_seed_to_request_gap() -> None:
    near_midnight = _local(2026, 10, 17, 23, 59, 30)

    remaining = consumption_seed.time_until_local_midnight(near_midnight, STOCKHOLM)

    assert remaining < consumption_seed.MIDNIGHT_GUARD


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        consumption_seed.same_day_seed_timestamps(dt.datetime(2026, 10, 17), STOCKHOLM, 6)

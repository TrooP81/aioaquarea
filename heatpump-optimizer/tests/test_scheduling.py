"""Tests for UTC and overlap-safe background scheduling."""

from __future__ import annotations

import datetime as dt

import pytest

from packages.core.scheduling import create_scheduler, utc_after, utc_now


def test_scheduler_timestamps_are_aware_utc() -> None:
    now = utc_now()
    later = utc_after(seconds=30)

    assert now.tzinfo is dt.timezone.utc
    assert later.tzinfo is dt.timezone.utc
    assert dt.timedelta(seconds=29) <= later - now <= dt.timedelta(seconds=31)


@pytest.mark.asyncio
async def test_scheduler_coalesces_misfires_and_prevents_overlap() -> None:
    async def noop() -> None:
        return None

    scheduler = create_scheduler()
    scheduler.start(paused=True)
    try:
        job = scheduler.add_job(
            noop,
            "interval",
            seconds=60,
            id="test-job",
            next_run_time=utc_after(seconds=5),
        )

        assert scheduler.timezone is dt.timezone.utc
        assert job.next_run_time.tzinfo is dt.timezone.utc
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 60
    finally:
        scheduler.shutdown(wait=False)

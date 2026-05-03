"""Tests for settings_service pure functions."""

import datetime as dt

import pytest

from packages.core.settings_service import is_comfort_hour, dhw_deadlines_from_schedule

TZ = "UTC"  # Tests use UTC timestamps with UTC-aligned schedule hours


class TestIsComfortHour:
    def test_weekday_in_schedule(self):
        schedule = {"weekday": [7, 8, 9, 17, 18, 19], "weekend": [8, 9, 10]}
        # Wednesday at 8am
        ts = dt.datetime(2026, 4, 29, 8, 30, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is True

    def test_weekday_not_in_schedule(self):
        schedule = {"weekday": [7, 8, 9, 17, 18, 19], "weekend": [8, 9, 10]}
        # Wednesday at 3am
        ts = dt.datetime(2026, 4, 29, 3, 0, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is False

    def test_weekend_in_schedule(self):
        schedule = {"weekday": [7, 8, 9], "weekend": [8, 9, 10, 11]}
        # Saturday at 10am
        ts = dt.datetime(2026, 5, 2, 10, 0, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is True

    def test_weekend_not_in_schedule(self):
        schedule = {"weekday": [7, 8, 9], "weekend": [8, 9, 10, 11]}
        # Sunday at 6am
        ts = dt.datetime(2026, 5, 3, 6, 0, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is False

    def test_empty_schedule(self):
        schedule = {"weekday": [], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 8, 0, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is False

    def test_missing_day_type_key(self):
        schedule = {}
        ts = dt.datetime(2026, 4, 29, 8, 0, tzinfo=dt.timezone.utc)
        assert is_comfort_hour(schedule, ts, tz_name=TZ) is False


class TestDhwDeadlines:
    def test_single_contiguous_block(self):
        schedule = {"weekday": [7, 8, 9, 10], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)  # Wednesday
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [7]

    def test_two_blocks(self):
        schedule = {"weekday": [7, 8, 9, 17, 18, 19], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [7, 17]

    def test_three_blocks(self):
        schedule = {"weekday": [6, 7, 12, 13, 20, 21, 22], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [6, 12, 20]

    def test_empty_schedule_returns_empty(self):
        schedule = {"weekday": [], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == []

    def test_weekend_uses_weekend_hours(self):
        schedule = {"weekday": [7, 8], "weekend": [9, 10, 11, 15, 16]}
        ts = dt.datetime(2026, 5, 2, 0, 0, tzinfo=dt.timezone.utc)  # Saturday
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [9, 15]

    def test_unsorted_hours_are_handled(self):
        schedule = {"weekday": [9, 7, 8, 18, 17], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)
        # Should still work correctly due to sorted(set(...))
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [7, 17]

    def test_duplicate_hours_handled(self):
        schedule = {"weekday": [7, 7, 8, 8, 9], "weekend": []}
        ts = dt.datetime(2026, 4, 29, 0, 0, tzinfo=dt.timezone.utc)
        assert dhw_deadlines_from_schedule(schedule, ts, tz_name=TZ) == [7]

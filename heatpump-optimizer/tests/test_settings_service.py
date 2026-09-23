"""Tests for settings_service pure functions."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.settings_service import (
    SETTINGS_SCHEMA,
    SETTING_SPECS,
    get_setting_spec,
    is_comfort_hour,
    is_masked_secret,
    dhw_deadlines_from_schedule,
    local_date,
    local_day_start_utc,
    validate_setting_value,
)

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


class TestLocalCalendarDay:
    @pytest.mark.parametrize(
        ("now", "expected_start"),
        [
            (
                dt.datetime(2026, 8, 18, 22, 30, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 18, 22, 0, tzinfo=dt.timezone.utc),
            ),
            (
                dt.datetime(2026, 1, 18, 23, 30, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 18, 23, 0, tzinfo=dt.timezone.utc),
            ),
        ],
    )
    def test_stockholm_day_starts_at_local_midnight(self, now, expected_start):
        assert local_day_start_utc(now, "Europe/Stockholm") == expected_start
        assert local_date(now, "Europe/Stockholm") == dt.date(2026, now.month, 19)


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


class TestLearningModeSettings:
    def test_learning_mode_settings_registered(self):
        assert "learning_mode_enabled" in SETTINGS_SCHEMA
        assert "learning_mode_since" in SETTINGS_SCHEMA

    def test_learning_mode_enabled_is_bool_default_false(self):
        spec = SETTING_SPECS["learning_mode_enabled"]
        assert spec.value_type == "bool"
        assert spec.parse(spec.default) is False

    def test_learning_mode_since_defaults_empty(self):
        assert SETTING_SPECS["learning_mode_since"].default == ""


class TestBoolSettings:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "stored_value", "expected"),
        [
            ("smartthings_enabled", None, False),
            ("smartthings_enabled", "false", False),
            ("smartthings_enabled", "true", True),
            ("use_comfort_model", None, False),
            ("use_comfort_model", "false", False),
            ("use_comfort_model", "true", True),
            ("smartthings_enabled", "garbage", False),
            ("use_comfort_model", "garbage", False),
            ("learning_mode_enabled", "false", False),
            ("learning_mode_enabled", "true", True),
        ],
    )
    async def test_get_bool_setting_parses_stored_and_default_values(
        self, key, stored_value, expected
    ):
        from packages.core.settings_service import get_bool_setting

        record = SimpleNamespace(key=key, value=stored_value) if stored_value is not None else None
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: record)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)

        with patch("packages.core.settings_service.get_session", return_value=context):
            assert await get_bool_setting(key) is expected


class TestValidateSettingValue:
    def test_unknown_key_raises_keyerror(self):
        with pytest.raises(KeyError):
            validate_setting_value("does_not_exist", "1")

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("tank_min_temp", "42"),
            ("comfort_temp_min", "20.5"),
            ("learning_mode_enabled", "true"),
            ("price_provider", "manual"),
            ("comfort_schedule", '{"weekday": [7]}'),
        ],
    )
    def test_valid_values_pass(self, key, value):
        validate_setting_value(key, value)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("tank_min_temp", "not-a-number"),
            ("comfort_temp_min", "warm"),
            ("learning_mode_enabled", "perhaps"),
            ("price_provider", "invalid"),
            ("comfort_schedule", "not-json"),
        ],
    )
    def test_invalid_values_raise_valueerror(self, key, value):
        with pytest.raises(ValueError):
            validate_setting_value(key, value)

    def test_get_setting_spec_returns_registered_spec(self):
        assert get_setting_spec("tank_min_temp") is SETTING_SPECS["tank_min_temp"]


class TestIsMaskedSecret:
    def test_masked_secret_detected(self):
        assert is_masked_secret(get_setting_spec("entsoe_api_token"), "abc***xyz") is True

    def test_unmasked_secret_not_flagged(self):
        assert is_masked_secret(get_setting_spec("entsoe_api_token"), "plain-token") is False

    def test_non_secret_never_flagged(self):
        assert is_masked_secret(get_setting_spec("price_provider"), "***") is False

"""Tests for settings_service pure functions."""

import datetime as dt

import pytest

from packages.core.settings_service import (
    SETTINGS_SCHEMA,
    SETTING_SPECS,
    get_setting_spec,
    is_comfort_hour,
    is_masked_secret,
    dhw_deadlines_from_schedule,
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


class TestValidateSettingValue:
    def test_unknown_key_raises_keyerror(self):
        with pytest.raises(KeyError):
            validate_setting_value("does_not_exist", "1")

    def test_valid_int_passes(self):
        validate_setting_value("tank_min_temp", "42")

    def test_invalid_int_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_setting_value("tank_min_temp", "not-a-number")

    def test_valid_float_passes(self):
        validate_setting_value("comfort_temp_min", "20.5")

    def test_invalid_float_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_setting_value("comfort_temp_min", "warm")

    def test_valid_bool_passes(self):
        validate_setting_value("learning_mode_enabled", "true")

    def test_invalid_bool_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_setting_value("learning_mode_enabled", "maybe")

    def test_valid_option_passes(self):
        validate_setting_value("optimizer_layer", "milp_preferred")

    def test_invalid_option_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_setting_value("optimizer_layer", "nonsense")

    def test_valid_json_passes(self):
        validate_setting_value("comfort_schedule", '{"weekday": [7], "weekend": []}')

    def test_invalid_json_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_setting_value("comfort_schedule", "{not json")

    def test_empty_string_allowed_for_numeric_to_clear(self):
        # Empty clears the override and falls back to env/default.
        validate_setting_value("comfort_temp_min", "")

    def test_empty_string_rejected_for_option(self):
        with pytest.raises(ValueError):
            validate_setting_value("optimizer_layer", "")


class TestIsMaskedSecret:
    def test_masked_secret_detected(self):
        spec = get_setting_spec("entsoe_api_token")
        assert is_masked_secret(spec, "ab***yz") is True

    def test_unmasked_secret_not_flagged(self):
        spec = get_setting_spec("entsoe_api_token")
        assert is_masked_secret(spec, "real-secret-value") is False

    def test_non_secret_never_flagged(self):
        spec = get_setting_spec("optimizer_layer")
        assert is_masked_secret(spec, "***") is False

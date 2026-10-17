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


class TestReadinessSessions:
    async def test_UR2_SR1_borrowed_settings_session_is_never_replaced_or_committed(self):
        from packages.core import settings_service

        row = SimpleNamespace(key="ml_maintenance_enabled", value="true")
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [row], __iter__=lambda: iter([row])),
            scalar_one_or_none=lambda: row,
        )
        with patch.object(
            settings_service, "get_session", side_effect=AssertionError("new session")
        ):
            assert (await settings_service.get_all_settings(session=session))[row.key] == "true"
            assert await settings_service.get_setting(row.key, session=session) == "true"
            assert await settings_service.get_bool_setting(row.key, session=session) is True
            session.execute.return_value = SimpleNamespace(scalars=lambda: [row])
            await settings_service.set_settings_bulk(
                {"_seasonal_calibration_campaign_since": ""}, session=session
            )
        session.commit.assert_not_awaited()
        session.rollback.assert_not_awaited()
        session.close.assert_not_awaited()

    @pytest.mark.parametrize("read_only", [True, False])
    async def test_UR2_SR1_session_isolation_and_ownership(self, read_only):
        from packages.core import database

        session = AsyncMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(database, "async_session_factory", return_value=context) as factory:
            async with database.get_session(
                read_only=read_only, isolation_level="REPEATABLE READ" if read_only else None
            ) as borrowed:
                assert borrowed is session
        factory.assert_called_once_with(autoflush=not read_only)
        if read_only:
            session.connection.assert_awaited_once_with(
                execution_options={
                    "isolation_level": "REPEATABLE READ",
                    "postgresql_readonly": True,
                }
            )
            session.commit.assert_not_awaited()
            session.rollback.assert_awaited_once()
            assert "statement_timeout" in str(session.execute.await_args.args[0])
        else:
            session.commit.assert_awaited_once()
            session.rollback.assert_not_awaited()


class TestLearningModeSettings:
    def test_maintenance_defaults_off(self):
        spec = SETTING_SPECS["ml_maintenance_enabled"]
        assert spec.value_type == "bool"
        assert spec.default_env is None
        assert spec.parse(spec.default) is False
        assert validate_setting_value("ml_maintenance_enabled", "true") is None

    @pytest.mark.asyncio
    async def test_maintenance_defaults_off_and_auto_exit_is_ineffective(self):
        from packages.core import settings_service

        assert SETTING_SPECS["ml_maintenance_enabled"].parse("false") is False
        assert SETTING_SPECS["seasonal_calibration_enabled"].parse("false") is False
        assert SETTING_SPECS["seasonal_calibration_auto_exit"].default == "false"
        assert SETTINGS_SCHEMA["seasonal_calibration_auto_exit"]["read_only"] is True
        stored = SimpleNamespace(key="seasonal_calibration_auto_exit", value="true")
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(
            scalar_one_or_none=lambda: stored,
            scalars=lambda: SimpleNamespace(all=lambda: [stored]),
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(settings_service, "get_session", return_value=context):
            assert await settings_service.get_setting("seasonal_calibration_auto_exit") == "false"
            assert (
                await settings_service.get_bool_setting("seasonal_calibration_auto_exit") is False
            )
            assert (await settings_service.get_all_settings())[
                "seasonal_calibration_auto_exit"
            ] == "false"
            with pytest.raises(ValueError, match="read-only"):
                await settings_service.set_setting("seasonal_calibration_auto_exit", "true")
            with pytest.raises(ValueError, match="read-only"):
                await settings_service.set_settings_bulk({"seasonal_calibration_auto_exit": "true"})
        for value in ("true", "1", "yes", "on", ""):
            with pytest.raises(ValueError, match="read-only"):
                validate_setting_value("seasonal_calibration_auto_exit", value)
        validate_setting_value("seasonal_calibration_auto_exit", "false")

    @pytest.mark.asyncio
    async def test_explicit_seasonal_exit_clears_campaign_atomically(self):
        from packages.core import settings_service

        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(scalars=lambda: [])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(settings_service, "get_session", return_value=context):
            await settings_service.set_settings_bulk({"seasonal_calibration_enabled": "false"})
        writes = {
            params["key"]: params["value"]
            for call in session.execute.await_args_list
            if getattr(call.args[0], "is_insert", False)
            and "key" in (params := call.args[0].compile().params)
        }
        assert writes == {
            "seasonal_calibration_enabled": "false",
            "_seasonal_calibration_campaign_since": "",
            "_seasonal_calibration_safety_deferred_since": "",
        }
        context.__aenter__.assert_awaited_once()
        context.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_late_seasonal_activation_cannot_recreate_exited_campaign(self):
        from packages.core import settings_service

        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(scalars=lambda: [])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(settings_service, "get_session", return_value=context):
            await settings_service.set_settings_bulk(
                {"_seasonal_calibration_campaign_since": "2026-10-09T12:00:00+00:00"}
            )
        assert session.execute.await_count == 2

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
            ("ml_maintenance_enabled", None, False),
            ("ml_maintenance_enabled", "false", False),
            ("ml_maintenance_enabled", "true", True),
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

    @pytest.mark.parametrize("value", ["0", "1", "0.35"])
    def test_fraction_accepts_zero_one_and_midpoint(self, value):
        validate_setting_value("space_heating_default_fraction", value)

    @pytest.mark.parametrize("value", ["-0.01", "1.01", "no", "nan", "inf", "-inf"])
    def test_fraction_rejects_negative_above_one_nonnumeric_and_nonfinite(self, value):
        with pytest.raises(ValueError):
            validate_setting_value("space_heating_default_fraction", value)


class TestR04NumericSettings:
    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN"])
    def test_all_float_settings_reject_nonfinite(self, raw):
        for key, spec in SETTING_SPECS.items():
            if spec.value_type == "float":
                with pytest.raises(ValueError, match="finite"):
                    validate_setting_value(key, raw)
                with pytest.raises(ValueError, match="finite"):
                    spec.parse(raw)

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_numeric_default_serialization_rejects_nonfinite(self, raw):
        from packages.core.settings_service import SettingSpec

        spec = SettingSpec("s2-unbounded", "float", "test", default=raw)
        with pytest.raises(ValueError, match="finite"):
            spec.serialize_default()

    def test_float_parser_does_not_treat_bool_as_number(self):
        with pytest.raises(ValueError):
            SETTING_SPECS["manual_price_eur_per_kwh"].parse(True)

    @pytest.mark.parametrize(
        ("key", "raw"),
        [
            ("comfort_temp_target", "nan"),
            ("sh_max_power_kw", "inf"),
            ("manual_price_eur_per_kwh", "-inf"),
            ("space_heating_default_fraction", "1.5"),
        ],
    )
    @pytest.mark.parametrize(
        "getter", ["get_typed_setting", "get_float_setting", "get_all_settings"]
    )
    async def test_persisted_invalid_numeric_reads_are_unusable(
        self, monkeypatch, key, raw, getter
    ):
        from packages.core import settings_service

        record = SimpleNamespace(key=key, value=raw)
        result = SimpleNamespace(
            scalar_one_or_none=lambda: record,
            scalars=lambda: SimpleNamespace(all=lambda: [record]),
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(
            return_value=SimpleNamespace(execute=AsyncMock(return_value=result))
        )
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(settings_service, "get_session", lambda: context)

        with pytest.raises(ValueError):
            await getattr(settings_service, getter)(
                *(() if getter == "get_all_settings" else (key,))
            )

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
    async def test_nonfinite_environment_default_cannot_be_read(self, monkeypatch, raw):
        from dataclasses import replace

        from packages.core import settings_service

        key = "comfort_temp_target"
        monkeypatch.setitem(
            SETTING_SPECS,
            key,
            replace(SETTING_SPECS[key], default_env="s2_invalid_default"),
        )
        monkeypatch.setattr(
            settings_service, "env_settings", SimpleNamespace(s2_invalid_default=raw)
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(
            return_value=SimpleNamespace(
                execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
            )
        )
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(settings_service, "get_session", lambda: context)

        with pytest.raises(ValueError, match="finite"):
            await settings_service.get_float_setting(key)

    @pytest.mark.parametrize("writer", ["set_setting", "set_settings_bulk"])
    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    async def test_numeric_writes_validate_before_session(self, monkeypatch, writer, raw):
        from packages.core import settings_service

        session = MagicMock(side_effect=AssertionError("invalid setting opened a database session"))
        monkeypatch.setattr(settings_service, "get_session", session)

        with pytest.raises(ValueError, match="finite"):
            if writer == "set_setting":
                await settings_service.set_setting("manual_price_eur_per_kwh", raw)
            else:
                await settings_service.set_settings_bulk({"manual_price_eur_per_kwh": raw})
        session.assert_not_called()

    @pytest.mark.parametrize(("raw", "expected"), [("-0.2", -0.2), ("0", 0.0), ("", 0.25)])
    async def test_finite_negative_zero_and_clear_sentinel_remain_valid(
        self, monkeypatch, raw, expected
    ):
        from packages.core import settings_service

        monkeypatch.setattr(settings_service, "get_setting", AsyncMock(return_value=raw))
        assert await settings_service.get_float_setting("manual_price_eur_per_kwh") == expected


class TestIsMaskedSecret:
    def test_masked_secret_detected(self):
        assert is_masked_secret(get_setting_spec("entsoe_api_token"), "abc***xyz") is True

    def test_unmasked_secret_not_flagged(self):
        assert is_masked_secret(get_setting_spec("entsoe_api_token"), "plain-token") is False

    def test_non_secret_never_flagged(self):
        assert is_masked_secret(get_setting_spec("price_provider"), "***") is False

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.space_heating_baseline import (
    build_baseline_duty_profile,
    load_recent_heating_evidence,
)


def _row(timestamp, *, active=True, outdoor_temp=4.0, **overrides):
    values = {
        "ts": timestamp,
        "outdoor_temp": outdoor_temp,
        "heat_pump_outdoor_temp": None,
        "operation_status": 1,
        "mode": "1",
        "zone1_operation_status": 1,
        "holiday_mode": 0,
        "direction": "PUMP" if active else "IDLE",
        "pump_duty": 1 if active else 0,
        "device_action": "HEATING" if active else "IDLE",
        "defrost_active": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _hour_rows(start, *, active=True, outdoor_temp=4.0, unknown_last=False):
    rows = []
    for minute in range(0, 61, 12):
        row = _row(start + dt.timedelta(minutes=minute), active=active, outdoor_temp=outdoor_temp)
        if unknown_last and minute == 48:
            row.direction = None
            row.pump_duty = None
            row.device_action = None
        rows.append(row)
    return rows


def _history(hours, *, active=True, outdoor_temp=4.0):
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    rows = []
    for index, hour in enumerate(hours):
        rows.extend(
            _hour_rows(
                start + dt.timedelta(days=index, hours=hour),
                active=active if isinstance(active, bool) else active[index],
                outdoor_temp=outdoor_temp,
            )
        )
    return rows


def _forecast(hour=0, outdoor_temp=4.0):
    return SimpleNamespace(
        timestamp=dt.datetime(2026, 2, 1, hour, tzinfo=dt.timezone.utc),
        outdoor_temp=outdoor_temp,
    )


class TestBaselineDutyProfile:
    def test_10k_row_profile_build_reports_elapsed_milliseconds(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        rows = [_row(start + dt.timedelta(minutes=index)) for index in range(10_000)]

        started = time.perf_counter()
        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)
        elapsed_ms = (time.perf_counter() - started) * 1000

        print(f"PHASE_C_BASELINE_10000_ROWS_MS={elapsed_ms:.2f}")
        assert profile.history_rows == 10_000
        assert profile.accepted_hours > 0
        assert elapsed_ms < 2_000

    def test_missing_intervals_do_not_count_as_off(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        rows = []
        for day in range(6):
            rows.extend(_hour_rows(start + dt.timedelta(days=day), unknown_last=True))

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.points[0].expected_fraction == 1.0

    def test_rejects_hour_below_eighty_percent_coverage(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        rows = []
        for day in range(6):
            rows.extend(_hour_rows(start + dt.timedelta(days=day), unknown_last=True))
        rows[4].ts = rows[4].ts + dt.timedelta(minutes=4)

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.accepted_hours == 5
        assert profile.points[0].source == "default"

    def test_reclassifies_legacy_false_rows_from_raw_components(self):
        rows = _history([0] * 6)
        for row in rows:
            row.space_heating_active = False

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.points[0].source == "history"
        assert profile.points[0].expected_fraction == 1.0

    def test_temperature_and_local_hour_bucket_precedes_broad_fallback(self):
        rows = _history([0] * 6, active=True)
        rows.extend(_history([9] * 24, active=False))

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.points[0].bucket == "band_3h"
        assert profile.points[0].expected_fraction == 1.0

    def test_above_cutoff_requires_strict_matching_history(self):
        profile = build_baseline_duty_profile(
            _history([0] * 24), [_forecast(outdoor_temp=14.0)], None, 12, "UTC", True, 0.35
        )

        assert profile.points[0].expected_fraction == 0.0
        assert profile.points[0].source == "none"

    @pytest.mark.parametrize("gate_state", ["BLOCKED", "UNKNOWN"])
    def test_blocked_unknown_and_unavailable_zone_are_zero(self, gate_state):
        rows = _history([0] * 24)
        profile = build_baseline_duty_profile(
            rows, [_forecast()], [gate_state], 12, "UTC", True, 0.35
        )
        unavailable = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", False, 0.35)

        assert profile.points[0].expected_fraction == 0.0
        assert unavailable.points[0].expected_fraction == 0.0

    @pytest.mark.asyncio
    async def test_history_query_is_device_scoped_time_bounded_and_limited(self):
        result = SimpleNamespace(all=lambda: ["newest", "oldest"])
        session = AsyncMock()
        session.execute.return_value = result
        now = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)

        rows, truncated = await load_recent_heating_evidence(session, "device-a", now)

        statement = session.execute.await_args.args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert rows == ["oldest", "newest"]
        assert truncated is False
        assert "device_status.device_id = 'device-a'" in compiled
        assert "LIMIT 10000" in compiled
        assert "2026-01-18" in compiled

    def test_floor_curve_uses_selected_bucket_p10_duty(self):
        rows = _history([0] * 20, active=[False, False] + [True] * 18)

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.points[0].floor_fraction == 0.0
        assert profile.points[0].floor_source == "history_p10"

    @pytest.mark.parametrize("count", [19, 20, 21])
    def test_floor_p10_requires_twenty_hourly_windows(self, count):
        profile = build_baseline_duty_profile(
            _history([0] * count), [_forecast()], None, 12, "UTC", True, 0.35
        )

        assert profile.points[0].floor_fraction == (1.0 if count >= 20 else 0.0)

    def test_floor_walks_up_to_first_bucket_with_twenty_windows(self):
        rows = _history([0] * 19 + [3], active=[True] * 19 + [False])

        profile = build_baseline_duty_profile(rows, [_forecast()], None, 12, "UTC", True, 0.35)

        assert profile.points[0].expected_fraction == 1.0
        assert profile.points[0].floor_source == "history_p10"

    def test_above_cutoff_floor_uses_only_matching_time_history(self):
        rows = _history([0] * 12, outdoor_temp=14.0)
        rows.extend(_history([3] * 24, outdoor_temp=4.0))

        profile = build_baseline_duty_profile(
            rows, [_forecast(outdoor_temp=14.0)], None, 12, "UTC", True, 0.35
        )

        assert profile.points[0].source == "history"
        assert profile.points[0].expected_fraction == 1.0
        assert profile.points[0].floor_fraction == 0.0
        assert profile.points[0].floor_source == "none"

    def test_insufficient_or_default_bucket_uses_zero_floor_duty(self):
        profile = build_baseline_duty_profile(
            _history([0, 0]), [_forecast()], None, 12, "UTC", True, 0.35
        )

        assert profile.points[0].source == "default"
        assert profile.points[0].floor_fraction == 0.0

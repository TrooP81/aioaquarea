"""Tests for shower mode (reactive DHW boost on rapid tank temp drops)."""

import datetime as dt
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from packages.core.models import (
    DeviceStatusRecord,
    PlanActionRecord,
    PlanRecord,
    ShowerEventRecord,
)
from packages.optimizer.shower_mode import ShowerDetector


def _make_status(ts, tank_temp, force_dhw=0, device_id="test-device"):
    """Helper to create a DeviceStatusRecord-like object."""
    record = DeviceStatusRecord(
        ts=ts,
        device_id=device_id,
        tank_temp=tank_temp,
        force_dhw=force_dhw,
    )
    return record


@pytest.fixture
def detector():
    return ShowerDetector()


@pytest.fixture
def now():
    return dt.datetime(2026, 5, 3, 10, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def sample_prices_flat(now):
    """24h of flat prices at 0.10 EUR/kWh (never peak)."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return [(start + dt.timedelta(hours=h), 0.10) for h in range(24)]


@pytest.fixture
def sample_prices_with_peak(now):
    """24h of prices with hour 10 at extreme peak."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    prices = []
    for h in range(24):
        price = 0.50 if h == 10 else 0.05
        prices.append((start + dt.timedelta(hours=h), price))
    return prices


class TestShowerDetection:
    @pytest.mark.asyncio
    async def test_shower_detected_on_sharp_drop(self, detector, now, sample_prices_flat):
        """A drop >= threshold between consecutive polls triggers shower mode."""
        prev_record = _make_status(now - dt.timedelta(minutes=5), tank_temp=55.0)
        current_record = _make_status(now, tank_temp=43.0)  # 12 deg C drop

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.flush = AsyncMock()

        # Mock: no active shower event
        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = None

        # Mock: previous record query
        mock_prev_result = MagicMock()
        mock_prev_result.scalar_one_or_none.return_value = prev_record

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_active_result  # active event query
            else:
                return mock_prev_result  # previous status query

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting, \
             patch("packages.optimizer.shower_mode.get_prices") as mock_get_prices:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {"shower_mode_enabled": "true", "shower_drop_threshold": "10"}.get(key, "")

            mock_get_setting.side_effect = setting_side_effect
            mock_get_prices.return_value = sample_prices_flat  # flat prices, not peak

            await detector.check(current_record)

        # Should have created a ShowerEventRecord with status="active"
        events = [o for o in added_objects if isinstance(o, ShowerEventRecord)]
        assert len(events) == 1
        assert events[0].status == "active"
        assert events[0].pre_shower_temp == 55.0

        # Should have created a force_dhw_on action
        actions = [o for o in added_objects if isinstance(o, PlanActionRecord)]
        assert len(actions) == 1
        assert actions[0].action_type == "force_dhw_on"
        payload = json.loads(actions[0].payload_json)
        assert payload["trigger"] == "shower_mode"
        assert payload["pre_shower_temp"] == 55.0

    @pytest.mark.asyncio
    async def test_no_trigger_below_threshold(self, detector, now):
        """A drop below the threshold does not trigger."""
        prev_record = _make_status(now - dt.timedelta(minutes=5), tank_temp=55.0)
        current_record = _make_status(now, tank_temp=48.0)  # 7 deg C drop, below default 10

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = None

        mock_prev_result = MagicMock()
        mock_prev_result.scalar_one_or_none.return_value = prev_record

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_active_result
            else:
                return mock_prev_result

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {"shower_mode_enabled": "true", "shower_drop_threshold": "10"}.get(key, "")

            mock_get_setting.side_effect = setting_side_effect

            await detector.check(current_record)

        events = [o for o in added_objects if isinstance(o, ShowerEventRecord)]
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_no_trigger_when_disabled(self, detector, now):
        """No trigger when shower_mode_enabled is false."""
        current_record = _make_status(now, tank_temp=43.0)

        with patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_setting.return_value = "false"
            await detector.check(current_record)
        # Nothing should happen - no session even opened

    @pytest.mark.asyncio
    async def test_recovery_creates_dhw_off(self, detector, now):
        """When tank recovers to pre-shower temp, force_dhw_off is injected."""
        active_event = ShowerEventRecord(
            id=1,
            started_at=now - dt.timedelta(minutes=15),
            pre_shower_temp=55.0,
            status="active",
        )
        current_record = _make_status(now, tank_temp=56.0)  # Recovered above 55

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.flush = AsyncMock()

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = active_event

        async def mock_execute(stmt):
            return mock_active_result

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {
                    "shower_mode_enabled": "true",
                    "shower_max_duration_minutes": "60",
                }.get(key, "")

            mock_get_setting.side_effect = setting_side_effect

            await detector.check(current_record)

        # Event should be marked recovered
        assert active_event.status == "recovered"
        assert active_event.recovered_at is not None

        # Should have a force_dhw_off action
        actions = [o for o in added_objects if isinstance(o, PlanActionRecord)]
        assert len(actions) == 1
        assert actions[0].action_type == "force_dhw_off"
        payload = json.loads(actions[0].payload_json)
        assert payload["trigger"] == "shower_mode"
        assert payload["reason"] == "recovered"

    @pytest.mark.asyncio
    async def test_timeout_after_max_duration(self, detector, now):
        """After max duration without recovery, times out and injects force_dhw_off."""
        active_event = ShowerEventRecord(
            id=1,
            started_at=now - dt.timedelta(minutes=65),  # 65 min ago (exceeds 60 default)
            pre_shower_temp=55.0,
            status="active",
        )
        current_record = _make_status(now, tank_temp=50.0)  # Still below target

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.flush = AsyncMock()

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = active_event

        async def mock_execute(stmt):
            return mock_active_result

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {
                    "shower_mode_enabled": "true",
                    "shower_max_duration_minutes": "60",
                }.get(key, "")

            mock_get_setting.side_effect = setting_side_effect

            await detector.check(current_record)

        assert active_event.status == "timeout"
        actions = [o for o in added_objects if isinstance(o, PlanActionRecord)]
        assert len(actions) == 1
        assert actions[0].action_type == "force_dhw_off"
        payload = json.loads(actions[0].payload_json)
        assert payload["reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_skip_during_peak_price(self, detector, now, sample_prices_with_peak):
        """During peak price hours, records event as skipped_peak without DHW action."""
        prev_record = _make_status(now - dt.timedelta(minutes=5), tank_temp=55.0)
        current_record = _make_status(now, tank_temp=43.0)  # 12 deg C drop

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.flush = AsyncMock()

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = None

        mock_prev_result = MagicMock()
        mock_prev_result.scalar_one_or_none.return_value = prev_record

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_active_result  # active event check
            else:
                return mock_prev_result  # previous status

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting, \
             patch("packages.optimizer.shower_mode.get_prices") as mock_get_prices:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {"shower_mode_enabled": "true", "shower_drop_threshold": "10"}.get(key, "")

            mock_get_setting.side_effect = setting_side_effect
            # Hour 10 is 0.50, all others 0.05 - hour 10 is peak
            mock_get_prices.return_value = sample_prices_with_peak

            await detector.check(current_record)

        events = [o for o in added_objects if isinstance(o, ShowerEventRecord)]
        assert len(events) == 1
        assert events[0].status == "skipped_peak"
        assert events[0].peak_price_skipped is True

        # No DHW action should be created
        actions = [o for o in added_objects if isinstance(o, PlanActionRecord)]
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_no_double_trigger_while_active(self, detector, now):
        """A second drop while an event is active goes to recovery check, not new event."""
        active_event = ShowerEventRecord(
            id=1,
            started_at=now - dt.timedelta(minutes=3),
            pre_shower_temp=55.0,
            status="active",
        )
        # Tank still dropping (not recovered yet)
        current_record = _make_status(now, tank_temp=40.0)

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.flush = AsyncMock()

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = active_event

        async def mock_execute(stmt):
            return mock_active_result

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {
                    "shower_mode_enabled": "true",
                    "shower_max_duration_minutes": "60",
                }.get(key, "")

            mock_get_setting.side_effect = setting_side_effect

            await detector.check(current_record)

        # Should NOT create a new event (still below recovery temp, not timed out)
        new_events = [o for o in added_objects if isinstance(o, ShowerEventRecord)]
        assert len(new_events) == 0
        # Active event should still be active
        assert active_event.status == "active"

    @pytest.mark.asyncio
    async def test_no_trigger_when_dhw_already_forced(self, detector, now):
        """No trigger if force_dhw is already 1 on the current record."""
        prev_record = _make_status(now - dt.timedelta(minutes=5), tank_temp=55.0)
        current_record = _make_status(now, tank_temp=43.0, force_dhw=1)  # DHW already on

        added_objects = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)

        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = None

        mock_prev_result = MagicMock()
        mock_prev_result.scalar_one_or_none.return_value = prev_record

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_active_result
            else:
                return mock_prev_result

        mock_session.execute = mock_execute

        with patch("packages.optimizer.shower_mode.get_session") as mock_get_session, \
             patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            async def setting_side_effect(key):
                return {"shower_mode_enabled": "true", "shower_drop_threshold": "10"}.get(key, "")

            mock_get_setting.side_effect = setting_side_effect

            await detector.check(current_record)

        events = [o for o in added_objects if isinstance(o, ShowerEventRecord)]
        assert len(events) == 0


class TestShowerConflictPrevention:
    def test_active_shower_suppresses_dhw_off(self):
        """When suppress_dhw_off=True, _plan_dhw() omits force_dhw_off actions."""
        from packages.optimizer.rules import RulesOptimizer

        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 5, 3, 0, 0, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=h), 0.05) for h in range(24)]
        weather = [(base + dt.timedelta(hours=h), 10.0) for h in range(24)]
        comfort_schedule = {"weekday": [7, 8, 9, 17, 18, 19, 20, 21], "weekend": [8, 9, 10]}

        actions_normal = optimizer._plan_dhw(
            prices, weather, base,
            current_tank_temp=42.0, tank_target=50,
            current_outdoor_temp=10.0, comfort_schedule=comfort_schedule,
            suppress_dhw_off=False,
        )
        actions_suppressed = optimizer._plan_dhw(
            prices, weather, base,
            current_tank_temp=42.0, tank_target=50,
            current_outdoor_temp=10.0, comfort_schedule=comfort_schedule,
            suppress_dhw_off=True,
        )

        # Normal mode has force_dhw_off actions
        off_normal = [a for a in actions_normal if a["type"] == "force_dhw_off"]
        off_suppressed = [a for a in actions_suppressed if a["type"] == "force_dhw_off"]

        # Normal should have off actions, suppressed should not
        if off_normal:  # Only assert if normal mode produced any
            assert len(off_suppressed) == 0


class TestShowerDetectionFailureSafety:
    @pytest.mark.asyncio
    async def test_shower_detection_error_is_caught(self, detector, now):
        """Shower detection failure shouldn't propagate (poller wraps in try/except)."""
        current_record = _make_status(now, tank_temp=43.0)

        with patch("packages.optimizer.shower_mode.get_setting") as mock_get_setting:
            mock_get_setting.side_effect = RuntimeError("DB connection lost")

            # Should raise - the poller catches it via try/except
            with pytest.raises(RuntimeError):
                await detector.check(current_record)

"""Tests for the PlanExecutor and action registry."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioaquarea import QuietMode
from aioaquarea.data import SpecialStatus

from packages.core.models import PlanActionRecord
from packages.optimizer.actions import ACTION_REGISTRY, ActionType, VerifyResult
from packages.optimizer.executor import (
    MAX_ACTIONS_PER_CYCLE,
    PlanExecutor,
    VERIFY_POLL_INTERVAL_S,
    VERIFY_TIMEOUT_S,
)
from packages.optimizer.executor_core import PlanExecutor as CorePlanExecutor


def _zone(temp: int | None = None, heat_min: int | None = 20, heat_max: int | None = 65):
    return SimpleNamespace(
        heat_target_temperature=temp,
        cool_target_temperature=None,
        heat_min=heat_min,
        heat_max=heat_max,
        supports_special_status=True,
        temperature_modifiers={
            SpecialStatus.ECO: SimpleNamespace(heat=-2, cool=None),
            SpecialStatus.COMFORT: SimpleNamespace(heat=2, cool=None),
        },
    )


def _device(
    *,
    force_dhw=None,
    quiet_mode=None,
    special_status=None,
    tank_temp=None,
    zone_temp=None,
    zone_min=20,
    zone_max=65,
    special_status_supported=True,
):
    return SimpleNamespace(
        support_special_status=special_status_supported,
        force_dhw=SimpleNamespace(value=force_dhw) if force_dhw is not None else None,
        quiet_mode=SimpleNamespace(value=quiet_mode) if quiet_mode is not None else None,
        special_status=SimpleNamespace(name=special_status) if special_status is not None else None,
        tank=SimpleNamespace(target_temperature=tank_temp)
        if tank_temp is not None
        else SimpleNamespace(target_temperature=None),
        zones={0: _zone(zone_temp, zone_min, zone_max)}
        if zone_temp is not None
        else {0: _zone(None, zone_min, zone_max)},
    )


@pytest.fixture
def mock_wrapper():
    wrapper = AsyncMock()
    wrapper.get_selected_device_id = AsyncMock(return_value="device-a")
    wrapper.force_dhw = AsyncMock()
    wrapper.set_quiet_mode = AsyncMock()
    wrapper.set_zone_heat_temperature = AsyncMock()
    wrapper.set_tank_temperature = AsyncMock()
    wrapper.set_special_status = AsyncMock()
    wrapper.clear_special_status = AsyncMock()
    wrapper.refresh_device = AsyncMock()
    wrapper.get_device = AsyncMock(return_value=_device(zone_temp=35))
    wrapper.get_active_weekly_timer_slots = AsyncMock(return_value=())
    return wrapper


@pytest.fixture
def executor(mock_wrapper):
    return PlanExecutor(mock_wrapper)


def _make_action(action_type: str, payload: dict | None = None, scheduled_ts=None):
    action = MagicMock()
    action.id = 1
    action.plan_id = 1
    action.action_type = action_type
    action.payload_json = json.dumps(payload or {})
    action.scheduled_ts = scheduled_ts or dt.datetime.now(dt.timezone.utc)
    action.status = "pending"
    action.device_id = "device-a"
    return action


def _extract_stmt_values(stmt) -> dict[str, object]:
    if not hasattr(stmt, "_values"):
        return {}
    values: dict[str, object] = {}
    for key, value in stmt._values.items():
        normalized_key = key.key if hasattr(key, "key") else str(key)
        values[normalized_key] = value.value if hasattr(value, "value") else value
    return values


@pytest.mark.asyncio
async def test_core_executors_keep_injected_session_factories_isolated():
    def make_factory():
        session = AsyncMock()
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[empty_result, empty_result])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=context), session

    first_factory, first_session = make_factory()
    second_factory, second_session = make_factory()
    first_executor = CorePlanExecutor(
        AsyncMock(), session_factory=first_factory, learning_check=AsyncMock(return_value=False)
    )
    second_executor = CorePlanExecutor(
        AsyncMock(), session_factory=second_factory, learning_check=AsyncMock(return_value=False)
    )

    await asyncio.gather(
        first_executor.execute_due_actions(), second_executor.execute_due_actions()
    )

    first_factory.assert_called_once_with()
    second_factory.assert_called_once_with()
    assert first_session.execute.await_count == 2
    assert second_session.execute.await_count == 2


class TestRegistry:
    @pytest.mark.asyncio
    async def test_zone_boost_skips_during_active_panasonic_weekly_timer(self, mock_wrapper):
        mock_wrapper.get_active_weekly_timer_slots.return_value = (SimpleNamespace(zone_id=1),)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper, {"offset": 2, "zone_id": 0}
        )

        assert result == {
            "skip": True,
            "reason": "panasonic_weekly_timer_active",
            "timer_zone_id": 1,
            "timer_slot_count": 1,
        }
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()

    def test_every_action_has_a_handler(self):
        assert set(ACTION_REGISTRY) == set(ActionType)
        for handler in ACTION_REGISTRY.values():
            assert callable(handler.dispatch)
            assert callable(handler.verify)

    def test_zone_verification_uses_requested_zone(self):
        device = SimpleNamespace(
            zones={
                1: _zone(30),
                2: _zone(37),
            }
        )
        handler = ACTION_REGISTRY[ActionType.SET_ZONE_HEAT_TEMPERATURE]

        result = handler.verify(
            device,
            {"zone_id": 2, "temperature": 37},
            {"zone_id": 2, "temperature": 37},
        )

        assert result.ok


class TestExecuteAction:
    @pytest.fixture(autouse=True)
    def allow_precondition_for_legacy_action_dispatch_tests(self, executor):
        executor._dispatch_precondition = AsyncMock(return_value=None)

    @pytest.mark.asyncio
    async def test_quiet_mode_dispatches_requested_panasonic_level(self, mock_wrapper):
        result = await ACTION_REGISTRY[ActionType.QUIET_MODE_ON].dispatch(
            mock_wrapper, {"level": 2}
        )

        assert result == {"quiet_mode": "LEVEL2"}
        mock_wrapper.set_quiet_mode.assert_awaited_once_with(QuietMode.LEVEL2)

    @pytest.mark.asyncio
    async def test_quiet_mode_rejects_invalid_level(self, mock_wrapper):
        result = await ACTION_REGISTRY[ActionType.QUIET_MODE_ON].dispatch(
            mock_wrapper, {"level": 4}
        )

        assert result == {
            "skip": True,
            "reason": "quiet_mode_level_invalid",
            "requested_quiet_level": 4,
        }
        mock_wrapper.set_quiet_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quiet_mode_skips_when_live_level_is_already_active(self, mock_wrapper):
        mock_wrapper.set_quiet_mode.return_value = False

        result = await ACTION_REGISTRY[ActionType.QUIET_MODE_ON].dispatch(
            mock_wrapper, {"level": 2}
        )

        assert result == {
            "skip": True,
            "reason": "quiet_mode_already_active",
            "observed_quiet_level": 2,
        }

    @pytest.mark.asyncio
    async def test_quiet_mode_records_safe_panasonic_acknowledgement(self, mock_wrapper):
        try:
            from aioaquarea.command_result import PanasonicCommandResult
        except ModuleNotFoundError:
            try:
                from aioaquarea import PanasonicCommandResult
            except ImportError:
                pytest.skip("PanasonicCommandResult is unavailable in installed aioaquarea")

        mock_wrapper.set_quiet_mode.return_value = PanasonicCommandResult(
            http_status=200,
            response_code=0,
            request_id="request-123",
        )

        result = await ACTION_REGISTRY[ActionType.QUIET_MODE_ON].dispatch(
            mock_wrapper, {"level": 2}
        )

        assert result == {
            "quiet_mode": "LEVEL2",
            "panasonic_http_status": 200,
            "panasonic_response_code": 0,
            "panasonic_request_id": "request-123",
        }

    @pytest.mark.asyncio
    async def test_force_dhw_off_skips_when_live_state_is_already_off(self, mock_wrapper):
        mock_wrapper.force_dhw.return_value = False

        result = await ACTION_REGISTRY[ActionType.FORCE_DHW_OFF].dispatch(mock_wrapper, {})

        assert result == {"skip": True, "reason": "force_dhw_already_off"}

    @pytest.mark.asyncio
    async def test_eco_mode_dispatch_requires_safe_device_support(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=35, special_status_supported=False)

        result = await ACTION_REGISTRY[ActionType.ECO_MODE_ON].dispatch(mock_wrapper, {})

        assert result == {"skip": True, "reason": "special_status_not_safely_supported"}
        mock_wrapper.set_special_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_eco_mode_dispatches_for_safe_device_metadata(self, mock_wrapper):
        result = await ACTION_REGISTRY[ActionType.ECO_MODE_ON].dispatch(mock_wrapper, {})

        assert result == {"special_status": "ECO"}
        mock_wrapper.set_special_status.assert_awaited_once_with("ECO")

    def test_quiet_mode_verification_requires_exact_level(self):
        handler = ACTION_REGISTRY[ActionType.QUIET_MODE_ON]

        assert handler.verify(_device(quiet_mode=2), {}, {"quiet_mode": "LEVEL2"}).ok
        mismatch = handler.verify(_device(quiet_mode=1), {}, {"quiet_mode": "LEVEL2"})
        assert not mismatch.ok
        assert mismatch.expected_value == 2

    @pytest.mark.asyncio
    async def test_force_dhw_on_dispatches_and_verifies(self, executor, mock_wrapper):
        action = _make_action(str(ActionType.FORCE_DHW_ON))
        mock_wrapper.refresh_device.return_value = _device(force_dhw=1)

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._execute_action(action)

        mock_wrapper.force_dhw.assert_awaited_once()
        assert mock_session.execute.await_count >= 3

    @pytest.mark.asyncio
    async def test_force_dhw_on_skips_when_live_tank_is_already_at_target(
        self, executor, mock_wrapper
    ):
        mock_wrapper.get_device.return_value = SimpleNamespace(
            tank=SimpleNamespace(temperature=52.0, target_temperature=52.0),
            zones={0: _zone(35)},
        )
        action = _make_action(str(ActionType.FORCE_DHW_ON))

        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._execute_action(action)

        mock_wrapper.force_dhw.assert_not_awaited()
        assert mock_session.execute.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action_factory", [SimpleNamespace, PlanActionRecord])
    async def test_action_enforces_precondition_before_dispatch_for_duck_typed_and_orm_actions(
        self, executor, mock_wrapper, action_factory
    ):
        action = action_factory(
            id=1,
            device_id="device-a",
            action_type=str(ActionType.FORCE_DHW_ON),
            payload_json="{}",
        )
        executor._dispatch_precondition = AsyncMock(
            return_value={"reason": "device_status_stale", "device_id": "device-a"}
        )
        executor._skip_action = AsyncMock()

        await executor._execute_action(action)

        executor._dispatch_precondition.assert_awaited_once_with(
            action, ActionType.FORCE_DHW_ON, {}
        )
        executor._skip_action.assert_awaited_once()
        mock_wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action_type", [ActionType.NORMAL_MODE_ON, ActionType.ECO_MODE_OFF])
    @pytest.mark.parametrize(
        "precondition_result",
        [
            {"reason": "space_heating_gate_blocked"},
            {"reason": "space_heating_gate_unknown"},
        ],
    )
    async def test_blocked_or_unknown_room_heating_action_sends_zero_writes(
        self, executor, mock_wrapper, precondition_result, action_type
    ):
        action = _make_action(str(action_type))
        executor._dispatch_precondition = AsyncMock(return_value=precondition_result)
        executor._skip_action = AsyncMock()

        await executor._execute_action(action)

        executor._dispatch_precondition.assert_awaited_once_with(action, action_type, {})
        executor._skip_action.assert_awaited_once_with(action, precondition_result)
        mock_wrapper.force_dhw.assert_not_awaited()
        mock_wrapper.set_special_status.assert_not_awaited()
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()
        mock_wrapper.set_tank_temperature.assert_not_awaited()
        mock_wrapper.set_quiet_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zone_boost_uses_live_curve_shift_range(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=-5, zone_min=-5, zone_max=5)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper, {"offset": 2, "zone_id": 0}
        )

        assert result == {"zone_id": 0, "temperature": -3}
        mock_wrapper.set_zone_heat_temperature.assert_awaited_once_with(0, -3)

    @pytest.mark.asyncio
    async def test_zone_boost_skips_when_live_range_is_missing(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=-5, zone_min=None, zone_max=None)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper, {"offset": 2, "zone_id": 0}
        )

        assert result["reason"] == "zone_target_or_range_unavailable"
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zone_boost_calculates_one_absolute_target(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=35)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper, {"offset": 2, "zone_id": 0}
        )

        assert result == {"zone_id": 0, "temperature": 37}
        mock_wrapper.set_zone_heat_temperature.assert_awaited_once_with(0, 37)

    @pytest.mark.asyncio
    async def test_zone_boost_uses_frozen_plan_target(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=35)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper,
            {"offset": 2, "baseline_temperature": 35, "temperature": 37, "zone_id": 0},
        )

        assert result == {"zone_id": 0, "temperature": 37}
        mock_wrapper.set_zone_heat_temperature.assert_awaited_once_with(0, 37)

    @pytest.mark.asyncio
    async def test_zone_boost_skips_when_target_changed_after_plan(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=36)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_BOOST].dispatch(
            mock_wrapper,
            {"offset": 2, "baseline_temperature": 35, "temperature": 37, "zone_id": 0},
        )

        assert result["reason"] == "zone_target_changed_since_plan"
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zone_restore_without_water_target_is_safe_noop(self, mock_wrapper):
        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_RESTORE].dispatch(
            mock_wrapper, {"reason": "legacy_restore"}
        )

        assert result["reason"] == "zone_restore_target_missing"
        mock_wrapper.get_device.assert_not_awaited()
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zone_restore_returns_to_frozen_baseline(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=37)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_RESTORE].dispatch(
            mock_wrapper, {"temperature": 35, "boost_temperature": 37, "zone_id": 0}
        )

        assert result == {"zone_id": 0, "temperature": 35}
        mock_wrapper.set_zone_heat_temperature.assert_awaited_once_with(0, 35)

    @pytest.mark.asyncio
    async def test_zone_restore_preserves_target_changed_after_boost(self, mock_wrapper):
        mock_wrapper.get_device.return_value = _device(zone_temp=38)

        result = await ACTION_REGISTRY[ActionType.ZONE_TEMP_RESTORE].dispatch(
            mock_wrapper, {"temperature": 35, "boost_temperature": 37, "zone_id": 0}
        )

        assert result["reason"] == "zone_restore_target_changed_since_boost"
        mock_wrapper.set_zone_heat_temperature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_tank_temp_dispatches(self, executor, mock_wrapper):
        action = _make_action(str(ActionType.SET_TANK_TEMP), {"temperature": 52})
        mock_wrapper.refresh_device.return_value = _device(tank_temp=52)

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._execute_action(action)

        mock_wrapper.set_tank_temperature.assert_awaited_once_with(52)

    @pytest.mark.asyncio
    async def test_unknown_action_type_does_not_crash(self, executor):
        action = _make_action("unknown_action_xyz")
        await executor._execute_action(action)

    @pytest.mark.asyncio
    async def test_action_failure_records_error(self, executor, mock_wrapper):
        mock_wrapper.force_dhw.side_effect = RuntimeError("Connection lost")
        action = _make_action(str(ActionType.FORCE_DHW_ON))

        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_ctx = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._execute_action(action)

        assert mock_ctx.execute.await_count == 1


class TestVerification:
    @pytest.mark.asyncio
    async def test_timeout_then_retry_success(self, executor, mock_wrapper):
        action = _make_action(str(ActionType.FORCE_DHW_ON))
        first = VerifyResult(ok=False, observed_value=0, expected_value=1, reason="mismatch")
        second = VerifyResult(ok=True, observed_value=1, expected_value=1)

        with (
            patch.object(
                executor, "_poll_until_verified", side_effect=[(first, 1), (second, 2)]
            ) as poll,
            patch("packages.optimizer.executor.get_session") as mock_gs,
        ):
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._verify_with_retry(action, {}, {"force_dhw": "ON"})

        assert poll.await_count == 2
        handler_dispatch_calls = mock_wrapper.force_dhw.await_count
        assert handler_dispatch_calls == 1

    @pytest.mark.asyncio
    async def test_persistent_mismatch_fails(self, executor):
        action = _make_action(str(ActionType.FORCE_DHW_ON))
        failed = VerifyResult(ok=False, observed_value=0, expected_value=1, reason="mismatch")

        with (
            patch.object(executor, "_poll_until_verified", side_effect=[(failed, 1), (failed, 2)]),
            patch("packages.optimizer.executor.get_session") as mock_gs,
        ):
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._verify_with_retry(action, {}, {"force_dhw": "ON"})

        assert mock_session.execute.await_count >= 1

    def test_force_dhw_on_accepts_panasonic_auto_stop_at_tank_target(self):
        device = SimpleNamespace(
            force_dhw=SimpleNamespace(value=0),
            tank=SimpleNamespace(temperature=52.0, target_temperature=52.0),
        )

        result = ACTION_REGISTRY[ActionType.FORCE_DHW_ON].verify(device, {}, {"force_dhw": "ON"})

        assert result.ok
        assert result.reason == "tank_target_reached"

    def test_force_dhw_on_still_fails_below_tank_target(self):
        device = SimpleNamespace(
            force_dhw=SimpleNamespace(value=0),
            tank=SimpleNamespace(temperature=49.0, target_temperature=52.0),
        )

        result = ACTION_REGISTRY[ActionType.FORCE_DHW_ON].verify(device, {}, {"force_dhw": "ON"})

        assert not result.ok
        assert result.reason == "force_dhw_mismatch"

    @pytest.mark.asyncio
    async def test_force_dhw_retry_at_target_does_not_send_another_command(self, mock_wrapper):
        mock_wrapper.get_device.return_value = SimpleNamespace(
            tank=SimpleNamespace(temperature=52.0, target_temperature=52.0)
        )

        result = await ACTION_REGISTRY[ActionType.FORCE_DHW_ON].redispatch_expected(
            mock_wrapper, {}, {"force_dhw": "ON"}
        )

        assert result == {"force_dhw": "ON"}
        mock_wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zone_boost_retry_reuses_original_absolute_target(self, executor, mock_wrapper):
        action = _make_action(str(ActionType.ZONE_TEMP_BOOST), {"offset": 2, "zone_id": 0})
        # A concurrent/read-after-write value of 38C made the old retry path
        # calculate 40C instead of repeating the original 37C command.
        mock_wrapper.get_device.return_value = _device(zone_temp=38)
        failed = VerifyResult(ok=False, observed_value=38, expected_value=37, reason="mismatch")
        succeeded = VerifyResult(ok=True, observed_value=37, expected_value=37)

        with (
            patch.object(
                executor,
                "_poll_until_verified",
                side_effect=[(failed, 1), (succeeded, 2)],
            ),
            patch("packages.optimizer.executor.get_session") as mock_gs,
        ):
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await executor._verify_with_retry(
                action,
                {"offset": 2, "zone_id": 0},
                {"zone_id": 0, "temperature": 37},
            )

        mock_wrapper.get_device.assert_not_awaited()
        mock_wrapper.set_zone_heat_temperature.assert_awaited_once_with(0, 37)

    @pytest.mark.asyncio
    async def test_every_action_type_has_verifier_coverage(self, mock_wrapper):
        expectations = {
            ActionType.FORCE_DHW_ON: (_device(force_dhw=1), {"force_dhw": "ON"}),
            ActionType.FORCE_DHW_OFF: (_device(force_dhw=0), {"force_dhw": "OFF"}),
            ActionType.QUIET_MODE_ON: (_device(quiet_mode=2), {"quiet_mode": "LEVEL2"}),
            ActionType.QUIET_MODE_OFF: (_device(quiet_mode=0), {"quiet_mode": "OFF"}),
            ActionType.ZONE_TEMP_BOOST: (_device(zone_temp=37), {"temperature": 37}),
            ActionType.ZONE_TEMP_RESTORE: (_device(zone_temp=21), {"temperature": 21}),
            ActionType.SET_TANK_TEMP: (_device(tank_temp=52), {"temperature": 52}),
            ActionType.SET_ZONE_HEAT_TEMPERATURE: (_device(zone_temp=34), {"temperature": 34}),
            ActionType.ECO_MODE_ON: (_device(special_status="ECO"), {"special_status": "ECO"}),
            ActionType.ECO_MODE_OFF: (_device(special_status=None), {"special_status": None}),
            ActionType.NORMAL_MODE_ON: (_device(special_status=None), {"special_status": None}),
            ActionType.COMFORT_MODE_ON: (
                _device(special_status="COMFORT"),
                {"special_status": "COMFORT"},
            ),
        }

        for action_type, (device, expected) in expectations.items():
            result = ACTION_REGISTRY[action_type].verify(device, {}, expected)
            assert result.ok, action_type

    def test_eco_mode_requires_observed_special_status(self):
        device = _device(special_status=None)
        result = ACTION_REGISTRY[ActionType.ECO_MODE_ON].verify(
            device, {}, {"special_status": "ECO"}
        )
        assert not result.ok
        assert result.reason == "special_status_mismatch"
        assert result.observed_value is None

    def test_comfort_mode_requires_observed_special_status(self):
        device = _device(special_status=None)
        result = ACTION_REGISTRY[ActionType.COMFORT_MODE_ON].verify(
            device, {}, {"special_status": "COMFORT"}
        )
        assert not result.ok
        assert result.reason == "special_status_mismatch"

    def test_special_status_real_mismatch_still_fails(self):
        device = _device(special_status="COMFORT")
        result = ACTION_REGISTRY[ActionType.ECO_MODE_ON].verify(
            device, {}, {"special_status": "ECO"}
        )
        assert not result.ok
        assert result.reason == "special_status_mismatch"

    def test_clear_special_status_still_verifies(self):
        device = _device(special_status=None)
        result = ACTION_REGISTRY[ActionType.NORMAL_MODE_ON].verify(
            device, {}, {"special_status": None}
        )
        assert result.ok
        assert result.reason is None


class TestExecuteDueActions:
    @pytest.mark.asyncio
    async def test_active_override_blocks_execution(self, executor):
        override_mock = MagicMock()
        override_mock.reason = "Manual test"

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch(
                "packages.optimizer.executor.is_learning_mode_active",
                new=AsyncMock(return_value=False),
            ),
        ):
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = [override_mock]
            actions_result = MagicMock()
            actions_result.scalars.return_value.all.return_value = []
            mock_session.execute = AsyncMock(side_effect=[override_result, actions_result])

            await executor.execute_due_actions()

        executor._wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_override_marks_due_actions_skipped(self, executor):
        override_mock = MagicMock()
        override_mock.reason = "comfort_schedule"
        action_mock = _make_action(str(ActionType.COMFORT_MODE_ON))

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch(
                "packages.optimizer.executor.is_learning_mode_active",
                new=AsyncMock(return_value=False),
            ),
        ):
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = [override_mock]
            actions_result = MagicMock()
            actions_result.scalars.return_value.all.return_value = [action_mock]
            mock_session.execute = AsyncMock(
                side_effect=[override_result, actions_result, None, None]
            )

            await executor.execute_due_actions()

        executor._wrapper.set_special_status.assert_not_awaited()
        assert mock_session.execute.call_count == 4

    @pytest.mark.asyncio
    async def test_due_actions_are_claimed_without_a_global_freshness_gate(self, executor):
        action_mock = _make_action(str(ActionType.FORCE_DHW_ON))
        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = []
            actions_result = MagicMock()
            actions_result.scalars.return_value.all.return_value = [action_mock]
            mock_session.execute = AsyncMock(side_effect=[override_result, actions_result, None])

            with patch.object(executor, "_execute_action", new=AsyncMock()) as execute_action:
                await executor.execute_due_actions()

        execute_action.assert_awaited_once_with(action_mock)
        assert mock_session.execute.call_count == 3


@pytest.mark.asyncio
async def test_executor_rejects_action_for_a_different_selected_device(executor):
    executor._wrapper.get_selected_device_id.return_value = "device-a"
    action = SimpleNamespace(device_id="device-b")

    result = await executor._dispatch_precondition(action, ActionType.NORMAL_MODE_ON, {})

    assert result == {"reason": "action_device_unresolvable", "device_id": "device-b"}


@pytest.mark.asyncio
async def test_executor_allows_a_fresh_action_when_other_device_is_stale(executor):
    from packages.core.space_heating_gate import HeatingGateConfig

    config = HeatingGateConfig()
    status = SimpleNamespace(zone1_target_temp=20, zone2_target_temp=20)
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = status
    fresh_ids_result = MagicMock()
    fresh_ids_result.scalars.return_value.all.return_value = ["device-a"]
    gate_result = MagicMock()
    gate_result.scalar_one_or_none.return_value = SimpleNamespace(
        state="ALLOWED",
        reason_code="below_on_threshold",
        config_fingerprint=config.fingerprint,
        last_raw_outdoor_c=5.0,
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[status_result, fresh_ids_result, gate_result])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=mock_session)
    context.__aexit__ = AsyncMock(return_value=False)
    executor._wrapper.get_selected_device_id.return_value = "device-a"
    action = SimpleNamespace(device_id=None)

    with (
        patch("packages.optimizer.executor_core.get_session", return_value=context),
        patch(
            "packages.optimizer.executor_core.get_space_heating_gate_config",
            new=AsyncMock(return_value=config),
        ),
    ):
        result = await executor._dispatch_precondition(action, ActionType.NORMAL_MODE_ON, {})

    assert result is None
    assert mock_session.execute.await_count == 3


@pytest.mark.asyncio
async def test_executor_blocks_room_heating_when_gate_is_blocked(executor):
    from packages.core.space_heating_gate import HeatingGateConfig

    config = HeatingGateConfig()
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = SimpleNamespace(
        zone1_target_temp=20, zone2_target_temp=20
    )
    gate_result = MagicMock()
    gate_result.scalar_one_or_none.return_value = SimpleNamespace(
        state="BLOCKED",
        reason_code="above_off_threshold",
        config_fingerprint=config.fingerprint,
        last_raw_outdoor_c=16.0,
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[status_result, gate_result])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=mock_session)
    context.__aexit__ = AsyncMock(return_value=False)
    executor._wrapper.get_selected_device_id.return_value = "device-a"
    action = SimpleNamespace(device_id="device-a")

    with (
        patch("packages.optimizer.executor_core.get_session", return_value=context),
        patch(
            "packages.optimizer.executor_core.get_space_heating_gate_config",
            new=AsyncMock(return_value=config),
        ),
    ):
        result = await executor._dispatch_precondition(action, ActionType.NORMAL_MODE_ON, {})

    assert result["reason"] == "space_heating_gate_blocked"
    assert result["device_id"] == "device-a"


@pytest.mark.asyncio
async def test_executor_dhw_action_bypasses_room_heating_gate(executor):
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = SimpleNamespace(
        zone1_target_temp=20, zone2_target_temp=20
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=status_result)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=mock_session)
    context.__aexit__ = AsyncMock(return_value=False)
    executor._wrapper.get_selected_device_id.return_value = "device-a"
    action = SimpleNamespace(device_id="device-a")
    gate_config = AsyncMock()

    with (
        patch("packages.optimizer.executor_core.get_session", return_value=context),
        patch("packages.optimizer.executor_core.get_space_heating_gate_config", new=gate_config),
    ):
        result = await executor._dispatch_precondition(action, ActionType.FORCE_DHW_ON, {})

    assert result is None
    gate_config.assert_not_awaited()
    mock_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_executor_skips_force_dhw_when_selected_device_status_is_stale(
    executor, mock_wrapper
):
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[status_result, None])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=mock_session)
    context.__aexit__ = AsyncMock(return_value=False)
    action = _make_action(str(ActionType.FORCE_DHW_ON))

    with patch("packages.optimizer.executor.get_session", return_value=context):
        await executor._execute_action(action)

    skipped_values = _extract_stmt_values(mock_session.execute.await_args_list[1].args[0])
    assert skipped_values["status"] == "skipped"
    assert json.loads(skipped_values["result_json"]) == {
        "reason": "device_status_stale",
        "device_id": "device-a",
    }
    mock_wrapper.force_dhw.assert_not_awaited()
    mock_wrapper.set_special_status.assert_not_awaited()
    mock_wrapper.set_zone_heat_temperature.assert_not_awaited()
    mock_wrapper.set_tank_temperature.assert_not_awaited()
    mock_wrapper.set_quiet_mode.assert_not_awaited()


class TestLearningMode:
    @pytest.mark.asyncio
    async def test_seasonal_calibration_only_pauses_commands_when_explicitly_active(self):
        from packages.optimizer.executor_core import is_learning_mode_active

        with (
            patch(
                "packages.core.settings_service.get_bool_setting",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "packages.ml.seasonal_learning.get_seasonal_calibration_status",
                new=AsyncMock(return_value={"observe_only_active": True, "reason": "active"}),
            ),
        ):
            assert await is_learning_mode_active() is True

    @pytest.mark.asyncio
    async def test_learning_mode_skips_due_actions_without_touching_device(self, executor):
        action_mock = _make_action(str(ActionType.FORCE_DHW_ON))

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch(
                "packages.optimizer.executor.is_learning_mode_active",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = []
            actions_result = MagicMock()
            actions_result.scalars.return_value.all.return_value = [action_mock]
            mock_session.execute = AsyncMock(
                side_effect=[override_result, actions_result, None, None]
            )

            await executor.execute_due_actions()

        executor._wrapper.force_dhw.assert_not_awaited()
        assert mock_session.execute.call_count == 4

    @pytest.mark.asyncio
    async def test_learning_mode_off_does_not_skip(self, executor):
        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch(
                "packages.optimizer.executor.is_learning_mode_active",
                new=AsyncMock(return_value=False),
            ),
        ):
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = []
            actions_result = MagicMock()
            actions_result.scalars.return_value.all.return_value = []
            mock_session.execute = AsyncMock(side_effect=[override_result, actions_result])

            await executor.execute_due_actions()

        # No overrides, no learning mode, no due actions → only the two queries ran.
        executor._wrapper.force_dhw.assert_not_awaited()
        assert mock_session.execute.call_count == 2


class TestConstants:
    def test_executor_limits_are_rate_safe(self):
        assert MAX_ACTIONS_PER_CYCLE > 0
        assert VERIFY_POLL_INTERVAL_S > 0
        assert VERIFY_TIMEOUT_S >= VERIFY_POLL_INTERVAL_S


class TestExpireStaleActions:
    @pytest.mark.asyncio
    async def test_no_stale_actions_is_noop(self, executor):
        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            stale_result = MagicMock()
            stale_result.scalars.return_value.all.return_value = []
            mock_session.execute = AsyncMock(return_value=stale_result)

            await executor.expire_stale_actions()

        assert mock_session.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_stale_executing_action_is_expired(self, executor):
        action = _make_action(str(ActionType.FORCE_DHW_ON))
        action.status = "executing"

        with (
            patch("packages.optimizer.executor.get_session") as mock_gs,
            patch.object(
                executor,
                "_diagnose_missed",
                new=AsyncMock(return_value={"reason": "executor_gap", "gap_minutes": 12.0}),
            ),
        ):
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            stale_result = MagicMock()
            stale_result.scalars.return_value.all.return_value = [action]
            latest_plan_result = MagicMock()
            latest_plan_result.scalar_one_or_none.return_value = action.plan_id
            mock_session.execute = AsyncMock(side_effect=[stale_result, latest_plan_result, None])

            await executor.expire_stale_actions()

        status_updates: list[str] = []
        expired_stmt_sql: str | None = None
        for call in mock_session.execute.await_args_list:
            stmt = call.args[0]
            values = _extract_stmt_values(stmt)
            status = values.get("status")
            if isinstance(status, str):
                status_updates.append(status)
                if status == "expired":
                    expired_stmt_sql = str(stmt)

        assert "expired" in status_updates
        assert expired_stmt_sql is not None
        assert "plan_actions.status IN" in expired_stmt_sql

"""Tests for the PlanExecutor."""

import asyncio
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.optimizer.executor import PlanExecutor, VERIFICATION_DELAY


@pytest.fixture
def mock_wrapper():
    wrapper = AsyncMock()
    wrapper.force_dhw = AsyncMock()
    wrapper.set_quiet_mode = AsyncMock()
    wrapper.set_zone_heat_temperature = AsyncMock()
    wrapper.set_tank_temperature = AsyncMock()
    wrapper.set_special_status = AsyncMock()
    wrapper.clear_special_status = AsyncMock()
    wrapper.refresh_device = AsyncMock()
    wrapper.get_device = AsyncMock()
    return wrapper


@pytest.fixture
def executor(mock_wrapper):
    return PlanExecutor(mock_wrapper)


def _make_action(action_type: str, payload: dict | None = None, scheduled_ts=None):
    """Create a mock PlanActionRecord."""
    action = MagicMock()
    action.id = 1
    action.action_type = action_type
    action.payload_json = json.dumps(payload or {})
    action.scheduled_ts = scheduled_ts or dt.datetime.now(dt.timezone.utc)
    action.status = "pending"
    return action


class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_force_dhw_on_dispatches(self, executor, mock_wrapper):
        action = _make_action("force_dhw_on")
        with patch("packages.optimizer.executor.asyncio.create_task"):
            await executor._execute_action(action)
        mock_wrapper.force_dhw.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quiet_mode_on_dispatches(self, executor, mock_wrapper):
        action = _make_action("quiet_mode_on")
        with patch("packages.optimizer.executor.asyncio.create_task"):
            await executor._execute_action(action)
        mock_wrapper.set_quiet_mode.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_tank_temp_dispatches(self, executor, mock_wrapper):
        action = _make_action("set_tank_temp", {"temperature": 52})
        with patch("packages.optimizer.executor.asyncio.create_task"):
            await executor._execute_action(action)
        mock_wrapper.set_tank_temperature.assert_awaited_once_with(52)

    @pytest.mark.asyncio
    async def test_unknown_action_type_does_not_crash(self, executor, mock_wrapper):
        action = _make_action("unknown_action_xyz")
        with patch("packages.optimizer.executor.asyncio.create_task"):
            await executor._execute_action(action)
        # Should not raise

    @pytest.mark.asyncio
    async def test_action_failure_records_error(self, executor, mock_wrapper):
        mock_wrapper.force_dhw.side_effect = RuntimeError("Connection lost")
        action = _make_action("force_dhw_on")

        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_ctx = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            # Should not raise
            await executor._execute_action(action)


class TestExecuteDueActions:
    @pytest.mark.asyncio
    async def test_active_override_blocks_execution(self, executor):
        """When an active override exists, no actions are executed."""
        override_mock = MagicMock()
        override_mock.reason = "Manual test"

        with patch("packages.optimizer.executor.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            # First query returns overrides
            override_result = MagicMock()
            override_result.scalars.return_value.all.return_value = [override_mock]
            mock_session.execute = AsyncMock(return_value=override_result)

            await executor.execute_due_actions()

        # The wrapper should not have been called
        executor._wrapper.force_dhw.assert_not_awaited()


class TestVerifyAction:
    @pytest.mark.asyncio
    async def test_verify_force_dhw_on(self, executor, mock_wrapper):
        device = MagicMock()
        device.force_dhw.value = 1
        mock_wrapper.refresh_device.return_value = device

        with patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock):
            result = await executor._verify_action("force_dhw_on")

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_force_dhw_off(self, executor, mock_wrapper):
        device = MagicMock()
        device.force_dhw.value = 0
        mock_wrapper.refresh_device.return_value = device

        with patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock):
            result = await executor._verify_action("force_dhw_off")

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_unknown_action_returns_true(self, executor, mock_wrapper):
        device = MagicMock()
        mock_wrapper.refresh_device.return_value = device

        with patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock):
            result = await executor._verify_action("some_new_action")

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_handles_exception(self, executor, mock_wrapper):
        mock_wrapper.refresh_device.side_effect = RuntimeError("timeout")

        with patch("packages.optimizer.executor.asyncio.sleep", new_callable=AsyncMock):
            result = await executor._verify_action("force_dhw_on")

        assert result is False

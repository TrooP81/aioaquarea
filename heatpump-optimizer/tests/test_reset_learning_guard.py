"""CG-T4: positive observation authority survives a secondary lookup failure."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.learning_state import LearningStateSnapshot, get_learning_state_details
from packages.optimizer.executor_core import LearningModeState, resolve_learning_mode_state
from packages.optimizer.executor_core import PlanExecutor


@pytest.mark.parametrize("manual,seasonal", [(True, False), (False, True)])
async def test_positive_evidence_wins_over_unreliable_secondary(monkeypatch, manual, seasonal):
    monkeypatch.setattr(
        "packages.optimizer.executor_core.get_learning_state_details",
        AsyncMock(return_value=(LearningStateSnapshot(manual, seasonal, False), {})),
    )
    assert await resolve_learning_mode_state() is LearningModeState.ACTIVE
    executor = PlanExecutor(MagicMock(), learning_check=resolve_learning_mode_state)
    executor._claim_due_safety_action = AsyncMock()
    executor._execute_safety_action = AsyncMock()
    await executor.execute_due_actions()
    executor._claim_due_safety_action.assert_not_awaited()
    executor._execute_safety_action.assert_not_awaited()


async def test_seasonal_positive_survives_manual_lookup_failure(monkeypatch):
    monkeypatch.setattr(
        "packages.core.settings_service.get_bool_setting",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_status",
        AsyncMock(return_value={"observe_only_active": True}),
    )
    state, _ = await get_learning_state_details()
    assert state.active
    assert not state.reliable

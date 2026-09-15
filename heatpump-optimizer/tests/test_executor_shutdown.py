from __future__ import annotations

import asyncio
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.optimizer.actions import ActionType
from packages.optimizer.executor import PlanExecutor


def _make_action(action_type: str, payload: dict | None = None):
    action = MagicMock()
    action.id = 1
    action.plan_id = 1
    action.action_type = action_type
    action.payload_json = json.dumps(payload or {})
    action.scheduled_ts = dt.datetime.now(dt.timezone.utc)
    action.status = "pending"
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
async def test_inflight_cancel_reconciles_dispatched_action_to_cancelled() -> None:
    wrapper = AsyncMock()
    executor = PlanExecutor(wrapper)
    action = _make_action(str(ActionType.FORCE_DHW_ON))
    fake_handler = MagicMock()
    fake_handler.dispatch = AsyncMock(return_value={"force_dhw": "ON"})

    with (
        patch("packages.optimizer.executor_core.get_action_handler", return_value=fake_handler),
        patch.object(executor, "_verify_with_retry", side_effect=asyncio.CancelledError),
        patch("packages.optimizer.executor.get_session") as mock_gs,
    ):
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(asyncio.CancelledError):
            await executor._execute_action(action)

    fake_handler.dispatch.assert_awaited_once_with(wrapper, {})

    status_updates: list[str] = []
    cancelled_payload: dict[str, object] | None = None
    for call in mock_session.execute.await_args_list:
        values = _extract_stmt_values(call.args[0])
        status = values.get("status")
        if isinstance(status, str):
            status_updates.append(status)
        if status == "cancelled" and isinstance(values.get("result_json"), str):
            cancelled_payload = json.loads(values["result_json"])

    assert "dispatched" in status_updates
    assert "cancelled" in status_updates
    assert cancelled_payload == {
        "reason": "shutdown_cancelled",
        "detail": "Executor shutdown interrupted action verification",
    }
    assert mock_session.add.await_count == 1
    added_audit = mock_session.add.await_args.args[0]
    assert added_audit.actor == "optimizer"
    assert added_audit.action == str(ActionType.FORCE_DHW_ON)
    assert added_audit.result == "cancelled"


@pytest.mark.asyncio
async def test_execute_due_actions_cancel_reconciles_all_claimed_executing_actions() -> None:
    wrapper = AsyncMock()
    executor = PlanExecutor(wrapper)

    first_action = _make_action(str(ActionType.FORCE_DHW_ON))
    first_action.id = 101
    second_action = _make_action(str(ActionType.QUIET_MODE_ON), {"level": 2})
    second_action.id = 102

    with (
        patch("packages.optimizer.executor.get_session") as mock_gs,
        patch(
            "packages.optimizer.executor.is_learning_mode_active",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            executor,
            "_execute_action",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ) as mock_execute_action,
    ):
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        override_result = MagicMock()
        override_result.scalars.return_value.all.return_value = []
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = [first_action, second_action]
        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = dt.datetime.now(dt.timezone.utc)
        executing_result = MagicMock()
        executing_result.scalars.return_value.all.return_value = [101, 102]

        # Override query, action query, freshness check, claim update,
        # executing reconciliation query, then cancellation update.
        mock_session.execute = AsyncMock(
            side_effect=[
                override_result,
                actions_result,
                status_result,
                None,
                executing_result,
                None,
            ]
        )

        with pytest.raises(asyncio.CancelledError):
            await executor.execute_due_actions()

    mock_execute_action.assert_awaited_once_with(first_action)

    cancelled_payload: dict[str, object] | None = None
    cancelled_updates = 0
    for call in mock_session.execute.await_args_list:
        values = _extract_stmt_values(call.args[0])
        if values.get("status") == "cancelled":
            cancelled_updates += 1
            if isinstance(values.get("result_json"), str):
                cancelled_payload = json.loads(values["result_json"])

    assert cancelled_updates == 1
    assert cancelled_payload == {
        "reason": "shutdown_cancelled",
        "detail": "Executor shutdown interrupted action verification",
    }
    assert mock_session.add.await_count == 2
    audit_results = [call.args[0].result for call in mock_session.add.await_args_list]
    audit_actions = [call.args[0].action for call in mock_session.add.await_args_list]
    assert audit_results == ["cancelled", "cancelled"]
    assert sorted(audit_actions) == sorted([first_action.action_type, second_action.action_type])

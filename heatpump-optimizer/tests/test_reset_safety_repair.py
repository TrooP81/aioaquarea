"""Focused terminal-event, fenced completion, and read-only safety reporting checks."""

from contextlib import asynccontextmanager
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    PlanActionRecord,
    ShowerEventRecord,
)
from packages.core.safety_reverts import complete_linked_shower, unresolved_revert_predicate
from packages.ml.seasonal_learning import (
    get_seasonal_calibration_progress,
    get_seasonal_calibration_status,
)
from packages.optimizer.executor_core import PlanExecutor
from packages.optimizer.shower_mode import ShowerDetector

NOW = dt.datetime(2026, 10, 16, 6, 22, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("terminal", ["operator_invalidated", "recovered", "timeout"])
async def test_CG_T2_T6_stale_recovery_cannot_reopen_terminal_event(terminal):
    event = ShowerEventRecord(
        id=26,
        device_id="test-device",
        status=terminal,
        activation_action_id=11762,
        expires_at=NOW - dt.timedelta(minutes=1),
        pre_shower_temp=50,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=True),
        execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None)),
    )
    await ShowerDetector()._check_recovery(
        session,
        event,
        DeviceStatusRecord(ts=NOW, device_id="test-device", tank_temp=55),
    )
    assert event.status == terminal
    session.execute.assert_awaited_once()
    sql = str(session.execute.call_args.args[0])
    assert "FOR UPDATE" in sql
    assert "shower_events.device_id" in sql and "shower_events.status IN" in sql
    assert session.execute.call_args.args[0].get_execution_options()["populate_existing"]


async def test_REPAIR2_owner_busy_never_mutates_or_reads_stale_event():
    event = ShowerEventRecord(id=26, status="active")
    session = SimpleNamespace(scalar=AsyncMock(return_value=False), execute=AsyncMock())
    await ShowerDetector()._check_recovery(
        session,
        event,
        DeviceStatusRecord(ts=NOW, device_id="test-device"),
    )
    assert event.status == "active"
    session.execute.assert_not_awaited()


async def test_REPAIR2_verified_OFF_only_closes_matching_open_link():
    session = SimpleNamespace(execute=AsyncMock())
    action = PlanActionRecord(
        action_type="force_dhw_off", reverts_action_id=11762, device_id="test-device"
    )
    await complete_linked_shower(session, action, verified_at=NOW)
    statement = session.execute.call_args.args[0]
    params = statement.compile().params
    assert params["activation_action_id_1"] == 11762
    assert params["device_id_1"] == "test-device"
    assert params["status"] == "recovered" and params["recovered_at"] == NOW
    assert "operator_invalidated" not in params["status_1"]
    action.action_type = "zone_temp_restore"
    session.execute.reset_mock()
    await complete_linked_shower(session, action, verified_at=NOW)
    session.execute.assert_not_awaited()


async def test_REPAIR2_normal_verified_OFF_atomically_closes_linked_event(monkeypatch):
    from tests.test_safety_reverts import _S1Clock, _S1RestoreStore

    store = _S1RestoreStore(restore_type="force_dhw_off", clock=_S1Clock(monkeypatch))
    event = ShowerEventRecord(
        id=26,
        device_id=store.device_id,
        activation_action_id=store.source.id,
        status="timeout_pending",
    )
    store.events.append(event)
    await store.executor().execute_due_actions()
    assert store.restore.status == "executed"
    assert event.status == "recovered" and event.recovered_at is not None
    assert len(store.audits) == 1
    assert store.audits[0].result == "success"


@pytest.mark.parametrize("failure", [None, "audit", "fence"])
async def test_REPAIR2_action_event_audit_share_fenced_transaction(monkeypatch, failure):
    stored = PlanActionRecord(
        id=11763,
        reverts_action_id=11762,
        device_id="test-device",
        status="executing",
        action_type="force_dhw_off",
        payload_json="{}",
        safety_claim_token=uuid4(),
    )
    changes, committed = [], []
    session = SimpleNamespace()

    async def execute(statement):
        sql = str(statement)
        if sql.startswith("SELECT"):
            return MagicMock(scalar_one_or_none=lambda: stored)
        changes.append("event" if "UPDATE shower_events" in sql else "action")
        return SimpleNamespace(rowcount=1)

    def add(audit):
        assert audit.action == "force_dhw_off"
        if failure == "audit":
            raise RuntimeError("audit failed")
        changes.append("audit")

    session.execute = execute
    session.add = add

    @asynccontextmanager
    async def sessions():
        try:
            yield session
        except BaseException:
            changes.clear()
            raise
        else:
            committed.extend(changes)

    executor = PlanExecutor(MagicMock(), session_factory=sessions)
    monkeypatch.setattr(executor, "_assert_safety_authority", AsyncMock())

    async def bounded(coro, deadline):
        return await coro

    attempt = SimpleNamespace(
        _bounded=bounded,
        remaining=lambda **kwargs: 10,
        check_fence=AsyncMock(
            side_effect=RuntimeError("lost fence") if failure == "fence" else None
        ),
    )
    monkeypatch.setattr(executor, "_attempt_for", lambda action: attempt)
    operation = executor._update_safety_action(
        stored,
        {"status": "executed", "executed_at": NOW},
        audit=AuditLogRecord(action="force_dhw_off"),
        cleanup=True,
    )
    if failure:
        with pytest.raises(RuntimeError):
            await operation
        assert committed == []
        assert stored.status == "executing"
    else:
        await operation
        assert committed == ["action", "event", "audit"]
        assert stored.status == "executed"
    attempt.check_fence.assert_awaited_once()


def values(active):
    from packages.core.settings_service import SETTING_SPECS

    settings = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    settings["seasonal_calibration_enabled"] = "true"
    settings["_seasonal_calibration_campaign_since"] = NOW.isoformat() if active else ""
    return settings


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "collect_space_heating_evidence"),
        (1, "resolve_current_safety_obligations"),
        (None, "safety_obligations_unavailable"),
    ],
)
async def test_CG_T2_T8_safety_priority_and_retired_only_clear(active, count, expected):
    settings = values(active)
    evidence = {
        "average_outdoor": 5,
        "outdoor_readings": 24,
        "open_revert_obligations": {"count": count},
        "reset_history": {
            "available": True,
            "data_reset_at": NOW.isoformat(),
            "historical_retirement": {
                "disposition": "operator_invalidated",
                "physical_state_verified": False,
            },
        },
    }
    component = {
        "collection": {
            "availability": "assessed",
            "total": 0,
            "minimum_train": 168,
            "remaining_train": 168,
            "remaining_holdout": 24,
            "train": 0,
            "holdout": 0,
        },
        "fit": {"status": "unqualified"},
        "reporting": {"status": "unqualified"},
        "next_step": "collect",
    }
    snapshot = {
        "as_of": NOW,
        "context": {"settings": settings},
        "seasonal_inputs": evidence,
        "readiness": {"components": {"demand": component, "indoor_heating": component}},
        "model_lifecycle": {"demand": {"trained": False}},
    }
    result = await get_seasonal_calibration_progress(snapshot=snapshot)
    assert result["next_step"] == expected
    assert result["observe_only_active"] is active
    assert settings["_seasonal_calibration_campaign_since"] == (NOW.isoformat() if active else "")
    assert result["reset_history"]["historical_retirement"]["physical_state_verified"] is False
    # Restart reporting uses only current statuses, never resurrecting audit retirements.
    again = await get_seasonal_calibration_status(
        now=NOW,
        context={"settings": settings},
        materialized=evidence,
    )
    assert again["open_revert_obligations"]["count"] == count


def test_REPAIR3_terminal_disposition_never_enters_unresolved_predicate():
    params = unresolved_revert_predicate().compile().params
    assert params["status_1"] == ["pending", "executing", "dispatched"]
    assert "operator_invalidated" not in json.dumps(params)


@pytest.mark.parametrize("history_state", ["retired", "unavailable", "absent"])
async def test_CG_T8_learning_API_separates_retirement_from_current_unknown(
    monkeypatch, history_state
):
    from packages.api.routers.optimizer import _learning_mode_status

    settings = values(True)
    settings["learning_mode_enabled"] = "true"
    settings["learning_mode_since"] = NOW.isoformat()
    history = {
        "available": True,
        "data_reset_at": NOW.isoformat(),
        "historical_retirement": {
            "disposition": "operator_invalidated",
            "physical_state_verified": False,
        },
    }
    if history_state == "unavailable":
        history = {"available": False, "data_reset_at": None, "historical_retirement": None}
    materialized = {"open_revert_obligations": {"count": None}}
    if history_state != "absent":
        materialized["reset_history"] = history
    result = await _learning_mode_status(
        context={"settings": settings},
        materialized=materialized,
        now=NOW,
    )
    assert result["open_revert_obligations"]["count"] is None
    assert result["reset_history"] == (None if history_state == "absent" else history)
    assert result["since"] == NOW.isoformat()
    assert result["linked_restores_eligible"] is False


async def test_CG_T7_ordinary_log_reset_preserves_minimal_operation_audits(monkeypatch):
    from packages.api.routers import admin
    from packages.core.data_reset import AUDIT_ACTIONS

    statements = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=0),
        add=MagicMock(),
        execute=AsyncMock(side_effect=lambda statement: statements.append(statement)),
    )

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(admin, "get_session", sessions)
    await admin.reset_data(admin.ResetRequest(scopes=["logs"], reset_models=False))
    audit_deletes = [stmt for stmt in statements if stmt.table.name == "audit_log"]
    assert len(audit_deletes) == 1
    assert "NOT IN" in str(audit_deletes[0])
    assert tuple(audit_deletes[0].compile().params["action_1"]) == AUDIT_ACTIONS

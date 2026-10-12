from __future__ import annotations

import asyncio
import ast
import datetime as dt
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from packages.core.models import ShowerEventRecord
from packages.core.operational_alerts import get_operational_alerts
from packages.core.resilience import RateLimiter, SafetyWriteCapacityError, safety_write_context
from packages.core.safety_reverts import UNRESOLVED_STATUSES
from packages.core.service_health import (
    record_safety_watchdog_result,
    service_heartbeat_details,
)
from packages.optimizer import executor_core
from packages.optimizer.actions import ActionType, VerifyResult
from packages.optimizer.executor_core import LearningModeState, PlanExecutor
from packages.optimizer.main import _validate_unique_restore_keys
from packages.optimizer.shower_mode import ShowerDetector
from tests.test_safety_reverts import _S1Clock, _S1RestoreStore


class _AsyncContext:
    def __init__(self, value, *, on_exit=None):
        self.value = value
        self.on_exit = on_exit

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        if self.on_exit is not None:
            self.on_exit(exc_type, exc)
        return False


def _values(statement) -> dict[str, object]:
    return {
        key.key if hasattr(key, "key") else str(key): (
            value.value if hasattr(value, "value") else value
        )
        for key, value in getattr(statement, "_values", {}).items()
    }


def _action(*, action_id=1, attempt_count=0, action_type="force_dhw_off"):
    payload = {"zone_id": 1}
    if action_type == "zone_temp_restore":
        payload["temperature"] = 21
    return SimpleNamespace(
        id=action_id,
        plan_id=99,
        device_id="device-a",
        action_type=action_type,
        reverts_action_id=2 if action_type in {"force_dhw_off", "zone_temp_restore"} else None,
        payload_json=json.dumps(payload),
        status="pending",
        safety_attempt_count=attempt_count,
        safety_next_retry_at=None,
    )


def _factory(session):
    @asynccontextmanager
    async def make_session():
        yield session

    return make_session


_ATTEMPT_BOUNDS = {"overall": 110, "work": 40, "verification": 60, "cancel_drain": 5}


def _owned_executor(store, *, wrapper=None, **overrides):
    """Executor wired to the fake row store and its dedicated session fence."""
    options = {
        "sleep": store.clock.sleep if store.clock is not None else AsyncMock(),
        "learning_check": AsyncMock(return_value=LearningModeState.INACTIVE),
        "device_quality_check": AsyncMock(
            return_value={"ready": True, "reasons": [], "threshold_seconds": 900}
        ),
        **overrides,
    }
    return PlanExecutor(
        store.wrapper if wrapper is None else wrapper,
        session_factory=store.sessions,
        connection_factory=store.connection.context,
        **options,
    )


async def _claim_owned_restore(executor, store):
    """Enter through the real claim, device fence acquisition, bind and CAS path."""
    action = await executor._claim_due_safety_action(executor_core.dt.datetime.now(dt.timezone.utc))
    assert action is not None and action.id == store.restore.id
    assert action.status == store.restore.status == "executing"
    assert action.safety_claim_token is not None
    assert action.safety_claim_token == store.restore.safety_claim_token
    attempt = executor._safety_attempts[action.safety_claim_token]
    assert (attempt.action_id, attempt.source_id, attempt.device_id) == (
        store.restore.id,
        store.source.id,
        store.device_id,
    )
    assert store.connection.locked
    return action, attempt


def _assert_released(executor, attempt, store):
    assert attempt.token not in executor._safety_attempts
    assert attempt._released and not store.connection.locked
    assert store.connection.events[-1] == "exit"


def _pending_writes(store):
    return [
        statement
        for statement in store.statements
        if getattr(statement, "is_update", False) and _values(statement).get("status") == "pending"
    ]


class TestPhase2ExecutorAcceptance:
    @staticmethod
    def _dispatch_executor(*query_results):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=query_results)
        wrapper = AsyncMock()
        wrapper.get_selected_device_id = AsyncMock(return_value="device-a")
        executor = PlanExecutor(
            wrapper,
            session_factory=_factory(session),
            device_quality_check=AsyncMock(
                return_value={"ready": True, "reasons": [], "threshold_seconds": 900}
            ),
        )
        return executor, wrapper, session

    _DISPATCH_NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)

    @classmethod
    def _frozen_dispatch_clock(cls):
        frozen_now = cls._DISPATCH_NOW

        class FrozenDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

        clock = SimpleNamespace(
            datetime=FrozenDateTime, timezone=dt.timezone, timedelta=dt.timedelta
        )
        return patch.object(executor_core, "dt", clock)

    @classmethod
    def _status_result(cls, *, zone1=20, zone2=20, age_seconds=60, **status_fields):
        status_fields.setdefault("ts", cls._DISPATCH_NOW - dt.timedelta(seconds=age_seconds))
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(
            zone1_target_temp=zone1, zone2_target_temp=zone2, **status_fields
        )
        return result

    @staticmethod
    def _unresolved_result(*actions):
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(actions)
        return result

    @staticmethod
    def _revert(action_type, *, device_id="device-a", zone_id=1):
        return SimpleNamespace(
            action_type=action_type,
            device_id=device_id,
            payload_json=json.dumps({"zone_id": zone_id}),
            status="pending",
            reverts_action_id=1,
        )

    @pytest.mark.asyncio
    async def test_L_AC1_learning_mode_blocks_all_claims_and_preserves_pending_safety_obligation(
        self,
    ):
        session = AsyncMock()
        safety_action = _action()
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=_factory(session),
            learning_check=AsyncMock(return_value=LearningModeState.ACTIVE),
        )
        executor._claim_due_safety_action = AsyncMock(return_value=safety_action)
        executor._execute_safety_action = AsyncMock()
        executor._execute_action = AsyncMock()

        await executor.execute_due_actions()

        executor._claim_due_safety_action.assert_not_awaited()
        executor._execute_safety_action.assert_not_awaited()
        executor._execute_action.assert_not_awaited()
        assert safety_action.status == "pending"
        assert safety_action.safety_attempt_count == 0
        assert safety_action.safety_next_retry_at is None
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_L_AC4_first_control_enabled_cycle_executes_oldest_due_safety_only(self):
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=_factory(AsyncMock()),
            learning_check=AsyncMock(return_value=LearningModeState.INACTIVE),
        )
        oldest_safety = _action(action_id=1)
        executor._claim_due_safety_action = AsyncMock(return_value=oldest_safety)
        executor._execute_safety_action = AsyncMock()
        executor._execute_action = AsyncMock()

        await executor.execute_due_actions()

        executor._claim_due_safety_action.assert_awaited_once()
        executor._execute_safety_action.assert_awaited_once_with(oldest_safety)
        executor._execute_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_L_AC7_seasonal_learning_blocks_all_claims(self):
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=_factory(AsyncMock()),
            learning_check=AsyncMock(return_value=LearningModeState.ACTIVE),
        )
        executor._claim_due_safety_action = AsyncMock()

        await executor.execute_due_actions()

        executor._claim_due_safety_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_lookup_error_runs_one_due_safety_revert_and_skips_ordinary(self):
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=_factory(AsyncMock()),
            learning_check=AsyncMock(side_effect=ValueError("invalid learning state")),
        )
        safety_action = _action()
        executor._claim_due_safety_action = AsyncMock(return_value=safety_action)
        executor._execute_safety_action = AsyncMock()
        executor._execute_action = AsyncMock()

        with capture_logs() as logs:
            await executor.execute_due_actions()

        executor._claim_due_safety_action.assert_awaited_once()
        executor._execute_safety_action.assert_awaited_once_with(safety_action)
        executor._execute_action.assert_not_awaited()
        assert any(log["event"] == "learning_mode_state_unknown" for log in logs)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("attempt_count", "minutes"), [(0, 1), (1, 1), (2, 1), (3, 15), (10, 15)]
    )
    async def test_P2_AC7_retry_cadence_is_one_one_one_then_fifteen(self, attempt_count, minutes):
        store = _S1RestoreStore(restore_type="force_dhw_off")
        store.restore.safety_attempt_count = attempt_count
        executor = _owned_executor(store)
        action, attempt = await _claim_owned_restore(executor, store)
        before = dt.datetime.now(dt.timezone.utc)

        with patch.object(
            executor,
            "_dispatch_precondition",
            new=AsyncMock(return_value={"reason": "device_status_stale"}),
        ):
            await executor._execute_safety_action(action)

        retry_at = store.restore.safety_next_retry_at
        assert isinstance(retry_at, dt.datetime)
        assert (
            dt.timedelta(minutes=minutes - 1)
            < retry_at - before
            < dt.timedelta(minutes=minutes + 1)
        )
        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == attempt_count + 1
        result = json.loads(store.restore.result_json)
        assert result["reason"] == "device_status_stale"
        assert result["safety_attempt"]["bounds_seconds"] == _ATTEMPT_BOUNDS
        assert result["safety_attempt"]["drain"] == "confirmed"
        assert len(_pending_writes(store)) == 1
        store.device.set_force_dhw.assert_not_awaited()
        _assert_released(executor, attempt, store)

    @pytest.mark.asyncio
    async def test_P2_AC7_cancellation_requeues_claimed_safety_action_without_redispatch(self):
        store = _S1RestoreStore(restore_type="force_dhw_off")
        executor = _owned_executor(store)

        with patch.object(
            executor,
            "_dispatch_precondition",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await executor.execute_due_actions()

        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == 1
        result = json.loads(store.restore.result_json)
        assert result["reason"] == executor_core.SHUTDOWN_CANCEL_REASON
        assert result["safety_attempt"]["drain"] == "confirmed"
        assert len(_pending_writes(store)) == 1
        store.device.set_force_dhw.assert_not_awaited()
        assert not executor._safety_attempts
        assert not store.connection.locked and store.connection.events[-1] == "exit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason", ["device_status_stale", "credentials_missing", "breaker_open"]
    )
    async def test_P2_AC8_deferrals_count_as_failed_safety_cycles(self, reason):
        store = _S1RestoreStore(restore_type="force_dhw_off")
        executor = _owned_executor(store)
        action, attempt = await _claim_owned_restore(executor, store)

        with (
            patch.object(
                executor,
                "_dispatch_precondition",
                new=AsyncMock(return_value={"reason": reason}),
            ),
            patch.object(
                executor,
                "_requeue_safety_action",
                new=AsyncMock(side_effect=executor._requeue_safety_action),
            ) as requeue,
        ):
            await executor._execute_safety_action(action)

        requeue.assert_awaited_once_with(action, reason)
        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == 1
        assert json.loads(store.restore.result_json)["reason"] == reason
        store.device.set_force_dhw.assert_not_awaited()
        _assert_released(executor, attempt, store)

    @pytest.mark.asyncio
    async def test_P2_AC8_safety_lane_quality_check_exception_requeues_and_counts(
        self, monkeypatch
    ):
        clock = _S1Clock(monkeypatch)
        sentinel = "P2_AC8_QUALITY_EXCEPTION_SENTINEL"
        store = _S1RestoreStore(restore_type="force_dhw_off", clock=clock)
        store.wrapper.safety_write = MagicMock(side_effect=AssertionError("safety write"))
        quality_check = AsyncMock(side_effect=RuntimeError(sentinel))
        executor = _owned_executor(store, device_quality_check=quality_check)
        action, attempt = await _claim_owned_restore(executor, store)
        frozen_now = clock.utcnow()

        with capture_logs() as logs:
            await executor._execute_safety_action(action)

        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == 1
        assert store.restore.safety_next_retry_at == frozen_now + dt.timedelta(minutes=1)
        assert json.loads(store.restore.result_json)["reason"] == "quality_check_failed"
        store.wrapper.safety_write.assert_not_called()
        assert sentinel not in repr(logs)
        _assert_released(executor, attempt, store)

        for expected_count, minutes in ((2, 1), (3, 1), (4, 15)):
            clock.advance(60)
            action, attempt = await _claim_owned_restore(executor, store)
            cycle_now = clock.utcnow()
            await executor._execute_safety_action(action)
            assert store.restore.safety_attempt_count == expected_count
            assert store.restore.safety_next_retry_at == cycle_now + dt.timedelta(minutes=minutes)
            _assert_released(executor, attempt, store)

        assert quality_check.await_count == 4
        store.wrapper.safety_write.assert_not_called()
        store.device.set_force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_P2_AC7_safety_cycle_performs_one_write_and_no_redispatch(self):
        store = _S1RestoreStore(restore_type="force_dhw_off")
        wrapper = MagicMock()
        wrapper.get_selected_device_id = AsyncMock(return_value=store.device_id)
        wrapper._dispatch_linked_restore = AsyncMock(return_value={"force_dhw": 0})
        executor = _owned_executor(store, wrapper=wrapper)
        action, attempt = await _claim_owned_restore(executor, store)
        handler = MagicMock()
        handler.dispatch = AsyncMock(return_value={"force_dhw": 0})
        handler.redispatch_expected = AsyncMock()
        handler.verify = MagicMock(
            return_value=VerifyResult(
                ok=False, observed_value=1, expected_value=0, reason="not_restored"
            )
        )
        failed = VerifyResult(ok=False, observed_value=1, expected_value=0, reason="timeout")

        with (
            patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
            patch.object(executor, "_safety_already_safe", new=AsyncMock(return_value=False)),
            patch.object(
                executor,
                "_read_safety_observation",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch("packages.optimizer.executor_core.get_action_handler", return_value=handler),
            patch.object(
                executor,
                "_poll_until_verified",
                new=AsyncMock(return_value=(failed, 1)),
            ),
            patch.object(
                executor,
                "_requeue_safety_action",
                new=AsyncMock(side_effect=executor._requeue_safety_action),
            ) as requeue,
        ):
            await executor._execute_safety_action(action)

        wrapper._dispatch_linked_restore.assert_awaited_once_with(
            action, store.source, attempt=attempt
        )
        handler.dispatch.assert_not_awaited()
        handler.redispatch_expected.assert_not_awaited()
        requeue.assert_awaited_once_with(action, "timeout")
        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == 1
        _assert_released(executor, attempt, store)

    @pytest.mark.asyncio
    async def test_P2_AC7_safety_attempt_calls_the_wrapper_write_once(self, monkeypatch):
        clock = _S1Clock(monkeypatch)
        store = _S1RestoreStore(restore_type="force_dhw_off", clock=clock)
        store.wrapper.force_dhw = AsyncMock(return_value=True)
        executor = _owned_executor(store)
        action, attempt = await _claim_owned_restore(executor, store)

        await executor._execute_safety_action(action)

        store.wrapper.force_dhw.assert_awaited_once()
        store.device.set_force_dhw.assert_not_awaited()
        assert store.restore.status == "pending"
        assert store.restore.safety_attempt_count == 1
        _assert_released(executor, attempt, store)

    @pytest.mark.asyncio
    async def test_A1_watchdog_failure_does_not_suppress_expiry_or_execution(self):
        wrapper = AsyncMock()
        expiry = AsyncMock()
        execute = AsyncMock()
        health = AsyncMock()
        with (
            patch("packages.optimizer.main.PlanExecutor") as executor_class,
            patch(
                "packages.optimizer.shower_mode.reconcile_shower_expiry",
                new=AsyncMock(side_effect=RuntimeError("watchdog down")),
            ),
            patch("packages.core.service_health.record_safety_watchdog_result", health),
        ):
            executor_class.return_value.expire_stale_actions = expiry
            executor_class.return_value.execute_due_actions = execute
            from packages.optimizer.main import execute_pending_actions

            await execute_pending_actions(wrapper)

        health.assert_awaited_once_with(success=False, reason="RuntimeError")
        expiry.assert_awaited_once_with()
        execute.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_A1_watchdog_health_write_is_independent_and_persisted(self):
        row = SimpleNamespace(details_json=None, updated_at=None)
        session = AsyncMock()
        session.get = AsyncMock(return_value=row)
        with patch(
            "packages.core.service_health.get_session",
            return_value=_AsyncContext(session),
        ):
            for reason in ("freshness", "credentials", "breaker"):
                await record_safety_watchdog_result(success=False, reason=reason)
            await record_safety_watchdog_result(success=True)

        details = service_heartbeat_details(row)
        assert details["consecutive_failures"] == 0
        assert details["last_reason"] == "breaker"
        assert details["last_success_at"]

    def test_A2_safety_status_enumeration_remains_exact_for_action_queries(self):
        assert set(UNRESOLVED_STATUSES) == {"pending", "executing", "dispatched"}

    def test_P2_duplicate_restore_link_is_rejected(self):
        with pytest.raises(ValueError, match="duplicate safety restore key: zone_boost:one"):
            _validate_unique_restore_keys(
                [
                    {"reverts_action_key": "zone_boost:one"},
                    {"reverts_action_key": "zone_boost:one"},
                ]
            )

    @pytest.mark.asyncio
    async def test_A2_skipped_events_are_not_selected_by_watchdog_query(self):
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        await ShowerDetector()._get_active_event(session, "device-a")

        statement = session.execute.await_args.args[0]
        params = statement.compile(dialect=postgresql.dialect()).params
        statuses = next(value for value in params.values() if isinstance(value, (list, tuple)))
        assert set(statuses) == {"active", "recovery_pending", "timeout_pending"}

    @pytest.mark.asyncio
    async def test_P2_AC14_missing_baseline_or_current_target_never_counts_as_safe(self):
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(
            force_dhw=0, zone1_target_temp=None
        )
        session.execute = AsyncMock(return_value=result)
        session.get = AsyncMock(
            return_value=SimpleNamespace(payload_json='{"baseline_temperature": 21}')
        )
        executor = PlanExecutor(AsyncMock(), session_factory=_factory(session))

        assert not await executor._safety_already_safe(
            _action(action_type="zone_temp_restore"),
            ActionType.ZONE_TEMP_RESTORE,
            {"zone_id": 1},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_target", [21, None])
    async def test_P2_AC14_zone_restore_only_completes_on_fresh_exact_baseline(
        self, current_target
    ):
        store = _S1RestoreStore(restore_type="zone_temp_restore")
        store.source.payload_json = json.dumps(
            {"zone_id": 1, "baseline_temperature": 21, "temperature": 23}
        )
        store.restore.payload_json = json.dumps({"zone_id": 1, "temperature": 21})
        now = dt.datetime.now(dt.timezone.utc)
        source_settled_at = now - dt.timedelta(seconds=5)
        store.source.result_json = json.dumps(
            {
                "dispatch_provenance": {
                    "schema": "source_dispatch_v1",
                    "drain": "confirmed",
                    "device_id": store.device_id,
                    "settled_at": source_settled_at.isoformat(),
                }
            }
        )
        store.status.ts = now
        store.status.zone1_target_temp = current_target
        store.status.native_policy_provenance = {
            "captured_at": now.isoformat(),
            "timing": {
                "observed_at": None,
                "retrieved_at": (source_settled_at + dt.timedelta(seconds=1)).isoformat(),
            },
        }
        wrapper = MagicMock()
        wrapper.get_selected_device_id = AsyncMock(return_value=store.device_id)
        wrapper._dispatch_linked_restore = AsyncMock(return_value={"skip": True})
        executor = _owned_executor(store, wrapper=wrapper)
        action, attempt = await _claim_owned_restore(executor, store)

        with (
            patch.object(
                executor,
                "_read_safety_observation",
                new=AsyncMock(return_value=SimpleNamespace()),
            ) as observation,
            patch.object(executor, "_mark_verified", new=AsyncMock()) as verified,
            patch.object(
                executor,
                "_requeue_safety_action",
                new=AsyncMock(side_effect=executor._requeue_safety_action),
            ) as requeue,
            patch("packages.optimizer.executor_core.get_action_handler") as handler_factory,
        ):
            handler_factory.return_value.dispatch = AsyncMock(return_value={"skip": True})
            handler_factory.return_value.verify = MagicMock(
                return_value=VerifyResult(
                    ok=False, observed_value=None, expected_value=21, reason="target_unknown"
                )
            )
            await executor._execute_safety_action(action)

        handler_factory.return_value.dispatch.assert_not_awaited()
        if current_target == 21:
            verified.assert_awaited_once()
            requeue.assert_not_awaited()
            observation.assert_not_awaited()
            wrapper._dispatch_linked_restore.assert_not_awaited()
        else:
            verified.assert_not_awaited()
            requeue.assert_awaited_once_with(action, "live_precondition")
            wrapper._dispatch_linked_restore.assert_awaited_once_with(
                action, store.source, attempt=attempt
            )
            assert store.restore.status == "pending"
        _assert_released(executor, attempt, store)

    @pytest.mark.asyncio
    async def test_P2_zone_restore_source_baseline_disagreement_retries_without_write(self):
        store = _S1RestoreStore(restore_type="zone_temp_restore")
        store.source.payload_json = json.dumps(
            {"zone_id": 1, "baseline_temperature": 22, "temperature": 24}
        )
        store.restore.payload_json = json.dumps({"zone_id": 1, "temperature": 21})
        store.status.zone1_target_temp = 21
        wrapper = MagicMock()
        wrapper.get_selected_device_id = AsyncMock(return_value=store.device_id)
        wrapper._dispatch_linked_restore = AsyncMock()
        executor = _owned_executor(store, wrapper=wrapper)

        with (
            patch("packages.optimizer.executor_core.get_action_handler") as handler_factory,
            capture_logs() as logs,
        ):
            handler_factory.return_value.dispatch = AsyncMock()
            await executor.execute_due_actions()

        handler_factory.return_value.dispatch.assert_not_awaited()
        wrapper._dispatch_linked_restore.assert_not_awaited()
        assert store.restore.status == "pending"
        assert store.restore.safety_claim_token is None
        assert store.restore.safety_attempt_count == 0
        assert not [s for s in store.statements if getattr(s, "is_update", False)]
        assert not executor._safety_attempts and not store.connection.locked
        assert {
            "event": "safety_restore_admission_deferred",
            "action_id": store.restore.id,
            "reason": "ValueError",
        }.items() <= next(
            log for log in logs if log["event"] == "safety_restore_admission_deferred"
        ).items()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("defect", "expected_status", "expected_attempts"),
        [
            ("valid", "executed", 0),
            ("ownerless", "pending", 0),
            ("token", "executing", 0),
            ("device", "executing", 0),
            ("source", "pending", 1),
            ("fence", "executing", 0),
            ("cas", "executing", 0),
        ],
    )
    async def test_P2_AC7_restore_dispatch_requires_live_owned_claim(
        self, monkeypatch, defect, expected_status, expected_attempts
    ):
        from uuid import uuid4

        from aioaquarea import ForceDHW

        clock = _S1Clock(monkeypatch)
        store = _S1RestoreStore(restore_type="force_dhw_off", clock=clock, detached=True)
        executor = _owned_executor(store)
        if defect == "ownerless":
            with pytest.raises(executor_core._SafetyAuthorityLost):
                await executor._execute_safety_action(store.restore)
            with pytest.raises(executor_core._SafetyAuthorityLost):
                await executor._requeue_safety_action(store.restore, "ownerless")
            assert store.connection.events == []
        else:
            action, attempt = await _claim_owned_restore(executor, store)
            if defect == "token":
                store.restore.safety_claim_token = uuid4()
            elif defect == "device":
                store.restore.device_id = "other-device"
            elif defect == "source":
                store.source.payload_json = json.dumps({"changed": True})
            elif defect == "fence":
                store.connection.locked = False
            elif defect == "cas":
                persisted_execute = store.execute

                async def lose_cas(statement, parameters=None):
                    if getattr(statement, "is_update", False):
                        store.statements.append(statement)
                        return SimpleNamespace(rowcount=0)
                    return await persisted_execute(statement, parameters)

                store.execute = lose_cas

            await executor._execute_safety_action(action)

            assert attempt.token not in executor._safety_attempts and attempt._released
        assert store.restore.status == expected_status
        assert store.restore.safety_attempt_count == expected_attempts
        if defect == "valid":
            store.device.set_force_dhw.assert_awaited_once_with(ForceDHW.OFF)
        else:
            store.device.set_force_dhw.assert_not_awaited()
            assert not store.audits

    def test_P2_AC9_dispatch_precondition_has_an_embargo_recheck_boundary(self):
        source = inspect.getsource(PlanExecutor._dispatch_precondition)
        assert "unresolved_revert_predicate" in source
        assert "zone_embargoed" in source or "dhw_embargoed" in source

    @pytest.mark.asyncio
    async def test_P2_AC9_skips_ordinary_dhw_on_when_same_device_revert_is_unresolved(self):
        executor, wrapper, session = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("force_dhw_off")),
            MagicMock(),
        )
        action = _action(action_type="force_dhw_on")

        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(action, ActionType.FORCE_DHW_ON, {})
            assert result["reason"] == "blocked_by_unresolved_revert"
            with pytest.raises(PermissionError, match="observe-only build"):
                await executor._execute_action(action)
        assert session.execute.await_count == 2
        assert action.status == "pending"
        wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_P2_AC9_allows_ordinary_dhw_on_for_a_different_device(self):
        executor, _, _ = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("force_dhw_off", device_id="device-b")),
        )

        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(
                _action(action_type="force_dhw_on"), ActionType.FORCE_DHW_ON, {}
            )

        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_fields", "expected"),
        [
            ({"age_seconds": 900}, None),
            ({"age_seconds": 901}, "device_status_stale"),
            ({"ts": None}, "device_status_timing_unknown"),
            ({"ts": "not-a-timestamp"}, "device_status_timing_unknown"),
            ({"age_seconds": -1}, "device_status_future"),
            (
                {
                    "native_policy_provenance": {
                        "captured_at": "2026-10-12T11:59:00+00:00",
                        "timing": {"observed_at": "2026-10-12T11:59:30+00:00"},
                    }
                },
                "device_status_clock_chronology",
            ),
        ],
        ids=["exact_900_fresh", "901_stale", "missing", "malformed", "future", "chronology"],
    )
    async def test_P2_AC9_dispatch_status_clock_admission_boundaries(self, status_fields, expected):
        executor, wrapper, _ = self._dispatch_executor(
            self._status_result(**status_fields),
            self._unresolved_result(self._revert("force_dhw_off", device_id="device-b")),
        )

        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(
                _action(action_type="force_dhw_on"), ActionType.FORCE_DHW_ON, {}
            )

        if expected is None:
            assert result is None
        else:
            assert result == {"reason": expected, "device_id": "device-a"}
        wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_P2_AC9_blocks_zone_increase_only_for_the_matching_zone(self):
        action = _action(action_type="set_zone_heat_temperature")
        action.payload_json = json.dumps({"zone_id": 2, "temperature": 21})
        executor, _, _ = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("zone_temp_restore", zone_id=2)),
        )

        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(
                action, ActionType.SET_ZONE_HEAT_TEMPERATURE, {"zone_id": 2, "temperature": 21}
            )

        assert result["reason"] == "blocked_by_unresolved_revert"

        from packages.core.space_heating_gate import HeatingGateConfig

        config = HeatingGateConfig()
        gate_result = MagicMock()
        gate_result.scalar_one_or_none.return_value = SimpleNamespace(
            state="ALLOWED",
            reason_code="below_on_threshold",
            config_fingerprint=config.fingerprint,
            last_raw_outdoor_c=5.0,
        )
        executor, _, _ = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("zone_temp_restore", zone_id=2)),
            gate_result,
        )
        with (
            patch(
                "packages.optimizer.executor_core.get_space_heating_gate_config",
                new=AsyncMock(return_value=config),
            ),
            self._frozen_dispatch_clock(),
        ):
            result = await executor._dispatch_precondition(
                action, ActionType.SET_ZONE_HEAT_TEMPERATURE, {"zone_id": 1, "temperature": 21}
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_P2_AC9_allows_zone_decrease_and_tagged_restore(self):
        action = _action(action_type="set_zone_heat_temperature")
        executor, _, session = self._dispatch_executor(self._status_result())

        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(
                action, ActionType.SET_ZONE_HEAT_TEMPERATURE, {"zone_id": 1, "temperature": 19}
            )

        assert result is None
        assert session.execute.await_count == 1

        restore = _action(action_type="zone_temp_restore")
        restore.reverts_action_id = 1
        executor, _, session = self._dispatch_executor(self._status_result())
        with self._frozen_dispatch_clock():
            result = await executor._dispatch_precondition(
                restore, ActionType.ZONE_TEMP_RESTORE, {"zone_id": 1}
            )

        assert result is None
        assert session.execute.await_count == 1


class TestPhase2LimiterAcceptance:
    @pytest.mark.asyncio
    async def test_P2_AC10_lock_safe_reserve_allows_safety_while_ordinary_callers_wait(self):
        limiter = RateLimiter(max_tokens=20, refill_per_second=20 / 3600, reserve_tokens=2)
        limiter._tokens = 2
        ordinary = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        assert not ordinary.done()

        token = safety_write_context.set(True)
        try:
            await limiter.acquire()
            await limiter.acquire()
        finally:
            safety_write_context.reset(token)
            ordinary.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ordinary

        assert limiter._tokens < 1

    @pytest.mark.asyncio
    async def test_P2_AC10_safety_capacity_fails_promptly_without_sleeping(self):
        limiter = RateLimiter(max_tokens=20, refill_per_second=20 / 3600, reserve_tokens=2)
        limiter._tokens = 0
        token = safety_write_context.set(True)
        try:
            with pytest.raises(SafetyWriteCapacityError):
                await limiter.acquire()
        finally:
            safety_write_context.reset(token)

    @pytest.mark.asyncio
    async def test_A3_four_safety_attempts_leave_the_documented_sixteen_write_floor(self):
        limiter = RateLimiter(max_tokens=20, refill_per_second=0, reserve_tokens=2)
        limiter._tokens = 20
        for _ in range(16):
            await limiter.acquire()
        token = safety_write_context.set(True)
        try:
            for _ in range(4):
                await limiter.acquire()
        finally:
            safety_write_context.reset(token)

        assert limiter._tokens == 0
        token = safety_write_context.set(True)
        try:
            with pytest.raises(SafetyWriteCapacityError):
                await limiter.acquire()
        finally:
            safety_write_context.reset(token)


class TestPhase2ShowerAcceptance:
    @staticmethod
    def _status(ts, tank_temp, *, device_id="device-a", force_dhw=0):
        return SimpleNamespace(ts=ts, device_id=device_id, tank_temp=tank_temp, force_dhw=force_dhw)

    @pytest.mark.asyncio
    async def test_P2_AC15_blocked_shower_is_dropped_and_recorded(self):
        now = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        previous = self._status(now - dt.timedelta(minutes=5), 55)
        current = self._status(now, 43)
        previous_result = MagicMock()
        previous_result.scalar_one_or_none.return_value = previous
        blocked_result = MagicMock()
        blocked_result.scalar_one_or_none.return_value = 42
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[previous_result, blocked_result]), add=MagicMock()
        )
        detector = ShowerDetector()
        detector._is_peak_price = AsyncMock(return_value=False)

        with patch(
            "packages.optimizer.shower_mode.get_setting",
            AsyncMock(
                side_effect=lambda key: {
                    "shower_drop_threshold": "10",
                    "shower_max_duration_minutes": "60",
                }[key]
            ),
        ):
            await detector._check_for_drop(session, current)

        event = session.add.call_args.args[0]
        assert isinstance(event, ShowerEventRecord)
        assert event.status == "skipped_observe_only_build"
        assert event.activation_action_id is None
        assert event.expires_at == current.ts
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_P2_AC16_recovery_failure_escapes_transaction_for_rollback(self):
        now = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        previous = self._status(now - dt.timedelta(minutes=5), 55)
        current = self._status(now, 43)
        previous_result = MagicMock()
        previous_result.scalar_one_or_none.return_value = previous
        blocked_result = MagicMock()
        blocked_result.scalar_one_or_none.return_value = None
        exit_args = []

        def on_exit(exc_type, exc):
            exit_args.append((exc_type, exc))

        active_event = MagicMock()
        active_event.scalar_one_or_none.return_value = ShowerEventRecord(
            id=1,
            started_at=now - dt.timedelta(minutes=65),
            expires_at=now - dt.timedelta(minutes=5),
            activation_action_id=11762,
            pre_shower_temp=55,
            status="active",
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[active_event, RuntimeError("linked OFF reschedule failed")]
            ),
            add=MagicMock(),
            flush=AsyncMock(side_effect=RuntimeError("linked OFF insert failed")),
        )
        detector = ShowerDetector()
        detector._is_peak_price = AsyncMock(return_value=False)

        with (
            patch(
                "packages.optimizer.shower_mode.get_setting",
                AsyncMock(
                    side_effect=lambda key: {
                        "shower_mode_enabled": "true",
                        "shower_drop_threshold": "10",
                        "shower_max_duration_minutes": "60",
                    }[key]
                ),
            ),
            patch(
                "packages.optimizer.shower_mode.get_session",
                return_value=_AsyncContext(session, on_exit=on_exit),
            ),
        ):
            with pytest.raises(RuntimeError, match="linked OFF"):
                await detector.check(current)

        assert exit_args and exit_args[0][0] is RuntimeError
        session.add.assert_not_called()
        session.flush.assert_not_awaited()


class TestPhase2PollerArchitecture:
    def test_P2_AC17_poller_status_and_consumption_paths_have_no_mutating_wrapper_calls(self):
        from packages.poller import main as poller_main

        source = "\n".join(
            [
                inspect.getsource(poller_main.poll_device_status),
                inspect.getsource(poller_main.poll_consumption),
            ]
        )
        for mutator in ("force_dhw", "set_temperature", "set_mode", "set_quiet", "safety_write"):
            assert mutator not in source

    def test_P2_AC17_non_optimizer_wrapper_construction_is_read_only(self):
        root = Path(__file__).parents[1] / "packages"
        constructors = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "AquareaWrapper":
                    constructors.append((path.relative_to(root.parent).as_posix(), node))
        for path, constructor in constructors:
            if path == "packages/optimizer/main.py":
                continue
            assert any(
                keyword.arg == "read_only"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in constructor.keywords
            ), path


class TestPhase2AlertAcceptance:
    @staticmethod
    def _alert_session(*, safety_action=None, blocked_events=(), watchdog=None):
        now = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        heartbeat_rows = [
            SimpleNamespace(service="poller", updated_at=now),
            SimpleNamespace(service="optimizer", updated_at=now),
        ]
        if watchdog is not None:
            heartbeat_rows.append(
                SimpleNamespace(
                    service="safety_watchdog",
                    updated_at=now,
                    details_json=json.dumps({"consecutive_failures": watchdog}),
                )
            )
        session = SimpleNamespace()
        session.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: heartbeat_rows)),
                SimpleNamespace(scalar_one_or_none=lambda: now),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
                SimpleNamespace(scalar_one_or_none=lambda: safety_action),
                SimpleNamespace(scalar_one_or_none=lambda: None),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(blocked_events))),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
                SimpleNamespace(scalar_one_or_none=lambda: None),
            ]
        )
        return now, session

    @pytest.mark.asyncio
    async def test_P2_AC8_safety_alert_exposes_structured_revert_evidence(self):
        now = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        action = SimpleNamespace(
            id=17,
            plan_id=9,
            device_id="device-a",
            scheduled_ts=now - dt.timedelta(minutes=12),
            safety_attempt_count=3,
            result_json=json.dumps({"reason": "credentials_missing"}),
            payload_json=json.dumps({"zone_id": 2}),
        )
        blocked = [
            SimpleNamespace(started_at=now - dt.timedelta(minutes=4)),
            SimpleNamespace(started_at=now - dt.timedelta(minutes=8)),
        ]
        _, session = self._alert_session(safety_action=action, blocked_events=blocked)

        with (
            patch(
                "packages.core.operational_alerts.get_bool_setting", AsyncMock(return_value=True)
            ),
            patch("packages.core.operational_alerts.get_int_setting", AsyncMock(return_value=60)),
            patch(
                "packages.core.operational_alerts.get_device_data_quality",
                AsyncMock(return_value={"threshold_seconds": 900}),
            ),
            patch(
                "packages.core.operational_alerts.get_planning_data_quality",
                AsyncMock(return_value={"control_allowed": True}),
            ),
            patch(
                "packages.ml.forecast_quality.get_forecast_scorecard", AsyncMock(return_value={})
            ),
            patch(
                "packages.core.operational_alerts.service_heartbeat_details",
                side_effect=lambda row: (
                    {"consecutive_failures": 3}
                    if row is not None and row.service == "safety_watchdog"
                    else {}
                ),
            ),
            patch(
                "packages.core.operational_alerts.project_panasonic_adapter_state",
                return_value={"state_fresh": True, "status": "available"},
            ),
            patch(
                "packages.core.operational_alerts.get_session", return_value=_AsyncContext(session)
            ),
        ):
            result = await get_operational_alerts(now=now)

        alert = next(
            alert for alert in result["alerts"] if alert["id"] == "safety_revert_unresolved"
        )
        assert alert["severity"] == "critical"
        assert alert["details"]["device_id"] == "device-a"
        assert alert["details"]["zone"] == 2
        assert alert["plan_id"] == 9
        assert alert["action_id"] == 17
        assert alert["details"]["age_seconds"] == 720
        assert alert["details"]["attempt_count"] == 3
        assert alert["details"]["last_reason"] == "credentials_missing"
        assert alert["details"]["blocked_trigger_count"] == 2
        assert (
            alert["details"]["latest_blocked_trigger_at"]
            == (now - dt.timedelta(minutes=4)).isoformat()
        )
        assert alert["href"] == "/?view=timeline&activity=safety#plan-action-17"

    @pytest.mark.asyncio
    async def test_P2_AC8_watchdog_threshold_and_reset_are_durable(self):
        row = SimpleNamespace(details_json=None, updated_at=None)
        session = AsyncMock()
        session.get = AsyncMock(return_value=row)
        with patch(
            "packages.core.service_health.get_session",
            return_value=_AsyncContext(session),
        ):
            await record_safety_watchdog_result(success=False, reason="freshness")
            await record_safety_watchdog_result(success=False, reason="credentials")
            assert service_heartbeat_details(row)["consecutive_failures"] == 2
            await record_safety_watchdog_result(success=False, reason="breaker")
            assert service_heartbeat_details(row)["consecutive_failures"] == 3
            await record_safety_watchdog_result(success=True)

        details = service_heartbeat_details(row)
        assert details["consecutive_failures"] == 0
        assert details["last_reason"] == "breaker"

    @pytest.mark.asyncio
    async def test_P2_AC8_watchdog_alerts_at_three_and_resets_after_success(self):
        async def alert_ids(failures):
            now, session = self._alert_session(watchdog=failures)
            with (
                patch(
                    "packages.core.operational_alerts.get_bool_setting",
                    AsyncMock(return_value=True),
                ),
                patch(
                    "packages.core.operational_alerts.get_int_setting", AsyncMock(return_value=60)
                ),
                patch(
                    "packages.core.operational_alerts.get_device_data_quality",
                    AsyncMock(return_value={"threshold_seconds": 900}),
                ),
                patch(
                    "packages.core.operational_alerts.get_planning_data_quality",
                    AsyncMock(return_value={"control_allowed": True}),
                ),
                patch(
                    "packages.ml.forecast_quality.get_forecast_scorecard",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "packages.core.operational_alerts.service_heartbeat_details",
                    side_effect=lambda row: (
                        json.loads(getattr(row, "details_json", None))
                        if row is not None and getattr(row, "details_json", None)
                        else {}
                    ),
                ),
                patch(
                    "packages.core.operational_alerts.project_panasonic_adapter_state",
                    return_value={"state_fresh": True, "status": "available"},
                ),
                patch(
                    "packages.core.operational_alerts.get_session",
                    return_value=_AsyncContext(session),
                ),
            ):
                result = await get_operational_alerts(now=now)
            return {alert["id"] for alert in result["alerts"]}, session

        for failures, expected in ((2, False), (3, True), (0, False)):
            session_ids, session = await alert_ids(failures)
            assert ("safety_watchdog_failing" in session_ids) is expected
            if failures == 2:
                statement = session.execute.await_args_list[0].args[0]
                assert "safety_watchdog" in str(
                    statement.compile(dialect=postgresql.dialect()).params
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["freshness", "credentials", "breaker"])
    async def test_P2_AC8_all_safety_deferrals_increment_attempts(self, reason):
        store = _S1RestoreStore(restore_type="force_dhw_off")
        store.restore.safety_attempt_count = 2
        executor = _owned_executor(store)
        action, attempt = await _claim_owned_restore(executor, store)

        with patch.object(
            executor,
            "_dispatch_precondition",
            new=AsyncMock(return_value={"reason": reason}),
        ):
            await executor._execute_safety_action(action)

        values = _values(_pending_writes(store)[-1])
        assert len(_pending_writes(store)) == 1
        assert values["safety_attempt_count"] == 3
        assert json.loads(values["result_json"])["reason"] == reason
        assert store.restore.safety_attempt_count == 3
        store.device.set_force_dhw.assert_not_awaited()
        _assert_released(executor, attempt, store)

    def test_P2_AC12_downgrade_mapping_is_neutral(self):
        migration = (
            Path(__file__).parents[1] / "migrations/versions/029_safety_revert_obligations.py"
        ).read_text(encoding="utf-8")
        assert "UPDATE shower_events SET status = 'skipped_revert'" in migration

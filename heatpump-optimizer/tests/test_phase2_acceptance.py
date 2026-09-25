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
from packages.optimizer.actions import ActionType, VerifyResult
from packages.optimizer.executor_core import PlanExecutor
from packages.optimizer.main import _validate_unique_restore_keys
from packages.optimizer.shower_mode import ShowerDetector


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

    @staticmethod
    def _status_result(*, zone1=20, zone2=20):
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(
            zone1_target_temp=zone1, zone2_target_temp=zone2
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
    async def test_P2_AC6_one_oldest_safety_claim_skips_ordinary_actions_in_learning_override(self):
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = _action()
        session.execute = AsyncMock(return_value=result)
        executor = PlanExecutor(
            AsyncMock(),
            session_factory=_factory(session),
            learning_check=AsyncMock(return_value=True),
        )

        with patch.object(executor, "_execute_safety_action", new=AsyncMock()) as safety:
            await executor.execute_due_actions()

        safety.assert_awaited_once()
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("attempt_count", "minutes"), [(0, 1), (1, 1), (2, 1), (3, 15), (10, 15)]
    )
    async def test_P2_AC7_retry_cadence_is_one_one_one_then_fifteen(self, attempt_count, minutes):
        session = AsyncMock()
        executor = PlanExecutor(AsyncMock(), session_factory=_factory(session))
        action = _action(attempt_count=attempt_count)
        before = dt.datetime.now(dt.timezone.utc)

        await executor._requeue_safety_action(action, "device_status_stale")

        values = _values(session.execute.await_args.args[0])
        retry_at = values["safety_next_retry_at"]
        assert isinstance(retry_at, dt.datetime)
        assert (
            dt.timedelta(minutes=minutes - 1)
            < retry_at - before
            < dt.timedelta(minutes=minutes + 1)
        )
        assert values["status"] == "pending"

    @pytest.mark.asyncio
    async def test_P2_AC7_cancellation_requeues_claimed_safety_action_without_redispatch(self):
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = _action()
        session.execute = AsyncMock(return_value=result)
        executor = PlanExecutor(AsyncMock(), session_factory=_factory(session))

        with (
            patch.object(
                executor,
                "_execute_safety_action",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()) as requeue,
        ):
            with pytest.raises(asyncio.CancelledError):
                await executor.execute_due_actions()

        requeue.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason", ["device_status_stale", "credentials_missing", "breaker_open"]
    )
    async def test_P2_AC8_deferrals_count_as_failed_safety_cycles(self, reason):
        executor = PlanExecutor(AsyncMock(), session_factory=_factory(AsyncMock()))
        action = _action()

        with (
            patch.object(
                executor,
                "_dispatch_precondition",
                new=AsyncMock(return_value={"reason": reason}),
            ),
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()) as requeue,
        ):
            await executor._execute_safety_action(action)

        requeue.assert_awaited_once_with(action, reason)

    @pytest.mark.asyncio
    async def test_P2_AC8_safety_lane_quality_check_exception_requeues_and_counts(
        self, monkeypatch
    ):
        from packages.optimizer import executor_core

        frozen_now = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)

        class FrozenDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

        monkeypatch.setattr(executor_core.dt, "datetime", FrozenDateTime)
        sentinel = "P2_AC8_QUALITY_EXCEPTION_SENTINEL"
        session = AsyncMock()
        wrapper = AsyncMock()
        wrapper.get_selected_device_id.return_value = "device-a"
        quality_check = AsyncMock(side_effect=RuntimeError(sentinel))
        executor = PlanExecutor(
            wrapper,
            session_factory=_factory(session),
            device_quality_check=quality_check,
        )
        action = _action()

        with capture_logs() as logs:
            await executor._execute_safety_action(action)

        values = _values(session.execute.await_args.args[0])
        assert values["status"] == "pending"
        assert values["safety_attempt_count"] == 1
        assert values["safety_next_retry_at"] == frozen_now + dt.timedelta(minutes=1)
        assert json.loads(values["result_json"])["reason"] == "quality_check_failed"
        wrapper.safety_write.assert_not_called()
        assert sentinel not in repr(logs)

        for _ in range(3):
            await executor._execute_safety_action(action)
            values = _values(session.execute.await_args.args[0])
            action.safety_attempt_count = values["safety_attempt_count"]

        assert values["safety_attempt_count"] >= 3

    @pytest.mark.asyncio
    async def test_P2_AC7_safety_cycle_performs_one_write_and_no_redispatch(self):
        wrapper = MagicMock()
        wrapper.safety_write.return_value = MagicMock()
        executor = PlanExecutor(wrapper, session_factory=_factory(AsyncMock()))
        action = _action()
        handler = MagicMock()
        handler.dispatch = AsyncMock(return_value={"force_dhw": 0})
        failed = VerifyResult(ok=False, observed_value=1, expected_value=0, reason="timeout")

        with (
            patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
            patch.object(executor, "_safety_already_safe", new=AsyncMock(return_value=False)),
            patch("packages.optimizer.executor_core.get_action_handler", return_value=handler),
            patch.object(
                executor,
                "_poll_until_verified",
                new=AsyncMock(return_value=(failed, 1)),
            ),
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()) as requeue,
        ):
            await executor._execute_safety_action(action)

        handler.dispatch.assert_awaited_once_with(wrapper, {"zone_id": 1})
        requeue.assert_awaited_once_with(action, "timeout")

    @pytest.mark.asyncio
    async def test_P2_AC7_safety_attempt_calls_the_wrapper_write_once(self):
        wrapper = MagicMock()
        wrapper.safety_write.return_value = MagicMock()
        wrapper.force_dhw = AsyncMock(return_value=True)
        executor = PlanExecutor(wrapper, session_factory=_factory(AsyncMock()))
        action = _action()
        failed = VerifyResult(ok=False, observed_value=1, expected_value=0, reason="timeout")

        with (
            patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
            patch.object(executor, "_safety_already_safe", new=AsyncMock(return_value=False)),
            patch.object(
                executor,
                "_poll_until_verified",
                new=AsyncMock(return_value=(failed, 1)),
            ),
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()),
        ):
            await executor._execute_safety_action(action)

        wrapper.force_dhw.assert_awaited_once()

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
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(
            force_dhw=0, zone1_target_temp=current_target
        )
        session.execute = AsyncMock(return_value=result)
        wrapper = MagicMock()
        wrapper.safety_write.return_value = MagicMock()
        executor = PlanExecutor(wrapper, session_factory=_factory(session))
        action = _action(action_type="zone_temp_restore")
        action.payload_json = json.dumps({"zone_id": 1, "temperature": 21})

        with (
            patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
            patch.object(executor, "_mark_verified", new=AsyncMock()) as verified,
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()) as requeue,
            patch("packages.optimizer.executor_core.get_action_handler") as handler_factory,
        ):
            handler_factory.return_value.dispatch = AsyncMock(return_value={"skip": True})
            await executor._execute_safety_action(action)

        if current_target == 21:
            verified.assert_awaited_once()
            requeue.assert_not_awaited()
            handler_factory.return_value.dispatch.assert_not_awaited()
        else:
            verified.assert_not_awaited()
            requeue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_P2_zone_restore_source_baseline_disagreement_retries_without_write(self):
        session = AsyncMock()
        status = MagicMock()
        status.scalar_one_or_none.return_value = SimpleNamespace(zone1_target_temp=21)
        source = SimpleNamespace(payload_json=json.dumps({"baseline_temperature": 22}))
        session.execute = AsyncMock(return_value=status)
        session.get = AsyncMock(return_value=source)
        wrapper = MagicMock()
        wrapper.safety_write.return_value = MagicMock()
        executor = PlanExecutor(wrapper, session_factory=_factory(session))
        action = _action(action_type="zone_temp_restore")
        action.reverts_action_id = 2

        with (
            patch.object(executor, "_dispatch_precondition", new=AsyncMock(return_value=None)),
            patch.object(executor, "_requeue_safety_action", new=AsyncMock()) as requeue,
            patch("packages.optimizer.executor_core.get_action_handler") as handler_factory,
        ):
            handler_factory.return_value.dispatch = AsyncMock()
            await executor._execute_safety_action(action)

        handler_factory.return_value.dispatch.assert_not_awaited()
        requeue.assert_awaited_once_with(action, "ValueError")

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

        await executor._execute_action(action)

        skipped = _values(session.execute.await_args_list[-1].args[0])
        assert skipped["status"] == "skipped"
        assert json.loads(skipped["result_json"])["reason"] == "blocked_by_unresolved_revert"
        wrapper.force_dhw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_P2_AC9_allows_ordinary_dhw_on_for_a_different_device(self):
        executor, _, _ = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("force_dhw_off", device_id="device-b")),
        )

        result = await executor._dispatch_precondition(
            _action(action_type="force_dhw_on"), ActionType.FORCE_DHW_ON, {}
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_P2_AC9_blocks_zone_increase_only_for_the_matching_zone(self):
        action = _action(action_type="set_zone_heat_temperature")
        action.payload_json = json.dumps({"zone_id": 2, "temperature": 21})
        executor, _, _ = self._dispatch_executor(
            self._status_result(),
            self._unresolved_result(self._revert("zone_temp_restore", zone_id=2)),
        )

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
        with patch(
            "packages.optimizer.executor_core.get_space_heating_gate_config",
            new=AsyncMock(return_value=config),
        ):
            result = await executor._dispatch_precondition(
                action, ActionType.SET_ZONE_HEAT_TEMPERATURE, {"zone_id": 1, "temperature": 21}
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_P2_AC9_allows_zone_decrease_and_tagged_restore(self):
        action = _action(action_type="set_zone_heat_temperature")
        executor, _, session = self._dispatch_executor(self._status_result())

        result = await executor._dispatch_precondition(
            action, ActionType.SET_ZONE_HEAT_TEMPERATURE, {"zone_id": 1, "temperature": 19}
        )

        assert result is None
        assert session.execute.await_count == 1

        restore = _action(action_type="zone_temp_restore")
        restore.reverts_action_id = 1
        executor, _, session = self._dispatch_executor(self._status_result())
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
        assert event.status == "skipped_unresolved_revert"
        assert event.activation_action_id is None
        assert event.expires_at == current.ts

    @pytest.mark.asyncio
    async def test_P2_AC16_activation_failure_after_flush_escapes_transaction_for_rollback(self):
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

        no_active_event = MagicMock()
        no_active_event.scalar_one_or_none.return_value = None
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[no_active_event, previous_result, blocked_result]),
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
            patch("packages.optimizer.shower_mode.activate_plan", new=AsyncMock()),
            patch(
                "packages.optimizer.shower_mode.get_session",
                return_value=_AsyncContext(session, on_exit=on_exit),
            ),
        ):
            with pytest.raises(RuntimeError, match="linked OFF"):
                await detector.check(current)

        assert exit_args and exit_args[0][0] is RuntimeError


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
        assert alert["href"] == "/?view=plan&activity=safety#plan-action-17"

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
        session = AsyncMock()
        executor = PlanExecutor(AsyncMock(), session_factory=_factory(session))
        action = _action(attempt_count=2)

        await executor._requeue_safety_action(action, reason)

        values = _values(session.execute.await_args.args[0])
        assert values["safety_attempt_count"] == 3
        assert json.loads(values["result_json"])["reason"] == reason

    def test_P2_AC12_downgrade_mapping_is_neutral(self):
        migration = (
            Path(__file__).parents[1] / "migrations/versions/029_safety_revert_obligations.py"
        ).read_text(encoding="utf-8")
        assert "UPDATE shower_events SET status = 'skipped_revert'" in migration

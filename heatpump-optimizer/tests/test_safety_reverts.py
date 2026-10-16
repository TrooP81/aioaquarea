from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from packages.core.safety_reverts import (
    dhw_embargoed,
    normalize_zone_id,
    unresolved_revert_device_ids,
    unresolved_revert_summary,
    validate_action_pair,
    zone_embargoed,
    zone_matches_baseline,
)


def _action(action_type, *, device_id="device-a", zone_id=1, status="pending", linked=True):
    return {
        "action_type": action_type,
        "device_id": device_id,
        "payload": {"zone_id": zone_id},
        "status": status,
        "reverts_action_id": 10 if linked else None,
    }


class TestSafetyRevertPairs:
    def test_p2_ac5_rejects_unpaired_action_types(self):
        with pytest.raises(ValueError, match="invalid safety action pair"):
            validate_action_pair(_action("force_dhw_on"), _action("zone_temp_restore"))

    def test_p2_ac5_rejects_cross_device_pair(self):
        with pytest.raises(ValueError, match="same device"):
            validate_action_pair(
                _action("force_dhw_on", device_id="device-a"),
                _action("force_dhw_off", device_id="device-b"),
            )

    def test_p2_ac5_rejects_cross_zone_pair(self):
        with pytest.raises(ValueError, match="same zone"):
            validate_action_pair(
                _action("zone_temp_boost", zone_id=1),
                _action("zone_temp_restore", zone_id=2),
            )


class TestSafetyRevertEvidence:
    @pytest.mark.parametrize(
        ("baseline", "current", "expected"),
        [(35, 35, True), (35.0, 35, True), (35, 36, False), (None, 35, False), (True, 1, False)],
    )
    def test_p2_ac14_zone_matches_baseline_truth_table(self, baseline, current, expected):
        assert zone_matches_baseline(baseline, current) is expected

    @pytest.mark.parametrize("zone_id", [None, 0, 1, False, "bad"])
    def test_normalizes_legacy_primary_zone(self, zone_id):
        assert normalize_zone_id(zone_id) == 1


class TestSafetyRevertEmbargo:
    def test_p2_ac9_dhw_embargo_is_device_scoped(self):
        actions = [_action("force_dhw_off", device_id="device-a")]
        assert dhw_embargoed(actions, "device-a")
        assert not dhw_embargoed(actions, "device-b")

    def test_p2_ac9_zone_embargo_is_device_and_zone_scoped(self):
        actions = [_action("zone_temp_restore", device_id="device-a", zone_id=2)]
        status = SimpleNamespace(zone1_target_temp=30, zone2_target_temp=30)
        increase = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-a",
            "payload": {"zone_id": 2, "temperature": 31},
        }
        other_zone = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-a",
            "payload": {"zone_id": 1, "temperature": 31},
        }
        other_device = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-b",
            "payload": {"zone_id": 2, "temperature": 31},
        }

        assert zone_embargoed(actions, increase, status)
        assert not zone_embargoed(actions, other_zone, status)
        assert not zone_embargoed(actions, other_device, status)


@pytest.mark.asyncio
async def test_unresolved_revert_summary_is_bounded_and_reports_oldest_age():
    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    oldest = now - dt.timedelta(seconds=180)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(one=lambda: (2, oldest)),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["force_dhw_off"])),
            ]
        )
    )

    summary = await unresolved_revert_summary(session, now=now)

    assert summary == {
        "count": 2,
        "oldest_scheduled_at": oldest,
        "oldest_age_seconds": 180,
        "action_types": ["force_dhw_off"],
    }


@pytest.mark.asyncio
async def test_unresolved_revert_device_ids_reports_bounded_overflow():
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: ["device-a", "device-b", "device-c"])
            )
        )
    )

    device_ids, overflowed = await unresolved_revert_device_ids(session, limit=2)

    assert device_ids == {"device-a", "device-b"}
    assert overflowed is True


S1_NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)


def _s1_result(value=None, rows=()):
    return SimpleNamespace(
        scalar_one_or_none=lambda: value,
        scalar_one=lambda: value,
        scalars=lambda: SimpleNamespace(all=lambda: list(rows)),
        rowcount=1,
    )


def _s1_session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


async def _s1_future_quality(monkeypatch):
    from packages.core import device_data_quality

    values = {
        "aquarea_username": "audit@example.invalid",
        "aquarea_password": "dummy",
        "device_status_max_age_minutes": "15",
        "poll_interval_seconds": "300",
    }
    monkeypatch.setattr(
        device_data_quality, "get_setting", AsyncMock(side_effect=lambda key: values[key])
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_s1_result(S1_NOW + dt.timedelta(hours=1)))
    )
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))
    return await device_data_quality.get_device_data_quality(now=S1_NOW)


async def test_QR_F01_future_device_evidence_is_not_fresh(monkeypatch):
    quality = await _s1_future_quality(monkeypatch)

    assert not quality["ready"], "Future-dated device evidence cannot establish current readiness"


@pytest.mark.parametrize("clock", ["ts", "captured_at", "observed_at", "retrieved_at"])
@pytest.mark.parametrize(
    "case",
    ["utc_equal", "naive_equal", "offset_equal", "utc_past", "naive_future", "offset_future"],
)
async def test_device_evidence_utc_boundaries(monkeypatch, clock, case):
    from packages.core import device_data_quality
    from packages.core.models import DeviceStatusRecord

    await _s1_future_quality(monkeypatch)
    timestamp = S1_NOW
    evaluation = S1_NOW
    if case == "utc_past":
        timestamp -= dt.timedelta(microseconds=1)
    elif case.endswith("future"):
        timestamp += dt.timedelta(microseconds=1)
    if case.startswith("naive"):
        timestamp = timestamp.replace(tzinfo=None)
        evaluation = evaluation.replace(tzinfo=None)
    elif case.startswith("offset"):
        offset = dt.timezone(dt.timedelta(hours=5, minutes=30))
        timestamp = timestamp.astimezone(offset)
        evaluation = evaluation.astimezone(dt.timezone(dt.timedelta(hours=-4)))
    provenance = {"captured_at": S1_NOW.isoformat(), "timing": {}}
    if clock != "ts":
        destination = provenance if clock == "captured_at" else provenance["timing"]
        destination[clock] = timestamp.isoformat()
    record = DeviceStatusRecord(
        ts=timestamp if clock == "ts" else S1_NOW,
        device_id="utc-boundary-device",
        native_policy_provenance=provenance,
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_s1_result(record)))
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))

    quality = await device_data_quality.get_device_data_quality(now=evaluation)

    future = case.endswith("future")
    assert quality["ready"] is not future
    assert quality["age_seconds"] == (None if future else 0)
    assert quality["reasons"] == (["device_status_future"] if future else [])
    assert quality["timestamp"].tzinfo == dt.timezone.utc


@pytest.mark.parametrize("clock", ["observed_at", "retrieved_at", "both"])
async def test_repair2_recent_row_with_stale_provenance_is_not_ready(monkeypatch, clock):
    from packages.core import device_data_quality
    from packages.core.models import DeviceStatusRecord

    await _s1_future_quality(monkeypatch)
    old = (S1_NOW - dt.timedelta(days=1)).isoformat()
    timing = {field: old for field in ("observed_at", "retrieved_at") if clock in (field, "both")}
    record = DeviceStatusRecord(
        ts=S1_NOW,
        device_id="repair2-device",
        native_policy_provenance={"captured_at": S1_NOW.isoformat(), "timing": timing},
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_s1_result(record)))
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))

    quality = await device_data_quality.get_device_data_quality(now=S1_NOW)

    assert quality["ready"] is False
    assert quality["reasons"] == ["device_status_stale"]
    assert quality["age_seconds"] == 86400


@pytest.mark.parametrize("clock_name", ["captured_at", "observed_at", "retrieved_at"])
@pytest.mark.parametrize("over_age", [dt.timedelta(0), dt.timedelta(microseconds=1)])
async def test_repair2_quality_uses_oldest_known_clock_at_policy_boundary(
    monkeypatch, clock_name, over_age
):
    from packages.core import device_data_quality
    from packages.core.models import DeviceStatusRecord

    await _s1_future_quality(monkeypatch)
    aged = S1_NOW - dt.timedelta(seconds=900) - over_age
    provenance = {"captured_at": S1_NOW.isoformat(), "timing": {}}
    destination = provenance if clock_name == "captured_at" else provenance["timing"]
    destination[clock_name] = aged.isoformat()
    record = DeviceStatusRecord(
        ts=S1_NOW, device_id="repair2-age-boundary", native_policy_provenance=provenance
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_s1_result(record)))
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))

    quality = await device_data_quality.get_device_data_quality(now=S1_NOW)

    assert quality["ready"] is (over_age == dt.timedelta(0))
    assert quality["threshold_seconds"] == quality["age_seconds"] == 900
    assert quality["reasons"] == ([] if over_age == dt.timedelta(0) else ["device_status_stale"])


async def test_QR_F01_future_evidence_cannot_complete_genuine_restore(monkeypatch):
    from packages.optimizer import executor_core

    class FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, timezone=None):
            return S1_NOW if timezone is not None else S1_NOW.replace(tzinfo=None)

    quality = await _s1_future_quality(monkeypatch)
    monkeypatch.setattr(
        executor_core,
        "dt",
        SimpleNamespace(datetime=FrozenDatetime, timezone=dt.timezone, timedelta=dt.timedelta),
    )
    source = SimpleNamespace(
        id=10,
        device_id="audit-device",
        action_type="zone_temp_boost",
        payload_json=json.dumps({"zone_id": 1, "baseline_temperature": 35, "temperature": 37}),
    )
    restore = SimpleNamespace(
        id=11,
        device_id="audit-device",
        plan_id=1,
        action_type="zone_temp_restore",
        payload_json=json.dumps({"zone_id": 1, "temperature": 35}),
        reverts_action_id=10,
        status="executing",
        safety_attempt_count=0,
        safety_claimed_at=S1_NOW,
        safety_claim_token=uuid4(),
        scheduled_ts=S1_NOW - dt.timedelta(minutes=1),
    )
    status = SimpleNamespace(
        ts=S1_NOW + dt.timedelta(hours=1),
        device_id="audit-device",
        force_dhw=0,
        zone1_target_temp=35,
        zone2_target_temp=None,
    )
    updates = []

    async def execute(statement):
        if getattr(statement, "is_update", False):
            updates.append({key.key: value.value for key, value in statement._values.items()})
            return _s1_result()
        if "plan_actions.safety_claim_token =" in str(statement):
            return _s1_result(restore)
        return _s1_result(status)

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=execute), get=AsyncMock(return_value=source), add=Mock()
    )
    wrapper = SimpleNamespace(
        get_selected_device_id=AsyncMock(return_value="audit-device"),
        _dispatch_linked_restore=AsyncMock(),
    )
    executor = executor_core.PlanExecutor(
        wrapper,
        session_factory=_s1_session_factory(session),
        learning_check=AsyncMock(return_value=executor_core.LearningModeState.INACTIVE),
        device_quality_check=AsyncMock(return_value=quality),
    )

    from packages.core.safety_attempts import SafetyAttempt

    connection = _S1FenceConnection()
    attempt = await SafetyAttempt.acquire(restore.device_id, connection.context, now=S1_NOW)
    assert attempt is not None
    attempt.bind(
        restore.id,
        source.id,
        restore.safety_claim_token,
        payload_json=restore.payload_json,
        source_payload_json=source.payload_json,
    )
    executor._safety_attempts[restore.safety_claim_token] = attempt
    await executor._execute_safety_action(restore)

    assert not any(update.get("status") == "executed" for update in updates), (
        "Future evidence completed an existing linked restore without current device evidence"
    )
    wrapper._dispatch_linked_restore.assert_not_awaited()


@pytest.mark.parametrize("transition", ["complete", "requeue"])
async def test_QR_F02_stale_worker_complete_or_requeue_requires_claim_CAS(transition):
    from packages.optimizer.actions import VerifyResult
    from packages.optimizer.executor_core import PlanExecutor

    action = SimpleNamespace(
        id=11,
        reverts_action_id=10,
        device_id="audit-device",
        action_type="zone_temp_restore",
        payload_json='{"zone_id":1,"temperature":35}',
        status="dispatched",
        safety_claimed_at=S1_NOW,
        safety_claim_token=uuid4(),
        safety_attempt_count=0,
        result_json='{"reason":"preserved_source_evidence"}',
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=lambda statement: (
                SimpleNamespace(rowcount=0) if statement.is_update else _s1_result(action)
            )
        ),
        add=Mock(),
    )
    executor = PlanExecutor(SimpleNamespace(), session_factory=_s1_session_factory(session))
    executor._assert_safety_authority = AsyncMock()

    async def run_db(awaitable, _deadline):
        return await awaitable

    executor._attempt_for = Mock(
        return_value=SimpleNamespace(
            metadata=lambda **kwargs: {},
            remaining=lambda **kwargs: 5,
            _bounded=run_db,
            check_fence=AsyncMock(),
        )
    )

    with pytest.raises(PermissionError):
        if transition == "complete":
            await executor._mark_verified(action, 1, VerifyResult(ok=True, expected_value=35))
        else:
            await executor._requeue_safety_action(action, "stale_worker")

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("plan_actions.safety_claim_token =" in statement for statement in statements)
    assert any("plan_actions.reverts_action_id =" in statement for statement in statements)
    assert any("plan_actions.device_id =" in statement for statement in statements)
    assert any("plan_actions.status" in statement for statement in statements)
    session.add.assert_not_called()


class _S1FenceConnection:
    def __init__(self, *, unlock=True):
        self.events = []
        self.locked = False
        self.invalidated = False
        self.unlock = unlock

    @asynccontextmanager
    async def context(self):
        self.events.append("enter")
        try:
            yield self
        finally:
            assert not self.locked or self.invalidated
            self.events.append("exit")

    async def execute(self, statement, parameters):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.events.append("lock")
            self.locked = True
            return _s1_result(True)
        if "pg_advisory_unlock" in sql:
            self.events.append("unlock")
            if self.unlock:
                self.locked = False
            return _s1_result(self.unlock)
        if "NOT EXISTS" in sql:
            self.events.append("absence")
            return _s1_result(not self.locked)
        self.events.append("check")
        return _s1_result(self.locked)

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")

    async def invalidate(self):
        self.events.append("invalidate")
        self.invalidated = True


async def test_restore_device_attempt_lock_is_process_scoped():
    from packages.core.safety_attempts import SafetyAttempt

    device_id = "process-scope-" + str(uuid4())
    first_connection, second_connection = _S1FenceConnection(), _S1FenceConnection()
    first = await SafetyAttempt.acquire(device_id, first_connection.context)
    other = None
    try:
        assert first is not None
        assert await SafetyAttempt.acquire(device_id, second_connection.context) is None
        other = await SafetyAttempt.acquire(device_id + "-other", second_connection.context)
        assert other is not None
    finally:
        if other is not None:
            assert await other.release()
        if first is not None:
            assert await first.release()
    assert first_connection.events.count("unlock") == 1
    assert second_connection.events.count("unlock") == 1


@pytest.mark.parametrize("unlock", [True, False])
async def test_restore_connection_unlock_or_invalidation_before_reuse(unlock):
    from packages.core.safety_attempts import SafetyAttempt

    connection = _S1FenceConnection(unlock=unlock)
    attempt = await SafetyAttempt.acquire("connection-cleanup-" + str(uuid4()), connection.context)
    assert attempt is not None

    assert await attempt.release()

    assert connection.events[-1] == "exit"
    assert connection.events.index("unlock") < connection.events.index("exit")
    if not unlock:
        assert connection.events.index("invalidate") < connection.events.index("exit")


class _S1Clock:
    def __init__(self, monkeypatch, *, utc_start=S1_NOW, track_real_time=False):
        import time as wall_time

        from packages.core import safety_attempts
        from packages.core.services import aquarea
        from packages.optimizer import executor_core

        self.elapsed = 0.0
        self.waits = []
        self.read_starts = []
        self.sdk_starts = []
        self.utc_start = utc_start
        self._real_clock = wall_time.monotonic if track_real_time else None
        self._real_start = wall_time.monotonic()
        clock = self

        class ClockDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                current = clock.utcnow()
                return current.replace(tzinfo=None) if tz is None else current.astimezone(tz)

        datetime_module = SimpleNamespace(**vars(dt))
        datetime_module.datetime = ClockDateTime
        for module in (safety_attempts, aquarea, executor_core):
            monkeypatch.setattr(module, "dt", datetime_module)
            monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=self.monotonic))

    def utcnow(self):
        return self.utc_start + dt.timedelta(seconds=self.monotonic() - 1000)

    def monotonic(self):
        real_elapsed = self._real_clock() - self._real_start if self._real_clock else 0.0
        return 1000.0 + self.elapsed + real_elapsed

    def advance(self, seconds):
        assert seconds >= 0
        self.elapsed += seconds

    async def sleep(self, seconds):
        self.waits.append((self.elapsed, seconds))
        self.advance(seconds)
        await asyncio.sleep(0)

    async def read(self, **kwargs):
        assert kwargs == {"allow_cached_fallback": False}
        self.read_starts.append(self.elapsed)
        self.advance(0.25)
        await asyncio.sleep(0)

    async def dispatch(self, seconds):
        self.sdk_starts.append(self.elapsed)
        self.advance(seconds)
        await asyncio.sleep(0)


class _S1RestoreStore:
    def __init__(
        self,
        *,
        current_target=37,
        source_clock=None,
        detached=False,
        restore_type="zone_temp_restore",
        clock=None,
        sdk_seconds=2.0,
    ):
        from aioaquarea import ForceDHW
        from aioaquarea.data import StatusDataMode
        from packages.core.models import DeviceStatusRecord, PlanActionRecord
        from packages.core.services.aquarea import AquareaWrapper

        self.device_id = "owned-restore-" + str(uuid4())
        self.detached = detached
        self.clock = clock
        now = clock.utcnow() if clock is not None else dt.datetime.now(dt.timezone.utc)
        self.source = PlanActionRecord(
            id=10,
            plan_id=1,
            device_id=self.device_id,
            action_type="force_dhw_on" if restore_type == "force_dhw_off" else "zone_temp_boost",
            payload_json="{}"
            if restore_type == "force_dhw_off"
            else '{"zone_id":1,"baseline_temperature":35,"temperature":37}',
            status="executed",
            scheduled_ts=now - dt.timedelta(minutes=5),
            executed_at=source_clock,
        )
        self.restore = PlanActionRecord(
            id=11,
            plan_id=1,
            device_id=self.device_id,
            action_type=restore_type,
            payload_json="{}"
            if restore_type == "force_dhw_off"
            else '{"zone_id":1,"temperature":35}',
            reverts_action_id=10,
            status="pending",
            scheduled_ts=now - dt.timedelta(minutes=1),
            safety_attempt_count=0,
            result_json='{"reason":"original_obligation"}',
        )
        self.actions = [self.source, self.restore]
        self.events = []
        self.status = DeviceStatusRecord(
            ts=now, device_id=self.device_id, zone1_target_temp=current_target
        )
        self.reads = []
        self.audits = []
        self.statements = []
        self.loaded_action_ids = []
        self.ledger_barrier = None
        self.connection = _S1FenceConnection()
        zone = SimpleNamespace(heat_target_temperature=current_target, heat_min=20, heat_max=65)
        self.device = SimpleNamespace(
            long_id=self.device_id,
            zones={1: zone},
            tank=None,
            force_dhw=ForceDHW.ON if restore_type == "force_dhw_off" else None,
            quiet_mode=None,
            special_status=None,
            status_data_mode=StatusDataMode.LIVE,
            refresh_data=AsyncMock(side_effect=clock.read if clock is not None else None),
        )

        async def set_temperature(temperature, *, zone_id):
            if clock is not None:
                await clock.dispatch(sdk_seconds)
            self.device.zones[zone_id].heat_target_temperature = temperature

        self.device.set_temperature = AsyncMock(side_effect=set_temperature)

        async def set_force_dhw(state):
            if clock is not None:
                await clock.dispatch(sdk_seconds)
            self.device.force_dhw = state

        self.device.set_force_dhw = AsyncMock(side_effect=set_force_dhw)
        self.wrapper = AquareaWrapper()
        self.wrapper._device = self.device
        self.wrapper._distributed_quota_enabled = lambda: False
        self.wrapper._read_limiter.acquire = AsyncMock()
        self.wrapper._write_limiter.acquire = AsyncMock()

    @staticmethod
    def _matches(expression, row):
        from sqlalchemy.sql import operators
        from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, Null

        if isinstance(expression, BooleanClauseList):
            values = [_S1RestoreStore._matches(clause, row) for clause in expression.clauses]
            return all(values) if expression.operator is operators.and_ else any(values)
        assert isinstance(expression, BinaryExpression), type(expression)
        actual = getattr(row, expression.left.name)
        expected = None if isinstance(expression.right, Null) else expression.right.value
        operator = expression.operator
        if operator is operators.is_:
            return actual is expected
        if operator is operators.is_not:
            return actual is not expected
        if operator is operators.eq:
            return actual == expected
        if operator is operators.in_op:
            return actual in expected
        if actual is None or expected is None:
            return False
        return operator(actual, expected)

    async def execute(self, statement, parameters=None):
        from packages.core.models import DeviceStatusRecord, PlanActionRecord
        from sqlalchemy.sql.elements import TextClause

        self.statements.append(statement)
        if isinstance(statement, TextClause):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql and self.ledger_barrier is not None:
                await self.ledger_barrier()
            if "clock_timestamp" in sql:
                return _s1_result(
                    self.clock.utcnow()
                    if self.clock is not None
                    else dt.datetime.now(dt.timezone.utc)
                )
            return _s1_result(0)
        if statement.is_update:
            assert statement.table.name in ("plan_actions", "shower_events")
            table_rows = self.events if statement.table.name == "shower_events" else self.actions
            matched = [
                row
                for row in table_rows
                if all(self._matches(clause, row) for clause in statement._where_criteria)
            ]
            for row in matched:
                for column, value in statement._values.items():
                    setattr(row, column.key, value.value)
            return SimpleNamespace(rowcount=len(matched))
        entity = statement.column_descriptions[0]["entity"]
        assert entity in (PlanActionRecord, DeviceStatusRecord)
        rows = self.actions if entity is PlanActionRecord else [self.status]
        rows = [
            row
            for row in rows
            if all(self._matches(clause, row) for clause in statement._where_criteria)
        ]
        if self.detached:
            rows = [
                type(row)(
                    **{column.name: getattr(row, column.name) for column in row.__table__.columns}
                )
                for row in rows
            ]
        return _s1_result(rows[0] if rows else None, rows)

    @asynccontextmanager
    async def sessions(self):
        from packages.core.models import AuditLogRecord, ExecutorVerificationReadRecord

        def add(record):
            if isinstance(record, AuditLogRecord):
                self.audits.append(record)
            else:
                assert isinstance(record, ExecutorVerificationReadRecord)
                self.reads.append(record)

        def get_action(_model, identity):
            self.loaded_action_ids.append(identity)
            return next((row for row in self.actions if row.id == identity), None)

        yield SimpleNamespace(execute=self.execute, get=AsyncMock(side_effect=get_action), add=add)

    def executor(self):
        from packages.optimizer.executor_core import LearningModeState, PlanExecutor

        return PlanExecutor(
            self.wrapper,
            session_factory=self.sessions,
            connection_factory=self.connection.context,
            sleep=self.clock.sleep if self.clock is not None else AsyncMock(),
            learning_check=AsyncMock(return_value=LearningModeState.INACTIVE),
            device_quality_check=AsyncMock(
                return_value={"ready": True, "reasons": [], "threshold_seconds": 900}
            ),
        )


def _repair2_provenance(now, defect, *, evidence_after):
    observed_at = retrieved_at = now
    if defect in ("old_observation", "both_old", "old_observation_missing_retrieval"):
        observed_at -= dt.timedelta(days=1)
    if defect in ("old_retrieval", "both_old"):
        retrieved_at -= dt.timedelta(days=1)
    if defect == "old_retrieval":
        observed_at = None
    if defect == "old_observation_missing_retrieval":
        retrieved_at = None
    if defect == "reversed":
        retrieved_at -= dt.timedelta(microseconds=1)
    if defect == "future_observation":
        observed_at += dt.timedelta(microseconds=1)
    if defect == "future_retrieval":
        retrieved_at += dt.timedelta(microseconds=1)
    if defect == "prebound":
        observed_at = evidence_after - dt.timedelta(microseconds=1)
    if defect == "missing":
        observed_at = retrieved_at = None
    return {
        "schema": "native_unchanged_v1",
        "captured_at": now.isoformat(),
        "timing": {
            "schema": "local_snapshot_v1",
            "observed_at": observed_at.isoformat() if observed_at is not None else None,
            "retrieved_at": retrieved_at.isoformat() if retrieved_at is not None else None,
        },
    }


@pytest.mark.parametrize(
    "defect",
    ["old_observation", "old_retrieval", "both_old", "reversed", "future_observation", "fresh"],
)
def test_repair2_live_normalization_validates_actual_remote_clock_fields(defect):
    from packages.optimizer.executor_core import PlanExecutor, _VerificationEvidenceUnavailable

    provenance = _repair2_provenance(
        S1_NOW, defect, evidence_after=S1_NOW - dt.timedelta(seconds=1)
    )
    timing = provenance["timing"]
    device = SimpleNamespace(
        zones={1: SimpleNamespace(heat_target_temperature=35)},
        status_observed_at=timing["observed_at"],
        status_retrieved_at=timing["retrieved_at"],
    )
    if defect == "fresh":
        observation = PlanExecutor._normalize_live_observation(device, as_of=S1_NOW)
        assert observation.zone_targets == {1: 35}
    else:
        with pytest.raises(_VerificationEvidenceUnavailable):
            PlanExecutor._normalize_live_observation(device, as_of=S1_NOW)


@pytest.mark.parametrize(
    "defect",
    [
        "old_observation",
        "old_retrieval",
        "both_old",
        "old_observation_missing_retrieval",
        "reversed",
        "future_observation",
        "future_retrieval",
        "prebound",
        "missing",
        "fresh",
    ],
)
async def test_repair2_persisted_ambiguous_dispatch_requires_known_fresh_clocks(
    monkeypatch, defect
):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock)
    accepted = defect == "fresh"

    async def ambiguous_dispatch(temperature, *, zone_id):
        await clock.dispatch(2)
        if accepted:
            store.device.zones[zone_id].heat_target_temperature = temperature
        raise RuntimeError("AMBIGUOUS mocked write")

    async def sleep_and_ingest(seconds):
        await clock.sleep(seconds)
        if store.restore.status == "dispatched":
            bound = dt.datetime.fromisoformat(
                json.loads(store.restore.result_json)["safety_attempt"]["dispatch_settled_at"]
            )
            store.status.ts = clock.utcnow()
            store.status.zone1_target_temp = 35
            store.status.native_policy_provenance = _repair2_provenance(
                clock.utcnow(), defect, evidence_after=bound
            )

    store.device.set_temperature = AsyncMock(side_effect=ambiguous_dispatch)
    executor = store.executor()
    executor._sleep = sleep_and_ingest

    await executor.execute_due_actions()

    assert store.restore.status == ("executed" if accepted else "pending")
    assert len(store.audits) == int(accepted)
    assert all(audit.result == "success" for audit in store.audits)
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert store.device.zones[1].heat_target_temperature == (35 if accepted else 37)
    assert [read.checkpoint_seconds for read in store.reads] == ([15] if accepted else [15, 60])
    result = json.loads(store.restore.result_json)
    assert result["safety_attempt"]["dispatch_outcome"] == "ambiguous"
    assert result["safety_attempt"]["drain"] == "confirmed"
    if not accepted:
        assert result["reason"]
        assert result["original_reason"] == "original_obligation"
        assert json.loads(store.restore.last_observed_json)["ok"] is False
    assert store.connection.events[-1] == "exit"


@pytest.mark.parametrize(
    "defect",
    [
        "old_observation",
        "old_retrieval",
        "both_old",
        "old_observation_missing_retrieval",
        "reversed",
        "future_observation",
        "future_retrieval",
        "prebound",
        "missing",
        "fresh",
    ],
)
async def test_repair2_already_safe_persisted_completion_requires_known_fresh_clocks(
    monkeypatch, defect
):
    from packages.optimizer.actions import ActionType

    clock = _S1Clock(monkeypatch)
    accepted = defect == "fresh"
    store = _S1RestoreStore(current_target=35 if accepted else 37, clock=clock)
    bound = clock.utcnow() - dt.timedelta(seconds=1)
    store.source.result_json = json.dumps(
        {
            "dispatch_provenance": {
                "schema": "source_dispatch_v1",
                "drain": "confirmed",
                "device_id": store.device_id,
                "settled_at": bound.isoformat(),
            }
        }
    )
    store.status.zone1_target_temp = 35
    store.status.native_policy_provenance = _repair2_provenance(
        clock.utcnow(), defect, evidence_after=bound
    )

    async def ambiguous_dispatch(temperature, *, zone_id):
        await clock.dispatch(2)
        raise RuntimeError("AMBIGUOUS mocked write")

    store.device.set_temperature = AsyncMock(side_effect=ambiguous_dispatch)

    executor = store.executor()
    executor._last_device_quality[store.device_id] = {"ready": True, "threshold_seconds": 900}
    already_safe = await executor._safety_already_safe(
        store.restore, ActionType.ZONE_TEMP_RESTORE, json.loads(store.restore.payload_json)
    )
    assert already_safe is accepted
    await executor.execute_due_actions()

    assert store.restore.status == ("executed" if accepted else "pending")
    assert len(store.audits) == int(accepted)
    assert all(audit.result == "success" for audit in store.audits)
    preflight_deferred = defect not in ("prebound", "missing", "fresh")
    assert [read.checkpoint_seconds for read in store.reads] == (
        [] if accepted or preflight_deferred else [15, 60]
    )
    if accepted or preflight_deferred:
        store.device.set_temperature.assert_not_awaited()
        store.device.refresh_data.assert_not_awaited()
    else:
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    if not accepted:
        assert json.loads(store.restore.result_json)["reason"]
        assert json.loads(store.restore.result_json)["original_reason"] == "original_obligation"
    assert store.connection.events[-1] == "exit"


@pytest.mark.parametrize("completion_path", ["already_safe", "ambiguous"])
@pytest.mark.parametrize("remote_clock", ["observed_at", "retrieved_at"])
@pytest.mark.parametrize("offset_microseconds", [-1, 0, 1])
async def test_repair2_independent_single_clock_settlement_boundary(
    monkeypatch, completion_path, remote_clock, offset_microseconds
):
    from packages.optimizer.actions import ActionType

    clock = _S1Clock(monkeypatch)
    accepted = offset_microseconds > 0
    store = _S1RestoreStore(current_target=35 if accepted else 37, clock=clock)
    source_bound = clock.utcnow() - dt.timedelta(seconds=1)
    store.source.result_json = json.dumps(
        {
            "dispatch_provenance": {
                "schema": "source_dispatch_v1",
                "drain": "confirmed",
                "device_id": store.device_id,
                "settled_at": source_bound.isoformat(),
            }
        }
    )

    def ingest(bound):
        store.status.ts = clock.utcnow()
        store.status.zone1_target_temp = 35
        store.status.native_policy_provenance = {
            "captured_at": clock.utcnow().isoformat(),
            "timing": {
                "observed_at": None,
                "retrieved_at": None,
                remote_clock: (bound + dt.timedelta(microseconds=offset_microseconds)).isoformat(),
            },
        }

    async def ambiguous_dispatch(temperature, *, zone_id):
        await clock.dispatch(2)
        if accepted:
            store.device.zones[zone_id].heat_target_temperature = temperature
        raise RuntimeError("AMBIGUOUS independent boundary write")

    async def sleep_and_ingest(seconds):
        await clock.sleep(seconds)
        if store.restore.status == "dispatched":
            ingest(
                dt.datetime.fromisoformat(
                    json.loads(store.restore.result_json)["safety_attempt"]["dispatch_settled_at"]
                )
            )

    executor = store.executor()
    executor._sleep = sleep_and_ingest
    if completion_path == "already_safe":
        ingest(source_bound)
        executor._last_device_quality[store.device_id] = {
            "ready": True,
            "threshold_seconds": 900,
        }
        assert (
            await executor._safety_already_safe(
                store.restore, ActionType.ZONE_TEMP_RESTORE, json.loads(store.restore.payload_json)
            )
            is accepted
        )
        observation = await executor._load_persisted_observation(store.device_id, source_bound)
        assert (observation is not None) is accepted
    else:
        store.device.zones[1].heat_target_temperature = 37
        store.status.zone1_target_temp = 37
    store.device.set_temperature = AsyncMock(side_effect=ambiguous_dispatch)

    await executor.execute_due_actions()

    assert store.restore.status == ("executed" if accepted else "pending")
    assert len(store.audits) == int(accepted)
    assert all(audit.result == "success" for audit in store.audits)
    assert store.device.zones[1].heat_target_temperature == (35 if accepted else 37)
    if completion_path == "already_safe" and accepted:
        assert store.reads == []
        store.device.refresh_data.assert_not_awaited()
        store.device.set_temperature.assert_not_awaited()
    else:
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
        assert [read.checkpoint_seconds for read in store.reads] == ([15] if accepted else [15, 60])
    result = json.loads(store.restore.result_json)
    assert result["safety_attempt"]["drain"] == "confirmed"
    assert result["safety_attempt"]["bounds_seconds"] == {
        "overall": 110.0,
        "work": 40.0,
        "verification": 60.0,
        "cancel_drain": 5.0,
    }
    if not accepted:
        assert result["reason"] and result["original_reason"] == "original_obligation"
        assert json.loads(store.restore.last_observed_json)["ok"] is False
    assert store.connection.events.count("exit") == 1


@pytest.mark.parametrize("source_clock", [None, S1_NOW])
async def test_restore_missing_source_clock_uses_postclaim_live_proof(source_clock):
    store = _S1RestoreStore(current_target=35, source_clock=source_clock)

    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    assert len(store.reads) == 1 and store.reads[0].checkpoint_seconds == 15
    store.device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
    store.device.set_temperature.assert_not_awaited()
    assert len(store.audits) == 1 and store.audits[0].result == "success"
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["drain"] == "confirmed"
    assert metadata["live_read"]["observed_at"] is None
    assert metadata["live_read"]["live"] is True
    assert json.loads(store.restore.result_json)["original_reason"] == "original_obligation"


@pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at", "both"])
async def test_restore_known_stale_clocks_cannot_complete(monkeypatch, clock_name):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35, clock=clock)
    original = store.wrapper._refresh_restore_status

    async def stale_proof(attempt):
        proof = await original(attempt)
        stale = proof.read_finished_at - dt.timedelta(days=1)
        clocks = (
            {"observed_at": stale, "retrieved_at": stale}
            if clock_name == "both"
            else {clock_name: stale}
        )
        return replace(proof, **clocks)

    store.wrapper._refresh_restore_status = stale_proof
    await store.executor().execute_due_actions()

    assert clock.elapsed == 15.25 and clock.read_starts == [15]
    assert store.restore.status == "pending" and not store.audits
    assert [read.checkpoint_seconds for read in store.reads] == [15]
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert "live_read" not in metadata and metadata["drain"] == "confirmed"
    store.device.set_temperature.assert_not_awaited()


@pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at", "both"])
async def test_restore_postcall_stale_clocks_cannot_certify_completion(monkeypatch, clock_name):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock)
    original = store.wrapper._refresh_restore_status
    proof_count = 0

    async def postcall_stale_proof(attempt):
        nonlocal proof_count
        proof = await original(attempt)
        proof_count += 1
        if proof_count == 1:
            return proof
        stale = proof.read_finished_at - dt.timedelta(days=1)
        clocks = (
            {"observed_at": stale, "retrieved_at": stale}
            if clock_name == "both"
            else {clock_name: stale}
        )
        return replace(proof, **clocks)

    store.wrapper._refresh_restore_status = postcall_stale_proof
    await store.executor().execute_due_actions()

    assert store.restore.status == "pending" and not store.audits
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert clock.sdk_starts == [15.25] and clock.read_starts == [15, 75.25]
    assert proof_count == 2 and [read.checkpoint_seconds for read in store.reads] == [15, 60]
    result = json.loads(store.restore.result_json)
    assert result["reason"] == "_VerificationEvidenceUnavailable"
    assert result["safety_attempt"]["drain"] == "confirmed"
    assert result["safety_attempt"].get("live_read", {}).get("checkpoint") != 60


@pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at", "both"])
@pytest.mark.parametrize("phase", ["source", "postcall"])
async def test_repair2_recent_live_clock_before_trusted_settlement_cannot_complete(
    monkeypatch, clock_name, phase
):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35 if phase == "source" else 37, clock=clock)
    source_bound = clock.utcnow() - dt.timedelta(seconds=1)
    if phase == "source":
        store.source.result_json = json.dumps(
            {
                "dispatch_provenance": {
                    "schema": "source_dispatch_v1",
                    "drain": "confirmed",
                    "device_id": store.device_id,
                    "settled_at": source_bound.isoformat(),
                }
            }
        )
    original = store.wrapper._refresh_restore_status

    async def pre_settlement_proof(attempt):
        proof = await original(attempt)
        if phase == "postcall" and attempt.dispatch_settled_at is None:
            return proof
        bound = attempt.dispatch_settled_at or source_bound
        remote = bound - dt.timedelta(microseconds=1)
        assert 0 < (proof.read_finished_at - remote).total_seconds() <= 60
        fields = (
            {clock_name: remote}
            if clock_name != "both"
            else {"observed_at": remote, "retrieved_at": remote}
        )
        return replace(proof, **fields)

    store.wrapper._refresh_restore_status = pre_settlement_proof

    await store.executor().execute_due_actions()

    assert store.restore.status == "pending" and not store.audits
    assert json.loads(store.restore.result_json)["reason"] == "_VerificationEvidenceUnavailable"
    assert json.loads(store.restore.result_json)["safety_attempt"]["drain"] == "confirmed"
    if phase == "source":
        assert [read.checkpoint_seconds for read in store.reads] == [15]
        store.device.set_temperature.assert_not_awaited()
    else:
        assert [read.checkpoint_seconds for read in store.reads] == [15, 60]
        store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)


@pytest.mark.parametrize("clock_name", ["observed_at", "retrieved_at"])
@pytest.mark.parametrize("threshold", [30, 900])
@pytest.mark.parametrize("over_age", [dt.timedelta(0), dt.timedelta(microseconds=1)])
async def test_restore_known_clock_max_age_boundary(monkeypatch, clock_name, threshold, over_age):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35, clock=clock)
    executor = store.executor()
    executor._device_quality_check = AsyncMock(
        return_value={"ready": True, "reasons": [], "threshold_seconds": threshold}
    )
    original = store.wrapper._refresh_restore_status

    async def aged_proof(attempt):
        proof = await original(attempt)
        measured = proof.read_finished_at - dt.timedelta(seconds=min(60, threshold)) - over_age
        return replace(proof, **{clock_name: measured})

    store.wrapper._refresh_restore_status = aged_proof
    await executor.execute_due_actions()

    accepted = over_age == dt.timedelta(0)
    assert store.restore.status == ("executed" if accepted else "pending")
    assert len(store.audits) == int(accepted)
    assert all(audit.result == "success" for audit in store.audits)
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert ("live_read" in metadata) is accepted
    assert clock.elapsed == 15.25
    store.device.set_temperature.assert_not_awaited()


@pytest.mark.parametrize(
    "defect", ["observation_after_retrieval", "observation_after_read", "retrieval_after_read"]
)
async def test_restore_known_clock_chronology_is_required(monkeypatch, defect):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35, clock=clock)
    original = store.wrapper._refresh_restore_status

    async def misordered_proof(attempt):
        proof = await original(attempt)
        if defect == "observation_after_retrieval":
            return replace(
                proof,
                observed_at=proof.read_finished_at,
                retrieved_at=proof.read_finished_at - dt.timedelta(microseconds=1),
            )
        clock.advance(0.25)
        field = "observed_at" if defect == "observation_after_read" else "retrieved_at"
        return replace(proof, **{field: clock.utcnow()})

    store.wrapper._refresh_restore_status = misordered_proof
    await store.executor().execute_due_actions()

    assert store.restore.status == "pending" and not store.audits
    assert "live_read" not in json.loads(store.restore.result_json)["safety_attempt"]
    store.device.set_temperature.assert_not_awaited()


async def test_restore_absent_clocks_use_new_elapsed_live_proof(monkeypatch):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35, clock=clock)

    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    assert clock.waits == [(0, 15)] and clock.read_starts == [15]
    assert clock.elapsed == 15.25
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["live_read"]["observed_at"] is None
    assert metadata["live_read"]["retrieved_at"] is None
    assert (
        metadata["live_read"]["finished_monotonic"] - metadata["live_read"]["started_monotonic"]
        == 0.25
    )
    assert metadata["live_read"]["live"] is True and metadata["drain"] == "confirmed"
    assert len(store.audits) == 1 and store.audits[0].result == "success"
    store.device.set_temperature.assert_not_awaited()


async def test_restore_mismatch_dispatches_exact_baseline_and_verifies(monkeypatch):
    store = _S1RestoreStore(current_target=37, clock=_S1Clock(monkeypatch))

    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert [read.checkpoint_seconds for read in store.reads] == [15, 60]
    assert len(store.audits) == 1 and store.audits[0].result == "success"
    assert store.connection.events[-1] == "exit"


@pytest.mark.parametrize("sdk_seconds", [2.0, 23.0])
async def test_restore_absolute_checkpoints_account_for_sdk_elapsed(monkeypatch, sdk_seconds):
    from packages.core.safety_attempts import (
        ATTEMPT_SECONDS,
        DRAIN_SECONDS,
        VERIFY_SECONDS,
        WORK_SECONDS,
    )

    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock, sdk_seconds=sdk_seconds)
    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert clock.sdk_starts == [15.25]
    assert clock.read_starts == [15, clock.sdk_starts[0] + 60]
    assert clock.elapsed == 75.5
    assert [read.checkpoint_seconds for read in store.reads] == [15, 60]
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["checkpoint_origin"] == "sdk_restore_call_entry"
    assert metadata["dispatch_started_monotonic"] == 1000 + clock.sdk_starts[0]
    settled = metadata["dispatch_settled_monotonic"]
    assert settled == metadata["dispatch_started_monotonic"] + sdk_seconds
    assert settled - 1000 <= WORK_SECONDS
    assert metadata["verification_deadline_monotonic"] == settled + VERIFY_SECONDS
    assert clock.monotonic() - settled < VERIFY_SECONDS
    assert clock.elapsed < ATTEMPT_SECONDS - DRAIN_SECONDS
    assert len(store.audits) == 1 and store.audits[0].result == "success"
    assert store.connection.events[-1] == "exit"


async def test_restore_bookkeeping_elapsed_cannot_reset_verification_deadline(monkeypatch):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock)
    executor = store.executor()
    original_update = executor._update_safety_action

    async def update_action(action, values, **kwargs):
        if values.get("status") == "dispatched":
            clock.advance(3)
            await asyncio.sleep(0)
        return await original_update(action, values, **kwargs)

    executor._update_safety_action = update_action
    await executor.execute_due_actions()

    assert store.restore.status == "executed"
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["dispatch_settled_monotonic"] == 1017.25
    assert metadata["verification_deadline_monotonic"] == 1077.25
    assert clock.read_starts == [15, 75.25] and clock.elapsed == 75.5
    assert [read.checkpoint_seconds for read in store.reads] == [15, 60]
    assert len(store.audits) == 1 and store.audits[0].result == "success"


async def test_restore_sdk_elapsed_cannot_extend_work_deadline(monkeypatch):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock, sdk_seconds=25)

    await store.executor().execute_due_actions()

    assert clock.elapsed == 40.25
    assert store.restore.status == "pending" and not store.audits
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert clock.read_starts == [15]
    assert [read.checkpoint_seconds for read in store.reads] == [15]
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["verification_deadline_monotonic"] is None
    assert metadata["drain"] == "confirmed" and metadata["possible_dispatch"] is True
    assert json.loads(store.restore.result_json)["reason"] == "SafetyAttemptDeadline"


async def test_restore_admission_checkpoint_is_absolute_from_claim(monkeypatch):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(current_target=35, clock=clock)
    executor = store.executor()

    async def precondition(*args):
        clock.advance(5)
        await asyncio.sleep(0)
        return None

    executor._dispatch_precondition = AsyncMock(side_effect=precondition)
    await executor.execute_due_actions()

    assert store.restore.status == "executed"
    assert clock.waits == [(5, 10)]
    assert clock.read_starts == [15] and clock.elapsed == 15.25
    assert [read.checkpoint_seconds for read in store.reads] == [15]
    store.device.set_temperature.assert_not_awaited()


async def test_restore_exact_remaining_wait_defers_without_checkpoint_60(monkeypatch):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock, sdk_seconds=0)

    await store.executor().execute_due_actions()

    assert store.restore.status == "pending"
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert [read.checkpoint_seconds for read in store.reads] == [15]
    assert clock.read_starts == [15]
    assert clock.elapsed == 45.25
    assert clock.waits == [(0, 15), (15.25, 15), (30.25, 15)]
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["verification_deadline_monotonic"] == 1075.25
    assert metadata["checkpoints"] == [15] and metadata["drain"] == "confirmed"
    assert json.loads(store.restore.result_json)["reason"] == "SafetyAttemptDeadline"
    assert not store.audits and store.connection.events[-1] == "exit"


@pytest.mark.parametrize("overrun_seconds", [2.0, 2.001])
async def test_restore_checkpoint_wait_overrun_reserves_and_reads_nothing(
    monkeypatch, overrun_seconds
):
    clock = _S1Clock(monkeypatch)
    store = _S1RestoreStore(clock=clock)
    executor = store.executor()
    reservations = []
    original_reserve = executor._reserve_verification_read

    async def reserve(action, lane, phase, checkpoint):
        remaining = executor._attempt_for(action).remaining()
        assert remaining > 0
        reservations.append(checkpoint)
        return await original_reserve(action, lane, phase, checkpoint)

    async def delayed_sleep(seconds):
        await clock.sleep(seconds)
        if len(clock.waits) == 4:
            clock.advance(overrun_seconds)
            await asyncio.sleep(0)

    executor._reserve_verification_read = reserve
    executor._sleep = delayed_sleep
    await executor.execute_due_actions()

    assert store.restore.status == "pending" and not store.audits
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)
    assert len(clock.waits) == 4 and clock.read_starts == [15]
    assert reservations == [15] and [read.checkpoint_seconds for read in store.reads] == [15]
    result = json.loads(store.restore.result_json)
    assert result["reason"] == "SafetyAttemptDeadline"
    metadata = result["safety_attempt"]
    assert clock.monotonic() >= metadata["verification_deadline_monotonic"]
    assert metadata["checkpoints"] == [15] and metadata["drain"] == "confirmed"
    assert store.connection.events[-1] == "exit"


async def test_restore_fence_quota_wait_blocks_reclaim(monkeypatch):
    store = _S1RestoreStore(clock=_S1Clock(monkeypatch))
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_quota():
        entered.set()
        await release.wait()

    store.wrapper._write_limiter.acquire = AsyncMock(side_effect=wait_for_quota)
    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        old_token = store.restore.safety_claim_token
        assert (
            await store.executor()._claim_due_safety_action(
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
            )
            is None
        )
        assert store.restore.safety_claim_token == old_token
        assert store.restore.status == "executing"
        store.device.set_temperature.assert_not_awaited()
        release.set()
        await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert store.restore.status == "executed"
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)


async def test_restore_fence_auth_retry_blocks_reclaim(monkeypatch):
    from aioaquarea import AquareaEnvironment
    from aioaquarea.api_client import AquareaAPIClient
    from aioaquarea.errors import AuthenticationError, AuthenticationErrorCodes

    store = _S1RestoreStore(clock=_S1Clock(monkeypatch))
    entered, release = asyncio.Event(), asyncio.Event()
    response = SimpleNamespace(
        content_type="application/json", status=200, json=AsyncMock(return_value={})
    )
    sdk_session = SimpleNamespace(request=AsyncMock(return_value=response))
    api = AquareaAPIClient(
        sdk_session, SimpleNamespace(), SimpleNamespace(), AquareaEnvironment.PRODUCTION
    )
    api.look_for_errors = AsyncMock(
        side_effect=[
            [AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED, "test expired")],
            [],
        ]
    )
    monkeypatch.setattr(
        "aioaquarea.api_client.PanasonicRequestHeader.get", AsyncMock(return_value={})
    )

    async def recover():
        entered.set()
        await release.wait()

    recovery = AsyncMock(side_effect=recover)
    api.set_refresh_authentication_callback(recovery)

    async def sdk_command(temperature, *, zone_id):
        await store.clock.dispatch(2)
        await api.request("POST", url="/offline-restore", json={"temperature": temperature})
        store.device.zones[zone_id].heat_target_temperature = temperature

    store.device.set_temperature = AsyncMock(side_effect=sdk_command)
    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        old_token = store.restore.safety_claim_token
        assert (
            await store.executor()._claim_due_safety_action(
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
            )
            is None
        )
        assert store.restore.safety_claim_token == old_token
        assert sdk_session.request.await_count == 1
        release.set()
        await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert store.restore.status == "executed"
    recovery.assert_awaited_once()
    assert sdk_session.request.await_count == 2
    store.device.set_temperature.assert_awaited_once_with(35, zone_id=1)


@pytest.mark.parametrize("wait_at", ["ledger", "quota"])
async def test_restore_verification_reservation_wait_is_fenced(wait_at):
    store = _S1RestoreStore(current_target=35)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_admission():
        entered.set()
        await release.wait()

    if wait_at == "ledger":
        store.ledger_barrier = wait_for_admission
    else:
        store.wrapper._read_limiter.acquire = AsyncMock(side_effect=wait_for_admission)
    worker = asyncio.create_task(store.executor().execute_due_actions())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (
            await store.executor()._claim_due_safety_action(
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
            )
            is None
        )
        store.restore.safety_claim_token = uuid4()
        release.set()
        await asyncio.wait_for(worker, 2)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert len(store.reads) == (0 if wait_at == "ledger" else 1)
    store.device.refresh_data.assert_not_awaited()
    store.device.set_temperature.assert_not_awaited()
    assert store.restore.status == "executing" and not store.audits


@pytest.mark.parametrize(
    "defect", ["cached", "observed_future", "retrieved_future", "read_future", "preclaim"]
)
async def test_restore_missing_source_clock_rejects_invalid_live_proof(defect):
    store = _S1RestoreStore(current_target=35)
    original = store.wrapper._refresh_restore_status

    async def invalid_proof(attempt):
        proof = await original(attempt)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(microseconds=1_000_000)
        if defect == "cached":
            return replace(proof, live=False)
        if defect == "observed_future":
            return replace(proof, observed_at=future)
        if defect == "retrieved_future":
            return replace(proof, retrieved_at=future)
        if defect == "read_future":
            return replace(proof, read_finished_at=future)
        return replace(proof, started_monotonic=attempt.claimed_monotonic - 0.001)

    store.wrapper._refresh_restore_status = invalid_proof

    await store.executor().execute_due_actions()

    assert store.restore.status == "pending"
    assert not store.audits and len(store.reads) == 1
    store.device.set_temperature.assert_not_awaited()


async def test_restore_postclaim_proof_survives_detached_row_sessions():
    store = _S1RestoreStore(current_target=35, detached=True)

    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    metadata = json.loads(store.restore.result_json)["safety_attempt"]
    assert metadata["live_read"]["checkpoint"] == 15
    assert metadata["live_read"]["live"] is True
    assert metadata["drain"] == "confirmed"


@pytest.mark.parametrize("clock", ["captured_at", "observed_at", "retrieved_at"])
async def test_future_snapshot_provenance_is_not_quality_evidence(monkeypatch, clock):
    from packages.core import device_data_quality
    from packages.core.models import DeviceStatusRecord

    await _s1_future_quality(monkeypatch)
    provenance = {"captured_at": S1_NOW.isoformat(), "timing": {}}
    destination = provenance if clock == "captured_at" else provenance["timing"]
    destination[clock] = (S1_NOW + dt.timedelta(microseconds=1)).isoformat()
    record = DeviceStatusRecord(
        ts=S1_NOW, device_id="future-metadata", native_policy_provenance=provenance
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_s1_result(record)))
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))

    quality = await device_data_quality.get_device_data_quality(now=S1_NOW)

    assert quality["ready"] is False
    assert quality["age_seconds"] is None
    assert quality["reasons"] == ["device_status_future"]


async def test_future_forecast_target_is_not_snapshot_issue_provenance(monkeypatch):
    from packages.core import device_data_quality
    from packages.core.models import DeviceStatusRecord

    await _s1_future_quality(monkeypatch)
    record = DeviceStatusRecord(
        ts=S1_NOW,
        device_id="forecast-target",
        native_policy_provenance={
            "captured_at": S1_NOW.isoformat(),
            "timer": {"slots": [{"target_timestamp": (S1_NOW + dt.timedelta(days=1)).isoformat()}]},
        },
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_s1_result(record)))
    monkeypatch.setattr(device_data_quality, "get_session", _s1_session_factory(session))

    assert (await device_data_quality.get_device_data_quality(now=S1_NOW))["ready"] is True


async def test_restore_ambiguous_dispatch_verifies_before_duplicate(monkeypatch):
    store = _S1RestoreStore(clock=_S1Clock(monkeypatch))

    async def lost_acknowledgement(temperature, *, zone_id):
        await store.clock.dispatch(2)
        store.device.zones[zone_id].heat_target_temperature = temperature
        raise TimeoutError("test acknowledgement lost")

    store.device.set_temperature = AsyncMock(side_effect=lost_acknowledgement)

    await store.executor().execute_due_actions()

    assert store.restore.status == "executed"
    assert store.device.set_temperature.await_count == 1
    assert [read.checkpoint_seconds for read in store.reads] == [15, 60]
    assert (
        json.loads(store.restore.result_json)["safety_attempt"]["dispatch_outcome"] == "ambiguous"
    )


async def test_restore_null_token_requires_legacy_drain():
    store = _S1RestoreStore(current_target=35)
    store.restore.status = "executing"
    store.restore.safety_claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    executor = store.executor()

    assert await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc)) is None
    assert store.restore.safety_claim_token is None
    store.device.refresh_data.assert_not_awaited()
    assert not store.reads and not store.audits

    store.restore.status = "pending"
    await executor.execute_due_actions()

    assert store.restore.safety_claim_token is not None
    assert store.restore.status == "executed"
    store.device.refresh_data.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "tokenized"),
    [
        ("pending", True),
        ("executing", False),
        ("dispatched", False),
        ("executing", True),
        ("dispatched", True),
    ],
)
@pytest.mark.parametrize("unproven", ["boot_id", "elapsed", "advisory_released"])
async def test_restore_unsettled_other_action_blocks_device(status, tokenized, unproven):
    from packages.core.models import PlanActionRecord

    store = _S1RestoreStore(current_target=35)
    metadata = {"boot_id": "different-old-boot", "drain": "unknown"}
    if unproven == "elapsed":
        metadata["started_at"] = (S1_NOW - dt.timedelta(days=1)).isoformat()
        metadata["bounds_seconds"] = {"overall": 110, "cancel_drain": 5}
    elif unproven == "advisory_released":
        metadata["advisory_released"] = True
        metadata["fence_lost"] = True
    actor = PlanActionRecord(
        id=20,
        plan_id=1,
        device_id=store.device_id,
        action_type="zone_temp_boost",
        payload_json='{"zone_id":2,"baseline_temperature":35,"temperature":37}',
        status=status,
        safety_claim_token=uuid4() if tokenized else None,
        scheduled_ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
        result_json=json.dumps({"safety_attempt": metadata} if tokenized else metadata),
    )
    store.actions.append(actor)

    assert await store.executor()._claim_due_safety_action(dt.datetime.now(dt.timezone.utc)) is None
    assert store.restore.status == "pending" and store.restore.safety_claim_token is None
    assert not store.reads and not store.audits
    store.device.set_temperature.assert_not_awaited()


async def test_restore_deadline_exhaustion_reserves_and_reads_nothing():
    import time

    store = _S1RestoreStore()
    executor = store.executor()
    claimed = await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
    assert claimed is not None
    attempt = executor._attempt_for(claimed)
    attempt.work_deadline = time.monotonic() - 0.001

    await executor._execute_safety_action(claimed)

    assert not store.reads and not store.audits
    store.device.refresh_data.assert_not_awaited()
    store.device.set_temperature.assert_not_awaited()
    assert store.restore.status == "pending"


@pytest.mark.parametrize("budget", ["work", "verification", "overall"])
async def test_restore_expired_budget_cannot_admit_checkpoint_60(budget):
    import time

    from packages.core.safety_attempts import DRAIN_SECONDS, SafetyAttemptDeadline

    store = _S1RestoreStore()
    executor = store.executor()
    claimed = await executor._claim_due_safety_action(dt.datetime.now(dt.timezone.utc))
    assert claimed is not None
    attempt = executor._attempt_for(claimed)
    attempt.authority_check = lambda: executor._assert_safety_authority(claimed)
    try:
        await attempt.run(executor._read_safety_observation(claimed, 15))
        assert [read.checkpoint_seconds for read in store.reads] == [15]
        progress = store.restore.result_json
        now = time.monotonic()
        if budget == "work":
            attempt.work_deadline = now
        elif budget == "verification":
            attempt.verify_deadline = now
        else:
            attempt.overall_deadline = now + DRAIN_SECONDS

        with pytest.raises(SafetyAttemptDeadline):
            await executor._read_safety_observation(claimed, 60)

        assert [read.checkpoint_seconds for read in store.reads] == [15]
        assert attempt.consumed_checkpoints == {15}
        assert store.restore.result_json == progress
        store.device.refresh_data.assert_awaited_once_with(allow_cached_fallback=False)
        store.device.set_temperature.assert_not_awaited()
        assert not store.audits
    finally:
        assert await attempt.drain()
        await executor._requeue_safety_action(claimed, "test_expired_budget_cleanup")
        assert await attempt.release()
    assert store.restore.status == "pending"


@pytest.mark.parametrize(
    "operation",
    [
        "skip",
        "defer",
        "cancel",
        "unverified",
        "failed",
        "ordinary_reservation",
        "ordinary_verification",
    ],
)
async def test_restore_cannot_enter_unfenced_legacy_mutation_helpers(operation):
    from packages.optimizer.actions import VerifyResult
    from packages.optimizer.executor_core import PlanExecutor

    store = _S1RestoreStore()
    session = SimpleNamespace(execute=AsyncMock(), add=Mock())
    executor = PlanExecutor(store.wrapper, session_factory=_s1_session_factory(session))
    action = store.restore
    with pytest.raises(PermissionError):
        if operation == "skip":
            await executor._skip_action(action, {"reason": "test"})
        elif operation == "defer":
            await executor._defer_action(action, {"reason": "test"})
        elif operation == "cancel":
            await executor._mark_cancelled(action)
        elif operation == "unverified":
            await executor._mark_executed_unverified(action, "test")
        elif operation == "failed":
            await executor._mark_failed(action, 1, VerifyResult(ok=False))
        elif operation == "ordinary_reservation":
            await executor._reserve_verification_read(action, "ordinary", "initial", 15)
        else:
            await executor._verify_with_retry(action, {}, {})
    session.execute.assert_not_awaited()
    session.add.assert_not_called()
    assert store.restore.status == "pending"

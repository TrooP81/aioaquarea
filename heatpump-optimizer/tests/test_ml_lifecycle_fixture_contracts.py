"""Offline contract regressions for the owned lifecycle fixtures (no DB access)."""

from pathlib import Path
import ast
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import datetime as dt
import importlib
import json
import pickle
import hashlib
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


SOURCE = Path(__file__).parent / "e2e/test_ml_lifecycle_postgresql.py"


def _investigation_events(observer):
    return [json.loads(value) for value in observer.events]


@pytest.mark.parametrize("outcome", ["identity", "none", "error", "cancel"])
async def test_investigation_wrapper_is_transparent(fixtures, outcome):
    import inspect

    observer = fixtures._Investigation()
    result = object() if outcome == "identity" else None
    failure = asyncio.CancelledError("SECRET") if outcome == "cancel" else ValueError("SECRET")
    calls = []

    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        if outcome in ("error", "cancel"):
            raise failure
        return result

    owner = SimpleNamespace(call=original)
    observer.wrap(owner, "call", "batch")
    assert inspect.signature(owner.call) == inspect.signature(original)
    assert owner.call.__name__ == original.__name__
    argument = object()
    try:
        if outcome in ("error", "cancel"):
            with pytest.raises(type(failure)) as raised:
                await owner.call(argument, keyword=argument)
            assert raised.value is failure
        else:
            assert await owner.call(argument, keyword=argument) is result
        assert calls == [((argument,), {"keyword": argument})]
        assert observer.active == 0
        assert observer.operation.get() == 0
        assert b"SECRET" not in b"".join(observer.events)
    finally:
        observer.restore()
    assert owner.call is original


@pytest.mark.parametrize(
    "reason,classification",
    [
        ("indoor_scope_unavailable", "inputs_unavailable"),
        ("indoor_dependency_scope_mismatch", "dependency_mismatch"),
        ("publication_conflict", "publication_fenced"),
        ("SECRET_EXCEPTION_TEXT", "UNKNOWN"),
    ],
)
async def test_investigation_publish_separates_fixed_failure_categories(
    fixtures, reason, classification
):
    observer = fixtures._Investigation()
    failure = fixtures.CASConflict(reason)

    async def publish(**kwargs):
        raise failure

    owner = SimpleNamespace(publish=publish)
    observer.wrap(owner, "publish", "publish")
    try:
        with pytest.raises(fixtures.CASConflict) as raised:
            await owner.publish(role="incumbent", qualification=object())
        assert raised.value is failure
        events = _investigation_events(observer)
        assert events[0]["publication_role"] == "incumbent"
        assert events[0]["qualification"] == "SUPPLIED"
        assert events[-1]["failure_class"] == classification
        assert reason.encode() not in b"".join(observer.events)
    finally:
        observer.restore()


@pytest.mark.parametrize("poisoned", [False, True])
async def test_investigation_wraps_real_batch_once_without_control_or_tasks(
    fixtures, monkeypatch, poisoned
):
    from packages.core import database, log_sink

    failure = RuntimeError("SECRET_BATCH_ERROR")
    calls = []
    pending = [object()]

    class Session:
        def add(self, value):
            calls.append("add")
            pending.append(value)

        async def execute(self, statement):
            calls.append("execute")
            if poisoned:
                raise failure
            return object()

    @asynccontextmanager
    async def sessions():
        calls.append("enter")
        try:
            yield Session()
        finally:
            calls.append("exit")

    monkeypatch.setattr(database, "get_session", sessions)
    observer = fixtures._Investigation()
    original = log_sink._write_batch
    observer.wrap(log_sink, "_write_batch", "batch")
    entry = SimpleNamespace(row={"event": "SECRET_ROW", "service": "offline", "level": "info"})
    tasks = asyncio.all_tasks()
    try:
        if poisoned:
            with pytest.raises(RuntimeError) as raised:
                await log_sink._write_batch([entry])
            assert raised.value is failure
        else:
            assert await log_sink._write_batch([entry]) is None
        assert calls == ["enter", "add", "execute", "exit"]
        assert len(pending) == 2
        assert asyncio.all_tasks() == tasks
        assert _investigation_events(observer)[0]["batch_count"] == 1
        assert b"SECRET" not in b"".join(observer.events)
    finally:
        observer.restore()
    assert log_sink._write_batch is original


async def test_investigation_profile_distinguishes_suspend_return_exception_cancel(fixtures):
    observer = fixtures._Investigation()
    entered, release = asyncio.Event(), asyncio.Event()

    async def materialize(*, outcome):
        entered.set()
        await release.wait()
        if outcome == "error":
            raise ValueError("SECRET")
        return object()

    prior = sys.getprofile()
    observer.install_profile((("materialize", materialize),))
    try:
        if prior is not None:
            assert sys.getprofile() is prior
            assert observer.drops["profile"] == 1
            return
        for outcome in ("return", "error", "cancel"):
            entered.clear()
            release.clear()
            before = len(observer.events)
            task = asyncio.create_task(materialize(outcome=outcome))
            await entered.wait()
            assert len(observer.events) == before  # Suspension is not completion.
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "error":
                release.set()
                with pytest.raises(ValueError, match="SECRET"):
                    await task
            else:
                release.set()
                assert await task is not None
            assert len(observer.events) == before + (outcome == "return")
    finally:
        observer.restore()
    assert sys.getprofile() is prior


async def test_investigation_requires_code_identity_not_structural_equality(fixtures):
    from types import FunctionType

    async def original():
        return None

    copy = original.__code__.replace()
    assert copy == original.__code__ and copy is not original.__code__
    duplicate = FunctionType(copy, original.__globals__)
    observer = fixtures._Investigation()
    prior = sys.getprofile()
    observer.install_profile((("materialize", original),))
    try:
        assert await duplicate() is None
        assert observer.events == []
        assert await original() is None
        assert len(observer.events) == (0 if prior is not None else 1)
    finally:
        observer.restore()
    assert sys.getprofile() is prior


def test_investigation_existing_and_unsupported_profiler_preserved(fixtures, monkeypatch):
    observer = fixtures._Investigation()
    prior = sys.getprofile()

    def sentinel(*args):
        return None

    monkeypatch.setattr(sys, "getprofile", lambda: sentinel)
    setter = AsyncMock()
    monkeypatch.setattr(sys, "setprofile", setter)
    observer.install_profile(())
    observer.restore()
    setter.assert_not_called()
    assert observer.drops["profile"] == 1
    monkeypatch.setattr(sys, "getprofile", lambda: None)
    monkeypatch.setattr(sys, "version_info", (3, 99))
    observer.install_profile(())
    setter.assert_not_called()
    assert observer.drops["profile"] == 2
    assert prior is None or callable(prior)


@pytest.mark.parametrize("poisoned", [False, True])
async def test_investigation_real_materializer_adds_no_sql_or_session_control(
    fixtures, monkeypatch, poisoned
):
    from packages.ml import forecast_quality
    from packages.core import settings_service
    from packages.poller import smartthings

    values = {
        "weather_provider": "smhi",
        "timezone": "Europe/Stockholm",
        "comfort_reference_sensor_id": "selected-room",
        "SECRET_SETTINGS_ID": object(),
    }
    settings_reader = AsyncMock(return_value=values)
    monkeypatch.setattr(settings_service, "get_all_settings", settings_reader)
    selection = AsyncMock(return_value=["selected-room"])
    monkeypatch.setattr(smartthings, "get_selected_device_ids", selection)
    failure = RuntimeError("SECRET poisoned transaction")

    class Session:
        def __init__(self):
            self.pending = [object()]
            self.calls = []

        async def execute(self, statement):
            self.calls.append("execute")
            if poisoned:
                raise failure
            # Only the application may consume this result.
            session = self

            class Result:
                def scalars(self):
                    session.calls.append("scalars")
                    return self

                def all(self):
                    session.calls.append("all")
                    return []

            return Result()

        def __getattr__(self, name):
            raise AssertionError("unexpected session control/access: " + name)

    session = Session()
    pending = session.pending[0]
    observer = fixtures._Investigation()
    observer.install_profile((("materialize", forecast_quality.materialize_native_inputs),))
    now = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
    before_tasks = asyncio.all_tasks()
    try:
        if poisoned:
            with pytest.raises(RuntimeError) as raised:
                await forecast_quality.materialize_native_inputs(
                    "thermal", now=now, session=session
                )
            assert raised.value is failure
            assert session.calls == ["execute"]
        else:
            assert (
                await forecast_quality.materialize_native_inputs(
                    "thermal", now=now, session=session
                )
                is None
            )
            assert session.calls == ["execute", "scalars", "all"]
            if observer.profile is not None:
                event = _investigation_events(observer)[0]
                assert event["status_count"] == 0
                assert event["clock"] == now.timestamp()
                assert event["provider"] == "smhi"
                assert event["timezone"] == "Europe/Stockholm"
                assert event["indoor_available"] == "NOT_REACHED"
                assert event["reference"] == "selected-room"
        assert asyncio.all_tasks() == before_tasks
        assert session.pending == [pending]
        settings_reader.assert_awaited_once_with(session=session)
        selection.assert_awaited_once_with(values=values)
    finally:
        observer.restore()


def test_investigation_hostile_locals_are_not_stringified_or_retained(fixtures, monkeypatch):
    import gc
    import weakref

    class Hostile:
        def __str__(self):
            raise AssertionError("str called")

        def __repr__(self):
            raise AssertionError("repr called")

        def __iter__(self):
            raise AssertionError("iteration called")

        def __hash__(self):
            raise AssertionError("hash called")

    observer = fixtures._Investigation()
    hostile = Hostile()
    code = (lambda: None).__code__
    observer.codes[id(code)] = (code, "prepare", frozenset())
    frame = SimpleNamespace(
        f_code=code,
        f_locals={
            "values": {"weather_provider": hostile, "timezone": "SECRET", "id": hostile},
            "component": hostile,
            "selected_ids": [hostile],
            "reference_sensor": "SECRET",
            "capabilities": [hostile],
            "statuses": [hostile],
            "readings": [hostile],
            "indoor_available": hostile,
            "reference_blocked": hostile,
        },
    )
    observer.record(
        "inputs_returned", lambda frame=frame, hostile=hostile: observer.inputs(frame, hostile)
    )
    assert len(observer.events) == 1
    encoded = b"".join(observer.events)
    assert b"SECRET" not in encoded
    assert b"Hostile" not in encoded
    assert _investigation_events(observer)[0]["indoor_available"] == "NOT_REACHED"
    assert observer.drops["serialization"] == 0
    reference = weakref.ref(hostile)
    frame.f_locals["values"]["self"] = frame.f_locals["values"]
    observer.record(
        "inputs_returned", lambda frame=frame, hostile=hostile: observer.inputs(frame, hostile)
    )
    del frame, hostile
    gc.collect()
    assert reference() is None
    stamp = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
    row = fixtures.DeviceStatusRecord(ts=stamp)

    def unloaded_access(_row):
        raise AssertionError("observer invoked an ORM descriptor")

    with monkeypatch.context() as patch:
        patch.setattr(fixtures.DeviceStatusRecord, "ts", property(unloaded_access))
        assert observer.loaded(row, "ts") is stamp
        del row.__dict__["ts"]
        assert observer.loaded(row, "ts") is None


@pytest.mark.parametrize(
    "age,available", [(-1, False), (0, True), (899, True), (900, True), (901, False)]
)
def test_investigation_observes_real_prepare_sensor_freshness(fixtures, age, available):
    from packages.ml import forecast_quality

    observer = fixtures._Investigation()
    now = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
    status_values = dict.fromkeys(
        column.name for column in fixtures.DeviceStatusRecord.__table__.columns
    )
    status_values.update(
        device_id="fixture-device",
        ts=now,
        direction="HEAT",
        device_action="HEATING",
        native_policy_provenance={},
        space_heating_active=True,
        tank_temp=30,
    )
    status = SimpleNamespace(**status_values)
    data = {
        "component": "thermal",
        "capabilities": ("indoor_heating",),
        "now": now,
        "values": {},
        "device_id": "fixture-device",
        "selected_ids": ["selected-room"],
        "reference_sensor": "selected-room",
        "reference_blocked": False,
        "statuses": [status],
        "weather": [],
        "prices": [],
        "counters": [],
        "weather_provider": "open-meteo",
        "timer": None,
        "raw_indoor": [
            SimpleNamespace(
                device_id="selected-room",
                timestamp=now - dt.timedelta(seconds=age),
                temperature=20.0,
                is_stale=False,
            ),
            SimpleNamespace(
                device_id="selected-room",
                timestamp=now,
                temperature=float("nan"),
                is_stale=False,
            ),
            SimpleNamespace(
                device_id="selected-room",
                timestamp=now,
                temperature=20,
                is_stale=True,
            ),
            SimpleNamespace(
                device_id="SECRET_UNSELECTED_SENSOR",
                timestamp=now,
                temperature=20,
                is_stale=False,
            ),
        ],
    }
    prior = sys.getprofile()
    observer.install_profile((("prepare", forecast_quality._prepare_native_inputs),))
    try:
        result = forecast_quality._prepare_native_inputs(data)
    finally:
        observer.restore()
    assert (result is not None) is available
    if prior is not None:
        assert observer.drops["profile"] == 1
        assert sys.getprofile() is prior
        return
    observed = _investigation_events(observer)
    assert len(observed) == 1
    assert observed[0]["indoor_available"] is available
    assert observed[0]["sensor_freshness_seconds"] == 900
    if age < 0:
        assert "latest_selected_age" not in observed[0]
    else:
        assert observed[0]["latest_selected_age"] == age
    assert observed[0]["filtered_count"] == (0 if age < 0 else 1)
    assert observed[0]["fresh_count"] == (1 if available else 0)
    assert observed[0]["sensor_count"] == 1
    assert b"SECRET" not in b"".join(observer.events)


def test_investigation_pending_orm_and_poisoned_transaction_are_untouched(fixtures):
    from sqlalchemy import Column, Integer, Table, MetaData, create_engine, event, text
    from sqlalchemy.orm import Session, registry
    from sqlalchemy.exc import IntegrityError, PendingRollbackError

    mapper = registry()
    table = Table("offline_observation", MetaData(), Column("id", Integer, primary_key=True))

    class Row:
        pass

    mapper.map_imperatively(Row, table)
    engine = create_engine("sqlite://")
    table.create(engine)
    flushes, sql = [], []
    event.listen(engine, "before_cursor_execute", lambda *args: sql.append(1))
    observer = fixtures._Investigation()
    observer.install_events()
    try:
        with Session(engine) as session:
            event.listen(session, "before_flush", lambda *args: flushes.append(1))
            row = Row()
            row.id = 1
            session.add(row)
            assert session.execute(text("SELECT 1")).scalar_one() == 1
            assert row in session.new
            assert flushes == []
            assert len(sql) == 1
            session.flush()  # Test-owned setup, not an observer operation.
            session.expunge(row)
            duplicate = Row()
            duplicate.id = 1
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                session.flush()
            before_sql, before_flush = len(sql), len(flushes)
            before_events = len(observer.events)
            with pytest.raises(PendingRollbackError):
                session.execute(text("SELECT 1"))
            assert not session.is_active
            assert len(sql) == before_sql
            assert len(flushes) == before_flush
            assert len(observer.events) == before_events
    finally:
        observer.restore()
        engine.dispose()
        mapper.dispose()


def test_investigation_sensor_and_thread_bounds_are_explicit(fixtures):
    import threading

    observer = fixtures._Investigation()
    code = (lambda: None).__code__
    observer.codes[id(code)] = (code, "prepare", frozenset())
    hostile = object()
    frame = SimpleNamespace(
        f_code=code,
        f_locals={
            "selected_ids": [hostile] * 65,
            "sensor_ids": [hostile] * 65,
            "fresh_ids": [hostile] * 65,
            "readings": [hostile] * 5001,
            "values": {"timezone": "SECRET" * 100000},
        },
    )
    observer.record("inputs_returned", lambda: observer.inputs(frame, None))
    record = _investigation_events(observer)[0]
    assert record["selected_count"] == "UNKNOWN"
    assert record["sensor_count"] == "UNKNOWN"
    assert record["fresh_count"] == "UNKNOWN"
    assert record["reading_count"] == 5001
    assert observer.drops["sensors"] == 3
    thread = threading.Thread(target=lambda: observer.record("test"))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(observer.events) == 1
    assert observer.drops["thread"] == 1
    frame.f_locals = {"readings": [hostile] * 5002}
    observer.record("inputs_returned", lambda: observer.inputs(frame, None))
    assert _investigation_events(observer)[-1]["reading_count"] == "UNKNOWN"
    assert observer.drops["sensors"] == 4
    assert b"SECRET" not in b"".join(observer.events)


async def test_investigation_operation_cap_and_final_boundary_never_settle(fixtures, monkeypatch):
    from packages.core import log_sink

    observer = fixtures._Investigation()

    async def original():
        return None

    owner = SimpleNamespace(call=original)
    observer.wrap(owner, "call", "batch")
    for name in ("shutdown_log_sink", "_flush_once"):
        monkeypatch.setattr(log_sink, name, AsyncMock(side_effect=AssertionError("settled")))
    try:
        for _ in range(129):
            await owner.call()
        observer.pending()
        assert len(observer.origins) == 128
        assert observer.next_alias["operation"] == 128
        assert observer.drops["aliases"] == 1
        assert _investigation_events(observer)[-2]["operation"] == 0
        log_sink.shutdown_log_sink.assert_not_called()
        log_sink._flush_once.assert_not_called()
    finally:
        observer.restore()


async def test_investigation_serialization_failure_cannot_change_real_result_or_error(
    fixtures, monkeypatch
):
    observer = fixtures._Investigation()
    expected = object()
    failure = RuntimeError("SECRET")

    async def original(*, fail=False):
        if fail:
            raise failure
        return expected

    owner = SimpleNamespace(call=original)
    observer.wrap(owner, "call", "batch")

    def broken(*args, **kwargs):
        raise ValueError("SECRET_SERIALIZER")

    monkeypatch.setattr(fixtures.json, "dumps", broken)
    try:
        assert await owner.call() is expected
        with pytest.raises(RuntimeError) as raised:
            await owner.call(fail=True)
        assert raised.value is failure
        assert observer.events == []
        assert observer.drops["serialization"] == 4
    finally:
        observer.restore()


def test_investigation_partial_profile_install_and_wrapper_restore_failure_are_independent(
    fixtures, monkeypatch
):
    observer = fixtures._Investigation()
    prior = sys.getprofile()
    if prior is not None:
        observer.install_profile(())
        assert observer.drops["profile"] == 1
        assert sys.getprofile() is prior
        return
    setter = sys.setprofile
    changes = []

    def partial(callback):
        changes.append(callback)
        setter(callback)
        if len(changes) == 1:
            raise RuntimeError("SECRET_PROFILE_INSTALL")

    class Owner:
        def __init__(self):
            self.broken = False
            self.call = original

        def __setattr__(self, name, value):
            if name == "call" and self.broken:
                raise RuntimeError("SECRET_RESTORE")
            object.__setattr__(self, name, value)

    async def original():
        return None

    owner = Owner()
    observer.wrap(owner, "call", "batch")
    owner.broken = True
    monkeypatch.setattr(fixtures.sys, "setprofile", partial)
    try:
        with pytest.raises(RuntimeError, match="SECRET_PROFILE_INSTALL"):
            observer.install_profile(())
    finally:
        observer.restore()
    assert changes == [observer.profile, None]
    assert sys.getprofile() is prior
    assert observer.drops["restore"] == 1


def test_investigation_events_cover_existing_and_new_engines_without_extra_sql(fixtures):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.exc import OperationalError

    observer = fixtures._Investigation()
    existing = create_engine("sqlite://")
    calls = []

    def count(*args):
        calls.append(1)

    event.listen(existing, "before_cursor_execute", count)
    observer.install_events()
    newer = create_engine("sqlite://")
    event.listen(newer, "before_cursor_execute", count)
    try:
        for engine in (existing, newer):
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1 /* SECRET_LITERAL */")).scalar_one() == 1
                connection.commit()
                with pytest.raises(OperationalError):
                    connection.execute(
                        text("INSERT INTO app_logs(message) VALUES (:secret)"),
                        {"secret": "SECRET_PARAMETER"},
                    )
                connection.rollback()
        events = _investigation_events(observer)
        labels = [item["event"] for item in events]
        assert len(calls) == 4
        assert labels.count("sql_attempted") == 4
        assert labels.count("cursor_completed") == 2
        assert labels.count("sql_failed") == 2
        assert labels.count("commit_notified") == 2
        assert labels.count("rollback_notified") == 2
        assert len({item["engine"] for item in events}) == 2
        assert all(item["resolved_relation"] == "UNKNOWN" for item in events)
        assert all(item["sequence_consumer"] == "UNKNOWN" for item in events)
        assert all(item["backend"] == "UNKNOWN" for item in events)
        assert b"SECRET" not in b"".join(observer.events)
        assert b"confirmed_commit" not in b"".join(observer.events)
    finally:
        observer.restore()
        existing.dispose()
        newer.dispose()


@pytest.mark.parametrize(
    "statement,schema",
    [
        ("INSERT INTO app_logs (message) VALUES ('SECRET')", "UNQUALIFIED"),
        ("INSERT INTO public.app_logs (message) VALUES ('SECRET')", "public"),
        ("SELECT setval('public.app_logs_id_seq', 123)", "UNKNOWN"),
        ("SELECT * FROM pg_class", "UNKNOWN"),
        (" " * 2048 + "INSERT INTO public.app_logs VALUES ('SECRET')", "UNKNOWN"),
    ],
)
def test_investigation_declared_target_never_proves_execution_or_sequence(
    fixtures, statement, schema
):
    observer = fixtures._Investigation()
    result = observer.target(None, statement)
    assert result["declared_schema"] == schema
    assert result["resolved_relation"] == "UNKNOWN"
    assert result["sequence_consumer"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(result)


async def test_investigation_cross_node_pending_batch_is_not_settled(fixtures, monkeypatch):
    observer = fixtures._Investigation()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def batch(argument):
        calls.append(argument)
        entered.set()
        await release.wait()

    owner = SimpleNamespace(call=batch)
    observer.wrap(owner, "call", "batch")
    observer.node = 1
    task = asyncio.create_task(owner.call(object()), name="SECRET_TASK_NAME")
    try:
        await entered.wait()
        observer.node = 2
        observer.stage = "post-drop"
        observer.pending()
        assert not task.done()
        assert observer.active == 1
        assert len(calls) == 1
        release.set()
        await task
        events = _investigation_events(observer)
        assert events[0]["operation_origin"] == 1
        assert events[-1]["node"] == 2
        assert events[-1]["operation_origin"] == 1
        assert b"SECRET_TASK_NAME" not in b"".join(observer.events)
    finally:
        release.set()
        await task
        observer.restore()


@pytest.mark.parametrize("bound", ["events", "arena", "event_bytes", "serialization"])
def test_investigation_storage_bounds_reserve_before_detail(fixtures, monkeypatch, bound):
    observer = fixtures._Investigation()
    assert (
        observer.EVENT_LIMIT,
        observer.EVENT_BYTES,
        observer.ARENA_BYTES,
        observer.RESERVE_BYTES,
    ) == (4096, 2048, 4 * 1024 * 1024, 16 * 1024)
    if bound == "serialization":
        monkeypatch.setattr(
            fixtures.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError())
        )
        observer.record("test")
    elif bound == "event_bytes":
        observer.record("test", lambda: {"padding": "x" * 2048})
    else:
        for _ in range(4097):
            observer.record("test", (lambda: {"padding": "x" * 1700}) if bound == "arena" else None)
        calls = []
        observer.record("test", lambda: calls.append(1))
        assert calls == []
        observer.pending()
        pending = json.loads(observer.pending_bytes)
        assert pending["active_operations"] == 0
        assert type(pending["buffered_rows"]) is int
        assert type(pending["flusher_present"]) is bool
        assert pending["process_boundary"] == "OPEN"
    assert observer.drops[bound] > 0
    assert len(observer.events) <= 4096
    assert observer.bytes <= 4 * 1024 * 1024
    assert all(len(item) <= 2048 for item in observer.events)
    observer.drops[bound] = 2**31 - 1
    observer.gap(bound)
    assert observer.drops[bound] == 2**31 - 1


@pytest.mark.parametrize("kind,cap", [("engine", 128), ("connection", 256), ("task", 256)])
def test_investigation_alias_maps_are_bounded_and_weak(fixtures, kind, cap):
    import gc
    import weakref

    class Item:
        pass

    observer = fixtures._Investigation()
    values = [Item() for _ in range(cap + 1)]
    assert [observer.alias(kind, item) for item in values[:cap]] == list(range(1, cap + 1))
    assert observer.alias(kind, values[-1]) == 0
    assert len(observer.aliases[kind]) == cap
    reference = weakref.ref(values[0])
    del values
    gc.collect()
    assert reference() is None
    assert observer.drops["aliases"] == 1


def test_investigation_stack_caps_no_source_or_hostile_paths(fixtures, monkeypatch):
    observer = fixtures._Investigation()
    code = (lambda: None).__code__
    inspected = []

    class Frame:
        def __init__(self, line):
            self.f_lineno = line
            self.f_back = None

        @property
        def f_code(self):
            inspected.append(self.f_lineno)
            return code

    frames = [Frame(index) for index in range(40)]
    for left, right in zip(frames, frames[1:]):
        left.f_back = right
    monkeypatch.setattr(fixtures.sys, "_getframe", lambda *a: frames[0])
    observer.stack_codes[id(code)] = (code, "known_code")
    assert observer.stack() == [["known_code", index] for index in range(6)]
    assert inspected == list(range(6))
    observer.stack_codes.clear()
    inspected.clear()
    assert observer.stack() == []
    assert inspected == list(range(24))


@pytest.mark.parametrize("failure", ["registration", "removal", "ownership", "profile"])
def test_investigation_cleanup_attempts_are_independent(fixtures, monkeypatch, failure):
    from sqlalchemy import event

    observer = fixtures._Investigation()
    original = AsyncMock()
    owner = SimpleNamespace(call=original)

    # AsyncMock has no __code__; use a genuine saved coroutine.
    async def call():
        return None

    owner.call = call
    prior_profile = sys.getprofile()
    observer.wrap(owner, "call", "batch")
    if failure == "registration":
        listen = event.listen
        attempts = []

        def partial(target, name, callback):
            attempts.append(name)
            if len(attempts) > 1:
                raise ValueError("SECRET")
            listen(target, name, callback)

        monkeypatch.setattr(event, "listen", partial)
        observer.install_events()
        assert len(attempts) == 5
    elif failure == "removal":
        observer.listeners = [("commit", object()), ("rollback", object())]
        attempts = []

        def broken(*args):
            attempts.append(args[1])
            raise ValueError("SECRET")

        monkeypatch.setattr(event, "remove", broken)
    elif failure == "ownership":
        owner.call = original
    elif failure == "profile":
        observer.profile = lambda *a: None
        # A different currently installed owner must remain untouched.
    observer.restore()
    assert sys.getprofile() is prior_profile
    if failure == "ownership":
        assert owner.call is original
        assert observer.drops["ownership"] == 1
    else:
        assert owner.call is call
    if failure == "removal":
        assert attempts == ["commit", "rollback"]
        assert observer.drops["restore"] == 2
    if failure == "profile":
        assert observer.drops["ownership"] == 1


def _decode_investigation_output(output):
    assert "LIFECYCLE_DIAGNOSTIC_INCOMPLETE" not in output, "diagnostic emission failure"
    records = []
    active = False
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if record.get("kind") == "header":
            assert not active
            active = True
        elif record.get("kind") == "footer":
            assert active
            assert record["count"] == len(records)
            encoded = [
                json.dumps(item, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"
                for item in records
            ]
            assert record["bytes"] == sum(map(len, encoded))
            assert record["sha256"] == hashlib.sha256(b"".join(encoded)).hexdigest()
            return records, record
        elif active:
            records.append(record)
    raise AssertionError("missing diagnostic footer")


@pytest.mark.parametrize("failure", ["passing", "setup", "call", "teardown"])
def test_investigation_ordinary_pytest_capture_retains_multiple_nodes(fixtures, tmp_path, failure):
    (tmp_path / "conftest.py").write_text(
        "from tests.e2e.test_ml_lifecycle_postgresql import (\n"
        "    _lifecycle_investigation, _lifecycle_investigation_node,\n"
        ")\n"
    )
    (tmp_path / "test_capture.py").write_text(
        "import pytest\n"
        "from tests.e2e import test_ml_lifecycle_postgresql as subject\n"
        "@pytest.fixture\n"
        "def fault():\n"
        f"    if {failure!r} == 'setup': raise RuntimeError('expected setup')\n"
        "    yield\n"
        f"    if {failure!r} == 'teardown': raise RuntimeError('expected teardown')\n"
        "def test_first():\n"
        "    print('ORDINARY_PASSING_PRINT')\n"
        "    subject._investigation_stage('baseline')\n"
        "def test_second(fault):\n"
        "    subject._investigation_stage('call')\n"
        f"    if {failure!r} == 'call': raise RuntimeError('expected call')\n"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(SOURCE.parents[2]), str(SOURCE.parents[3]))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == (0 if failure == "passing" else 1), run.stdout + run.stderr
    records, footer = _decode_investigation_output(run.stderr)
    assert {item["node"] for item in records} >= {1, 2}
    assert any(item["stage"] == "baseline" and item["node"] == 1 for item in records)
    assert any(item["event"] == "observation_boundary" for item in records)
    assert "ORDINARY_PASSING_PRINT" not in run.stdout + run.stderr
    assert footer["coverage_complete"] is False
    assert footer["pending"]["process_boundary"] == "OPEN"
    assert type(footer["pending"]["inflight_rows"]) is int
    with pytest.raises(AssertionError, match="missing diagnostic footer"):
        _decode_investigation_output(run.stderr[: run.stderr.rfind('{"bytes"')])


@pytest.mark.parametrize(
    "fault", ["enter", "exit", "write", "flush", "serialize", "digest", "reserve"]
)
def test_investigation_emission_failure_has_fixed_bounded_marker(
    fixtures, monkeypatch, capsys, fault
):
    observer = fixtures._Investigation()
    observer.record("test")

    class Capture:
        _global_capturing = None
        _capture_fixture = None

        def global_and_fixture_disabled(self):
            from contextlib import contextmanager

            @contextmanager
            def disabled():
                if fault == "enter":
                    raise RuntimeError("SECRET")
                try:
                    yield
                finally:
                    if fault == "exit":
                        raise RuntimeError("SECRET")

            return disabled()

    class Stream:
        def write(self, value):
            if fault == "write":
                raise RuntimeError("SECRET")
            return len(value)

        def flush(self):
            if fault == "flush":
                raise RuntimeError("SECRET")

    monkeypatch.setattr(fixtures.sys, "stderr", Stream())
    if fault == "serialize":
        monkeypatch.setattr(
            fixtures.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError())
        )
    if fault == "digest":
        monkeypatch.setattr(fixtures.hashlib, "sha256", lambda: (_ for _ in ()).throw(ValueError()))
    if fault == "reserve":
        monkeypatch.setattr(observer, "RESERVE_BYTES", 1)
    observer.emit(Capture())
    assert observer.drops["emission"] == 1
    output = capsys.readouterr().out
    assert output == "LIFECYCLE_DIAGNOSTIC_INCOMPLETE emission\n"
    assert len(output.encode()) < 2048
    with pytest.raises(AssertionError, match="diagnostic emission failure"):
        _decode_investigation_output(output)


@pytest.mark.parametrize("fault", ["partial_enter", "exit", "ownership"])
def test_investigation_capture_restoration_attempts_are_independent(fixtures, capsys, fault):
    from contextlib import contextmanager

    observer = fixtures._Investigation()
    attempts = []

    class State:
        started = True

        def is_started(self):
            return self.started

        _is_started = is_started

    class Capture:
        def __init__(self):
            self._global_capturing = State()
            self._capture_fixture = State()

        @contextmanager
        def global_and_fixture_disabled(self):
            self._global_capturing.started = False
            self._capture_fixture.started = False
            if fault == "partial_enter":
                raise RuntimeError("SECRET_PARTIAL_ENTER")
            yield
            if fault == "ownership":
                self._capture_fixture = State()
                self._capture_fixture.started = False
            raise RuntimeError("SECRET_EXIT")

        def resume_global_capture(self):
            attempts.append("global")
            raise RuntimeError("SECRET_GLOBAL_RESTORE")

        def resume_fixture(self):
            attempts.append("fixture")
            self._capture_fixture.started = True

    capture = Capture()
    observer.emit(capture)
    assert attempts == (["global"] if fault == "ownership" else ["global", "fixture"])
    assert observer.drops["emission"] == 1
    assert observer.drops["restore"] == 1
    assert observer.drops["ownership"] == (1 if fault == "ownership" else 0)
    assert capture._capture_fixture.started is (fault != "ownership")
    assert "SECRET" not in capsys.readouterr().out


def _function(name, source=None):
    tree = ast.parse(SOURCE.read_text() if source is None else source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


@pytest.mark.parametrize(
    "name",
    [
        "_readiness_source_fixture",
        "test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse",
    ],
)
def test_readiness_temperatures_are_explicit_and_ordered(name):
    node = _function(name)
    values = {
        key.value: value.value
        for mapping in ast.walk(node)
        if isinstance(mapping, ast.Dict)
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant)
        and key.value in {"comfort_temp_min", "comfort_temp_target", "comfort_temp_max"}
        and isinstance(value, ast.Constant)
    }
    assert tuple(
        float(values[key])
        for key in ("comfort_temp_min", "comfort_temp_target", "comfort_temp_max")
    ) == (18, 20.5, 25)


def test_artifact_uses_real_scope_before_signing():
    _assert_artifact_contract(SOURCE.read_text())


def _assert_artifact_contract(source):
    node = _function("_artifact", source)
    assert isinstance(node, ast.AsyncFunctionDef)
    source = ast.unparse(node)
    assert source.index("locked_demand_scope(device_id, session=session)") < source.index(
        "return publish_signed_artifact("
    )
    assert "'device_id': device_id" in source
    assert "'scope': manifest_scope" in source


@pytest.fixture
def fixtures(monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE.parents[2]))
    monkeypatch.setenv("SECRET_KEY", "offline-lifecycle-fixture-contract-test-key")
    return importlib.import_module("tests.e2e.test_ml_lifecycle_postgresql")


def _temperature_values(name, source=None):
    return {
        key.value: value.value
        for mapping in ast.walk(_function(name, source))
        if isinstance(mapping, ast.Dict)
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant)
        and str(key.value).startswith("comfort_temp_")
        and isinstance(value, ast.Constant)
    }


def test_real_settings_validator_accepts_snapshots_rejects_original_blanks(fixtures):
    from packages.core.settings_service import normalize_contract_settings_updates

    for name in (
        "_readiness_source_fixture",
        "test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse",
    ):
        values = _temperature_values(name)
        assert normalize_contract_settings_updates(values) == {
            "comfort_temp_min": "18.0",
            "comfort_temp_target": "20.5",
            "comfort_temp_max": "25.0",
        }
        for key in values:
            with pytest.raises(ValueError, match="finite numeric value"):
                normalize_contract_settings_updates({**values, key: ""})


def _assert_chronology(source):
    node = _function("test_UR2_SR1_repeatable_read_in_place_edit", source)
    writer = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.AsyncWith) and "set_settings_bulk" in ast.unparse(part)
    )
    signing = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "new_artifact" for t in part.targets)
    )
    text = ast.unparse(node)
    assert writer.end_lineno < signing.lineno
    assert "session=session" in ast.unparse(writer)
    assert text.index("old_artifact = await _artifact") < text.index("first = asyncio.create_task")
    assert (
        text.index("new_artifact = await _artifact")
        < text.index("expected_incumbent=old_artifact.artifact_hash")
        < text.index("changed.set()")
    )
    assert "asyncio.wait_for(reached.wait(), 3)" in ast.unparse(
        next(part for part in node.body if isinstance(part, ast.Try))
    )
    assert "finally:" in text and "asyncio.gather(first, return_exceptions=True)" in text
    for model in (
        "IndoorTempReading",
        "DeviceStatusRecord",
        "WeatherRecord",
        "ConsumptionRecord",
    ):
        assert f"update({model})" in ast.unparse(writer)
    assert ".values(ts=" not in ast.unparse(writer)
    assert ".values(timestamp=" not in ast.unparse(writer)


def test_ur2_signs_only_after_coherent_writer_commits():
    source = SOURCE.read_text()
    _assert_chronology(source)
    # Negative sensitivity: restore the old pre-transition signing order.
    node = _function("test_UR2_SR1_repeatable_read_in_place_edit", source)
    signing = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "new_artifact" for t in part.targets)
    )
    lines = source.splitlines(keepends=True)
    moved = "".join(lines[signing.lineno - 1 : signing.end_lineno])
    lines[signing.lineno - 1 : signing.end_lineno] = []
    index = next(i for i, line in enumerate(lines) if "reached, changed =" in line)
    lines.insert(index, "    " + moved.lstrip())
    with pytest.raises(AssertionError):
        _assert_chronology("".join(lines))


@pytest.mark.asyncio
async def test_artifact_real_signing_keeps_identity_and_scope(fixtures, monkeypatch, tmp_path):
    from packages.ml import forecast_quality, safe_persistence

    monkeypatch.setattr(fixtures.settings, "model_dir", str(tmp_path))
    monkeypatch.setattr(fixtures.settings, "secret_key", "offline-fixture-test-key")
    events = []
    # Windows cannot open directories for fsync. This offline test covers real
    # serialization/HMAC, not durability; the unchanged Linux fsync E2E covers that.
    directory_syncs = []
    monkeypatch.setattr(
        safe_persistence, "_fsync_directory", lambda path: directory_syncs.append(path)
    )
    scope = {"installation_id": "offline", "demand_dependency_generation": 3}
    session = object()

    @asynccontextmanager
    async def sessions():
        yield session
        events.append("committed")

    @asynccontextmanager
    async def locked(device_id, *, session):
        assert device_id == "fixture-device"
        events.append("scope")
        yield dict(scope), session

    monkeypatch.setattr(forecast_quality, "locked_demand_scope", locked)
    artifact = await fixtures._artifact(SimpleNamespace(sessions=sessions), "offline-fit")
    restored, payload = fixtures.verify_signed_artifact(
        artifact.artifact_hash, component="demand", schema_version="demand_lifecycle_v1"
    )
    assert events == ["scope", "committed"]
    assert directory_syncs and all(path.is_dir() for path in directory_syncs)
    assert restored.manifest["device_id"] == "fixture-device"
    assert restored.manifest["scope"] == scope
    assert pickle.loads(payload) == {"fit_id": "offline-fit", "weights": [1.25, 3.5]}
    scope["demand_dependency_generation"] = 4
    assert restored.manifest["scope"]["demand_dependency_generation"] == 3

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise ValueError("current_demand_scope_unavailable")
        yield

    monkeypatch.setattr(forecast_quality, "locked_demand_scope", unavailable)
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="current_demand_scope_unavailable"):
        await fixtures._artifact(SimpleNamespace(sessions=sessions), "must-not-sign")
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("key", ["device_id", "scope"])
def test_missing_signed_manifest_contract_is_detected(key):
    source = SOURCE.read_text()
    line = (
        '            "device_id": device_id,\n'
        if key == "device_id"
        else '            "scope": manifest_scope,\n'
    )
    with pytest.raises(AssertionError):
        _assert_artifact_contract(source.replace(line, ""))


def test_private_metadata_resolves_circular_and_plan_fks_without_global_mutation(fixtures):
    from sqlalchemy import create_mock_engine

    originals = {
        model: (
            model.__table__.schema,
            tuple(str(fk.target_fullname) for fk in model.__table__.foreign_keys),
        )
        for model in fixtures.FIXTURE_MODELS
    }
    metadata = fixtures._fixture_metadata("owned_fixture")
    statements = []
    engine = create_mock_engine(
        "postgresql://",
        lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    metadata.create_all(engine, checkfirst=False)
    ddl = "\n".join(statements)
    assert "CREATE TABLE owned_fixture.settings" in ddl
    assert "REFERENCES owned_fixture.house_installations" in ddl
    assert "REFERENCES owned_fixture.house_contract_revisions" in ddl
    assert "REFERENCES owned_fixture.plan_actions" in ddl
    assert "ALTER TABLE owned_fixture.house_" in ddl
    for model, original in originals.items():
        assert (
            model.__table__.schema,
            tuple(str(fk.target_fullname) for fk in model.__table__.foreign_keys),
        ) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "namespace,kind,resolved,default,owned,allowed",
    [
        ("owned", "S", 10, True, True, True),
        ("public", "S", 10, True, True, False),
        ("owned", "r", 10, True, True, False),
        ("owned", "S", 11, True, True, False),
        ("owned", "S", 10, False, True, False),
        ("owned", "S", 10, True, False, False),
    ],
)
async def test_sequence_ownership_negative_sensitivity(
    fixtures, namespace, kind, resolved, default, owned, allowed
):
    connection = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(one=lambda: (namespace, kind, resolved, default, owned))
        )
    )
    call = fixtures._assert_local_sequence(connection, "owned", 10, 20, "id")
    if allowed:
        await call
    else:
        with pytest.raises(AssertionError):
            await call


@pytest.mark.parametrize("public_only", [False, True])
def test_relation_oid_resolution_rejects_public_only_or_shadowed(fixtures, public_only):
    connection = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                one=lambda: (None, 99) if public_only else (10, 99),
                scalar_one=lambda: "owned",
            )
        )
    )
    with pytest.raises(AssertionError, match="nonlocal fixture table"):
        fixtures._require_local_table(
            SimpleNamespace(execute=lambda *args, **kwargs: connection.execute.return_value),
            fixtures.SettingRecord.__table__,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "public_relation", "public_fk"])
async def test_full_catalog_check_rejects_nonlocal_relations_and_fk_targets(fixtures, fault):
    models = {model.__tablename__: model for model in fixtures.FIXTURE_MODELS}
    oids = {name: index for index, name in enumerate(models, start=10)}

    async def execute(statement, parameters):
        sql = str(statement)
        if "SELECT c.oid, to_regclass(:name)" in sql:
            oid = oids[parameters["name"]]
            row = (oid, oid + 999 if fault == "public_relation" else oid, "r")
        elif "SELECT attname FROM pg_attribute" in sql:
            model = next(model for name, model in models.items() if oids[name] == parameters["oid"])
            columns = list(model.__table__.columns.keys())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: columns))
        elif "FROM pg_constraint c WHERE" in sql:
            model = next(model for name, model in models.items() if oids[name] == parameters["oid"])
            rows = [
                (
                    [element.parent.name for element in fk.elements],
                    oids[fk.referred_table.name] + (999 if fault == "public_fk" else 0),
                    [element.column.name for element in fk.elements],
                )
                for fk in model.__table__.foreign_key_constraints
            ]
            return SimpleNamespace(all=lambda: rows)
        elif "pg_get_serial_sequence" in sql:
            return SimpleNamespace(scalar_one=lambda: 500)
        elif "SELECT to_regclass(:name)::oid" in sql:
            return SimpleNamespace(scalar_one=lambda: 600)
        elif "SELECT n.nspname" in sql:
            row = ("owned", "S", parameters["seq"], True, parameters["seq"] != 600)
        else:
            raise AssertionError(sql)
        return SimpleNamespace(one=lambda: row)

    connection = SimpleNamespace(execute=execute)
    if fault is None:
        await fixtures._assert_fixture_storage(connection, "owned")
    else:
        with pytest.raises(AssertionError, match="nonlocal"):
            await fixtures._assert_fixture_storage(connection, "owned")


def test_public_comparison_failure_cannot_bypass_schema_drop():
    node = _function("lifecycle_database")
    cleanup = ast.unparse(_function("_cleanup_lifecycle_storage"))
    assert "_cleanup_lifecycle_storage" in ast.unparse(node)
    assert "('pre-drop', lambda: compare(" in cleanup
    assert "('post-drop', lambda: compare(" in cleanup
    assert cleanup.index("('drop', drop)") < cleanup.index("('absence', verify_absence)")
    assert cleanup.count("async with control.begin()") == 3
    assert "retained_storage_state" in cleanup
    assert "('control-dispose', control.dispose)" in cleanup
    assert "fixture_storage_ownership_checked" in ast.unparse(node)


def test_thermal_prerequisite_is_opt_in_after_locality_before_materialization():
    source = SOURCE.read_text()
    route = ast.unparse(_function("_thermal_route_owner"))
    assert route.index("_require_local_table") < route.index("_initialize_thermal_installation")
    assert route.index("_initialize_thermal_installation") < route.index("hook.materialize")
    assert "HouseInstallationRecord" in route
    assert source.count("await _initialize_thermal_installation(") == 1
    initializer = ast.unparse(_function("_initialize_thermal_installation"))
    for forbidden in ("set_settings_bulk", "ensure_current_contract", "_readiness_source_fixture"):
        assert forbidden not in initializer


@pytest.mark.asyncio
async def test_nonlocal_thermal_table_prevents_initialization(fixtures, monkeypatch):
    initialize = AsyncMock()
    monkeypatch.setattr(fixtures, "_initialize_thermal_installation", initialize)

    class Connection:
        async def run_sync(self, callback):
            callback(
                SimpleNamespace(
                    execute=lambda *args, **kwargs: SimpleNamespace(
                        scalar_one=lambda: "owned", one=lambda: (10, 99)
                    )
                )
            )

    @asynccontextmanager
    async def begin():
        yield Connection()

    with pytest.raises(AssertionError, match="nonlocal fixture table"):
        await fixtures._thermal_route_owner(
            SimpleNamespace(engine=SimpleNamespace(begin=begin)), monkeypatch
        )
    initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "present", "insert", "readback", "committed-readback"])
async def test_thermal_singleton_prerequisite_and_failures(fixtures, fault):
    events, added = [], []
    state = {"row": None}

    class Session:
        async def get(self, model, key):
            assert model is fixtures.HouseInstallationRecord and key == 1
            events.append("get")
            if fault == "present" and not added:
                return object()
            if fault == "committed-readback" and "commit" in events:
                return None
            return state["row"]

        def add(self, row):
            events.append("add")
            added.append(row)
            state["row"] = row

        async def flush(self):
            events.append("flush")
            if fault == "insert":
                raise ValueError("insert failed")

        async def refresh(self, row):
            events.append("refresh")
            if fault == "readback":
                row.indoor_dependency_generation = 9

    @asynccontextmanager
    async def sessions():
        try:
            yield Session()
            events.append("commit")
        except BaseException:
            events.append("rollback")
            if "commit" not in events:
                state["row"] = None
            raise

    call = fixtures._initialize_thermal_installation(SimpleNamespace(sessions=sessions))
    if fault:
        with pytest.raises(ValueError if fault == "insert" else AssertionError):
            await call
        assert "rollback" in events
        if fault != "committed-readback":
            assert state["row"] is None and "commit" not in events
        else:
            assert events.count("commit") == 1  # post-commit read failure is not hidden
    else:
        await call
        assert len(added) == 1 and events.count("commit") == 2
        row = added[0]
        assert row.singleton_id == 1 and row.installation_id != row.house_id
        assert row.current_revision_id is None
        assert (row.demand_dependency_generation, row.indoor_dependency_generation) == (1, 1)
        assert events.index("flush") < events.index("refresh") < events.index("commit")


def _thermal_frozen_inputs():
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute as route

    inputs = route.native_inputs(route.NOW)
    inputs.update(
        installation_id="synthetic-installation",
        indoor_dependency_generation=1,
        capabilities=["indoor_heating", "indoor_cooling"],
    )
    return inputs


@pytest.mark.parametrize("head", ["indoor_heating", "indoor_cooling"])
def test_thermal_snapshot_real_freezing_scope_and_nested_copy(fixtures, head):
    from packages.ml.forecast_quality import freeze_native_pair, native_scope

    frozen = _thermal_frozen_inputs()
    original = deepcopy(frozen)
    issued = dt.datetime.fromisoformat(frozen["issued_at"]) + dt.timedelta(hours=2)
    snapshot = fixtures._thermal_native_snapshot(
        SimpleNamespace(fitted={"inputs": frozen}), issued, head
    )
    assert "scope" not in snapshot
    assert snapshot["capabilities"] == ["indoor_heating", "indoor_cooling"]
    assert native_scope(snapshot) == native_scope(frozen)

    def predictor(snapshot, horizons):
        return dict.fromkeys(horizons, 21.0)

    pair = freeze_native_pair(
        snapshot,
        epoch="offline",
        component="thermal",
        candidate_hash="candidate",
        incumbent_hash=None,
        horizons=(15, 30, 60),
        candidate_predictor=predictor,
        incumbent_predictor=predictor,
        baseline_predictor=predictor,
    )
    assert pair is not None
    assert json.loads(pair.snapshot_json)["scope"] == native_scope(frozen)
    snapshot["sensor_basis"]["sensor_ids"].append("mutated")
    snapshot["native_settings"]["mutated"] = []
    snapshot["schemas"]["artifact"] = "mutated"
    snapshot["configured_sensor_ids"].append("mutated")
    snapshot["capabilities"].clear()
    snapshot["timer"]["slots"].append({"mutated": True})
    assert frozen == original


@pytest.mark.parametrize(
    "key,value",
    [
        ("installation_id", "other"),
        ("indoor_dependency_generation", 2),
        ("configured_sensor_ids", ["other"]),
        ("reference_sensor_id", "other"),
        ("sensor_basis", {"kind": "other"}),
        ("native_settings", {"other": True}),
        ("schemas", {"artifact": "other"}),
        ("timer", {"status": "unknown"}),
        ("capabilities", []),
        ("capabilities", ["tank_cooling"]),
    ],
)
@pytest.mark.parametrize("remove", [False, True])
def test_thermal_scope_dependency_negative_sensitivity(fixtures, key, value, remove):
    from packages.ml.forecast_quality import native_scope

    original = _thermal_frozen_inputs()
    changed = deepcopy(original)
    if remove:
        changed.pop(key)
    else:
        changed[key] = value
    try:
        scope = native_scope(changed)
    except KeyError:
        pass  # required dependency rejected, never manufactured
    else:
        assert scope != native_scope(original)
    if key == "capabilities":
        with pytest.raises((AssertionError, KeyError)):
            fixtures._thermal_native_snapshot(
                SimpleNamespace(fitted={"inputs": changed}),
                dt.datetime.fromisoformat(original["issued_at"]),
                "indoor_heating",
            )


def _fingerprint_fixture(fixtures):
    raw = {
        "settings": (10, (1, "r", 2, 3), (1, "versions"), "catalog", "fk", "defaults"),
        "sequence:settings_id_seq": (20, "SECRET_CONFIGURATION", (1, True), "dependencies"),
    }
    return raw, fixtures._public_fixture_components(
        raw, {"settings": "content"}, {"settings": {"local_oid": 10, "resolved_oid": 10}}
    )


@pytest.mark.parametrize(
    "object_name,component",
    [
        ("public.settings", "content"),
        ("public.settings", "versions"),
        ("public.settings", "catalog"),
        ("public.settings", "constraints"),
        ("public.settings", "defaults"),
        ("public.settings", "identity"),
        ("public.settings", "bindings"),
        ("public.settings_id_seq", "configuration"),
        ("public.settings_id_seq", "value_state"),
        ("public.settings_id_seq", "dependencies"),
    ],
)
def test_named_public_drift_component_sensitivity(fixtures, capsys, object_name, component):
    _, baseline = _fingerprint_fixture(fixtures)
    changed = deepcopy(baseline)
    changed[object_name][component] = "changed"
    for phase in ("pre-drop", "post-drop"):
        fixtures._emit_public_diagnostic(phase, {"commit": "offline"}, baseline, changed, False)
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["phase"] == phase and receipt["writer"] == "UNKNOWN"
        assert receipt["unchanged"] is False
        (delta,) = receipt["deltas"]
        assert delta["object"] == object_name and delta["change"] == "changed"
        assert delta["components"].keys() == {component}
        assert delta["before_hash"] != delta["after_hash"]


def test_public_diagnostic_add_remove_stability_and_redaction(fixtures, capsys):
    raw, baseline = _fingerprint_fixture(fixtures)
    assert fixtures._public_fixture_delta(baseline, deepcopy(baseline)) == []
    for old, new, classification in (({}, baseline, "added"), (baseline, {}, "removed")):
        fixtures._emit_public_diagnostic("pre-drop", {}, old, new, False)
        output = capsys.readouterr().out
        assert "SECRET_CONFIGURATION" not in output
        assert "row_payload" not in output
        assert {delta["change"] for delta in json.loads(output)["deltas"]} == {classification}
    version_changed = deepcopy(raw)
    relation = list(version_changed["settings"])
    relation[2] = (1, "reverted-write-versions")
    version_changed["settings"] = tuple(relation)
    assert version_changed != raw  # advisory equal content cannot excuse reverted writes
    sequence_changed = deepcopy(raw)
    sequence_changed["sequence:settings_id_seq"] = (20, "SECRET_CONFIGURATION", (2, True), "d")
    assert sequence_changed != raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "query",
        "post-query",
        "serialization",
        "drop",
        "absence",
        "engine",
        "control",
        "receipt",
        "drift",
        "post-drift",
    ],
)
async def test_cleanup_attempts_all_obligations_without_swallowing(
    fixtures, monkeypatch, capsys, fault
):
    events, transactions = [], []
    serial = 0
    source = {"commit": "offline"}
    raw, components = _fingerprint_fixture(fixtures)

    class Control:
        @asynccontextmanager
        async def begin(self):
            nonlocal serial
            serial += 1
            transactions.append(serial)
            yield SimpleNamespace(execute=execute, transaction=serial)

        async def dispose(self):
            events.append("control")
            if fault == "control":
                raise ValueError("SECRET_ERROR")

    async def execute(statement, parameters=None):
        if "DROP SCHEMA" in str(statement):
            events.append("drop")
            if fault == "drop":
                raise ValueError("SECRET_ERROR")
        else:
            events.append("absence")
            if fault == "absence":
                raise ValueError("SECRET_ERROR")
        return SimpleNamespace(scalar_one=lambda: None)

    async def fingerprint(connection, *, diagnostics):
        events.append("fingerprint")
        if fault == "query" or (fault == "post-query" and connection.transaction == 4):
            raise ValueError("SECRET_ERROR")
        diagnostics.update(deepcopy(components))
        if (fault == "drift" and connection.transaction == 1) or (
            fault == "post-drift" and connection.transaction == 4
        ):
            return {}
        return raw

    async def dispose():
        events.append("engine")
        if fault == "engine":
            raise ValueError("SECRET_ERROR")

    def emit(state):
        events.append("receipt")
        assert state["schema_removed"] == (fault != "absence")
        if fault in {"drift", "query", "post-query", "post-drift"}:
            assert state["public_fixture_storage_unchanged"] is False
        if fault == "receipt":
            raise ValueError("SECRET_ERROR")

    monkeypatch.setattr(fixtures, "guard_owned_database", AsyncMock())
    monkeypatch.setattr(fixtures, "retained_storage_state", AsyncMock(return_value="retained"))
    monkeypatch.setattr(fixtures, "_public_fixture_state", fingerprint)
    if fault == "serialization":

        def fail_serialization(*args, **kwargs):
            raise TypeError("SECRET_ERROR")

        monkeypatch.setattr(fixtures, "json", SimpleNamespace(dumps=fail_serialization))
    call = fixtures._cleanup_lifecycle_storage(
        engine=SimpleNamespace(dispose=dispose),
        control=Control(),
        schema="owned",
        created=True,
        retained="retained",
        public_baseline=raw,
        diagnostic_baseline=components,
        source=source,
        emit_receipt=emit,
    )
    if fault:
        with pytest.raises(BaseExceptionGroup) as failure:
            await call
        assert failure.value.exceptions
    else:
        await call
    assert events == [
        "fingerprint",
        "drop",
        "absence",
        "fingerprint",
        "engine",
        "control",
        "receipt",
    ]
    assert transactions == [1, 2, 3, 4]
    assert "SECRET_ERROR" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_g3_observer_preserves_none_identity_and_exception(fixtures, monkeypatch):
    from packages.ml import forecast_quality, thermal

    result = {"sensor_basis": {"kind": "heatpump_temperature"}, "capability": "tank_cooling"}
    native = AsyncMock(side_effect=[None, result, ValueError("original rejection")])
    history = AsyncMock(return_value=None)
    monkeypatch.setattr(forecast_quality, "materialize_native_inputs", native)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", history)
    observations = []
    with fixtures._trace_thermal_materializers(monkeypatch, observations):
        assert await forecast_quality.materialize_native_inputs("thermal") is None
        assert await forecast_quality.materialize_native_inputs("thermal") is result
        with pytest.raises(ValueError, match="original rejection"):
            await forecast_quality.materialize_native_inputs("thermal")
        assert await thermal.materialize_thermal_inputs("device") is None
    assert forecast_quality.materialize_native_inputs is native
    assert thermal.materialize_thermal_inputs is history
    assert observations[-2]["exception"] == "ValueError"
    assert observations[-1] == {"call": "thermal_history", "result": "None"}


@pytest.mark.asyncio
@pytest.mark.parametrize("history_error", [False, True])
async def test_g3_differential_is_prerequisite_only_and_rolls_back(
    fixtures, monkeypatch, capsys, history_error
):
    from packages.ml import forecast_quality, thermal

    state = {"present": False}
    rolled_back = []
    additions = []
    inputs = {
        "device_id": "audit-device",
        "reference_sensor_id": "selected-room",
        "weather_provider": "open-meteo",
        "configured_sensor_ids": ["selected-room"],
        "sensor_basis": {"kind": "indoor"},
        "capability": "indoor_heating",
    }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return None

        async def execute(self, *args):
            return SimpleNamespace(one=lambda: (10, 10))

        def add(self, row):
            assert isinstance(row, fixtures.HouseInstallationRecord)
            additions.append(row)
            state["present"] = True

        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    class Connection:
        async def begin(self):
            state["present"] = False
            return self

        async def rollback(self):
            rolled_back.append(state["present"])
            state["present"] = False

    @asynccontextmanager
    async def connect():
        yield Connection()

    async def materialize(component, **kwargs):
        assert component == "thermal"
        assert kwargs["now"] is clock
        return inputs if state["present"] else None

    clock = object()
    original_native = AsyncMock(side_effect=materialize)
    original_history = AsyncMock(
        side_effect=ValueError("second boundary") if history_error else None,
        return_value=None,
    )
    monkeypatch.setattr(forecast_quality, "materialize_native_inputs", original_native)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", original_history)
    monkeypatch.setattr(fixtures, "async_sessionmaker", lambda *args, **kwargs: Session)
    database = SimpleNamespace(
        sessions=sessions, schema="owned", engine=SimpleNamespace(connect=connect)
    )
    assert await fixtures._thermal_prerequisite_probe(database, monkeypatch, clock) is None
    assert rolled_back == [False, True] and not state["present"]
    assert len(additions) == 1 and additions[0].current_revision_id is None
    assert original_native.await_count == 4 and original_history.await_count == 1
    assert forecast_quality.materialize_native_inputs is original_native
    assert thermal.materialize_thermal_inputs is original_history
    output = capsys.readouterr().out
    assert output.count('"rolled_back": true') == 2
    if history_error:
        assert '"probe_exception": "ValueError"' in output
    # The diagnostic helper contains no settings/contract initialization or model work.
    text = ast.unparse(_function("_thermal_prerequisite_probe"))
    for forbidden in (
        "set_settings_bulk",
        "ensure_current_contract",
        ".fit(",
        ".publish(",
        "qualify",
    ):
        assert forbidden not in text


def test_all_artifact_callers_await_and_fault_setup_is_opt_in():
    for path in (SOURCE, SOURCE.with_name("test_ml_lifecycle_verification.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_artifact"
            ):
                assert isinstance(parents[node], ast.Await), (path, node.lineno)
    for name in (
        "test_reset_and_status_never_initialize_empty_authority",
        "test_maintenance_off_admin_reset_only_revokes_existing_state",
    ):
        source = SOURCE.with_name("test_ml_lifecycle_verification.py").read_text()
        text = ast.unparse(_function(name, source))
        if name.startswith("test_reset"):
            assert "_owner(" not in text and "_artifact(" not in text
        else:
            branch = next(node for node in _function(name, source).body if isinstance(node, ast.If))
            assert "demand=True" in ast.unparse(branch)


@pytest.mark.asyncio
@pytest.mark.parametrize("demand", [False, True])
async def test_owner_prepares_demand_only_when_requested_and_before_authority(
    fixtures, monkeypatch, demand
):
    events = []
    lease = object()

    async def prepare(database):
        events.append("prepare")

    async def initialize():
        events.append("initialize")

    async def enable(enabled):
        assert enabled is True
        events.append("enable")

    async def acquire(worker):
        assert worker.startswith("worker-")
        events.append("acquire")
        return lease

    monkeypatch.setattr(fixtures, "_prepare_demand_fixture", prepare)
    database = SimpleNamespace(
        lifecycle=SimpleNamespace(
            initialize=initialize, set_maintenance_enabled=enable, acquire=acquire
        )
    )
    assert await fixtures._owner(database, demand=demand) is lease
    assert events == (["prepare"] if demand else []) + ["initialize", "enable", "acquire"]


@pytest.mark.asyncio
async def test_g3_history_cancellation_is_not_swallowed(fixtures, monkeypatch):
    from packages.ml import thermal

    cancellation = asyncio.CancelledError("offline cancellation")
    history = AsyncMock(side_effect=cancellation)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", history)
    observations = []
    with pytest.raises(asyncio.CancelledError) as caught:
        with fixtures._trace_thermal_materializers(monkeypatch, observations):
            await thermal.materialize_thermal_inputs("fixture-device")
    assert caught.value is cancellation
    assert thermal.materialize_thermal_inputs is history
    assert observations == [{"call": "thermal_history", "exception": "CancelledError"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["row_version", "sequence"])
async def test_public_state_observes_fresh_row_and_sequence_drift_without_writes(fixtures, drift):
    state = {"row_version": "before", "sequence": 7}
    statements = []

    async def execute(statement, parameters=None):
        sql = str(statement)
        statements.append(sql)
        if "SELECT to_regclass" in sql:
            return SimpleNamespace(scalar_one=lambda: 10)
        if "SELECT count(*)" in sql:
            return SimpleNamespace(one=lambda: (1, state["row_version"], "content"))
        if "SELECT DISTINCT n.nspname" in sql:
            return SimpleNamespace(all=lambda: [("owned", "related", 42)])
        if "SELECT relnamespace" in sql:
            return SimpleNamespace(one=lambda: (2200, "r", 1, 10))
        if "JOIN pg_sequence" in sql:
            return SimpleNamespace(all=lambda: [(20, "settings_id_seq", "configuration")])
        if "SELECT last_value,is_called" in sql:
            return SimpleNamespace(one=lambda: (state["sequence"], True))
        return SimpleNamespace(scalar_one=lambda: "catalog-digest")

    connection = SimpleNamespace(
        execute=execute,
        dialect=SimpleNamespace(
            identifier_preparer=SimpleNamespace(quote_identifier=lambda name: f'"{name}"')
        ),
    )
    before = await fixtures._public_fixture_state(connection)
    assert {model.__tablename__ for model in fixtures.FIXTURE_MODELS} <= before.keys()
    state[drift] = "after" if drift == "row_version" else 8
    after = await fixtures._public_fixture_state(connection)
    assert before != after
    key = "sequence:settings_id_seq" if drift == "sequence" else "settings"
    assert before[key] != after[key]
    assert all(sql.lstrip().startswith("SELECT ") for sql in statements)
    assert not any("nextval(" in sql.lower() for sql in statements)

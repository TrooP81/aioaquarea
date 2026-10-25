"""Offline S5 ownership and bounded best-effort logging regressions."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("service", ["api", "poller"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_R23_startup_failure_disposes_engine_after_owned_drain(monkeypatch, service, failure):
    import importlib
    from packages.core import database

    module = importlib.import_module(f"packages.{service}.main")
    calls = []

    def record(name):
        calls.append(name)

    async def fail(*args):
        calls.append("startup")
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("startup failed")

    wrapper = SimpleNamespace(
        start=fail,
        stop=AsyncMock(side_effect=lambda: record("wrapper")),
    )
    monkeypatch.setattr(module, "AquareaWrapper", lambda **kwargs: wrapper)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dispose=AsyncMock(side_effect=lambda: record("engine")))
    )
    monkeypatch.setattr(
        "packages.core.log_sink.shutdown_log_sink",
        AsyncMock(side_effect=lambda: record("logs")),
    )
    if service == "poller":
        monkeypatch.setattr(module, "_run_service", fail)
        monkeypatch.setattr(
            module,
            "create_scheduler",
            lambda: SimpleNamespace(close=AsyncMock(side_effect=lambda: record("scheduler"))),
        )
        monkeypatch.setattr(
            "packages.ml.lifecycle.MaintenanceRunner",
            lambda **kwargs: SimpleNamespace(
                close=AsyncMock(side_effect=lambda: record("maintenance"))
            ),
        )
    else:
        monkeypatch.setattr(module.settings, "panasonic_distributed_read_quota_enabled", True)
        # API ordinary startup errors are deliberately nonfatal; cancellation is not.
        if failure == "error":
            async with module.lifespan(SimpleNamespace(state=SimpleNamespace())):
                calls.append("serving")
            assert calls == ["startup", "wrapper", "serving", "logs", "engine"]
            return

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        if service == "api":
            async with module.lifespan(SimpleNamespace(state=SimpleNamespace())):
                pytest.fail("cancelled startup must not serve")
        else:
            await module.main()
    assert calls == [
        "startup",
        *(["scheduler", "maintenance"] if service == "poller" else []),
        "logs",
        "wrapper",
        "engine",
    ]


async def test_R23_cleanup_repeated_cancellation_keeps_resource_ownership():
    from packages.core.runtime_ownership import finish_owned_cleanup

    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def cleanup():
        started.set()
        await release.wait()
        calls.append("disposed")

    task = asyncio.create_task(finish_owned_cleanup(cleanup))
    await started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert calls == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["disposed"]


@pytest.mark.parametrize("failed_owner", [None, "scheduler", "maintenance", "logs", "wrapper"])
async def test_R23_engine_disposal_requires_all_owners_to_settle(monkeypatch, failed_owner):
    from packages.core.runtime_ownership import close_service_resources

    calls = []

    def close(name):
        async def run():
            calls.append(name)
            if name == failed_owner:
                raise RuntimeError("unsettled owner")

        return run

    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", close("logs"))
    engine = SimpleNamespace(dispose=AsyncMock(side_effect=close("engine")))
    options = {
        "scheduler": SimpleNamespace(close=close("scheduler")),
        "maintenance": SimpleNamespace(close=close("maintenance")),
        "wrapper": SimpleNamespace(stop=close("wrapper")),
        "engine": engine,
    }
    if failed_owner is None:
        await close_service_resources(**options)
        assert calls == ["scheduler", "maintenance", "logs", "wrapper", "engine"]
        engine.dispose.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="unsettled owner"):
            await close_service_resources(**options)
        expected = ["scheduler", "maintenance", "logs", "wrapper"]
        assert calls == expected[: expected.index(failed_owner) + 1]
        engine.dispose.assert_not_awaited()


async def test_R23_successful_API_lifespan_disposes_after_serving(monkeypatch):
    from packages.api import main
    from packages.core import database

    calls = []

    async def stop():
        calls.append("wrapper")

    async def logs():
        calls.append("logs")

    async def dispose():
        calls.append("engine")

    monkeypatch.setattr(main.settings, "panasonic_distributed_read_quota_enabled", True)
    wrapper = SimpleNamespace(start=AsyncMock(), stop=stop)
    monkeypatch.setattr(main, "AquareaWrapper", lambda **kwargs: wrapper)
    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", logs)
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    application = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(application):
        assert application.state.aquarea_wrapper is wrapper
        assert calls == []
    assert calls == ["logs", "wrapper", "engine"]
    assert not hasattr(application.state, "aquarea_wrapper")


def _cooldown_harness(monkeypatch, *, failure=None):
    """Isolate publication ownership; S4's physical/input validators are separate gates."""
    from packages.core import house_contract
    from packages.optimizer import main
    from packages.optimizer.executor_core import LearningModeState
    from packages.core.comfort_schedule import TIMEZONE_POLICY, timezone_data_version

    sentinel = -1_000_000_000.0
    contract_json = {
        "schema_version": 2,
        "installation_id": "00000000-0000-0000-0000-000000000001",
        "house_id": "00000000-0000-0000-0000-000000000002",
        "timezone": "Europe/Stockholm",
        "comfort_schedule": {"weekday": [], "weekend": []},
        "comfort_temp_min": 18.0,
        "comfort_temp_target": 20.0,
        "comfort_temp_max": 25.0,
        "timezone_policy": TIMEZONE_POLICY,
        "tzdata_version": timezone_data_version(),
    }
    revision = SimpleNamespace(
        id=7,
        contract_hash="a" * 64,
        contract_json=contract_json,
        effective_at=dt.datetime.now(dt.timezone.utc),
        source="unit_test",
    )
    monkeypatch.setattr(main, "_last_plan_generated_at", sentinel)
    monkeypatch.setattr(main, "_optimization_lock", asyncio.Lock())
    monkeypatch.setattr(
        main,
        "_freeze_contract_revision",
        AsyncMock(return_value=(revision.id, revision.contract_hash)),
    )
    monkeypatch.setattr(house_contract, "ensure_current_contract", AsyncMock(return_value=revision))
    monkeypatch.setattr(
        main, "resolve_learning_mode_state", AsyncMock(return_value=LearningModeState.ACTIVE)
    )
    monkeypatch.setattr(main, "get_setting", AsyncMock(return_value="rules_only"))
    monkeypatch.setattr(main._demand_model, "arefresh_committed", AsyncMock())
    monkeypatch.setattr(main.comfort_model, "arefresh_if_changed", AsyncMock())
    monkeypatch.setattr(main, "get_device_data_quality", AsyncMock(return_value={"ready": True}))
    now = dt.datetime.now(dt.timezone.utc)
    generate = AsyncMock(
        return_value={
            "horizon_start": now,
            "horizon_end": now + dt.timedelta(hours=1),
            "version": "s5-cooldown-fixture",
            "actions": [],
        }
    )
    monkeypatch.setattr(
        main,
        "_select_optimizer",
        AsyncMock(return_value=("rules", SimpleNamespace(generate_plan=generate))),
    )
    monkeypatch.setattr(
        "packages.optimizer.represented_strategy.validate_represented_plan", lambda plan: None
    )
    monkeypatch.setattr(
        main,
        "_validated_planning_quality",
        lambda *args, **kwargs: {"price": {"area": "SE3", "source": "manual", "currency": "SEK"}},
    )
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    records = []

    def add(record):
        record.id = 123
        records.append(record)

    async def flush():
        events.append(("flush", main._last_plan_generated_at))
        if failure == "flush":
            raise RuntimeError("flush failed")

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(
            add=add,
            flush=flush,
            get=AsyncMock(return_value=revision),
        )
        events.append(("commit_enter", main._last_plan_generated_at))
        entered.set()
        await release.wait()
        if failure == "commit":
            raise RuntimeError("commit failed")
        events.append(("committed", main._last_plan_generated_at))

    monkeypatch.setattr(main, "get_session", session)
    return SimpleNamespace(
        main=main,
        entered=entered,
        release=release,
        generate=generate,
        sentinel=sentinel,
        events=events,
        records=records,
    )


async def test_R30_scheduled_waiter_rechecks_cooldown_after_successful_commit(monkeypatch):
    harness = _cooldown_harness(monkeypatch)
    first = asyncio.create_task(harness.main.run_optimization(scheduled=True))
    await harness.entered.wait()
    second = asyncio.create_task(harness.main.run_optimization(scheduled=True))
    try:
        await asyncio.sleep(0)
        assert not second.done()
        assert harness.main._last_plan_generated_at == harness.sentinel
        harness.generate.assert_awaited_once()
    finally:
        harness.release.set()
        results = await asyncio.gather(first, second)
    assert results == [123, None]
    assert harness.events == [
        ("flush", harness.sentinel),
        ("commit_enter", harness.sentinel),
        ("committed", harness.sentinel),
    ]
    assert harness.main._last_plan_generated_at > harness.sentinel
    assert len(harness.records) == 1
    assert harness.records[0].status == "diagnostic"
    harness.generate.assert_awaited_once()


@pytest.mark.parametrize("failure", ["flush", "commit", "cancel"])
async def test_R30_unsuccessful_publication_never_consumes_cooldown(monkeypatch, failure):
    harness = _cooldown_harness(monkeypatch, failure=failure)
    task = asyncio.create_task(harness.main.run_optimization(scheduled=True))
    if failure == "cancel":
        await harness.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        harness.release.set()
        assert await task is None
    assert harness.main._last_plan_generated_at == harness.sentinel
    assert all(event[0] != "committed" for event in harness.events)


@pytest.fixture
def isolated_sink(monkeypatch):
    from packages.core import log_sink

    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_BUFFER_LOCK", asyncio.Lock())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    monkeypatch.setattr(log_sink, "_FLUSH_TASK", None)
    monkeypatch.setattr(log_sink, "_INFLIGHT_TASK", None)
    monkeypatch.setattr(log_sink, "_INFLIGHT_BATCH", [])
    return log_sink


def test_R35_encoded_row_limit_is_bytes_and_admission_never_waits(isolated_sink):
    import json

    sink = isolated_sink
    assert sink._MAX_OWNED_ROWS == 1000
    assert sink._BATCH_SIZE == 100
    assert sink._MAX_ENCODED_ROW_BYTES == 8192
    assert sink._SHUTDOWN_TIMEOUT_SECONDS == 5
    event = {"event": "encoded", "text": "x"}
    sink._db_log_processor(None, "info", event)
    base = len(json.dumps(sink._LOG_BUFFER.pop().row, ensure_ascii=False).encode("utf-8")) - 1
    sink._db_log_processor(None, "info", {"event": "encoded", "text": "x" * (8192 - base)})
    assert len(sink._LOG_BUFFER) == 1
    assert len(json.dumps(sink._LOG_BUFFER[0].row, ensure_ascii=False).encode("utf-8")) == 8192
    sink._db_log_processor(None, "info", {"event": "encoded", "text": "x" * (8193 - base)})
    sink._db_log_processor(None, "info", {"event": "encoded", "text": "🙂" * 1000})
    assert len(sink._LOG_BUFFER) == 1
    assert sink.log_sink_status()["drops"] == {"oversize": 2}
    sink._ADMISSION_LOCK.acquire()
    try:
        assert sink._db_log_processor(None, "info", event) is event
    finally:
        sink._ADMISSION_LOCK.release()
    assert sink.log_sink_status()["drops"]["saturation"] == 1


async def test_R35_flush_acknowledges_only_after_transaction_exit(monkeypatch, isolated_sink):
    sink = isolated_sink
    entered, release = asyncio.Event(), asyncio.Event()

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(add=lambda row: None, execute=AsyncMock())

        async def __aexit__(self, *args):
            entered.set()
            await release.wait()

    monkeypatch.setattr("packages.core.database.get_session", Context)
    sink._db_log_processor(None, "info", {"event": "pending commit"})
    task = asyncio.create_task(sink._flush_once())
    await entered.wait()
    assert sink.log_sink_status()["owned_rows"] == 1
    assert sink.log_sink_status()["inflight_rows"] == 1
    release.set()
    await task
    assert sink.log_sink_status()["owned_rows"] == 0
    assert sink.log_sink_status()["inflight_rows"] == 0


async def test_R35_shutdown_unsettled_writer_retains_ownership(monkeypatch, isolated_sink):
    sink = isolated_sink
    started, release = asyncio.Event(), asyncio.Event()

    async def resistant_write(batch):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(sink, "_write_batch", resistant_write)
    monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", 0.03)
    sink._db_log_processor(None, "info", {"event": "unsettled"})
    await sink._flush_once(timeout=0)
    await started.wait()
    try:
        with pytest.raises(RuntimeError, match="resource disposal forbidden"):
            await asyncio.wait_for(sink.shutdown_log_sink(), timeout=1)
        assert sink.log_sink_status()["owned_rows"] == 1
        assert sink.log_sink_status()["drops"].get("shutdown", 0) == 0
        assert sink.log_sink_status()["accepting"] is False
    finally:
        task = sink._INFLIGHT_TASK
        release.set()
        await task
        sink._settle_inflight()
    assert sink.log_sink_status()["owned_rows"] == 0

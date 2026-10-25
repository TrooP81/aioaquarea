"""Offline ownership contracts. Every negative case has a separate sink generation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import structlog


def load_sink(monkeypatch):
    """Fresh module, not a reset of a live production owner."""
    from packages.core import log_sink

    name = "packages.core._logging_contract_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, log_sink.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    saved = structlog.get_config().copy()
    return module, saved


@asynccontextmanager
async def successful_session():
    yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())


@pytest.fixture
def sink(monkeypatch):
    module, saved = load_sink(monkeypatch)
    yield module
    structlog.configure(**saved)


def emit(sink, event="synthetic"):
    sink._db_log_processor(None, "info", {"event": event})


async def test_explicit_factory_retained_no_fallback_and_commit_ack(sink, monkeypatch):
    calls = []

    @asynccontextmanager
    async def factory():
        calls.append("enter")
        yield SimpleNamespace(add=lambda row: calls.append("add"), execute=AsyncMock())
        calls.append("commit")

    monkeypatch.setattr(
        "packages.core.database.get_session",
        lambda: pytest.fail("explicit binding must not fall back"),
    )
    lease = sink.bind_log_session_factory(factory)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await sink.shutdown_log_sink()
    assert calls == ["enter", "add", "commit"]
    assert sink.log_sink_status()["counts"]["acknowledged"] == 1
    lease.release()
    assert not sink.log_sink_status()["accepting"]
    with pytest.raises(RuntimeError, match="fresh explicit binding"):
        sink.configure_structlog_with_db("unit")


async def test_binding_conflicts_startup_and_explicit_restart(sink):
    lease = sink.bind_log_session_factory(successful_session)
    with pytest.raises(RuntimeError):
        sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    first = sink._FLUSH_TASK
    generation = sink.log_sink_status()["generation"]
    sink.configure_structlog_with_db("unit")
    assert sink._FLUSH_TASK is first
    with pytest.raises(RuntimeError):
        sink.configure_structlog_with_db("other")
    with pytest.raises(RuntimeError):
        lease.release()
    await sink.shutdown_log_sink()
    with pytest.raises(RuntimeError):
        sink.configure_structlog_with_db("unit")
    lease.release()
    second = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    assert sink.log_sink_status()["generation"] > generation
    assert sink._FLUSH_TASK is not first
    await sink.shutdown_log_sink()
    second.release()


def test_import_preloop_then_lifespan_default_restart(sink, monkeypatch):
    monkeypatch.setattr("packages.core.database.get_session", successful_session)
    sink.configure_structlog_with_db("api")
    assert sink.log_sink_status()["state"] == "PRELOOP"
    emit(sink)
    with pytest.raises(RuntimeError):
        sink.bind_log_session_factory(successful_session)

    async def lifespan():
        sink.configure_structlog_with_db("api")
        task = sink._FLUSH_TASK
        sink.configure_structlog_with_db("api")
        assert task is sink._FLUSH_TASK
        await sink.shutdown_log_sink()

    asyncio.run(lifespan())
    generation = sink.log_sink_status()["generation"]
    asyncio.run(lifespan())
    assert sink.log_sink_status()["generation"] == generation + 1
    assert sink.log_sink_status()["counts"]["admitted"] == 1
    assert sink.log_sink_status()["counts"]["acknowledged"] == 1


async def test_shared_controller_repeated_cancellation_and_configure_race(sink):
    started, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def factory():
        started.set()
        await release.wait()
        yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())

    lease = sink.bind_log_session_factory(factory)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    first = asyncio.create_task(sink.shutdown_log_sink())
    await started.wait()
    controller = sink._OWNER.controller
    deadline = sink._OWNER.deadline
    second = asyncio.create_task(sink.shutdown_log_sink())
    for _ in range(3):
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
    with pytest.raises(RuntimeError):
        sink.configure_structlog_with_db("unit")
    assert sink._OWNER.controller is controller
    assert sink._OWNER.deadline == deadline
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await sink.shutdown_log_sink()
    counts = sink.log_sink_status()["counts"]
    assert counts["caller_cancellations"] == 3
    assert counts["shutdown_outcomes"] == 1
    assert counts["admitted"] == counts["acknowledged"] == 1
    lease.release()


@pytest.mark.parametrize("budget", [0.03, 5])
async def test_resistant_flusher_holding_lock_and_late_completion_refuses_restart(
    sink, monkeypatch, budget
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resistant():
        async with sink._BUFFER_LOCK:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass

    monkeypatch.setattr(sink, "_flush_loop", resistant)
    if budget == 5:
        assert sink._SHUTDOWN_TIMEOUT_SECONDS == 5
    else:
        monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", budget)
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="flusher-live.*resource disposal forbidden"):
            await asyncio.wait_for(sink.shutdown_log_sink(), budget + 0.5)
        controller = sink._OWNER.controller
        assert sink._BUFFER_LOCK.locked()
        for operation in (
            lambda: sink.configure_structlog_with_db("unit"),
            lambda: sink.bind_log_session_factory(successful_session),
            lease.release,
        ):
            with pytest.raises(RuntimeError):
                operation()
        with pytest.raises(RuntimeError):
            await sink.shutdown_log_sink()
        assert sink._OWNER.controller is controller
        from packages import core
        from packages.core.runtime_ownership import close_service_resources

        engine = SimpleNamespace(dispose=AsyncMock())
        with monkeypatch.context() as patch:
            patch.setattr(core, "log_sink", sink)
            patch.setitem(sys.modules, "packages.core.log_sink", sink)
            with pytest.raises(RuntimeError, match="resource disposal forbidden"):
                await close_service_resources(engine=engine)
        engine.dispose.assert_not_awaited()
    finally:
        release.set()
        await sink._FLUSH_TASK
    assert sink.log_sink_status()["state"] == "FAILED_UNSETTLED"
    with pytest.raises(RuntimeError):
        sink.configure_structlog_with_db("unit")
    counts = sink.log_sink_status()["counts"]
    assert counts["unsettled_shutdowns"] == counts["shutdown_expirations"] == 1
    assert counts["shutdown_outcomes"] == 1


@pytest.mark.parametrize("queued", [False, True])
async def test_unavailable_or_queued_lock_never_enqueues_shutdown_waiter(sink, queued):
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    # Terminal flusher: isolate the drain lock precondition.
    sink._FLUSH_TASK.cancel()
    await asyncio.gather(sink._FLUSH_TASK, return_exceptions=True)
    await sink._BUFFER_LOCK.acquire()
    waiter = None
    if queued:
        waiter = asyncio.create_task(sink._BUFFER_LOCK.acquire())
        await asyncio.sleep(0)
        sink._BUFFER_LOCK.release()
        assert not sink._BUFFER_LOCK.locked()
        assert not sink._lock_available(sink._BUFFER_LOCK)
    try:
        # Execute controller inline so the queued waiter cannot resume first.
        owner = sink._OWNER
        owner.state = "SHUTTING_DOWN"
        owner.deadline = sink.time.monotonic() + 5
        await sink._shutdown_controller(owner)
        assert owner.state == "FAILED_UNSETTLED"
        assert owner.failure == "drain-lock-unavailable"
        assert len(sink._BUFFER_LOCK._waiters or ()) == (1 if queued else 0)
        with pytest.raises(RuntimeError):
            lease.release()
    finally:
        if waiter:
            await waiter
        sink._BUFFER_LOCK.release()


async def test_generation_reservation_thread_encoding_shutdown_and_late_callback(sink, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = sink._encode_row

    def encode(*args):
        entered.set()
        release.wait(2)
        return original(*args)

    monkeypatch.setattr(sink, "_encode_row", encode)
    monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", 0.03)
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    worker = threading.Thread(target=emit, args=(sink,))
    worker.start()
    assert await asyncio.to_thread(entered.wait, 1)
    with pytest.raises(RuntimeError, match="encoding-live"):
        await sink.shutdown_log_sink()
    assert sink.log_sink_status()["encoding_rows"] == 1
    release.set()
    await asyncio.to_thread(worker.join, 1)
    assert not worker.is_alive()
    assert sink.log_sink_status()["owned_rows"] == 0
    assert sink.log_sink_status()["drops"]["shutdown"] == 1
    assert sink.log_sink_status()["counts"].get("admitted", 0) == 0
    with pytest.raises(RuntimeError):
        lease.release()


async def test_partial_setup_and_task_creation_failure(sink, monkeypatch):
    lease = sink.bind_log_session_factory(successful_session)
    loop = asyncio.get_running_loop()
    original = loop.create_task

    def fail(*args, **kwargs):
        raise RuntimeError("task creation refused")

    with monkeypatch.context() as patch:
        patch.setattr(loop, "create_task", fail)
        with pytest.raises(RuntimeError, match="task creation refused"):
            sink.configure_structlog_with_db("unit")
    assert sink._FLUSH_TASK is None
    await sink.shutdown_log_sink()
    assert sink.log_sink_status()["counts"]["task_creation_failures"] == 1
    assert loop.create_task == original
    lease.release()


async def test_unused_binding_release_and_foreign_lifecycle_refusal(sink):
    lease = sink.bind_log_session_factory(successful_session)
    lease.release()
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    before = sink.log_sink_status()

    def foreign():
        with pytest.raises(RuntimeError, match="another thread"):
            sink.configure_structlog_with_db("unit")
        with pytest.raises(RuntimeError, match="another thread"):
            asyncio.run(sink.shutdown_log_sink())

    await asyncio.to_thread(foreign)
    assert sink.log_sink_status() == before
    await sink.shutdown_log_sink()
    lease.release()


@pytest.mark.parametrize("kind", ["writer", "controller"])
async def test_task_creation_failure_has_no_orphan(sink, monkeypatch, kind):
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    loop = asyncio.get_running_loop()

    def fail(coroutine, **kwargs):
        raise RuntimeError("task refused")

    with monkeypatch.context() as patch:
        patch.setattr(loop, "create_task", fail)
        with pytest.raises(RuntimeError, match="task refused"):
            if kind == "writer":
                await sink._flush_once()
            else:
                await sink.shutdown_log_sink()
    if kind == "writer":
        assert sink._INFLIGHT_TASK is None
        assert sink._INFLIGHT_BATCH == []
        await sink.shutdown_log_sink()
        counts = sink.log_sink_status()["counts"]
        assert counts["batch_failures"] == counts["retried_rows"] == 1
        assert counts["acknowledged"] == 1
        lease.release()
    else:
        assert sink._OWNER.controller is None
        assert sink.log_sink_status()["state"] == "FAILED_UNSETTLED"
        with pytest.raises(RuntimeError):
            await sink.shutdown_log_sink()
        with pytest.raises(RuntimeError):
            lease.release()
        sink._FLUSH_TASK.cancel()
        await asyncio.gather(sink._FLUSH_TASK, return_exceptions=True)


async def test_unexpected_controller_cancellation_before_first_turn_is_fenced(sink):
    sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    waiter = asyncio.create_task(sink.shutdown_log_sink())
    await asyncio.sleep(0)
    controller = sink._OWNER.controller
    controller.cancel()
    with pytest.raises(RuntimeError, match="controller-CancelledError"):
        await waiter
    assert sink.log_sink_status()["counts"]["shutdown_outcomes"] == 1
    assert sink.log_sink_status()["counts"]["unsettled_shutdowns"] == 1
    sink._FLUSH_TASK.cancel()
    await asyncio.gather(sink._FLUSH_TASK, return_exceptions=True)


async def test_task_factory_reentry_cannot_replace_reserved_flusher(sink):
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()
    seen = []

    def factory(loop, coroutine, **kwargs):
        emit(sink, "task-factory-producer")
        with pytest.raises(RuntimeError, match="task creation"):
            sink.configure_structlog_with_db("unit")
        seen.append(True)
        return asyncio.Task(coroutine, loop=loop, **kwargs)

    sink.bind_log_session_factory(successful_session)
    loop.set_task_factory(factory)
    try:
        sink.configure_structlog_with_db("unit")
    finally:
        loop.set_task_factory(original)
    assert seen == [True]
    assert sink.log_sink_status()["encoding_rows"] == 0
    await sink.shutdown_log_sink()


async def test_failures_retries_and_loss_are_not_green(sink, monkeypatch):
    @asynccontextmanager
    async def failed():
        yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())
        raise RuntimeError("commit refused")

    monkeypatch.setattr(
        "packages.core.database.get_session", lambda: pytest.fail("must not fall back")
    )
    lease = sink.bind_log_session_factory(failed)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await sink._flush_once()
    assert sink.log_sink_status()["owned_rows"] == 1
    await sink._flush_once()
    await sink.shutdown_log_sink()
    counts = sink.log_sink_status()["counts"]
    assert counts["batch_failures"] == 3
    assert counts["retried_rows"] == 2
    assert counts["admitted"] == counts["admitted_loss"] == 1
    assert counts.get("acknowledged", 0) == 0
    lease.release()


async def test_writer_timeout_cancel_one_time_and_late_ack(sink, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resistant(batch):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    monkeypatch.setattr(sink, "_write_batch", resistant)
    monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", 0.03)
    sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await sink._flush_once(timeout=0.01)
    await entered.wait()
    await sink._flush_once(timeout=0)
    with pytest.raises(RuntimeError):
        await sink.shutdown_log_sink()
    release.set()
    await sink._INFLIGHT_TASK
    sink._settle_inflight()
    sink._settle_inflight()
    counts = sink.log_sink_status()["counts"]
    assert counts["attempt_timeouts"] == counts["writer_cancel_requests"] == 1
    assert counts["acknowledged"] == 1
    assert counts.get("retried_rows", 0) == 0
    assert sink.log_sink_status()["state"] == "FAILED_UNSETTLED"


async def test_terminal_writer_cancellation_is_not_failure_or_acknowledgement(sink):
    @asynccontextmanager
    async def cancelled():
        raise asyncio.CancelledError
        yield

    lease = sink.bind_log_session_factory(cancelled)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await sink._flush_once()
    assert sink.log_sink_status()["counts"]["writer_cancellations"] == 1
    assert sink.log_sink_status()["owned_rows"] == 1
    await sink.shutdown_log_sink()
    counts = sink.log_sink_status()["counts"]
    assert counts["writer_cancellations"] == 2
    assert counts["retried_rows"] == counts["admitted_loss"] == counts["admitted"] == 1
    assert counts.get("batch_failures", 0) == counts.get("acknowledged", 0) == 0
    lease.release()


async def test_between_fixture_rejection_cannot_be_hidden_by_new_binding(sink):
    from tests.e2e.test_ml_lifecycle_postgresql import _assert_logging_accounting

    for index in range(2):
        lease = sink.bind_log_session_factory(successful_session)
        sink.configure_structlog_with_db("unit")
        emit(sink)
        await sink.shutdown_log_sink()
        lease.release()
        if index == 0:
            _assert_logging_accounting(sink.log_sink_status(), complete=True)
            emit(sink, "cached-producer-between-fixtures")
    counts = sink.log_sink_status()["counts"]
    assert counts["admitted"] == counts["acknowledged"] == 2
    assert counts["rejected"] == 1
    with pytest.raises(AssertionError, match="rejected or lost"):
        _assert_logging_accounting(sink.log_sink_status(), complete=True)


async def test_settled_expiration_accounts_queued_loss_once(sink, monkeypatch):
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    await asyncio.sleep(0)
    emit(sink)
    monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", 0)
    await sink.shutdown_log_sink()
    await sink.shutdown_log_sink()
    status = sink.log_sink_status()
    assert status["state"] == "SETTLED"
    assert status["counts"]["shutdown_expirations"] == status["counts"]["shutdown_outcomes"] == 1
    assert status["counts"]["admitted"] == status["counts"]["admitted_loss"] == 1
    assert status["owned_rows"] == 0
    lease.release()


async def test_quiescent_cleanup_attempts_independent_obligations_before_restoration(
    sink, monkeypatch
):
    from packages import core
    from packages.core.runtime_ownership import finish_owned_cleanup
    from tests.e2e.test_ml_lifecycle_postgresql import _finish_logging_fixture

    monkeypatch.setattr(core, "log_sink", sink)
    saved = structlog.get_config().copy()
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    calls = []

    async def verify():
        assert sink.log_sink_status()["explicit_binding"]
        calls.append("verify")
        raise AssertionError("readback failed")

    async def storage():
        assert sink.log_sink_status()["explicit_binding"]
        calls.append("storage")
        raise RuntimeError("receipt failed")

    async def cleanup():
        await _finish_logging_fixture(
            lease=lease,
            child_identity=None,
            verify_logging=verify,
            cleanup_storage=storage,
            saved_logging=saved,
        )

    with pytest.raises(BaseExceptionGroup) as errors:
        await finish_owned_cleanup(cleanup)
    assert len(errors.value.exceptions) == 2
    assert calls == ["verify", "storage"]
    assert not sink.log_sink_status()["explicit_binding"]
    assert not sink.log_sink_status()["accepting"]
    assert structlog.get_config() == saved


def test_unproven_child_refuses_before_binding_or_fatal_exit(tmp_path, monkeypatch):
    from tests.e2e.test_ml_lifecycle_postgresql import _logging_child_identity

    phase = tmp_path / "evidence" / "negative-child"
    phase.mkdir(parents=True)
    monkeypatch.setenv("COP_MANUAL_TEST_PHASE", "negative-child")
    monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(phase.parent))
    (phase / "process.json").write_text(json.dumps({"pid": os.getpid() + 1, "pgid": 0}))
    with pytest.raises(AssertionError):
        _logging_child_identity({"temp_root": str(tmp_path)}, {})


@pytest.mark.parametrize("where", ["setup", "body", "success", "cancel"])
def test_real_pytest_finalizer_ordering(tmp_path, where):
    """Real dependency unwinding, not manual calls pretending to be pytest."""
    source = """
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from packages.core import log_sink as sink
from packages.core.runtime_ownership import finish_owned_cleanup
from tests.e2e.test_ml_lifecycle_postgresql import _finish_logging_fixture
import structlog

events = []
@pytest.fixture
def network():
    events.append("network-enter")
    yield
    assert events[-1] == "patch-undo"
    events.append("network-exit")

@pytest.fixture
def patch(network):
    events.append("patch")
    yield
    assert events[-1] == "dispose"
    assert sink.log_sink_status()["explicit_binding"] is False
    assert sink.log_sink_status()["accepting"] is False
    events.append("patch-undo")

@pytest_asyncio.fixture
async def owner(patch):
    saved = structlog.get_config().copy()
    release = asyncio.Event()
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())
        if WHERE == "cancel":
            await release.wait()
        events.append("commit")
    lease = sink.bind_log_session_factory(factory)
    try:
        sink.configure_structlog_with_db("child")
        sink._db_log_processor(None, "info", {"event": "synthetic"})
        if WHERE == "setup":
            raise RuntimeError("setup sentinel")
        yield
    finally:
        async def verify():
            assert events[-1] == "commit"
        async def storage():
            await asyncio.sleep(0)
            events.append("dispose")
        async def cleanup():
            await _finish_logging_fixture(
                lease=lease, child_identity=None, verify_logging=verify,
                cleanup_storage=storage, saved_logging=saved)
        if WHERE == "cancel":
            task = asyncio.create_task(finish_owned_cleanup(cleanup))
            await asyncio.sleep(0)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert "dispose" not in events
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                events.append("cancel-propagated")
                events.pop()
        else:
            await finish_owned_cleanup(cleanup)

async def test_1(owner):
    if WHERE == "body":
        raise RuntimeError("body sentinel")

def test_2():
    assert events[-1] == "network-exit"
    print("NEXT-FIXTURE-SETTLED")
"""
    child = tmp_path / "test_child.py"
    child.write_text("WHERE = " + repr(where) + "\n" + source)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-s",
            "-q",
            str(child),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == (0 if where in ("success", "cancel") else 1), (
        result.stdout + result.stderr
    )
    assert "NEXT-FIXTURE-SETTLED" in result.stdout
    assert "fixture cleanup failed" not in result.stdout


def test_owned_child_unsettled_exit_does_not_unwind_or_run_next_fixture(tmp_path, monkeypatch):
    """Use the real phase parent and its process receipt, with NO database/socket."""
    from tests.e2e import test_manual_cop_postgresql as coordinator

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    proof = {"commit": "a" * 40, "digest": "b" * 64, "binding": "synthetic-offline"}
    monkeypatch.setattr(coordinator, "current_verification_source_proof", lambda: proof)
    monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(evidence))
    monkeypatch.setenv("COP_MANUAL_TEST_REVISION", "synthetic-offline")
    child = tmp_path / "test_unsettled.py"
    child.write_text(
        """
import asyncio, json, os
from pathlib import Path
import pytest
import pytest_asyncio
import structlog
from packages.core import log_sink as sink
from packages.core.runtime_ownership import finish_owned_cleanup
from tests.e2e.test_ml_lifecycle_postgresql import (
    _finish_logging_fixture, _logging_child_identity)

@pytest.fixture
def network():
    try:
        yield
    finally:
        print("FORBIDDEN-NETWORK-UNWIND", flush=True)

@pytest_asyncio.fixture
async def owner(network):
    source = {"commit": "a"*40, "source_manifest_sha256": "b"*64}
    receipt = {"temp_root": str(Path(os.environ["COP_MANUAL_TEST_EVIDENCE"]).parent),
               "revision": "synthetic-offline"}
    identity = _logging_child_identity(receipt, source)
    saved = structlog.get_config().copy()
    entered = asyncio.Event()
    async def resistant():
        async with sink._BUFFER_LOCK:
            entered.set()
            while True:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    pass
    sink._flush_loop = resistant
    sink._SHUTDOWN_TIMEOUT_SECONDS = 0.02
    lease = sink.bind_log_session_factory(lambda: None)
    sink.configure_structlog_with_db("child")
    await entered.wait()
    try:
        yield
    finally:
        async def forbidden():
            print("FORBIDDEN-DISPOSAL", flush=True)
        async def cleanup():
            await _finish_logging_fixture(
                lease=lease, child_identity=identity, verify_logging=forbidden,
                cleanup_storage=forbidden, saved_logging=saved)
        await finish_owned_cleanup(cleanup)
        print("FORBIDDEN-OWNER-UNWIND", flush=True)

async def test_1(owner):
    print("OWNED-CHILD-STARTED", flush=True)
def test_2():
    print("FORBIDDEN-NEXT-FIXTURE", flush=True)
"""
    )
    with pytest.raises(AssertionError, match="verification phase logging-unsettled failed"):
        coordinator.run_verification_phase(
            "logging-unsettled",
            [
                sys.executable,
                "-m",
                "pytest",
                "--noconftest",
                "-s",
                "-q",
                str(child),
                "-p",
                "no:cacheprovider",
                "-o",
                "asyncio_mode=auto",
            ],
            limit=20,
            owned_sql=False,
        )
    phase = evidence / "logging-unsettled"
    process = json.loads((phase / "process.json").read_text())
    output = (phase / "stdout.log").read_text() + (phase / "stderr.log").read_text()
    assert process["exit"] == 86 and not process["alive"] and not process["timed_out"]
    assert "OWNED-CHILD-STARTED" in output
    assert "FORBIDDEN-" not in output
    assert not (phase / "nodes.json").exists(), "incomplete child cannot supply green evidence"


@pytest.mark.parametrize("failure", ["drop", "failure", "retry", "cancel", "conservation"])
def test_phase_accounting_rejects_hidden_negative_outcomes(failure):
    from tests.e2e.test_ml_lifecycle_postgresql import _assert_logging_accounting

    status = {
        "drops": {},
        "counts": {"admitted": 1, "acknowledged": 1},
        "owned_rows": 0,
        "encoding_rows": 0,
        "inflight_rows": 0,
        "state": "SETTLED",
    }
    if failure == "drop":
        status["drops"]["shutdown"] = 1
    else:
        key = {
            "failure": "batch_failures",
            "retry": "retried_rows",
            "cancel": "writer_cancellations",
            "conservation": "admitted",
        }[failure]
        status["counts"][key] = 2
    with pytest.raises(AssertionError):
        _assert_logging_accounting(status, complete=True)


@pytest.mark.parametrize("failure_point", ["enter", "retention"])
async def test_independent_bound_factory_failures_never_use_default(
    sink, monkeypatch, failure_point
):
    """Entering a transaction and retention SQL are failures too, not only commit."""
    calls = []

    @asynccontextmanager
    async def factory():
        calls.append("enter")
        if failure_point == "enter":
            raise RuntimeError("synthetic enter failure")
        yield SimpleNamespace(
            add=lambda row: calls.append("add"),
            execute=AsyncMock(side_effect=RuntimeError("synthetic retention failure")),
        )
        pytest.fail("a failed retention statement cannot reach commit")

    monkeypatch.setattr(
        "packages.core.database.get_session",
        lambda: pytest.fail("bound failure must not use application factory"),
    )
    lease = sink.bind_log_session_factory(factory)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await sink.shutdown_log_sink()
    status = sink.log_sink_status()
    assert status["state"] == "SETTLED"
    assert calls == (["enter"] if failure_point == "enter" else ["enter", "add"])
    assert status["counts"]["batch_failures"] == 1
    assert status["counts"]["admitted"] == status["counts"]["admitted_loss"] == 1
    assert status["counts"].get("acknowledged", 0) == 0
    assert status["owned_rows"] == status["inflight_rows"] == 0
    lease.release()


@pytest.mark.parametrize("kind", ["writer", "controller"])
async def test_independent_task_factory_reentry_preserves_every_task_slot(sink, kind):
    """Reentry at writer/controller creation must be fenced, not only at startup."""
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    created = []

    def factory(loop, coroutine, **kwargs):
        assert not sink._ADMISSION_LOCK.locked()
        with pytest.raises(RuntimeError, match="task creation"):
            sink.configure_structlog_with_db("unit")
        reentrant_shutdown = sink.shutdown_log_sink()
        try:
            with pytest.raises(RuntimeError, match="task creation"):
                reentrant_shutdown.send(None)
        finally:
            reentrant_shutdown.close()
        with pytest.raises(RuntimeError):
            lease.release()
        with pytest.raises(RuntimeError):
            sink.bind_log_session_factory(successful_session)
        task = asyncio.Task(coroutine, loop=loop, **kwargs)
        created.append(task)
        return task

    loop.set_task_factory(factory)
    try:
        if kind == "writer":
            await sink._flush_once()
            assert len(created) == 1
        else:
            await sink.shutdown_log_sink()
            assert len(created) == 2  # controller and its real drain writer
    finally:
        loop.set_task_factory(previous)
    await sink.shutdown_log_sink()
    assert all(task.done() for task in created)
    status = sink.log_sink_status()
    assert status["counts"]["admitted"] == status["counts"]["acknowledged"] == 1
    assert status["counts"]["shutdown_outcomes"] == 1
    assert not status["drops"]
    lease.release()


async def test_independent_encoder_settles_before_deadline_without_late_admission(
    sink, monkeypatch
):
    """An encoder finishing in-budget must be joined but must not reopen admission."""
    entered, release = threading.Event(), threading.Event()
    original = sink._encode_row

    def encode(*args):
        entered.set()
        assert release.wait(2), "test must release its isolated producer"
        return original(*args)

    monkeypatch.setattr(sink, "_encode_row", encode)
    lease = sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    worker = threading.Thread(target=emit, args=(sink,))
    worker.start()
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        shutdown = asyncio.create_task(sink.shutdown_log_sink())
        # First turn closes admission, before the encoder is allowed to finish.
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert not sink.log_sink_status()["accepting"]
        with pytest.raises(RuntimeError):
            lease.release()
        release.set()
        await shutdown
    finally:
        release.set()
        await asyncio.to_thread(worker.join, 2)
        if shutdown is not None:
            await shutdown
    assert not worker.is_alive()
    status = sink.log_sink_status()
    assert status["state"] == "SETTLED"
    assert status["owned_rows"] == status["encoding_rows"] == 0
    assert status["counts"]["rejected"] == status["drops"]["shutdown"] == 1
    assert status["counts"].get("admitted", 0) == 0
    assert status["counts"].get("shutdown_expirations", 0) == 0
    lease.release()


async def test_independent_controller_cancel_during_write_retains_owner_and_cached_failure(sink):
    """Controller loss mid-drain cannot release its live writer or authorize restart."""
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def factory():
        entered.set()
        await release.wait()
        yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())

    lease = sink.bind_log_session_factory(factory)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    shutdown = asyncio.create_task(sink.shutdown_log_sink())
    await entered.wait()
    controller, writer = sink._OWNER.controller, sink._INFLIGHT_TASK
    deadline = sink._OWNER.deadline
    try:
        controller.cancel()
        with pytest.raises(RuntimeError, match="CancelledError.*resource disposal forbidden"):
            await shutdown
        assert sink._INFLIGHT_TASK is writer and not writer.done()
        assert sink._OWNER.factory is factory
        assert not sink._BUFFER_LOCK.locked()
        with pytest.raises(RuntimeError):
            lease.release()
        release.set()
        await writer
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError):
            await sink.shutdown_log_sink()
        with pytest.raises(RuntimeError):
            sink.configure_structlog_with_db("unit")
        assert sink._OWNER.controller is controller
        assert sink._OWNER.deadline == deadline
        status = sink.log_sink_status()
        assert status["state"] == "FAILED_UNSETTLED"
        assert status["counts"]["shutdown_outcomes"] == status["counts"]["unsettled_shutdowns"] == 1
        assert status["counts"]["admitted"] == status["counts"]["acknowledged"] == 1
        assert status["counts"].get("writer_cancel_requests", 0) == 0
    finally:
        release.set()
        await writer


async def test_independent_flusher_and_writer_consume_one_absolute_budget(sink, monkeypatch):
    """The drain attempt receives only the budget left after flusher settlement."""
    flusher_entered, flusher_cancelled = asyncio.Event(), asyncio.Event()
    release_flusher, release_writer = asyncio.Event(), asyncio.Event()
    waits = []
    budget = 0.15
    real_wait = asyncio.wait

    async def flusher():
        async with sink._BUFFER_LOCK:
            flusher_entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                flusher_cancelled.set()
                await release_flusher.wait()

    async def writer(batch):
        while not release_writer.is_set():
            try:
                await release_writer.wait()
            except asyncio.CancelledError:
                pass

    async def observed_wait(tasks, *, timeout):
        waits.append((next(iter(tasks)), timeout, sink._OWNER.deadline - sink.time.monotonic()))
        return await real_wait(tasks, timeout=timeout)

    async def release_after_cancellation():
        await flusher_cancelled.wait()
        await asyncio.sleep(0.03)
        release_flusher.set()

    # Replace only this fresh sink's asyncio reference, never the event loop's module.
    proxy = SimpleNamespace(**vars(asyncio))
    proxy.wait = observed_wait
    monkeypatch.setattr(sink, "asyncio", proxy)
    monkeypatch.setattr(sink, "_flush_loop", flusher)
    monkeypatch.setattr(sink, "_write_batch", writer)
    monkeypatch.setattr(sink, "_SHUTDOWN_TIMEOUT_SECONDS", budget)
    sink.bind_log_session_factory(successful_session)
    sink.configure_structlog_with_db("unit")
    emit(sink)
    await flusher_entered.wait()
    releaser = asyncio.create_task(release_after_cancellation())
    try:
        with pytest.raises(RuntimeError, match="writer-or-encoding-live"):
            await sink.shutdown_log_sink()
        deadline = sink._OWNER.deadline
        controller = sink._OWNER.controller
        assert waits[0][0] is sink._FLUSH_TASK
        writer_waits = [item for item in waits if item[0] is sink._INFLIGHT_TASK]
        assert writer_waits and writer_waits[0][1] < budget - 0.02
        assert all(timeout <= max(0, remaining) + 0.001 for _, timeout, remaining in waits)
        with pytest.raises(RuntimeError):
            await sink.shutdown_log_sink()
        assert sink._OWNER.deadline == deadline
        assert sink._OWNER.controller is controller
        counts = sink.log_sink_status()["counts"]
        assert counts["attempt_timeouts"] == counts["writer_cancel_requests"] == 1
        assert counts["shutdown_expirations"] == counts["shutdown_outcomes"] == 1
    finally:
        release_flusher.set()
        release_writer.set()
        await releaser
        await sink._FLUSH_TASK
        if sink._INFLIGHT_TASK is not None:
            await sink._INFLIGHT_TASK


def test_independent_real_api_import_and_lifespan_default_restart():
    """Exercise the actual import/lifespan call sites in a fresh process, no DB/device."""
    source = """
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from packages.core.config import settings
settings.api_token = "synthetic-offline-token"
settings.panasonic_distributed_read_quota_enabled = False
from packages.core import database, log_sink as sink
@asynccontextmanager
async def factory():
    yield SimpleNamespace(add=lambda row: None, execute=AsyncMock())
database.get_session = factory
disposed = []
async def dispose():
    status = sink.log_sink_status()
    assert status["state"] == "SETTLED"
    assert status["owned_rows"] == 0
    assert status["counts"]["admitted"] == status["counts"]["acknowledged"]
    disposed.append(True)
database.engine = SimpleNamespace(dispose=dispose)
from packages.api import main
assert sink.log_sink_status()["state"] == "PRELOOP"
assert sink._FLUSH_TASK is None
sink._db_log_processor(None, "info", {"event": "import-buffer"})
async def exercise():
    async with main.lifespan(main.app):
        first = sink._FLUSH_TASK
        generation = sink.log_sink_status()["generation"]
        assert first is not None
        main.configure_logging("api")
        assert sink._FLUSH_TASK is first
    assert first.done() and disposed == [True]
    async with main.lifespan(main.app):
        assert sink._FLUSH_TASK is not first
        assert sink.log_sink_status()["generation"] == generation + 1
        sink._db_log_processor(None, "info", {"event": "restart-buffer"})
    assert disposed == [True, True]
    assert sink.log_sink_status()["counts"]["admitted"] >= 2
asyncio.run(exercise())
print("REAL-API-IMPORT-LIFESPAN-RESTART-SETTLED")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REAL-API-IMPORT-LIFESPAN-RESTART-SETTLED" in result.stdout


@pytest.mark.parametrize("commit_failure", [False, True])
async def test_LOG_CR01_real_autoflush_notifies_only_after_successful_commit(commit_failure):
    """Real mapped ORM autoflush and DBAPI commit failure, no async driver/socket."""
    import datetime as dt

    from sqlalchemy import create_engine, delete, event, select, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import Session

    from packages.core.models import AppLogRecord
    from tests.e2e.test_ml_lifecycle_postgresql import _logging_session_factory

    engine = create_engine("sqlite:///:memory:")
    commits = asyncio.Queue()
    commit_observations, statements = [], []

    class LocalSession(Session):
        def __init__(self, bind=None, **kwargs):
            assert bind is None
            # AsyncSession's real greenlet bridge runs this real SQLite Session.
            # Nothing mocks flush/execute/commit or disables default autoflush.
            super().__init__(bind=engine, **kwargs)

    def committed(_session):
        commit_observations.append(commits.qsize())

    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.split()[0].upper())

    event.listen(LocalSession, "after_commit", committed)
    event.listen(engine, "before_cursor_execute", sql)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("CREATE TABLE commit_parent (id INTEGER PRIMARY KEY)"))
            connection.execute(
                text(
                    "CREATE TABLE commit_child (id INTEGER PRIMARY KEY, parent_id INTEGER "
                    "REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED)"
                )
            )
            AppLogRecord.__table__.create(connection)
        maker = async_sessionmaker(
            class_=AsyncSession, sync_session_class=LocalSession, expire_on_commit=False
        )
        sessions = _logging_session_factory(maker, commits)
        # SQLite stores DateTime without a timezone, unlike PostgreSQL. Keep
        # default ORM synchronization/autoflush enabled with a matching cutoff.
        cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=24)
        async with sessions() as session:
            assert session.autoflush is True
            session.add(
                AppLogRecord(
                    event="expired",
                    level="INFO",
                    service="test",
                    ts=cutoff - dt.timedelta(hours=1),
                )
            )
        assert commits.get_nowait() is True  # seed flushes at commit, not at DELETE
        assert commits.empty()
        commit_observations.clear()
        statements.clear()

        async def transaction():
            async with sessions() as session:
                row = AppLogRecord(event="retained", level="INFO", service="test")
                session.add(row)
                assert row in session.new
                await session.execute(delete(AppLogRecord).where(AppLogRecord.ts < cutoff))
                assert row.id is not None and not session.new, "real ORM autoflush required"
                assert statements == ["INSERT", "DELETE"]
                assert commits.empty(), "notification must not precede transaction commit"
                if commit_failure:
                    # SQLite accepts this now but rejects the actual DBAPI commit.
                    await session.execute(
                        text("INSERT INTO commit_child (id,parent_id) VALUES (1,999)")
                    )

        if commit_failure:
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                await transaction()
            assert commits.empty()
            assert commit_observations == []
        else:
            await transaction()
            assert commit_observations == [0], "after_commit still precedes notification"
            assert commits.qsize() == 1, "autoflushed INSERT must notify after commit"

        notifications = commits.qsize()
        async with sessions() as session:
            rows = (await session.execute(select(AppLogRecord.event))).scalars().all()
        assert rows == (["expired"] if commit_failure else ["retained"])
        assert commits.qsize() == notifications, "read-only commits must not notify"
    finally:
        event.remove(LocalSession, "after_commit", committed)
        event.remove(engine, "before_cursor_execute", sql)
        engine.dispose()


@pytest.mark.parametrize("outcome", ["success", "body_error", "cancelled"])
async def test_LOG_CR01_independent_session_listener_isolation_and_cleanup(outcome):
    """Real bulk ORM DML, per-session attribution, and listener removal on every exit."""
    from sqlalchemy import create_engine, select, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import Session

    from packages.core.models import AppLogRecord
    from tests.e2e.test_ml_lifecycle_postgresql import _logging_session_factory

    engine = create_engine("sqlite:///:memory:")
    commits = asyncio.Queue()
    targets = []

    class LocalSession(Session):
        def __init__(self, bind=None, **kwargs):
            assert bind is None
            super().__init__(bind=engine, **kwargs)

    def assert_listeners(target, present):
        assert bool(target.dispatch.before_flush) is present
        assert bool(target.dispatch.do_orm_execute) is present

    try:
        with engine.begin() as connection:
            AppLogRecord.__table__.create(connection)
        maker = async_sessionmaker(
            class_=AsyncSession, sync_session_class=LocalSession, expire_on_commit=False
        )
        async with maker() as session:
            assert_listeners(session.sync_session, False)
            session.add(AppLogRecord(event="base", level="INFO", service="test"))
            await session.commit()
        sessions = _logging_session_factory(maker, commits)

        async def transaction():
            async with sessions() as session:
                target = session.sync_session
                targets.append(target)
                assert_listeners(target, True)
                assert not session.new and not session.dirty and not session.deleted
                await session.execute(update(AppLogRecord).values(event="tracked"))
                assert not session.new and not session.dirty and not session.deleted
                assert commits.empty(), "bulk DML must not notify before awaited commit"
                if outcome == "body_error":
                    raise ValueError("synthetic transaction body error")
                if outcome == "cancelled":
                    raise asyncio.CancelledError("synthetic transaction body cancellation")

        if outcome == "success":
            await transaction()
            assert commits.get_nowait() is True
        else:
            error = ValueError if outcome == "body_error" else asyncio.CancelledError
            with pytest.raises(error):
                await transaction()
        assert commits.empty()
        assert_listeners(targets[-1], False)
        async with maker() as session:
            values = (await session.execute(select(AppLogRecord.event))).scalars().all()
        assert values == (["tracked"] if outcome == "success" else ["base"])

        # An unrelated writer while a fresh logging context is open must not
        # contaminate that context's activity flag or create a false read-only signal.
        async with sessions() as reader:
            targets.append(reader.sync_session)
            assert_listeners(reader.sync_session, True)
            async with maker() as unrelated:
                assert_listeners(unrelated.sync_session, False)
                await unrelated.execute(update(AppLogRecord).values(event="unrelated"))
                await unrelated.commit()
            values = (await reader.execute(select(AppLogRecord.event))).scalars().all()
            assert values == ["unrelated"]
            assert commits.empty()
        assert commits.empty(), "another session's committed write must not notify this reader"
        assert all(
            not target.dispatch.before_flush and not target.dispatch.do_orm_execute
            for target in targets
        )
    finally:
        engine.dispose()

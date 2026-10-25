"""S5 repair-one regressions for reviewed runtime and reporting findings."""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import datetime as dt
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def sink(monkeypatch):
    from packages.core import log_sink

    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_DROP_COUNTS", Counter())
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    monkeypatch.setattr(log_sink, "_ENCODING_ROWS", 0)
    monkeypatch.setattr(log_sink, "_DIAGNOSTIC_LOCK", threading.Lock())
    monkeypatch.setattr(log_sink, "_DIAGNOSTIC_THREAD", None)
    monkeypatch.setattr(log_sink, "_DIAGNOSTIC_DROPS", 0)
    monkeypatch.setattr(log_sink, "_INFLIGHT_TASK", None)
    monkeypatch.setattr(log_sink, "_INFLIGHT_BATCH", [])
    monkeypatch.setattr(log_sink, "_BUFFER_LOCK", asyncio.Lock())
    return log_sink


def test_R35_full_queue_never_touches_event_before_rejecting(sink):
    for index in range(1000):
        sink._db_log_processor(None, "info", {"event": str(index)})

    class Untouchable(dict):
        def get(self, *args):
            pytest.fail("saturated producer inspected an event before reserving capacity")

        def items(self):
            pytest.fail("saturated producer traversed an event before reserving capacity")

    event = Untouchable(event="full")
    assert sink._db_log_processor(None, "info", event) is event
    assert sink.log_sink_status()["owned_rows"] == 1000
    assert sink.log_sink_status()["drops"]["saturation"] == 1


def test_R35_concurrent_encoding_owns_capacity_and_reject_releases_slot(monkeypatch, sink):
    for index in range(998):
        sink._db_log_processor(None, "info", {"event": str(index)})
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def blocked_encoding(*args):
        calls.append(args)
        entered.wait(timeout=2)
        assert release.wait(timeout=2)
        raise ValueError("unencodable fixture")

    monkeypatch.setattr(sink, "_encode_row", blocked_encoding)
    workers = [
        threading.Thread(target=sink._db_log_processor, args=(None, "info", {"event": "owned"}))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        entered.wait(timeout=2)
        assert sink.log_sink_status()["encoding_rows"] == 2
        assert sink.log_sink_status()["owned_rows"] == 1000
        sink._db_log_processor(None, "info", {"event": "saturated"})
        assert len(calls) == 2
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
    assert sink.log_sink_status()["owned_rows"] == 998
    assert sink.log_sink_status()["encoding_rows"] == 0
    assert sink.log_sink_status()["drops"] == {"saturation": 1, "oversize": 2}


def test_R35_traversal_and_serialization_are_bounded_without_user_hooks(monkeypatch, sink):
    real_dumps = json.dumps
    strings = []

    def bounded_dumps(value, **kwargs):
        if isinstance(value, str):
            strings.append(len(value))
            assert len(value) <= 1, "whole oversized string serialized"
        return real_dumps(value, **kwargs)

    class ForbiddenString:
        def __str__(self):
            pytest.fail("arbitrary user normalization hook invoked")

    cycle = []
    cycle.append(cycle)
    deep = []
    for _ in range(40):
        deep = [deep]
    monkeypatch.setattr(sink.json, "dumps", bounded_dumps)
    for value in ("x" * 1_000_000, list(range(10_000)), deep, cycle, ForbiddenString()):
        sink._db_log_processor(None, "info", {"event": "bounded", "value": value})
    assert sink.log_sink_status()["owned_rows"] == 0
    assert sink.log_sink_status()["drops"] == {"oversize": 5}
    assert max(strings) == 1
    assert len(strings) < 10_000


async def test_R35_stalled_failure_diagnostics_are_single_owned_offloop_writer(monkeypatch, sink):
    entered, release = threading.Event(), threading.Event()
    writes = []
    real_stderr = sink.sys.stderr

    class StalledSink:
        def write(self, message):
            if not message.startswith("log sink flush failed:"):
                return real_stderr.write(message)
            writes.append(message)
            entered.set()
            assert release.wait(timeout=3)

    monkeypatch.setattr(sink.sys, "stderr", StalledSink())
    monkeypatch.setattr(
        sink, "_write_batch", AsyncMock(side_effect=RuntimeError("private fixture"))
    )
    for index in range(1000):
        sink._db_log_processor(None, "info", {"event": str(index)})
    watchdog = threading.Timer(2, release.set)
    watchdog.start()
    try:
        await asyncio.wait_for(sink._flush_once(), timeout=0.5)
        assert await asyncio.to_thread(entered.wait, 0.5)
        for _ in range(4):
            await asyncio.wait_for(sink._flush_once(), timeout=0.5)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.5)
        assert not release.is_set(), "event loop waited for stalled stderr"
        assert len(writes) == 1
        assert sink.log_sink_status()["diagnostic_pending"] is True
        assert sink.log_sink_status()["diagnostic_drops"] == 4
        assert "private fixture" not in writes[0]
    finally:
        release.set()
        watchdog.cancel()
        if sink._DIAGNOSTIC_THREAD is not None:
            await asyncio.to_thread(sink._DIAGNOSTIC_THREAD.join, 1)
    assert sink.log_sink_status()["diagnostic_pending"] is False


@pytest.mark.parametrize(
    "case",
    [
        "fresh",
        "configured_fresh",
        "stale",
        "future",
        "missing",
        "nan",
        "inf",
        "remote_future",
        "gate_stale",
        "gate_future",
        "gate_missing",
        "gate_nonfinite",
    ],
)
async def test_R32_original_outcome_owner_and_summary_require_admissible_fresh_evidence(
    monkeypatch, case
):
    from packages.api.routers import optimizer as router
    from packages.core import outcome_experiments as owner
    from packages.core.models import DeviceStatusRecord, SpaceHeatingGateRecord
    from packages.core.settings_service import SETTING_SPECS
    from packages.core.space_heating_gate import HeatingGateConfig

    now = dt.datetime.now(dt.timezone.utc)
    timestamp = (
        now - dt.timedelta(hours=1)
        if case == "stale"
        else now - dt.timedelta(minutes=30)
        if case == "configured_fresh"
        else now + dt.timedelta(hours=1)
        if case == "future"
        else None
        if case == "missing"
        else now
    )
    status = SimpleNamespace(
        device_id="offline",
        ts=timestamp,
        outdoor_temp=float("nan") if case == "nan" else float("inf") if case == "inf" else 5.0,
        space_heating_evidence="device_off",
        device_action="OFF",
    )
    if case == "remote_future":
        status.native_policy_provenance = {
            "captured_at": now.isoformat(),
            "timing": {"observed_at": (now + dt.timedelta(hours=1)).isoformat()},
        }
    config = HeatingGateConfig()
    gate = SimpleNamespace(
        state="ALLOWED",
        config_fingerprint=config.fingerprint,
        reason_code="fixture",
        last_raw_outdoor_c=float("nan") if case == "gate_nonfinite" else 5.0,
        source_status_ts=now - dt.timedelta(hours=1) if case == "gate_stale" else now,
        evaluated_at=now + dt.timedelta(hours=1)
        if case == "gate_future"
        else None
        if case == "gate_missing"
        else now,
    )
    values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    if case == "configured_fresh":
        values["device_status_max_age_minutes"] = "60"
    ready = case in ("fresh", "configured_fresh")
    statements = []

    async def execute(statement):
        assert statement.is_select, "reporting must not perform DML"
        statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is DeviceStatusRecord:
            assert "device_status.ts <=" in str(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: status)
        assert entity is SpaceHeatingGateRecord
        return SimpleNamespace(scalar_one_or_none=lambda: gate)

    @asynccontextmanager
    async def sessions(**options):
        assert options["read_only"] is True
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(owner, "get_session", sessions)
    monkeypatch.setattr(owner, "get_bool_setting", AsyncMock(return_value=True))
    monkeypatch.setattr(owner, "get_float_setting", AsyncMock(return_value=0.5))
    monkeypatch.setattr(owner, "get_all_settings", AsyncMock(return_value=values))
    monkeypatch.setattr(owner, "get_space_heating_gate_config", AsyncMock(return_value=config))
    payload = await owner.get_outcome_experiment_status()
    direct = owner.assess_manual_trial_conditions(
        owner.HeatCurveConfig(),
        status,
        gate,
        config,
        now=now,
        max_age_seconds=3600 if case == "configured_fresh" else 900,
    )
    assert direct["ready"] is ready
    assert payload["conditions"]["ready"] is ready
    assert (payload["status"] == "manual_review_ready") is ready
    assert payload["mode"] == "manual_review_only"
    json.dumps(payload, allow_nan=False)

    @asynccontextmanager
    async def summary_session(**options):
        yield SimpleNamespace()

    monkeypatch.setattr(router, "get_session", summary_session)
    monkeypatch.setattr(
        router,
        "measured_window_outcome",
        AsyncMock(return_value={"cost": {}, "comfort": {}, "weather_matched_comparison": {}}),
    )
    monkeypatch.setattr("packages.api._helpers.get_price_area", AsyncMock(return_value="SE3"))
    monkeypatch.setattr(
        "packages.core.settings_service.get_float_setting", AsyncMock(return_value=20)
    )
    monkeypatch.setattr("packages.core.settings_service.get_user_tz", AsyncMock(return_value="UTC"))
    summary = await router.get_outcome_summary(days=7)
    assert summary["experiment"]["conditions"]["ready"] is ready
    assert summary["experiment"]["mode"] == "manual_review_only"
    assert statements and all(statement.is_select for statement in statements)


@pytest.mark.parametrize(
    "stage", ["first_error", "first_cancel", "scheduler_error", "running_cancel"]
)
async def test_R23_standalone_ML_owns_startup_and_cancellation_cleanup(monkeypatch, stage):
    from packages.ml import main

    calls = []
    scheduler = SimpleNamespace(add_job=Mock(), start=Mock(), close=AsyncMock())

    async def retrain(**kwargs):
        calls.append("first_await")
        assert kwargs == {"automatic": True, "source": "ml_startup"}
        if stage == "first_error":
            raise RuntimeError("startup fixture")
        if stage == "first_cancel":
            raise asyncio.CancelledError

    async def close_scheduler():
        calls.append("scheduler")

    async def logs():
        calls.append("logs")

    async def dispose():
        calls.append("engine")

    async def stop_loop(*args):
        raise asyncio.CancelledError

    scheduler.close.side_effect = close_scheduler
    if stage == "scheduler_error":
        scheduler.start.side_effect = RuntimeError("scheduler fixture")
    monkeypatch.setattr(main, "retrain_models", retrain)
    monkeypatch.setattr(main, "create_scheduler", lambda: scheduler)
    monkeypatch.setattr(main.demand_model, "load_latest", lambda: None)
    monkeypatch.setattr(
        "packages.core.logging.configure_logging", lambda name: calls.append("logging")
    )
    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", logs)
    monkeypatch.setattr("packages.core.database.engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main.asyncio, "sleep", stop_loop)
    expected = (
        RuntimeError if stage in ("first_error", "scheduler_error") else asyncio.CancelledError
    )
    with pytest.raises(expected):
        await main.main()
    assert calls == [
        "logging",
        "first_await",
        *(["scheduler"] if stage in ("scheduler_error", "running_cancel") else []),
        "logs",
        "engine",
    ]
    if stage in ("scheduler_error", "running_cancel"):
        job = scheduler.add_job.call_args
        assert job.kwargs["day_of_week"] == "sun"
        assert job.kwargs["timezone"] == "UTC"
        assert job.kwargs["kwargs"] == {"automatic": True, "source": "ml_schedule"}


@pytest.mark.parametrize(
    "timezone_name,timestamps,expected",
    [
        ("America/New_York", ["2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z"], {1: 1.0, 3: 1.0}),
        ("America/New_York", ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"], {1: 0.5}),
        ("Australia/Sydney", ["2026-10-03T15:30:00Z", "2026-10-03T16:30:00Z"], {1: 1.0, 3: 1.0}),
    ],
)
async def test_R22_configured_timezone_DST_gap_and_fold(
    monkeypatch, timezone_name, timestamps, expected
):
    from packages.core import settings_service

    rows = [
        (dt.datetime.fromisoformat(value), "OFF" if expected == {1: 0.5} and index else "HEATING")
        for index, value in enumerate(timestamps)
    ]

    @asynccontextmanager
    async def sessions():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))

    monkeypatch.setattr(settings_service, "get_session", sessions)
    monkeypatch.setattr(settings_service, "get_user_tz", AsyncMock(return_value=timezone_name))
    scores = await settings_service.get_learned_usage()
    assert scores == {"weekday": {}, "weekend": expected}


async def test_R30_real_initial_publication_state_at_clock_60_is_not_on_cooldown(monkeypatch):
    import time
    from packages.optimizer import main
    from tests.test_s5_runtime_contracts import _cooldown_harness

    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    initial = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "_last_plan_generated_at"
    )
    harness = _cooldown_harness(monkeypatch)
    monkeypatch.setattr(main, "_last_plan_generated_at", initial)
    monkeypatch.setattr(time, "monotonic", lambda: 60.0)
    harness.release.set()
    assert await main.run_optimization(scheduled=True) == 123
    assert harness.events == [
        ("flush", initial),
        ("commit_enter", initial),
        ("committed", initial),
    ]
    assert main._last_plan_generated_at == 60.0
    assert await main.run_optimization(scheduled=True) is None
    harness.generate.assert_awaited_once()


@pytest.mark.parametrize("action_type", ["force_dhw_off", "quiet_mode_off"])
def test_R31_offset_equivalent_distinct_restore_links_survive_normalization(action_type):
    from packages.optimizer.rules_engine import RulesOptimizer
    from packages.optimizer.main import _validate_unique_restore_keys

    first = {
        "type": action_type,
        "ts": "2026-10-15T10:00:00+02:00",
        "reverts_action_key": "boost:A",
        "payload": {"reason": "target_reached"},
    }
    second = {**first, "ts": "2026-10-15T08:00:00Z", "reverts_action_key": "boost:B"}
    duplicate = {**first, "ts": "2026-10-15T08:00:00+00:00"}
    normalized = RulesOptimizer._normalise_actions([first, second, duplicate])
    assert len(normalized) == 2
    assert {row["reverts_action_key"] for row in normalized} == {"boost:A", "boost:B"}
    _validate_unique_restore_keys(normalized)


def test_R31_distinct_source_keys_and_genuine_unlinked_quiet_collapse():
    from packages.optimizer.rules_engine import RulesOptimizer

    source = {
        "type": "force_dhw_on",
        "ts": "2026-10-15T10:00:00+02:00",
        "action_key": "boost:A",
        "payload": {},
    }
    other = {**source, "action_key": "boost:B", "ts": "2026-10-15T08:00:00Z"}
    assert len(RulesOptimizer._normalise_actions([source, other, dict(source)])) == 2
    quiet = [
        {"type": "quiet_mode_off", "ts": "2026-10-15T08:00:00Z", "payload": {}},
        {"type": "quiet_mode_on", "ts": "2026-10-15T10:00:00+02:00", "payload": {"level": 2}},
        {"type": "quiet_mode_on", "ts": "2026-10-15T09:00:00Z", "payload": {"level": 2}},
        {"type": "quiet_mode_off", "ts": "2026-10-15T10:00:00Z", "payload": {}},
    ]
    normalized = RulesOptimizer._normalise_actions(quiet)
    assert [row["type"] for row in normalized] == ["quiet_mode_on", "quiet_mode_off"]


@pytest.mark.parametrize("fault", ["status", "horizon", "naive_clock"])
async def test_S4_earlier_represented_gate_remains_real_and_nonpublishing(fault):
    from tests.test_optimizer_api_contracts import _admissible_plan, _run_admission

    plan = _admissible_plan()
    if fault == "status":
        plan["diagnostic_status"] = "unproven"
        reason = "Diagnostic strategy is unproven"
    elif fault == "horizon":
        plan["horizon_end"] += dt.timedelta(hours=1)
        reason = "Diagnostic commands or horizon differ from represented trace"
    else:
        plan["horizon_start"] = plan["horizon_start"].replace(tzinfo=None)
        reason = "Represented strategy requires aware instants"
    outcome = await _run_admission(plan, isolate_frozen_quality=False)
    assert outcome.result is None
    assert outcome.records == []
    outcome.session_factory.assert_called_once()
    assert outcome.cooldown == 0.0
    refusal = [log for log in outcome.logs if log.get("event") == "optimizer_no_plan"]
    assert refusal == [
        {
            "event": "optimizer_no_plan",
            "log_level": "info",
            "reason": "unproven_serialized_strategy",
            "refusal": reason,
        }
    ]

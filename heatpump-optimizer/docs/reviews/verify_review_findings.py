"""Offline reproductions for the 2026-10-12 optimizer/dashboard review.

No production connection, cloud call, fitting, or device operation is allowed.
The executor uses the repository's fake database, clock, fence and device.
An exit status of zero means the probes completed, not that defects are fixed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import selectors
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4


APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = APP_ROOT.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(APP_ROOT), str(REPO_ROOT)]

# These values override .env before application modules are imported.
os.environ.update(
    APP_ENVIRONMENT="development",
    API_TOKEN="review-offline-synthetic-token",
    SECRET_KEY="review-offline-synthetic-key",
    DATABASE_URL="postgresql+asyncpg://review:review@127.0.0.1:1/review",
    REDIS_URL="redis://127.0.0.1:1/0",
    HPO_TEST_OFFLINE="1",
    PYTHONDONTWRITEBYTECODE="1",
    PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
)
_scratch = Path(tempfile.gettempdir()) / f"hpo-review-probes-{uuid4().hex}"
os.environ["MODEL_DIR"] = str(_scratch)


def reject_network(*_args, **_kwargs):
    raise AssertionError("Real network access is forbidden in this review harness")


socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.create_connection = reject_network
socket.getaddrinfo = reject_network


class NoIOSelector(selectors.BaseSelector):
    """No sockets, including Windows' internal loopback socketpair."""

    def register(self, *_args):
        raise AssertionError("External I/O is forbidden in this review harness")

    def unregister(self, *_args):
        raise AssertionError("External I/O is forbidden in this review harness")

    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.005))
        return []

    def get_map(self):
        return {}

    def close(self):
        pass


class OfflineLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(NoIOSelector())

    def _make_self_pipe(self):
        self._ssock = self._csock = None

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        pass


def git_value(*args):
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


async def restore_learning_race():
    import pytest

    from packages.optimizer.executor_core import LearningModeState
    from tests.test_safety_reverts import _S1Clock, _S1RestoreStore

    patcher = pytest.MonkeyPatch()
    clock = _S1Clock(patcher)
    store = _S1RestoreStore(restore_type="force_dhw_off", clock=clock)
    executor = store.executor()
    active = False
    lookups = 0

    async def learning():
        nonlocal lookups
        lookups += 1
        return LearningModeState.ACTIVE if active else LearningModeState.INACTIVE

    async def sleep(seconds):
        nonlocal active
        active = True
        await clock.sleep(seconds)

    executor._learning_check = learning
    executor._sleep = sleep
    try:
        await executor.execute_due_actions()
        writes = store.device.set_force_dhw.await_count
        return {
            "defect_reproduced": active and writes > 0,
            "observation_active_before_dispatch": active,
            "learning_checks": lookups,
            "mock_device_writes": writes,
            "restore_status": store.restore.status,
            "sdk_call_starts_seconds": clock.sdk_starts,
            "expected_after_fix": "No SDK write may start after positive observation activation",
        }
    finally:
        patcher.undo()


async def restore_metadata_overwrite():
    import pytest

    from packages.optimizer.executor_core import PlanExecutor
    from packages.optimizer.shower_mode import ShowerDetector
    from tests.test_safety_reverts import _S1Clock, _S1RestoreStore

    patcher = pytest.MonkeyPatch()
    clock = _S1Clock(patcher)
    store = _S1RestoreStore(restore_type="force_dhw_off", clock=clock)
    action = store.restore
    token = uuid4()
    action.safety_claim_token = token
    action.safety_claimed_at = clock.utcnow() - dt.timedelta(minutes=10)
    metadata = {
        "schema": "safety_attempt_v1",
        "claim_token": str(token),
        "action_id": action.id,
        "source_id": action.reverts_action_id,
        "device_id": action.device_id,
        "drain": "confirmed",
        "owned_tasks_done": True,
        "fence_lost": False,
    }
    action.result_json = json.dumps({"reason": "deferred", "safety_attempt": metadata})
    before = PlanExecutor._confirmed_drain(action)
    original_due = action.scheduled_ts
    event = SimpleNamespace(activation_action_id=store.source.id)
    try:
        async with store.sessions() as session:
            await ShowerDetector()._reschedule_linked_off(session, event, clock.utcnow(), "timeout")
        after = PlanExecutor._confirmed_drain(action)
        token_preserved = action.safety_claim_token == token
        result_keys = sorted(json.loads(action.result_json))
        due_after_reschedule = action.scheduled_ts
        executor = store.executor()
        admitted = await executor._claim_due_safety_action(clock.utcnow())
        if admitted is not None:
            # A future fix may admit this fake restore; release its fake ownership
            # without executing it, so this verification branch never dispatches.
            attempt = executor._attempt_for(admitted)
            await attempt.drain()
            await attempt.release()
            executor._safety_attempts.pop(attempt.token, None)
        return {
            "defect_reproduced": before and not after and admitted is None,
            "drain_proof_before": before,
            "drain_proof_after": after,
            "claim_token_preserved": token_preserved,
            "claim_admitted_after_reschedule": admitted is not None,
            "result_keys_after": result_keys,
            "original_due_at": original_due.isoformat(),
            "due_at_after": due_after_reschedule.isoformat(),
            "age_reset_reproduced": due_after_reschedule > original_due,
            "expected_after_fix": "Preserve admission evidence and original obligation age",
        }
    finally:
        patcher.undo()


async def credential_cache():
    from packages.core.services.aquarea import AquareaWrapper

    wrapper = AquareaWrapper(read_only=True)
    old_client = SimpleNamespace()
    wrapper._client = old_client
    wrapper._authenticated = True
    wrapper._credentials = ("old@example.test", "synthetic-old-password")
    wrapper._authenticate = AsyncMock()
    new_values = {
        "aquarea_username": "new@example.test",
        "aquarea_password": "synthetic-new-password",
    }
    reader = AsyncMock(side_effect=lambda key: new_values[key])
    with patch("packages.core.settings_service.get_setting", new=reader):
        await wrapper._ensure_authenticated()
    unchanged = wrapper._client is old_client
    return {
        "defect_reproduced": unchanged and reader.await_count == 0,
        "settings_reads": reader.await_count,
        "old_client_retained": unchanged,
        "reauthentication_requested": wrapper._authenticate.await_count,
        "expected_after_fix": "Detect changed credential revision before reusing a client",
    }


def freshness():
    from packages.core.device_data_quality import device_data_quality_from_inputs
    from packages.core.operational_alerts import device_status_is_fresh

    now = dt.datetime.now(dt.timezone.utc)
    record = SimpleNamespace(
        ts=now,
        native_policy_provenance={
            "captured_at": now.isoformat(),
            "timing": {
                "observed_at": (now - dt.timedelta(hours=1)).isoformat(),
                "retrieved_at": now.isoformat(),
            },
        },
    )
    quality = device_data_quality_from_inputs(
        now=now,
        status=record,
        values={
            "aquarea_username": "synthetic",
            "aquarea_password": "synthetic",
            "device_status_max_age_minutes": "15",
            "poll_interval_seconds": "300",
        },
    )
    displayed_fresh = device_status_is_fresh(
        record.ts, now=now, threshold_seconds=quality["threshold_seconds"]
    )
    return {
        "defect_reproduced": displayed_fresh and not quality["ready"],
        "canonical_quality_ready": quality["ready"],
        "canonical_reasons": quality["reasons"],
        "dashboard_fresh": displayed_fresh,
        "future_row_also_fresh": device_status_is_fresh(
            now + dt.timedelta(hours=1), now=now, threshold_seconds=900
        ),
        "expected_after_fix": "Known stale, future or misordered evidence is never labelled live",
    }


def reset_foreign_key():
    from packages.api.routers.admin import RESET_SCOPES
    from packages.core.models import ExecutorVerificationReadRecord

    foreign_key = next(iter(ExecutorVerificationReadRecord.__table__.c.action_id.foreign_keys))
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE plan_actions (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute(
            "CREATE TABLE executor_verification_reads "
            "(id INTEGER PRIMARY KEY, action_id INTEGER REFERENCES plan_actions(id) "
            "ON DELETE RESTRICT)"
        )
        connection.execute("INSERT INTO plan_actions VALUES(11, 'executed')")
        connection.execute("INSERT INTO executor_verification_reads VALUES(1, 11)")
        error = None
        try:
            connection.execute("DELETE FROM plan_actions")
        except sqlite3.IntegrityError as exception:
            error = str(exception)
    reset_tables = [table.__tablename__ for table in RESET_SCOPES["plans"]]
    omitted = "executor_verification_reads" not in reset_tables
    return {
        "defect_reproduced": foreign_key.ondelete == "RESTRICT" and omitted and error is not None,
        "actual_foreign_key_target": foreign_key.target_fullname,
        "actual_ondelete": foreign_key.ondelete,
        "reset_tables": reset_tables,
        "in_memory_delete_error": error,
        "scope": "Foreign-key contract only; not a live PostgreSQL endpoint test",
        "expected_after_fix": "Dependency-aware reset or explicit non-destructive refusal",
    }


def timezone_validation():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from packages.core.settings_service import validate_setting_value

    invalid = "Europe/Stockholmm"
    accepted = True
    try:
        validate_setting_value("timezone", invalid)
    except ValueError:
        accepted = False
    failure = None
    try:
        ZoneInfo(invalid)
    except ZoneInfoNotFoundError as exception:
        failure = type(exception).__name__
    return {
        "defect_reproduced": accepted and failure is not None,
        "invalid_value_accepted": accepted,
        "consumer_error": failure,
        "expected_after_fix": "Reject invalid IANA timezone before persistence",
    }


def nested_log_datetime():
    from packages.core import log_sink

    event = {
        "event": "review_synthetic_seasonal_cycle",
        "open_revert_obligations": {"oldest_scheduled_at": dt.datetime.now(dt.timezone.utc)},
    }
    failure = None
    buffer = log_sink._LOG_BUFFER
    initial_size = len(buffer)
    try:
        log_sink._db_log_processor(None, "info", event)
    except (TypeError, ValueError) as exception:
        failure = str(exception)
    finally:
        # Only remove synthetic entries from this isolated process's in-memory buffer.
        del buffer[initial_size:]
    return {
        "defect_reproduced": failure is not None,
        "serialization_error": failure,
        "current_source_handles_nested_datetime": failure is None,
        "database_flush_started": log_sink._FLUSH_TASK is not None,
        "scope": "Isolated log processor; no live maintenance cycle or database flush",
    }


async def probes():
    return {
        "F01": await restore_learning_race(),
        "F02": freshness(),
        "F03": await credential_cache(),
        "F04": reset_foreign_key(),
        "F06": timezone_validation(),
        "F12_F15": await restore_metadata_overwrite(),
        "F13": nested_log_datetime(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional path for local JSON evidence")
    parser.add_argument("--pure-tests", action="store_true", help="Run the six isolated domain files")
    args = parser.parse_args()
    import structlog

    structlog.configure(processors=[], logger_factory=structlog.ReturnLoggerFactory())
    try:
        if args.pure_tests:
            if args.output:
                parser.error("--pure-tests cannot be combined with --output")
            import pytest

            class OfflinePolicy(asyncio.DefaultEventLoopPolicy):
                _loop_factory = OfflineLoop

            asyncio.set_event_loop_policy(OfflinePolicy())
            files = [
                "test_panasonic_control_state.py",
                "test_space_heating_gate.py",
                "test_heat_curve.py",
                "test_time_slots.py",
                "test_comfort_assessment.py",
                "test_plan_risk.py",
            ]
            raise SystemExit(
                pytest.main(
                    ["--noconftest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-q"]
                    + [str(APP_ROOT / "tests" / name) for name in files]
                )
            )
        results = asyncio.run(probes(), loop_factory=OfflineLoop)
        sources = [
            "packages/optimizer/executor_core.py",
            "packages/optimizer/shower_mode.py",
            "packages/core/services/aquarea.py",
            "packages/core/device_data_quality.py",
            "packages/core/operational_alerts.py",
            "packages/core/settings_service.py",
            "packages/core/log_sink.py",
            "packages/api/routers/admin.py",
        ]
        evidence = {
            "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "head": git_value("rev-parse", "HEAD"),
            "branch": git_value("branch", "--show-current"),
            "python": sys.version,
            "verification_kind": "offline_in_memory_no_network",
            "source_sha256": {
                name: hashlib.sha256((APP_ROOT / name).read_bytes()).hexdigest() for name in sources
            },
            "results": results,
        }
        encoded = json.dumps(evidence, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
            print(f"Evidence written to {args.output.resolve()}")
        else:
            print(encoded, end="")
    finally:
        # Empty scratch directories may be created by application imports.
        # rmdir never recursively deletes anything and cannot remove a nonempty directory.
        try:
            _scratch.rmdir()
        except (FileNotFoundError, OSError):
            pass


if __name__ == "__main__":
    main()

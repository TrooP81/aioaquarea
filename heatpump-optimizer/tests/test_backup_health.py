from __future__ import annotations

import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.backup_health import BackupPolicy, project_backup_health

NOW = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)


def evidence():
    binding = {
        "archive_id": "private-archive",
        "sha256": "a" * 64,
        "source_id": "private-source",
        "schema_revision": "123",
    }
    return {
        "schema_version": 1,
        "last_attempt": {
            "id": "attempt",
            "started_at": (NOW - dt.timedelta(hours=1)).isoformat(),
            "finished_at": NOW.isoformat(),
            "status": "succeeded",
        },
        "last_success": {
            **binding,
            "snapshot_at": (NOW - dt.timedelta(hours=1)).isoformat(),
            "completed_at": NOW.isoformat(),
        },
        "last_verification": {
            **binding,
            "id": "verification",
            "started_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
            "status": "succeeded",
        },
        "last_verified": {**binding, "verified_at": NOW.isoformat()},
    }


def project(state, now=NOW):
    return project_backup_health(state, BackupPolicy(), now=now)


def test_histories_and_explicit_public_allowlist():
    state = evidence()
    state["password"] = "SENTINEL"
    state["last_attempt"]["path"] = "SENTINEL"
    result = project(state)
    assert result["ready"]
    serialized = json.dumps(result)
    for forbidden in ("SENTINEL", "private-", "sha256", "schema_revision", "archive_id"):
        assert forbidden not in serialized
    failed = copy.deepcopy(state)
    failed["last_attempt"]["status"] = "failed"
    result = project(failed)
    assert not result["ready"]
    assert "backup_attempt_failed" in result["reason_codes"]
    assert result["last_success"] == project(state)["last_success"]
    assert result["next_due_at"] == project(state)["next_due_at"]


@pytest.mark.parametrize(
    "raw", [None, "invalid", "[]", "{}", '{"status":"ok"}', {"schema_version": 2}]
)
def test_unknown_never_uses_heartbeat(raw):
    assert not project(raw)["ready"]
    assert project(raw)["last_success"] is None


@pytest.mark.parametrize(
    ("days", "stale"), [(30, False), (32 - 1 / 86400, False), (32, True), (33, True)]
)
def test_monthly_freshness_boundary(days, stale):
    state = evidence()
    state["last_success"]["snapshot_at"] = (NOW - dt.timedelta(days=days)).isoformat()
    assert ("backup_success_stale" in project(state)["reason_codes"]) is stale


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("last_attempt", {"status": "failed"}, "backup_attempt_failed"),
        ("last_attempt", {"replica_status": "required_failed"}, "backup_replica_required_failed"),
        ("last_verification", {"status": "failed"}, "backup_verification_failed"),
        (
            "last_verification",
            {"status": "failed", "reason_code": "cleanup_failed"},
            "backup_cleanup_failed",
        ),
        ("last_verification", {"sha256": "b" * 64}, "backup_reporting_incomplete"),
        ("last_verified", {"source_id": "different"}, "backup_restore_unqualified"),
        ("last_success", {"snapshot_at": "2027-01-01T00:00:00+00:00"}, "backup_success_missing"),
        ("last_success", {"completed_at": "bad"}, "backup_success_missing"),
    ],
)
def test_failure_paths(field, value, reason):
    state = evidence()
    state[field].update(value)
    assert reason in project(state)["reason_codes"]
    assert not project(state)["ready"]


def test_running_and_optional_replica_warning():
    state = evidence()
    state["last_attempt"]["status"] = "running"
    assert project(state)["ready"]
    assert "backup_attempt_overdue" in project(state, NOW + dt.timedelta(hours=1))["reason_codes"]
    state["last_attempt"]["status"] = "succeeded"
    state["last_attempt"]["replica_status"] = "optional_failed"
    assert project(state)["ready"]
    assert project(state)["status"] == "warning"


def test_incomplete_verification_never_looks_healthy():
    state = evidence()
    state["last_verification"]["status"] = "running"
    assert not project(state)["ready"]
    assert "backup_reporting_incomplete" in project(state)["reason_codes"]
    state["last_verification"]["status"] = "succeeded"
    state["last_verification"]["started_at"] = (NOW + dt.timedelta(seconds=1)).isoformat()
    assert not project(state)["ready"]


@pytest.mark.parametrize("value", [[], {}, 123, True])
def test_malformed_status_and_attempt_ids_are_unknown_not_exceptions(value):
    state = evidence()
    state["last_attempt"]["status"] = value
    state["last_attempt"]["id"] = value
    state["last_verification"]["status"] = value
    assert not project(state)["ready"]
    assert project(state)["last_attempt"]["status"] == "unknown"


@pytest.mark.parametrize(
    "updates",
    [
        {"backup_interval_seconds": "7776001"},
        {"backup_max_age_seconds": "2592000"},
        {"backup_retention_days": "30"},
        {"backup_timeout_seconds": "nan"},
        {"backup_timeout_seconds": True},
        {"backup_timeout_seconds": "60.5"},
    ],
)
def test_policy_rejects_invalid_or_inconsistent_values(updates):
    with pytest.raises(ValueError):
        BackupPolicy.from_settings(updates)


def test_policy_empty_falls_back_to_environment():
    assert (
        BackupPolicy.from_settings(
            {"backup_interval_seconds": ""}, fallbacks={"backup_interval_seconds": "86400"}
        ).interval_seconds
        == 86400
    )


async def test_bulk_validation_under_existing_lock_before_upsert():
    from packages.core import settings_service

    session = AsyncMock()
    session.execute.side_effect = [
        None,
        SimpleNamespace(
            scalars=lambda: [
                SimpleNamespace(key="backup_retention_days", value="33"),
            ]
        ),
    ]
    with pytest.raises(ValueError, match="retention"):
        await settings_service.set_settings_bulk(
            {"backup_max_age_seconds": str(34 * 86400)}, session=session
        )
    assert session.execute.await_count == 2
    assert "pg_advisory_xact_lock" in str(session.execute.call_args_list[0].args[0])
    session.commit.assert_not_awaited()


async def test_single_writer_delegates():
    from packages.core import settings_service

    with patch.object(settings_service, "set_settings_bulk", new_callable=AsyncMock) as bulk:
        await settings_service.set_setting("backup_retention_days", "120")
    bulk.assert_awaited_once_with({"backup_retention_days": "120"})


async def test_concurrent_merged_changes_cannot_persist_invalid_combination(monkeypatch):
    """A transaction-lock fake models committed visibility between bulk writers."""
    import asyncio
    from contextlib import asynccontextmanager

    from packages.core import settings_service

    lock = asyncio.Lock()
    stored = {}

    class Transaction:
        def __init__(self):
            self.owns_lock = False
            self.pending = {}

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql:
                await lock.acquire()
                self.owns_lock = True
                await asyncio.sleep(0)
                return None
            if sql.startswith("SELECT"):
                assert self.owns_lock
                return SimpleNamespace(
                    scalars=lambda: [
                        SimpleNamespace(key=key, value=value) for key, value in stored.items()
                    ]
                )
            assert self.owns_lock
            values = statement.compile().params
            self.pending[values["key"]] = values["value"]

    @asynccontextmanager
    async def session():
        transaction = Transaction()
        try:
            yield transaction
            stored.update(transaction.pending)
        finally:
            if transaction.owns_lock:
                lock.release()

    monkeypatch.setattr(settings_service, "get_session", session)
    results = await asyncio.gather(
        settings_service.set_settings_bulk({"backup_retention_days": "34"}),
        settings_service.set_settings_bulk(
            {"backup_max_age_seconds": str(35 * 86400), "backup_timeout_seconds": "120"}
        ),
        return_exceptions=True,
    )
    assert results[0] is None
    assert isinstance(results[1], ValueError)
    assert stored == {"backup_retention_days": "34"}  # rejected batch wrote nothing

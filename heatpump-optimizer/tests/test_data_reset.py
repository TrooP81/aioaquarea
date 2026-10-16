"""RESET1–6 / CG-T1–8 unit evidence, not PostgreSQL/Timescale qualification.

Transaction fakes roll back on any exception. No sockets, device or Redis clients.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from packages.core import data_reset as reset
from packages.core.models import (
    AuditLogRecord,
    MLLifecycleStateRecord,
    PlanActionRecord,
    PriceScopeRecord,
    SettingRecord,
    ShowerEventRecord,
    SmartThingsToken,
)

NOW = dt.datetime(2026, 10, 16, 6, 22, tzinfo=dt.timezone.utc)


def scope():
    return {
        "context": "desktop-linux",
        "project": "isolated-reset-test",
        "compose_files": {
            identity: ["/isolated/compose.yml"]
            for identity in ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
        },
        "database": "isolated_reset",
        "database_oid": 123,
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "old_model_volume": "isolated_old",
        "new_model_volume": "isolated_new",
        "source_revision": "unit-fake",
        "owners": ["a" * 64, "b" * 64, "c" * 64],
        "maintenance_container": "d" * 64,
    }


class Rows(list):
    def all(self):
        return list(self)

    def first(self):
        return self[0] if self else None


class Database:
    def __init__(self):
        self.tables = {name: [] for name in reset.Base.metadata.tables}
        self.tables["plan_actions"] = [
            PlanActionRecord(
                id=11762,
                action_type="force_dhw_on",
                device_id="test-device",
                status="expired",
                payload_json="{}",
                scheduled_ts=NOW,
            ),
            PlanActionRecord(
                id=11763,
                reverts_action_id=11762,
                action_type="force_dhw_off",
                device_id="test-device",
                status="pending",
                payload_json="{}",
                scheduled_ts=NOW,
                result_json='{"reason":"old_context"}',
            ),
        ]
        self.tables["shower_events"] = [
            ShowerEventRecord(
                id=26,
                device_id="test-device",
                activation_action_id=11762,
                status="timeout_pending",
                started_at=NOW,
                expires_at=NOW,
                pre_shower_temp=55,
            ),
        ]
        self.tables["ml_lifecycle_state"] = [
            MLLifecycleStateRecord(id=1, epoch=str(uuid4()), maintenance_enabled=True),
        ]
        self.tables["settings"] = [
            SettingRecord(key="manual_cop_monitoring_enabled", value="true", updated_at=NOW),
            SettingRecord(key="learning_mode_since", value=NOW.isoformat(), updated_at=NOW),
            SettingRecord(key="ml_maintenance_enabled", value="true", updated_at=NOW),
        ]
        self.tables["smartthings_oauth_token"] = [
            SmartThingsToken(
                id=1,
                access_token="fake-access",
                refresh_token="fake-refresh",
                token_type="bearer",
                expires_at=NOW,
                updated_at=NOW,
            ),
        ]
        self.tables["price_scopes"] = [
            PriceScopeRecord(
                area="tb:" + "a" * 28,
                provider="tibber",
                home_id="A",
                currency="SEK",
                amount_basis="provider_total",
                interval_schema="hourly_duration_weighted_v1",
                tariff_schema_version="tibber_provider_total_v1",
                application_extra_fee=0,
            )
        ]
        self.wipes = 0
        self.fail_audit = False
        self.fail_wipe = False

    @asynccontextmanager
    async def sessions(self):
        before = copy.deepcopy(self.tables)
        try:
            yield self
        except BaseException:
            self.tables = before
            raise

    async def get(self, model, key):
        return next((row for row in self.tables[model.__tablename__] if row.id == key), None)

    async def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = self.tables[model.__tablename__]
        if model is AuditLogRecord:
            params = statement.compile().params
            actions = params["action_1"]
            rows = (
                [row for row in rows if row.action in actions]
                if isinstance(actions, tuple | list)
                else [row for row in rows if row.action == actions]
            )
            if isinstance(actions, tuple | list):
                rows = list(reversed(rows))
        return Rows(rows)

    async def execute(self, statement, *args):
        sql = str(statement)
        if sql.startswith("TRUNCATE TABLE"):
            assert "CONTINUE IDENTITY RESTRICT" in sql
            assert "CASCADE" not in sql and "RESTART" not in sql
            self.wipes += 1
            if self.fail_wipe:
                raise RuntimeError("injected dependency failure")
            for name in reset.WIPE_TABLES:
                self.tables[name] = []
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    async def scalar(self, statement):
        sql = str(statement)
        if "pg_stat_activity" in sql:
            return 0
        assert sql.startswith("SELECT count(*) FROM public.")
        return len(self.tables[sql.split('"')[1]])

    def add(self, row):
        if isinstance(row, AuditLogRecord) and self.fail_audit:
            raise RuntimeError("injected audit failure")
        self.tables[row.__tablename__].append(row)

    async def flush(self):
        pass


class Receipts:
    def __init__(self, fail=None):
        self.values = {}
        self.fail = fail

    def persist(self, operation, stage, value):
        if stage == self.fail:
            raise OSError("injected durable storage failure")
        key = (operation, stage)
        if key in self.values:
            assert self.values[key] == value
        self.values[key] = copy.deepcopy(value)


@pytest.fixture
def transaction_guards(monkeypatch):
    guard = AsyncMock()
    monkeypatch.setattr(reset, "_guard", guard)
    return guard


async def planned(db):
    return await reset.plan(
        db.sessions,
        AsyncMock(),
        scope=scope(),
        operation_uuid=str(uuid4()),
        operator="test-operator",
        statement="Only the exact historical tuple is invalid.",
        statement_at=NOW.isoformat(),
        device_id="test-device",
    )


async def apply(db, manifest, receipts=None, resource_guard=None):
    return await reset.apply(
        db.sessions,
        resource_guard or AsyncMock(),
        receipts or Receipts(),
        manifest,
        approved_digest=manifest["digest"],
    )


async def test_CG_T1_exact_retirement_is_administrative_atomic(transaction_guards):
    db = Database()
    manifest = await planned(db)
    db.fail_wipe = True
    with pytest.raises(reset.ResetStageFailure, match="retirement_receipt_durable"):
        await apply(db, manifest)
    source, restore = db.tables["plan_actions"]
    event = db.tables["shower_events"][0]
    assert source.status == "expired"
    assert restore.status == event.status == "operator_invalidated"
    assert restore.executed_at is None and event.recovered_at is None
    result = json.loads(restore.result_json)
    assert result["previous_result"] == {"reason": "old_context"}
    receipt = json.loads(db.tables["audit_log"][0].payload_json)
    assert receipt["target"] == [11763, 11762, 26]
    assert receipt["device_write"] is receipt["physical_state_verified"] is False
    assert receipt["reason"] == "reset_superseded"
    assert "verified" not in result and "success" not in result
    summary = await reset.reset_history_summary(db)
    assert summary["data_reset_at"] is None
    assert summary["historical_retirement"]["disposition"] == "operator_invalidated"


async def test_RESET2_RESET4_exact_wipe_preserves_every_retained_column(transaction_guards):
    db = Database()
    retained = {
        name: [reset.row_values(row) for row in db.tables[name]]
        for name in ("settings", "smartthings_oauth_token", "price_scopes")
    }
    manifest = await planned(db)
    result = await apply(db, manifest)
    assert result["new_epoch"] != manifest["previous_epoch"]
    for name in reset.WIPE_TABLES:
        assert len(db.tables[name]) == {"audit_log": 2, "ml_lifecycle_state": 1}.get(name, 0)
    for name, values in retained.items():
        assert [reset.row_values(row) for row in db.tables[name]] == values
    state = db.tables["ml_lifecycle_state"][0]
    assert state.maintenance_enabled is True
    assert state.lease_owner is None and state.lease_expires_at is None
    assert state.next_maintenance_at is None
    summary = await reset.reset_history_summary(db)
    assert summary["historical_retirement"]["physical_state_verified"] is False
    assert summary["data_reset_at"] == result["data_reset_at"]


@pytest.mark.parametrize(
    "drift",
    [
        "new_restore",
        "new_event",
        "claim",
        "executing",
        "payload",
        "linkage",
        "epoch",
        "source_status",
        "event_status",
        "orphan_activation",
    ],
)
async def test_CG_T3_T5_drift_never_wipes(transaction_guards, drift):
    db = Database()
    manifest = await planned(db)
    source, restore = db.tables["plan_actions"]
    if drift == "new_restore":
        db.tables["plan_actions"].append(
            PlanActionRecord(
                id=20000,
                reverts_action_id=11762,
                status="pending",
                device_id="other",
            )
        )
    elif drift == "new_event":
        db.tables["shower_events"].append(ShowerEventRecord(id=200, status="active"))
    elif drift == "claim":
        restore.safety_claim_token = uuid4()
    elif drift == "executing":
        restore.status = "executing"
    elif drift == "payload":
        restore.payload_json = '{"changed":true}'
    elif drift == "linkage":
        restore.reverts_action_id = 99
    elif drift == "epoch":
        db.tables["ml_lifecycle_state"][0].epoch = str(uuid4())
    elif drift == "source_status":
        source.status = "executed"
    elif drift == "event_status":
        db.tables["shower_events"][0].status = "recovered"
    elif drift == "orphan_activation":
        db.tables["plan_actions"].append(
            PlanActionRecord(
                id=20000,
                status="executed",
                action_type="zone_temp_boost",
            )
        )
    with pytest.raises(reset.ResetStageFailure):
        await apply(db, manifest)
    assert db.wipes == 0
    assert not db.tables["audit_log"]


async def test_CG_T6_replay_is_idempotent_and_altered_manifest_conflicts(transaction_guards):
    db = Database()
    manifest = await planned(db)
    receipts = Receipts()
    first = await apply(db, manifest, receipts)
    assert await apply(db, manifest, receipts) == first
    assert db.wipes == 1
    changed = {**manifest, "statement": "different attestation"}
    changed["digest"] = reset.manifest_digest(changed)
    with pytest.raises(reset.ResetStageFailure):
        await apply(db, changed, Receipts())
    assert db.wipes == 1
    assert len(db.tables["audit_log"]) == 2


@pytest.mark.parametrize(
    "failure,expected_stage,retired",
    [
        ("prepared", "preparation", False),
        ("audit", "preparation", False),
        ("retired", "retirement_committed_receipt_pending", True),
        ("reset", "reset_committed_receipt_pending", True),
    ],
)
async def test_CG_T7_receipt_audit_stage_failures(
    transaction_guards, failure, expected_stage, retired
):
    db = Database()
    manifest = await planned(db)
    receipts = Receipts(fail=failure)
    db.fail_audit = failure == "audit"
    with pytest.raises(reset.ResetStageFailure) as error:
        await apply(db, manifest, receipts)
    assert error.value.stage == expected_stage
    if failure == "reset":
        assert not db.tables["plan_actions"]
        receipts.fail = None
        await apply(db, manifest, receipts)
        assert db.wipes == 1
    else:
        assert db.wipes == 0
        assert db.tables["plan_actions"][1].status == (
            "operator_invalidated" if retired else "pending"
        )
        assert bool(db.tables["audit_log"]) == retired


async def test_CG_T5_resource_ownership_loss_after_retirement_stops_wipe(transaction_guards):
    db = Database()
    manifest = await planned(db)
    guard = AsyncMock(side_effect=[None, reset.ResetRefused("ownership lost")])
    with pytest.raises(reset.ResetStageFailure, match="retirement_receipt_durable"):
        await apply(db, manifest, resource_guard=guard)
    assert db.wipes == 0
    assert db.tables["plan_actions"][1].status == "operator_invalidated"


async def test_CG_T7_committed_audit_readback_failure_stops_before_wipe(
    transaction_guards, monkeypatch
):
    db = Database()
    manifest = await planned(db)
    original = reset._audit
    calls = 0

    async def unreadable_readback(*args):
        nonlocal calls
        calls += 1
        return None if calls == 3 else await original(*args)

    monkeypatch.setattr(reset, "_audit", unreadable_readback)
    with pytest.raises(reset.ResetStageFailure, match="retirement_committed_receipt_pending"):
        await apply(db, manifest)
    assert db.wipes == 0
    assert db.tables["plan_actions"][1].status == "operator_invalidated"
    assert len(db.tables["audit_log"]) == 1


def test_RESET1_manifest_rejects_wrong_scope_and_unapproved_target():
    manifest = {
        "version": 2,
        "operation_uuid": str(uuid4()),
        "scope": scope(),
        "operator": "test",
        "statement": "invalid",
        "statement_at": NOW.isoformat(),
        "device_id": "test-device",
        "previous_epoch": str(uuid4()),
        "target": [1, 2, 3],
        "fingerprint": "a" * 64,
        "previous_statuses": ["pending", "expired", "timeout_pending"],
    }
    manifest["digest"] = reset.manifest_digest(manifest)
    with pytest.raises(reset.ResetRefused, match="exact historical tuple"):
        reset.validate_manifest(manifest)
    manifest["target"] = list(reset.TARGET)
    manifest["scope"]["context"] = "default"
    manifest["digest"] = reset.manifest_digest(manifest)
    with pytest.raises(reset.ResetRefused, match="context"):
        reset.validate_manifest(manifest)


async def test_PF_T6_plan_emits_incompatible_v2_manifest(transaction_guards):
    manifest = await planned(Database())
    assert manifest["version"] == 2
    assert manifest["scope"]["compose_files"] == scope()["compose_files"]
    assert manifest["digest"] == reset.manifest_digest(manifest)


async def test_PF_T1_passive_scope_cannot_enter_locked_preparation(transaction_guards):
    guard = AsyncMock()
    with pytest.raises(reset.ResetRefused, match="resource scope"):
        await reset.plan(
            Database().sessions,
            guard,
            scope={"kind": "preliminary_read_only_target_preflight_NOT_EXECUTABLE"},
            operation_uuid=str(uuid4()),
            operator="test",
            statement="Passive information is not executable.",
            statement_at=NOW.isoformat(),
            device_id="test-device",
        )
    guard.assert_not_awaited()
    transaction_guards.assert_not_awaited()


@pytest.mark.parametrize(
    "drift", ["old_version", "stale_digest", "old_global_list", "old_approval"]
)
async def test_PF_T6_incompatible_or_changed_manifest_refuses_before_work(
    transaction_guards, drift
):
    db = Database()
    manifest = await planned(db)
    approved_digest = manifest["digest"]
    if drift == "old_version":
        manifest["version"] = 1
        manifest["digest"] = reset.manifest_digest(manifest)
    elif drift == "old_global_list":
        manifest["scope"]["compose_files"] = ["/isolated/compose.yml"]
        manifest["digest"] = reset.manifest_digest(manifest)
    else:
        manifest["scope"]["compose_files"][manifest["scope"]["owners"][0]].append(
            "/isolated/changed.yml"
        )
        if drift == "old_approval":
            manifest["digest"] = reset.manifest_digest(manifest)
    guard = AsyncMock(side_effect=AssertionError("validation must precede resource access"))
    receipts = SimpleNamespace(persist=MagicMock())
    with pytest.raises(reset.ResetRefused):
        await reset.apply(db.sessions, guard, receipts, manifest, approved_digest=approved_digest)
    guard.assert_not_awaited()
    receipts.persist.assert_not_called()
    assert db.wipes == 0


class GuardSession:
    def __init__(
        self,
        *,
        lock=True,
        sessions=0,
        unknown=False,
        dependency=False,
        address="127.0.0.1",
        port=5432,
    ):
        self.lock, self.sessions, self.unknown, self.dependency = (
            lock,
            sessions,
            unknown,
            dependency,
        )
        self.address, self.port = address, port
        self.sql = []

    async def scalar(self, sql, params=None):
        value = str(sql)
        self.sql.append(value)
        if "pg_try_advisory" in value:
            return self.lock
        if "pg_stat_activity" in value:
            return self.sessions
        return False

    async def scalars(self, sql):
        if "pg_class" in str(sql):
            return Rows(
                [*reset.WIPE_TABLES, *reset.RETAIN_TABLES] + (["surprise"] if self.unknown else [])
            )
        return Rows(["037"])

    async def execute(self, sql, params=None):
        self.sql.append(str(sql))
        if "current_database()" in str(sql):
            from ipaddress import ip_address

            # PostgreSQL inet::text carries the host mask; host(inet) does not.
            address = (
                self.address
                if "host(inet_server_addr())" in str(sql)
                else (f"{self.address}/{ip_address(self.address).max_prefixlen}")
            )
            return SimpleNamespace(one=lambda: ("isolated_reset", 123, address, self.port))
        return Rows([("other_schema", "unexpected")] if self.dependency else [])

    async def get(self, model, key):
        return SimpleNamespace(lease_owner=None, lease_expires_at=None)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"lock": False}, "owner"),
        ({"sessions": 1}, "sessions"),
        ({"unknown": True}, "tables"),
        ({"dependency": True}, "dependency"),
    ],
)
async def test_RESET5_guard_refuses_ownership_sessions_inventory_dependencies(kwargs, reason):
    session = GuardSession(**kwargs)
    with pytest.raises(reset.ResetRefused, match=reason):
        await reset._guard(session, scope(), "test-device")
    assert not any("TRUNCATE" in sql for sql in session.sql)


async def test_RESET5_guard_uses_same_device_key_and_bounded_exclusive_locks():
    session = GuardSession()
    await reset._guard(session, scope(), "test-device")
    assert any("ACCESS EXCLUSIVE MODE NOWAIT" in sql for sql in session.sql)
    assert "SET LOCAL lock_timeout = '2s'" in session.sql
    assert "SET LOCAL statement_timeout = '30s'" in session.sql


@pytest.mark.parametrize(
    "approved,address,port",
    [
        ("127.0.0.1", "127.0.0.10", 5432),
        ("127.0.0.1", "127.0.0.1", 5433),
        ("127.0.0.1", "::ffff:127.0.0.1", 5432),
        ("2001:db8::2", "2001:db8::20", 5432),
        ("2001:db8::2", "2001:db8::2", 5433),
    ],
)
async def test_RESET5_database_guard_refuses_different_exact_endpoint(approved, address, port):
    session = GuardSession(address=address, port=port)
    approved_scope = {**scope(), "server_address": approved}
    with pytest.raises(reset.ResetRefused, match="database identity drift"):
        await reset._guard(session, approved_scope, "test-device")
    assert not any("LOCK TABLE" in sql or "TRUNCATE" in sql for sql in session.sql)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1/31",
        "::1/127",
        "172.31.1.0/24",
        "2001:db8::/64",
        "db",
        "",
        None,
        2130706433,
        "fe80::1%eth0",
        "[::1]",
    ],
)
def test_RESET5_canonical_host_refuses_networks_names_and_scoped_aliases(value):
    with pytest.raises(reset.ResetRefused, match="unscoped IP literal"):
        reset.canonical_server_address(value)


async def test_RESET6_malformed_retirement_evidence_is_unavailable():
    db = Database()
    db.add(AuditLogRecord(action=reset.AUDIT_ACTIONS[1], payload_json='{"device_write":true}'))
    assert (await reset.reset_history_summary(db))["available"] is False


def test_REPAIR1_tool_has_no_device_or_redis_imports():
    import ast

    for module in (reset, __import__("packages.core.reset_operator", fromlist=["main"])):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(
            "aquarea" in name or "redis" in name or "services" in name for name in imports
        )

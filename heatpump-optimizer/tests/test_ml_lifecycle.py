"""Offline proofs of poller-owned model maintenance orchestration."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
import datetime as dt
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from packages.ml import lifecycle as module
from packages.ml.lifecycle import ArtifactReference, Lease, Lifecycle, LifecycleSnapshot


@pytest.fixture
def windows_directory_fsync(monkeypatch):
    """Exercise lifecycle behavior, not POSIX directory durability, on Windows."""
    if os.name != "nt":
        yield
        return
    # Windows cannot open directories through os.open for fsync. Keep real file
    # writes/fsync, signing, publication and verification; replace only this
    # platform-specific durability boundary in the explicitly requesting tests.
    directory_fsync = Mock()
    monkeypatch.setattr(module.safe_persistence, "_fsync_directory", directory_fsync)
    yield
    directory_fsync.assert_called()


def scalar(value):
    result = Mock()
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


def qualified_reference(component="thermal", capabilities=("indoor_heating",)):
    from packages.ml.forecast_quality import QUALIFICATION_POLICY_SCHEMA, native_scope
    from tests.test_forecast_quality import TestProspectiveNativeEvidence

    now = module._now()
    inputs = TestProspectiveNativeEvidence._inputs()
    inputs["schemas"]["artifact"] = module.LIFECYCLE_SCHEMAS[component]
    scope = native_scope(inputs)
    return ArtifactReference(
        "a" * 64,
        "epoch",
        component,
        module.LIFECYCLE_SCHEMAS[component],
        {"scope": scope, "device_id": "heatpump", "capabilities": list(capabilities)},
        "incumbent",
        7,
        now,
        now + dt.timedelta(hours=72),
        now,
        {
            **scope,
            "qualification_policy": QUALIFICATION_POLICY_SCHEMA,
            "horizons": [60],
            "capabilities": list(capabilities),
        },
    )


class Sessions:
    def __init__(self, results):
        self.session = AsyncMock()
        self.session.execute.side_effect = results
        self.committed = False

    @asynccontextmanager
    async def __call__(self):
        yield self.session
        self.committed = True

    def sql(self):
        return [
            str(call.args[0].compile(dialect=postgresql.dialect()))
            for call in self.session.execute.call_args_list
        ]


async def test_UR2_SR1_lifecycle_reads_borrow_snapshot_session(state):
    from packages.ml.forecast_quality import native_scope

    session = AsyncMock()
    result = {
        "pairs": 0,
        "device_day_clusters": 0,
        "issue_cohorts": 0,
        "last_valid_outcome_at": None,
        "pending_outcomes": 0,
        "horizons": {},
        "outdoor_bins": {},
        "exclusions": {},
    }
    session.execute.return_value = SimpleNamespace(
        mappings=lambda: SimpleNamespace(one=lambda: result)
    )
    factory = Mock(side_effect=AssertionError("independent lifecycle session"))
    authority = Lifecycle(factory)
    authority._read = AsyncMock(return_value=state)
    now = TestR06ThermalQualificationRoute.NOW
    inputs = TestR06ThermalQualificationRoute.native_inputs(now)
    reference = ArtifactReference(
        "a" * 64,
        state.epoch,
        "thermal",
        "thermal_v5",
        {
            "frozen_at": now.isoformat(),
            "device_id": "audit-device",
            "scope": native_scope(inputs),
            "horizons": [15, 30, 60],
            "capabilities": ["indoor_heating", "indoor_cooling"],
        },
        "candidate",
        state.revision,
    )
    assert await authority.read(session=session) is state
    evidence = await authority.reporting_evidence(reference, now=now, session=session)
    assert evidence["counts"]["pairs"] == 0 and evidence["as_of"] == now.isoformat()
    assert "ml_forecast_issues" in str(session.execute.await_args.args[0])
    factory.assert_not_called()
    session.commit.assert_not_awaited()
    session.close.assert_not_awaited()


async def test_evidence_page_progress_is_atomic_and_retries_are_fenced(state, lease):
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    counted = Mock()
    counted.one.return_value = (250, 250)
    sessions = Sessions([counted, Mock()])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=state)
    checkpoint = await lifecycle.begin_evidence_scan(
        lease,
        reference,
        key="scan",
        window_start=module._now() - dt.timedelta(days=14),
        window_end=module._now(),
    )
    assert checkpoint["high_water"] == checkpoint["expected_rows"] == 250
    assert checkpoint["cursor"] == 0 and checkpoint["complete"] is False
    assert "max(ml_forecast_issues.id)" in sessions.sql()[0]
    assert "issued_at >=" in sessions.sql()[0] and "issued_at <=" in sessions.sql()[0]
    snapshot = replace(state, checkpoints={"scan": checkpoint})
    lifecycle._read = AsyncMock(return_value=snapshot)
    page = {
        "checkpoint": checkpoint,
        "cursor": 1,
        "complete": False,
        "rows": [
            {
                "issue": {
                    "id": 1,
                    "epoch": lease.epoch,
                    "device_id": "heatpump",
                    "component": "demand",
                    "candidate_hash": reference.artifact_hash,
                    "cohort_key": "cohort",
                    "horizon_minutes": 60,
                }
            }
        ],
    }
    outcome = {
        "status": "incomplete",
        "exclusion_reason": "missing_target",
        "observed_at": None,
        "result": {},
    }
    sessions.session.execute.side_effect = [Mock(), RuntimeError("page commit failed")]
    sessions.committed = False
    with pytest.raises(RuntimeError, match="page commit failed"):
        await lifecycle.commit_evidence_page(lease, "scan", page, [outcome])
    assert sessions.committed is False
    sessions.session.execute.side_effect = [Mock(), Mock()]
    await lifecycle.commit_evidence_page(lease, "scan", page, [outcome])
    assert sessions.committed is True
    assert "ON CONFLICT (issue_id) DO UPDATE" in sessions.sql()[-2]
    progressed = {**checkpoint, "cursor": 1, "processed_rows": 1}
    lifecycle._read = AsyncMock(return_value=replace(state, checkpoints={"scan": progressed}))
    with pytest.raises(module.CASConflict, match="progress_conflict"):
        await lifecycle.commit_evidence_page(lease, "scan", page, [outcome])


async def test_default_hooks_publish_exact_fit_only_after_cas(state, lease, monkeypatch):
    import numpy as np
    from packages.ml.demand_model_core import build_demand_candidate
    from packages.ml.forecast_quality import check_exact_fit, native_scope
    from tests.test_forecast_quality import TestProspectiveNativeEvidence

    database = MemoryLifecycle(state)
    database.publish = AsyncMock(return_value=module.PublicationResult(False, reason="lease_lost"))
    owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=True))
    inputs = TestProspectiveNativeEvidence._inputs()
    start = module._now() - dt.timedelta(days=30)
    features = np.zeros((500, 10))
    features[:, 0] = np.tile(np.arange(-5.0, 20.0), 20)
    targets = 3.0 - 0.1 * features[:, 0]
    payload = build_demand_candidate(
        {
            "features": features,
            "targets": targets,
            "bounds": [
                (start + dt.timedelta(hours=index), start + dt.timedelta(hours=index + 1))
                for index in range(500)
            ],
            "device_id": "heatpump",
        }
    )
    checks = []

    def publish(restored, **options):
        options["round_trip_check"](restored)
        checks.append(options["manifest"])
        return SimpleNamespace(artifact_hash="a" * 64, manifest=options["manifest"])

    monkeypatch.setattr(module.safe_persistence, "publish_signed_artifact", publish)
    try:
        result = await owner.hooks["demand"].commit_candidate(
            lease,
            {
                "payload": payload,
                "inputs": inputs,
                "data_cutoff": module._now(),
                "horizons": (60, 180, 360, 720, 1440),
                "capabilities": ("demand",),
            },
            state,
        )
        assert result.committed is False and result.reason == "lease_lost"
        assert checks[0]["scope"] == native_scope(inputs)
        assert checks[0]["decision_offsets_hours"][0] == 360
        assert database.state.references == ()
        check_exact_fit("demand", payload)
    finally:
        await owner.close()


async def test_demand_preprocessing_uses_lifecycle_owned_worker(state, monkeypatch):
    from packages.ml import forecast_quality as quality
    from packages.ml.demand_model_core import DemandModel
    from tests.test_forecast_quality import TestProspectiveNativeEvidence
    from tests.test_ml import TestDemandModel, _mock_get_session

    inputs = TestProspectiveNativeEvidence._inputs()
    inputs["device_id"] = "one"
    inputs["weather_provider"] = "manual"
    monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(
        "packages.ml.demand_model_core.get_session",
        _mock_get_session(TestDemandModel._lifecycle_preprocessing_rows()),
    )
    monkeypatch.setattr("packages.ml.demand_model_core.get_user_tz", AsyncMock(return_value="UTC"))
    threads = []
    original = DemandModel._preprocess_materialized

    def checked(data):
        threads.append(threading.current_thread().name)
        return original(data)

    monkeypatch.setattr(DemandModel, "_preprocess_materialized", staticmethod(checked))
    owner = module.MaintenanceRunner(
        MemoryLifecycle(state), enabled_reader=AsyncMock(return_value=True)
    )
    try:
        result = await owner.hooks["demand"].materialize(module._request(request_row()))
        assert result["data"]["device_id"] == "one"
        assert threads and all(name.startswith("ml-maintenance") for name in threads)
        assert owner._executor is not None
    finally:
        await owner.close()


def test_reference_expiry_is_outcome_based_and_scope_bound(state):
    from packages.ml.forecast_quality import NATIVE_POLICY_SCHEMA, QUALIFICATION_POLICY_SCHEMA

    now = module._now()
    scope = {"policy_schema": NATIVE_POLICY_SCHEMA, "sensor_fingerprint": "sensor"}
    reference = ArtifactReference(
        "a" * 64,
        state.epoch,
        "demand",
        "demand_lifecycle_v1",
        {"scope": scope},
        "incumbent",
        7,
        now,
        now + dt.timedelta(hours=72),
        now,
        {
            **scope,
            "qualification_policy": QUALIFICATION_POLICY_SCHEMA,
            "horizons": [60],
            "capabilities": ["demand"],
        },
    )
    assert module.reference_is_qualified(reference, now=now, scope=scope)
    assert not module.reference_is_qualified(
        reference, now=now + dt.timedelta(hours=72), scope=scope
    )
    assert not module.reference_is_qualified(
        reference, now=now, scope={**scope, "sensor_fingerprint": "changed"}
    )
    assert not module.reference_is_qualified(
        replace(reference, role="candidate"), now=now, scope=scope
    )


async def test_qualified_incumbent_never_adds_unscheduled_looks(state, monkeypatch):
    from packages.ml import forecast_quality as quality
    from tests.test_forecast_quality import TestProspectiveNativeEvidence

    now = module._now()
    frozen_at = now - dt.timedelta(days=90)
    reference = qualified_reference("demand", ("demand",))
    manifest = {
        **reference.manifest,
        "frozen_at": frozen_at.isoformat(),
        "qualification_policy": quality.QUALIFICATION_POLICY_SCHEMA,
        "horizons": [60],
        "frozen_incumbent_hash": None,
    }
    reference = replace(reference, manifest=manifest)
    checkpoints = {
        f"decision:{reference.artifact_hash}:{index}": {"status": "evaluated"}
        for index in range(len(quality.DECISION_OFFSETS_HOURS))
    }
    checkpoints["collection:" + reference.artifact_hash] = {"last_slot": 360, "complete": True}
    database = MemoryLifecycle(replace(state, references=(reference,), checkpoints=checkpoints))
    database.begin_evidence_scan = AsyncMock()
    database.evidence_page = AsyncMock(return_value={"checkpoint": {"complete": True}})
    database.claim_decision = AsyncMock(side_effect=AssertionError("unscheduled look"))
    database.decision_evidence = AsyncMock(side_effect=AssertionError("unscheduled scoring"))
    inputs = TestProspectiveNativeEvidence._inputs()
    inputs["schemas"]["artifact"] = module.LIFECYCLE_SCHEMAS["demand"]
    monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(module, "_now", lambda: now)
    owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=True))
    try:
        lease = await database.acquire("owner")
        await owner.hooks["demand"].review_candidate(lease, reference)
        database.claim_decision.assert_not_awaited()
        database.decision_evidence.assert_not_awaited()
    finally:
        await owner.close()


async def test_rejected_candidate_preserves_only_healthy_qualified_demand(monkeypatch):
    import numpy as np
    from packages.ml.demand_model_core import DemandModel

    reference = qualified_reference("demand", ("demand",))
    model = DemandModel()
    incumbent = Mock()
    incumbent.predict.return_value = np.asarray([2.0])
    model._model = incumbent
    model._qualified_reference = reference
    model._prepare_data = AsyncMock(return_value=(np.zeros((200, 10)), np.ones(200)))
    monkeypatch.setattr("packages.ml.demand_model_core.time_series_cv_mae", lambda *_: (0.98, 0.01))
    monkeypatch.setattr("packages.ml.demand_model_core.time_series_cv_naive_mae", lambda *_: 1.0)
    result = await model.train()
    assert result["status"] == "no_skill"
    assert model.is_trained is True
    assert model._model is incumbent
    assert model.predict_hourly([{"temperature": 5.0}], hours=1) == [2.0]
    model._qualified_reference = replace(
        reference, qualification_expires_at=module._now() - dt.timedelta(seconds=1)
    )
    result = await model.train()
    assert result["status"] == "no_skill"
    assert model.is_trained is False
    assert model.predict_hourly([{"temperature": 5.0}], hours=1) == [2.5]


def test_loaded_temperature_heads_expire_without_a_database_refresh(monkeypatch):
    from packages.ml.comfort_model import ComfortModel
    from packages.ml.thermal import ThermalModel

    now = module._now()
    comfort = ComfortModel()
    comfort._model = Mock()
    comfort._qualified_reference = qualified_reference("comfort", ("indoor",))
    thermal = ThermalModel()
    thermal._qualified_reference = qualified_reference()
    thermal.params.last_calibrated = now
    thermal.params.calibration_status["indoor_heating"] = "validated"
    thermal.params.indoor_heating_rate = 0.9
    assert comfort.is_trained is True
    assert thermal.confidence_for("indoor_heating") == "learned"
    monkeypatch.setattr(module, "_now", lambda: now + dt.timedelta(hours=73))
    assert comfort.is_trained is False
    assert thermal.confidence_for("indoor_heating") == "default"
    assert thermal._indoor_heating_rate(5.0) == pytest.approx(0.55)


@pytest.fixture
def state():
    return LifecycleSnapshot(
        "epoch", 7, 8, True, "poller", module._now() + dt.timedelta(seconds=90)
    )


@pytest.fixture
def lease(state):
    return Lease(
        state.epoch, state.lease_owner, state.fence, state.revision, state.lease_expires_at
    )


def request_row(status="queued"):
    return SimpleNamespace(
        id="request-1",
        epoch="epoch",
        component="demand",
        source="api",
        dedup_key="manual",
        status=status,
        requested_at=module._now(),
        attempted_at=None,
        finished_at=None,
        details={},
    )


@pytest.mark.parametrize("family", ["tank_", "indoor_"])
@pytest.mark.parametrize("identity", ["legacy-dedup", "structured"])
async def test_R06_enqueue_and_claim_preserve_family_dto(state, lease, family, identity):
    from packages.core.models import MLMaintenanceRequestRecord

    dedup_key = f"thermal-family:{family}:{'a' * 64}" if identity == "legacy-dedup" else "opaque"
    details = {"family": family}
    row_values = {
        "id": "r06-persisted-request",
        "epoch": state.epoch,
        "component": "thermal",
        "source": "scheduled",
        "requested_at": module._now(),
        "dedup_key": dedup_key,
        "details": details,
    }
    row = MLMaintenanceRequestRecord(**row_values, status="queued")
    sessions = Sessions([Mock(), scalar(state.epoch), scalar(row)])
    authority = Lifecycle(sessions)
    options = {"family": family} if identity == "structured" else {}

    queued = await authority.enqueue("thermal", "scheduled", dedup_key=dedup_key, **options)

    assert isinstance(queued, module.MaintenanceRequest)
    assert queued.details == details and queued.dedup_key == dedup_key
    assert (
        sessions.session.execute.call_args_list[-1].args[0].compile().params["details"] == details
    )
    claimed_row = MLMaintenanceRequestRecord(
        **row_values, status="running", fence=lease.fence, attempted_at=module._now()
    )
    sessions.session.execute.side_effect = [scalar(row), scalar(claimed_row)]
    authority._lock = AsyncMock()
    authority._read = AsyncMock(return_value=state)

    claimed = await authority.claim_request(lease, "thermal")

    assert isinstance(claimed, module.MaintenanceRequest)
    assert claimed.status == "running" and claimed.dedup_key == dedup_key
    assert claimed.details == details
    assert (
        sessions.session.execute.call_args_list[-1].args[0].compile().params["details"] == details
    )


@pytest.mark.parametrize("family", ["unknown", "", True, 1, ["indoor_"]])
async def test_R06_invalid_enqueue_family_is_rejected_before_storage(family):
    sessions = Sessions([])
    with pytest.raises(ValueError, match="family"):
        await Lifecycle(sessions).enqueue("thermal", "scheduled", family=family)
    sessions.session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "details,dedup_key",
    [
        ({"family": "unknown"}, "opaque"),
        ({"family": True}, "opaque"),
        ({"family": ["indoor_"]}, "opaque"),
        ({"family": None}, "opaque"),
        ({}, "thermal-family:unknown:fit"),
        ({}, "thermal-family:indoor_"),
        ({"family": "indoor_"}, "thermal-family:tank_:fit"),
    ],
)
async def test_R06_unknown_or_conflicting_request_family_never_materializes(
    monkeypatch, details, dedup_key
):
    from packages.ml import forecast_quality as quality

    inputs = AsyncMock(return_value=None)
    monkeypatch.setattr(quality, "materialize_native_inputs", inputs)
    runner = SimpleNamespace(lifecycle=Mock(), _enabled=AsyncMock(return_value=True))
    hook = module.native_component_hooks(runner, None)["thermal"]
    request = module.MaintenanceRequest(
        "r06-invalid",
        "epoch",
        "thermal",
        "scheduled",
        "running",
        module._now(),
        details=details,
        dedup_key=dedup_key,
    )

    assert await hook.materialize(request) is None
    inputs.assert_not_awaited()


async def test_explicit_disabled_request_is_durable_and_deduplicated():
    row = request_row()
    sessions = Sessions([Mock(), scalar("epoch"), scalar(row)])
    lifecycle = Lifecycle(sessions)
    result = await lifecycle.enqueue("demand", "api", dedup_key="manual")
    assert sessions.committed
    assert result.response(False)["status"] == "queued"
    assert result.response(False)["message"] == "maintenance disabled"
    assert result.response(False)["control_ready"] is False
    sql = sessions.sql()
    assert "ON CONFLICT (id) DO NOTHING" in sql[0]
    assert "FOR UPDATE" in sql[1]
    assert "ON CONFLICT (epoch, component, dedup_key) DO NOTHING" in sql[2]
    sessions.session.execute.side_effect = [Mock(), scalar("epoch"), scalar(None), scalar(row)]
    assert (await lifecycle.enqueue("demand", "api", dedup_key="manual")).id == result.id
    assert all(not statement.startswith("UPDATE ") for statement in sessions.sql())


async def test_automatic_request_disabled_does_not_touch_storage(monkeypatch):
    enabled = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "get_bool_setting", enabled)
    lifecycle = AsyncMock()
    assert (
        await module.enqueue_maintenance_request(
            "demand", "startup", automatic=True, lifecycle=lifecycle
        )
        is None
    )
    enabled.assert_awaited_once_with("ml_maintenance_enabled")
    lifecycle.prepare_maintenance.assert_not_awaited()
    lifecycle.enqueue.assert_not_awaited()


@pytest.mark.parametrize("stored", [None, "false"])
async def test_disabled_owner_prepare_never_initializes(stored):
    record = SimpleNamespace(value=stored) if stored is not None else None
    sessions = Sessions([scalar(record)])
    assert await Lifecycle(sessions).prepare_maintenance() is None
    assert len(sessions.sql()) == 1
    assert sessions.sql()[0].startswith("SELECT ")


async def test_claim_persists_daily_demand_attempt_before_running(state, lease):
    sessions = Sessions([scalar(request_row()), scalar(1), scalar(request_row("running"))])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=state)
    assert (await lifecycle.claim_request(lease, "demand")).status == "running"
    assert sessions.committed
    sql = sessions.sql()
    assert "last_demand_attempt_at=clock_timestamp()" in sql[1]
    assert "last_demand_attempt_at IS NULL" in sql[1]
    assert "last_demand_attempt_at <= clock_timestamp() -" in sql[1]
    assert (
        dt.timedelta(hours=24)
        in sessions.session.execute.call_args_list[1].args[0].compile().params.values()
    )
    assert "status=" in sql[2] and "attempted_at=clock_timestamp()" in sql[2]


async def test_daily_demand_guard_does_not_start_another_attempt(state, lease):
    sessions = Sessions([scalar(request_row()), scalar(None)])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=state)
    assert await lifecycle.claim_request(lease, "demand") is None
    assert len(sessions.sql()) == 2
    assert all("UPDATE ml_maintenance_requests" not in statement for statement in sessions.sql())


async def test_frozen_candidate_is_not_replaced(state, lease):
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    sessions = Sessions([])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=replace(state, references=(candidate,)))
    assert await lifecycle.claim_request(lease, "demand") is None
    sessions.session.execute.assert_not_awaited()


@pytest.mark.parametrize("due", [None, 1])
async def test_review_cadence_is_persisted_under_the_lease(lease, due):
    sessions = Sessions([scalar(due), Mock()])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    assert await lifecycle.begin_cycle(lease) is (due is not None)
    sql = sessions.sql()[0]
    assert "next_maintenance_at <= clock_timestamp()" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
    assert (
        dt.timedelta(hours=6)
        in sessions.session.execute.call_args_list[0].args[0].compile().params.values()
    )


async def test_jittered_admission_uses_fixed_utc_slot_grid(lease):
    sessions = Sessions([scalar(1), Mock()])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    assert await lifecycle.begin_cycle(lease)
    statement = sessions.session.execute.call_args_list[0].args[0]
    assert "date_bin(" in sessions.sql()[0]
    assert dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) in statement.compile().params.values()


async def test_collection_slots_recover_after_recorded_gap(state, lease, monkeypatch):
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    snapshot = replace(state, references=(reference,))
    sessions = Sessions([])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    monkeypatch.setattr(module.safe_persistence, "_require_publication_owner", Mock())
    recorded = []
    for slot in (0, 2, 3):
        lifecycle._read = AsyncMock(return_value=snapshot)
        sessions.session.execute.side_effect = [scalar(slot + 1), Mock(), Mock()]
        issue = {
            "epoch": lease.epoch,
            "device_id": "heatpump",
            "component": reference.component,
            "candidate_hash": reference.artifact_hash,
            "cohort_key": f"slot-{slot}",
            "horizon_minutes": 60,
            "issued_at": module._now() + slot * module.MAINTENANCE_CADENCE,
        }
        await lifecycle.record_native_issues(lease, reference, [issue], slot=slot)
        checkpoints = sessions.session.execute.call_args.args[0].compile().params["checkpoints"]
        recorded.append(checkpoints["collection:" + reference.artifact_hash])
        snapshot = replace(snapshot, checkpoints=checkpoints)
    assert [row["complete"] for row in recorded] == [True, False, True]
    assert [row["gaps"] for row in recorded] == [0, 1, 1]
    assert recorded[-1]["last_slot"] == 3
    assert recorded[-1]["collected_slots"] == 3
    assert recorded[-1]["expected_slots"] == 4
    inserts = [sql for sql in sessions.sql() if sql.startswith("INSERT INTO ml_forecast_issues")]
    assert len(inserts) == 3


async def test_native_collection_uses_utc_slots_without_backdating(state, monkeypatch):
    from packages.ml import forecast_quality as quality
    from tests.test_forecast_quality import TestProspectiveNativeEvidence

    frozen_at = dt.datetime(2026, 10, 1, 0, 5, tzinfo=dt.timezone.utc)
    now = dt.datetime(2026, 10, 1, 12, 1, tzinfo=dt.timezone.utc)
    inputs = TestProspectiveNativeEvidence._inputs()
    inputs["schemas"]["artifact"] = module.LIFECYCLE_SCHEMAS["demand"]
    reference = qualified_reference("demand", ("demand",))
    reference = replace(
        reference,
        role="candidate",
        manifest={
            **reference.manifest,
            "scope": quality.native_scope(inputs),
            "sensor_basis": inputs["sensor_basis"],
            "frozen_at": frozen_at.isoformat(),
            "qualification_policy": quality.QUALIFICATION_POLICY_SCHEMA,
            "horizons": [60],
            "frozen_incumbent_hash": None,
        },
    )
    database = MemoryLifecycle(replace(state, references=(reference,)))
    database.load_committed = AsyncMock(return_value={})
    database.record_native_issues = AsyncMock()
    database.begin_evidence_scan = AsyncMock()
    database.evidence_page = AsyncMock(return_value={"checkpoint": {"complete": True}})
    monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(module, "_now", lambda: now)
    owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=True))
    owner._fit = AsyncMock(return_value=SimpleNamespace(rows=lambda: [{"issued_at": now}]))
    try:
        await owner.hooks["demand"].review_candidate(await database.acquire("owner"), reference)
        assert database.record_native_issues.await_args.kwargs["slot"] == 2
        assert database.record_native_issues.await_args.args[2] == [{"issued_at": now}]
    finally:
        await owner.close()


async def test_reset_without_authority_has_zero_dml():
    sessions = Sessions([scalar(None)])
    assert await module.invalidate_lifecycle(sessions.session) is None
    assert len(sessions.sql()) == 1
    assert sessions.sql()[0].startswith("SELECT ")
    assert "FOR UPDATE" in sessions.sql()[0]


class MemoryLifecycle:
    def __init__(self, state):
        self.state = state
        self.clock = module._now()
        self.next_cycle = None
        self.last_demand = None
        self.attempts = []
        self.released = False
        self.lost = False
        self.renewed = asyncio.Event()
        self.finished = []
        self.queue = {}
        self.prune_evidence = AsyncMock(return_value={"issues": 0, "outcomes": 0, "requests": 0})
        self.prune_artifacts = AsyncMock(return_value=[])
        self.record_artifact_unavailability = AsyncMock()
        self.clear_artifact_unavailability = AsyncMock()
        self.invalidate_component = AsyncMock()

    async def prepare_maintenance(self):
        return self.state

    async def acquire(self, owner):
        self.state = replace(self.state, lease_owner=owner)
        return Lease(
            self.state.epoch,
            owner,
            self.state.fence,
            self.state.revision,
            self.state.lease_expires_at,
        )

    async def begin_cycle(self, lease):
        if self.next_cycle is not None and self.next_cycle > self.clock:
            return False
        self.next_cycle = self.clock + dt.timedelta(hours=6)
        return True

    async def enqueue(self, component, source, **kwargs):
        row = replace(module._request(request_row()), component=component, source=source)
        self.queue[component] = row
        return row

    async def read(self):
        return self.state

    async def renew(self, lease):
        if self.lost:
            raise module.LeaseLost("lease_lost")
        self.renewed.set()
        return lease

    async def claim_request(self, lease, component):
        if component == "demand":
            if self.last_demand is not None and self.clock - self.last_demand < dt.timedelta(
                hours=24
            ):
                return None
            self.last_demand = self.clock
        self.attempts.append((component, self.clock))
        return self.queue[component]

    async def defer_component(self, lease, component, reason):
        self.queue[component] = replace(self.queue[component], details={"reason": reason})

    async def finish_request(self, lease, request_id, status, details):
        self.finished.append((status, details))

    async def release(self, lease):
        self.released = True


def hooks(*, fit=None, commit=None, review=None):
    return module.ComponentHooks(
        materialize=AsyncMock(return_value={"values": (1, 2, 3)}),
        fit=fit or Mock(return_value={"fitted": True}),
        commit_candidate=commit or AsyncMock(return_value=module.PublicationResult(True, 9)),
        review_candidate=review,
    )


class TestR06ThermalQualificationRoute:
    NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)

    @classmethod
    def cohort(cls):
        from packages.core.models import DeviceStatusRecord, IndoorTempReading, WeatherRecord

        records, readings, weather = [], [], []
        temperature = 21.0
        start = cls.NOW - dt.timedelta(hours=96)
        for index in range(96 * 12 + 1):
            timestamp = start + dt.timedelta(minutes=5 * index)
            heating = (index * 5 // 60) % 6 < 3
            records.append(
                DeviceStatusRecord(
                    ts=timestamp,
                    device_id="audit-device",
                    space_heating_active=heating,
                    operation_status=1,
                    mode="1",
                    direction="PUMP",
                    pump_duty=1,
                    device_action="HEATING" if heating else "IDLE",
                    defrost_active=False,
                    zone1_operation_status=1,
                    zone2_operation_status=0,
                    zone1_temp=35.0,
                    zone1_target_temp=40.0,
                    outdoor_temp=5.0,
                    tank_temp=45.0,
                    native_policy_provenance={},
                    tank_target_temp=50.0,
                )
            )
            readings.append(
                IndoorTempReading(
                    timestamp=timestamp,
                    temperature=round(temperature, 4),
                    device_id="selected-room",
                    is_stale=False,
                )
            )
            temperature += (0.6 if heating else -0.4) * 5 / 60
            if index % 12 == 0:
                weather.append(
                    WeatherRecord(
                        ts=timestamp,
                        source="open-meteo",
                        temperature=5.0,
                        wind_speed=3.0,
                        irradiance=0.0,
                        precipitation=0.0,
                        humidity=60.0,
                        cloud_cover=0.5,
                        forecast_issued_at=timestamp - dt.timedelta(hours=1),
                    )
                )
        return records, readings, weather

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_UR3_SR2_rejection_counts_preserved_by_existing_owner(self, state, enabled):
        from packages.ml import thermal

        records, readings, weather = self.cohort()
        data = {
            "records": records[:40],
            "readings": readings[:40],
            "weather": weather,
            "device_id": "audit-device",
            "data_cutoff": self.NOW,
        }
        materialized = {
            "data": data,
            "inputs": self.native_inputs(self.NOW),
            "requested_family": "indoor_",
            "diagnostics": {},
        }
        database = MemoryLifecycle(state)
        owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=enabled))
        real_fit = owner.hooks["thermal"].fit
        fit = Mock(wraps=real_fit)
        commit = AsyncMock(side_effect=AssertionError("rejected/disabled candidate publication"))
        materialize = AsyncMock(return_value=materialized)
        owner.hooks = {"thermal": module.ComponentHooks(materialize, fit, commit)}
        try:
            result = await owner.run()
        finally:
            await owner.close()
        commit.assert_not_awaited()
        if not enabled:
            assert result["status"] == "disabled"
            fit.assert_not_called()
            materialize.assert_not_awaited()
            assert database.finished == []
        else:
            fit.assert_called_once_with(materialized)
            assert result["components"]["thermal"] == "queued"
            status, details = database.finished[0]
            assert status == "queued" and details["reason"] == "insufficient_training_data"
            diagnostics = details["readiness_rejection"]
            assert diagnostics["requested_family"] == "indoor_"
            measured = diagnostics["components"]["indoor_heating"]
            expected = thermal._rate_sample_counts(
                thermal._extract_indoor_rate_samples(readings[:40], records[:40], weather)[0]
            )
            assert (measured["total"], measured["train"], measured["holdout"]) == (
                expected["total"],
                expected["train"],
                expected["holdout"],
            )
            assert measured["train"] < 20 and measured["holdout"] >= 3
            assert "holdout_targets" not in measured["validation"]

    async def test_SC006_complete_indoor_cohort_has_automatic_qualification_route(
        self, state, monkeypatch
    ):
        import pickle
        from packages.core import settings_service
        from packages.core.models import DeviceStatusRecord, IndoorTempReading, WeatherRecord
        from packages.ml import forecast_quality, thermal

        records, readings, weather = self.cohort()
        future = WeatherRecord(
            ts=self.NOW + dt.timedelta(hours=1),
            source="open-meteo",
            temperature=99.0,
            forecast_issued_at=self.NOW + dt.timedelta(minutes=1),
        )

        async def execute(statement):
            entity = statement.column_descriptions[0]["entity"]
            timestamps = [
                value
                for value in statement.compile().params.values()
                if isinstance(value, dt.datetime)
            ]
            lower, upper = min(timestamps), max(timestamps)
            if entity is DeviceStatusRecord:
                rows = [row for row in records if lower <= row.ts <= upper]
            elif entity is IndoorTempReading:
                rows = [row for row in readings if lower <= row.timestamp <= upper]
            elif entity is WeatherRecord:
                rows = [
                    row
                    for row in weather + [future]
                    if lower <= row.ts <= upper and row.forecast_issued_at <= self.NOW
                ]
            else:
                rows = []
            result = Mock()
            result.scalars.return_value.all.return_value = rows
            return result

        sessions = Sessions([])
        sessions.session.execute.side_effect = execute
        monkeypatch.setattr(thermal, "get_session", sessions)
        monkeypatch.setattr(forecast_quality, "get_session", sessions)
        monkeypatch.setattr(
            settings_service,
            "get_all_settings",
            AsyncMock(
                return_value={
                    "comfort_reference_sensor_id": "selected-room",
                    "smartthings_device_ids": "selected-room",
                    "weather_provider": "open-meteo",
                    "price_provider": "manual",
                    "timezone": "UTC",
                }
            ),
        )
        monkeypatch.setattr(module, "_now", lambda: self.NOW)
        owner = module.MaintenanceRunner(
            MemoryLifecycle(state), enabled_reader=AsyncMock(return_value=True)
        )
        try:
            hook = owner.hooks["thermal"]
            materialized = await hook.materialize(None)
            assert materialized is not None
            payload = thermal.build_thermal_candidate(materialized["data"])
            assert payload is not None
            assert set(payload["declared_components"]) == {"indoor_heating", "indoor_cooling"}
            forecast_quality.check_exact_fit("thermal", pickle.loads(pickle.dumps(payload)))
            assert all(row.temperature != 99.0 for row in materialized["data"]["weather"])
            assert (
                thermal.build_thermal_candidate({**materialized["data"], "records": records[:2]})
                is None
            )
            fitted = hook.fit(materialized)
            assert fitted is not None, "validated indoor heads must reach automatic publication"
            assert set(fitted["capabilities"]) == {"indoor_heating", "indoor_cooling"}
            assert fitted["inputs"]["sensor_basis"]["sensor_ids"] == ["selected-room"]
            assert fitted["inputs"]["schemas"]["input"] == "frozen_native_inputs_v2"
        finally:
            await owner.close()

    @pytest.mark.parametrize("rejection", ["records", "partition", "no_benefit", "family"])
    async def test_UR3_SR2_rejection_counts_preserved(self, state, monkeypatch, rejection):
        from packages.ml import thermal

        records, readings, weather = self.cohort()
        if rejection == "records":
            records = records[:2]
        elif rejection == "partition":
            records, readings = records[:40], readings[:40]
        elif rejection == "no_benefit":
            for row in readings:
                row.temperature = 20.0
        data = {
            "records": records,
            "readings": readings,
            "weather": weather,
            "data_cutoff": self.NOW,
            "device_id": "audit-device",
        }
        diagnostics = {}
        payload = thermal.build_thermal_candidate(data, diagnostics=diagnostics)
        expected = thermal.ThermalModel._extract_indoor_rate_samples(readings, records, weather)[0]
        counts = diagnostics["components"]["indoor_heating"]
        train, holdout = thermal.ThermalModel._partition_rate_samples(expected)
        assert (counts["total"], counts["train"], counts["holdout"]) == (
            len(expected),
            len(train),
            len(holdout),
        )
        assert diagnostics["source_records"] == len(records)
        if rejection == "family":
            assert payload is not None
            owner = module.MaintenanceRunner(
                MemoryLifecycle(state), enabled_reader=AsyncMock(return_value=True)
            )
            materialized = {
                "data": data,
                "inputs": self.native_inputs(self.NOW),
                "requested_family": "tank_",
                "input_families": {"indoor_": self.native_inputs(self.NOW)},
            }
            try:
                assert owner.hooks["thermal"].fit(materialized) is None
            finally:
                await owner.close()
            assert materialized["diagnostics"]["reason"] == "requested_family_unvalidated"
            assert materialized["diagnostics"]["requested_family"] == "tank_"
        else:
            assert payload is None
            assert (
                diagnostics["reason"]
                == {
                    "records": "insufficient_status_records",
                    "partition": "insufficient_training_data",
                    "no_benefit": "rejected_no_skill_over_baseline",
                }[rejection]
            )
            if rejection == "no_benefit":
                assert counts["train"] >= 20 and counts["holdout"] >= 3
                assert counts["fit_status"] == "rejected_no_skill_over_baseline"
                assert "holdout_targets" not in counts["validation"]

    @pytest.mark.usefixtures("windows_directory_fsync")
    async def test_UR3_SR2_native_dto_to_qualification_no_incumbent(self, state, monkeypatch):
        from packages.core.models import MLMaintenanceRequestRecord
        from packages.ml import thermal

        records, readings, weather = self.cohort()
        heating, cooling = thermal.ThermalModel._extract_indoor_rate_samples(
            readings, records, weather
        )
        assert all(
            thermal._rate_sample_counts(samples)["remaining_train"] == 0
            for samples in (heating, cooling)
        )
        assert state.reference("thermal") is None
        row = MLMaintenanceRequestRecord(
            id="ur3-family",
            epoch=state.epoch,
            component="thermal",
            source="seasonal",
            status="running",
            fence=state.fence,
            requested_at=self.NOW,
            attempted_at=self.NOW,
            details={"family": "indoor_"},
            dedup_key="ur3-scoped-family",
        )
        request = module._request(row)
        assert module._thermal_request_family(request.details, request.dedup_key) == "indoor_"
        await self.test_signed_indoor_candidate_issues_and_evaluates_each_head(state, monkeypatch)

    @classmethod
    def native_inputs(cls, now, family="indoor_"):
        from packages.ml.comfort_model import SENSOR_SELECTION_SCHEMA
        from tests.test_forecast_quality import TestProspectiveNativeEvidence

        inputs = TestProspectiveNativeEvidence._inputs()
        shift = now - TestProspectiveNativeEvidence.BASE
        for row in inputs["status_history"]:
            row["ts"] = (dt.datetime.fromisoformat(row["ts"]) + shift).isoformat()
            row["device_id"] = "audit-device"
        for row in inputs["weather"]:
            row["ts"] = (dt.datetime.fromisoformat(row["ts"]) + shift).isoformat()
            row["forecast_issued_at"] = now.isoformat()
        basis = (
            {
                "kind": "heatpump_temperature",
                "sensor_ids": ["audit-device"],
                "field": "tank_temp",
                "resolution_c": 1.0,
            }
            if family == "tank_"
            else {
                "kind": "reference",
                "sensor_ids": ["selected-room"],
                "resolution_c": 0.1,
                "selection_schema": SENSOR_SELECTION_SCHEMA,
                "selected_sensor_ids": ["selected-room"],
            }
        )
        inputs.update(
            {
                "issued_at": now.isoformat(),
                "device_id": "audit-device",
                "capability": family + "cooling",
                "sensor_basis": basis,
                "sensor_history": [
                    {
                        "timestamp": now.isoformat(),
                        "temperature": 45.0 if family == "tank_" else 21.0,
                    }
                ],
                "indoor_temperature": 21.0,
                "indoor_trend": 0.0,
                "tank_temperature": 45.0,
                "reference_sensor_id": "selected-room",
                "configured_sensor_ids": ["selected-room"],
                "weather_provider": "open-meteo",
                "timezone": "UTC",
                "schemas": {
                    "input": "frozen_native_inputs_v1"
                    if family == "tank_"
                    else "frozen_native_inputs_v2",
                    "artifact": module.LIFECYCLE_SCHEMAS["thermal"],
                    "weather_provider": "open-meteo",
                    "price_provider": "manual",
                },
            }
        )
        inputs["timer"]["fetched_at"] = now.isoformat()
        return inputs

    @classmethod
    def prospective_pairs(cls, candidate_hash, scope):
        pairs = []
        for day in range(14):
            for cohort in range(4):
                issued = cls.NOW + dt.timedelta(days=day, hours=cohort * 6)
                observed = 20.0 + (day % 4) * 0.6 + cohort * 0.5
                capability = "indoor_heating" if cohort % 2 == 0 else "indoor_cooling"
                for horizon in (15, 30, 60):
                    target = issued + dt.timedelta(minutes=horizon)
                    pairs.append(
                        {
                            "device_id": "audit-device",
                            "canonical_key": f"{day}:{cohort}",
                            "cohort_key": f"{day}:{cohort}",
                            "horizon_minutes": horizon,
                            "issued_at": issued.isoformat(),
                            "target_at": target.isoformat(),
                            "observed_at": target.isoformat(),
                            "status": "valid",
                            "candidate_hash": candidate_hash,
                            "incumbent_hash": None,
                            "issue_kind": "prospective_native",
                            "policy_schema": "native_unchanged_v1",
                            "capability": capability,
                            "scope": scope,
                            "outdoor_bin": (8, 10, 12)[day % 3],
                            "observed": observed,
                            "candidate": observed + 0.10 + 0.01 * ((day + cohort) % 4),
                            "incumbent": observed + 1.0 + 0.02 * day + 0.03 * cohort,
                            "baseline": observed + 1.2 + 0.03 * day + 0.02 * cohort,
                        }
                    )
        return pairs

    @pytest.mark.usefixtures("windows_directory_fsync")
    async def test_signed_indoor_candidate_issues_and_evaluates_each_head(self, state, monkeypatch):
        import math
        from packages.ml import forecast_quality as quality, thermal

        records, readings, weather = self.cohort()
        raw = {
            "records": records,
            "readings": readings,
            "weather": weather,
            "device_id": "audit-device",
            "data_cutoff": self.NOW,
        }
        monkeypatch.setattr(thermal, "materialize_thermal_inputs", AsyncMock(return_value=raw))
        clock = [self.NOW]
        monkeypatch.setattr(module, "_now", lambda: clock[0])

        async def inputs(component, *, now, capabilities=(), **_options):
            family = (
                "indoor_" if capabilities and capabilities[0].startswith("indoor_") else "tank_"
            )
            return self.native_inputs(now, family)

        monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(side_effect=inputs))
        database = MemoryLifecycle(state)

        async def publish(lease, artifact, **options):
            assert options["expected_revision"] == database.state.revision
            reference = ArtifactReference(
                artifact.artifact_hash,
                lease.epoch,
                "thermal",
                module.LIFECYCLE_SCHEMAS["thermal"],
                artifact.manifest,
                options.get("role", "candidate"),
                database.state.revision + 1,
            )
            database.state = replace(
                database.state, revision=reference.committed_revision, references=(reference,)
            )
            return module.PublicationResult(True, reference.committed_revision)

        database.publish = AsyncMock(side_effect=publish)
        database.load_committed = AsyncMock(
            side_effect=lambda component, **options: module.safe_persistence.load_signed_artifact(
                options["expected_hash"],
                component=component,
                schema_version=module.LIFECYCLE_SCHEMAS[component],
            )
        )
        database.record_native_issues = AsyncMock()
        database.begin_evidence_scan = AsyncMock()
        database.evidence_page = AsyncMock(return_value={"checkpoint": {"complete": True}})
        database.claim_decision = AsyncMock(return_value=True)
        database.finish_decision = AsyncMock()
        owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=True))
        try:
            hook = owner.hooks["thermal"]
            fitted = hook.fit(await hook.materialize(None))
            assert fitted is not None and fitted["capabilities"] == (
                "indoor_heating",
                "indoor_cooling",
            )
            lease = await database.acquire("r06-unit-owner")
            assert (await hook.commit_candidate(lease, fitted, database.state)).committed
            reference = database.state.reference("thermal", "candidate")
            assert reference is not None and not module.reference_is_qualified(reference)
            await hook.review_candidate(lease, reference)
            rows = database.record_native_issues.await_args.args[2]
            assert len(rows) == 3 and {row["horizon_minutes"] for row in rows} == {15, 30, 60}
            assert all(row["input_snapshot"]["capability"] == "indoor_cooling" for row in rows)
            assert all(
                row["input_snapshot"]["scope"] == reference.manifest["scope"] for row in rows
            )
            assert all(
                math.isfinite(value) for row in rows for value in row["predictions"].values()
            )
            assert all(
                set(row["predictions"]) == {"candidate", "incumbent", "baseline"} for row in rows
            )
            database.claim_decision.assert_not_awaited()
            assert database.publish.await_count == 1
            pairs = self.prospective_pairs(reference.artifact_hash, reference.manifest["scope"])
            database.decision_evidence = AsyncMock(return_value=(pairs, True))
            clock[0] += dt.timedelta(hours=360)
            await hook.review_candidate(lease, reference)
            database.finish_decision.assert_awaited_once()
            result = database.finish_decision.await_args.args[3]
            assert result["qualified"] is True and result["reason"] == "qualified_for_reporting"
            assert result["counts"]["pairs"] == 168
            assert result["control_ready"] is False
            assert result["ordinary_writes_allowed"] is False
            assert {"indoor_heating", "indoor_cooling"} <= set(result["evidence"])
            assert set(result["scope"]["capabilities"]) == {"indoor_heating", "indoor_cooling"}
            assert database.publish.await_count == 2
            promoted = database.publish.await_args.kwargs
            assert (
                promoted["role"] == "incumbent"
                and promoted["qualification"].scope == result["scope"]
            )
            assert database.state.reference("thermal").artifact_hash == reference.artifact_hash
        finally:
            await owner.close()

    @pytest.mark.parametrize("identity", ["legacy-dedup", "structured"])
    @pytest.mark.usefixtures("windows_directory_fsync")
    async def test_supported_families_are_queued_serially_after_publication(
        self, state, monkeypatch, identity
    ):
        from dataclasses import asdict
        from packages.core.models import MLMaintenanceRequestRecord
        from packages.ml import forecast_quality as quality, thermal

        records, readings, weather = self.cohort()
        raw = {
            "records": records,
            "readings": readings,
            "weather": weather,
            "device_id": "audit-device",
            "data_cutoff": self.NOW,
        }
        payload = thermal.build_thermal_candidate(raw)
        model = thermal.ThermalModel()
        model.params = model._decode_params(payload["params"])
        for head, rate in (("tank_heating", 12.0), ("tank_cooling", -1.5)):
            model._validate_rate_component(head, [(rate, 5.0, 0.25)] * 40)
            assert model.params.calibration_status[head] == "validated"
        payload = {
            **payload,
            "params": asdict(model.params),
            "validation": {**payload["validation"], **model.validation},
            "declared_components": [
                *payload["declared_components"],
                "tank_heating",
                "tank_cooling",
            ],
        }
        quality.check_exact_fit("thermal", payload)
        monkeypatch.setattr(thermal, "build_thermal_candidate", Mock(return_value=payload))
        monkeypatch.setattr(thermal, "materialize_thermal_inputs", AsyncMock(return_value=raw))

        async def inputs(component, *, now, capabilities=(), **_options):
            return self.native_inputs(
                now,
                "indoor_" if capabilities and capabilities[0].startswith("indoor_") else "tank_",
            )

        monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(side_effect=inputs))
        monkeypatch.setattr(module, "_now", lambda: self.NOW)
        database = MemoryLifecycle(state)
        database.publish = AsyncMock(return_value=module.PublicationResult(True, 9))
        database.enqueue = AsyncMock()
        owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=True))
        try:
            hook = owner.hooks["thermal"]
            fitted = hook.fit(await hook.materialize(None))
            assert fitted["capabilities"] == ("tank_heating", "tank_cooling")
            assert fitted["remaining_families"] == ("indoor_",)
            lease = await database.acquire("r06-family-owner")
            assert (await hook.commit_candidate(lease, fitted, database.state)).committed
            database.enqueue.assert_awaited_once()
            request = database.enqueue.await_args
            assert request.args == ("thermal", "scheduled") and request.kwargs["lease"] == lease
            assert request.kwargs["family"] == "indoor_"
            row_values = {
                "id": "r06-family-request",
                "epoch": lease.epoch,
                "component": "thermal",
                "source": "scheduled",
                "requested_at": self.NOW,
                "dedup_key": (
                    request.kwargs["dedup_key"] if identity == "legacy-dedup" else "opaque"
                ),
                "details": {} if identity == "legacy-dedup" else {"family": "indoor_"},
            }
            row = MLMaintenanceRequestRecord(**row_values, status="queued")
            queued = module._request(row)
            assert isinstance(queued, module.MaintenanceRequest)
            assert request.kwargs["dedup_key"].startswith("thermal-family:indoor_:")
            assert queued.details == row_values["details"] and queued.dedup_key == row.dedup_key
            claimed_row = MLMaintenanceRequestRecord(
                **row_values, status="running", fence=lease.fence, attempted_at=self.NOW
            )
            sessions = Sessions([scalar(row), scalar(claimed_row)])
            authority = Lifecycle(sessions)
            authority._lock = AsyncMock()
            authority._read = AsyncMock(return_value=database.state)
            claimed = await authority.claim_request(lease, "thermal")
            assert isinstance(claimed, module.MaintenanceRequest) and claimed.status == "running"
            assert claimed.details == row_values["details"] and claimed.dedup_key == row.dedup_key
            assert (
                sessions.session.execute.call_args_list[-1].args[0].compile().params["details"]
                == (row_values["details"])
            )
            next_fit = hook.fit(await hook.materialize(claimed))
            assert next_fit["capabilities"] == ("indoor_heating", "indoor_cooling")
            assert next_fit["inputs"]["sensor_basis"]["sensor_ids"] == ["selected-room"]
            assert next_fit["remaining_families"] == ()
            assert (await hook.commit_candidate(lease, next_fit, database.state)).committed
            assert database.publish.await_args.args[1].manifest["capabilities"] == [
                "indoor_heating",
                "indoor_cooling",
            ]
            database.enqueue.assert_awaited_once()
            tank_row = MLMaintenanceRequestRecord(
                **{**row_values, "details": {"family": "tank_"}, "dedup_key": "tank-opaque"},
                status="running",
                fence=lease.fence,
                attempted_at=self.NOW,
            )
            tank_fit = hook.fit(await hook.materialize(module._request(tank_row)))
            assert tank_fit["capabilities"] == ("tank_heating", "tank_cooling")
            assert tank_fit["remaining_families"] == ()
            database.publish.return_value = module.PublicationResult(
                False, reason="revision_conflict"
            )
            assert not (await hook.commit_candidate(lease, next_fit, database.state)).committed
            database.enqueue.assert_awaited_once()
        finally:
            await owner.close()

    def test_changed_exact_fit_cannot_enter_family_publication(self, state, monkeypatch):
        from packages.ml import thermal

        records, readings, weather = self.cohort()
        raw = {
            "records": records,
            "readings": readings,
            "weather": weather,
            "device_id": "audit-device",
            "data_cutoff": self.NOW,
        }
        payload = thermal.build_thermal_candidate(raw)
        payload["validation"]["indoor_heating"]["holdout_predictions"][0] += 0.5
        monkeypatch.setattr(thermal, "build_thermal_candidate", Mock(return_value=payload))
        runner = SimpleNamespace(lifecycle=Mock(), _enabled=AsyncMock(return_value=True))
        hook = module.native_component_hooks(runner, None)["thermal"]
        with pytest.raises(ValueError, match="evaluated-fit round trip mismatch"):
            hook.fit({"data": raw, "inputs": self.native_inputs(self.NOW)})
        runner.lifecycle.assert_not_called()


def availability_database(state):
    database = MemoryLifecycle(state)
    sessions = Sessions([])
    authority = Lifecycle(sessions)
    authority._lock = AsyncMock()
    authority._read = AsyncMock(side_effect=lambda session: database.state)

    def update_checkpoints(statement):
        checkpoints = statement.compile().params.get("checkpoints")
        if checkpoints is not None:
            database.state = replace(database.state, checkpoints=checkpoints)
        return Mock()

    async def record(lease, error):
        return await authority.record_artifact_unavailability(lease, error)

    async def recovered(lease, reference):
        await authority.clear_artifact_unavailability(lease, reference)

    sessions.session.execute.side_effect = update_checkpoints
    database.record_artifact_unavailability.side_effect = record
    database.clear_artifact_unavailability.side_effect = recovered
    return database


async def test_six_hour_cadence_daily_demand_and_single_candidate(state):
    database = MemoryLifecycle(state)
    demand = hooks(fit=Mock(side_effect=ValueError("failed fit")))
    enabled = AsyncMock(return_value=True)
    for hour in (0, 6, 12, 18, 24):
        database.clock = module._now() + dt.timedelta(hours=hour)
        owner = module.MaintenanceRunner(database, hooks={"demand": demand}, enabled_reader=enabled)
        try:
            assert (await owner.run())["status"] == "reviewed"
            assert (await owner.run())["reason"] == "six_hour_cadence"
        finally:
            await owner.close()
    assert len(database.attempts) == 2
    assert database.attempts[1][1] - database.attempts[0][1] >= dt.timedelta(hours=24)
    assert len(database.finished) == 2 and all(
        status == "failed" for status, _ in database.finished
    )
    assert demand.fit.call_count == 2
    demand.commit_candidate.assert_not_awaited()
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    database.state = replace(database.state, references=(candidate,))
    frozen = hooks(review=AsyncMock())
    for hour in (30, 36, 42, 48):
        database.clock = module._now() + dt.timedelta(hours=hour)
        owner = module.MaintenanceRunner(database, hooks={"demand": frozen}, enabled_reader=enabled)
        try:
            assert (await owner.run())["components"]["demand"] == "candidate_frozen"
        finally:
            await owner.close()
    frozen.fit.assert_not_called()
    frozen.materialize.assert_not_awaited()
    frozen.commit_candidate.assert_not_awaited()
    assert frozen.review_candidate.await_count == 4
    assert database.state.reference("demand", "candidate") == candidate


async def test_damaged_candidate_is_fenced_and_other_models_continue(state, monkeypatch):
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    incumbent = replace(qualified_reference("demand", ("demand",)), artifact_hash="b" * 64)
    database = MemoryLifecycle(replace(state, references=(incumbent, candidate)))

    async def invalidate(lease, reference, reason):
        assert reference == candidate and reason == "artifact_integrity_failure"
        database.state = replace(
            database.state,
            revision=database.state.revision + 1,
            references=(incumbent, replace(candidate, role="retained")),
        )

    database.invalidate_component = AsyncMock(side_effect=invalidate)
    reader = Lifecycle()
    reader.read = database.read
    monkeypatch.setattr(
        module.safe_persistence,
        "verify_signed_artifact",
        Mock(side_effect=ValueError("Artifact hash mismatch")),
    )

    async def review(lease, reference):
        if reference.role == "candidate":
            await reader.load_committed("demand", role="candidate")

    demand = hooks(review=review)
    comfort = hooks()
    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": demand, "comfort": comfort},
        enabled_reader=AsyncMock(return_value=True),
    )
    failures, cycles = [], []
    try:
        for hour in (0, 6):
            database.clock = module._now() + dt.timedelta(hours=hour)
            try:
                cycles.append(await owner.run())
            except ValueError as error:
                failures.append(str(error))
        assert failures == []
        assert [cycle["components"]["comfort"] for cycle in cycles] == ["succeeded", "succeeded"]
        assert cycles[0]["components"]["demand"] == "failed"
        assert cycles[1]["components"]["demand"] == "succeeded"
        database.invalidate_component.assert_awaited_once()
        assert database.state.reference("demand") == incumbent
        assert database.state.reference("demand", "candidate") is None
        assert comfort.materialize.await_count == 2
        demand.materialize.assert_awaited_once()
        demand.commit_candidate.assert_awaited_once()
    finally:
        await owner.close()


async def test_candidate_review_failure_is_component_local(state):
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    database = MemoryLifecycle(replace(state, references=(candidate,)))
    demand = hooks(review=AsyncMock(side_effect=RuntimeError("temporary review failure")))
    comfort = hooks()
    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": demand, "comfort": comfort},
        enabled_reader=AsyncMock(return_value=True),
    )
    try:
        result = await owner.run()
        assert result["components"]["demand"] == "failed"
        assert result["components"]["comfort"] == "succeeded"
        assert database.state.reference("demand", "candidate") == candidate
        comfort.materialize.assert_awaited_once()
    finally:
        await owner.close()


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("storage temporarily inaccessible"),
        FileNotFoundError("storage temporarily unmounted"),
        OSError(5, "temporary storage I/O failure"),
        TimeoutError("storage timeout"),
    ],
    ids=["permission", "missing", "io", "timeout"],
)
def test_transient_artifact_verification_is_unavailable_not_damage(state, monkeypatch, failure):
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    monkeypatch.setattr(
        module.safe_persistence, "verify_signed_artifact", Mock(side_effect=failure)
    )
    with pytest.raises(module.LifecycleUnavailable):
        module.verify_reference(reference)


def test_transient_artifact_status_reports_availability_not_integrity(state, monkeypatch, tmp_path):
    from packages.ml import model_status

    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    snapshot = replace(state, references=(reference,))
    monkeypatch.setattr(
        module.safe_persistence,
        "verify_signed_artifact",
        Mock(side_effect=PermissionError("offline probe")),
    )
    status = model_status._inspect_model_artifact("demand", tmp_path, snapshot)
    assert status["status"] == "unavailable"
    assert status["unavailable_reason"] == "artifact_unavailable"
    assert status["control_ready"] is False and status["ordinary_writes_allowed"] is False
    assert snapshot.reference("demand", "candidate") == reference


async def test_recovery_clears_outage_after_review_promotes_same_artifact(state, lease):
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    progress = {"cursor": 17, "complete": False}
    sessions = Sessions([Mock()])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(
        return_value=replace(
            state,
            references=(replace(candidate, role="incumbent", committed_revision=9),),
            checkpoints={
                "availability:" + candidate.artifact_hash: {"status": "unavailable", "attempts": 1},
                "scan:" + candidate.artifact_hash: progress,
            },
        )
    )
    await lifecycle.clear_artifact_unavailability(lease, candidate)
    checkpoint = sessions.session.execute.call_args.args[0].compile().params["checkpoints"]
    assert checkpoint["availability:" + candidate.artifact_hash]["status"] == "available"
    assert checkpoint["scan:" + candidate.artifact_hash] == progress


async def test_persistent_permissions_never_escalate_or_exceed_retry_bound(
    state, lease, monkeypatch
):
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    database = availability_database(replace(state, references=(reference,)))
    start = database.clock
    monkeypatch.setattr(module, "_now", lambda: database.clock)
    confirmation = Mock(side_effect=AssertionError("permission failure is not missing-file proof"))
    monkeypatch.setattr(module, "_confirm_artifact_missing", confirmation)
    error = module.ArtifactUnavailableError(reference, PermissionError("offline permission probe"))
    for hour, attempts in ((0, 1), (6, 2), (18, 3), (42, 3), (66, 3)):
        database.clock = start + dt.timedelta(hours=hour)
        assert await database.record_artifact_unavailability(lease, error) is False
        checkpoint = database.state.checkpoints["availability:" + reference.artifact_hash]
        assert checkpoint["attempts"] == attempts and checkpoint["status"] == "unavailable"
        assert dt.datetime.fromisoformat(
            checkpoint["next_retry_at"]
        ) - database.clock <= dt.timedelta(hours=24)
    confirmation.assert_not_called()
    assert database.state.reference("demand", "candidate") == reference


@pytest.mark.parametrize("failure_kind", ["dbapi", "pool_timeout", "os_connection"])
async def test_database_connectivity_outage_preserves_candidate_authority(state, failure_kind):
    from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError

    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    database = MemoryLifecycle(replace(state, references=(reference,)))
    failures = {
        "dbapi": DBAPIError(None, None, OSError("offline connectivity probe")),
        "pool_timeout": SQLAlchemyTimeoutError("offline pool timeout probe"),
        "os_connection": ConnectionRefusedError("offline connection probe"),
    }
    database.read = AsyncMock(side_effect=failures[failure_kind])
    owner = module.MaintenanceRunner(
        database, hooks={"demand": hooks()}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        result = await owner.run()
        assert set(result["components"].values()) == {"unavailable"}
        assert database.state.reference("demand", "candidate") == reference
        database.invalidate_component.assert_not_awaited()
        database.record_artifact_unavailability.assert_not_awaited()
    finally:
        await owner.close()


def test_R13_actual_directory_artifact_is_permanent_malformed_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, "epoch", "demand", "demand_lifecycle_v1", {}, "candidate", 1
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.mkdir(parents=True)

    with pytest.raises(module.ArtifactIntegrityError):
        module.verify_reference(reference)


@pytest.mark.parametrize("failure", [PermissionError, IsADirectoryError])
def test_R13_nonfile_error_variants_require_same_entry_confirmation(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, "epoch", "demand", "demand_lifecycle_v1", {}, "candidate", 1
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.mkdir(parents=True)
    monkeypatch.setattr(
        module.safe_persistence, "verify_signed_artifact", Mock(side_effect=failure("s2 nonfile"))
    )

    with pytest.raises(module.ArtifactIntegrityError):
        module.verify_reference(reference)


def test_R13_entry_swap_during_failed_read_is_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, "epoch", "demand", "demand_lifecycle_v1", {}, "candidate", 1
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.mkdir(parents=True)

    def swapped(*_args, **_kwargs):
        path.rmdir()
        path.write_bytes(b"replacement entry not yet admitted")
        raise PermissionError("s2 atomic entry replacement")

    monkeypatch.setattr(module.safe_persistence, "verify_signed_artifact", swapped)
    with pytest.raises(module.ArtifactUnavailableError):
        module.verify_reference(reference)
    assert path.is_file()


def test_R13_real_file_permissions_remain_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, "epoch", "demand", "demand_lifecycle_v1", {}, "candidate", 1
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unreadable control")
    monkeypatch.setattr(
        module.safe_persistence,
        "verify_signed_artifact",
        Mock(side_effect=PermissionError("s2 real permissions")),
    )

    with pytest.raises(module.ArtifactUnavailableError):
        module.verify_reference(reference)


def test_R13_inaccessible_entry_type_cannot_prove_integrity(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, "epoch", "demand", "demand_lifecycle_v1", {}, "candidate", 1
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.mkdir(parents=True)
    original = Path.lstat

    def inaccessible(current):
        if current == path:
            raise PermissionError("s2 metadata unavailable")
        return original(current)

    monkeypatch.setattr(Path, "lstat", inaccessible)
    monkeypatch.setattr(
        module.safe_persistence,
        "verify_signed_artifact",
        Mock(side_effect=PermissionError("s2 unreadable entry")),
    )
    with pytest.raises(module.ArtifactUnavailableError):
        module.verify_reference(reference)


async def test_R13_confirmed_directory_fences_candidate_without_retry_and_allows_repair(
    state, tmp_path, monkeypatch
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    incumbent = replace(qualified_reference("demand", ("demand",)), artifact_hash="b" * 64)
    progress = {"cursor": 17, "processed_rows": 17, "complete": False}
    database = availability_database(
        replace(
            state,
            references=(incumbent, candidate),
            checkpoints={"scan:" + candidate.artifact_hash: progress},
        )
    )
    monkeypatch.setattr(module, "_now", lambda: database.clock)
    path = module.safe_persistence.artifact_path(candidate.artifact_hash)
    path.mkdir(parents=True)

    async def invalidate(lease, reference, reason):
        assert reference == candidate and lease.epoch == candidate.epoch
        assert lease.fence == database.state.fence
        assert reason == "artifact_integrity_failure"
        database.state = replace(
            database.state,
            revision=database.state.revision + 1,
            references=(incumbent, replace(candidate, role="retained")),
        )

    database.invalidate_component.side_effect = invalidate

    async def review(lease, reference):
        if reference.role == "candidate":
            module.verify_reference(reference)

    demand = hooks(review=review)
    comfort = hooks()
    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": demand, "comfort": comfort},
        enabled_reader=AsyncMock(return_value=True),
    )
    try:
        result = await owner.run()
        assert result["components"]["demand"] == "failed"
        assert result["components"]["comfort"] == "succeeded"
        database.invalidate_component.assert_awaited_once()
        database.record_artifact_unavailability.assert_not_awaited()
        assert database.state.reference("demand") == incumbent
        assert database.state.reference("demand", "candidate") is None
        assert database.state.checkpoints["scan:" + candidate.artifact_hash] == progress
        demand.materialize.assert_not_awaited()
        path.rmdir()
        database.clock += dt.timedelta(hours=6)
        assert (await owner.run())["components"]["demand"] == "succeeded"
        demand.materialize.assert_awaited_once()
        demand.commit_candidate.assert_awaited_once()
        assert database.state.reference("demand") == incumbent
        assert database.state.checkpoints["scan:" + candidate.artifact_hash] == progress
    finally:
        await owner.close()
    assert database.released is True and owner._active is None


async def test_R13_entry_swap_preserves_candidate_progress_until_successful_retry(
    state, tmp_path, monkeypatch
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    candidate = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    incumbent = replace(qualified_reference("demand", ("demand",)), artifact_hash="b" * 64)
    progress = {"cursor": 17, "processed_rows": 17, "complete": False}
    database = availability_database(
        replace(
            state,
            references=(incumbent, candidate),
            checkpoints={"scan:" + candidate.artifact_hash: progress},
        )
    )
    monkeypatch.setattr(module, "_now", lambda: database.clock)
    path = module.safe_persistence.artifact_path(candidate.artifact_hash)
    path.mkdir(parents=True)

    def swapped(*_args, **_kwargs):
        path.rmdir()
        path.write_bytes(b"entry replaced during unavailable verification")
        raise PermissionError("s2 entry replacement")

    verification = Mock(side_effect=swapped)
    monkeypatch.setattr(module.safe_persistence, "verify_signed_artifact", verification)

    async def review(lease, reference):
        if reference.role == "candidate":
            module.verify_reference(reference)

    demand = hooks(review=review)
    owner = module.MaintenanceRunner(
        database, hooks={"demand": demand}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        assert (await owner.run())["components"]["demand"] == "unavailable"
        database.invalidate_component.assert_not_awaited()
        assert database.state.reference("demand") == incumbent
        assert database.state.reference("demand", "candidate") == candidate
        assert database.state.checkpoints["scan:" + candidate.artifact_hash] == progress
        availability = database.state.checkpoints["availability:" + candidate.artifact_hash]
        assert availability["attempts"] == 1 and availability["status"] == "unavailable"
        database.clock += dt.timedelta(hours=6)
        verification.side_effect = None
        verification.return_value = (SimpleNamespace(manifest={}), b"fit")
        assert (await owner.run())["components"]["demand"] == "candidate_frozen"
        assert (
            database.state.checkpoints["availability:" + candidate.artifact_hash]["status"]
            == "available"
        )
        assert database.state.reference("demand") == incumbent
        assert database.state.reference("demand", "candidate") == candidate
        assert database.state.checkpoints["scan:" + candidate.artifact_hash] == progress
        assert verification.call_count == 2
        demand.materialize.assert_not_awaited()
        demand.commit_candidate.assert_not_awaited()
        database.invalidate_component.assert_not_awaited()
    finally:
        await owner.close()
    assert database.released is True and owner._active is None


async def test_candidate_storage_outage_preserves_progress_and_recovers(state, monkeypatch):
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    progress = {"cursor": 17, "processed_rows": 17, "complete": False}
    database = availability_database(
        replace(
            state,
            references=(reference,),
            checkpoints={"scan:" + reference.artifact_hash: progress},
        )
    )
    monkeypatch.setattr(module, "_now", lambda: database.clock)
    verification = Mock(
        side_effect=[
            PermissionError("offline permission probe"),
            (SimpleNamespace(manifest={}), b"fit"),
        ]
    )
    monkeypatch.setattr(module.safe_persistence, "verify_signed_artifact", verification)

    async def review(lease, current):
        module.verify_reference(current)

    demand = hooks(review=review)
    owner = module.MaintenanceRunner(
        database, hooks={"demand": demand}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        assert (await owner.run())["components"]["demand"] == "unavailable"
        checkpoint = database.state.checkpoints["availability:" + reference.artifact_hash]
        assert checkpoint["attempts"] == 1 and checkpoint["status"] == "unavailable"
        assert dt.datetime.fromisoformat(
            checkpoint["next_retry_at"]
        ) == database.clock + dt.timedelta(hours=6)
        database.clock += dt.timedelta(hours=6)
        assert (await owner.run())["components"]["demand"] == "candidate_frozen"
        assert database.state.reference("demand", "candidate") == reference
        assert database.state.checkpoints["scan:" + reference.artifact_hash] == progress
        assert (
            database.state.checkpoints["availability:" + reference.artifact_hash]["status"]
            == "available"
        )
        assert verification.call_count == 2
        database.invalidate_component.assert_not_awaited()
        demand.materialize.assert_not_awaited()
        demand.commit_candidate.assert_not_awaited()
    finally:
        await owner.close()


@pytest.mark.parametrize("storage_available", [False, True])
async def test_missing_candidate_requires_bounded_retries_and_storage_confirmation(
    state, tmp_path, monkeypatch, storage_available
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    database = availability_database(replace(state, references=(reference,)))
    monkeypatch.setattr(module, "_now", lambda: database.clock)
    root = module.safe_persistence.artifact_path(reference.artifact_hash).parent
    if storage_available:
        root.mkdir()
    verification = Mock(side_effect=FileNotFoundError("offline missing-file probe"))
    monkeypatch.setattr(module.safe_persistence, "verify_signed_artifact", verification)
    log = Mock()
    monkeypatch.setattr(module, "logger", log)

    async def review(lease, current):
        module.verify_reference(current)

    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": hooks(review=review)},
        enabled_reader=AsyncMock(return_value=True),
    )
    start = database.clock
    try:
        for hour, attempts in ((0, 1), (6, 2), (12, 2), (18, 3)):
            database.clock = start + dt.timedelta(hours=hour)
            result = await owner.run()
            expected = "failed" if hour == 18 and storage_available else "unavailable"
            assert result["components"]["demand"] == expected
            assert verification.call_count == attempts
        checkpoint = database.state.checkpoints["availability:" + reference.artifact_hash]
        assert checkpoint["attempts"] == 3
        if storage_available:
            database.invalidate_component.assert_awaited_once()
            assert database.invalidate_component.await_args.args[2] == "artifact_missing_confirmed"
            assert checkpoint["status"] == "confirmed_missing"
        else:
            database.invalidate_component.assert_not_awaited()
            assert checkpoint["status"] == "unavailable"
        alerts = [
            call for call in log.warning.call_args_list if call.args[0] == "ml_artifact_unavailable"
        ]
        assert len(alerts) == 3 and alerts[-1].kwargs["attempts"] == 3
    finally:
        await owner.close()


@pytest.mark.parametrize("damage", ["directory", "bytes", "schema"])
@pytest.mark.parametrize("stale_reference", ["retained", "republished"])
async def test_R13_damaged_invalidation_records_reason_and_advances_revision(
    state, lease, tmp_path, monkeypatch, stale_reference, damage
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    candidate = ArtifactReference(
        "a" * 64,
        state.epoch,
        "demand",
        "wrong_schema" if damage == "schema" else "demand_lifecycle_v1",
        {},
        "candidate",
        state.revision,
    )
    path = module.safe_persistence.artifact_path(candidate.artifact_hash)
    if damage == "directory":
        path.mkdir(parents=True)
    else:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"stable damaged artifact")
    sessions = Sessions([scalar(9), Mock(rowcount=1)])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=replace(state, references=(candidate,)))
    log = Mock()
    monkeypatch.setattr(module, "logger", log)
    await lifecycle.invalidate_component(lease, candidate, "artifact_integrity_failure")
    assert sessions.committed
    state_update, artifact_update = sessions.session.execute.call_args_list
    assert "nextval('ml_lifecycle_token_seq')" in sessions.sql()[0]
    checkpoints = state_update.args[0].compile().params["checkpoints"]
    recorded = checkpoints["invalidated:" + candidate.artifact_hash]
    assert recorded["reason"] == "artifact_integrity_failure" and recorded["fence"] == lease.fence
    assert artifact_update.args[0].compile().params["role"] == "retained"
    log.warning.assert_called_once_with(
        "ml_component_invalidated",
        component="demand",
        artifact_hash=candidate.artifact_hash,
        role="candidate",
        fence=lease.fence,
        revision=9,
        reason="artifact_integrity_failure",
    )
    lifecycle._read = AsyncMock(
        return_value=replace(
            state,
            references=(
                replace(candidate, role="retained")
                if stale_reference == "retained"
                else replace(candidate, committed_revision=candidate.committed_revision + 1),
            ),
        )
    )
    with pytest.raises(module.CASConflict, match="invalidation_reference_changed"):
        await lifecycle.invalidate_component(lease, candidate, "artifact_integrity_failure")
    assert sessions.session.execute.await_count == 2


@pytest.mark.parametrize(
    "condition", ["healthy", "entry_replaced", "permissions", "missing_storage"]
)
async def test_R13_revalidation_preserves_authority_without_DML(
    state, lease, tmp_path, monkeypatch, condition
):
    import pickle

    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    if condition != "missing_storage":
        path.parent.mkdir(parents=True)
        if condition == "entry_replaced":
            path.mkdir()
        else:
            path.write_bytes(b"admission fixture")
    if condition == "healthy":
        monkeypatch.setattr(
            module.safe_persistence,
            "verify_signed_artifact",
            Mock(return_value=(SimpleNamespace(manifest={}), pickle.dumps({"fit": "healthy"}))),
        )
    elif condition == "permissions":
        monkeypatch.setattr(
            module.safe_persistence,
            "verify_signed_artifact",
            Mock(side_effect=PermissionError("real-file admission outage")),
        )

    async def admission(session, attempt):
        assert attempt == lease
        if condition == "entry_replaced":
            path.rmdir()
            path.write_bytes(b"different entry still cannot authorize earlier confirmation")

    sessions = Sessions([])
    lifecycle = Lifecycle(sessions)
    before = replace(state, references=(reference,), checkpoints={"scan": {"cursor": 17}})
    lifecycle._lock = AsyncMock(side_effect=admission)
    lifecycle._read = AsyncMock(return_value=before)
    expected_error = (
        module.CASConflict
        if condition in {"healthy", "entry_replaced"}
        else module.ArtifactUnavailableError
    )

    with pytest.raises(expected_error):
        await lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")

    assert sessions.session.execute.await_count == 0 and sessions.committed is False
    assert await lifecycle._read(sessions.session) == before


@pytest.mark.skipif(
    os.name == "nt",
    reason="Creating directory symlinks requires Windows symlink privilege; exercised on POSIX",
)
async def test_R13_symlink_replacement_at_admission_has_zero_DML(
    state, lease, tmp_path, monkeypatch
):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(model_dir))
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    path = model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    path.parent.mkdir()
    first = tmp_path / "first-untrusted-target"
    second = tmp_path / "second-untrusted-target"
    first.mkdir()
    second.mkdir()
    path.symlink_to(first, target_is_directory=True)
    with pytest.raises(module.ArtifactIntegrityError):
        module.verify_reference(reference)

    async def admission(session, attempt):
        assert attempt == lease
        path.unlink()
        path.symlink_to(second, target_is_directory=True)

    sessions = Sessions([scalar(9), Mock(rowcount=1)])
    lifecycle = Lifecycle(sessions)
    before = replace(state, references=(reference,))
    lifecycle._lock = AsyncMock(side_effect=admission)
    lifecycle._read = AsyncMock(return_value=before)
    try:
        await lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")
    except (module.CASConflict, module.ArtifactUnavailableError):
        pass

    assert sessions.session.execute.await_count == 0
    assert await lifecycle._read(sessions.session) == before


async def test_R13_stable_payload_corruption_remains_fenced_after_revalidation(
    state, lease, tmp_path, monkeypatch
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    reference = ArtifactReference(
        "a" * 64, state.epoch, "demand", "demand_lifecycle_v1", {}, "candidate", state.revision
    )
    path = module.safe_persistence.artifact_path(reference.artifact_hash)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stable payload corruption fixture")
    verification = Mock(return_value=(SimpleNamespace(manifest={}), b""))
    monkeypatch.setattr(module.safe_persistence, "verify_signed_artifact", verification)
    with pytest.raises(module.ArtifactIntegrityError):
        module._load_reference(reference)
    verification.reset_mock()
    sessions = Sessions([scalar(9), Mock(rowcount=1)])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=replace(state, references=(reference,)))

    await lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")

    assert sessions.committed and sessions.session.execute.await_count == 2
    verification.assert_called_once_with(
        reference.artifact_hash,
        component=reference.component,
        schema_version=reference.schema_version,
    )


@pytest.mark.parametrize(
    "condition", ["restored_file", "new_directory", "storage_outage", "stable_missing"]
)
@pytest.mark.usefixtures("windows_directory_fsync")
async def test_R13_missing_confirmation_is_rechecked_at_invalidation_admission(
    state, lease, tmp_path, monkeypatch, condition
):
    monkeypatch.setattr(module.safe_persistence.settings, "model_dir", str(tmp_path))
    manifest = {
        "fit_id": "missing-admission",
        "data_cutoff": module._now().isoformat(),
        "input_schema": "test_inputs",
        "output_schema": "test_outputs",
        "runtime_versions": {"python": "test"},
        "evidence_hashes": [],
    }
    artifact = module.safe_persistence.publish_signed_artifact(
        {"fit": "healthy"},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=manifest,
    )
    reference = ArtifactReference(
        artifact.artifact_hash,
        state.epoch,
        "demand",
        artifact.schema_version,
        manifest,
        "candidate",
        state.revision,
    )
    path = artifact.path
    signed_bytes = path.read_bytes()
    path.unlink()
    if condition == "storage_outage":
        path.parent.rmdir()

    async def admission(session, attempt):
        assert attempt == lease
        if condition == "restored_file":
            path.write_bytes(signed_bytes)
            module.verify_reference(reference)
        elif condition == "new_directory":
            path.mkdir()

    sessions = Sessions([scalar(9), Mock(rowcount=1)])
    lifecycle = Lifecycle(sessions)
    before = replace(
        state,
        references=(reference,),
        checkpoints={
            "availability:" + reference.artifact_hash: {
                "status": "confirmed_missing",
                "attempts": 3,
                "fence": lease.fence,
            }
        },
    )
    lifecycle._lock = AsyncMock(side_effect=admission)
    lifecycle._read = AsyncMock(return_value=before)
    try:
        await lifecycle.invalidate_component(lease, reference, "artifact_missing_confirmed")
    except module.ArtifactUnavailableError:
        assert condition == "storage_outage"
    except module.CASConflict:
        assert condition in {"restored_file", "new_directory"}

    expected_writes = 2 if condition == "stable_missing" else 0
    assert sessions.session.execute.await_count == expected_writes
    assert sessions.committed is (condition == "stable_missing")
    assert await lifecycle._read(sessions.session) == before


async def test_maintenance_cycle_schedules_retention_under_its_lease(state):
    database = MemoryLifecycle(state)
    database.prune_evidence = AsyncMock(return_value={"issues": 1, "outcomes": 1, "requests": 0})
    database.prune_artifacts = AsyncMock(return_value=["orphan.pkl"])
    owner = module.MaintenanceRunner(
        database, hooks={}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        result = await owner.run()
        database.prune_evidence.assert_awaited_once()
        database.prune_artifacts.assert_awaited_once()
        evidence_lease = database.prune_evidence.await_args.args[0]
        artifact_lease = database.prune_artifacts.await_args.args[0]
        assert evidence_lease == artifact_lease
        assert evidence_lease.owner == owner._owner and evidence_lease.fence == state.fence
        assert result["retention"] == {"issues": 1, "outcomes": 1, "requests": 0, "artifacts": 1}
    finally:
        await owner.close()


async def test_evidence_retention_is_bounded_and_reference_aware(lease):
    sessions = Sessions([Mock(rowcount=2), Mock(rowcount=2), Mock(rowcount=1)])
    lifecycle = Lifecycle(sessions)
    lifecycle._lock = AsyncMock()
    assert await lifecycle.prune_evidence(lease) == {"issues": 2, "outcomes": 2, "requests": 1}
    statements = sessions.sql()
    assert len(statements) == 3 and all("LIMIT " in sql for sql in statements)
    issue_delete = sessions.session.execute.call_args_list[1].args[0]
    params = issue_delete.compile().params
    assert module.EVIDENCE_RETENTION == dt.timedelta(days=90)
    assert set(module.PROTECTED_ROLES) in [
        set(value) for value in params.values() if isinstance(value, list)
    ]


async def test_cancelled_worker_cannot_publish(state):
    database = MemoryLifecycle(state)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    event_thread = threading.get_ident()

    def fit(inputs):
        assert threading.get_ident() != event_thread
        assert inputs == {"values": (1, 2, 3)}
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test failed to release its owned worker"
        return {"fitted": True}

    component = hooks(fit=fit)
    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": component},
        enabled_reader=AsyncMock(return_value=True),
        renewal_seconds=0.01,
    )
    task = asyncio.create_task(owner.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        database.renewed.clear()
        task.cancel()
        await asyncio.wait_for(database.renewed.wait(), 2)
        assert not task.done() and database.released is False
        assert (await owner.run())["status"] == "busy"
        component.commit_candidate.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.close()
    assert database.released
    assert database.finished[-1][0] == "cancelled"
    component.commit_candidate.assert_not_awaited()


async def test_lease_loss_prevents_publication(state):
    database = MemoryLifecycle(state)

    def fit(inputs):
        database.lost = True
        return {"fitted": True}

    component = hooks(fit=fit)
    owner = module.MaintenanceRunner(
        database, hooks={"demand": component}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        assert (await owner.run())["status"] == "lease_lost"
    finally:
        await owner.close()
    component.commit_candidate.assert_not_awaited()
    assert database.finished == []


async def test_disabled_runner_has_no_automatic_writes():
    database = AsyncMock()
    owner = module.MaintenanceRunner(database, enabled_reader=AsyncMock(return_value=False))
    assert (await owner.run())["reason"] == "maintenance_disabled"
    assert database.mock_calls == []
    await owner.close()


async def test_fit_commit_runs_only_on_the_owner_loop(state):
    database = MemoryLifecycle(state)
    event_thread = threading.get_ident()
    threads = []

    def fit(inputs):
        threads.append(threading.get_ident())
        return inputs

    async def commit(lease, fitted, snapshot):
        assert threading.get_ident() == event_thread
        assert fitted == {"values": (1, 2, 3)}
        assert snapshot.lease_owner == lease.owner
        return module.PublicationResult(True, 9)

    owner = module.MaintenanceRunner(
        database,
        hooks={"demand": hooks(fit=fit, commit=commit)},
        enabled_reader=AsyncMock(return_value=True),
    )
    try:
        assert (await owner.run())["components"]["demand"] == "succeeded"
    finally:
        await owner.close()
    assert len(threads) == 1 and threads[0] != event_thread


@pytest.mark.parametrize("component", ["demand", "comfort", "thermal"])
async def test_all_entrypoints_enqueue_only(monkeypatch, component):
    from packages.api.routers import models_router
    from packages.ml.comfort_model import comfort_model
    from packages.ml.models import DemandModel
    from packages.ml.thermal import thermal_model

    queued = {"request_id": "request-1", "status": "queued", "maintenance_enabled": False}
    enqueue = AsyncMock(return_value=queued)
    monkeypatch.setattr(models_router, "enqueue_maintenance_request", enqueue)
    monkeypatch.setattr(
        comfort_model, "train", AsyncMock(side_effect=AssertionError("inline comfort fit"))
    )
    monkeypatch.setattr(
        DemandModel, "train", AsyncMock(side_effect=AssertionError("inline demand fit"))
    )
    monkeypatch.setattr(
        thermal_model, "calibrate", AsyncMock(side_effect=AssertionError("inline thermal fit"))
    )

    @asynccontextmanager
    async def sessions():
        yield Mock()

    monkeypatch.setattr(models_router, "get_session", sessions)
    routes = {
        "demand": ("/api/ml/train", models_router.trigger_ml_training),
        "comfort": ("/api/comfort-model/train", models_router.trigger_comfort_model_training),
        "thermal": ("/api/thermal/calibrate", models_router.calibrate_thermal_model),
    }
    path, handler = routes[component]
    result = await handler()
    envelope = result["demand"] if component == "demand" else result
    assert envelope["request_id"] == "request-1"
    assert envelope["status"] == "queued" and envelope["control_ready"] is False
    enqueue.assert_awaited_once_with(component, "api")
    assert (
        next(route for route in models_router.router.routes if route.path == path).status_code
        == 202
    )
    comfort_model.train.assert_not_awaited()
    DemandModel.train.assert_not_awaited()
    thermal_model.calibrate.assert_not_awaited()
    if component == "demand":
        assert result["cop"] == {
            "status": "retired",
            "supported": False,
            "error": "cop_model_retired",
            "reason": "unverified_thermal_output",
        }


async def test_request_storage_failure_refuses_without_fitting(monkeypatch):
    from fastapi import HTTPException
    from packages.api.routers import models_router

    monkeypatch.setattr(
        models_router,
        "enqueue_maintenance_request",
        AsyncMock(side_effect=module.LifecycleUnavailable("storage unavailable")),
    )
    with pytest.raises(HTTPException) as caught:
        await models_router.trigger_ml_training()
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "ml_maintenance_unavailable"}


async def test_request_status_is_read_only_and_reports_disabled(monkeypatch):
    from uuid import uuid4
    from packages.api.routers import models_router

    request_id = uuid4()
    database = Mock()
    database.get_request = AsyncMock(return_value=module._request(request_row()))
    monkeypatch.setattr(models_router, "Lifecycle", Mock(return_value=database))
    monkeypatch.setattr(models_router, "get_bool_setting", AsyncMock(return_value=False))
    status = await models_router.get_maintenance_request(request_id)
    assert status["status"] == "queued" and status["message"] == "maintenance disabled"
    assert database.mock_calls == [(("get_request", (str(request_id),), {}))]


async def test_disabled_poller_job_and_seasonal_cycle_do_not_queue(monkeypatch):
    from packages.ml import seasonal_learning
    from packages.poller import main as poller

    monkeypatch.setattr(poller, "get_bool_setting", AsyncMock(return_value=False))
    seasonal_job = AsyncMock(side_effect=AssertionError("disabled seasonal job"))
    monkeypatch.setattr(poller, "run_seasonal_calibration", seasonal_job)
    owner = AsyncMock()
    assert (await poller.run_ml_maintenance(owner))["reason"] == "maintenance_disabled"
    owner.run.assert_not_awaited()
    seasonal_job.assert_not_awaited()
    monkeypatch.setattr(
        seasonal_learning,
        "_transition_seasonal_campaign",
        AsyncMock(return_value={"observe_only_active": True}),
    )
    monkeypatch.setattr(seasonal_learning, "get_bool_setting", AsyncMock(return_value=False))
    enqueue = AsyncMock(side_effect=AssertionError("disabled seasonal request"))
    monkeypatch.setattr(module, "enqueue_maintenance_request", enqueue)
    assert (await seasonal_learning.run_seasonal_calibration_cycle())[
        "cycle"
    ] == "maintenance_disabled"
    enqueue.assert_not_awaited()


async def test_committed_thermal_reader_keeps_component_validation(monkeypatch):
    from dataclasses import asdict
    from packages.ml.thermal import ThermalModel

    model = ThermalModel()
    evaluated = ThermalModel()
    evaluated._validate_rate_component("indoor_heating", [(0.6, 5.0, 0.25)] * 40)
    evaluated._validate_rate_component("tank_heating", [(12.0, 10.0, 0.25)] * 40)
    evaluated.params.last_calibrated = module._now()
    assert evaluated.params.calibration_status["tank_heating"] == "validated"
    reference = qualified_reference()
    database = AsyncMock()
    database.read.return_value = LifecycleSnapshot("epoch", 7, 8, True, None, None, (reference,))
    database.load_committed.return_value = {
        "schema_version": 5,
        "params": asdict(evaluated.params),
        "validation": evaluated.validation,
        "declared_components": ["indoor_heating", "tank_heating"],
    }
    monkeypatch.setattr(model, "calibrate", AsyncMock(side_effect=AssertionError("planner fit")))
    monkeypatch.setattr(model, "load_latest", Mock(side_effect=AssertionError("legacy adoption")))
    assert await model.arefresh_committed(lifecycle=database, scope=reference.manifest["scope"])
    database.load_committed.assert_awaited_once_with(
        "thermal", expected_hash=reference.artifact_hash
    )
    assert model.confidence_for("indoor_heating") == "learned"
    assert model.confidence_for("tank_heating") == "default"
    assert model.params.tank_heating_rate == 4.0
    assert model._tank_heating_rate(10) == 4.8
    model.calibrate.assert_not_awaited()
    model.load_latest.assert_not_called()


@pytest.mark.parametrize(
    "qualification", ["missing", "other_head", "undeclared", "expired", "candidate"]
)
def test_thermal_confidence_requires_current_head_qualification(qualification):
    from packages.ml.thermal import ThermalModel

    model = ThermalModel()
    model.params.last_calibrated = module._now()
    model.params.calibration_status["tank_heating"] = "validated"
    reference = qualified_reference("thermal", ("tank_heating",))
    if qualification == "missing":
        reference = None
    elif qualification == "other_head":
        reference = qualified_reference()
    elif qualification == "undeclared":
        reference = replace(
            reference, manifest={**reference.manifest, "capabilities": ["indoor_heating"]}
        )
    elif qualification == "expired":
        reference = replace(
            reference, qualification_expires_at=module._now() - dt.timedelta(seconds=1)
        )
    elif qualification == "candidate":
        reference = replace(reference, role="candidate")
    model._qualified_reference = reference
    assert model.confidence_for("tank_heating") == "default"


async def test_thermal_committed_cache_refreshes_within_sixty_seconds(monkeypatch):
    from packages.ml import thermal

    tick = [0.0]
    monkeypatch.setattr(thermal, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    model = thermal.ThermalModel()

    async def refresh():
        model._qualified_reference = qualified_reference()
        model.params.last_calibrated = module._now()
        model.params.calibration_status["indoor_heating"] = "validated"
        return True

    model.arefresh_committed = AsyncMock(side_effect=refresh)
    assert all(await asyncio.gather(*(model.arefresh_if_changed() for _ in range(3))))
    tick[0] = 59.999
    assert await model.arefresh_if_changed()
    model.arefresh_committed.assert_awaited_once()
    tick[0] = 60.0
    assert await model.arefresh_if_changed()
    assert model.arefresh_committed.await_count == 2
    assert await model.arefresh_if_changed(force=True)
    assert model.arefresh_committed.await_count == 3

    async def unavailable():
        model.reset()
        return False

    model.arefresh_committed.side_effect = unavailable
    tick[0] = 120.0
    assert await model.arefresh_if_changed() is False
    assert model.confidence_for("indoor_heating") == "default"


@pytest.mark.parametrize(
    "failure",
    [
        module.LifecycleUnavailable("no_committed_artifact"),
        ValueError("integrity failure"),
        OSError("database unavailable"),
    ],
)
async def test_committed_thermal_reader_fails_to_defaults_without_legacy(failure, monkeypatch):
    from packages.ml.thermal import ThermalModel, ThermalParams

    model = ThermalModel()
    model.params.tank_heating_rate = 99
    database = AsyncMock()
    reference = qualified_reference()
    database.read.return_value = LifecycleSnapshot("epoch", 7, 8, True, None, None, (reference,))
    database.load_committed.side_effect = failure
    monkeypatch.setattr(model, "load_latest", Mock(side_effect=AssertionError("legacy adoption")))
    assert (
        await model.arefresh_committed(lifecycle=database, scope=reference.manifest["scope"])
        is False
    )
    assert model.params == ThermalParams()
    model.load_latest.assert_not_called()


async def test_optimizer_startup_queues_after_quality_without_fitting(monkeypatch):
    from packages.optimizer import main as optimizer

    monkeypatch.setattr("packages.core.logging.configure_logging", Mock())
    monkeypatch.setattr(optimizer, "_load_ml_models", Mock())
    monkeypatch.setattr(
        optimizer, "get_device_data_quality", AsyncMock(return_value={"ready": True, "reasons": []})
    )
    model = SimpleNamespace(
        is_trained=False, train=AsyncMock(side_effect=AssertionError("startup fit"))
    )
    monkeypatch.setattr(optimizer, "comfort_model", model)
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "enqueue_maintenance_request", enqueue)
    events = []
    wrapper = Mock(
        start=AsyncMock(side_effect=RuntimeError("owned fixture stop")),
        stop=AsyncMock(side_effect=lambda: events.append("wrapper")),
    )
    monkeypatch.setattr(optimizer, "AquareaWrapper", Mock(return_value=wrapper))
    monkeypatch.setattr(
        "packages.core.log_sink.shutdown_log_sink",
        AsyncMock(side_effect=lambda: events.append("log_drain")),
    )
    monkeypatch.setattr(
        "packages.core.database.engine",
        SimpleNamespace(dispose=AsyncMock(side_effect=lambda: events.append("engine"))),
    )
    with pytest.raises(RuntimeError, match="owned fixture stop"):
        await optimizer.main()
    assert events == ["log_drain", "wrapper", "engine"]
    wrapper.stop.assert_awaited_once_with()
    assert enqueue.await_count == 1
    assert enqueue.await_args.args == ("comfort", "optimizer_startup")
    assert enqueue.await_args.kwargs["automatic"] is True
    model.train.assert_not_awaited()


async def test_poller_registers_single_utc_maintenance_owner(monkeypatch):
    from packages.poller import main as poller

    events = []
    scheduler = Mock(close=AsyncMock(side_effect=lambda: events.append("scheduler")))
    owner = Mock(close=AsyncMock(side_effect=lambda: events.append("owner")))
    wrapper = Mock(start=AsyncMock(), stop=AsyncMock(side_effect=lambda: events.append("wrapper")))
    shutdown = Mock(wait=AsyncMock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(
        poller,
        "asyncio",
        SimpleNamespace(Event=lambda: shutdown, get_running_loop=asyncio.get_running_loop),
    )
    monkeypatch.setattr(poller, "create_scheduler", Mock(return_value=scheduler))
    monkeypatch.setattr(poller, "AquareaWrapper", Mock(return_value=wrapper))
    monkeypatch.setattr(poller, "record_service_heartbeat", AsyncMock())
    monkeypatch.setattr(poller, "get_int_setting", AsyncMock(return_value=300))
    monkeypatch.setattr("packages.core.logging.configure_logging", Mock())
    monkeypatch.setattr(
        "packages.core.log_sink.shutdown_log_sink",
        AsyncMock(side_effect=lambda: events.append("log_drain")),
    )
    monkeypatch.setattr(module, "MaintenanceRunner", Mock(return_value=owner))
    monkeypatch.setattr(
        "packages.core.database.engine",
        SimpleNamespace(dispose=AsyncMock(side_effect=lambda: events.append("engine"))),
    )
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "enqueue_maintenance_request", enqueue)
    await poller.main()
    jobs = scheduler.add_job.call_args_list
    maintenance = [call for call in jobs if call.kwargs["id"] == "ml_maintenance"]
    assert len(maintenance) == 1
    job = maintenance[0]
    assert job.args == (poller.run_ml_maintenance, "cron")
    assert job.kwargs["hour"] == "0,6,12,18"
    assert job.kwargs["minute"] == 0
    assert job.kwargs["timezone"] == "UTC"
    assert job.kwargs["coalesce"] is True and job.kwargs["max_instances"] == 1
    assert job.kwargs["args"] == [owner]
    assert job.kwargs["next_run_time"].utcoffset() == dt.timedelta()
    assert not {"comfort_model_retrain", "seasonal_calibration"} & {
        call.kwargs["id"] for call in jobs
    }
    assert [call.args[0] for call in enqueue.await_args_list] == ["demand", "comfort", "thermal"]
    assert all(
        call.args[1] == "poller_startup" and call.kwargs["automatic"]
        for call in enqueue.await_args_list
    )
    poller.AquareaWrapper.assert_called_once_with(read_only=True)
    assert events == ["scheduler", "owner", "log_drain", "wrapper", "engine"]
    scheduler.close.assert_awaited_once_with()
    scheduler.shutdown.assert_not_called()
    owner.close.assert_awaited_once_with()
    wrapper.stop.assert_awaited_once_with()


@pytest.mark.parametrize("bulk", [False, True])
async def test_setting_disable_revokes_lease_in_its_own_transaction(monkeypatch, bulk):
    from packages.core import settings_service

    records = Mock()
    records.scalars.return_value.__iter__ = Mock(return_value=iter(()))
    responses = [Mock(), scalar(1), Mock(rowcount=1)]
    if bulk:
        responses = [Mock(), records, *responses]
    sessions = Sessions(responses)
    monkeypatch.setattr(settings_service, "get_session", sessions)
    if bulk:
        await settings_service.set_settings_bulk({"ml_maintenance_enabled": "false"})
    else:
        await settings_service.set_setting("ml_maintenance_enabled", "false")
    assert sessions.committed
    sql = sessions.sql()
    gate = next(statement for statement in sql if statement.startswith("UPDATE ml_lifecycle_state"))
    assert gate.count("nextval('ml_lifecycle_token_seq')") == 2
    assert "lease_owner=" in gate and "lease_expires_at=" in gate
    assert "maintenance_enabled IS DISTINCT FROM false" in gate
    assert any(statement.startswith("UPDATE ml_maintenance_requests") for statement in sql)
    assert not any(statement.startswith("INSERT INTO ml_lifecycle_state") for statement in sql)


async def test_automatic_enqueue_disabled_is_zero_dml():
    sessions = Sessions([scalar(SimpleNamespace(value="false"))])
    assert await Lifecycle(sessions).enqueue("comfort", "startup", automatic=True) is None
    assert len(sessions.sql()) == 1 and sessions.sql()[0].startswith("SELECT ")
    assert "FOR UPDATE" in sessions.sql()[0]


async def test_pending_model_hooks_leave_requests_queued(state):
    database = MemoryLifecycle(state)
    owner = module.MaintenanceRunner(
        database, hooks={}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        assert (await owner.run())["components"] == {
            component: "queued" for component in module.LIFECYCLE_SCHEMAS
        }
    finally:
        await owner.close()
    assert database.attempts == [] and database.finished == []
    assert database.last_demand is None
    assert all(
        request.details == {"reason": "model_lifecycle_pending"}
        for request in database.queue.values()
    )


async def test_fitting_worker_cannot_publish_files_or_lifecycle_authority(state, lease, tmp_path):
    database = MemoryLifecycle(state)
    sessions = Sessions([])
    authority = Lifecycle(sessions)
    refused = []

    def fit(inputs):
        with pytest.raises(PermissionError, match="Fitting workers"):
            module.safe_persistence.safe_dump(inputs, tmp_path / "legacy.pkl")
        refused.append("legacy")
        with pytest.raises(PermissionError, match="Fitting workers"):
            module.safe_persistence.safe_publish_temp(
                tmp_path / "temp.pkl", tmp_path / "legacy.pkl"
            )
        refused.append("rename")
        with pytest.raises(PermissionError, match="Fitting workers"):
            module.safe_persistence.publish_signed_artifact(
                inputs, component="demand", schema_version="demand_lifecycle_v1", manifest={}
            )
        refused.append("signed")
        with pytest.raises(PermissionError, match="Fitting workers"):
            asyncio.run(
                authority.publish(
                    lease, Mock(), expected_revision=state.revision, expected_incumbent=None
                )
            )
        refused.append("cas")
        return inputs

    owner = module.MaintenanceRunner(
        database, hooks={"demand": hooks(fit=fit)}, enabled_reader=AsyncMock(return_value=True)
    )
    try:
        assert (await owner.run())["components"]["demand"] == "succeeded"
    finally:
        await owner.close()
    assert refused == ["legacy", "rename", "signed", "cas"]
    sessions.session.execute.assert_not_awaited()
    assert not (tmp_path / "legacy.pkl").exists()
    module.safe_persistence._require_publication_owner()

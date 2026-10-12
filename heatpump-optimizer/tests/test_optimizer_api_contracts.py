"""Focused API contract tests for dashboard plan and optimization endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.schemas import DashboardResponse
from packages.api.routers.dashboard import get_dashboard
from packages.api.routers.dashboard import get_stats
from packages.api.routers.models_router import get_heat_curve_advice, get_thermal_curve
from packages.core.heat_curve import HeatCurveConfig
from packages.core.space_heating_gate import HeatingGateConfig
from packages.api.routers.optimizer import (
    get_optimizer_status,
    get_plan_detail,
    get_plans,
    get_optimization_request,
    optimize_now,
)
from packages.ml.model_status import clear_artifact_status_cache
from packages.ml.safe_persistence import safe_dump
from packages.ml.thermal import ThermalModel


@pytest.mark.parametrize(
    "proof", ["valid", "no_phase", "old_fence", "expired", "finished", "none_running"]
)
def test_UR5_maintenance_activity_requires_request_lease_fence_phase(proof):
    from packages.ml import model_status
    from packages.ml.lifecycle import LifecycleSnapshot

    now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
    state = LifecycleSnapshot("epoch", 7, 9, True, "owned-worker", now + dt.timedelta(minutes=2))
    request = {
        "id": "actual-running",
        "epoch": "epoch",
        "fence": 9,
        "status": "running",
        "attempted_at": now - dt.timedelta(seconds=1),
        "finished_at": None,
        "details": {"phase": "fitting"},
    }
    if proof == "no_phase":
        request["details"] = {}
    elif proof == "old_fence":
        request["fence"] = 8
    elif proof == "expired":
        from dataclasses import replace

        state = replace(state, lease_expires_at=now)
    elif proof == "finished":
        request["finished_at"] = now
    captured = {
        "context": {
            "settings": {
                "ml_maintenance_enabled": "true",
                "seasonal_calibration_auto_train": "true",
            }
        },
        "state": state,
        "as_of": now,
        "queue_counts": {"queued": 2, "running": 1},
        "running_requests": {} if proof == "none_running" else {"thermal": request},
    }
    result = model_status._maintenance_readiness(captured)
    assert result["enabled"] is True and result["preference"] == "automatic"
    if proof == "valid":
        assert (
            result["running"] is True
            and result["queue_count"] == 2
            and result["phase"] == "fitting"
        )
        assert result["last_attempt_at"] == request["attempted_at"].isoformat()
    else:
        assert all(
            result[key] is None
            for key in ("running", "queue_count", "phase", "last_attempt_at", "next_due_at")
        )
        assert result["reason"] is not None


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("request_revision", [7, 6])
def test_UR5_fit_provenance_requires_declared_head_and_matching_request(declared, request_revision):
    from packages.ml import model_status
    from packages.ml.lifecycle import ArtifactReference, LifecycleSnapshot

    reference = ArtifactReference(
        "a" * 64,
        "epoch",
        "thermal",
        "thermal_v5",
        {
            "data_cutoff": "2026-10-10T12:00:00Z",
            "capabilities": ["indoor_heating"] if declared else ["tank_heating"],
            "metrics": {"indoor_heating": {"train_samples": 30, "holdout_samples": 12}},
        },
        "candidate",
        7,
    )
    state = LifecycleSnapshot("epoch", 7, 9, False, None, None, (reference,))
    request = {
        "id": "actual-fit-request",
        "status": "succeeded",
        "details": {
            "revision": request_revision,
            "readiness_rejection": {
                "components": {"indoor_heating": {"fit_status": "rejected_no_skill_over_baseline"}}
            },
        },
    }
    fitted = model_status._fit_readiness(
        "indoor_heating",
        {"state": state, "requests": {"thermal": request}},
        {"thermal": {"status": "candidate", "unavailable_reason": None}},
    )
    assert fitted["status"] == ("validated" if declared else "not_declared")
    assert fitted["request_id"] == ("actual-fit-request" if request_revision == 7 else None)
    assert fitted["data_cutoff"] == "2026-10-10T12:00:00Z"


@pytest.mark.parametrize("enabled", [False, True, None])
@pytest.mark.parametrize("preference", ["automatic", "manual", "unknown"])
def test_UR5_collection_and_maintenance_without_qualified_incumbent(tmp_path, enabled, preference):
    from packages.core import settings_service
    from packages.core.models import DeviceStatusRecord, IndoorTempReading, WeatherRecord
    from packages.ml import model_status
    from packages.ml.demand_model_core import DemandModel
    from tests.test_ml import TestDemandModel
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute
    from zoneinfo import ZoneInfo

    now = TestR06ThermalQualificationRoute.NOW
    records, readings, weather = TestR06ThermalQualificationRoute.cohort()
    values = {key: spec.serialize_default() for key, spec in settings_service.SETTING_SPECS.items()}
    values.update(
        {
            "ml_maintenance_enabled": "unknown" if enabled is None else str(enabled).lower(),
            "seasonal_calibration_auto_train": {
                "automatic": "true",
                "manual": "false",
                "unknown": "unknown",
            }[preference],
            "timezone": "UTC",
        }
    )

    def copied(rows, model):
        return [
            SimpleNamespace(
                **{column.name: getattr(row, column.name) for column in model.__table__.columns}
            )
            for row in rows
        ]

    demand_rows = TestDemandModel._lifecycle_preprocessing_rows(390)
    start = now - dt.timedelta(hours=390)
    for result, clock in ((demand_rows[0], "ts"), (demand_rows[1], "ts"), (demand_rows[2], "ts")):
        original = result._rows[0].ts
        for row in result._rows:
            row.ts = start + (row.ts - original)
    context = {
        "settings": values,
        "as_of": now,
        "selected_ids": ["selected-room"],
        "reference_sensor_id": "selected-room",
        "device_id": "audit-device",
        "weather_provider": "open-meteo",
        "schemas": {"thermal": "thermal_v5"},
    }
    captured = {
        "context": context,
        "as_of": now,
        "generation": "live-no-incumbent",
        "state": None,
        "model_dir": tmp_path,
        "evidence": {},
        "requests": {},
        "running_requests": {},
        "queue_counts": None,
        "source_records": len(demand_rows[0]._rows),
        "latest_status": copied(records[-1:], DeviceStatusRecord)[0],
        "seasonal_inputs": {
            "average_outdoor": 5.0,
            "outdoor_readings": 24,
            "open_revert_obligations": {"count": 0},
        },
        "native": None,
        "thermal": {
            "records": copied(records, DeviceStatusRecord),
            "readings": copied(readings, IndoorTempReading),
            "weather": copied(weather, WeatherRecord),
            "indoor_blocker": None,
        },
        "demand": {
            "consumption_rows": demand_rows[0]._rows,
            "weather_rows": demand_rows[1]._rows,
            "statuses": demand_rows[2]._rows,
            "timezone": ZoneInfo("UTC"),
            "lifecycle_evidence": True,
        },
    }
    expected = DemandModel._preprocess_materialized(captured["demand"])
    result = model_status._compose_readiness(captured)
    readiness = result["readiness"]
    assert readiness["maintenance"]["enabled"] is enabled
    assert readiness["maintenance"]["preference"] == preference
    assert (
        readiness["maintenance"]["running"] is None
        and readiness["maintenance"]["next_due_at"] is None
    )
    indoor = readiness["components"]["indoor_heating"]
    assert indoor["collection"]["train"] >= 20 and indoor["collection"]["holdout"] >= 3
    assert (
        indoor["fit"]["status"] == "awaiting_fit"
        and indoor["next_step"] == "await_fit_or_validation"
    )
    assert indoor["reporting"]["counts"] is None
    assert result["thermal_model"]["indoor_heating_samples"] is None
    assert readiness["components"]["demand"]["collection"]["total"] == len(expected["bounds"])
    assert readiness["components"]["demand"]["collection"]["train"] >= 168


async def test_UR4_SR3_real_get_is_read_only(monkeypatch):
    from packages.core import database, settings_service, learning_state
    from packages.core.models import (
        DeviceStatusRecord,
        IndoorTempReading,
        WeatherRecord,
        SettingRecord,
        MLLifecycleStateRecord,
        ConsumptionRecord,
    )
    from packages.ml import (
        model_status,
        seasonal_learning,
        thermal,
        forecast_quality,
        lifecycle,
        safe_persistence,
    )
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute
    from tests.test_ml import TestDemandModel

    now = TestR06ThermalQualificationRoute.NOW
    records, readings, weather = TestR06ThermalQualificationRoute.cohort()
    values = {key: spec.serialize_default() for key, spec in settings_service.SETTING_SPECS.items()}
    values.update(
        {
            "seasonal_calibration_enabled": "true",
            "seasonal_calibration_window_days": "7",
            "seasonal_calibration_max_outdoor_c": "12",
            "smartthings_device_ids": "selected-room",
            "comfort_reference_sensor_id": "selected-room",
            "timezone": "UTC",
            "aquarea_username": "public-test",
            "aquarea_password": "public-test",
        }
    )
    stored = [SettingRecord(key=key, value=value) for key, value in values.items()]
    demand_rows = TestDemandModel._lifecycle_preprocessing_rows()[0]._rows
    for row in demand_rows:
        row.device_id = "audit-device"
    active, statements = [], []

    async def execute(statement, *_args):
        assert getattr(statement, "is_select", False), str(statement)
        statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        sql = str(statement).lower()
        params = statement.compile().params
        if entity is SettingRecord:
            rows = stored
            key = params.get("key_1")
            value = next((row for row in stored if row.key == key), None)
        elif entity is MLLifecycleStateRecord:
            rows, value = [], None
        elif "avg(" in sql:
            return SimpleNamespace(one=lambda: (5.0, 24))
        elif "count(" in sql:
            return SimpleNamespace(
                scalar_one=lambda: len(demand_rows),
                one=lambda: (0, None),
                scalars=lambda: [],
                all=lambda: [],
            )
        else:
            rows = {
                DeviceStatusRecord: records,
                IndoorTempReading: readings,
                WeatherRecord: weather,
                ConsumptionRecord: demand_rows,
            }.get(entity, [])
            limits = [
                number
                for name, number in params.items()
                if name.startswith("param_") and isinstance(number, int)
            ]
            if limits and max(limits) < len(rows):
                rows = rows[-max(limits) :]
            value = rows[-1] if rows else None
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            all=lambda: [],
            scalar_one_or_none=lambda: value,
        )

    class SessionContext:
        async def __aenter__(self):
            active.append(True)
            return SimpleNamespace(execute=AsyncMock(side_effect=execute))

        async def __aexit__(self, *_args):
            active.pop()
            return False

    def factory(**_kwargs):
        return SessionContext()

    for owner in (
        database,
        settings_service,
        model_status,
        seasonal_learning,
        thermal,
        forecast_quality,
    ):
        monkeypatch.setattr(owner, "get_session", factory)
    monkeypatch.setattr(model_status, "_now", lambda: now)
    original = thermal.ThermalModel._extract_indoor_rate_samples

    def extraction(*args):
        assert not active, "extraction ran before read-session closure"
        return original(*args)

    monkeypatch.setattr(
        thermal.ThermalModel, "_extract_indoor_rate_samples", staticmethod(extraction)
    )
    for owner, symbol in (
        (settings_service, "set_settings_bulk"),
        (seasonal_learning, "set_settings_bulk"),
        (lifecycle, "enqueue_maintenance_request"),
        (safe_persistence, "publish_signed_artifact"),
    ):
        monkeypatch.setattr(owner, symbol, MagicMock(side_effect=AssertionError("GET mutation")))
    monkeypatch.setattr(
        thermal.thermal_model,
        "arefresh_if_changed",
        AsyncMock(side_effect=AssertionError("singleton refresh")),
    )
    monkeypatch.setattr(
        thermal.ThermalModel, "_fit_rate", MagicMock(side_effect=AssertionError("GET fit"))
    )
    before = repr(thermal.thermal_model.params)
    status = await seasonal_learning.get_seasonal_calibration_status(now=now)
    assert (
        status["observe_only_active"] is False
        and status["reason"] == "awaiting_explicit_seasonal_cycle"
    )
    learning, _ = await learning_state.get_learning_state_details()
    assert learning.manual_enabled is False and learning.seasonal_active is False
    progress = await seasonal_learning.get_seasonal_calibration_progress()
    response = await get_optimizer_status()
    assert progress["indoor_heating"]["samples"] > 20
    assert (
        response["seasonal_calibration"]["readiness"]["components"]["indoor_heating"]["collection"][
            "train"
        ]
        > 20
    )
    assert response["seasonal_calibration"]["readiness"]["maintenance"]["enabled"] is False
    assert response["thermal_model"]["indoor_heating_samples"] is None
    assert response["model_lifecycle"]["thermal"]["counts"] is None
    assert response["ordinary_writes_allowed"] is False and "data_freshness" in response
    assert not active and before == repr(thermal.thermal_model.params)
    assert statements and all(statement.is_select for statement in statements)
    assert "aquarea_password" not in json.dumps(response)


@pytest.mark.parametrize(
    "case",
    [
        "coalesce",
        "cancel_before_worker",
        "waiter_limit",
        "materialization_deadline",
        "input_budget",
        "drain_late_worker",
        "session_loss",
    ],
)
async def test_UR2_SR4_singleflight_cancellation_and_budgets(monkeypatch, case):
    import threading
    from packages.ml import model_status

    started, release = asyncio.Event(), asyncio.Event()
    worker_entered, worker_release = threading.Event(), threading.Event()
    captures, closed = [], []

    async def capture(**options):
        captures.append(options)
        started.set()
        try:
            await release.wait()
            if case == "session_loss":
                raise OSError("owned session disconnected")
            return {
                "generation": options["generation"],
                "as_of": options["as_of"],
                "rows": list(range(50_000)),
            }
        finally:
            closed.append(True)

    def compose(data):
        assert closed, "worker started before read-session closure"
        worker_entered.set()
        if case == "drain_late_worker":
            assert worker_release.wait(3)
        return {
            "readiness": {"generation": data["generation"], "as_of": data["as_of"].isoformat()},
            "rows": len(data["rows"]),
        }

    monkeypatch.setattr(model_status, "_capture_readiness_inputs", capture)
    monkeypatch.setattr(model_status, "_compose_readiness", compose)
    if case in {"materialization_deadline", "drain_late_worker"}:
        monkeypatch.setattr(
            model_status,
            "_READINESS_MATERIALIZATION_SECONDS",
            0.03 if case == "materialization_deadline" else 1,
        )
        monkeypatch.setattr(
            model_status,
            "_READINESS_RESPONSE_SECONDS",
            1.0 if case == "materialization_deadline" else 0.05,
        )
    if case == "input_budget":
        monkeypatch.setattr(model_status, "_READINESS_MAX_BYTES", 1000)
    first = asyncio.create_task(model_status.get_readiness_snapshot(route="ur2-test"))
    await started.wait()
    tasks = [first]
    try:
        if case == "cancel_before_worker":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert closed and not worker_entered.is_set()
        elif case == "materialization_deadline":
            result = await first
            assert result["readiness"]["reason"] == "readiness_materialization_deadline"
            assert closed and not worker_entered.is_set()
        elif case in {"input_budget", "session_loss"}:
            release.set()
            result = await first
            assert result["readiness"]["reason"] == (
                "readiness_input_budget_exceeded"
                if case == "input_budget"
                else "readiness_unavailable"
            )
            assert not worker_entered.is_set()
        elif case == "drain_late_worker":
            release.set()
            await asyncio.to_thread(worker_entered.wait, 1)
            result = await first
            assert result["readiness"]["reason"] == "readiness_response_deadline"
            refused = await model_status.get_readiness_snapshot(route="ur2-test")
            assert refused["readiness"]["reason"] == "readiness_busy"
            assert len(captures) == 1
            worker_release.set()
            flight = model_status._READINESS_FLIGHTS[asyncio.get_running_loop()][1]
            await flight.task
        else:
            count = 31 if case == "waiter_limit" else 2
            tasks.extend(
                asyncio.create_task(model_status.get_readiness_snapshot(route="ur2-test"))
                for _ in range(count)
            )
            await asyncio.sleep(0)
            if case == "waiter_limit":
                refused = await model_status.get_readiness_snapshot(route="ur2-test")
                assert refused["readiness"]["reason"] == "readiness_busy"
            else:
                tasks[1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[1]
            release.set()
            results = await asyncio.gather(*(task for task in tasks if not task.cancelled()))
            assert (
                len(captures) == 1
                and len({result["readiness"]["generation"] for result in results}) == 1
            )
            assert results[0]["rows"] == 50_000
            second = await model_status.get_readiness_snapshot(route="ur2-test")
            assert (
                len(captures) == 2
                and second["readiness"]["generation"] != results[0]["readiness"]["generation"]
            )
    finally:
        release.set()
        worker_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        active = model_status._READINESS_FLIGHTS.get(asyncio.get_running_loop())
        if active:
            await asyncio.gather(active[1].task, return_exceptions=True)
    assert not model_status._READINESS_FLIGHTS.get(asyncio.get_running_loop())


async def test_UR2_SR1_captured_model_status_uses_frozen_clock_without_read_or_cache_mutation(
    tmp_path,
):
    from packages.ml import model_status
    from packages.ml.lifecycle import LifecycleSnapshot

    now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
    state = LifecycleSnapshot("readiness-epoch", 7, 9, False, None, None)
    before = dict(model_status._artifact_status_cache)
    with (
        patch.object(model_status, "Lifecycle", side_effect=AssertionError("later DB snapshot")),
        patch.object(model_status, "_now", side_effect=AssertionError("later clock")),
    ):
        statuses = await model_status.get_model_reporting_statuses(
            tmp_path, snapshot=state, evidence_by_component={}, as_of=now, context={"as_of": now}
        )
    assert all(status["counts"] is None for status in statuses.values())
    assert all(status["evidence_reason"] == "no_selected_reference" for status in statuses.values())
    assert model_status._artifact_status_cache == before


def test_UR5_artifact_failure_preserves_captured_native_evidence(tmp_path):
    from packages.ml import model_status
    from packages.ml.lifecycle import ArtifactReference, ArtifactUnavailableError, LifecycleSnapshot

    now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
    reference = ArtifactReference(
        "a" * 64,
        "readiness-epoch",
        "thermal",
        "thermal_v5",
        {"frozen_at": (now - dt.timedelta(days=3)).isoformat(), "horizons": [15, 30, 60]},
        "candidate",
        7,
    )
    state = LifecycleSnapshot("readiness-epoch", 7, 9, False, None, None, (reference,))
    evidence = {
        "counts": {
            "pairs": 18,
            "device_day_clusters": 3,
            "issue_cohorts": 6,
            "horizons": {"15": 6, "30": 6, "60": 6},
            "outdoor_bins": {"0": 18},
        },
        "pending_outcomes": 2,
        "as_of": now.isoformat(),
    }
    with patch.object(
        model_status,
        "verify_reference",
        side_effect=ArtifactUnavailableError(reference, FileNotFoundError()),
    ):
        status = model_status._inspect_model_artifact(
            "thermal", tmp_path, state, evidence, as_of=now, context={}
        )
    assert status["unavailable_reason"] == "artifact_unavailable"
    assert status["counts"]["pairs"] == 18 and status["pending_outcomes"] == 2
    assert status["candidate_hash"] == reference.artifact_hash
    assert status["control_ready"] is False


class TestReadinessReportingRepairs:
    @pytest.fixture
    def captured(self, tmp_path, monkeypatch):
        from zoneinfo import ZoneInfo

        from packages.core import settings_service
        from packages.core.models import DeviceStatusRecord, IndoorTempReading, WeatherRecord
        from packages.ml import model_status
        from packages.ml.lifecycle import LifecycleSnapshot
        from tests.test_ml import TestDemandModel
        from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute

        now = TestR06ThermalQualificationRoute.NOW
        records, readings, weather = TestR06ThermalQualificationRoute.cohort()
        values = {
            key: spec.serialize_default() for key, spec in settings_service.SETTING_SPECS.items()
        }
        values.update(
            {
                "ml_maintenance_enabled": "true",
                "seasonal_calibration_auto_train": "false",
                "timezone": "UTC",
            }
        )

        def copied(rows, model):
            return [
                SimpleNamespace(
                    **{column.name: getattr(row, column.name) for column in model.__table__.columns}
                )
                for row in rows
            ]

        demand_rows = TestDemandModel._lifecycle_preprocessing_rows(390)
        start = now - dt.timedelta(hours=390)
        for result in demand_rows:
            original = result._rows[0].ts
            for row in result._rows:
                row.ts = start + (row.ts - original)
        references = (
            self.reference("thermal", ["indoor_heating", "indoor_cooling"], now),
            self.reference("demand", ["demand"], now),
        )
        monkeypatch.setattr(model_status, "verify_reference", lambda _reference: None)
        return {
            "context": {
                "settings": values,
                "as_of": now,
                "selected_ids": ["selected-room"],
                "reference_sensor_id": "selected-room",
                "device_id": "audit-device",
                "weather_provider": "open-meteo",
                "schemas": {"thermal": "thermal_v5", "demand": "demand_lifecycle_v1"},
            },
            "as_of": now,
            "generation": "readiness-repair2-captured",
            "state": LifecycleSnapshot(
                "readiness-epoch",
                7,
                9,
                True,
                "owned-worker",
                now + dt.timedelta(minutes=2),
                references,
            ),
            "model_dir": tmp_path,
            "evidence": {},
            "requests": {},
            "running_requests": {
                "thermal": {
                    "id": "actual-running-request",
                    "epoch": "readiness-epoch",
                    "fence": 9,
                    "status": "running",
                    "attempted_at": now - dt.timedelta(seconds=1),
                    "finished_at": None,
                    "details": {"phase": "fitting"},
                }
            },
            "queue_counts": {"queued": 2, "running": 1},
            "source_records": len(demand_rows[0]._rows),
            "latest_status": copied(records[-1:], DeviceStatusRecord)[0],
            "seasonal_inputs": {
                "average_outdoor": 5.0,
                "outdoor_readings": 24,
                "open_revert_obligations": {"count": 0},
            },
            "native": None,
            "thermal": {
                "records": copied(records, DeviceStatusRecord),
                "readings": copied(readings, IndoorTempReading),
                "weather": copied(weather, WeatherRecord),
                "indoor_blocker": None,
            },
            "demand": {
                "consumption_rows": demand_rows[0]._rows,
                "weather_rows": demand_rows[1]._rows,
                "statuses": demand_rows[2]._rows,
                "timezone": ZoneInfo("UTC"),
                "lifecycle_evidence": True,
            },
        }

    @staticmethod
    def reference(family, capabilities, now):
        from packages.ml.forecast_quality import NATIVE_POLICY_SCHEMA, QUALIFICATION_POLICY_SCHEMA
        from packages.ml.lifecycle import ArtifactReference, LIFECYCLE_SCHEMAS, Qualification

        scope = {
            "device_id": "audit-device",
            "weather_provider": "open-meteo",
            "policy_schema": NATIVE_POLICY_SCHEMA,
        }
        qualification = Qualification(
            now - dt.timedelta(hours=1),
            now - dt.timedelta(hours=2),
            {
                **scope,
                "horizons": [15, 30, 60],
                "capabilities": list(capabilities),
                "outdoor_bins": [0, 1],
                "qualification_policy": QUALIFICATION_POLICY_SCHEMA,
            },
        )
        validation = {"train_samples": 334, "holdout_samples": 159, "r2": 0.7}
        return ArtifactReference(
            ("a" if family == "thermal" else "b") * 64,
            "readiness-epoch",
            family,
            LIFECYCLE_SCHEMAS[family],
            {
                "data_cutoff": (now - dt.timedelta(days=3)).isoformat(),
                "frozen_at": (now - dt.timedelta(days=3)).isoformat(),
                "samples": 528,
                "scope": scope,
                "horizons": [15, 30, 60],
                "capabilities": list(capabilities),
                "metrics": validation
                if family == "demand"
                else {capability: dict(validation) for capability in capabilities},
            },
            "incumbent",
            7,
            qualification.qualified_at,
            qualification.expires_at,
            qualification.last_valid_outcome_at,
            qualification.scope,
        )

    @staticmethod
    def replace_reference(captured, reference):
        from dataclasses import replace

        captured["state"] = replace(
            captured["state"],
            references=tuple(
                reference if current.component == reference.component else current
                for current in captured["state"].references
            ),
        )

    @staticmethod
    async def compose(captured, monkeypatch, path):
        from packages.ml import model_status

        if path == "composition":
            return model_status._compose_readiness(captured)

        async def capture(**options):
            return {
                **captured,
                "as_of": options["as_of"],
                "generation": options["generation"],
            }

        monkeypatch.setattr(
            model_status, "_capture_readiness_inputs", AsyncMock(side_effect=capture)
        )
        return await model_status.get_readiness_snapshot(
            route="sr-qr-repair2", as_of=captured["as_of"]
        )

    @pytest.mark.parametrize(
        "capabilities",
        [
            ["tank_heating"],
            ["indoor_heating"],
            ["indoor_cooling"],
            ["indoor_heating", "indoor_cooling"],
        ],
        ids=["tank-only", "heating-only", "cooling-only", "both-indoor"],
    )
    @pytest.mark.parametrize("collected", [True, False], ids=["live-collected", "empty-source"])
    def test_SR_QR_F01_UR5_SR2_qualified_reference_is_head_scoped(
        self, captured, capabilities, collected
    ):
        from packages.ml import model_status

        reference = self.reference("thermal", capabilities, captured["as_of"])
        self.replace_reference(captured, reference)
        if not collected:
            captured["thermal"]["readings"] = []
        result = model_status._compose_readiness(captured)
        for component in ("indoor_heating", "indoor_cooling"):
            head = result["readiness"]["components"][component]
            declared = component in capabilities
            assert head["fit"]["status"] == ("validated" if declared else "not_declared")
            assert head["reporting"]["status"] == (
                "qualified-for-reporting" if declared else "unqualified"
            )
            assert head["reporting"]["artifact_hash"] == reference.artifact_hash
            if collected:
                assert head["collection"]["train"] >= 20 and head["collection"]["holdout"] >= 3
                assert head["next_step"] == (
                    "ready_for_reporting" if declared else "await_fit_or_validation"
                )
            else:
                assert head["collection"]["total"] == 0
                assert head["next_step"] == "collect_space_heating_evidence"
        assert result["readiness"]["components"]["demand"]["next_step"] == "ready_for_reporting"

    @pytest.mark.parametrize(
        "scope_case",
        [
            "valid",
            "heating-only",
            "missing",
            "empty",
            "no-capabilities",
            "null-capabilities",
            "string-capabilities",
            "dict-capabilities",
            "invalid-entry",
            "scope-list",
            "scope-string",
            "expired",
            "wrong-policy",
            "wrong-device",
        ],
    )
    def test_SR_QR_F01_UR5_SR2_qualification_scope_cannot_leak(self, captured, scope_case):
        from dataclasses import replace
        from packages.ml import model_status

        reference = captured["state"].reference("thermal")
        scope = dict(reference.qualification_scope)
        if scope_case == "heating-only":
            scope["capabilities"] = ["indoor_heating"]
        elif scope_case == "missing":
            scope = None
        elif scope_case == "empty":
            scope = {}
        elif scope_case == "no-capabilities":
            scope.pop("capabilities")
        elif scope_case in {
            "null-capabilities",
            "string-capabilities",
            "dict-capabilities",
            "invalid-entry",
        }:
            scope["capabilities"] = {
                "null-capabilities": None,
                "string-capabilities": "indoor_heating indoor_cooling",
                "dict-capabilities": {"indoor_heating": True, "indoor_cooling": True},
                "invalid-entry": ["indoor_heating", None],
            }[scope_case]
        elif scope_case in {"scope-list", "scope-string"}:
            scope = ["indoor_heating"] if scope_case == "scope-list" else "indoor_heating"
        elif scope_case == "wrong-policy":
            scope["qualification_policy"] = "unknown-policy"
        elif scope_case == "wrong-device":
            scope["device_id"] = "another-device"
        reference = replace(reference, qualification_scope=scope)
        if scope_case == "expired":
            reference = replace(reference, qualification_expires_at=captured["as_of"])
        self.replace_reference(captured, reference)
        result = model_status._compose_readiness(captured)
        for component in ("indoor_heating", "indoor_cooling"):
            head = result["readiness"]["components"][component]
            qualified = scope_case == "valid" or (
                scope_case == "heating-only" and component == "indoor_heating"
            )
            assert (head["reporting"]["status"] == "qualified-for-reporting") is qualified
            assert (head["next_step"] == "ready_for_reporting") is qualified
        assert (
            result["readiness"]["components"]["demand"]["reporting"]["status"]
            == "qualified-for-reporting"
        )

    @pytest.mark.parametrize("path", ["composition", "generation"])
    @pytest.mark.parametrize("family", ["thermal", "demand"])
    @pytest.mark.parametrize(
        "metrics",
        [None, ["private-artifact-path"], "private-artifact-path"],
        ids=["null", "list", "string"],
    )
    async def test_SR_QR_F02_UR5_SR4_malformed_metrics_preserve_collection(
        self, captured, monkeypatch, path, family, metrics
    ):
        from dataclasses import replace
        from packages.ml import model_status

        good = model_status._compose_readiness(captured)
        reference = captured["state"].reference(family)
        self.replace_reference(
            captured, replace(reference, manifest={**reference.manifest, "metrics": metrics})
        )
        result = await self.compose(captured, monkeypatch, path)
        self.assert_live_evidence_preserved(good, result, path)
        for component, head in result["readiness"]["components"].items():
            malformed = (component == "demand") == (family == "demand")
            if malformed:
                assert head["fit"]["availability"] == "unavailable"
                assert head["fit"]["status"] is None and head["fit"]["validation"] is None
                assert head["fit"]["reason"] == "integrity_or_schema_failed"
                assert head["fit"]["artifact_hash"] == reference.artifact_hash
                assert head["reporting"]["status"] == "unavailable"
                assert head["reporting"]["reason"] == "integrity_or_schema_failed"
            else:
                assert head["fit"] == good["readiness"]["components"][component]["fit"]
                assert head["reporting"] == good["readiness"]["components"][component]["reporting"]
        assert "private-artifact-path" not in json.dumps(result, default=str)

    @pytest.mark.parametrize("path", ["composition", "generation"])
    @pytest.mark.parametrize(
        "metrics",
        [None, ["private-nested-path"], "private-nested-path"],
        ids=["null", "list", "string"],
    )
    async def test_SR_QR_F02_UR5_SR4_nested_metrics_fail_only_the_head(
        self, captured, monkeypatch, path, metrics
    ):
        from dataclasses import replace
        from packages.ml import model_status

        good = model_status._compose_readiness(captured)
        reference = captured["state"].reference("thermal")
        self.replace_reference(
            captured,
            replace(
                reference,
                manifest={
                    **reference.manifest,
                    "metrics": {**reference.manifest["metrics"], "indoor_heating": metrics},
                },
            ),
        )
        result = await self.compose(captured, monkeypatch, path)
        self.assert_live_evidence_preserved(good, result, path)
        heating = result["readiness"]["components"]["indoor_heating"]
        assert heating["fit"]["availability"] == "unavailable"
        assert heating["fit"]["validation"] is None and heating["fit"]["status"] is None
        assert heating["reporting"]["status"] == "unavailable"
        assert (
            heating["fit"]["reason"]
            == heating["reporting"]["reason"]
            == "integrity_or_schema_failed"
        )
        assert heating["next_step"] == "readiness_unavailable"
        for component in ("indoor_cooling", "demand"):
            assert (
                result["readiness"]["components"][component]
                == good["readiness"]["components"][component]
            )
        assert result["model_lifecycle"]["thermal"]["metrics"]["indoor_heating"] is None
        assert (
            result["model_lifecycle"]["thermal"]["metrics"]["indoor_cooling"]
            == (good["model_lifecycle"]["thermal"]["metrics"]["indoor_cooling"])
        )
        assert (
            captured["state"].reference("thermal").manifest["metrics"]["indoor_heating"] == metrics
        )
        assert "private-nested-path" not in json.dumps(result, default=str)

    @staticmethod
    def assert_live_evidence_preserved(good, result, path):
        readiness = result["readiness"]
        assert readiness["availability"] == "assessed" and readiness["reason"] is None
        for field in ("as_of", "context_digest", "source_watermarks", "maintenance"):
            assert readiness[field] == good["readiness"][field]
        if path == "composition":
            assert readiness["generation"] == good["readiness"]["generation"]
        else:
            assert (
                readiness["generation"]
                and readiness["generation"] != good["readiness"]["generation"]
            )
        assert (
            result["context"] == good["context"]
            and result["seasonal_inputs"] == good["seasonal_inputs"]
        )
        assert result["demand_quality"] == good["demand_quality"]
        assert result["device_quality"] == good["device_quality"]
        for component, head in readiness["components"].items():
            assert head["collection"] == good["readiness"]["components"][component]["collection"]
        assert readiness["components"]["indoor_heating"]["collection"]["train"] >= 20
        assert readiness["components"]["demand"]["collection"]["total"] == 390
        assert (
            readiness["maintenance"]["running"] is True
            and readiness["maintenance"]["queue_count"] == 2
        )

    @pytest.mark.parametrize("path", ["composition", "generation"])
    async def test_SR_QR_F02_UR5_SR4_artifact_inspection_keeps_live_and_other_components(
        self, captured, monkeypatch, path
    ):
        from packages.ml import model_status
        from packages.ml.lifecycle import ArtifactUnavailableError

        good = model_status._compose_readiness(captured)

        def inspect(reference):
            if reference.component == "thermal":
                raise ArtifactUnavailableError(
                    reference, FileNotFoundError("private-artifact-path")
                )

        monkeypatch.setattr(model_status, "verify_reference", inspect)
        result = await self.compose(captured, monkeypatch, path)
        self.assert_live_evidence_preserved(good, result, path)
        assert (
            result["readiness"]["components"]["demand"] == good["readiness"]["components"]["demand"]
        )
        for component in ("indoor_heating", "indoor_cooling"):
            head = result["readiness"]["components"][component]
            assert head["fit"]["availability"] == "unavailable"
            assert head["fit"]["reason"] == "artifact_unavailable"
            assert head["reporting"]["status"] == "unavailable"
        assert "private-artifact-path" not in json.dumps(result, default=str)

    @pytest.mark.parametrize("failure", ["source", "cancellation"])
    async def test_SR_QR_F02_UR2_SR4_source_failure_and_cancellation_are_not_good_data(
        self, captured, monkeypatch, failure
    ):
        from packages.ml import model_status

        error = (
            OSError("private-source-detail") if failure == "source" else asyncio.CancelledError()
        )
        monkeypatch.setattr(model_status, "_capture_readiness_inputs", AsyncMock(side_effect=error))
        if failure == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await model_status.get_readiness_snapshot(
                    route="sr-qr-repair2", as_of=captured["as_of"]
                )
        else:
            result = await model_status.get_readiness_snapshot(
                route="sr-qr-repair2", as_of=captured["as_of"]
            )
            assert result["readiness"]["availability"] == "unavailable"
            assert all(
                head["collection"]["total"] is None
                for head in result["readiness"]["components"].values()
            )
            assert result["readiness"]["context_digest"] is None
            assert "private-source-detail" not in json.dumps(result["readiness"])
        assert not model_status._READINESS_FLIGHTS.get(asyncio.get_running_loop())


def _thermal_model(**params) -> ThermalModel:
    model = ThermalModel()
    model.arefresh_if_changed = AsyncMock(return_value=False)
    model.params.tank_heating_rate = 2.5
    for name, value in params.items():
        setattr(model.params, name, value)
    return model


_RETIRED_COP_STATUS = {
    "trained": False,
    "last_trained": None,
    "samples": 0,
    "source_records": 0,
    "metrics": None,
    "status": "retired",
    "supported": False,
    "unavailable_reason": "retired",
    "reason": "unverified_thermal_output",
}


def _session_context(session):
    class _AsyncContextManager:
        def __init__(self, value):
            self._value = value

        async def __aenter__(self):
            return self._value

        async def __aexit__(self, *args):
            return False

    return _AsyncContextManager(session)


@pytest.fixture
def optimizer_status_data_quality(monkeypatch):
    reader = AsyncMock(
        return_value={
            "timestamp": None,
            "age_seconds": None,
            "threshold_seconds": 900,
            "ready": False,
        }
    )
    monkeypatch.setattr("packages.api.routers.optimizer.get_device_data_quality", reader)
    return reader


@pytest.fixture
def optimizer_status_api(monkeypatch, optimizer_status_data_quality):
    from packages.api.routers import optimizer
    from packages.ml import model_status
    from packages.core.settings_service import SETTING_SPECS

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    clock = MagicMock(return_value=now)
    monkeypatch.setattr(model_status, "_now", clock)
    layer_setting = AsyncMock(return_value="auto")
    monkeypatch.setattr("packages.core.settings_service.get_setting", layer_setting)
    snapshot = AsyncMock(
        return_value={
            "active_layer": "rules_v7",
            "effective_layer": "rules",
            "selection_reason": "auto_milp_disabled_cop_retired",
        }
    )
    monkeypatch.setattr("packages.optimizer.main.get_optimizer_status_snapshot", snapshot)
    learning = {
        "enabled": False,
        "linked_restores_eligible": True,
        "open_revert_obligations": {"count": 1},
    }
    monkeypatch.setattr(optimizer, "_learning_mode_status", AsyncMock(return_value=learning))
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_status",
        AsyncMock(return_value=dict(_RETIRED_COP_STATUS)),
    )
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_reporting_statuses",
        AsyncMock(
            return_value={
                component: {"status": "unqualified", "control_ready": False}
                for component in ("demand", "comfort", "thermal")
            }
        ),
    )
    monkeypatch.setattr(
        "packages.ml.models.DemandModel.training_data_quality",
        AsyncMock(return_value={"usable_samples": 0}),
    )
    monkeypatch.setattr("packages.ml.thermal.thermal_model", _thermal_model())
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_progress",
        AsyncMock(return_value={"enabled": False, "observe_only_active": False}),
    )
    captured_input = SimpleNamespace(values=None, status=None)

    async def capture(**_kwargs):
        as_of = model_status._now(dt.timezone.utc)
        result = model_status._unavailable_readiness(
            "fixture", as_of=as_of, generation="freshness-fixture"
        )
        values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
        values["optimizer_layer"] = layer_setting.return_value
        if captured_input.values:
            values.update(captured_input.values)
        result["context"] = {"settings": values, "as_of": as_of}
        try:
            result["device_quality"] = await optimizer_status_data_quality(
                now=as_of, values=captured_input.values or values, status=captured_input.status
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result["device_quality"] = None
        return result

    capture_reader = AsyncMock(side_effect=capture)
    monkeypatch.setattr(model_status, "get_readiness_snapshot", capture_reader)
    session = AsyncMock()
    session.add = MagicMock()
    session.execute.return_value = SimpleNamespace(scalar=lambda: 42)
    monkeypatch.setattr(optimizer, "get_session", lambda: _session_context(session))
    with (
        patch("packages.core.services.aquarea.AquareaWrapper.start", new=AsyncMock()) as device,
        patch("redis.asyncio.Redis.from_url") as redis_factory,
        patch("redis.asyncio.from_url") as redis_connection,
        patch("packages.core.settings_service.set_setting", new=AsyncMock()) as single_write,
        patch("packages.core.settings_service.set_settings_bulk", new=AsyncMock()) as bulk_write,
    ):
        yield SimpleNamespace(
            now=now,
            clock=clock,
            data_quality=optimizer_status_data_quality,
            layer_setting=layer_setting,
            snapshot=snapshot,
            learning=learning,
            inputs=captured_input,
            capture=capture_reader,
        )
        device.assert_not_awaited()
        redis_factory.assert_not_called()
        redis_connection.assert_not_called()
        single_write.assert_not_awaited()
        bulk_write.assert_not_awaited()
    assert all(call.args[0].is_select for call in session.execute.await_args_list)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.parametrize("layer", ["rules_only", "auto"])
@pytest.mark.parametrize(
    ("age", "timestamp_kind", "credentials", "expected_age", "fresh"),
    [
        pytest.param(60.4, "offset", True, 60, True, id="fresh"),
        pytest.param(900, "naive", True, 900, True, id="threshold"),
        pytest.param(901, "utc", True, 901, False, id="stale"),
        pytest.param(None, "utc", True, None, False, id="missing"),
        pytest.param(60, "utc", False, 60, False, id="credentials-missing"),
        pytest.param(-30, "utc", True, None, False, id="future"),
    ],
)
async def test_optimizer_status_data_freshness_uses_real_helper(
    optimizer_status_api, monkeypatch, layer, age, timestamp_kind, credentials, expected_age, fresh
):
    from packages.core.device_data_quality import get_device_data_quality

    fixture = optimizer_status_api
    fixture.layer_setting.return_value = layer
    selection_reason = "auto_milp_disabled_cop_retired" if layer == "auto" else "configured_layer"
    fixture.snapshot.return_value["selection_reason"] = selection_reason
    timestamp = fixture.now - dt.timedelta(seconds=age) if age is not None else None
    stored_timestamp = timestamp
    if timestamp is not None and timestamp_kind == "naive":
        stored_timestamp = timestamp.replace(tzinfo=None)
    elif timestamp is not None and timestamp_kind == "offset":
        stored_timestamp = timestamp.astimezone(dt.timezone(dt.timedelta(hours=2)))
    settings_values = {
        "aquarea_username": "fixture-only-user" if credentials else "",
        "aquarea_password": "fixture-only-password" if credentials else "",
        "device_status_max_age_minutes": "15",
        "poll_interval_seconds": "300",
    }
    settings_reader = AsyncMock(side_effect=AssertionError("later settings read"))
    monkeypatch.setattr("packages.core.device_data_quality.get_setting", settings_reader)
    session = AsyncMock()
    session.add = MagicMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: stored_timestamp)
    monkeypatch.setattr(
        "packages.core.device_data_quality.get_session", lambda: _session_context(session)
    )
    fixture.inputs.values = settings_values
    fixture.inputs.status = stored_timestamp
    quality_reader = fixture.data_quality
    quality_reader.side_effect = get_device_data_quality

    response = await get_optimizer_status()

    assert response["data_freshness"] == {
        "latest_device_status": timestamp.isoformat() if timestamp else None,
        "age_seconds": expected_age,
        "stale_after_seconds": 900,
        "fresh": fresh,
    }
    quality_reader.assert_awaited_once_with(
        now=fixture.now, values=settings_values, status=stored_timestamp
    )
    fixture.clock.assert_called_once_with(dt.timezone.utc)
    settings_reader.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.delete.assert_not_awaited()
    assert response["configured_layer"] == layer
    assert response["active_layer"] == "rules_v7"
    assert response["effective_layer"] == "rules"
    assert response["selection_reason"] == selection_reason
    assert response["build_policy"] == "observe_only"
    assert response["control_ready"] is response["ordinary_writes_allowed"] is False
    assert response["optimization_requires_new_build"] is True
    assert response["learning_mode"] == fixture.learning
    assert "fixture-only-user" not in json.dumps(response)
    assert "fixture-only-password" not in json.dumps(response)


async def test_optimizer_status_data_freshness_lookup_failure_preserves_response(
    optimizer_status_api,
):
    fixture = optimizer_status_api
    expected = await get_optimizer_status()
    expected.pop("data_freshness")
    fixture.data_quality.reset_mock()
    fixture.clock.reset_mock()
    fixture.data_quality.side_effect = RuntimeError("private freshness lookup failure")

    response = await get_optimizer_status()

    assert "data_freshness" not in response
    assert response == expected
    assert fixture.data_quality.await_args.kwargs["now"] == fixture.now
    fixture.clock.assert_called_once_with(dt.timezone.utc)
    assert response["build_policy"] == "observe_only"
    assert response["control_ready"] is response["ordinary_writes_allowed"] is False
    assert response["optimization_requires_new_build"] is True
    assert "private freshness lookup failure" not in json.dumps(response)


async def test_optimizer_status_data_freshness_propagates_cancellation(optimizer_status_api):
    fixture = optimizer_status_api
    fixture.data_quality.side_effect = asyncio.CancelledError("fixture cancellation")

    with pytest.raises(asyncio.CancelledError):
        await get_optimizer_status()

    assert fixture.data_quality.await_args.kwargs["now"] == fixture.now
    fixture.clock.assert_called_once_with(dt.timezone.utc)


@pytest.mark.parametrize("planner", ["rules", "milp", "fallback"])
@pytest.mark.parametrize("force_replace", [False, True])
@pytest.mark.parametrize("learning_state", ["active", "inactive"])
async def test_diagnostic_has_zero_actions_and_is_never_active(
    planner, force_replace, learning_state
):
    from packages.core.models import PlanActionRecord, PlanRecord
    from packages.optimizer import InfeasibleError
    from packages.optimizer.executor_core import LearningModeState
    from packages.optimizer.main import run_optimization

    now = dt.datetime.now(dt.timezone.utc)
    plan = {
        "horizon_start": now,
        "horizon_end": now + dt.timedelta(hours=24),
        "version": "rules_v7" if planner == "rules" else "milp_v1",
        "actions": [{"type": "force_dhw_on", "ts": now.isoformat(), "payload": {}}],
        "forecast_snapshot": {"device_id": "device-a", "price_forecast": []},
    }
    optimizer = SimpleNamespace(generate_plan=AsyncMock(return_value=plan))
    fallback = SimpleNamespace(generate_plan=AsyncMock(return_value=plan))
    if planner == "fallback":
        optimizer.generate_plan.side_effect = InfeasibleError("isolated fallback")
    session = AsyncMock()
    records = []

    def add(record):
        record.id = 321
        records.append(record)

    session.add = MagicMock(side_effect=add)
    with (
        patch("packages.optimizer.main.get_session", return_value=_session_context(session)),
        patch("packages.optimizer.main.get_setting", new=AsyncMock(return_value=planner)),
        patch(
            "packages.optimizer.main._select_optimizer",
            new=AsyncMock(return_value=("rules" if planner == "rules" else "milp", optimizer)),
        ),
        patch("packages.optimizer.main._new_rules_optimizer", return_value=fallback),
        patch(
            "packages.optimizer.main.resolve_learning_mode_state",
            new=AsyncMock(return_value=LearningModeState(learning_state)),
        ),
        patch(
            "packages.optimizer.main.get_device_data_quality",
            new=AsyncMock(return_value={"ready": True}),
        ),
        patch(
            "packages.optimizer.main.get_planning_data_quality",
            new=AsyncMock(return_value={"control_allowed": True}),
        ),
        patch("packages.optimizer.main.comfort_model.arefresh_if_changed", new=AsyncMock()),
        patch(
            "packages.optimizer.main.get_active_price_context",
            new=AsyncMock(
                return_value=SimpleNamespace(area="SE3", source="manual", currency="SEK")
            ),
        ),
        patch(
            "packages.optimizer.main._has_material_near_term_change",
            new=AsyncMock(return_value=(False, 42)),
        ) as reuse,
        patch("packages.core.plan_lifecycle.activate_plan", new=AsyncMock()) as activate,
    ):
        assert await run_optimization(force_replace=force_replace) == 321

    assert len(records) == 1
    assert isinstance(records[0], PlanRecord)
    assert not any(isinstance(record, PlanActionRecord) for record in records)
    assert records[0].status == "diagnostic"
    assert json.loads(records[0].plan_json)["actions"] == []
    session.execute.assert_not_awaited()
    reuse.assert_not_awaited()
    activate.assert_not_awaited()
    if planner == "fallback":
        fallback.generate_plan.assert_awaited_once()


async def test_activate_plan_refuses_diagnostic_before_supersession():
    from packages.core.models import PlanRecord
    from packages.core.plan_lifecycle import activate_plan

    session = AsyncMock()
    session.add = MagicMock()
    plan = PlanRecord(status="diagnostic", plan_json='{"actions": []}')

    with pytest.raises(ValueError, match="Diagnostic plans cannot be activated"):
        await activate_plan(session, plan)

    session.execute.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
    assert plan.status == "diagnostic"


def test_public_forecast_quality_is_reporting_only_without_changing_prediction_evidence():
    from packages.api.routers.models_router import _reporting_forecast_quality

    quality = {"status": "passed", "control_allowed": True, "reason": "validated", "samples": 60}

    reported = _reporting_forecast_quality(quality)

    assert reported == {
        "status": "passed",
        "control_allowed": False,
        "control_reason": "observe_only_build",
        "reason": "validated",
        "samples": 60,
    }
    assert quality == {
        "status": "passed",
        "control_allowed": True,
        "reason": "validated",
        "samples": 60,
    }
    assert reported is not quality


@pytest.mark.parametrize("scopes", [["plans"], ["prices", "plans"]])
async def test_reset_refuses_open_restore_11763(scopes):
    from fastapi import HTTPException
    from packages.api.routers.admin import ResetRequest, reset_data

    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 11763
    session.execute.return_value = result
    with (
        patch("packages.api.routers.admin.get_session", return_value=_session_context(session)),
        patch("packages.api.routers.admin.reset_ml_models") as reset_models,
        pytest.raises(HTTPException) as error,
    ):
        await reset_data(ResetRequest(scopes=scopes))

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "unresolved_safety_reverts", "action_id": 11763}
    session.scalar.assert_not_awaited()
    session.add.assert_not_called()
    reset_models.assert_not_called()
    assert session.execute.await_count == 1
    statement = session.execute.await_args.args[0]
    assert statement.__visit_name__ == "select"
    assert "reverts_action_id IS NOT NULL" in str(statement)
    assert statement._for_update_arg is not None


@pytest.mark.parametrize("learning_state", ["active", "inactive", "unknown"])
async def test_restart_and_rollback_preserve_restore_and_embargo(learning_state):
    from packages.core.models import PlanActionRecord, PlanRecord
    from packages.core.plan_lifecycle import supersede_active_plan
    from packages.core.safety_reverts import dhw_embargoed
    from packages.core.services.aquarea import AquareaWrapper
    from packages.optimizer.executor_core import LearningModeState, PlanExecutor

    now = dt.datetime.now(dt.timezone.utc)
    restore = PlanActionRecord(
        id=11763,
        plan_id=1,
        device_id="device-a",
        action_type="force_dhw_off",
        payload_json="{}",
        reverts_action_id=11762,
        status="pending",
        scheduled_ts=now + dt.timedelta(hours=1),
    )
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [PlanRecord(id=1, status="active")]
    session.execute.return_value = result
    await supersede_active_plan(session)
    cancellation = session.execute.await_args_list[1].args[0]
    assert "reverts_action_id IS NULL" in str(cancellation)
    assert restore.status == "pending"
    assert dhw_embargoed([restore], "device-a") is True
    for _restart in range(2):
        wrapper = AquareaWrapper(read_only=False)
        executor = PlanExecutor(
            wrapper, learning_check=AsyncMock(return_value=LearningModeState(learning_state))
        )
        executor._claim_due_safety_action = AsyncMock(return_value=None)
        await executor.execute_due_actions()
        with pytest.raises(PermissionError, match="observe-only build"):
            await wrapper.force_dhw(True)
        assert restore.reverts_action_id == 11762
        assert restore.status == "pending"
        assert dhw_embargoed([restore], "device-a") is True


async def test_default_history_excludes_diagnostics_and_explicit_detail_has_no_outcome():
    from fastapi import HTTPException
    from packages.api.routers.optimizer import get_plan_activity
    from packages.core.models import PlanRecord

    now = dt.datetime.now(dt.timezone.utc)
    diagnostic = PlanRecord(
        id=999,
        created_at=now,
        horizon_start=now,
        horizon_end=now + dt.timedelta(hours=24),
        optimizer_version="rules_v7",
        price_currency="SEK",
        price_source="manual",
        plan_json='{"actions": []}',
        status="diagnostic",
    )
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [(diagnostic, 0)]
    result.scalar_one_or_none.return_value = diagnostic
    session.execute.return_value = result
    with (
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
        patch("packages.api.routers.optimizer.plan_measurement", new=AsyncMock()) as measurement,
    ):
        assert await get_plans(limit=10) == []
        assert "diagnostic" in session.execute.await_args.args[0].compile().params.values()
        with pytest.raises(HTTPException) as error:
            await get_plan_detail(999)
        assert error.value.status_code == 404
        detail = await get_plan_detail(999, include_diagnostics=True)
        assert detail.status == "diagnostic"
        assert detail.actions_count == 0
        assert detail.actions == []
        assert detail.outcome["execution_allowed"] is False
        assert "measurement" not in detail.outcome
        measurement.assert_not_awaited()
        result.all.return_value = []
        assert await get_plan_activity(limit=100, status=None) == []
        assert "diagnostic" in session.execute.await_args.args[0].compile().params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("learning_state", "expected_learning_mode", "expected_reliable", "expected_plan_driven"),
    [
        ("active", True, True, False),
        ("inactive", False, True, False),
        ("unknown", False, False, False),
    ],
)
async def test_thermal_curve_projects_tri_state_learning_contract(
    learning_state, expected_learning_mode, expected_reliable, expected_plan_driven
):
    from packages.optimizer.executor_core import LearningModeState

    status = SimpleNamespace(tank_temp=48.0, tank_target_temp=52, outdoor_temp=7.0, zone1_temp=20.0)
    active_plan = SimpleNamespace(id=42)
    action = SimpleNamespace(
        action_type="force_dhw_on",
        scheduled_ts=dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0),
        payload_json=json.dumps({"dhw_minutes": 30}),
    )
    status_result = SimpleNamespace(scalar_one_or_none=lambda: status)
    plan_result = SimpleNamespace(scalar_one_or_none=lambda: active_plan)
    actions_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [action]))
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[status_result, plan_result, actions_result])
    )
    thermal_model = SimpleNamespace(
        load_latest=MagicMock(),
        arefresh_if_changed=AsyncMock(return_value=False),
        predict_temperature_curve=MagicMock(return_value=[]),
        predict_planned_tank_curve=MagicMock(return_value=[]),
        predict_managed_tank_curve=MagicMock(return_value=[]),
    )
    with (
        patch(
            "packages.api.routers.models_router.get_session", return_value=_session_context(session)
        ),
        patch(
            "packages.core.settings_service.get_comfort_schedule", new=AsyncMock(return_value={})
        ),
        patch("packages.core.settings_service.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value=None)),
        patch(
            "packages.optimizer.executor_core.resolve_learning_mode_state",
            new=AsyncMock(return_value=LearningModeState(learning_state)),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
    ):
        response = await get_thermal_curve(hours=1)

    assert response["current"] == {
        "tank_temp": 48.0,
        "tank_target": 52,
        "outdoor_temp": 7.0,
        "zone1_temp": 20.0,
        "tank_min_temp": response["current"]["tank_min_temp"],
        "tank_min_temp_offpeak": response["current"]["tank_min_temp_offpeak"],
        "plan_driven": expected_plan_driven,
        "learning_mode": expected_learning_mode,
        "learning_mode_state": learning_state,
        "learning_mode_reliable": expected_reliable,
        "plan_id": 42,
    }


@pytest.mark.asyncio
async def test_api_thermal_process_refreshes_committed_rates(monkeypatch):
    from packages.api.routers import models_router
    from tests.test_ml_lifecycle import qualified_reference

    now = dt.datetime.now(dt.timezone.utc)
    status = SimpleNamespace(
        tank_temp=48.0, tank_target_temp=52, outdoor_temp=10.0, zone1_temp=20.0, ts=now
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: status))
    )
    model = ThermalModel()

    async def refresh():
        model.params.tank_heating_rate = 8.0
        model.params.tank_heating_outdoor_factor = 0.0
        model.params.last_calibrated = now
        model.params.calibration_status["tank_heating"] = "validated"
        model._qualified_reference = qualified_reference("thermal", ("tank_heating",))
        return True

    refresh_reader = AsyncMock(side_effect=refresh)
    model.arefresh_if_changed = refresh_reader
    monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
    monkeypatch.setattr(models_router, "get_session", lambda: _session_context(session))
    monkeypatch.setattr(
        "packages.core.control_temperature.get_control_temperature",
        AsyncMock(return_value=SimpleNamespace(is_usable=False, value=None, reason="missing")),
    )
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_status",
        AsyncMock(return_value={"status": "qualified-for-reporting"}),
    )
    response = await models_router.get_thermal_status()
    assert response["reporting_state"] == "qualified-for-reporting"
    assert response["predictions"]["tank_heating"]["heating_rate_per_hour"] == 8.0
    assert response["predictions"]["tank_heating"]["confidence"] == "learned"
    assert response["control_ready"] is False and response["ordinary_writes_allowed"] is False
    refresh_reader.assert_awaited_once()


async def test_optimizer_status_preserves_captured_thermal_even_if_seasonal_reporting_fails(
    monkeypatch,
):
    from packages.api.routers import optimizer
    from packages.ml import model_status

    model = ThermalModel()
    now = dt.datetime.now(dt.timezone.utc)
    before = repr(model.params)
    data_quality = AsyncMock(side_effect=AssertionError("later quality snapshot"))
    monkeypatch.setattr(optimizer, "get_device_data_quality", data_quality)
    captured = model_status._unavailable_readiness(
        "fixture", as_of=now, generation="captured-thermal"
    )
    captured.update(
        {
            "context": {"settings": {"optimizer_layer": "rules_only"}},
            "device_quality": {
                "timestamp": now,
                "age_seconds": 0,
                "threshold_seconds": 900,
                "ready": True,
            },
        }
    )
    captured["thermal_model"].update(
        {"tank_heating_rate": 8.0, "confidence": "learned", "calibrated": True}
    )
    snapshot_reader = AsyncMock(return_value=captured)
    monkeypatch.setattr(model_status, "get_readiness_snapshot", snapshot_reader)
    refresh_reader = AsyncMock(side_effect=AssertionError("singleton refresh"))
    model.arefresh_if_changed = refresh_reader
    monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
    monkeypatch.setattr(
        "packages.optimizer.main.get_optimizer_status_snapshot",
        AsyncMock(return_value={"active_layer": "rules_v7"}),
    )
    monkeypatch.setattr(
        optimizer, "_learning_mode_status", AsyncMock(return_value={"enabled": False})
    )
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_status", AsyncMock(return_value={"status": "retired"})
    )
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_progress",
        AsyncMock(side_effect=RuntimeError("seasonal lookup unavailable")),
    )
    response = await optimizer.get_optimizer_status()
    assert response["thermal_model"]["tank_heating_rate"] == 8.0
    assert response["thermal_model"]["confidence"] == "learned"
    assert response["seasonal_calibration"]["reason"] == "seasonal_reporting_unavailable"
    assert response["control_ready"] is False and response["ordinary_writes_allowed"] is False
    refresh_reader.assert_not_awaited()
    data_quality.assert_not_awaited()
    snapshot_reader.assert_awaited_once_with(route="/api/optimizer/status")
    assert before == repr(model.params)
    assert response["data_freshness"] == {
        "latest_device_status": now.isoformat(),
        "age_seconds": 0,
        "stale_after_seconds": 900,
        "fresh": True,
    }


@pytest.mark.parametrize(
    "reader", ["get_thermal_status", "get_thermal_curve", "get_indoor_forecast"]
)
async def test_every_api_thermal_reader_refreshes_before_reading_inputs(monkeypatch, reader):
    from packages.api.routers import models_router

    model = ThermalModel()
    refresh_reader = AsyncMock(return_value=False)
    model.arefresh_if_changed = refresh_reader
    monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
    monkeypatch.setattr(models_router, "_read_live_forecast_quality", AsyncMock(return_value={}))
    monkeypatch.setattr(models_router, "get_price_area", AsyncMock(return_value="SE3"))
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_status",
        AsyncMock(return_value={"status": "unqualified"}),
    )

    def stop_after_refresh():
        refresh_reader.assert_awaited_once()
        raise RuntimeError("stop before fixture input reads")

    monkeypatch.setattr(models_router, "get_session", stop_after_refresh)
    with pytest.raises(RuntimeError, match="stop before fixture input reads"):
        await getattr(models_router, reader)()


async def test_thermal_curve_lookup_failure_returns_unknown_metadata():
    status = SimpleNamespace(
        id=1,
        tank_temp=48.0,
        tank_target_temp=52,
        outdoor_temp=7.0,
        zone1_temp=20.0,
    )
    status_result = SimpleNamespace(scalar_one_or_none=lambda: status)
    session = SimpleNamespace(execute=AsyncMock(return_value=status_result))
    thermal_model = SimpleNamespace(
        load_latest=MagicMock(),
        arefresh_if_changed=AsyncMock(return_value=False),
        predict_temperature_curve=MagicMock(return_value=[]),
        predict_managed_tank_curve=MagicMock(return_value=[]),
    )
    with (
        patch(
            "packages.api.routers.models_router.get_session", return_value=_session_context(session)
        ),
        patch(
            "packages.core.settings_service.get_comfort_schedule", new=AsyncMock(return_value={})
        ),
        patch("packages.core.settings_service.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value=None)),
        patch(
            "packages.optimizer.executor_core.resolve_learning_mode_state",
            new=AsyncMock(side_effect=RuntimeError("unavailable")),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
    ):
        response = await get_thermal_curve(hours=1)

    assert response["current"]["learning_mode_state"] == "unknown"
    assert response["current"]["learning_mode"] is False
    assert response["current"]["learning_mode_reliable"] is False


def test_dashboard_response_serializes_space_heating_gate_evidence() -> None:
    response = DashboardResponse(
        space_heating_gate={
            "state": "BLOCKED",
            "reason": "above_off_threshold",
            "profile_id": "WH_MXC12J9E8_J_DEFAULT",
            "on_threshold_c": 13.0,
            "off_threshold_c": 15.0,
            "fingerprint_matches": True,
        }
    )

    assert response.model_dump(mode="json")["space_heating_gate"] == {
        "state": "BLOCKED",
        "reason": "above_off_threshold",
        "profile_id": "WH_MXC12J9E8_J_DEFAULT",
        "on_threshold_c": 13.0,
        "off_threshold_c": 15.0,
        "fingerprint_matches": True,
    }


@pytest.mark.asyncio
async def test_dashboard_uses_data_quality_threshold_for_status_freshness() -> None:
    empty_status_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    empty_price_result = SimpleNamespace(one_or_none=lambda: None)
    empty_consumption_result = SimpleNamespace(one_or_none=lambda: None)
    empty_records_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    empty_prices_result = SimpleNamespace(all=lambda: [])
    empty_plan_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    empty_override_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                empty_status_result,
                empty_price_result,
                empty_consumption_result,
                empty_records_result,
                empty_prices_result,
                empty_plan_result,
                empty_override_result,
            ]
        )
    )
    freshness_check = MagicMock(return_value=False)

    with (
        patch(
            "packages.api.routers.dashboard.get_device_data_quality",
            new=AsyncMock(return_value={"threshold_seconds": 900}),
        ) as get_device_data_quality,
        patch("packages.api.routers.dashboard.get_user_tz", new=AsyncMock(return_value="UTC")),
        patch("packages.api.routers.dashboard.get_price_area", new=AsyncMock(return_value="FI")),
        patch(
            "packages.api.routers.dashboard.get_space_heating_gate_config",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "packages.api.routers.dashboard.resolve_effective_gate",
            return_value=SimpleNamespace(
                state="UNKNOWN",
                reason_code="no_data",
                profile_id=None,
                on_operator=None,
                off_operator=None,
                base_c=None,
                on_threshold_c=None,
                off_threshold_c=None,
                last_raw_outdoor_c=None,
                fingerprint_matches=None,
            ),
        ),
        patch("packages.api.routers.dashboard.get_session", return_value=_session_context(session)),
        patch(
            "packages.api.routers.dashboard.device_status_is_fresh",
            freshness_check,
        ),
    ):
        await get_dashboard()

    get_device_data_quality.assert_awaited_once()
    assert freshness_check.call_args.kwargs["threshold_seconds"] == 900
    dashboard_sql = [
        str(call.args[0])
        for call in session.execute.call_args_list
        if "source_date" in str(call.args[0])
    ]
    assert any("consumption.ts >=" in sql and "consumption.ts <=" in sql for sql in dashboard_sql)


@pytest.mark.asyncio
async def test_stats_source_date_grouping_keeps_timestamp_bounds() -> None:
    consumption_result = SimpleNamespace(one=lambda: (1.0, 2.0, 3.0))
    price_result = SimpleNamespace(scalar=lambda: 0.1)
    session = SimpleNamespace(execute=AsyncMock(side_effect=[consumption_result, price_result]))

    with patch(
        "packages.api.routers.dashboard.get_session", return_value=_session_context(session)
    ):
        response = await get_stats("day")

    assert response.total_kwh == 6.0
    stats_sql = str(session.execute.call_args_list[0].args[0])
    assert "source_date" in stats_sql
    assert "consumption.ts >=" in stats_sql
    assert "consumption.ts <=" in stats_sql


@pytest.mark.asyncio
async def test_optimizer_status_uses_filtered_demand_training_quality(tmp_path) -> None:
    from packages.ml import model_status

    quality = {
        "raw_records": 188,
        "intervals": 187,
        "usable_samples": 19,
        "minimum_samples": 168,
        "remaining_samples": 149,
        "ready_to_train": False,
    }
    count_result = MagicMock()
    count_result.scalar.return_value = 188
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = _thermal_model()
    captured = model_status._unavailable_readiness(
        "fixture", as_of=dt.datetime.now(dt.timezone.utc), generation="filtered-demand-fixture"
    )
    captured["context"] = {"settings": {"optimizer_layer": "rules_only"}}
    captured["demand_quality"] = quality
    captured["model_lifecycle"]["demand"]["source_records"] = 188

    with (
        patch.object(
            model_status, "get_readiness_snapshot", new=AsyncMock(return_value=captured)
        ) as capture,
        patch(
            "packages.api.routers.optimizer.get_device_data_quality",
            new=AsyncMock(
                return_value={
                    "timestamp": None,
                    "age_seconds": None,
                    "threshold_seconds": 900,
                    "ready": False,
                }
            ),
        ),
        patch(
            "packages.core.settings_service.get_setting",
            new=AsyncMock(return_value="rules_only"),
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(
                return_value={
                    "active_layer": "rules_v3",
                    "cop_trained": False,
                    "demand_trained": False,
                }
            ),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value=quality),
        ) as training_data_quality,
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch(
            "packages.api.routers.optimizer.get_session",
            return_value=_session_context(session),
        ),
    ):
        response = await get_optimizer_status()

    training_data_quality.assert_not_awaited()
    capture.assert_awaited_once_with(route="/api/optimizer/status")
    assert response["demand_model"]["data_quality"] == quality
    assert response["demand_model"]["samples"] == 0
    assert response["demand_model"]["source_records"] == 188
    assert response["cop_model"] == {
        "trained": False,
        "last_trained": None,
        "samples": 0,
        "source_records": 0,
        "metrics": None,
        "status": "retired",
        "supported": False,
        "unavailable_reason": "retired",
        "reason": "unverified_thermal_output",
    }
    assert response["cop_trained"] is False
    assert response["fallback_layer"] == "rules_v7"
    assert response["thermal_model"] == {
        "lifecycle": response["model_lifecycle"]["thermal"],
        "status": "unavailable",
        "control_ready": False,
        "calibrated": False,
        "tank_heating_rate": None,
        "confidence": "default",
        "indoor_heating_confidence": "default",
        "indoor_heating_samples": None,
        "calibration_status": {},
        "last_calibrated": None,
        "sample_basis": "artifact_diagnostics",
    }


class TestManualCOPAPI:
    @pytest.fixture
    async def client(self):
        import httpx

        from packages.api.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            yield client

    def observation(self):
        from packages.core.models import ManualCOPObservationRecord

        now = dt.datetime.now(dt.timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return ManualCOPObservationRecord(
            device_id="fixture-device",
            granularity="day",
            period_date=start.date(),
            timezone="UTC",
            period_start=start,
            period_end=start + dt.timedelta(days=1),
            value=9.7,
            observed_at=now,
            completeness="partial",
            created_at=now,
            updated_at=now,
            revision=9007199254740993,
        )

    def body(self, row, revision="0"):
        return {
            "value": row.value,
            "observed_at": row.observed_at.isoformat(),
            "completeness": row.completeness,
            "timezone": row.timezone,
            "expected_revision": revision,
        }

    def result(self, row=None, rows=None, scalar=None):
        return SimpleNamespace(
            scalar=lambda: scalar,
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: rows or []),
        )

    async def test_first_fixture_create_duplicate_and_forecasting_only_flag(self, client):
        from packages.api.routers import feeds

        row = self.observation()
        path = f"/api/cop/manual/fixture-device/day/{row.period_date}"
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    self.result(row=row),
                    self.result(scalar=True),
                    self.result(),
                ]
            )
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            created = await client.put(path, json=self.body(row))
            duplicate = await client.put(path, json=self.body(row))
        assert created.status_code == 201
        assert duplicate.status_code == 409
        assert created.json()["value"] == 9.7
        assert created.json()["revision"] == "9007199254740993"
        assert {
            key: created.json()[key]
            for key in ("source", "acquisition", "scope", "measured", "accuracy", "energy_boundary")
        } == {
            "source": "controller_estimate",
            "acquisition": "manual_entry",
            "scope": "total",
            "measured": False,
            "accuracy": "approximate",
            "energy_boundary": "unknown",
        }
        for enabled, expected in ((False, "disabled"), (True, "insufficient_data")):
            session.execute = AsyncMock(
                side_effect=[
                    self.result(),
                    self.result(rows=["fixture-device"]),
                    self.result(rows=[row]),
                    self.result(row=row),
                    self.result(rows=[]),
                    self.result(),
                ]
            )
            with (
                patch.object(feeds, "get_session", return_value=_session_context(session)),
                patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=enabled)),
                patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
                patch(
                    "packages.ml.models.DirectionAwareCOP.compute_cop_intervals", new=AsyncMock()
                ) as legacy,
            ):
                response = await client.get("/api/cop/manual?device_id=fixture-device")
            assert response.status_code == 200
            assert response.json()["granularity"] == "day"
            assert response.json()["monitoring"]["status"] == expected
            assert response.json()["monitoring"]["forecast"] is None
            legacy.assert_not_awaited()

    async def test_choices_do_not_require_a_device_or_cloud_discovery(self, client):
        from packages.api.routers import feeds

        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(),
                    self.result(rows=["fixture-device", "ledger-only"]),
                ]
            )
        )
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=False)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="Europe/Stockholm")),
        ):
            response = await client.get("/api/cop/manual")
        assert response.status_code == 200
        assert response.json()["devices"] == ["fixture-device", "ledger-only"]
        assert response.json()["default_timezone"] == "Europe/Stockholm"
        assert response.json()["observations"] == []
        assert all(call.args[0].is_select for call in session.execute.call_args_list[1:])
        assert "REPEATABLE READ READ ONLY" in str(session.execute.call_args_list[0].args[0])

    async def test_atomic_tokens_and_device_keys_apply_to_update_and_delete(self, client):
        from packages.api.routers import feeds

        row = self.observation()
        token = "9007199254740993"
        path = f"/api/cop/manual/fixture-device/day/{row.period_date}"
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    self.result(row=row),
                    self.result(scalar=True),
                    self.result(row=9007199254740993),
                    self.result(scalar=True),
                    self.result(),
                ]
            )
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            updated = await client.put(path, json=self.body(row, token))
            deleted = await client.request("DELETE", path, json={"expected_revision": token})
            stale = await client.put(path, json=self.body(row, token))
        assert updated.status_code == 200
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert stale.status_code == 409
        for call in (session.execute.call_args_list[1], session.execute.call_args_list[3]):
            statement = call.args[0]
            assert "revision" in str(statement)
            assert token not in str(statement)
            assert 9007199254740993 in statement.compile().params.values()
            assert "fixture-device" in statement.compile().params.values()

    async def test_unknown_device_and_stale_delete_refusals(self, client):
        from packages.api.routers import feeds

        row = self.observation()
        path = f"/api/cop/manual/fixture-device/day/{row.period_date}"
        for known, status in ((False, 404), (True, 409)):
            session = SimpleNamespace(
                execute=AsyncMock(
                    side_effect=[
                        self.result(scalar=known),
                        self.result(),
                    ]
                )
            )
            with patch.object(feeds, "get_session", return_value=_session_context(session)):
                response = await client.request("DELETE", path, json={"expected_revision": "1"})
            assert response.status_code == status

    async def test_invalid_month_delete_identity_never_opens_storage(self, client):
        from packages.api.routers import feeds

        with patch.object(
            feeds, "get_session", side_effect=AssertionError("Invalid month opened DB")
        ):
            response = await client.request(
                "DELETE",
                "/api/cop/manual/fixture-device/month/2026-10-04",
                json={"expected_revision": "1"},
            )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "timezone",
        [
            "utc",
            "US/eastern",
            "us/Eastern",
            "Europe/stockholm",
            "europe/Stockholm",
            "Etc/utc",
            "Invalid/Timezone",
            "localtime",
            "posixrules",
            "posix/UTC",
            "right/UTC",
        ],
    )
    async def test_invalid_timezone_keys_never_open_storage_with_permissive_lookup(
        self, client, timezone
    ):
        from zoneinfo import ZoneInfo

        from packages.api.routers import feeds
        from packages.core import cop_monitoring

        row = self.observation()
        with (
            patch.object(cop_monitoring, "ZoneInfo", return_value=ZoneInfo("UTC")),
            patch.object(
                feeds, "get_session", side_effect=AssertionError("Invalid timezone opened DB")
            ) as storage,
        ):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}",
                json=self.body(row) | {"timezone": timezone},
            )
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid timezone or calendar period"
        storage.assert_not_called()

    @pytest.mark.parametrize("timezone", ["UTC", "US/Eastern", "Etc/UTC", "Asia/Calcutta"])
    @pytest.mark.parametrize("granularity", ["day", "month"])
    @pytest.mark.parametrize(("revision", "status"), [("0", 201), ("9007199254740993", 200)])
    async def test_supported_exact_timezone_keys_preserve_calendar_and_write_status(
        self, client, timezone, granularity, revision, status
    ):
        from packages.api.routers import feeds
        from packages.core.cop_monitoring import period_bounds

        row = self.observation()
        row.granularity = granularity
        row.period_date = dt.date(2026, 9, 1)
        row.timezone = timezone
        row.period_start, row.period_end = period_bounds(granularity, row.period_date, timezone)
        row.observed_at = row.period_end + dt.timedelta(hours=2)
        row.completeness = "closed"
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    self.result(row=row),
                ]
            )
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            response = await client.put(
                f"/api/cop/manual/fixture-device/{granularity}/{row.period_date}",
                json=self.body(row, revision),
            )
        assert response.status_code == status
        assert response.json()["timezone"] == timezone
        assert response.json()["value"] == 9.7
        assert response.json()["completeness"] == "closed"
        assert session.execute.await_count == 2
        parameters = session.execute.call_args_list[1].args[0].compile().params
        assert parameters["timezone"] == timezone
        assert parameters["period_start"] == row.period_start
        assert parameters["period_end"] == row.period_end

    @pytest.mark.parametrize(("known", "status"), [(False, 404), (True, 409)])
    async def test_valid_timezone_put_preserves_unknown_device_and_stale_revision(
        self, client, known, status
    ):
        from packages.api.routers import feeds

        row = self.observation()
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=known),
                    self.result(),
                ]
            )
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}",
                json=self.body(row, "9007199254740993"),
            )
        assert response.status_code == status
        assert session.execute.await_count == (2 if known else 1)

    async def test_valid_timezone_storage_constraint_failure_remains_503(self, client):
        from sqlalchemy.exc import IntegrityError

        from packages.api.routers import feeds

        row = self.observation()
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    IntegrityError("fixture storage constraint", {}, Exception("fixture")),
                ]
            )
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}", json=self.body(row)
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Manual COP storage unavailable"}
        assert session.execute.await_count == 2

    @pytest.mark.parametrize("value", [True, "9.7", 0, -1])
    async def test_invalid_client_fields_never_open_storage(self, client, value):
        from packages.api.routers import feeds

        row = self.observation()
        with patch.object(
            feeds, "get_session", side_effect=AssertionError("invalid request opened DB")
        ):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}",
                json=self.body(row) | {"value": value},
            )
        assert response.status_code == 422

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e309"])
    async def test_nonfinite_json_numbers_return_422_without_storage(self, client, literal):
        from packages.api.routers import feeds

        row = self.observation()
        content = json.dumps(self.body(row)).replace('"value": 9.7', f'"value": {literal}')
        with patch.object(feeds, "get_session", side_effect=AssertionError("Nonfinite opened DB")):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}",
                content=content,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 422

    async def test_commit_failure_does_not_return_a_revision(self, client):
        from contextlib import asynccontextmanager
        from sqlalchemy.exc import SQLAlchemyError

        from packages.api.routers import feeds

        row = self.observation()
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    self.result(row=row),
                ]
            )
        )

        @asynccontextmanager
        async def failed_commit():
            yield session
            raise SQLAlchemyError("fixture commit failure")

        with patch.object(feeds, "get_session", failed_commit):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}", json=self.body(row)
            )
        assert response.status_code == 503
        assert "revision" not in response.json()

    async def test_fit_starts_only_after_read_snapshot_closes(self, client):
        from contextlib import asynccontextmanager

        from packages.api.routers import feeds
        from packages.core.cop_monitoring import monitoring_result

        closed = False
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(),
                    self.result(rows=["fixture-device"]),
                    self.result(rows=[]),
                    self.result(),
                    self.result(),
                ]
            )
        )

        @asynccontextmanager
        async def read_snapshot():
            nonlocal closed
            try:
                yield session
            finally:
                closed = True

        async def check_closed(observations, issue_at, enabled):
            assert closed
            return monitoring_result(observations, issue_at) | {
                "status": "unavailable",
                "reason": "fixture_fit_failure",
            }

        with (
            patch.object(feeds, "get_session", read_snapshot),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(feeds, "forecast_snapshot", check_closed),
        ):
            response = await client.get("/api/cop/manual?device_id=fixture-device")
        assert response.status_code == 200
        assert response.json()["monitoring"]["forecast"] is None
        assert response.json()["monitoring"]["status"] == "unavailable"

    @pytest.mark.parametrize("fail_on", [0, 2, 4])
    async def test_read_storage_failure_closes_snapshot_without_forecasting(self, client, fail_on):
        from contextlib import asynccontextmanager
        from sqlalchemy.exc import SQLAlchemyError

        from packages.api.routers import feeds

        row = self.observation()
        results = [
            self.result(),
            self.result(rows=["fixture-device"]),
            self.result(rows=[row]),
            self.result(row=row),
            self.result(rows=[]),
            self.result(),
        ]
        results[fail_on] = SQLAlchemyError("Disposable read failure")
        session = SimpleNamespace(execute=AsyncMock(side_effect=results))
        closed = []

        @asynccontextmanager
        async def read_snapshot():
            try:
                yield session
            finally:
                closed.append(True)

        with (
            patch.object(feeds, "get_session", read_snapshot),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(feeds, "forecast_snapshot", new=AsyncMock()) as forecast,
        ):
            response = await client.get("/api/cop/manual?device_id=fixture-device")
        assert response.status_code == 503
        assert response.json() == {"detail": "Manual COP storage unavailable"}
        assert closed == [True]
        assert session.execute.await_count == fail_on + 1
        forecast.assert_not_awaited()

    async def test_cancelled_read_closes_snapshot_without_starting_a_fit(self, client):
        from contextlib import asynccontextmanager

        from packages.api.routers import feeds

        entered = asyncio.Event()
        blocked = asyncio.Event()
        closed = []

        async def execute(statement):
            entered.set()
            await blocked.wait()
            raise AssertionError("Cancelled storage request resumed")

        session = SimpleNamespace(execute=execute)

        @asynccontextmanager
        async def read_snapshot():
            try:
                yield session
            finally:
                closed.append(True)

        with (
            patch.object(feeds, "get_session", read_snapshot),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(feeds, "forecast_snapshot", new=AsyncMock()) as forecast,
        ):
            request = asyncio.create_task(client.get("/api/cop/manual?device_id=fixture-device"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert closed == [True]
                forecast.assert_not_awaited()
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        assert request.done()
        assert not blocked.is_set()

    async def test_existing_daily_revision_unavailable_at_issue_is_not_no_data(self, client):
        from packages.api.routers import feeds

        row = self.observation()
        row.updated_at += dt.timedelta(days=1)
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(),
                    self.result(rows=["fixture-device"]),
                    self.result(rows=[row]),
                    self.result(),
                    self.result(row=row),
                    self.result(rows=[]),
                    self.result(),
                ]
            )
        )
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            response = await client.get(
                "/api/cop/manual?device_id=fixture-device&issue_at=2000-01-01"
            )
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["monitoring"]["status"] == "insufficient_data"
        assert response.json()["monitoring"]["reason"] == "no_current_revision_available"
        assert response.json()["monitoring"]["forecast"] is None
        assert response.json()["issue_at"].startswith(str(dt.datetime.now(dt.timezone.utc).date()))

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    @pytest.mark.parametrize(("token", "status"), [(None, 401), ("wrong-token", 403)])
    async def test_global_auth_refuses_before_storage(self, method, token, status, monkeypatch):
        import httpx

        from packages.api.main import app
        from packages.api.routers import feeds
        from packages.core.config import settings

        monkeypatch.setattr(settings, "api_token", "test-token")
        row = self.observation()
        path = (
            "/api/cop/manual"
            if method == "GET"
            else f"/api/cop/manual/fixture-device/day/{row.period_date}"
        )
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        body = self.body(row) if method == "PUT" else {"expected_revision": "1"}
        with (
            patch.object(
                feeds, "get_session", side_effect=AssertionError("Unauthorized DB access")
            ),
            patch.object(
                feeds,
                "get_bool_setting",
                new=AsyncMock(side_effect=AssertionError("Unauthorized settings access")),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
            ) as unsigned:
                response = await unsigned.request(method, path, json=body)
        assert response.status_code == status

    async def test_disabled_auth_retains_existing_installation_perimeter_contract(
        self, client, monkeypatch
    ):
        from packages.api.routers import feeds
        from packages.core.config import settings

        monkeypatch.setattr(settings, "api_token", "disabled")
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(),
                    self.result(rows=["fixture-device"]),
                ]
            )
        )
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=False)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            response = await client.get(
                "/api/cop/manual", headers={"Authorization": "Bearer unrelated"}
            )
        assert response.status_code == 200

    async def test_structured_mutation_logs_keys_tokens_and_outcome_not_reading(self, client):
        from packages.api.routers import feeds

        row = self.observation()
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    self.result(scalar=True),
                    self.result(row=row),
                ]
            )
        )
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds.logger, "info") as log,
        ):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}", json=self.body(row)
            )
        assert response.status_code == 201
        assert log.call_args.args == ("manual_cop_mutation",)
        assert set(log.call_args.kwargs) == {
            "device_id",
            "granularity",
            "period_date",
            "expected_revision",
            "revision",
            "outcome",
        }
        assert log.call_args.kwargs["expected_revision"] == "0"
        assert log.call_args.kwargs["revision"] == "9007199254740993"
        assert log.call_args.kwargs["outcome"] == "created"


class TestCOPRetirementAPI:
    @pytest.mark.parametrize(
        "maintenance_enabled",
        [False, True, None],
    )
    async def test_no_selector_training_only_enqueues_demand(self, maintenance_enabled, tmp_path):
        import httpx

        from packages.api.main import app
        from packages.api.routers.models_router import trigger_ml_training
        from packages.ml.lifecycle import LifecycleUnavailable

        import inspect

        assert not inspect.signature(trigger_ml_training).parameters
        operation = app.openapi()["paths"]["/api/ml/train"]["post"]
        assert "requestBody" not in operation
        assert not operation.get("parameters")
        artifact = tmp_path / "cop_model_weather_dhw_v6_archived.pkl"
        artifact.write_bytes(b"archived COP bytes")
        demand = MagicMock()
        demand.train = AsyncMock(side_effect=AssertionError("Demand fit"))
        enqueue = AsyncMock()
        demand_result = {
            "request_id": "fixture-request",
            "status": "queued",
            "maintenance_enabled": maintenance_enabled,
        }
        if maintenance_enabled is None:
            enqueue.side_effect = LifecycleUnavailable("fixture storage failure")
        else:
            enqueue.return_value = demand_result
        session = MagicMock()
        with (
            patch("packages.ml.models.DemandModel", side_effect=AssertionError("Demand instance")),
            patch("packages.ml.models.COPModel", side_effect=AssertionError("COP instance")),
            patch("packages.ml.safe_persistence.safe_load", side_effect=AssertionError("COP load")),
            patch("packages.api.routers.models_router.enqueue_maintenance_request", enqueue),
            patch(
                "packages.api.routers.models_router.get_session",
                return_value=_session_context(session),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                response = await client.post("/api/ml/train")

        enqueue.assert_awaited_once_with("demand", "api")
        demand.train.assert_not_awaited()
        assert artifact.read_bytes() == b"archived COP bytes"
        if maintenance_enabled is None:
            assert response.status_code == 503
            assert response.json()["detail"] == {"code": "ml_maintenance_unavailable"}
            session.add.assert_not_called()
            return
        assert response.status_code == 202
        body = response.json()
        assert body["cop"] == {
            "status": "retired",
            "supported": False,
            "error": "cop_model_retired",
            "reason": "unverified_thermal_output",
        }
        assert body["demand"] == {
            **demand_result,
            "control_ready": False,
            "control_readiness": {"ready": False, "reason": "observe_only_build"},
        }
        session.add.assert_called_once()

    @pytest.mark.parametrize(("hours", "expected_status"), [(24, 410), (168, 410), (0, 422)])
    async def test_compute_is_gone_without_sessions_or_computation(self, hours, expected_status):
        import httpx

        from packages.api.main import app

        with (
            patch(
                "packages.api.routers.feeds.get_session", side_effect=AssertionError("COP session")
            ),
            patch(
                "packages.ml.models.DirectionAwareCOP.compute_cop_intervals",
                side_effect=AssertionError("COP compute"),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                response = await client.post(f"/api/cop/compute?hours={hours}")

        assert response.status_code == expected_status
        if expected_status == 410:
            assert response.json()["detail"]["code"] == "cop_retired"

    @pytest.mark.parametrize("populated", [False, True])
    async def test_history_and_stats_only_read_labelled_archives(self, populated):
        import httpx

        from packages.api.main import app

        row = SimpleNamespace(
            ts=dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc),
            cop_value=3.5,
            mode="HEATING_WATER",
            outdoor_temp=5.0,
            electrical_kwh=1.0,
            thermal_kwh=3.5,
        )
        records = [row] if populated else []
        archive = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: records))
        aggregates = SimpleNamespace(
            one=lambda: (3.5, 1, 3.5, 3.5) if populated else (None, 0, None, None)
        )
        session = SimpleNamespace(execute=AsyncMock(side_effect=[archive, aggregates]))
        with (
            patch("packages.api.routers.feeds.get_session", return_value=_session_context(session)),
            patch("packages.ml.models.get_session", return_value=_session_context(session)),
            patch(
                "packages.ml.models.DirectionAwareCOP.compute_cop_intervals",
                side_effect=AssertionError("COP compute"),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                history = await client.get("/api/cop/history")
                stats = await client.get("/api/cop/stats")

        assert history.status_code == stats.status_code == 200
        assert history.headers["X-COP-Status"] == stats.headers["X-COP-Status"] == "retired"
        expected_history = (
            []
            if not populated
            else [
                {
                    "ts": row.ts.isoformat(),
                    "cop_value": 3.5,
                    "mode": "HEATING_WATER",
                    "outdoor_temp": 5.0,
                    "electrical_kwh": 1.0,
                    "thermal_kwh": 3.5,
                    "provenance": "legacy_unverified_estimate",
                    "confidence": "estimated",
                    "measured": False,
                }
            ]
        )
        assert history.json() == expected_history
        assert stats.json() == {
            "avg_cop": 3.5 if populated else None,
            "sample_count": 1 if populated else 0,
            "min_cop": 3.5 if populated else None,
            "max_cop": 3.5 if populated else None,
            "period_hours": 168,
            "mode_filter": None,
            "status": "retired",
            "provenance": "legacy_unverified_estimate",
            "confidence": "estimated",
            "measured": False,
        }
        assert session.execute.await_count == 2
        assert all(call.args[0].is_select for call in session.execute.call_args_list)

    @pytest.mark.parametrize("control_allowed", [False, True])
    async def test_readiness_ignores_cop_and_preserves_planning_input_gate(self, control_allowed):
        import httpx

        from packages.api.main import app

        now = dt.datetime.now(dt.timezone.utc)
        heartbeats = [
            SimpleNamespace(service=service, updated_at=now)
            for service in ("poller", "optimizer", "backup")
        ]
        rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: heartbeats))
        session = SimpleNamespace(execute=AsyncMock(return_value=rows))
        device_quality = {
            "ready": True,
            "credentials_configured": True,
            "timestamp": now,
            "age_seconds": 0,
            "threshold_seconds": 300,
            "reasons": [],
        }
        planning_quality = {
            "control_allowed": control_allowed,
            "reasons": [] if control_allowed else ["fixture_stale_price"],
        }
        with (
            patch(
                "packages.api.routers.models_router.get_session",
                return_value=_session_context(session),
            ),
            patch(
                "packages.core.device_data_quality.get_device_data_quality",
                new=AsyncMock(return_value=device_quality),
            ),
            patch(
                "packages.core.planning_data_quality.get_planning_data_quality",
                new=AsyncMock(return_value=planning_quality),
            ),
            patch(
                "packages.ml.model_status._inspect_model_artifact",
                side_effect=AssertionError("COP artifact inspection"),
            ),
            patch("packages.ml.models.COPModel", side_effect=AssertionError("COP constructor")),
            patch("packages.ml.safe_persistence.safe_load", side_effect=AssertionError("COP load")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
            ) as client:
                response = await client.get("/health/ready")

        assert response.status_code == (200 if control_allowed else 503)
        body = response.json() if control_allowed else response.json()["detail"]
        assert body["status"] == ("ready" if control_allowed else "degraded")
        assert body["planning_data_quality"] == planning_quality
        if not control_allowed:
            assert body["stale"] == ["planning_inputs"]
        session.execute.assert_awaited_once()
        assert session.execute.await_args.args[0].is_select


def _write_signed_artifact(path, payload) -> None:
    with patch(
        "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
    ):
        safe_dump(payload, path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("optimizer_status_data_quality")
async def test_optimizer_status_returns_consumption_evidence_and_artifact_metrics(tmp_path) -> None:
    from packages.ml import model_status

    count_result = MagicMock()
    count_result.scalar.return_value = 42
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = _thermal_model()
    artifact_status = {
        "trained": True,
        "last_trained": "2026-10-01T12:00:00+00:00",
        "samples": 36,
        "metrics": {"mae": 0.123, "samples": 36},
        "unavailable_reason": None,
    }
    captured = model_status._unavailable_readiness(
        "fixture", as_of=dt.datetime.now(dt.timezone.utc), generation="artifact-metrics-fixture"
    )
    captured["context"] = {"settings": {"optimizer_layer": "rules_only"}}
    captured["model_lifecycle"]["demand"].update(
        {**artifact_status, "status": "unqualified", "source_records": 42}
    )
    captured["demand_quality"] = {"usable_samples": 2}

    with (
        patch.object(
            model_status, "get_readiness_snapshot", new=AsyncMock(return_value=captured)
        ) as capture,
        patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(return_value="rules_only")
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(return_value={"active_layer": "rules_v3"}),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch(
            "packages.ml.model_status.get_model_status",
            new=AsyncMock(
                side_effect=lambda kind, directory: (
                    dict(_RETIRED_COP_STATUS) if kind == "cop" else dict(artifact_status)
                )
            ),
        ),
        patch(
            "packages.ml.model_status.get_model_reporting_statuses",
            new=AsyncMock(
                return_value={
                    component: {
                        **(artifact_status if component == "demand" else {"trained": False}),
                        "status": "unqualified",
                        "control_ready": False,
                        "ordinary_writes_allowed": False,
                    }
                    for component in ("demand", "comfort", "thermal")
                }
            ),
        ),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value={"usable_samples": 2}),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
    ):
        response = await get_optimizer_status()

    assert response["cop_model"] == _RETIRED_COP_STATUS
    assert response["demand_model"]["source_records"] == 42
    assert response["demand_model"]["trained"] is True
    assert response["demand_model"]["last_trained"] == "2026-10-01T12:00:00+00:00"
    assert response["demand_model"]["metrics"] == {"mae": 0.123, "samples": 36}
    assert response["demand_model"]["control_ready"] is False
    assert response["demand_model"]["ordinary_writes_allowed"] is False
    capture.assert_awaited_once_with(route="/api/optimizer/status")
    assert response["demand_model"]["data_quality"]["usable_samples"] == 2
    assert response["demand_model"]["samples"] == 36
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("optimizer_status_data_quality")
async def test_optimizer_status_reports_indoor_heating_evidence_for_calibrated_model(
    tmp_path,
) -> None:
    from packages.ml import model_status

    count_result = MagicMock()
    count_result.scalar.return_value = 0
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    calibrated_at = dt.datetime(2026, 10, 8, 16, 48, tzinfo=dt.timezone.utc)
    thermal_model = _thermal_model(
        last_calibrated=calibrated_at,
        tank_heating_rate=22.334,
        indoor_heating_samples=3,
        calibration_status={"tank_heating": "validated", "zone_cooling": "insufficient_samples"},
    )
    captured = model_status._unavailable_readiness(
        "fixture", as_of=calibrated_at, generation="thermal-artifact-fixture"
    )
    captured["context"] = {"settings": {"optimizer_layer": "rules_only"}}
    captured["thermal_model"].update(
        {
            "calibrated": True,
            "tank_heating_rate": 22.33,
            "indoor_heating_samples": 3,
            "last_calibrated": calibrated_at.isoformat(),
            "calibration_status": dict(thermal_model.params.calibration_status),
        }
    )

    with (
        patch.object(
            model_status, "get_readiness_snapshot", new=AsyncMock(return_value=captured)
        ) as capture,
        patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(return_value="rules_only")
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(return_value={"active_layer": "rules_v7"}),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value={"usable_samples": 0}),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
    ):
        response = await get_optimizer_status()

    assert response["thermal_model"] == {
        "lifecycle": response["model_lifecycle"]["thermal"],
        "status": "unavailable",
        "control_ready": False,
        "calibrated": True,
        "tank_heating_rate": 22.33,
        "confidence": "default",
        "indoor_heating_confidence": "default",
        "indoor_heating_samples": 3,
        "calibration_status": {"tank_heating": "validated", "zone_cooling": "insufficient_samples"},
        "last_calibrated": calibrated_at.isoformat(),
        "sample_basis": "artifact_diagnostics",
    }
    assert (
        response["seasonal_calibration"]["readiness"]["components"]["indoor_heating"]["collection"][
            "total"
        ]
        is None
    )
    capture.assert_awaited_once_with(route="/api/optimizer/status")
    thermal_model.arefresh_if_changed.assert_not_awaited()


@pytest.mark.parametrize(
    ("model_kind", "filename", "payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v6_20261001_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"mae": 0.2, "samples": 17}},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"mae": 0.3, "samples": 23}},
        ),
    ],
)
def test_model_status_never_adopts_uncommitted_signed_directory_artifact(
    model_kind, filename, payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, payload)
    archive_bytes = (tmp_path / filename).read_bytes()
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    if model_kind == "cop":
        assert status == _RETIRED_COP_STATUS
    else:
        assert status["trained"] is False
        assert status["metrics"] is None
        assert status["committed_hash"] is None
        assert status["unavailable_reason"] == "lifecycle_unavailable"
        assert status["control_ready"] is False
    assert (tmp_path / filename).read_bytes() == archive_bytes


@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v6_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v3_20261001_1200.pkl"),
    ],
)
def test_model_status_reports_signature_failure_without_details(
    model_kind, filename, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    (tmp_path / filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    with patch("packages.ml.models.MODEL_DIR", tmp_path):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == (
        "retired" if model_kind == "cop" else "lifecycle_unavailable"
    )
    assert str(tmp_path) not in str(status)


@pytest.mark.asyncio
@pytest.mark.usefixtures("optimizer_status_data_quality")
@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v6_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v3_20261001_1200.pkl"),
    ],
)
async def test_optimizer_status_contains_safe_failure_for_bad_artifact(
    model_kind, filename, tmp_path
) -> None:
    from packages.ml import model_status as reporting_status

    (tmp_path / filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    count_result = MagicMock()
    count_result.scalar.return_value = 42
    session = SimpleNamespace(execute=AsyncMock(return_value=count_result))
    thermal_model = _thermal_model()
    now = dt.datetime.now(dt.timezone.utc)
    captured = reporting_status._unavailable_readiness(
        "lifecycle_unavailable", as_of=now, generation="bad-artifact-fixture"
    )
    captured["context"] = {"settings": {"optimizer_layer": "rules_only"}}
    captured["model_lifecycle"] = await reporting_status.get_model_reporting_statuses(
        tmp_path, snapshot=None, evidence_by_component={}, as_of=now, context=captured["context"]
    )

    with (
        patch.object(
            reporting_status, "get_readiness_snapshot", new=AsyncMock(return_value=captured)
        ) as capture,
        patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(return_value="rules_only")
        ),
        patch(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            new=AsyncMock(return_value={"active_layer": "rules_v3"}),
        ),
        patch(
            "packages.api.routers.optimizer._learning_mode_status",
            new=AsyncMock(return_value={"enabled": False}),
        ),
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.models.DemandModel.training_data_quality",
            new=AsyncMock(return_value={"usable_samples": 0}),
        ),
        patch("packages.ml.thermal.thermal_model", thermal_model),
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
    ):
        response = await get_optimizer_status()

    model_status = response[f"{model_kind}_model"]
    assert model_status["trained"] is False
    assert model_status["unavailable_reason"] == (
        "retired" if model_kind == "cop" else "lifecycle_unavailable"
    )
    assert response["cop_model"] == _RETIRED_COP_STATUS
    assert str(tmp_path) not in json.dumps(response)
    assert (tmp_path / filename).read_bytes() == b"not a signed artifact"
    capture.assert_awaited_once_with(route="/api/optimizer/status")


@pytest.mark.parametrize(
    ("model_kind", "filename"),
    [
        ("cop", "cop_model_weather_dhw_v3_20261001_1200.pkl"),
        ("cop", "cop_model_weather_dhw_v4_20261001_1200.pkl"),
        ("demand", "demand_model_weather_v2_20261001_1200.pkl"),
    ],
)
def test_model_status_ignores_pre_current_version_artifacts(model_kind, filename, tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, {"model": SimpleNamespace(n_features_in_=7)})
    clear_artifact_status_cache()
    status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == (
        "retired" if model_kind == "cop" else "lifecycle_unavailable"
    )


@pytest.mark.parametrize(
    ("model_kind", "older_filename", "newer_filename", "older_payload", "newer_payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v6_20261001_1200.pkl",
            "cop_model_weather_dhw_v6_20261002_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"samples": 17}},
            {"model": SimpleNamespace(n_features_in_=1)},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            "demand_model_weather_v3_20261002_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"samples": 23}},
            {"median": SimpleNamespace(n_features_in_=1)},
        ),
    ],
)
def test_model_status_refuses_older_directory_fallback_when_newer_is_incompatible(
    model_kind, older_filename, newer_filename, older_payload, newer_payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / older_filename, older_payload)
    _write_signed_artifact(tmp_path / newer_filename, newer_payload)
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    if model_kind == "cop":
        assert status == _RETIRED_COP_STATUS
    else:
        assert status["trained"] is False
        assert status["metrics"] is None
        assert status["committed_hash"] is None
        assert status["unavailable_reason"] == "lifecycle_unavailable"
    assert (tmp_path / older_filename).exists() and (tmp_path / newer_filename).exists()


@pytest.mark.parametrize(
    ("model_kind", "older_filename", "newer_filename", "older_payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v6_20261001_1200.pkl",
            "cop_model_weather_dhw_v6_20261002_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=7), "metrics": {"samples": 17}},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            "demand_model_weather_v3_20261002_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=10), "metrics": {"samples": 23}},
        ),
    ],
)
def test_model_status_refuses_older_directory_fallback_when_newer_fails_integrity(
    model_kind, older_filename, newer_filename, older_payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / older_filename, older_payload)
    (tmp_path / newer_filename).write_bytes(b"not a signed artifact")
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    if model_kind == "cop":
        assert status == _RETIRED_COP_STATUS
    else:
        assert status["trained"] is False
        assert status["last_trained"] is None
        assert status["metrics"] is None
        assert status["unavailable_reason"] == "lifecycle_unavailable"
    assert (tmp_path / older_filename).exists() and (tmp_path / newer_filename).exists()


@pytest.mark.parametrize(
    ("model_kind", "filename", "payload"),
    [
        (
            "cop",
            "cop_model_weather_dhw_v6_20261001_1200.pkl",
            {"model": SimpleNamespace(n_features_in_=1)},
        ),
        (
            "demand",
            "demand_model_weather_v3_20261001_1200.pkl",
            {"median": SimpleNamespace(n_features_in_=1)},
        ),
    ],
)
def test_model_status_reports_only_incompatible_artifact(
    model_kind, filename, payload, tmp_path
) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    _write_signed_artifact(tmp_path / filename, payload)
    clear_artifact_status_cache()
    with (
        patch("packages.ml.models.MODEL_DIR", tmp_path),
        patch(
            "packages.ml.safe_persistence._validate_path", side_effect=lambda candidate: candidate
        ),
    ):
        status = _inspect_model_artifact(model_kind, tmp_path)

    assert status["trained"] is False
    assert status["unavailable_reason"] == (
        "retired" if model_kind == "cop" else "lifecycle_unavailable"
    )


def test_model_status_cache_never_grants_permission_without_committed_state(tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    path = tmp_path / "demand_model_weather_v3_20261001_1200.pkl"
    path.write_bytes(b"first")
    clear_artifact_status_cache()
    with patch("packages.ml.models.DemandModel", side_effect=AssertionError("Legacy loader")):
        first = _inspect_model_artifact("demand", tmp_path)
        second = _inspect_model_artifact("demand", tmp_path)
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
        third = _inspect_model_artifact("demand", tmp_path)

    assert first == second == third
    assert first["metrics"] is None
    assert first["trained"] is False
    assert first["control_ready"] is False
    assert first["status"] == "unavailable"


def test_model_status_never_adopts_new_uncommitted_directory_files(tmp_path) -> None:
    from packages.ml.model_status import _inspect_model_artifact

    first_path = tmp_path / "demand_model_weather_v3_20261001_1200.pkl"
    first_path.write_bytes(b"first")
    clear_artifact_status_cache()
    with patch("packages.ml.models.DemandModel", side_effect=AssertionError("Legacy loader")):
        first = _inspect_model_artifact("demand", tmp_path)
        (tmp_path / "demand_model_weather_v3_20261002_1200.pkl").write_bytes(b"second")
        second = _inspect_model_artifact("demand", tmp_path)

    assert first == second
    assert first["trained"] is False
    assert first["committed_hash"] is None
    assert first["ordinary_writes_allowed"] is False


@pytest.fixture
def lifecycle_status_api(tmp_path, monkeypatch, optimizer_status_data_quality):
    from packages.core.config import settings
    from packages.ml.lifecycle import ArtifactReference, LifecycleSnapshot
    from packages.ml.safe_persistence import publish_signed_artifact
    from packages.ml import model_status
    from packages.core.settings_service import SETTING_SPECS

    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(settings, "model_dir", str(tmp_path))
    monkeypatch.setattr(settings, "secret_key", "test-only-status-signing-key")
    monkeypatch.setattr("packages.ml.safe_persistence._fsync_directory", lambda directory: None)
    monkeypatch.setattr("packages.ml.models.MODEL_DIR", tmp_path)
    manifest = {
        "fit_id": "api-fit-1",
        "data_cutoff": (now - dt.timedelta(days=15)).isoformat(),
        "frozen_at": (now - dt.timedelta(days=15)).isoformat(),
        "device_id": "device-a",
        "horizons": [60],
        "capabilities": ["space_heating_demand"],
        "scope": {"policy": "native_unchanged_v1"},
        "input_schema": "native_inputs_v1",
        "output_schema": "demand_v1",
        "runtime_versions": {"python": "3.11"},
        "evidence_hashes": [],
        "metrics": {"mae": 0.3, "samples": 23},
    }
    artifact = publish_signed_artifact(
        {"weights": [2.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=manifest,
    )
    expiry = now + dt.timedelta(seconds=30)
    reference = ArtifactReference(
        artifact.artifact_hash,
        "epoch-1",
        "demand",
        "demand_lifecycle_v1",
        manifest,
        "incumbent",
        7,
        now - dt.timedelta(hours=1),
        expiry,
        expiry - dt.timedelta(hours=72),
        {"policy": "native_unchanged_v1"},
    )
    state = LifecycleSnapshot("epoch-1", 7, 5, False, None, None, (reference,))
    reader = SimpleNamespace(
        read=AsyncMock(return_value=state),
        reporting_evidence=AsyncMock(
            return_value={
                "counts": {
                    "pairs": 48,
                    "device_day_clusters": 12,
                    "issue_cohorts": 18,
                    "horizons": {"60": 48},
                    "outdoor_bins": {"0": 24, "2": 24},
                },
                "exclusions": {"unknown_execution": 3},
                "pending_outcomes": 2,
                "last_valid_outcome_at": (now - dt.timedelta(hours=1)).isoformat(),
                "as_of": now.isoformat(),
            }
        ),
    )
    monkeypatch.setattr("packages.ml.model_status.Lifecycle", lambda: reader)
    monkeypatch.setattr(
        "packages.core.settings_service.get_setting", AsyncMock(return_value="rules_only")
    )
    monkeypatch.setattr(
        "packages.optimizer.main.get_optimizer_status_snapshot",
        AsyncMock(return_value={"active_layer": "rules_v7"}),
    )
    monkeypatch.setattr(
        "packages.api.routers.optimizer._learning_mode_status",
        AsyncMock(return_value={"enabled": False}),
    )
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_progress",
        AsyncMock(
            return_value={
                "enabled": True,
                "observe_only_active": True,
                "auto_exit": False,
                "next_step": "await_prospective_reporting_qualification",
            }
        ),
    )
    monkeypatch.setattr(
        "packages.ml.models.DemandModel.training_data_quality",
        AsyncMock(return_value={"usable_samples": 0}),
    )
    monkeypatch.setattr("packages.ml.thermal.thermal_model", _thermal_model())
    rows = MagicMock()
    rows.scalar.return_value = 42
    session = SimpleNamespace(execute=AsyncMock(return_value=rows))
    monkeypatch.setattr(
        "packages.api.routers.optimizer.get_session", lambda: _session_context(session)
    )

    async def capture(**_kwargs):
        as_of = model_status._now()
        result = model_status._unavailable_readiness(
            "fixture", as_of=as_of, generation="lifecycle-status-fixture"
        )
        values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
        result["context"] = {"settings": values, "as_of": as_of}
        try:
            captured_state = await reader.read()
            selected = captured_state.reference("demand", "candidate") or captured_state.reference(
                "demand"
            )
            evidence = await reader.reporting_evidence(selected, now=as_of) if selected else None
            result["model_lifecycle"] = await model_status.get_model_reporting_statuses(
                tmp_path,
                snapshot=captured_state,
                evidence_by_component={"demand": evidence},
                as_of=as_of,
                context=result["context"],
            )
        except Exception:
            pass
        result["device_quality"] = await optimizer_status_data_quality(now=as_of)
        return result

    monkeypatch.setattr(model_status, "get_readiness_snapshot", AsyncMock(side_effect=capture))
    clear_artifact_status_cache()
    return SimpleNamespace(state=state, reader=reader, artifact=artifact, now=now)


@pytest.mark.parametrize(
    "reporting_state",
    ["unqualified", "candidate", "qualified-for-reporting", "expired", "unavailable"],
)
async def test_five_reporting_states_never_offer_control(
    lifecycle_status_api, monkeypatch, reporting_state
):
    from dataclasses import replace

    fixture = lifecycle_status_api
    now = fixture.now
    if reporting_state == "unqualified":
        fixture.reader.read.return_value = replace(fixture.state, references=())
    elif reporting_state == "candidate":
        candidate = replace(
            fixture.state.references[0],
            role="candidate",
            qualified_at=None,
            qualification_expires_at=None,
            last_valid_outcome_at=None,
            qualification_scope={},
        )
        fixture.reader.read.return_value = replace(fixture.state, references=(candidate,))
    elif reporting_state == "expired":
        now += dt.timedelta(seconds=30)
    elif reporting_state == "unavailable":
        fixture.reader.read.side_effect = OSError("private unavailable dependency")
    monkeypatch.setattr("packages.ml.model_status._now", lambda: now)
    response = await get_optimizer_status()
    status = response["model_lifecycle"]["demand"]
    assert response["demand_model"]["status"] == reporting_state
    assert {
        "configured_layer",
        "active_layer",
        "fallback_layer",
        "learning_mode",
        "cop_model",
        "demand_model",
        "thermal_model",
    } <= response.keys()
    assert response["control_ready"] is response["ordinary_writes_allowed"] is False
    assert response["optimization_requires_new_build"] is True
    assert response["seasonal_calibration"]["observe_only_active"] is True
    assert response["seasonal_calibration"]["auto_exit"] is False
    assert all(
        component["control_ready"] is component["ordinary_writes_allowed"] is False
        for component in response["model_lifecycle"].values()
    )
    assert status["status"] == reporting_state
    assert status["control_ready"] is status["ordinary_writes_allowed"] is False
    assert status["control_readiness"] == {"ready": False, "reason": "observe_only_build"}
    assert {
        "missing_counts",
        "missing_diversity",
        "exclusion_counts",
        "next_cadence_at",
        "last_valid_outcome_at",
        "next_decision_checkpoint",
        "candidate_progress",
    } <= status.keys()
    if reporting_state in {"candidate", "qualified-for-reporting", "expired"}:
        assert status["missing_counts"]["paired_outcomes"] == 12
        assert status["missing_diversity"]["populated_outdoor_bins"] == 1
        assert status["exclusion_counts"] == {"unknown_execution": 3}
        assert status["next_decision_checkpoint"]["index"] == 0
        assert status["next_decision_checkpoint"]["due"] is True
        assert status["next_cadence_at"] is None
        assert status["cadence_reason"] == "maintenance_disabled"
    if reporting_state == "candidate":
        assert status["incumbent_status"] == "unqualified"
        assert status["candidate_progress"]["status"] == "candidate"
        assert status["candidate_progress"]["control_ready"] is False
    encoded = json.dumps(status).lower()
    assert "resume automatic control" not in encoded
    assert "approved for control" not in encoded
    assert "private unavailable dependency" not in encoded


async def test_reporting_evidence_is_a_single_read_only_scoped_snapshot(lifecycle_status_api):
    from packages.ml.lifecycle import Lifecycle

    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        mappings=lambda: SimpleNamespace(
            one=lambda: {
                "pairs": 48,
                "device_day_clusters": 12,
                "issue_cohorts": 18,
                "horizons": {"60": 48},
                "outdoor_bins": {"0": 24, "2": 24},
                "exclusions": {"unknown_execution": 3},
                "pending_outcomes": 2,
                "last_valid_outcome_at": lifecycle_status_api.now,
            }
        )
    )
    lifecycle = Lifecycle()
    lifecycle._sessions = lambda: _session_context(session)
    reference = lifecycle_status_api.state.references[0]
    result = await lifecycle.reporting_evidence(reference, now=lifecycle_status_api.now)
    assert result["counts"]["pairs"] == 48
    assert result["exclusions"] == {"unknown_execution": 3}
    assert result["pending_outcomes"] == 2
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    assert statement.is_select is True
    sql = str(statement)
    assert "UPDATE " not in sql and "INSERT " not in sql and "DELETE " not in sql
    assert "LIMIT " not in sql
    assert {"epoch-1", "device-a", "demand", reference.artifact_hash} <= set(
        value for value in statement.compile().params.values() if isinstance(value, str)
    )


async def test_qualified_incumbent_and_candidate_progress_are_separate(
    lifecycle_status_api, monkeypatch
):
    from dataclasses import replace
    from packages.ml.lifecycle import ArtifactReference
    from packages.ml.safe_persistence import publish_signed_artifact

    fixture = lifecycle_status_api
    manifest = {**fixture.state.references[0].manifest, "fit_id": "api-fit-2"}
    artifact = publish_signed_artifact(
        {"weights": [3.0]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest=manifest,
    )
    candidate = ArtifactReference(
        artifact.artifact_hash, "epoch-1", "demand", "demand_lifecycle_v1", manifest, "candidate", 8
    )
    fixture.reader.read.return_value = replace(
        fixture.state,
        references=(*fixture.state.references, candidate),
        maintenance_enabled=True,
        next_maintenance_at=fixture.now + dt.timedelta(hours=6),
        last_demand_attempt_at=fixture.now - dt.timedelta(hours=1),
        checkpoints={
            f"decision:{candidate.artifact_hash}:0": {
                "status": "evaluated",
                "result": {"reason": "insufficient_evidence"},
            }
        },
    )
    monkeypatch.setattr("packages.ml.model_status._now", lambda: fixture.now)
    response = await get_optimizer_status()
    status = response["demand_model"]
    assert status["status"] == status["incumbent_status"] == "qualified-for-reporting"
    assert status["candidate_progress"]["status"] == "candidate"
    assert status["candidate_progress"]["missing_counts"]["paired_outcomes"] == 12
    assert status["candidate_progress"]["last_valid_outcome_at"] != status["last_valid_outcome_at"]
    assert status["next_cadence_at"] == (fixture.now + dt.timedelta(hours=23)).isoformat()
    assert status["next_decision_checkpoint"]["index"] == 1
    assert status["next_decision_checkpoint"]["due"] is False
    assert status["last_decision"]["reason"] == "insufficient_evidence"
    assert response["seasonal_calibration"]["observe_only_active"] is True
    assert response["seasonal_calibration"]["auto_exit"] is False
    assert response["ordinary_writes_allowed"] is response["control_ready"] is False


@pytest.mark.parametrize("reason", ["outcomes_expired", "incompatible_sensor_policy_scope"])
async def test_invalidated_incumbent_retains_expired_reporting_history(
    lifecycle_status_api, monkeypatch, reason
):
    from dataclasses import replace
    from packages.ml.lifecycle import Lease, Lifecycle
    from packages.ml.model_status import _inspect_model_artifact

    fixture = lifecycle_status_api
    now = fixture.now + dt.timedelta(hours=1)
    monkeypatch.setattr("packages.ml.lifecycle._now", lambda: now)
    monkeypatch.setattr("packages.ml.model_status._now", lambda: now)
    session = AsyncMock()
    session.execute.return_value.scalar_one = MagicMock(return_value=8)
    session.execute.return_value.rowcount = 1
    lifecycle = Lifecycle()
    lifecycle._sessions = lambda: _session_context(session)
    lifecycle._lock = AsyncMock()
    lifecycle._read = AsyncMock(return_value=fixture.state)
    lease = Lease("epoch-1", "fixture-owner", 5, 7, now + dt.timedelta(hours=1))
    reference = fixture.state.references[0]
    with patch("packages.ml.safe_persistence._require_publication_owner") as require_owner:
        await lifecycle.invalidate_component(lease, reference, reason)
    require_owner.assert_called_once_with()
    lifecycle._lock.assert_awaited_once_with(session, lease)
    updates = session.execute.await_args_list[1].args[0].compile().params
    assert updates["qualified_at"] == reference.qualified_at
    assert updates["qualification_expires_at"] <= now
    assert updates["last_valid_outcome_at"] == reference.last_valid_outcome_at
    expired = replace(reference, qualification_expires_at=updates["qualification_expires_at"])
    status = _inspect_model_artifact(
        "demand", fixture.artifact.path.parent, replace(fixture.state, references=(expired,))
    )
    assert status["status"] == "expired"
    assert status["control_ready"] is status["ordinary_writes_allowed"] is False


@pytest.mark.parametrize("component", ["comfort", "thermal"])
@pytest.mark.parametrize(
    "reporting_state",
    ["unqualified", "candidate", "qualified-for-reporting", "expired", "unavailable"],
)
async def test_model_status_routes_expose_reporting_only_lifecycle(
    monkeypatch, component, reporting_state
):
    from packages.api.routers import models_router
    from packages.ml.model_status import _empty_status

    report = {**_empty_status("fixture"), "status": reporting_state}
    read_report = AsyncMock(return_value=report)
    monkeypatch.setattr("packages.ml.model_status.get_model_status", read_report)
    monkeypatch.setattr("packages.ml.comfort_model.comfort_model.arefresh_if_changed", AsyncMock())
    monkeypatch.setattr("packages.ml.thermal.thermal_model.load_latest", MagicMock())
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    )
    monkeypatch.setattr(models_router, "get_session", lambda: _session_context(session))
    result = await (
        models_router.get_comfort_model_status()
        if component == "comfort"
        else models_router.get_thermal_status()
    )
    assert result["lifecycle"] == report
    assert result["reporting_state"] == reporting_state
    assert result["control_ready"] is result["ordinary_writes_allowed"] is False
    assert result["control_readiness"] == {"ready": False, "reason": "observe_only_build"}
    assert read_report.await_args is not None
    assert read_report.await_args.args[0] == component


@pytest.mark.parametrize("invalid_counts", [None, [], {"pairs": "unknown"}])
async def test_invalid_reporting_counts_fail_unavailable(lifecycle_status_api, invalid_counts):
    from packages.ml.model_status import get_model_status

    lifecycle_status_api.reader.reporting_evidence.return_value = {"counts": invalid_counts}
    status = await get_model_status("demand", lifecycle_status_api.artifact.path.parent)
    assert status["status"] == "unavailable"
    assert status["counts"] is status["missing_counts"] is None
    assert status["control_ready"] is status["ordinary_writes_allowed"] is False


async def test_qualification_success_reports_readiness_without_exiting_observation(
    lifecycle_status_api, monkeypatch
):
    from packages.ml.model_status import _empty_status

    reports = {
        component: {**_empty_status("fixture"), "status": "qualified-for-reporting"}
        for component in ("demand", "comfort", "thermal")
    }
    monkeypatch.setattr(
        "packages.ml.model_status.get_model_reporting_statuses", AsyncMock(return_value=reports)
    )
    learning = {"enabled": True, "effective_active": True, "sources": ["manual", "seasonal"]}
    monkeypatch.setattr(
        "packages.api.routers.optimizer._learning_mode_status", AsyncMock(return_value=learning)
    )
    with (
        patch("packages.core.settings_service.set_setting", new=AsyncMock()) as single_write,
        patch("packages.core.settings_service.set_settings_bulk", new=AsyncMock()) as bulk_write,
    ):
        response = await get_optimizer_status()
    assert response["seasonal_calibration"]["reporting_ready"] is True
    assert response["seasonal_calibration"]["next_step"] == "ready_for_reporting"
    assert response["seasonal_calibration"]["enabled"] is True
    assert response["seasonal_calibration"]["observe_only_active"] is True
    assert response["seasonal_calibration"]["auto_exit"] is False
    assert response["learning_mode"] == learning
    assert response["control_ready"] is response["ordinary_writes_allowed"] is False
    single_write.assert_not_awaited()
    bulk_write.assert_not_awaited()


async def test_cache_expiry_refuses_stale_state(lifecycle_status_api, monkeypatch):
    monkeypatch.setattr("packages.ml.model_status._now", lambda: lifecycle_status_api.now)
    first = await get_optimizer_status()
    assert first["demand_model"]["status"] == "qualified-for-reporting"
    assert first["demand_model"]["committed_hash"] == lifecycle_status_api.artifact.artifact_hash
    monkeypatch.setattr(
        "packages.ml.model_status._now", lambda: lifecycle_status_api.now + dt.timedelta(seconds=30)
    )
    expired = await get_optimizer_status()
    assert expired["demand_model"]["status"] == "expired"
    assert expired["demand_model"]["control_ready"] is False
    assert expired["ordinary_writes_allowed"] is False
    assert expired["cop_model"] == _RETIRED_COP_STATUS
    assert lifecycle_status_api.reader.read.await_count == 2


async def test_uncommitted_artifact_is_never_loaded(lifecycle_status_api):
    from dataclasses import replace

    lifecycle_status_api.reader.read.return_value = replace(
        lifecycle_status_api.state, references=()
    )
    with patch(
        "packages.ml.safe_persistence.pickle.loads", side_effect=AssertionError("Uncommitted load")
    ):
        response = await get_optimizer_status()
    assert response["demand_model"]["trained"] is False
    assert response["demand_model"]["committed_hash"] is None
    assert response["control_ready"] is False
    assert lifecycle_status_api.artifact.path.exists()


@pytest.mark.parametrize("tampering", ["hash", "hmac", "schema", "db"])
async def test_hash_hmac_schema_tampering_refuses_load(
    lifecycle_status_api, monkeypatch, tampering
):
    from dataclasses import replace
    from packages.core.config import settings

    first = await get_optimizer_status()
    assert first["demand_model"]["trained"] is True
    if tampering == "hash":
        lifecycle_status_api.artifact.path.write_bytes(b"tampered")
    elif tampering == "hmac":
        monkeypatch.setattr(settings, "secret_key", "different-test-signing-key")
    elif tampering == "schema":
        reference = replace(lifecycle_status_api.state.references[0], schema_version="legacy")
        lifecycle_status_api.reader.read.return_value = replace(
            lifecycle_status_api.state, references=(reference,)
        )
    else:
        lifecycle_status_api.reader.read.side_effect = OSError("private DB failure")
    with patch(
        "packages.ml.safe_persistence.pickle.loads", side_effect=AssertionError("Untrusted load")
    ):
        response = await get_optimizer_status()
    assert response["demand_model"]["status"] == "unavailable"
    assert response["demand_model"]["trained"] is False
    assert response["demand_model"]["control_ready"] is False
    assert response["ordinary_writes_allowed"] is False
    assert response["cop_model"] == _RETIRED_COP_STATUS
    assert "private" not in json.dumps(response)


@pytest.mark.asyncio
async def test_plan_list_preserves_lifecycle_and_price_context() -> None:
    plan = SimpleNamespace(
        id=7,
        created_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        horizon_start=dt.datetime(2026, 9, 14, 1, tzinfo=dt.timezone.utc),
        horizon_end=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
        optimizer_version="milp_v1+ml",
        cost_estimate_eur=3.21,
        price_currency="GBP",
        price_source="octopus",
        status="superseded",
        status_reason="replaced_by_manual_replan",
        superseded_at=dt.datetime(2026, 9, 14, 2, tzinfo=dt.timezone.utc),
        superseded_by_plan_id=8,
    )
    session = SimpleNamespace()
    result = MagicMock()
    result.all.return_value = [(plan, 4)]
    session.execute = AsyncMock(return_value=result)

    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        [response] = await get_plans(limit=10)

    assert response.model_dump() == {
        "id": 7,
        "created_at": plan.created_at,
        "horizon_start": plan.horizon_start,
        "horizon_end": plan.horizon_end,
        "optimizer_version": "milp_v1+ml",
        "cost_estimate_eur": 3.21,
        "price_currency": "GBP",
        "price_source": "octopus",
        "actions_count": 4,
        "status": "superseded",
        "status_reason": "replaced_by_manual_replan",
        "superseded_at": plan.superseded_at,
        "superseded_by_plan_id": 8,
    }


@pytest.mark.asyncio
async def test_plan_detail_preserves_provenance_and_change_summary() -> None:
    plan = SimpleNamespace(
        id=7,
        created_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        horizon_start=dt.datetime(2026, 9, 14, 1, tzinfo=dt.timezone.utc),
        horizon_end=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
        optimizer_version="rules_v3",
        cost_estimate_eur=2.5,
        price_currency="EUR",
        price_source="entsoe",
        status="active",
        status_reason=None,
        superseded_at=None,
        superseded_by_plan_id=None,
        plan_json=json.dumps({"change_summary": json.dumps({"reason": "price_shift"})}),
        input_provenance_json=json.dumps({"input_quality": {"price": {"fresh": True}}}),
    )
    action = SimpleNamespace(
        id=10,
        scheduled_ts=dt.datetime(2026, 9, 14, 3, tzinfo=dt.timezone.utc),
        action_type="quiet_mode_on",
        payload_json="{}",
        status="pending",
        executed_at=None,
        result_json=None,
    )
    session = SimpleNamespace()
    plan_result = SimpleNamespace(scalar_one_or_none=lambda: plan)
    actions_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [action]))
    session.execute = AsyncMock(side_effect=[plan_result, actions_result])

    with (
        patch("packages.api.routers.optimizer.get_session", return_value=_session_context(session)),
        patch(
            "packages.api.routers.optimizer.plan_measurement",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("packages.api._helpers.get_price_area", new_callable=AsyncMock, return_value="NL"),
        patch(
            "packages.core.settings_service.get_float_setting",
            new_callable=AsyncMock,
            return_value=18.0,
        ),
        patch(
            "packages.core.settings_service.get_user_tz", new_callable=AsyncMock, return_value="UTC"
        ),
    ):
        response = await get_plan_detail(7)

    assert response.change_summary == {"reason": "price_shift"}
    assert response.provenance == {"input_quality": {"price": {"fresh": True}}}
    assert response.price_source == "entsoe"


@pytest.mark.asyncio
async def test_optimize_now_enqueues_and_status_reports_durable_record() -> None:
    session = SimpleNamespace()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock()
    added = {}

    def assign_id(request):
        added["request"] = request

    async def flush():
        added["request"].id = 42

    session.add.side_effect = assign_id
    session.flush.side_effect = flush
    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        queued = await optimize_now()

    assert queued == {"status": "queued", "request_id": 42}
    request = added["request"]
    assert request.requested_by == "api"

    session.get.return_value = SimpleNamespace(
        id=42,
        status="completed",
        requested_at=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc),
        started_at=dt.datetime(2026, 9, 14, 0, 1, tzinfo=dt.timezone.utc),
        completed_at=dt.datetime(2026, 9, 14, 0, 2, tzinfo=dt.timezone.utc),
        plan_id=9,
        error=None,
    )
    with patch(
        "packages.api.routers.optimizer.get_session", return_value=_session_context(session)
    ):
        status = await get_optimization_request(42)

    assert status["status"] == "completed"
    assert status["plan_id"] == 9
    assert status["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "fingerprint_matches", "suggestion_available"),
    [
        ("ALLOWED", True, True),
        ("BLOCKED", True, False),
        ("UNKNOWN", True, False),
        ("ALLOWED", False, False),
    ],
    ids=["allowed", "blocked", "unknown", "fingerprint_mismatch"],
)
async def test_heat_curve_advice_uses_current_device_gate_and_fails_closed(
    state, fingerprint_matches, suggestion_available
) -> None:
    config = HeatingGateConfig()
    gate_row = SimpleNamespace(
        device_id="device-a",
        state=state,
        reason_code="test_gate_state",
        config_fingerprint=config.fingerprint if fingerprint_matches else "obsolete",
        last_raw_outdoor_c=5.0,
    )
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = SimpleNamespace(
        device_id="device-a", outdoor_temp=5.0
    )
    gate_result = MagicMock()
    gate_result.scalar_one_or_none.return_value = gate_row
    session = SimpleNamespace(execute=AsyncMock(side_effect=[status_result, gate_result]))

    with (
        patch(
            "packages.core.control_temperature.get_control_temperature",
            new=AsyncMock(return_value=SimpleNamespace(value=18.0, is_usable=True)),
        ),
        patch(
            "packages.core.device_data_quality.get_device_data_quality",
            new=AsyncMock(return_value={"ready": True}),
        ),
        patch(
            "packages.api.routers.models_router.get_session",
            return_value=_session_context(session),
        ),
        patch(
            "packages.core.settings_service.get_heat_curve_config",
            new=AsyncMock(return_value=HeatCurveConfig()),
        ),
        patch(
            "packages.core.settings_service.get_space_heating_gate_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "packages.core.settings_service.get_float_setting",
            new=AsyncMock(return_value=21.5),
        ),
        patch(
            "packages.core.settings_service.get_heat_curve_verification_state",
            new=AsyncMock(return_value={"status": "verified"}),
        ),
    ):
        response = await get_heat_curve_advice()

    gate_statement = session.execute.await_args_list[1].args[0]
    assert "device-a" in gate_statement.compile().params.values()
    assert (response["suggested"] is not None) is suggestion_available
    if suggestion_available:
        assert response["status"] == "too_cold"
        assert response["controllability"] == "heat_curve_effective"
    else:
        assert response["suggested"] is None
        assert response["status"] == "not_controllable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cop_count", "consumption_count"),
    [(0, 0), (49, 50), (50, 49), (50, 50), (10000, 10000)],
)
async def test_auto_never_queries_or_qualifies_legacy_history(cop_count, consumption_count):
    from packages.optimizer.main import _select_optimizer

    cop_result = MagicMock()
    cop_result.scalar.return_value = cop_count
    consumption_result = MagicMock()
    consumption_result.scalar.return_value = consumption_count
    session = SimpleNamespace(execute=AsyncMock(side_effect=[cop_result, consumption_result]))

    with (
        patch(
            "packages.optimizer.main.get_session", return_value=_session_context(session)
        ) as get_session,
        patch("packages.optimizer.main._demand_model", SimpleNamespace(is_trained=True)),
        patch("packages.optimizer.main.MILPOptimizer", side_effect=AssertionError("auto MILP")),
    ):
        layer, optimizer = await _select_optimizer("auto")

    assert layer == "rules"
    assert type(optimizer).__name__ == "RulesOptimizer"
    get_session.assert_not_called()
    session.execute.assert_not_awaited()

"""Scoped review regressions using captured inputs and fake resources only."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_optimizer_api_contracts import (
    TestReadinessReportingRepairs as _ExistingReadiness,
)


class TestQRReadinessProjection:
    captured = _ExistingReadiness.captured
    reference = staticmethod(_ExistingReadiness.reference)

    @pytest.mark.parametrize("case", ["tank_only", "selected_candidate", "expired", "qualified"])
    async def test_QR_F2_API_uses_selected_head_projection(self, captured, monkeypatch, case):
        from packages.api.routers import optimizer
        from packages.ml import model_status, seasonal_learning
        from packages.ml.lifecycle import LifecycleSnapshot

        incumbent = self.reference(
            "thermal",
            ["tank_heating"] if case == "tank_only" else ["indoor_heating"],
            captured["as_of"],
        )
        references = [captured["state"].reference("demand"), incumbent]
        if case == "selected_candidate":
            references.append(
                replace(
                    incumbent,
                    role="candidate",
                    artifact_hash="c" * 64,
                    qualified_at=None,
                    qualification_expires_at=None,
                    last_valid_outcome_at=None,
                    qualification_scope=None,
                )
            )
        elif case == "expired":
            references[-1] = replace(incumbent, qualification_expires_at=captured["as_of"])
        captured["state"] = replace(captured["state"], references=tuple(references))
        assert isinstance(captured["state"], LifecycleSnapshot)
        snapshot = model_status._compose_readiness(captured)
        # Compose returns the projection; the route snapshot also carries captured inputs.
        snapshot.update(
            context=captured["context"],
            as_of=captured["as_of"],
            seasonal_inputs=captured["seasonal_inputs"],
        )
        monkeypatch.setattr(
            model_status, "get_readiness_snapshot", AsyncMock(return_value=snapshot)
        )
        monkeypatch.setattr(model_status, "get_model_status", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "packages.optimizer.main.get_optimizer_status_snapshot",
            AsyncMock(return_value={"active_layer": "rules"}),
        )
        monkeypatch.setattr(optimizer, "_learning_mode_status", AsyncMock(return_value={}))
        monkeypatch.setattr(
            seasonal_learning,
            "get_seasonal_calibration_status",
            AsyncMock(
                return_value={
                    "enabled": True,
                    "observe_only_active": True,
                    "heating_season_detected": True,
                    "reason": "fixture",
                    "auto_train": False,
                    "auto_exit": False,
                    "open_revert_obligations": captured["seasonal_inputs"][
                        "open_revert_obligations"
                    ],
                }
            ),
        )
        monkeypatch.setattr(
            "packages.core.settings_service.set_settings_bulk",
            AsyncMock(side_effect=AssertionError("GET must not write")),
        )

        response = await optimizer.get_optimizer_status()

        seasonal = response["seasonal_calibration"]
        assert seasonal["open_revert_obligations"]["count"] == 0
        indoor = seasonal["readiness"]["components"]["indoor_heating"]
        assert seasonal["reporting_ready"] is (case == "qualified")
        assert (indoor["reporting"]["status"] == "qualified-for-reporting") is (case == "qualified")
        assert (seasonal["next_step"] == "ready_for_reporting") is (case == "qualified")
        assert seasonal["control_ready"] is response["ordinary_writes_allowed"] is False
        if case == "tank_only":
            assert snapshot["model_lifecycle"]["thermal"]["status"] == "qualified-for-reporting"
            assert indoor["fit"]["status"] == "not_declared"
            assert seasonal["next_step"] == "await_indoor_heating_fit"
        elif case == "selected_candidate":
            assert indoor["reporting"]["artifact_hash"] == "c" * 64
            assert indoor["reporting"]["status"] == "candidate"


async def test_QR_F2_unavailable_snapshot_has_explicit_false_summary(monkeypatch):
    from packages.api.routers import optimizer
    from packages.ml import model_status

    snapshot = {
        "context": None,
        "as_of": dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc),
        "seasonal_inputs": None,
        "model_lifecycle": {
            component: {"status": "qualified-for-reporting", "trained": True}
            for component in ("demand", "thermal", "comfort")
        },
        "readiness": {"components": {}},
        "thermal_model": {},
        "demand_quality": {},
        "device_quality": None,
    }
    monkeypatch.setattr(model_status, "get_readiness_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(model_status, "get_model_status", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "packages.optimizer.main.get_optimizer_status_snapshot",
        AsyncMock(return_value={"active_layer": "rules"}),
    )
    response = await optimizer.get_optimizer_status()
    seasonal = response["seasonal_calibration"]
    assert seasonal["reporting_ready"] is seasonal["control_ready"] is False
    assert seasonal["next_step"] == "readiness_unavailable"
    assert seasonal["enabled"] is seasonal["observe_only_active"] is None


@pytest.mark.parametrize("case", ["future_B", "explicit_A_latest_B", "explicit_A_missing"])
async def test_QR_F3_capture_bounds_clock_and_selected_device(monkeypatch, case):
    from packages.core import settings_service
    from packages.core.models import DeviceStatusRecord
    from packages.ml import model_status, seasonal_learning, thermal, forecast_quality
    from packages.ml.lifecycle import LifecycleUnavailable

    now = dt.datetime(2026, 10, 12, 8, tzinfo=dt.timezone.utc)
    a = DeviceStatusRecord(device_id="A", ts=now - dt.timedelta(minutes=1))
    b = DeviceStatusRecord(
        device_id="B",
        ts=now + dt.timedelta(minutes=1) if case == "future_B" else now - dt.timedelta(seconds=1),
    )
    rows = [b] if case == "explicit_A_missing" else [a, b]
    statements = []

    async def execute(statement):
        assert statement.is_select, "readiness must not perform DML"
        statements.append(statement)
        params = statement.compile().params
        if statement.column_descriptions[0]["entity"] is DeviceStatusRecord:
            assert params["ts_1"] == now
            assert "device_status.ts <=" in str(statement)
            candidates = [row for row in rows if row.ts <= params["ts_1"]]
            if case != "future_B":
                assert params["device_id_1"] == "A"
                candidates = [row for row in candidates if row.device_id == params["device_id_1"]]
            chosen = max(candidates, key=lambda row: row.ts) if candidates else None
            return SimpleNamespace(scalar_one_or_none=lambda: chosen)
        return SimpleNamespace(scalar_one=lambda: 0)

    class Session:
        async def __aenter__(self):
            return SimpleNamespace(execute=execute)

        async def __aexit__(self, *_args):
            return False

    def sessions(**options):
        assert options == {"read_only": True, "isolation_level": "REPEATABLE READ"}
        return Session()

    values = {key: spec.serialize_default() for key, spec in settings_service.SETTING_SPECS.items()}
    monkeypatch.setattr(model_status, "get_session", sessions)
    monkeypatch.setattr(settings_service, "get_all_settings", AsyncMock(return_value=values))
    monkeypatch.setattr(
        "packages.poller.smartthings.get_selected_device_ids", AsyncMock(return_value=["room"])
    )
    monkeypatch.setattr(
        model_status.Lifecycle,
        "read",
        AsyncMock(side_effect=LifecycleUnavailable("lifecycle_not_initialized")),
    )
    monkeypatch.setattr(
        seasonal_learning, "materialize_seasonal_inputs", AsyncMock(return_value={})
    )
    native = AsyncMock(return_value={})
    materialized = AsyncMock(return_value={})
    monkeypatch.setattr(forecast_quality, "materialize_native_inputs", native)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", materialized)
    captured = await model_status._capture_readiness_inputs(
        as_of=now,
        generation="qr-f3",
        components=("thermal",),
        supplied_context=None if case == "future_B" else {"device_id": "A"},
    )
    assert captured.get("unavailable_reason") is None
    assert captured["context"]["device_id"] == "A"
    assert len(statements) == 2
    assert "A" in statements[1].compile().params.values()
    assert now in statements[1].compile().params.values()
    assert materialized.await_args.args[0] == "A"
    assert native.await_args.kwargs["device_id"] == "A"
    latest = captured["latest_status"]
    if case == "explicit_A_missing":
        assert latest is None
    else:
        assert latest.device_id == "A" and latest.ts == a.ts and latest.ts <= now
    from packages.core.device_data_quality import device_data_quality_from_inputs

    quality = device_data_quality_from_inputs(
        now=now,
        values={
            **values,
            "aquarea_username": "test",
            "aquarea_password": "test",
        },
        status=latest,
    )
    assert quality["ready"] is (case != "explicit_A_missing")
    assert quality["age_seconds"] == (None if latest is None else 60)


def _meter_rows():
    now = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    return [
        SimpleNamespace(
            device_id="A",
            ts=now + dt.timedelta(minutes=minute),
            heat_kwh=value,
            cool_kwh=0.0,
            tank_kwh=0.0,
            source_date=now.date(),
            source_timezone="UTC",
        )
        for minute, value in [(30, 0), (90, 2), (91, 2), (150, 4), (151, 4)]
    ]


@pytest.mark.parametrize("window", ["whole", "clipped", "missing_tariff"])
def test_R15_tariff_allocation_uses_interval_duration_not_endpoint(window):
    from packages.core.plan_outcome import duration_allocated_cost

    rows = _meter_rows()
    midnight = rows[0].ts.replace(minute=0)
    prices = {
        midnight: 0.1,
        midnight + dt.timedelta(hours=1): -0.2,
        midnight + dt.timedelta(hours=2): 0.3,
    }
    if window == "missing_tariff":
        prices.pop(midnight + dt.timedelta(hours=1))
    start = midnight + dt.timedelta(hours=1) if window == "clipped" else midnight
    end = midnight + dt.timedelta(hours=2 if window == "clipped" else 3)
    result = duration_allocated_cost(rows, prices, start=start, end=end)
    assert result["measured_kwh"] == (2 if window == "clipped" else 4)
    assert result["actual_cost"] == pytest.approx(
        -0.4 if window == "clipped" else 0.4 if window == "missing_tariff" else 0.0
    )
    assert result["coverage_pct"] == (50.0 if window == "missing_tariff" else 100.0)
    assert result["tariff_allocation_estimated"] is True
    assert result["command_attribution_supported"] is False


@pytest.mark.parametrize("case", ["valid", "empty", "pending", "price_gap", "invalid_price"])
def test_R15_baseline_uses_confirmed_deltas_duration_and_device(case):
    from packages.core.plan_outcome import observed_rate_baseline

    rows = _meter_rows()
    midnight = rows[0].ts.replace(minute=0)
    foreign = [
        SimpleNamespace(**{**vars(row), "device_id": "B", "heat_kwh": row.heat_kwh * 100})
        for row in rows
    ]
    if case == "empty":
        rows = []
    elif case == "pending":
        rows = rows[:2]
    prices = [(midnight + dt.timedelta(hours=hour), 0.2) for hour in range(3)]
    if case == "price_gap":
        prices.pop(1)
    elif case == "invalid_price":
        prices[1] = (prices[1][0], float("nan"))
    result = observed_rate_baseline(rows + foreign, prices, device_id="A")
    assert result["strategy_cost"] is None
    assert result["strategy_cost_reason"] == "serialized_strategy_replay_unavailable"
    assert result["baseline_estimated"] is True
    assert result["unshifted_baseline_cost"] == (pytest.approx(1.2) if case == "valid" else None)
    if case == "valid":
        assert result["measured_interval_hours"] == 2
        assert result["measured_interval_kwh"] == 4
        assert result["confirmed_channel_intervals"] == 2


async def test_R15_owner_returns_nullable_strategy_and_bounded_baseline(monkeypatch):
    from packages.optimizer import rules_engine

    rows = _meter_rows()
    statements = []
    now = rows[-1].ts

    class Context:
        async def __aenter__(self):
            async def execute(statement):
                assert statement.is_select
                statements.append(statement)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

            return SimpleNamespace(execute=execute)

        async def __aexit__(self, *_args):
            return False

    def sessions(**kwargs):
        assert kwargs == {"read_only": True}
        return Context()

    monkeypatch.setattr(rules_engine, "get_session", sessions)
    monkeypatch.setattr(rules_engine, "get_user_tz", AsyncMock(return_value="UTC"))
    optimizer = rules_engine.RulesOptimizer()
    prices = [(now.replace(hour=0, minute=0) + dt.timedelta(hours=hour), 0.2) for hour in range(3)]
    assert await optimizer._estimate_cost([{"type": "force_dhw_on"}], prices) is None
    result = await optimizer._estimate_unshifted_baseline(prices, device_id="A", now=now)
    assert result["unshifted_baseline_cost"] == pytest.approx(1.2)
    params = statements[0].compile().params
    assert params["device_id_1"] == "A"
    assert params["ts_2"] == now and params["ts_1"] == now - dt.timedelta(days=7)
    assert params["param_1"] == 20001


@pytest.mark.parametrize("case", ["source_failure", "source_overflow"])
async def test_R15_unavailable_baseline_never_invents_daily_consumption(monkeypatch, case):
    from packages.optimizer import rules_engine

    class Context:
        async def __aenter__(self):
            if case == "source_failure":
                raise RuntimeError("fixture source failure")
            return SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(
                        scalars=lambda: SimpleNamespace(all=lambda: [None] * 20001)
                    )
                )
            )

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(rules_engine, "get_session", lambda **kwargs: Context())
    monkeypatch.setattr(rules_engine, "get_user_tz", AsyncMock(return_value="UTC"))
    now = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    result = await rules_engine.RulesOptimizer()._estimate_unshifted_baseline(
        [(now, 0.2)], device_id="A", now=now
    )
    assert result["strategy_cost"] is result["unshifted_baseline_cost"] is None
    assert result["baseline_unavailable_reason"] == (
        "source_unavailable" if case == "source_failure" else "source_row_budget_exceeded"
    )


@pytest.mark.parametrize(
    "indoor,outdoor", [(None, 5), (18, None), (float("nan"), 5), (18, float("inf"))]
)
def test_R32_missing_or_nonfinite_advice_never_suggests(indoor, outdoor):
    from packages.core.heat_curve import HeatCurveConfig, heat_curve_advice
    from packages.core.space_heating_gate import HeatingGateConfig, resolve_effective_gate

    config = HeatingGateConfig()
    gate = SimpleNamespace(
        state="ALLOWED",
        reason_code="fixture",
        config_fingerprint=config.fingerprint,
        last_raw_outdoor_c=5.0,
    )
    result = heat_curve_advice(
        HeatCurveConfig(), indoor, 21.5, outdoor, resolve_effective_gate(gate, config)
    )
    assert result["status"] == "insufficient_data" and result["suggested"] is None
    assert result["manual_only"] is True


@pytest.mark.parametrize(
    "case",
    [
        "fresh",
        "stale_status",
        "future_status",
        "stale_room",
        "future_room",
        "foreign_room",
        "nonfinite_status",
    ],
)
async def test_R32_API_requires_fresh_selected_nonactuating_evidence(monkeypatch, case):
    from packages.api.routers import models_router
    from packages.core import settings_service
    from packages.core.models import DeviceStatusRecord, IndoorTempReading, SettingRecord
    from packages.core.heat_curve import HeatCurveConfig
    from packages.core.space_heating_gate import HeatingGateConfig

    now = dt.datetime.now(dt.timezone.utc)
    status = DeviceStatusRecord(
        device_id="A",
        ts=now + dt.timedelta(hours=1)
        if case == "future_status"
        else now - dt.timedelta(hours=1)
        if case == "stale_status"
        else now,
        outdoor_temp=float("nan") if case == "nonfinite_status" else 5.0,
    )
    room = SimpleNamespace(
        id=1,
        device_id="foreign" if case == "foreign_room" else "room",
        timestamp=now + dt.timedelta(hours=1)
        if case == "future_room"
        else now - dt.timedelta(hours=1)
        if case == "stale_room"
        else now,
        temperature=18.0,
        is_stale=False,
        device_label="Room",
        room="Room",
    )
    config = HeatingGateConfig()
    gate = SimpleNamespace(
        device_id="A",
        state="ALLOWED",
        reason_code="fixture",
        config_fingerprint=config.fingerprint,
        last_raw_outdoor_c=5.0,
    )
    statements = []

    async def execute(statement):
        assert statement.is_select
        statements.append(statement)
        params = statement.compile().params
        entity = statement.column_descriptions[0]["entity"]
        if entity is DeviceStatusRecord:
            assert "device_status.ts <=" in str(statement)
            return SimpleNamespace(
                scalar_one_or_none=lambda: status if status.ts <= params["ts_1"] else None
            )
        if entity is IndoorTempReading:
            assert params["device_id_1"] == ["room"]
            assert params["timestamp_2"] >= now
            rows = (
                [room]
                if (
                    params["timestamp_1"] <= room.timestamp <= params["timestamp_2"]
                    and room.device_id in params["device_id_1"]
                    and not room.is_stale
                )
                else []
            )
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        if entity is SettingRecord:
            key = params["key_1"]
            value = (
                "room"
                if key == "comfort_reference_sensor_id"
                else SimpleNamespace(value=values[key])
            )
            return SimpleNamespace(scalar_one_or_none=lambda: value)
        return SimpleNamespace(scalar_one_or_none=lambda: gate)

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(execute=execute)

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(models_router, "get_session", lambda **options: Context())
    monkeypatch.setattr(
        "packages.poller.smartthings.get_selected_device_ids", AsyncMock(return_value=["room"])
    )
    values = {
        "aquarea_username": "test",
        "aquarea_password": "test",
        "device_status_max_age_minutes": "15",
        "poll_interval_seconds": "300",
    }
    monkeypatch.setattr(
        settings_service,
        "get_setting",
        AsyncMock(side_effect=lambda key, **kwargs: values[key]),
    )
    monkeypatch.setattr(
        settings_service, "get_heat_curve_config", AsyncMock(return_value=HeatCurveConfig())
    )
    monkeypatch.setattr(
        settings_service, "get_space_heating_gate_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(settings_service, "get_float_setting", AsyncMock(return_value=21.5))
    monkeypatch.setattr(
        settings_service,
        "get_heat_curve_verification_state",
        AsyncMock(return_value={"status": "verified"}),
    )
    writes = AsyncMock(side_effect=AssertionError("manual advice must not mutate settings"))
    monkeypatch.setattr(settings_service, "set_heat_curve_verification_state", writes)
    result = await models_router.get_heat_curve_advice()
    assert result["manual_only"] is True
    assert (result["suggested"] is not None) is (case == "fresh")
    if case == "fresh":
        assert result["status"] == "too_cold"
    else:
        assert result["status"] in {"insufficient_data", "not_controllable"}
    assert statements and all(statement.is_select for statement in statements)
    writes.assert_not_awaited()
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("origin", ["pending", "audit_recovery"])
@pytest.mark.parametrize("current", ["fresh", "insufficient"])
@pytest.mark.parametrize("thresholds_met", [False, True])
async def test_R32_GET_verification_is_derived_without_DML(
    monkeypatch, origin, current, thresholds_met
):
    from packages.api.routers import models_router
    from packages.core import settings_service
    from packages.core.heat_curve import (
        HeatCurveConfig,
        HEAT_CURVE_SETTING_KEYS,
        VERIFICATION_MIN_HOURS,
        VERIFICATION_MIN_INDOOR_SAMPLES,
        VERIFICATION_MIN_HEATING_CONDITION_SAMPLES,
        start_heat_curve_verification,
    )
    from packages.core.models import (
        DeviceStatusRecord,
        IndoorTempReading,
        AuditLogRecord,
    )
    from packages.core.space_heating_gate import HeatingGateConfig

    now = dt.datetime.now(dt.timezone.utc)
    started_at = now - dt.timedelta(hours=VERIFICATION_MIN_HOURS + 1 if thresholds_met else 1)
    curve, config = HeatCurveConfig(), HeatingGateConfig()
    persisted = start_heat_curve_verification(
        started_at=started_at.isoformat(),
        previous_curve=None,
        applied_curve=curve,
        baseline_indoor_temp=18.0,
        baseline_outdoor_temp=5.0,
        comfort_target=21.5,
        source="settings_save",
    )
    assert VERIFICATION_MIN_HOURS + 1 < 7 * 24
    stored = persisted if origin == "pending" else {}
    before = json.dumps(stored, sort_keys=True)
    statements = []
    status = SimpleNamespace(device_id="A", ts=now, outdoor_temp=5.0)
    gate = SimpleNamespace(
        device_id="A",
        state="ALLOWED",
        reason_code="fixture",
        config_fingerprint=config.fingerprint,
        last_raw_outdoor_c=5.0,
    )

    async def execute(statement):
        assert statement.is_select, "GET must perform zero DML"
        statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        sql = str(statement)
        if entity is AuditLogRecord:
            audit = SimpleNamespace(
                ts=started_at,
                payload_json=json.dumps({next(iter(HEAT_CURVE_SETTING_KEYS)): 40}),
            )
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [audit]))
        if entity is IndoorTempReading:
            if "indoor_temp_readings.timestamp > " in sql or (
                "timestamp > " in sql and "timestamp <=" not in sql
            ):
                values = [20.0] * (VERIFICATION_MIN_INDOOR_SAMPLES if thresholds_met else 0)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))
            return SimpleNamespace(scalar=lambda: 18.0)
        if entity is DeviceStatusRecord:
            if "device_status.ts > " in sql:
                values = [5.0] * (
                    VERIFICATION_MIN_HEATING_CONDITION_SAMPLES if thresholds_met else 0
                )
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))
            return SimpleNamespace(scalar_one_or_none=lambda: status)
        return SimpleNamespace(scalar_one_or_none=lambda: gate)

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(execute=execute)

        async def __aexit__(self, *_args):
            return False

    def sessions(**options):
        assert options == {"read_only": True}
        return Context()

    monkeypatch.setattr(models_router, "get_session", sessions)
    monkeypatch.setattr(
        "packages.core.control_temperature.get_control_temperature",
        AsyncMock(
            return_value=SimpleNamespace(
                value=18.0 if current == "fresh" else None,
                is_usable=current == "fresh",
            )
        ),
    )
    monkeypatch.setattr(
        "packages.core.device_data_quality.get_device_data_quality",
        AsyncMock(return_value={"ready": current == "fresh"}),
    )
    monkeypatch.setattr(settings_service, "get_heat_curve_config", AsyncMock(return_value=curve))
    monkeypatch.setattr(
        settings_service, "get_space_heating_gate_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(settings_service, "get_float_setting", AsyncMock(return_value=21.5))
    monkeypatch.setattr(
        settings_service, "get_heat_curve_verification_state", AsyncMock(return_value=stored)
    )
    setter = AsyncMock(side_effect=AssertionError("GET persistence forbidden"))
    for name in ("set_heat_curve_verification_state", "set_setting", "set_settings_bulk"):
        monkeypatch.setattr(settings_service, name, setter)

    response = await models_router.get_heat_curve_advice()

    verification = response["verification"]
    assert verification["status"] == ("verified" if thresholds_met else "pending")
    assert verification["recommendation_available"] is thresholds_met
    assert verification["minimum_hours"] == VERIFICATION_MIN_HOURS
    assert verification["minimum_indoor_samples"] == VERIFICATION_MIN_INDOOR_SAMPLES
    assert verification["minimum_heating_condition_samples"] == (
        VERIFICATION_MIN_HEATING_CONDITION_SAMPLES
    )
    assert (response["suggested"] is not None) is (thresholds_met and current == "fresh")
    assert response["manual_only"] is True
    assert response["status"] == (
        "insufficient_data"
        if current == "insufficient"
        else "too_cold"
        if thresholds_met
        else "verification_pending"
    )
    if thresholds_met:
        assert verification["completed_at"]
    assert statements and all(statement.is_select for statement in statements)
    assert json.dumps(stored, sort_keys=True) == before
    setter.assert_not_awaited()
    json.dumps(response, allow_nan=False)

import pytest

from packages.core.comfort_assessment import build_comfort_assessment
from packages.core.control_temperature import RoomComfortEnvelope
from packages.core.heat_curve import HeatCurveConfig
from packages.core.space_heating_gate import HeatingGateConfig, resolve_effective_gate
from types import SimpleNamespace


def test_cutoff_blocked_forecast_miss_gets_manual_trial_advice():
    assessment = build_comfort_assessment(
        forecast=[
            {
                "hour": 18,
                "ts": "2026-07-29T07:00:00+00:00",
                "predicted_indoor_temp": 20.4,
                "space_heating_fraction": 0.0,
                "model_source": "rules_thermal_fallback",
            }
        ],
        targets=[{"hour": 18, "target": 21.5, "comfort_hour": True}],
        weather=[{"outdoor_temp": 17.8}],
        planned_actions=[],
        heat_curve=HeatCurveConfig(heating_off_outdoor_c=12.0),
        gate_projections=[
            resolve_effective_gate(
                SimpleNamespace(
                    state="BLOCKED",
                    reason_code="above_off_threshold",
                    config_fingerprint=HeatingGateConfig().fingerprint,
                    last_raw_outdoor_c=17.8,
                ),
                HeatingGateConfig(),
            )
        ],
    )

    assert assessment["state"] == "at_risk"
    assert assessment["controllability"]["status"] == "blocked_by_heating_off_cutoff"
    advice = assessment["recommendations"][0]
    assert advice["manual_only"] is True
    assert advice["verification_required"] is True
    assert advice["minimum_candidate_value_c"] == 18.3


def test_later_filtered_miss_uses_its_original_blocked_gate_projection():
    assessment = build_comfort_assessment(
        forecast=[
            {"hour": 1, "predicted_indoor_temp": 21.0, "space_heating_fraction": 0.0},
            {"hour": 2, "predicted_indoor_temp": 20.9, "space_heating_fraction": 0.0},
            {"hour": 3, "predicted_indoor_temp": 20.0, "space_heating_fraction": 0.0},
        ],
        targets=[
            {"hour": 1, "target": 21.5, "comfort_hour": False},
            {"hour": 2, "target": 21.5, "comfort_hour": False},
            {"hour": 3, "target": 21.5, "comfort_hour": True},
        ],
        weather=[{"outdoor_temp": 10.0}, {"outdoor_temp": 10.0}, {"outdoor_temp": 17.0}],
        planned_actions=[],
        heat_curve=HeatCurveConfig(heating_off_outdoor_c=12.0),
        gate_projections=[
            SimpleNamespace(state="ALLOWED"),
            SimpleNamespace(state="ALLOWED"),
            SimpleNamespace(state="BLOCKED"),
        ],
    )

    assert assessment["misses"] == [assessment["first_miss"]]
    assert assessment["first_miss"]["hour"] == 3
    assert assessment["controllability"]["status"] == "blocked_by_heating_off_cutoff"


def test_mode_only_miss_is_explained_without_manual_curve_advice():
    assessment = build_comfort_assessment(
        forecast=[
            {
                "hour": 4,
                "predicted_indoor_temp": 20.9,
                "space_heating_fraction": 0.0,
            }
        ],
        targets=[{"hour": 4, "target": 21.5, "comfort_hour": True}],
        weather=[{"outdoor_temp": 8.0}],
        planned_actions=[{"hour": 4, "action_type": "normal_mode_on"}],
        heat_curve=HeatCurveConfig(heating_off_outdoor_c=13.0),
    )

    assert assessment["controllability"]["status"] == "mode_only_no_space_heat"
    assert assessment["recommendations"] == []


def test_unrelated_mode_action_is_not_used_to_explain_an_earlier_miss():
    assessment = build_comfort_assessment(
        forecast=[
            {
                "hour": 4,
                "predicted_indoor_temp": 20.9,
                "space_heating_fraction": 0.0,
            }
        ],
        targets=[{"hour": 4, "target": 21.5, "comfort_hour": True}],
        weather=[{"outdoor_temp": 8.0}],
        planned_actions=[{"hour": 18, "action_type": "normal_mode_on"}],
        heat_curve=HeatCurveConfig(heating_off_outdoor_c=13.0),
    )

    assert assessment["controllability"]["status"] == "no_space_heat_expected"


def test_setback_miss_does_not_claim_a_comfort_target_miss():
    assessment = build_comfort_assessment(
        forecast=[{"hour": 3, "predicted_indoor_temp": 18.5, "space_heating_fraction": 0.0}],
        targets=[{"hour": 3, "target": 19.0, "comfort_hour": False}],
        weather=[{"outdoor_temp": 10.0}],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
    )

    assert assessment["state"] == "at_risk"
    assert assessment["first_miss"]["comfort_hour"] is False


def test_simultaneous_under_min_and_over_max_reports_min_priority_conflict():
    assessment = build_comfort_assessment(
        forecast=[],
        targets=[],
        weather=[],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        room_envelope=RoomComfortEnvelope(19.0, 19.0, 23.0, ("bedroom",)),
        comfort_temp_min=20.0,
    )

    assert assessment["state"] == "conflict"
    assert assessment["reason_code"] == "comfort_conflict_min_priority"
    assert assessment["min_comfort_active"] is True
    assert assessment["room_overheat_active"] is True


def test_room_overheat_alone_suppresses_discretionary_heat():
    assessment = build_comfort_assessment(
        forecast=[],
        targets=[],
        weather=[],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        room_envelope=RoomComfortEnvelope(21.0, 20.5, 23.0, ("bedroom",)),
        comfort_temp_min=20.0,
    )

    assert assessment["state"] == "room_overheat_suppression"
    assert assessment["reason_code"] == "room_overheat_suppression"


def test_baseline_heat_insufficient_assessment():
    assessment = build_comfort_assessment(
        forecast=[
            {
                "hour": 1,
                "predicted_indoor_temp": 19.0,
                "space_heating_fraction": 0.35,
                "baseline_heating_fraction": 0.35,
                "space_heating_source": "baseline",
                "baseline_heating_source": "history",
            }
        ],
        targets=[{"hour": 1, "target": 20.0, "comfort_hour": True}],
        weather=[{"outdoor_temp": 3.0}],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
    )

    assert assessment["controllability"]["status"] == "baseline_heat_insufficient"


class TestForecastLimitationMessages:
    @staticmethod
    def assess(status, reason):
        return build_comfort_assessment(
            forecast=[],
            targets=[],
            weather=[],
            planned_actions=[],
            heat_curve=HeatCurveConfig(),
            forecast_status=status,
            forecast_reason=reason,
        )

    def test_insufficient_scored_forecasts_is_not_a_sensor_warning(self):
        from packages.ml.forecast_quality import _quality_gate

        quality = _quality_gate({"samples": 0})
        assert quality["control_allowed"] is False
        assessment = self.assess("fallback", quality["reason"])

        assert assessment["state"] == "degraded"
        assert assessment["reason_code"] == "insufficient_scored_forecasts"
        assert "too few scored forecasts" in assessment["summary"].lower()
        assert "sensor" not in assessment["summary"].lower()
        assert "fresh trusted" not in assessment["summary"].lower()
        assert assessment["misses"] == assessment["recommendations"] == []

    def test_observe_only_build_explains_write_prohibition(self):
        assessment = self.assess("fallback", "observe_only_build")

        assert assessment["state"] == "degraded"
        assert assessment["reason_code"] == "observe_only_build"
        assert "observe-only" in assessment["summary"]
        assert "writes are prohibited" in assessment["summary"]
        assert "fresh trusted" not in assessment["summary"].lower()

    @pytest.mark.parametrize(
        "reason",
        [
            "no_trusted_indoor_observation",
            "no_fresh_selected_sensor_readings",
            "reference_sensor_not_fresh",
            "all_sensor_readings_rejected_as_outliers",
        ],
    )
    def test_missing_or_stale_sensor_keeps_sensor_warning(self, reason):
        assessment = self.assess("unavailable", reason)

        assert assessment["state"] == "unavailable"
        assert assessment["reason_code"] == reason
        assert "fresh trusted indoor observation is required" in assessment["summary"]
        assert assessment["misses"] == assessment["recommendations"] == []

    @pytest.mark.parametrize(
        "status, reason",
        [
            ("fallback", None),
            ("legacy", None),
            ("unavailable", "stale_forecast"),
            ("unavailable", "numeric_forecast_unavailable"),
        ],
    )
    def test_other_forecast_limitations_do_not_invent_sensor_failure(self, status, reason):
        assessment = self.assess(status, reason)

        assert assessment["state"] == ("degraded" if status == "fallback" else "unavailable")
        assert "fresh trusted" not in assessment["summary"].lower()

    @pytest.mark.parametrize(
        "reason",
        [
            "insufficient_scored_forecasts",
            "no_fresh_selected_sensor_readings",
        ],
    )
    def test_active_plan_preserves_the_actual_limitation_reason(self, reason):
        from packages.api.routers.models_router import _build_active_plan_comfort_assessment

        status = "fallback" if reason == "insufficient_scored_forecasts" else "unavailable"
        assessment = _build_active_plan_comfort_assessment(
            plan_snapshot={
                "forecast_status": status,
                "forecast_quality": {"reason": reason},
            },
            planned_actions=[],
            heat_curve=HeatCurveConfig(),
            gate_projections=[],
            room_envelope=None,
            comfort_temp_min=20.0,
        )

        assert assessment["reason_code"] == reason
        assert assessment == self.assess(status, reason)

    def test_reporting_policy_does_not_misclassify_quality_or_change_raw_gate(self):
        from packages.api.routers.models_router import (
            _forecast_status_from_quality,
            _reporting_forecast_quality,
        )

        raw = {"control_allowed": True, "reason": "ok"}
        reported = _reporting_forecast_quality(raw)

        assert reported["control_reason"] == "observe_only_build"
        assert reported["control_allowed"] is False
        assert reported["reason"] == "ok"
        assert raw == {"control_allowed": True, "reason": "ok"}
        assert _forecast_status_from_quality(raw) == "available"

    def test_active_plan_missing_sensor_reason_takes_priority_over_quality(self):
        from packages.api.routers.models_router import _build_active_plan_comfort_assessment

        assessment = _build_active_plan_comfort_assessment(
            plan_snapshot={
                "forecast_status": "unavailable",
                "forecast_unavailable_reason": "no_fresh_selected_sensor_readings",
                "forecast_quality": {"reason": "insufficient_scored_forecasts"},
            },
            planned_actions=[],
            heat_curve=HeatCurveConfig(),
            gate_projections=[],
            room_envelope=None,
            comfort_temp_min=20.0,
        )

        assert assessment["reason_code"] == "no_fresh_selected_sensor_readings"
        assert "fresh trusted indoor observation is required" in assessment["summary"]

    def test_active_plan_accepts_absent_quality_metadata(self):
        from packages.api.routers.models_router import _build_active_plan_comfort_assessment

        assessment = _build_active_plan_comfort_assessment(
            plan_snapshot={"forecast_status": "legacy", "forecast_quality": None},
            planned_actions=[],
            heat_curve=HeatCurveConfig(),
            gate_projections=[],
            room_envelope=None,
            comfort_temp_min=20.0,
        )

        assert assessment["reason_code"] == "legacy_forecast"
        assert "legacy format" in assessment["summary"]


class TestForecastLimitationEdgeCases:
    @staticmethod
    def assess(status, reason):
        return TestForecastLimitationMessages.assess(status, reason)

    def test_unavailable_without_reason_does_not_claim_sensor_failure(self):
        assessment = self.assess("unavailable", None)

        assert assessment["state"] == "unavailable"
        assert assessment["reason_code"] == "forecast_unavailable"
        assert "fresh trusted" not in assessment["summary"].lower()

    @pytest.mark.parametrize(
        "reason",
        ["insufficient_evidence,flat_evidence", "forecast_quality_gate_error:ValueError"],
    )
    def test_unknown_or_compound_reasons_are_named_without_sensor_claim(self, reason):
        assessment = self.assess("fallback", reason)

        assert assessment["reason_code"] == reason
        assert assessment["summary"].startswith("Comfort risk cannot be assessed: ")
        assert "fresh trusted" not in assessment["summary"].lower()

    def test_explicit_reason_overrides_status_default(self):
        assessment = self.assess("legacy", "insufficient_scored_forecasts")

        assert assessment["state"] == "unavailable"
        assert assessment["reason_code"] == "insufficient_scored_forecasts"
        assert "too few scored forecasts" in assessment["summary"].lower()

    def test_available_forecast_has_no_limitation_reason(self):
        assessment = self.assess("available", "insufficient_scored_forecasts")

        assert "reason_code" not in assessment
        assert assessment["state"] not in {"degraded", "unavailable"}

    def test_active_plan_observe_only_snapshot_reason_is_explained(self):
        from packages.api.routers.models_router import _build_active_plan_comfort_assessment

        assessment = _build_active_plan_comfort_assessment(
            plan_snapshot={
                "forecast_status": "fallback",
                "forecast_unavailable_reason": None,
                "forecast_quality": {"reason": "observe_only_build"},
            },
            planned_actions=[],
            heat_curve=HeatCurveConfig(),
            gate_projections=[],
            room_envelope=None,
            comfort_temp_min=20.0,
        )

        assert assessment["reason_code"] == "observe_only_build"
        assert "writes are prohibited" in assessment["summary"]


class TestIndoorForecastApiLimitationReason:
    """G12: the live /api/thermal/indoor-forecast response carries the real reason."""

    @staticmethod
    async def call_api(
        monkeypatch,
        *,
        quality,
        indoor_value,
        reason=None,
        contract=None,
        instant=None,
        saved_snapshot=None,
        saved_plan_status="active",
        thermal_context_current=True,
        weather_rows=(),
        provider="open-meteo",
        input_hook=None,
        installation_get=None,
        corrupt_owner=False,
        hours=2,
        owner_start=None,
        learned_context=False,
    ):
        import datetime as dt
        import json
        from unittest.mock import AsyncMock

        import httpx
        from fastapi import FastAPI

        from packages.api.routers import models_router
        from packages.api.schemas import IndoorForecastResponse
        from packages.core import settings_service
        from packages.core.control_temperature import ControlTemperature
        from packages.core.models import DeviceStatusRecord, PlanRecord, WeatherRecord
        from packages.core.house_contract import (
            HousePlanningContract,
            _contract_hash,
            snapshot_from_revision,
        )
        from uuid import UUID
        from packages.ml.thermal import ThermalModel
        from tests.test_optimizer_api_contracts import _session_context

        now = instant or dt.datetime.now(dt.timezone.utc)
        if instant is not None:

            class FrozenDatetime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

            monkeypatch.setattr(
                models_router,
                "dt",
                SimpleNamespace(
                    datetime=FrozenDatetime, timedelta=dt.timedelta, timezone=dt.timezone
                ),
            )
        status = DeviceStatusRecord(ts=now, device_id="g12-device", outdoor_temp=5, zone1_temp=35)
        projection = (
            contract
            or HousePlanningContract.from_settings(
                {
                    "timezone": "UTC",
                    "comfort_schedule": '{"weekday":[],"weekend":[]}',
                    "comfort_temp_min": "20",
                    "comfort_temp_target": "20.5",
                    "comfort_temp_max": "23",
                },
                UUID(int=1),
                UUID(int=2),
            ).to_projection()
        )
        revision = SimpleNamespace(
            id=1,
            contract_hash=_contract_hash(projection),
            contract_json=projection,
            effective_at=now,
            source="fixture",
        )
        start = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        owner_start = owner_start or start
        owner_end = (
            dt.datetime.fromisoformat(saved_snapshot["forecast_with_plan"][-1]["ts"])
            if saved_snapshot and saved_snapshot.get("forecast_with_plan")
            else owner_start + dt.timedelta(hours=hours)
        )
        authority = await snapshot_from_revision(
            SimpleNamespace(get=AsyncMock(return_value=revision)), 1, owner_start, owner_end
        )
        if corrupt_owner:
            authority["schedule_projection"][0]["target_c"] += 1
        saved_plan = (
            SimpleNamespace(
                id=91,
                plan_json=json.dumps(
                    {"forecast_snapshot": saved_snapshot, "house_contract_snapshot": authority}
                ),
                created_at=now,
                horizon_start=owner_start,
                horizon_end=owner_end,
                contract_revision_id=1,
                contract_hash=revision.contract_hash,
                contract_state="current",
                status=saved_plan_status,
            )
            if saved_snapshot is not None
            else None
        )

        async def execute(statement):
            model = statement.column_descriptions[0]["entity"]
            if model is WeatherRecord:
                compiled = statement.compile()
                assert "weather.source =" in str(compiled)
                assert provider in compiled.params.values()
                rows = [row for row in weather_rows if row.source == provider]
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
            return SimpleNamespace(
                scalar_one_or_none=lambda: (
                    status
                    if model is DeviceStatusRecord
                    else saved_plan
                    if model is PlanRecord
                    else None
                ),
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

        floats = {"comfort_temp_min": 20.0, "comfort_temp_max": 23.0}
        model = ThermalModel()
        if learned_context:
            model.capture_dependency_context = lambda: ("qualified-learned-reference", 1)
        model.arefresh_if_changed = AsyncMock(return_value=False)
        model.dependency_context_is_current = AsyncMock(return_value=thermal_context_current)
        monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
        monkeypatch.setattr(
            models_router, "_read_live_forecast_quality", AsyncMock(return_value=quality)
        )
        monkeypatch.setattr(
            models_router,
            "_saved_plan_model_dependencies_are_current",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(models_router, "get_price_area", AsyncMock(return_value="SE3"))
        monkeypatch.setattr(
            models_router,
            "get_session",
            lambda: _session_context(
                SimpleNamespace(
                    execute=execute,
                    get=installation_get
                    or AsyncMock(
                        return_value=SimpleNamespace(
                            installation_id=UUID(int=1), indoor_dependency_generation=0
                        )
                    ),
                )
            ),
        )
        monkeypatch.setattr(
            "packages.core.house_contract.ensure_current_contract", AsyncMock(return_value=revision)
        )
        monkeypatch.setattr(
            "packages.core.control_temperature.get_control_temperature",
            input_hook
            or AsyncMock(
                return_value=ControlTemperature(
                    value=indoor_value,
                    confidence="high" if indoor_value is not None else "low",
                    sensor_count=1 if indoor_value is not None else 0,
                    sample_count=1,
                    latest_reading=now if indoor_value is not None else None,
                    reason=reason,
                )
            ),
        )
        monkeypatch.setattr(
            settings_service, "get_heat_curve_config", AsyncMock(return_value=HeatCurveConfig())
        )
        monkeypatch.setattr(
            settings_service, "get_float_setting", AsyncMock(side_effect=lambda key: floats[key])
        )
        monkeypatch.setattr(
            settings_service,
            "get_space_heating_gate_config",
            AsyncMock(return_value=HeatingGateConfig()),
        )
        monkeypatch.setattr(
            settings_service, "get_comfort_schedule", AsyncMock(return_value={"weekday": []})
        )
        monkeypatch.setattr(settings_service, "get_setting", AsyncMock(return_value=provider))
        monkeypatch.setattr(settings_service, "get_user_tz", AsyncMock(return_value="UTC"))
        monkeypatch.setattr(settings_service, "is_comfort_hour", lambda *a, **k: True)

        app = FastAPI()
        app.include_router(models_router.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://g12.invalid",
        ) as client:
            response = await client.get(f"/api/thermal/indoor-forecast?hours={hours}")

        assert response.status_code == 200
        payload = response.json()
        IndoorForecastResponse.model_validate(payload)
        return payload

    async def test_fresh_sensor_with_too_few_scored_forecasts(self, monkeypatch):
        payload = await self.call_api(
            monkeypatch,
            quality={"control_allowed": False, "reason": "insufficient_scored_forecasts"},
            indoor_value=21.8,
            reason="reference_sensor",
        )

        assert payload["current_indoor"] == 21.8
        assert payload["forecast_status"] == "fallback"
        assert payload["forecast_unavailable_reason"] is None
        assessment = payload["comfort_assessment"]
        assert assessment["state"] == "degraded"
        assert assessment["reason_code"] == "insufficient_scored_forecasts"
        assert "too few scored forecasts" in assessment["summary"].lower()
        assert "fresh trusted" not in assessment["summary"].lower()
        assert payload["forecast_quality"]["control_reason"] == "observe_only_build"
        assert payload["forecast_quality"]["control_allowed"] is False
        assert payload["forecast_quality"]["reason"] == "insufficient_scored_forecasts"

    async def test_qualified_forecast_is_reported_observe_only_without_sensor_claim(
        self, monkeypatch
    ):
        payload = await self.call_api(
            monkeypatch,
            quality={"control_allowed": True, "reason": "promotion_thresholds_met"},
            indoor_value=21.8,
        )

        assert payload["forecast_status"] == "available"
        assert "reason_code" not in payload["comfort_assessment"]
        assert "fresh trusted" not in payload["comfort_assessment"]["summary"].lower()
        assert payload["forecast_quality"]["control_reason"] == "observe_only_build"
        assert payload["forecast_quality"]["control_allowed"] is False

    async def test_missing_sensor_still_reports_sensor_reason(self, monkeypatch):
        payload = await self.call_api(
            monkeypatch,
            quality={"control_allowed": False, "reason": "insufficient_scored_forecasts"},
            indoor_value=None,
            reason="no_fresh_selected_sensor_readings",
        )

        assert payload["current_indoor"] is None
        assert payload["forecast_status"] == "unavailable"
        assert payload["forecast_unavailable_reason"] == "no_fresh_selected_sensor_readings"

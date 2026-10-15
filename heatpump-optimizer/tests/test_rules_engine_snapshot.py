"""Snapshot compatibility and room-comfort policy regression tests."""

import datetime as dt
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

import packages.optimizer.rules_engine as rules_engine
from packages.api.routers.models_router import (
    _build_active_plan_comfort_assessment,
    _forecast_status_from_quality,
    _plan_forecast_window,
    _safe_room_comfort_envelope,
)
from packages.core.comfort_assessment import build_comfort_assessment
from packages.core.control_temperature import (
    ControlTemperature,
    RoomComfortEnvelope,
    SensorTemperature,
    build_room_comfort_envelope,
    get_control_temperature,
)
from packages.core.heat_curve import HeatCurveConfig
from packages.core.space_heating_baseline import (
    BaselineDutyPoint,
    BaselineDutyProfile,
    build_baseline_duty_profile,
)
from packages.optimizer.rules_engine import RulesOptimizer, minimum_floor_protection_duties


class TestR09SavedForecastValidity:
    @staticmethod
    def _snapshot():
        start = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
        timestamp = (start + dt.timedelta(hours=1)).isoformat()
        return start, {
            "version": "indoor_forecast_v5",
            "forecast_status": "fallback",
            "current_indoor": 20.0,
            "forecast": [{"ts": timestamp, "predicted_indoor_temp": 20.0}],
            "forecast_with_plan": [{"ts": timestamp, "predicted_indoor_temp": 20.0}],
            "forecast_no_heating": [{"ts": timestamp, "predicted_indoor_temp": 20.0}],
            "target_schedule": [{"ts": timestamp, "target": 20.0}],
            "weather_forecast": [{"ts": timestamp, "outdoor_temp": 0.0}],
            "price_forecast": [{"ts": timestamp, "price_eur_per_kwh": -0.2, "price_per_kwh": -0.2}],
            "forecast_quality": {},
        }

    @pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
    @pytest.mark.parametrize(
        "field",
        [
            "forecast",
            "forecast_with_plan",
            "forecast_no_heating",
            "prediction_lower_c",
            "prediction_upper_c",
            "current_indoor",
            "target",
            "outdoor_temp",
            "price_eur_per_kwh",
            "price_per_kwh",
            "metadata",
        ],
    )
    def test_SC058_nonfinite_saved_windows_are_unusable(self, invalid, field):
        start, snapshot = self._snapshot()
        if field in {"forecast", "forecast_with_plan", "forecast_no_heating"}:
            snapshot[field][0]["predicted_indoor_temp"] = invalid
        elif field in {"prediction_lower_c", "prediction_upper_c"}:
            snapshot["forecast_with_plan"][0][field] = invalid
        elif field == "current_indoor":
            snapshot[field] = invalid
        elif field == "target":
            snapshot["target_schedule"][0][field] = invalid
        elif field == "outdoor_temp":
            snapshot["weather_forecast"][0][field] = invalid
        elif field == "metadata":
            snapshot["forecast_quality"]["mae"] = invalid
        else:
            snapshot["price_forecast"][0][field] = invalid

        assert _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1) is None

    @pytest.mark.parametrize("field", ["prediction", "target", "price"])
    def test_nonfinite_numeric_strings_are_not_valid_curve_numbers(self, field):
        start, snapshot = self._snapshot()
        if field == "prediction":
            snapshot["forecast_with_plan"][0]["predicted_indoor_temp"] = "NaN"
        elif field == "target":
            snapshot["target_schedule"][0]["target"] = "NaN"
        else:
            snapshot["price_forecast"][0]["price_eur_per_kwh"] = "NaN"

        assert _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1) is None

    def test_zero_outdoor_and_negative_prices_remain_valid(self):
        start, snapshot = self._snapshot()
        result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)
        assert result is not None
        assert result["weather_forecast"][0]["outdoor_temp"] == 0.0
        assert result["price_forecast"][0]["price_per_kwh"] == -0.2


@pytest.mark.parametrize("fault", ["weather", "price", "indoor"])
async def test_R09_live_numeric_fault_returns_valid_unavailable_api(monkeypatch, fault):
    import httpx
    from fastapi import FastAPI

    from packages.api.routers import models_router
    from packages.api.schemas import IndoorForecastResponse
    from packages.core.models import DeviceStatusRecord, PriceRecord, WeatherRecord
    from packages.ml.thermal import ThermalModel
    from tests.test_optimizer_api_contracts import _session_context

    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    status = DeviceStatusRecord(ts=now, device_id="s2-device", outdoor_temp=5, zone1_temp=35)
    weather = WeatherRecord(
        ts=target, source="open-meteo", temperature=float("nan") if fault == "weather" else 5
    )
    price = PriceRecord(
        ts=target, area="SE3", price_eur_per_kwh=float("inf") if fault == "price" else 0.2
    )

    async def execute(statement):
        model = statement.column_descriptions[0]["entity"]
        rows = [weather] if model is WeatherRecord else [price] if model is PriceRecord else []
        return SimpleNamespace(
            scalar_one_or_none=lambda: status if model is DeviceStatusRecord else None,
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )

    model = ThermalModel()
    model.arefresh_if_changed = AsyncMock(return_value=False)
    monkeypatch.setattr("packages.ml.thermal.thermal_model", model)
    monkeypatch.setattr(
        models_router,
        "_read_live_forecast_quality",
        AsyncMock(return_value={"status": "fallback", "control_allowed": False}),
    )
    monkeypatch.setattr(models_router, "get_price_area", AsyncMock(return_value="SE3"))
    monkeypatch.setattr(
        models_router, "get_session", lambda: _session_context(SimpleNamespace(execute=execute))
    )
    monkeypatch.setattr(
        "packages.core.control_temperature.get_control_temperature",
        AsyncMock(
            return_value=ControlTemperature(
                value=float("nan") if fault == "indoor" else 20,
                confidence="high",
                sensor_count=1,
                sample_count=1,
                latest_reading=now,
            )
        ),
    )
    app = FastAPI()
    app.include_router(models_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://s2.invalid",
    ) as client:
        response = await client.get("/api/thermal/indoor-forecast?hours=1")

    assert response.status_code == 200
    body = IndoorForecastResponse.model_validate(response.json())
    assert body.forecast_status == "unavailable"
    assert body.forecast_with_plan == body.forecast == body.forecast_no_heating == []
    assert body.forecast_quality["control_allowed"] is False
    assert "nonfinite" in body.forecast_unavailable_reason
    json.dumps(response.json(), allow_nan=False)


@pytest.mark.parametrize("version", ["indoor_forecast_v1", "indoor_forecast_v2"])
def test_legacy_v1_v2_snapshot_read(version):
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": version,
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [point],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": start.isoformat()}],
        "price_forecast": [{"ts": start.isoformat(), "price_eur_per_kwh": 0.1}],
    }

    result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

    assert result is not None
    assert result["forecast_quality"] == {"status": "legacy"}
    assert result["room_comfort"] == {"status": "legacy"}


def test_under_min_and_over_max_treats_floor_protection_as_a_soft_cap():
    duties = minimum_floor_protection_duties(
        basis_temperature=17.0,
        comfort_temp_min=18.0,
        indoor_rates=[(1.0, -0.5)],
    )
    from packages.core.config import settings
    from packages.core.space_heating_gate import SpaceHeatingGateState
    from packages.optimizer import InfeasibleError
    from packages.optimizer.milp import MILPOptimizer
    from tests.test_milp import _context

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    def solve(*, demand, rates, room):
        # Declared native SH: demand over the 12 kW declared electrical power.
        with patch.object(settings, "sh_max_power_kw", 12.0):
            return MILPOptimizer()._solve(
                [(start, 0.1)],
                [(start, 5.0)],
                cop_fn=lambda temperature, hour=12: 3.0,
                demand_per_hour=[demand],
                current_tank_temp=48.0,
                current_indoor_temp=room,
                indoor_targets=[18.0],
                tank_min_per_hour=[43.0],
                tank_max_temp_setting=55.0,
                indoor_rates=[rates],
                gate_states=[SpaceHeatingGateState.ALLOWED],
                room_overheat_active=[True],
                floor_protection_duty=duties,
                indoor_max_temp_setting=24.0,
                # A 48.2 C hardware ceiling admits no 5-minute pulse (+0.42 C), isolating
                # the native SH duty from any cost-driven DHW diversion.
                hardware_tank_ceiling=48.2,
                observation_context=_context(
                    [(start, 0.1)], device_id="synthetic-floor-device", demand=[demand]
                ),
            )

    assert duties == pytest.approx([1.0])
    # Floor protection is soft: the declared 1/3 native duty cannot lift 17 C to the 18 C
    # floor (17 - 0.5 + 1/3 * 1.5 = 17.0), so the represented plan is refused.
    with pytest.raises(InfeasibleError):
        solve(demand=4.0, rates=(1.0, -0.5), room=17.0)

    retained = solve(demand=4.0, rates=(1.0, -0.5), room=18.5)
    retained_point = retained["forecast_snapshot"]["forecast_with_plan"][0]
    assert retained["actions"] == []
    assert retained_point["native_space_heating_fraction"] == pytest.approx(1 / 3)
    assert 0.0 < retained_point["space_heating_fraction"] <= duties[0]
    assert retained_point["space_heating_fraction"] == pytest.approx(1 / 3)
    assert retained_point["predicted_indoor_temp"] == pytest.approx(18.5)

    recovered = solve(demand=12.0, rates=(1.2, -0.1), room=17.0)
    recovered_point = recovered["forecast_snapshot"]["forecast_with_plan"][0]
    assert recovered["actions"] == []
    assert recovered_point["space_heating_fraction"] == pytest.approx(1.0)
    assert recovered_point["predicted_indoor_temp"] == pytest.approx(18.2)


def test_room_overheat_suppresses_discretionary_heat():
    duties = minimum_floor_protection_duties(
        basis_temperature=21.0,
        comfort_temp_min=18.0,
        indoor_rates=[(1.0, -0.2)],
    )

    assert duties == [0.0]


def test_overheat_veto_preserves_minimum_floor_exception():
    duties = minimum_floor_protection_duties(
        basis_temperature=17.0,
        comfort_temp_min=18.0,
        indoor_rates=[(1.0, -0.5)],
    )
    assessment = build_comfort_assessment(
        forecast=[],
        targets=[],
        weather=[],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        room_envelope=RoomComfortEnvelope(17.0, 17.0, 23.0, ("bedroom",)),
        comfort_temp_min=18.0,
    )

    assert duties == [1.0]
    assert assessment["state"] == "conflict"
    assert assessment["min_comfort_active"] is True


def test_failed_scorecard_uses_fallback_and_never_on_target():
    snapshot = RulesOptimizer._build_forecast_snapshot(
        prices=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 0.1)],
        weather=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 5.0)],
        weather_full=[],
        actions=[],
        horizon_start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        current_indoor=20.0,
        current_water_temp=35.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule={},
        comfort_temp_target=20.0,
        comfort_temp_min=18.0,
        tz_name="UTC",
        quality_gate={"status": "fallback", "control_allowed": False, "reason": "failed"},
    )
    assessment = build_comfort_assessment(
        forecast=snapshot["forecast"],
        targets=snapshot["target_schedule"],
        weather=snapshot["weather_forecast"],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        forecast_status=snapshot["forecast_status"],
    )

    assert snapshot["forecast_quality"]["status"] == "fallback"
    assert snapshot["forecast_status"] == "fallback"
    assert assessment["state"] == "degraded"
    assert assessment["state"] != "on_target"


def test_stored_fallback_snapshot_remains_fallback_and_degraded():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": "indoor_forecast_v3",
        "forecast_status": "fallback",
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [point],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": start.isoformat()}],
        "price_forecast": [{"ts": start.isoformat(), "price_eur_per_kwh": 0.1}],
    }

    result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)
    assessment = build_comfort_assessment(
        forecast=result["forecast_with_plan"],
        targets=result["target_schedule"],
        weather=result["weather_forecast"],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        forecast_status=result["forecast_status"],
    )

    assert result is not None
    assert result["forecast_status"] == "legacy"
    assert assessment["state"] == "unavailable"
    assert assessment["state"] != "on_target"


def test_live_gate_denial_is_fallback_and_never_on_target():
    status = _forecast_status_from_quality({"status": "fallback", "control_allowed": False})
    assessment = build_comfort_assessment(
        forecast=[],
        targets=[],
        weather=[],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        forecast_status=status,
    )

    assert status == "fallback"
    assert assessment["state"] == "degraded"
    assert assessment["state"] != "on_target"


def test_legacy_snapshot_returns_legacy_status():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": "indoor_forecast_v2",
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [point],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": start.isoformat()}],
        "price_forecast": [{"ts": start.isoformat(), "price_eur_per_kwh": 0.1}],
    }

    result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

    assert result is not None
    assert result["forecast_status"] == "legacy"


def test_rules_snapshot_uses_plan_start_reference_sensor_observation():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snapshot = RulesOptimizer._build_forecast_snapshot(
        prices=[(start, 0.1)],
        weather=[(start, 5.0)],
        weather_full=[],
        actions=[],
        horizon_start=start,
        current_indoor=23.8,
        current_water_temp=35.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule={},
        comfort_temp_target=20.0,
        comfort_temp_min=18.0,
        tz_name="UTC",
        control_input={
            "available": True,
            "reference_sensor_id": "reference",
            "reference_sensor_label": "Reference room",
        },
    )

    assert snapshot["version"] == "indoor_forecast_v5"
    assert snapshot["observed_history"][0] == {
        "hour": 0,
        "ts": start.isoformat(),
        "temperature": 23.8,
    }
    assert snapshot["sensor_basis"] == {"label": "Reference room", "kind": "reference"}


def test_rules_snapshot_uses_target_timestamp_weather():
    start = dt.datetime(2026, 1, 1, 18, tzinfo=dt.timezone.utc)
    snapshot = RulesOptimizer._build_forecast_snapshot(
        prices=[(start, 0.1)],
        weather=[(start, 1.0)],
        weather_full=[
            {"ts": start, "temperature": 1.0},
            {"ts": start + dt.timedelta(hours=1), "temperature": 9.0},
        ],
        actions=[],
        horizon_start=start,
        current_indoor=20.0,
        current_water_temp=35.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule={},
        comfort_temp_target=20.0,
        comfort_temp_min=18.0,
        tz_name="UTC",
    )

    assert snapshot["weather_forecast"][0]["ts"] == (start + dt.timedelta(hours=1)).isoformat()
    assert snapshot["weather_forecast"][0]["outdoor_temp"] == 9.0
    assert snapshot["target_schedule"][0]["ts"] == (start + dt.timedelta(hours=1)).isoformat()


@pytest.mark.parametrize(("source", "fraction"), [("history", 0.8), ("default", 0.35)])
def test_allowed_snapshot_uses_history_or_default_baseline(source, fraction):
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    profile = BaselineDutyProfile(
        (BaselineDutyPoint(start + dt.timedelta(hours=1), fraction, 0.0, source, "none", None, 0),),
        0,
        0,
        False,
    )
    with patch.object(
        rules_engine.thermal_model,
        "predict_indoor_controlled_curve",
        return_value=[{"hour": 1, "predicted_indoor_temp": 20.0, "source": "linear"}],
    ):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            baseline_profile=profile,
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            effective_baseline_mode="on",
            live_baseline_fractions=[fraction],
        )

    point = snapshot["forecast_with_plan"][0]
    assert point["baseline_heating_fraction"] == fraction
    assert point["baseline_heating_source"] == source
    assert point["space_heating_source"] == "baseline"
    assert point["space_heating_fraction"] == fraction


def test_snapshot_tracks_explicit_heating_source_per_hour_after_restore():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    baseline_fractions = [0.4, 1.0, 0.6, 0.8, 0.3, 0.7]
    profile = BaselineDutyProfile(
        tuple(
            BaselineDutyPoint(
                start + dt.timedelta(hours=index + 1),
                fraction,
                0.0,
                "history",
                "none",
                "band_3h",
                20,
            )
            for index, fraction in enumerate(baseline_fractions)
        ),
        20,
        20,
        False,
    )

    def predict(**kwargs):
        return [
            {"hour": index + 1, "predicted_indoor_temp": 20.0, "source": "linear"}
            for index in range(len(kwargs["heating_fractions"]))
        ]

    with patch.object(
        rules_engine.thermal_model, "predict_indoor_controlled_curve", side_effect=predict
    ):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(start + dt.timedelta(hours=index), 0.1) for index in range(6)],
            weather=[(start + dt.timedelta(hours=index), 5.0) for index in range(6)],
            weather_full=[],
            actions=[
                {"type": "zone_temp_boost", "ts": (start + dt.timedelta(hours=2)).isoformat()},
                {"type": "zone_temp_restore", "ts": (start + dt.timedelta(hours=3)).isoformat()},
            ],
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            baseline_profile=profile,
            gate_projections=[SimpleNamespace(state="ALLOWED") for _ in baseline_fractions],
            effective_baseline_mode="on",
            live_baseline_fractions=baseline_fractions,
        )

    points = snapshot["forecast_with_plan"]
    assert [point["space_heating_fraction"] for point in points] == [0.4, 1.0, 1.0, 0.8, 0.3, 0.7]
    assert [point["space_heating_source"] for point in points] == [
        "baseline",
        "baseline",
        "explicit_override",
        "baseline",
        "baseline",
        "baseline",
    ]


def test_bug5_allowed_history_baseline_changes_plan_forecast_from_no_heating():
    start = dt.datetime(2026, 1, 1, 21, tzinfo=dt.timezone.utc)
    profile = BaselineDutyProfile(
        (
            BaselineDutyPoint(
                start + dt.timedelta(hours=1), 1.0, 1.0, "history", "history_p10", "band_3h", 20
            ),
        ),
        20,
        20,
        False,
    )

    def predict(**kwargs):
        fraction = kwargs["heating_fractions"][0]
        return [{"hour": 1, "predicted_indoor_temp": 19.0 + fraction, "source": "linear"}]

    with patch.object(
        rules_engine.thermal_model, "predict_indoor_controlled_curve", side_effect=predict
    ):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 12.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=19.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            baseline_profile=profile,
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            effective_baseline_mode="on",
            live_baseline_fractions=[1.0],
        )

    planned = snapshot["forecast_with_plan"][0]
    no_heating = snapshot["forecast_no_heating"][0]
    assert planned["space_heating_fraction"] > 0.0
    assert planned["predicted_indoor_temp"] != no_heating["predicted_indoor_temp"]


def test_bug5_replay_history_baseline_is_live_only_in_on_mode():
    start = dt.datetime(2026, 2, 1, 21, tzinfo=dt.timezone.utc)
    history: list[SimpleNamespace] = []
    for day in range(20):
        for minute in range(0, 61, 12):
            history.append(
                SimpleNamespace(
                    ts=start + dt.timedelta(days=day - 20, hours=1, minutes=minute),
                    outdoor_temp=12.0,
                    heat_pump_outdoor_temp=None,
                    operation_status=1,
                    mode="1",
                    zone1_operation_status=1,
                    holiday_mode=0,
                    direction="PUMP",
                    pump_duty=1,
                    device_action="HEATING",
                    defrost_active=False,
                )
            )
    profile = build_baseline_duty_profile(
        history,
        [SimpleNamespace(timestamp=start + dt.timedelta(hours=1), outdoor_temp=12.0)],
        ["ALLOWED"],
        13.0,
        "UTC",
        True,
        0.35,
    )

    def predict(**kwargs):
        fraction = kwargs["heating_fractions"][0]
        return [{"hour": 1, "predicted_indoor_temp": 19.0 + fraction, "source": "linear"}]

    def snapshot(mode: str, live_fractions: list[float]):
        return RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 12.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=19.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13.0),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            baseline_profile=profile,
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            effective_baseline_mode=mode,
            live_baseline_fractions=live_fractions,
        )

    with patch.object(
        rules_engine.thermal_model, "predict_indoor_controlled_curve", side_effect=predict
    ):
        on = snapshot("on", [profile.points[0].expected_fraction])
        shadow = snapshot("shadow", [0.0])

    assert profile.points[0].expected_fraction > 0.0
    assert on["forecast_with_plan"][0]["space_heating_fraction"] > 0.0
    assert (
        on["forecast_with_plan"][0]["predicted_indoor_temp"]
        != on["forecast_no_heating"][0]["predicted_indoor_temp"]
    )
    assert shadow["forecast_with_plan"][0]["space_heating_fraction"] == 0.0
    assert (
        shadow["forecast_with_plan"][0]["predicted_indoor_temp"]
        == shadow["forecast_no_heating"][0]["predicted_indoor_temp"]
    )
    assert (
        shadow["forecast_with_plan_baseline"][0]["predicted_indoor_temp"]
        != shadow["forecast_with_plan"][0]["predicted_indoor_temp"]
    )


def test_shadow_baseline_does_not_change_live_curve_or_actions():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    profile = BaselineDutyProfile(
        (
            BaselineDutyPoint(
                start + dt.timedelta(hours=1), 0.8, 0.7, "history", "history_p10", "band_3h", 20
            ),
        ),
        20,
        20,
        False,
    )

    def predict(**kwargs):
        return [
            {
                "hour": 1,
                "predicted_indoor_temp": 20.0 + kwargs["heating_fractions"][0],
                "source": "linear",
            }
        ]

    with patch.object(
        rules_engine.thermal_model, "predict_indoor_controlled_curve", side_effect=predict
    ):
        shadow = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            baseline_profile=profile,
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            effective_baseline_mode="shadow",
            live_baseline_fractions=[0.0],
            learning_mode_active=True,
        )
        off = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            effective_baseline_mode="off",
            live_baseline_fractions=[0.0],
        )

    assert shadow["forecast_with_plan"][0]["space_heating_fraction"] == 0.0
    assert (
        shadow["forecast_with_plan"][0]["predicted_indoor_temp"]
        == off["forecast_with_plan"][0]["predicted_indoor_temp"]
    )
    assert (
        shadow["forecast_with_plan_baseline"][0]["predicted_indoor_temp"]
        != shadow["forecast_with_plan"][0]["predicted_indoor_temp"]
    )
    assert shadow["baseline_evaluation"]["learning_mode"] is True
    assert shadow["baseline_evaluation"] == {"learning_mode": True, "eligible": True}


@pytest.mark.asyncio
async def test_learning_mode_resolution_fails_closed(monkeypatch):
    async def unavailable():
        raise RuntimeError("settings unavailable")

    from packages.optimizer import executor_core
    from packages.optimizer.executor_core import LearningModeState

    monkeypatch.setattr(executor_core, "resolve_learning_mode_state", unavailable)

    assert await rules_engine._resolve_learning_mode() is LearningModeState.UNKNOWN


@pytest.mark.asyncio
async def test_unknown_learning_state_skips_rules_plan_before_database_reads(monkeypatch):
    from packages.optimizer.executor_core import LearningModeState

    monkeypatch.setattr(
        rules_engine,
        "_resolve_learning_mode",
        AsyncMock(return_value=LearningModeState.UNKNOWN),
    )
    get_session = MagicMock()
    monkeypatch.setattr(rules_engine, "get_session", get_session)

    assert await RulesOptimizer().generate_plan() is None
    get_session.assert_not_called()


@pytest.mark.parametrize(
    ("learning_mode_state", "expected"),
    [
        ("inactive", {"learning_mode": False, "eligible": False}),
        ("active", {"learning_mode": True, "eligible": True}),
        ("unknown", {"learning_mode": False, "eligible": False}),
    ],
)
def test_snapshot_baseline_evaluation_uses_resolved_learning_mode(learning_mode_state, expected):
    from packages.optimizer.executor_core import LearningModeState

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    state = LearningModeState(learning_mode_state)
    snapshot = RulesOptimizer._build_forecast_snapshot(
        prices=[(start, 0.1)],
        weather=[(start, 5.0)],
        weather_full=[],
        actions=[],
        horizon_start=start,
        current_indoor=20.0,
        current_water_temp=35.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule={},
        comfort_temp_target=20.0,
        comfort_temp_min=18.0,
        tz_name="UTC",
        effective_baseline_mode="shadow",
        learning_mode_active=state is LearningModeState.ACTIVE,
    )

    assert snapshot["baseline_evaluation"] == expected


def test_private_baseline_curves_are_not_returned_by_plan_forecast_api():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": "indoor_forecast_v5",
        "forecast_status": "fallback",
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [
            {
                **point,
                "baseline_heating_fraction": 0.35,
                "baseline_heating_source": "default",
                "space_heating_source": "baseline",
            }
        ],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": point["ts"], "outdoor_temp": 5.0}],
        "price_forecast": [{"ts": point["ts"], "price_eur_per_kwh": 0.1}],
        "forecast_with_plan_baseline": [point],
        "forecast_with_plan_zero_baseline": [point],
        "baseline_evaluation": {"eligible": True},
    }

    response = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

    assert response is not None
    assert response["forecast_with_plan"][0]["baseline_heating_source"] == "default"
    assert "forecast_with_plan_baseline" not in response
    assert "forecast_with_plan_zero_baseline" not in response
    assert "baseline_evaluation" not in response


@pytest.mark.parametrize("version", ["indoor_forecast_v4", "indoor_forecast_v5"])
def test_v5_snapshot_round_trips_plan_forecast_window(version):
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": version,
        "forecast_status": "fallback",
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [point],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": point["ts"], "outdoor_temp": 5.0}],
        "price_forecast": [{"ts": point["ts"], "price_eur_per_kwh": 0.1}],
    }

    result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

    assert result is not None
    assert result["forecast_with_plan"][0]["predicted_indoor_temp"] == 20.0
    assert result["forecast_status"] == "fallback"


def test_v5_snapshot_is_nonlegacy_and_exposes_source_metadata():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    point = {"ts": (start + dt.timedelta(hours=1)).isoformat(), "predicted_indoor_temp": 20.0}
    snapshot = {
        "version": "indoor_forecast_v5",
        "forecast_status": "available",
        "current_indoor": 20.0,
        "forecast": [point],
        "forecast_with_plan": [
            {
                **point,
                "baseline_heating_fraction": 0.8,
                "baseline_heating_source": "history",
                "space_heating_source": "baseline",
            }
        ],
        "forecast_no_heating": [point],
        "target_schedule": [{"ts": point["ts"], "target": 20.0}],
        "weather_forecast": [{"ts": point["ts"], "outdoor_temp": 5.0}],
        "price_forecast": [{"ts": point["ts"], "price_eur_per_kwh": 0.1}],
        "space_heating_baseline": {"effective_mode": "on", "live_baseline_applied": True},
    }

    result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

    assert result is not None
    assert result["forecast_status"] == "available"
    assert result["space_heating_baseline"]["live_baseline_applied"] is True
    assert result["forecast_with_plan"][0]["space_heating_source"] == "baseline"


def test_observing_plan_persists_scoreable_shadow_without_changing_live_forecast_or_actions():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    live = [{"hour": 1, "predicted_indoor_temp": 19.8, "source": "linear_controlled"}]
    shadow = [
        {
            "hour": 1,
            "predicted_indoor_temp": 20.2,
            "source": "comfort_model_controlled",
            "segment_kind": "direct",
            "space_heating_fraction": 0.0,
        }
    ]
    with (
        patch.object(
            rules_engine.thermal_model, "predict_indoor_controlled_curve", return_value=live
        ),
        patch.object(
            rules_engine.thermal_model, "predict_indoor_candidate_curve", return_value=shadow
        ),
    ):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            quality_gate={"status": "observing", "control_allowed": False},
        )

    assert snapshot["forecast_with_plan"][0]["model_source"] == "linear_controlled"
    assert snapshot["shadow_forecast_with_plan"][0] == {
        "hour": 1,
        "ts": (start + dt.timedelta(hours=1)).isoformat(),
        "predicted_indoor_temp": 20.2,
        "source": "rules_shadow_candidate",
        "model_source": "comfort_model_controlled",
        "segment_kind": "direct",
        "space_heating_fraction": 0.0,
    }
    response = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)
    assert response is not None
    assert "shadow_forecast_with_plan" not in response


def test_controlled_shadow_survives_without_passive_artifacts():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    comfort = MagicMock(
        is_ready_for_control=True,
        direct_forecast_horizons_minutes=(60, 180, 360, 720),
        _passive_direct_models={},
    )
    comfort.predict_indoor_temp.return_value = 20.4
    actions = [
        {
            "type": "zone_temp_boost",
            "ts": start.isoformat(),
            "payload": {"offset": 2.0},
        }
    ]

    with patch("packages.ml.comfort_model.comfort_model", comfort):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(start, 0.1)],
            weather=[(start, 5.0)],
            weather_full=[],
            actions=actions,
            horizon_start=start,
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            gate_projections=[SimpleNamespace(state="ALLOWED")],
            quality_gate={"status": "observing", "control_allowed": False},
        )

    assert snapshot.get("shadow_forecast_with_plan")
    assert snapshot.get("shadow_forecast_no_heating") is None
    from packages.ml.forecast_quality import _learned_candidate

    assert (
        _learned_candidate(snapshot["forecast_with_plan"], snapshot["shadow_forecast_with_plan"])
        == snapshot["shadow_forecast_with_plan"]
    )


def test_shadow_keys_are_absent_from_decision_executor_api_and_web_sources():
    root = Path(__file__).resolve().parents[1]
    approved = {
        root / "packages" / "optimizer" / "rules_engine.py",
        root / "packages" / "ml" / "forecast_quality.py",
    }
    scanned = [
        *root.glob("packages/**/*.py"),
        *root.glob("web/**/*.ts"),
        *root.glob("web/**/*.tsx"),
    ]
    private_snapshot_keys = {
        "shadow_forecast_",
        "forecast_with_plan_baseline",
        "forecast_with_plan_zero_baseline",
        "baseline_evaluation",
    }
    offenders = [
        path.relative_to(root).as_posix()
        for path in scanned
        if path not in approved
        and any(key in path.read_text(encoding="utf-8") for key in private_snapshot_keys)
    ]

    assert offenders == []


def test_active_plan_envelope_reports_overheat_and_minimum_comfort_conflict():
    envelope = RoomComfortEnvelope(19.0, 19.0, 23.0, ("bedroom",))
    assessment = _build_active_plan_comfort_assessment(
        plan_snapshot={
            "forecast_status": "available",
            "forecast_with_plan": [],
            "target_schedule": [],
            "weather_forecast": [],
        },
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        gate_projections=[],
        room_envelope=envelope,
        comfort_temp_min=20.0,
    )

    assert assessment["state"] == "conflict"
    assert assessment["reason_code"] == "comfort_conflict_min_priority"


def test_room_envelope_failure_has_no_overheat_claim():
    with patch(
        "packages.core.control_temperature.build_room_comfort_envelope",
        side_effect=RuntimeError("private sensor details"),
    ):
        envelope = _safe_room_comfort_envelope(SimpleNamespace(), comfort_temp_max=22.0)
    assessment = _build_active_plan_comfort_assessment(
        plan_snapshot={
            "forecast_status": "available",
            "forecast_with_plan": [],
            "target_schedule": [],
            "weather_forecast": [],
        },
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        gate_projections=[],
        room_envelope=envelope,
        comfort_temp_min=20.0,
    )

    assert envelope is None
    assert assessment.get("room_overheat_active") is False
    assert assessment["state"] == "on_target"


def test_rules_gate_passed_uses_learned_forecast():
    predict = MagicMock(
        return_value=[{"hour": 1, "predicted_indoor_temp": 20.1, "source": "learned"}]
    )
    with patch(
        "packages.optimizer.rules_engine.thermal_model.predict_indoor_controlled_curve", predict
    ):
        RulesOptimizer._build_forecast_snapshot(
            prices=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 0.1)],
            weather=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            quality_gate={"status": "allowed", "control_allowed": True},
        )

    assert all(call.kwargs["use_learned_forecast"] is True for call in predict.call_args_list)


def test_rules_snapshot_passes_passive_change_limit_to_live_and_shadow_curves():
    live = [{"hour": 1, "predicted_indoor_temp": 20.0, "source": "linear"}]
    with (
        patch.object(
            rules_engine.thermal_model, "predict_indoor_controlled_curve", return_value=live
        ) as live_predict,
        patch.object(
            rules_engine.thermal_model, "predict_indoor_candidate_curve", return_value=None
        ) as shadow_predict,
    ):
        RulesOptimizer._build_forecast_snapshot(
            prices=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 0.1)],
            weather=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            quality_gate={"status": "observing", "control_allowed": False},
            max_passive_change_c_per_hour=0.2,
        )

    assert all(
        call.kwargs["max_passive_change_c_per_hour"] == 0.2
        for call in live_predict.call_args_list + shadow_predict.call_args_list
    )


def test_rules_observing_gate_uses_linear_fallback_and_never_permits_control():
    predict = MagicMock(
        return_value=[{"hour": 1, "predicted_indoor_temp": 20.1, "source": "linear"}]
    )
    with patch(
        "packages.optimizer.rules_engine.thermal_model.predict_indoor_controlled_curve", predict
    ):
        snapshot = RulesOptimizer._build_forecast_snapshot(
            prices=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 0.1)],
            weather=[(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 5.0)],
            weather_full=[],
            actions=[],
            horizon_start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={},
            comfort_temp_target=20.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
            quality_gate={"status": "observing", "control_allowed": False},
        )

    assert snapshot["forecast_status"] == "fallback"
    assert snapshot["forecast_quality"]["status"] == "observing"
    assert all(call.kwargs["use_learned_forecast"] is False for call in predict.call_args_list)


@pytest.mark.asyncio
async def test_rules_gate_error_falls_back():
    from packages.ml.forecast_quality import evaluate_live_control_gate

    gate = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=MagicMock(),
        scorecard_loader=AsyncMock(side_effect=RuntimeError()),
    )

    assert gate["status"] == "fallback"
    assert gate["control_allowed"] is False


async def _represented_policy_setting(name):
    """Only the freshness policy keys the represented observation context reads."""
    return {"device_status_max_age_minutes": "15", "poll_interval_seconds": "300"}[name]


def _no_database(*_args, **_kwargs):
    raise AssertionError("unexpected database session")


def _isolated_represented_inputs(stack, optimizer, horizon):
    """Price/weather rows aligned to the generator's own horizon; no real DB sessions."""

    async def prices(_session, start, _end):
        horizon["start"] = start
        if "status" in horizon:
            from packages.core.config import settings
            from tests.test_milp import _context

            status = horizon["status"]
            declared = _context(
                [(start, 0.1)],
                device_id=status.device_id,
                demand=[0],
                power=settings.sh_max_power_kw,
            )["native_sh_provenance"]
            declared["captured_at"] = status.ts.isoformat()
            status.native_policy_provenance = {
                "diagnostic_assumptions": {
                    "native_sh_provenance": declared,
                    "auxiliary_power_kw": 0,
                }
            }
        return [(start, 0.1)]

    async def weather(_session, start, _end):
        return [(start, 5.0)]

    stack.enter_context(patch.object(optimizer, "_get_prices", new=AsyncMock(side_effect=prices)))
    stack.enter_context(patch.object(optimizer, "_get_weather", new=AsyncMock(side_effect=weather)))
    stack.enter_context(
        patch(
            "packages.core.settings_service.get_setting",
            new=AsyncMock(side_effect=_represented_policy_setting),
        )
    )
    # Committed thermal authority lives in the DB; keep the uncalibrated defaults instead.
    stack.enter_context(
        patch.object(
            rules_engine.thermal_model, "arefresh_if_changed", new=AsyncMock(return_value=False)
        )
    )
    return [
        stack.enter_context(
            patch("packages.core.settings_service.get_session", side_effect=_no_database)
        ),
        stack.enter_context(patch("packages.core.database.get_session", side_effect=_no_database)),
    ]


def _peak_candidate(horizon):
    return lambda *_args, **_kwargs: [
        {"type": "peak_avoidance", "ts": horizon["start"].isoformat(), "payload": {}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_target", ["control_temperature", "room_envelope"])
async def test_rules_room_evidence_failure_keeps_non_comfort_actions_and_private_logs(
    failure_target,
):
    from packages.optimizer.executor_core import LearningModeState

    observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    horizon = {}
    status = SimpleNamespace(
        ts=observed_at,
        force_dhw=0,
        tank_heat_max=65.0,
        tank_heat_min=30.0,
        device_id="device-1",
        holiday_mode=0,
        tank_temp=48.0,
        zone1_temp=35.0,
        zone1_target_temp=None,
        zone1_heat_min=None,
        zone1_heat_max=None,
        tank_target_temp=52.0,
        special_status_supported=False,
        special_status=None,
        quiet_mode=None,
        heat_pump_outdoor_temp=None,
        outdoor_temp=5.0,
    )
    horizon["status"] = status
    session = MagicMock()
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=query_result)

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    gate = SimpleNamespace(
        state="ALLOWED",
        reason_code="test",
        profile_id=None,
        base_c=5.0,
        on_threshold_c=4.0,
        off_threshold_c=6.0,
        last_raw_outdoor_c=5.0,
        fingerprint_matches=True,
    )
    projection = SimpleNamespace(state="ALLOWED")
    optimizer = RulesOptimizer()

    async def setting_float(name):
        return {
            "learned_schedule_threshold": 0.2,
            "comfort_temp_target": 20.0,
            "comfort_temp_min": 18.0,
            "comfort_temp_max": 25.0,
            "tank_min_temp": 45.0,
            "tank_min_temp_offpeak": 40.0,
            "tank_max_temp": 55.0,
        }[name]

    async def setting_int(name):
        return {"quiet_mode_start": 22, "quiet_mode_end": 7}.get(name, 10)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                rules_engine,
                "_resolve_learning_mode",
                new=AsyncMock(return_value=LearningModeState.INACTIVE),
            )
        )
        stack.enter_context(
            patch.object(rules_engine, "get_session", return_value=SessionContext())
        )
        database_sentinels = _isolated_represented_inputs(stack, optimizer, horizon)
        stack.enter_context(
            patch.object(optimizer, "_get_weather_full", new=AsyncMock(return_value=[]))
        )
        stack.enter_context(
            patch.object(optimizer, "_get_last_status", new=AsyncMock(return_value=status))
        )
        stack.enter_context(
            patch.object(rules_engine.thermal_model, "calibrate", new=AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "resolve_outdoor_temperature",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        effective_c=5.0,
                        heat_pump_c=None,
                        weather_c=5.0,
                        source="weather",
                        weather_provider="test",
                        compensation_c=0.0,
                        fallback_reason=None,
                    )
                ),
            )
        )
        if failure_target == "control_temperature":
            stack.enter_context(
                patch.object(
                    rules_engine,
                    "get_control_temperature",
                    new=AsyncMock(side_effect=RuntimeError("sensor details must not be logged")),
                )
            )
        else:
            stack.enter_context(
                patch.object(
                    rules_engine,
                    "get_control_temperature",
                    new=AsyncMock(
                        return_value=ControlTemperature(
                            value=23.8,
                            confidence="high",
                            sensor_count=1,
                            sample_count=1,
                            latest_reading=observed_at,
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    rules_engine,
                    "build_room_comfort_envelope",
                    side_effect=RuntimeError("sensor details must not be logged"),
                )
            )
        stack.enter_context(
            patch.object(
                rules_engine, "get_heat_curve_config", new=AsyncMock(return_value=HeatCurveConfig())
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine, "get_space_heating_gate_config", new=AsyncMock(return_value={})
            )
        )
        stack.enter_context(patch.object(rules_engine, "resolve_effective_gate", return_value=gate))
        stack.enter_context(
            patch.object(rules_engine, "project_gate_states", return_value=[projection])
        )
        stack.enter_context(
            patch.object(rules_engine, "get_effective_schedule", new=AsyncMock(return_value={}))
        )
        stack.enter_context(
            patch.object(rules_engine, "get_user_tz", new=AsyncMock(return_value="UTC"))
        )
        stack.enter_context(
            patch.object(rules_engine, "get_float_setting", side_effect=setting_float)
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "get_setting",
                new=AsyncMock(
                    side_effect=lambda name: {"space_heating_baseline_mode": "off"}[name]
                ),
            )
        )
        stack.enter_context(patch.object(rules_engine, "get_int_setting", side_effect=setting_int))
        stack.enter_context(
            patch.object(rules_engine, "panasonic_tank_heating_available", return_value=False)
        )
        stack.enter_context(
            patch.object(rules_engine, "panasonic_zone_heating_available", return_value=False)
        )
        stack.enter_context(
            patch.object(optimizer, "_plan_peak_avoidance", side_effect=_peak_candidate(horizon))
        )
        stack.enter_context(
            patch.object(optimizer, "_estimate_cost", new=AsyncMock(return_value=0.0))
        )
        forecast_snapshot_builder = stack.enter_context(
            patch.object(
                RulesOptimizer,
                "_build_forecast_snapshot",
                wraps=RulesOptimizer._build_forecast_snapshot,
            )
        )
        quality_gate = stack.enter_context(
            patch.object(
                rules_engine,
                "evaluate_live_control_gate",
                new=AsyncMock(
                    return_value={"status": "fallback", "control_allowed": False, "reason": "test"}
                ),
            )
        )
        warning = stack.enter_context(patch.object(rules_engine.logger, "warning"))
        plan = await optimizer.generate_plan()

    assert all(not sentinel.called for sentinel in database_sentinels)
    assert plan is not None
    # The unsupported candidate is retained only as unproven metadata, never certified.
    certified = plan["actions"] + plan["diagnostic_strategy_actions"]
    assert all(item["type"] in {"force_dhw_on", "force_dhw_off"} for item in certified)
    assert plan["unproven_strategies"]["schema"] == "native_sh_trace_v1"
    assert plan["unproven_strategies"]["status"] == "unproven"
    unproven_types = [item["type"] for item in plan["unproven_strategies"]["actions"]]
    assert unproven_types.count("peak_avoidance") == 1
    if failure_target == "control_temperature":
        assert plan["diagnostic_status"] == "unproven"
        assert plan["reason"] == "actual_initial_indoor_state_missing"
        assert plan["actions"] == plan["diagnostic_strategy_actions"] == []
        assert plan["cost_estimate"] is None
        assert plan["control_input"]["indoor_temp"] is None
        assert plan["forecast_snapshot"]["forecast_with_plan"] == []
        assert "represented_strategy" not in plan
    else:
        assert plan["diagnostic_status"] == "conditional_feasible"
    assert plan["forecast_snapshot"]["control_input"]["room_comfort"]["affected_rooms"] == []
    assert plan["forecast_snapshot"]["forecast_status"] == (
        "unavailable" if failure_target == "control_temperature" else "fallback"
    )
    quality_gate.assert_awaited_once()
    assert forecast_snapshot_builder.call_args.kwargs["quality_gate"]["status"] == "fallback"
    warning_text = repr(warning.call_args_list)
    assert "sensor details must not be logged" not in warning_text
    assert "device-1" not in warning_text
    assert "23.8" not in warning_text
    assert "26.1" not in warning_text


async def _generate_with_passive_policy(limit, curve, *, gate=None):
    """Run the rules generator; ``curve=None`` keeps the real controlled curve."""
    observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    horizon = {}
    optimizer = RulesOptimizer()
    status = SimpleNamespace(
        ts=observed_at,
        force_dhw=0,
        tank_heat_max=65.0,
        tank_heat_min=30.0,
        device_id="device-1",
        holiday_mode=0,
        tank_temp=48.0,
        zone1_temp=35.0,
        zone1_target_temp=34.0,
        zone1_heat_min=20,
        zone1_heat_max=65,
        tank_target_temp=52.0,
        special_status_supported=True,
        special_status=0,
        quiet_mode=None,
        heat_pump_outdoor_temp=None,
        outdoor_temp=5.0,
    )
    horizon["status"] = status
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    async def setting_float(name):
        return {
            "learned_schedule_threshold": 0.2,
            "comfort_temp_target": 20.5,
            "comfort_temp_min": 18.0,
            "comfort_temp_max": 22.0,
            "indoor_forecast_max_passive_change_c_per_hour": limit,
            "tank_min_temp": 45.0,
            "tank_min_temp_offpeak": 40.0,
            "tank_max_temp": 55.0,
        }[name]

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(rules_engine, "get_session", return_value=SessionContext())
        )
        from packages.optimizer.executor_core import LearningModeState

        stack.enter_context(
            patch.object(
                rules_engine,
                "_resolve_learning_mode",
                new=AsyncMock(return_value=LearningModeState.INACTIVE),
            )
        )
        database_sentinels = _isolated_represented_inputs(stack, optimizer, horizon)
        stack.enter_context(
            patch.object(optimizer, "_get_weather_full", new=AsyncMock(return_value=[]))
        )
        stack.enter_context(
            patch.object(optimizer, "_get_last_status", new=AsyncMock(return_value=status))
        )
        stack.enter_context(patch.object(rules_engine.thermal_model, "load_latest"))
        stack.enter_context(
            patch.object(rules_engine.thermal_model, "calibrate", new=AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "resolve_outdoor_temperature",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        effective_c=5.0,
                        heat_pump_c=None,
                        weather_c=5.0,
                        source="weather",
                        weather_provider="test",
                        compensation_c=0.0,
                        fallback_reason=None,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "get_control_temperature",
                new=AsyncMock(
                    return_value=ControlTemperature(
                        value=20.0,
                        confidence="high",
                        sensor_count=1,
                        sample_count=1,
                        latest_reading=observed_at,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine, "get_heat_curve_config", new=AsyncMock(return_value=HeatCurveConfig())
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine, "get_space_heating_gate_config", new=AsyncMock(return_value={})
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "resolve_effective_gate",
                return_value=SimpleNamespace(
                    state="ALLOWED",
                    reason_code="test",
                    profile_id=None,
                    base_c=5.0,
                    on_threshold_c=4.0,
                    off_threshold_c=6.0,
                    last_raw_outdoor_c=5.0,
                    fingerprint_matches=True,
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine, "project_gate_states", return_value=[SimpleNamespace(state="ALLOWED")]
            )
        )
        stack.enter_context(
            patch.object(rules_engine, "get_effective_schedule", new=AsyncMock(return_value={}))
        )
        stack.enter_context(
            patch.object(rules_engine, "get_user_tz", new=AsyncMock(return_value="UTC"))
        )
        stack.enter_context(
            patch.object(rules_engine, "get_float_setting", side_effect=setting_float)
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "get_setting",
                new=AsyncMock(
                    side_effect=lambda name: {
                        "space_heating_baseline_mode": "off",
                        "space_heating_default_fraction": "0.35",
                    }[name]
                ),
            )
        )
        stack.enter_context(
            patch.object(
                rules_engine,
                "get_int_setting",
                new=AsyncMock(
                    side_effect=lambda name: {
                        "quiet_mode_start": 22,
                        "quiet_mode_end": 7,
                        "price_comfort_override_pct": 90,
                        "price_eco_upgrade_pct": 25,
                    }[name]
                ),
            )
        )
        stack.enter_context(
            patch.object(rules_engine, "panasonic_tank_heating_available", return_value=False)
        )
        stack.enter_context(
            patch.object(rules_engine, "panasonic_zone_heating_available", return_value=True)
        )
        stack.enter_context(
            patch.object(
                optimizer,
                "_plan_peak_avoidance",
                side_effect=_peak_candidate(horizon),
            )
        )
        stack.enter_context(
            patch.object(optimizer, "_estimate_cost", new=AsyncMock(return_value=0.0))
        )
        stack.enter_context(
            patch.object(
                rules_engine, "evaluate_live_control_gate", new=AsyncMock(return_value=gate)
            )
        )
        if curve is not None:
            stack.enter_context(
                patch.object(rules_engine.thermal_model, "predict_indoor_controlled_curve", curve)
            )
        plan = await optimizer.generate_plan()
    assert all(not sentinel.called for sentinel in database_sentinels)
    return plan


@pytest.mark.asyncio
async def test_rules_plan_passes_resolved_passive_change_limit_to_decisions_and_snapshot():
    curve = MagicMock(return_value=[{"hour": 1, "predicted_indoor_temp": 20.0, "source": "linear"}])
    plan = await _generate_with_passive_policy(0.2, curve)

    assert plan is not None
    assert len(curve.call_args_list) >= 4
    assert all(call.kwargs["max_passive_change_c_per_hour"] == 0.2 for call in curve.call_args_list)
    certified = plan["actions"] + plan["diagnostic_strategy_actions"]
    assert all(item["type"] != "peak_avoidance" for item in certified)
    unproven_types = [item["type"] for item in plan["unproven_strategies"]["actions"]]
    assert unproven_types.count("peak_avoidance") == 1


@pytest.mark.asyncio
async def test_rules_refuses_certification_when_policy_aware_rates_are_nonfinite():
    curve = MagicMock(
        return_value=[{"hour": 1, "predicted_indoor_temp": float("nan"), "source": "linear"}]
    )
    with patch.object(rules_engine.logger, "info") as info:
        plan = await _generate_with_passive_policy(0.2, curve)

    assert plan is None
    assert any(
        call.args == ("rules_strategy_unproven",) and call.kwargs["reason"] == "DataIncompleteError"
        for call in info.call_args_list
    )


def _policy_curve(**kwargs):
    """Synthetic controlled curve: passive drift follows the resolved policy, full heat 1.5 C/h."""
    limit = kwargs["max_passive_change_c_per_hour"]
    fractions = kwargs["heating_fractions"]
    value = kwargs["current_indoor"]
    rows = []
    for hour in range(kwargs["hours"]):
        fraction = fractions[hour] if hour < len(fractions) else 0.0
        value += -limit + fraction * (1.5 + limit)
        rows.append(
            {"hour": hour + 1, "predicted_indoor_temp": round(value, 1), "source": "linear"}
        )
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0.2, 0.6])
async def test_rules_represented_forecast_consumes_resolved_passive_policy(limit):
    curve = MagicMock(side_effect=_policy_curve)
    plan = await _generate_with_passive_policy(limit, curve)

    assert plan is not None
    assert plan["diagnostic_status"] == "conditional_feasible"
    row = plan["represented_strategy"]["hours"][0]
    # The passive rate follows the resolved policy; full heat is not capped by it.
    assert row["indoor_passive_c_per_h"] == pytest.approx(-limit)
    assert row["indoor_full_heat_c_per_h"] == pytest.approx(1.5)
    replay = plan["serialized_replay"]
    expected = 20.0
    previous = dt.datetime.fromisoformat(replay["states"][0]["ts"])
    for state in replay["states"][1:]:
        instant = dt.datetime.fromisoformat(state["ts"])
        duration = (instant - previous).total_seconds() / 3600
        expected += duration * (-limit + state["space_heating_fraction"] * (1.5 + limit))
        previous = instant
    assert replay["states"][-1]["indoor_c"] == pytest.approx(expected)
    final = plan["forecast_snapshot"]["forecast_with_plan"][-1]["predicted_indoor_temp"]
    assert final == pytest.approx(expected, abs=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "learned_change"), [(0.2, -0.2), (0.6, -0.3)])
async def test_rules_plan_real_curve_admitted_gate_publishes_policy_capped_passive_rate(
    limit, learned_change
):
    gate = {"status": "available", "control_allowed": True, "reason": "synthetic_admitted"}
    with _synthetic_passive_predictor(-0.3) as models:
        plan = await _generate_with_passive_policy(limit, None, gate=gate)

    assert plan is not None
    assert plan["diagnostic_status"] == "conditional_feasible"
    assert models[60].calls > 0
    row = plan["represented_strategy"]["hours"][0]
    assert row["indoor_passive_c_per_h"] == pytest.approx(learned_change)
    assert row["indoor_full_heat_c_per_h"] == pytest.approx(
        _curve_oracle(20.0, 1, outdoor=5.0, heated=True)[0] - 20.0
    )
    assert plan["forecast_snapshot"]["forecast_quality"]["control_allowed"] is True


class _SyntheticPassiveDirect:
    """Constant raw passive delta; the real projection, readiness and policy clamp still run."""

    def __init__(self, delta_c):
        self.delta_c = delta_c
        self.calls = 0

    def predict(self, rows):
        import numpy as np

        self.calls += 1
        return np.full(len(rows), self.delta_c)


@contextmanager
def _synthetic_passive_predictor(raw_change_c_per_hour):
    """Admit synthetic validated passive direct models on the real comfort-model singleton.

    The control model stays observation-only, so heated scenarios exercise the
    unqualified linear fallback inside the same real controlled curve.
    """
    from packages.ml.comfort_model import ComfortModel, comfort_model

    models = {
        horizon: _SyntheticPassiveDirect(raw_change_c_per_hour * horizon / 60)
        for horizon in (60, 180, 360, 720)
    }
    metrics = {
        "passive_horizons": {
            str(horizon): {"status": "trained", "mae": 0.1, "persistence_mae": 0.3}
            for horizon in models
        }
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(comfort_model, "_model", object()))
        stack.enter_context(patch.object(comfort_model, "_qualified_reference", None))
        stack.enter_context(patch.object(comfort_model, "_passive_direct_models", models))
        stack.enter_context(patch.object(comfort_model, "_metrics", metrics))
        stack.enter_context(
            patch.object(
                ComfortModel, "is_ready_for_control", new_callable=PropertyMock, return_value=False
            )
        )
        yield models


def _curve_oracle(start, hours, *, outdoor, heated, direct=None):
    """Independent hourly oracle: unrounded internal state, 0.1 C published values.

    ``direct`` maps an hour to the validated direct passive change from ``start``;
    other hours continue with linear physics from the previous internal state.
    """
    from packages.ml.thermal import _solar_gain_c, thermal_model

    supply = HeatCurveConfig().planned_supply_temperature(outdoor)
    values, indoor = [], start
    for hour in range(1, hours + 1):
        if direct is not None and hour in direct:
            indoor = start + direct[hour]
        else:
            rate = thermal_model._free_float_step(indoor, outdoor) - indoor
            if heated and supply > indoor + 5.0:
                rate = thermal_model._indoor_heating_rate(outdoor) + _solar_gain_c(0.0)
            indoor += rate
        values.append(round(indoor, 1))
    return values


def _published(rates, index, start=20.0):
    values, value = [], start
    for rate in rates:
        value = round(value + rate[index], 1)
        values.append(value)
    return values


def _passive_policy_inputs(hours, *, outdoor=5.0):
    start = dt.datetime(2026, 1, 5, 6, tzinfo=dt.timezone.utc)
    prices = [
        (start + dt.timedelta(hours=hour), price)
        for hour, price in zip(range(hours), [0.30, 0.05, 0.40, 0.08, 0.35, 0.25])
    ]
    weather = [(timestamp, outdoor) for timestamp, _price in prices]
    return start, prices, weather


def _policy_rates(limit, *, learned, hours=6, current_indoor=20.0):
    _start, prices, weather = _passive_policy_inputs(hours)
    return RulesOptimizer._represented_indoor_rates(
        prices=prices,
        weather=weather,
        weather_full=[],
        current_indoor=current_indoor,
        heat_curve=HeatCurveConfig(),
        use_learned_forecast=learned,
        max_passive_change_c_per_hour=limit,
    )


@pytest.mark.parametrize(
    ("limit", "anchors"),
    # Raw synthetic passive drift is -0.3 C/h; 0.2 binds, 0.6 does not cap it.
    [(0.2, {1: 19.8, 3: 19.4, 6: 18.8}), (0.6, {1: 19.7, 3: 19.1, 6: 18.2})],
)
def test_real_controlled_curve_admitted_learned_gate_applies_passive_policy(limit, anchors):
    with _synthetic_passive_predictor(-0.3) as models:
        rates = _policy_rates(limit, learned=True)

    assert {horizon: model.calls > 0 for horizon, model in models.items()} == {
        60: True,
        180: True,
        360: True,
        720: False,
    }
    passive_curve = _published(rates, 1)
    for hour, expected in anchors.items():
        assert passive_curve[hour - 1] == pytest.approx(expected)
    # Validated direct horizons apply the resolved policy as a cap on the learned
    # change; hours without one continue with linear physics, never a guessed rate.
    direct = {hour: max(-limit * hour, -0.3 * hour) for hour in (1, 3, 6)}
    assert passive_curve == pytest.approx(
        _curve_oracle(20.0, 6, outdoor=5.0, heated=False, direct=direct)
    )
    # The heated scenario is the unqualified linear fallback, not capped by the policy.
    assert _published(rates, 0) == pytest.approx(_curve_oracle(20.0, 6, outdoor=5.0, heated=True))
    assert all(full > passive for full, passive in rates)


def test_real_controlled_curve_unqualified_fallback_ignores_passive_policy():
    with _synthetic_passive_predictor(-0.3) as models:
        tight = _policy_rates(0.2, learned=False)
        loose = _policy_rates(0.6, learned=False)

    assert all(model.calls == 0 for model in models.values())
    assert tight == pytest.approx(loose)
    assert _published(tight, 1) == pytest.approx(_curve_oracle(20.0, 6, outdoor=5.0, heated=False))
    assert _published(tight, 0) == pytest.approx(_curve_oracle(20.0, 6, outdoor=5.0, heated=True))


@pytest.mark.parametrize("limit", [0.2, 0.6])
def test_represented_plan_replays_real_policy_rates_with_native_mix_and_dhw_diversion(limit):
    from packages.core.config import settings
    from packages.optimizer.represented_strategy import (
        assumed_native_demand,
        build_represented_plan,
    )

    start, prices, weather = _passive_policy_inputs(6)
    with _synthetic_passive_predictor(-0.3):
        rates = _policy_rates(limit, learned=True)
    gates = ["ALLOWED"] * 6
    captured = start - dt.timedelta(minutes=1)
    from tests.test_milp import _context

    context = _context(
        prices,
        device_id="synthetic-device",
        observed_at=captured - dt.timedelta(minutes=2),
        captured_at=captured,
        tank_target_c=55.0,
        power=settings.sh_max_power_kw,
        demand=[settings.sh_max_power_kw * 3 / 7] * 6,
    )
    context["native_sh_provenance"]["captured_at"] = captured.isoformat()
    demand = assumed_native_demand(weather, gates, context["native_sh_provenance"])
    plan = build_represented_plan(
        prices=prices,
        weather=weather,
        cop_fn=lambda _outdoor, _hour=12: 3.0,
        demand_per_hour=demand,
        current_tank_temp=46.0,
        current_indoor_temp=20.0,
        gate_states=gates,
        hardware_tank_ceiling=65.0,
        context=context,
        dhw_deadlines=[6],
        dhw_deadline_tank_floors_c=[45],
        indoor_targets=[18.0] * 6,
        tank_min_per_hour=[45.0] * 6,
        tank_max_temp_setting=55.0,
        indoor_rates=rates,
        indoor_max_temp_setting=23.0,
        timeout_seconds=20,
    )

    rows = plan["represented_strategy"]["hours"]
    assert [(row["indoor_full_heat_c_per_h"], row["indoor_passive_c_per_h"]) for row in rows] == (
        pytest.approx(rates)
    )
    assert all(0 < row["native_sh_fraction"] < 1 for row in rows)
    replay = plan["serialized_replay"]
    diverted = [state for state in replay["states"][1:] if state["dhw_active"]]
    assert diverted and all(state["space_heating_fraction"] == 0.0 for state in diverted)
    assert [action["type"] for action in plan["actions"]].count("force_dhw_on") >= 1

    indoor, cost, upper = 20.0, 0.0, 0.0
    previous = start
    for state in replay["states"][1:]:
        instant = dt.datetime.fromisoformat(state["ts"])
        row = rows[int((previous - start).total_seconds() // 3600)]
        duration = (instant - previous).total_seconds() / 3600
        fraction = 0.0 if state["dhw_active"] else row["native_sh_fraction"]
        assert state["space_heating_fraction"] == pytest.approx(fraction)
        indoor += duration * (
            row["indoor_passive_c_per_h"]
            + fraction * (row["indoor_full_heat_c_per_h"] - row["indoor_passive_c_per_h"])
        )
        assert state["indoor_c"] == pytest.approx(indoor)
        power = row["dhw_power_kw"] if state["dhw_active"] else settings.sh_max_power_kw * fraction
        cost += power * duration * row["price_per_kwh"]
        upper += (
            max(row["dhw_power_kw"], row["native_sh_power_kw"]) * duration * row["price_per_kwh"]
        )
        previous = instant
    assert plan["cost_estimate"] == pytest.approx(cost)
    assert 0 < plan["cost_estimate"] <= upper
    serialized = json.dumps(plan, default=str)
    for private in ("comfort_model", "passive_direct", "direct_horizon", "persistence_mae"):
        assert private not in serialized


class TestControlBasisAndForecastContract:
    @pytest.mark.asyncio
    async def test_reference_basis_wins_over_four_sensor_mean(self):
        now = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id=device_id,
                temperature=temperature,
                timestamp=now,
                is_stale=False,
                device_label=device_id,
                room=device_id,
            )
            for device_id, temperature in (
                ("sensor-1", 26.1),
                ("sensor-2", 25.2),
                ("sensor-3", 25.8),
                ("reference", 23.8),
            )
        ]
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = rows
        reference_result = MagicMock()
        reference_result.scalar_one_or_none.return_value = "reference"
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[rows_result, reference_result])

        with patch(
            "packages.poller.smartthings.get_selected_device_ids",
            new=AsyncMock(return_value=[row.device_id for row in rows]),
        ):
            control = await get_control_temperature(now=now, session=session)

        assert control.value == pytest.approx(23.8)
        assert control.reason == "reference_sensor"
        assert control.value != pytest.approx(sum(row.temperature for row in rows) / len(rows))

    @pytest.mark.asyncio
    async def test_stale_sensor_data_is_unusable_and_single_fresh_sensor_is_medium_confidence(self):
        now = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        empty_rows = MagicMock()
        empty_rows.scalars.return_value.all.return_value = []
        reference_result = MagicMock()
        reference_result.scalar_one_or_none.return_value = None
        stale_session = MagicMock()
        stale_session.execute = AsyncMock(side_effect=[empty_rows, reference_result])

        single_row = SimpleNamespace(
            device_id="single",
            temperature=21.4,
            timestamp=now,
            is_stale=False,
            device_label="single",
            room="office",
        )
        single_rows = MagicMock()
        single_rows.scalars.return_value.all.return_value = [single_row]
        single_session = MagicMock()
        single_session.execute = AsyncMock(side_effect=[single_rows, reference_result])

        with patch(
            "packages.poller.smartthings.get_selected_device_ids",
            new=AsyncMock(return_value=["single"]),
        ):
            stale = await get_control_temperature(now=now, session=stale_session)
            single = await get_control_temperature(now=now, session=single_session)

        assert stale.value is None
        assert stale.confidence == "low"
        assert single.value == pytest.approx(21.4)
        assert single.confidence == "medium"

    def test_all_overheated_rooms_are_reported_without_changing_the_control_basis(self):
        now = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        control = ControlTemperature(
            value=26.0,
            confidence="high",
            sensor_count=2,
            sample_count=2,
            latest_reading=now,
            sensors=(
                SensorTemperature("a", "A", "living", 26.0, now),
                SensorTemperature("b", "B", "bedroom", 27.0, now),
            ),
        )

        envelope = build_room_comfort_envelope(control, comfort_temp_max=25.0)

        assert envelope.basis_temperature == 26.0
        assert envelope.fresh_inlier_min == 26.0
        assert envelope.fresh_inlier_max == 27.0
        assert envelope.rooms_above_max == ("living", "bedroom")

    def test_rules_envelope_failure_fails_closed_without_overheat_suppression(self):
        control = ControlTemperature(
            value=None,
            confidence="low",
            sensor_count=0,
            sample_count=0,
            latest_reading=None,
            reason="room_control_evidence_unavailable",
        )

        envelope = build_room_comfort_envelope(control, comfort_temp_max=25.0)
        duties = minimum_floor_protection_duties(
            basis_temperature=envelope.basis_temperature,
            comfort_temp_min=18.0,
            indoor_rates=[(1.0, -0.5)],
        )

        assert envelope.rooms_above_max == ()
        assert duties == [0.0]

    def test_api_preserves_plan_start_observation_and_declared_sensor_basis(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        point = {
            "ts": (start + dt.timedelta(hours=1)).isoformat(),
            "predicted_indoor_temp": 23.9,
        }
        snapshot = {
            "version": "indoor_forecast_v3",
            "current_indoor": 23.8,
            "observed_history": [{"hour": 0, "ts": start.isoformat(), "temperature": 23.8}],
            "sensor_basis": {"kind": "reference", "label": "Reference room"},
            "forecast": [point],
            "forecast_with_plan": [point],
            "forecast_no_heating": [point],
            "target_schedule": [{"ts": point["ts"], "target": 20.0}],
            "weather_forecast": [{"ts": start.isoformat()}],
            "price_forecast": [{"ts": start.isoformat(), "price_eur_per_kwh": 0.1}],
        }

        result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

        assert result is not None
        assert result["observed_history"][0]["hour"] == 0
        assert result["observed_history"][0]["ts"] == start.isoformat()
        assert result["observed_history"][0]["temperature"] == pytest.approx(23.8)
        assert result["sensor_basis"] == snapshot["sensor_basis"]
        assert result["observed_history"][0]["temperature"] != pytest.approx(25.225)

    def test_api_legacy_snapshot_without_history_remains_usable(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        point = {
            "ts": (start + dt.timedelta(hours=1)).isoformat(),
            "predicted_indoor_temp": 20.0,
        }
        snapshot = {
            "version": "indoor_forecast_v2",
            "current_indoor": 20.0,
            "forecast": [point],
            "forecast_with_plan": [point],
            "forecast_no_heating": [point],
            "target_schedule": [{"ts": point["ts"], "target": 20.0}],
            "weather_forecast": [{"ts": start.isoformat()}],
            "price_forecast": [{"ts": start.isoformat(), "price_eur_per_kwh": 0.1}],
        }

        result = _plan_forecast_window(json.dumps({"forecast_snapshot": snapshot}), start, 1)

        assert result is not None
        assert result["observed_history"] == []
        assert result["sensor_basis"] == {}

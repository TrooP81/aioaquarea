import pytest

from packages.core.heat_curve import (
    HeatCurveConfig,
    evaluate_heat_curve_verification,
    heat_curve_advice,
    start_heat_curve_verification,
)
from packages.core.space_heating_gate import HeatingGateConfig, resolve_effective_gate


def _gate_evidence(state="ALLOWED", fingerprint_matches=True):
    config = HeatingGateConfig()
    row = type(
        "GateRow",
        (),
        {
            "state": state,
            "reason_code": "test_gate_state",
            "config_fingerprint": config.fingerprint if fingerprint_matches else "obsolete",
            "last_raw_outdoor_c": 12.0,
        },
    )()
    return resolve_effective_gate(row, config)


def test_controller_curve_interpolates_and_clamps():
    curve = HeatCurveConfig()

    assert curve.supply_temperature(-5) == 47
    assert curve.supply_temperature(5) == 47
    assert curve.supply_temperature(10) == 35
    assert curve.supply_temperature(15) == 23
    assert curve.supply_temperature(20) == 23


def test_supply_projection_is_independent_of_room_heating_eligibility():
    curve = HeatCurveConfig(heating_off_outdoor_c=13)

    assert curve.planned_supply_temperature(12.9) == pytest.approx(28.04)
    assert curve.planned_supply_temperature(13) == pytest.approx(27.8)
    assert curve.planned_supply_temperature(21) == 23


def test_allowed_advice_does_not_infer_eligibility_from_solar_gain_warm_weather():
    curve = HeatCurveConfig()

    advice = heat_curve_advice(
        curve,
        indoor_temp=24.3,
        comfort_target=21.5,
        outdoor_temp=21.0,
        gate_evidence=_gate_evidence(),
    )

    assert advice["status"] == "too_warm"
    assert advice["suggested"] is not None
    assert advice["controllability"] == "heat_curve_effective"


def test_too_warm_advice_is_bounded_during_heating_season():
    curve = HeatCurveConfig()

    advice = heat_curve_advice(
        curve,
        indoor_temp=24.3,
        comfort_target=21.5,
        outdoor_temp=8.0,
        gate_evidence=_gate_evidence(),
    )

    assert advice["status"] == "too_warm"
    assert advice["suggested"]["supply_cold_c"] == 45.0
    assert advice["suggested"]["supply_warm_c"] == 21.0
    assert advice["suggested"]["heating_off_outdoor_c"] == 11.0
    assert advice["suggested"]["delta_t_c"] == 4.0


@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN"])
def test_advice_suppresses_actions_when_gate_is_not_allowed(state):
    advice = heat_curve_advice(
        HeatCurveConfig(),
        indoor_temp=18.0,
        comfort_target=21.5,
        outdoor_temp=5.0,
        gate_evidence=_gate_evidence(state),
    )

    assert advice["status"] == "not_controllable"
    assert advice["suggested"] is None
    assert advice["controllability"] == f"space_heating_gate_{state.lower()}"


def test_advice_suppresses_actions_when_gate_fingerprint_mismatches():
    advice = heat_curve_advice(
        HeatCurveConfig(),
        indoor_temp=18.0,
        comfort_target=21.5,
        outdoor_temp=5.0,
        gate_evidence=_gate_evidence(fingerprint_matches=False),
    )

    assert advice["status"] == "not_controllable"
    assert advice["gate_state"] == "UNKNOWN"
    assert advice["gate_reason"] == "config_fingerprint_mismatch"


def test_curve_rejects_invalid_point_order():
    with pytest.raises(ValueError, match="Cold outdoor point"):
        HeatCurveConfig(outdoor_cold_c=15, outdoor_warm_c=5).validate()


def test_verification_locks_new_advice_without_cool_weather_evidence():
    state = start_heat_curve_verification(
        started_at="2026-07-12T12:00:00+00:00",
        previous_curve=HeatCurveConfig(),
        applied_curve=HeatCurveConfig(supply_cold_c=45, supply_warm_c=21),
        baseline_indoor_temp=24.3,
        baseline_outdoor_temp=21.0,
        comfort_target=21.5,
    )

    result = evaluate_heat_curve_verification(
        state,
        elapsed_hours=25,
        indoor_samples=[24.2, 24.0, 23.9, 23.8, 23.7, 23.6],
        heating_condition_samples=0,
        current_comfort_target=21.5,
    )

    assert result["status"] == "pending"
    assert result["recommendation_available"] is False
    assert any("cooler outdoor" in reason for reason in result["reasons"])


def test_verification_unlocks_after_observation_window_and_measures_effect():
    state = start_heat_curve_verification(
        started_at="2026-07-12T12:00:00+00:00",
        previous_curve=HeatCurveConfig(),
        applied_curve=HeatCurveConfig(supply_cold_c=45, supply_warm_c=21),
        baseline_indoor_temp=24.3,
        baseline_outdoor_temp=8.0,
        comfort_target=21.5,
    )

    result = evaluate_heat_curve_verification(
        state,
        elapsed_hours=25,
        indoor_samples=[22.4, 22.3, 22.2, 22.1, 22.0, 21.9],
        heating_condition_samples=3,
        current_comfort_target=21.5,
    )

    assert result["status"] == "verified"
    assert result["recommendation_available"] is True
    assert result["comfort_improvement_c"] == pytest.approx(2.15)
    assert result["verification_decision"] == "accepted"
    assert result["effect_evidence"] == "high"

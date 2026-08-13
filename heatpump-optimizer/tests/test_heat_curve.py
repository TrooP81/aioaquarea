import pytest

from packages.core.heat_curve import (
    HeatCurveConfig,
    evaluate_heat_curve_verification,
    heat_curve_advice,
    start_heat_curve_verification,
)


def test_controller_curve_interpolates_and_clamps():
    curve = HeatCurveConfig()

    assert curve.supply_temperature(-5) == 47
    assert curve.supply_temperature(5) == 47
    assert curve.supply_temperature(10) == 35
    assert curve.supply_temperature(15) == 23
    assert curve.supply_temperature(20) == 23


def test_controller_cutoff_removes_planned_space_heat():
    curve = HeatCurveConfig(heating_off_outdoor_c=13)

    assert curve.planned_supply_temperature(12.9) == pytest.approx(28.04)
    assert curve.planned_supply_temperature(13) == 13
    assert curve.planned_supply_temperature(21) == 21


def test_advice_is_suppressed_outside_the_heating_season():
    curve = HeatCurveConfig()

    advice = heat_curve_advice(
        curve,
        indoor_temp=24.3,
        comfort_target=21.5,
        outdoor_temp=21.0,
    )

    assert advice["status"] == "outside_heating_season"
    assert advice["suggested"] is None
    assert advice["controllability"] == "not_controllable_by_heat_curve"
    assert any("Space heating is off" in reason for reason in advice["reasons"])


def test_too_warm_advice_is_bounded_during_heating_season():
    curve = HeatCurveConfig()

    advice = heat_curve_advice(
        curve,
        indoor_temp=24.3,
        comfort_target=21.5,
        outdoor_temp=8.0,
    )

    assert advice["status"] == "too_warm"
    assert advice["suggested"]["supply_cold_c"] == 45.0
    assert advice["suggested"]["supply_warm_c"] == 21.0
    assert advice["suggested"]["heating_off_outdoor_c"] == 12.0
    assert advice["suggested"]["delta_t_c"] == 4.0


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

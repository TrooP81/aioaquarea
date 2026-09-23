from packages.core.comfort_assessment import build_comfort_assessment
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

    assert assessment["controllability"]["status"] == "no_space_heat_planned"


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

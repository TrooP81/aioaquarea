from types import SimpleNamespace

from packages.core.heat_curve import HeatCurveConfig
from packages.core.outcome_experiments import assess_manual_trial_conditions
from packages.core.space_heating_gate import HeatingGateConfig


def _gate(state: str):
    config = HeatingGateConfig()
    return (
        SimpleNamespace(
            state=state,
            reason_code="test_gate",
            config_fingerprint=config.fingerprint,
            last_raw_outdoor_c=5.0,
        ),
        config,
    )


def test_manual_trial_is_withheld_above_heat_curve_cutoff():
    gate_row, gate_config = _gate("BLOCKED")
    result = assess_manual_trial_conditions(
        HeatCurveConfig(heating_off_outdoor_c=13.0),
        SimpleNamespace(outdoor_temp=16.0, device_action="OFF", defrost_active=False),
        gate_row,
        gate_config,
    )

    assert result["ready"] is False
    assert result["reason"] == "space_heating_gate_blocked"


def test_manual_trial_is_withheld_for_dhw_even_when_cold():
    gate_row, gate_config = _gate("ALLOWED")
    result = assess_manual_trial_conditions(
        HeatCurveConfig(),
        SimpleNamespace(
            outdoor_temp=5.0,
            device_action="HEATING_WATER",
            defrost_active=False,
            space_heating_evidence="domestic_hot_water",
        ),
        gate_row,
        gate_config,
    )

    assert result["ready"] is False
    assert result["reason"] == "domestic_hot_water_active"


def test_manual_trial_can_be_reviewed_in_cool_non_conflicting_conditions():
    gate_row, gate_config = _gate("ALLOWED")
    result = assess_manual_trial_conditions(
        HeatCurveConfig(),
        SimpleNamespace(
            outdoor_temp=5.0,
            device_action="OFF",
            defrost_active=False,
            space_heating_active=False,
            space_heating_evidence="device_off",
        ),
        gate_row,
        gate_config,
    )

    assert result["ready"] is True
    assert result["space_heating_confirmed"] is False

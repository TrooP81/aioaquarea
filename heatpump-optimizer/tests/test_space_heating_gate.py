from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.space_heating_gate import (
    HeatingGateConfig,
    SpaceHeatingGateState,
    WH_MXC12J9E8_J_DEFAULT,
    project_gate_states,
    resolve_effective_gate,
    transition_gate,
)


@pytest.mark.parametrize(
    ("previous", "temperature", "expected"),
    [
        ("UNKNOWN", 13.9, "ALLOWED"),
        ("UNKNOWN", 16.1, "BLOCKED"),
        ("UNKNOWN", 14.0, "UNKNOWN"),
        ("UNKNOWN", 16.0, "UNKNOWN"),
        ("ALLOWED", 15.0, "ALLOWED"),
        ("ALLOWED", 16.1, "BLOCKED"),
        ("ALLOWED", 16.0, "ALLOWED"),
        ("BLOCKED", 15.0, "BLOCKED"),
        ("BLOCKED", 13.9, "ALLOWED"),
        ("BLOCKED", 14.0, "BLOCKED"),
    ],
)
def test_transition_gate_implements_approved_hysteresis(previous, temperature, expected):
    evidence = transition_gate(
        previous, temperature, HeatingGateConfig(base_c=13, on_offset_c=1, off_offset_c=3)
    )

    assert evidence.state == expected


@pytest.mark.parametrize("invalid", [None, True, "13", float("nan"), float("inf")])
def test_invalid_raw_temperature_preserves_reliable_state_and_fails_closed(invalid):
    evidence = transition_gate(SpaceHeatingGateState.ALLOWED, invalid, HeatingGateConfig())

    assert evidence.state is SpaceHeatingGateState.ALLOWED
    assert evidence.last_raw_outdoor_c is None


def test_configuration_mismatch_is_immediately_unknown_and_projection_does_not_repair_it():
    config = HeatingGateConfig()
    row = SimpleNamespace(
        state="ALLOWED",
        reason_code="below_on_threshold",
        config_fingerprint="obsolete",
        last_raw_outdoor_c=10.0,
    )

    effective = resolve_effective_gate(row, config)
    projected = project_gate_states(effective, [14.0], config)

    assert effective.state is SpaceHeatingGateState.UNKNOWN
    assert effective.fingerprint_matches is False
    assert projected[0].state is SpaceHeatingGateState.UNKNOWN


def test_gate_configuration_requires_finite_ordered_thresholds():
    with pytest.raises(ValueError, match="lower"):
        HeatingGateConfig(on_offset_c=3, off_offset_c=1)
    with pytest.raises(ValueError, match="finite"):
        HeatingGateConfig(base_c=float("nan"))


def test_j_tcap_profile_has_approved_defaults_and_strict_operators():
    config = HeatingGateConfig()

    assert config.profile_id == WH_MXC12J9E8_J_DEFAULT
    assert (config.base_c, config.on_threshold_c, config.off_threshold_c) == (12.0, 13.0, 15.0)
    assert (
        transition_gate("UNKNOWN", 13.0, config).state,
        transition_gate("UNKNOWN", 15.0, config).state,
    ) == (
        SpaceHeatingGateState.UNKNOWN,
        SpaceHeatingGateState.UNKNOWN,
    )
    assert transition_gate("UNKNOWN", 12.9, config).state is SpaceHeatingGateState.ALLOWED
    assert transition_gate("UNKNOWN", 15.1, config).state is SpaceHeatingGateState.BLOCKED


def test_gate_sequence_persists_across_restart_and_only_changes_outside_band():
    config = HeatingGateConfig()
    state = SpaceHeatingGateState.UNKNOWN

    for temperature, expected in [
        (12.9, SpaceHeatingGateState.ALLOWED),
        (13.0, SpaceHeatingGateState.ALLOWED),
        (14.0, SpaceHeatingGateState.ALLOWED),
        (15.0, SpaceHeatingGateState.ALLOWED),
        (15.1, SpaceHeatingGateState.BLOCKED),
        (14.0, SpaceHeatingGateState.BLOCKED),
    ]:
        state = transition_gate(state, temperature, config).state
        assert state is expected

    restarted = resolve_effective_gate(
        SimpleNamespace(
            state=state,
            reason_code="above_off_threshold",
            config_fingerprint=config.fingerprint,
            last_raw_outdoor_c=14.0,
        ),
        config,
    )
    assert restarted.state is SpaceHeatingGateState.BLOCKED
    assert transition_gate(restarted.state, 12.9, config).state is SpaceHeatingGateState.ALLOWED


def test_planners_have_no_stateless_heat_curve_cutoff():
    root = Path(__file__).parents[1]
    planner_paths = (
        root / "packages" / "optimizer" / "milp.py",
        root / "packages" / "optimizer" / "rule_mixins.py",
        root / "packages" / "optimizer" / "rules_engine.py",
    )

    for planner_path in planner_paths:
        source = planner_path.read_text(encoding="utf-8")
        assert "heating_off_outdoor_c" not in source

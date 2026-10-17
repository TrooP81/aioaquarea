"""Stateful, fail-closed eligibility for room-heating increases."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable


class SpaceHeatingGateState(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


WH_MXC12J9E8_J_DEFAULT = "WH_MXC12J9E8_J_DEFAULT"
_BEHAVIOR_PROFILES = {
    WH_MXC12J9E8_J_DEFAULT: {
        "base_c": 12.0,
        "on_offset_c": 1.0,
        "off_offset_c": 3.0,
        "on_operator": "<",
        "off_operator": ">",
    }
}


@dataclass(frozen=True)
class HeatingGateConfig:
    """Recorded Panasonic heating-off base and hysteresis offsets."""

    profile_id: str = WH_MXC12J9E8_J_DEFAULT
    base_c: float = 12.0
    on_offset_c: float = 1.0
    off_offset_c: float = 3.0

    def __post_init__(self) -> None:
        if self.profile_id not in _BEHAVIOR_PROFILES:
            raise ValueError("Unknown space-heating behavior profile")
        values = (self.base_c, self.on_offset_c, self.off_offset_c)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError("Heating gate values must be finite numbers")
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Heating gate values must be finite numbers")
        if self.on_threshold_c >= self.off_threshold_c:
            raise ValueError("Heating gate ON threshold must be lower than OFF threshold")

    @property
    def on_threshold_c(self) -> float:
        return float(self.base_c) + float(self.on_offset_c)

    @property
    def off_threshold_c(self) -> float:
        return float(self.base_c) + float(self.off_offset_c)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "version": 2,
                "profile_id": self.profile_id,
                "on_operator": self.on_operator,
                "off_operator": self.off_operator,
                "base_c": self.base_c,
                "on_offset_c": self.on_offset_c,
                "off_offset_c": self.off_offset_c,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    @property
    def on_operator(self) -> str:
        return _BEHAVIOR_PROFILES[self.profile_id]["on_operator"]

    @property
    def off_operator(self) -> str:
        return _BEHAVIOR_PROFILES[self.profile_id]["off_operator"]


@dataclass(frozen=True)
class GateEvidence:
    state: SpaceHeatingGateState
    reason_code: str
    config_fingerprint: str
    profile_id: str
    on_operator: str
    off_operator: str
    base_c: float
    on_threshold_c: float
    off_threshold_c: float
    last_raw_outdoor_c: float | None


@dataclass(frozen=True)
class EffectiveGateEvidence(GateEvidence):
    fingerprint_matches: bool


def _finite_temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    temperature = float(value)
    return temperature if math.isfinite(temperature) else None


def transition_gate(
    previous: SpaceHeatingGateState | str | None,
    raw_temp: Any,
    config: HeatingGateConfig,
) -> GateEvidence:
    """Evaluate raw pump temperature without inventing an in-band state."""

    try:
        prior = SpaceHeatingGateState(previous or SpaceHeatingGateState.UNKNOWN)
    except ValueError:
        prior = SpaceHeatingGateState.UNKNOWN
    temperature = _finite_temperature(raw_temp)
    if temperature is None:
        return GateEvidence(
            state=prior
            if prior is not SpaceHeatingGateState.UNKNOWN
            else SpaceHeatingGateState.UNKNOWN,
            reason_code="raw_outdoor_temperature_invalid",
            config_fingerprint=config.fingerprint,
            profile_id=config.profile_id,
            on_operator=config.on_operator,
            off_operator=config.off_operator,
            base_c=float(config.base_c),
            on_threshold_c=config.on_threshold_c,
            off_threshold_c=config.off_threshold_c,
            last_raw_outdoor_c=None,
        )
    if temperature < config.on_threshold_c:
        state, reason = SpaceHeatingGateState.ALLOWED, "below_on_threshold"
    elif temperature > config.off_threshold_c:
        state, reason = SpaceHeatingGateState.BLOCKED, "above_off_threshold"
    else:
        state = prior
        reason = (
            "within_hysteresis_band"
            if prior is not SpaceHeatingGateState.UNKNOWN
            else "initial_in_band"
        )
    return GateEvidence(
        state=state,
        reason_code=reason,
        config_fingerprint=config.fingerprint,
        profile_id=config.profile_id,
        on_operator=config.on_operator,
        off_operator=config.off_operator,
        base_c=float(config.base_c),
        on_threshold_c=config.on_threshold_c,
        off_threshold_c=config.off_threshold_c,
        last_raw_outdoor_c=temperature,
    )


def resolve_effective_gate(row: Any | None, config: HeatingGateConfig) -> EffectiveGateEvidence:
    """Expose persisted evidence only when it matches the active configuration."""

    if row is None or getattr(row, "config_fingerprint", None) != config.fingerprint:
        return EffectiveGateEvidence(
            state=SpaceHeatingGateState.UNKNOWN,
            reason_code="gate_missing" if row is None else "config_fingerprint_mismatch",
            config_fingerprint=config.fingerprint,
            profile_id=config.profile_id,
            on_operator=config.on_operator,
            off_operator=config.off_operator,
            base_c=float(config.base_c),
            on_threshold_c=config.on_threshold_c,
            off_threshold_c=config.off_threshold_c,
            last_raw_outdoor_c=None,
            fingerprint_matches=False,
        )
    try:
        state = SpaceHeatingGateState(row.state)
    except (TypeError, ValueError):
        state = SpaceHeatingGateState.UNKNOWN
    return EffectiveGateEvidence(
        state=state,
        reason_code=getattr(row, "reason_code", "malformed_gate_row"),
        config_fingerprint=config.fingerprint,
        profile_id=config.profile_id,
        on_operator=config.on_operator,
        off_operator=config.off_operator,
        base_c=float(config.base_c),
        on_threshold_c=config.on_threshold_c,
        off_threshold_c=config.off_threshold_c,
        last_raw_outdoor_c=_finite_temperature(getattr(row, "last_raw_outdoor_c", None)),
        fingerprint_matches=True,
    )


def project_gate_states(
    seed: EffectiveGateEvidence,
    forecast_temperatures: Iterable[Any],
    config: HeatingGateConfig,
) -> list[GateEvidence]:
    """Project forecast eligibility without mutating authoritative evidence."""

    previous = seed.state if seed.fingerprint_matches else SpaceHeatingGateState.UNKNOWN
    projections = []
    for temperature in forecast_temperatures:
        evidence = transition_gate(previous, temperature, config)
        projections.append(evidence)
        previous = evidence.state
    return projections

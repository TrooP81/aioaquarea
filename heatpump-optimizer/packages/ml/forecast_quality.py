"""Out-of-sample quality scoring for immutable plan forecasts."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from packages.ml.models_common import is_finite_number as _finite_number
import structlog
from sqlalchemy import and_, desc, or_, select

from packages.core.database import copy_materialized_rows, get_session
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    HouseInstallationRecord,
    IndoorTempReading,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.optimizer.executor_core import (
    INITIAL_VERIFY_CHECKPOINTS_S,
    REDISPATCH_VERIFY_CHECKPOINTS_S,
)

logger = structlog.get_logger()

HORIZONS = (1, 3, 6, 12, 24)
MIN_GATE_SAMPLES = 30
MIN_HORIZON_GATE_SAMPLES = 12
MIN_REGIME_GATE_SAMPLES = 12
MIN_BIAS_CORRECTION_SAMPLES = 12
MIN_INTERVAL_SAMPLES = 12
MAX_GATE_MAE_C = 1.25
MAX_GATE_ABS_BIAS_C = 0.5
MAX_GATE_P90_ABS_ERROR_C = 2.0
MAX_BIAS_CORRECTION_C = 0.4
MIN_BASELINE_PROMOTION_PLANS = 20
MIN_BASELINE_PROMOTION_PAIRS = 60
MIN_BASELINE_PROMOTION_DAYS = 7
MIN_BASELINE_HORIZON_PAIRS = 12
MIN_BASELINE_MAE_IMPROVEMENT_C = 0.10
MAX_BASELINE_CANDIDATE_ABS_BIAS_C = 0.50
MAX_BASELINE_CANDIDATE_P90_C = 2.0
MAX_DISPATCH_ACTION_LOOKUP = 500
CARRYOVER_LOOKBACK = dt.timedelta(hours=24)
DISPATCH_DRIFT_MARGIN = dt.timedelta(
    seconds=max(INITIAL_VERIFY_CHECKPOINTS_S) + max(REDISPATCH_VERIFY_CHECKPOINTS_S) + 60
)

# A condition without evaluation data must never be treated as evidence that
# the forecast is accurate.  These modest, additive planning reserves protect
# comfort until enough real outcomes have been observed in the condition.
UNOBSERVED_REGIME_MARGIN_C = {
    "rain": 0.25,
    "cold": 0.35,
    "mild": 0.0,
    "windy": 0.2,
    "sunny": 0.15,
    "humid": 0.1,
    "cloudy": 0.1,
}
WEATHER_REGIMES = tuple(UNOBSERVED_REGIME_MARGIN_C)

NATIVE_POLICY_SCHEMA = "native_unchanged_v1"
NATIVE_ESTIMATOR_SCHEMA = "native_persistence_15m_v1"
QUALIFICATION_POLICY_SCHEMA = "prospective_native_fixed_daily_v1"
DECISION_OFFSETS_HOURS = tuple(range(15 * 24, 85 * 24 + 1, 24))
QUALIFICATION_FAMILY_ALPHA = 0.05
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_CHUNK = 128
BOOTSTRAP_BLOCK_DAYS = 2
MAX_QUALIFICATION_PAIRS = 50_000
MIN_PROSPECTIVE_DAYS = 14
MIN_DEVICE_DAY_CLUSTERS = 14
MIN_ISSUE_COHORTS = 20
MIN_PROSPECTIVE_PAIRS = 60
MIN_OUTDOOR_BIN_PAIRS = 12
MIN_POPULATED_OUTDOOR_BINS = 3
MIN_RELATIVE_MAE_IMPROVEMENT = 0.05
MIN_DEMAND_MAE_IMPROVEMENT_RELATIVE = 0.05
MIN_TEMPERATURE_MAE_IMPROVEMENT_C = 0.10
NATIVE_TOLERANCES = {
    "status_gap_seconds": 900,
    "sensor_age_seconds": 900,
    "outcome_match_seconds": 600,
    "timer_age_seconds": 3600,
    "weather_age_seconds": 7200,
    "supply_resolution_c": 1.0,
    "tank_resolution_c": 1.0,
    "indoor_resolution_c": 0.1,
    "setpoint_change_c": 0.0,
    "heat_state_changes": 0,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def timer_fingerprint(timer: Mapping[str, Any]) -> str:
    return evidence_hash(
        {key: timer.get(key) for key in ("status", "enabled", "slots", "timezone")}
    )


def native_scope(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    scope = {
        "policy_schema": NATIVE_POLICY_SCHEMA,
        "estimator_schema": NATIVE_ESTIMATOR_SCHEMA,
        "sensor_fingerprint": evidence_hash(snapshot["sensor_basis"]),
        "policy_fingerprint": evidence_hash(
            {
                "settings": snapshot["native_settings"],
                "timer": timer_fingerprint(snapshot["timer"]),
            }
        ),
        "schema_fingerprint": evidence_hash(snapshot["schemas"]),
        "tolerances": dict(NATIVE_TOLERANCES),
    }
    if snapshot.get("schemas", {}).get("artifact") == "demand_lifecycle_v1":
        scope["component_dependency_schema"] = "demand_p2_1_dependencies_v1"
        scope["component_dependency_generation"] = snapshot.get("demand_dependency_generation")
        scope["component_dependency_fingerprint"] = evidence_hash(
            {
                "installation_id": snapshot.get("installation_id"),
                "device_id": snapshot.get("device_id"),
                "weather_provider": snapshot.get("weather_provider"),
                "timezone": snapshot.get("timezone"),
                "tzdata_version": snapshot.get("tzdata_version"),
                "max_rate_kw": snapshot.get("max_rate_kw"),
                "dependency_generation": snapshot.get("demand_dependency_generation"),
                "feature_schema": "demand_model_weather_v3",
                "target_schema": "electrical_heating_kwh_per_hour_v1",
            }
        )
    capabilities = snapshot.get("capabilities", ())
    if (
        snapshot.get("schemas", {}).get("artifact") == "thermal_v5"
        and any(str(capability).startswith("indoor_") for capability in capabilities)
    ) or snapshot.get("schemas", {}).get("artifact") == "comfort_v8":
        scope["component_dependency_schema"] = "indoor_sensor_dependencies_v1"
        scope["component_dependency_generation"] = snapshot.get("indoor_dependency_generation")
        scope["component_dependency_fingerprint"] = evidence_hash(
            {
                "installation_id": snapshot.get("installation_id"),
                "configured_sensor_ids": sorted(snapshot.get("configured_sensor_ids", ())),
                "reference_sensor_id": snapshot.get("reference_sensor_id") or None,
                "selection_schema": "selected_indoor_sensors_v2",
                "generation": snapshot.get("indoor_dependency_generation"),
            }
        )
    return scope


@dataclass(frozen=True)
class FrozenNativeIssue:
    epoch: str
    device_id: str
    component: str
    cohort_key: str
    issued_at: dt.datetime
    horizons: tuple[int, ...]
    incumbent_hash: str | None
    candidate_hash: str
    snapshot_json: str
    predictions_json: str

    def rows(self) -> list[dict[str, Any]]:
        snapshot = json.loads(self.snapshot_json)
        predictions = json.loads(self.predictions_json)
        return [
            {
                "epoch": self.epoch,
                "device_id": self.device_id,
                "component": self.component,
                "cohort_key": self.cohort_key,
                "horizon_minutes": horizon,
                "issue_kind": "prospective_native",
                "policy_schema": NATIVE_POLICY_SCHEMA,
                "issued_at": self.issued_at,
                "target_at": self.issued_at + dt.timedelta(minutes=horizon),
                "device_day": self.issued_at.date(),
                "incumbent_hash": self.incumbent_hash,
                "candidate_hash": self.candidate_hash,
                "snapshot_hash": evidence_hash(snapshot),
                "input_snapshot": json.loads(self.snapshot_json),
                "predictions": predictions[str(horizon)],
                "expected_horizons": list(self.horizons),
            }
            for horizon in self.horizons
        ]


def estimate_native_trajectory(
    inputs: Mapping[str, Any], horizons: tuple[int, ...]
) -> dict[str, Any] | None:
    issued_at = as_utc(inputs.get("issued_at"))
    history = inputs.get("status_history", [])
    timer = inputs.get("timer", {})
    timer_at = as_utc(timer.get("fetched_at"))
    if (
        issued_at is None
        or not history
        or not horizons
        or min(horizons) <= 0
        or max(horizons) > 1440
        or timer.get("status") != "known"
        or not isinstance(timer.get("enabled"), bool)
        or timer_at is None
        or not 0 <= (issued_at - timer_at).total_seconds() <= 3600
        or (timer["enabled"] and any(slot.get("enabled") for slot in timer.get("slots", [])))
        or inputs.get("actions")
    ):
        return None
    times = [as_utc(row.get("ts")) for row in history]
    if (
        any(value is None or value > issued_at for value in times)
        or times[0] > issued_at - dt.timedelta(hours=1)
        or (issued_at - times[-1]).total_seconds() > 900
        or any(
            not 0 < (right - left).total_seconds() <= 900 for left, right in zip(times, times[1:])
        )
    ):
        return None
    latest = history[-1]
    active = latest.get("space_heating_active")
    if not isinstance(active, bool) or latest.get("space_heating_evidence") == "not_confirmed":
        return None
    if latest.get("defrost_active") is not False:
        return None
    if latest.get("device_action") == "COOLING":
        return None
    dhw = latest.get("device_action") == "HEATING_WATER" or latest.get("direction") == "WATER"
    native = inputs.get("native_settings", {})
    if any(
        native.get(key) not in (0, False) for key in ("force_dhw", "force_heater", "holiday_mode")
    ):
        return None
    if native.get("operation_status") not in (0, 1) or native.get("mode") is None:
        return None
    sensor_history = inputs.get("sensor_history", [])
    sensor_times = [as_utc(row.get("timestamp") or row.get("ts")) for row in sensor_history]
    if (
        not sensor_times
        or any(value is None or value > issued_at for value in sensor_times)
        or (issued_at - sensor_times[-1]).total_seconds() > NATIVE_TOLERANCES["sensor_age_seconds"]
    ):
        return None
    if any(
        not isinstance(native.get(key), (float, int))
        for key in ("zone1_target_temp", "tank_target_temp")
    ):
        return None
    weather = inputs.get("weather", [])
    for horizon in horizons:
        target = issued_at + dt.timedelta(minutes=horizon)
        matching = [
            row
            for row in weather
            if (timestamp := as_utc(row.get("ts"))) is not None
            and abs((timestamp - target).total_seconds()) <= 3600
        ]
        if not matching:
            return None
        for row in matching:
            retrieved = as_utc(row.get("forecast_issued_at"))
            if retrieved is None or not 0 <= (issued_at - retrieved).total_seconds() <= 7200:
                return None
            if not isinstance(row.get("temperature"), (int, float)) or not math.isfinite(
                row["temperature"]
            ):
                return None
    supply = latest.get("zone1_temp")
    if not isinstance(supply, (int, float)) or not math.isfinite(supply):
        return None
    return {
        "schema": NATIVE_ESTIMATOR_SCHEMA,
        "assumption": "last_confirmed_native_state_and_supply_persist",
        "steps": [
            {
                "minute": minute,
                "space_heating_active": active,
                "dhw_active": dhw,
                "zone_supply_c": float(supply),
            }
            for minute in range(15, max(horizons) + 15, 15)
        ],
    }


def freeze_native_pair(
    inputs: Mapping[str, Any],
    *,
    epoch: str,
    component: str,
    candidate_hash: str,
    incumbent_hash: str | None,
    horizons: tuple[int, ...],
    candidate_predictor: Callable,
    incumbent_predictor: Callable,
    baseline_predictor: Callable,
) -> FrozenNativeIssue | None:
    snapshot = json.loads(_canonical_json(dict(inputs)))
    trajectory = estimate_native_trajectory(snapshot, horizons)
    if trajectory is None:
        return None
    if component in {"comfort", "demand"} and trajectory["steps"][0]["dhw_active"]:
        return None
    snapshot["policy_schema"] = NATIVE_POLICY_SCHEMA
    snapshot["expected_native_trajectory"] = trajectory
    snapshot["tolerances"] = dict(NATIVE_TOLERANCES)
    snapshot["scope"] = native_scope(snapshot)
    frozen = _canonical_json(snapshot)
    predictions = {}
    for name, predictor in (
        ("candidate", candidate_predictor),
        ("incumbent", incumbent_predictor),
        ("baseline", baseline_predictor),
    ):
        values = predictor(json.loads(frozen), horizons)
        if set(values) != set(horizons) or any(
            not math.isfinite(float(value)) for value in values.values()
        ):
            return None
        for horizon in horizons:
            predictions.setdefault(str(horizon), {})[name] = float(values[horizon])
    issued_at = as_utc(snapshot["issued_at"])
    cohort = evidence_hash(
        {
            "device_id": snapshot["device_id"],
            "component": component,
            "issued_at": issued_at.isoformat(),
            "capability": snapshot["capability"],
        }
    )
    return FrozenNativeIssue(
        epoch,
        snapshot["device_id"],
        component,
        cohort,
        issued_at,
        horizons,
        incumbent_hash,
        candidate_hash,
        frozen,
        _canonical_json(predictions),
    )


def native_trace_exclusion(
    issue: Mapping[str, Any], trace: Mapping[str, Any]
) -> tuple[str, str] | None:
    if (
        issue["issue_kind"] != "prospective_native"
        or issue["policy_schema"] != NATIVE_POLICY_SCHEMA
    ):
        return "excluded", "not_prospective_native"
    if issue["input_snapshot"].get("unavailable_reason"):
        return "excluded", "unconstructable_native_policy"
    if trace.get("lookup_complete") is not True or trace.get("overflow"):
        return "incomplete", "execution_lookup_incomplete"
    if (
        trace.get("overrides") is None
        or trace.get("restores") is None
        or trace.get("actions") is None
    ):
        return "incomplete", "execution_lookup_incomplete"
    if trace["overrides"]:
        return "excluded", "override"
    if trace["restores"]:
        return "excluded", "unresolved_restore"
    for action in trace["actions"]:
        if action.get("never_dispatched") is True:
            continue
        if action.get("dispatched") is True:
            return "excluded", "unmatched_action"
        return "incomplete", "unknown_dispatch"
    snapshot = issue["input_snapshot"]
    if trace.get("scope") != snapshot["scope"]:
        return "excluded", "incompatible_scope"
    statuses = trace.get("statuses", [])
    start, end = issue["issued_at"], issue["target_at"]
    times = [as_utc(row.get("ts")) for row in statuses]
    if (
        not times
        or any(value is None for value in times)
        or not 0 <= (start - times[0]).total_seconds() <= 900
        or abs((end - times[-1]).total_seconds()) > 600
        or any(
            not 0 < (right - left).total_seconds() <= 900 for left, right in zip(times, times[1:])
        )
    ):
        return "excluded", "stale_or_gapped_telemetry"
    expected = snapshot["expected_native_trajectory"]["steps"][0]
    for status, timestamp in zip(statuses, times):
        if status.get("device_id") != issue["device_id"]:
            return "excluded", "device_scope_mismatch"
        provenance = status.get("native_policy_provenance")
        if not isinstance(provenance, dict) or provenance.get("schema") != NATIVE_POLICY_SCHEMA:
            return "excluded", "unknown_execution"
        timer = provenance.get("timer", {})
        timer_at = as_utc(timer.get("fetched_at"))
        if (
            timer.get("status") != "known"
            or timer_at is None
            or not 0 <= (timestamp - timer_at).total_seconds() <= 3600
        ):
            return "excluded", "unknown_timer"
        if timer_fingerprint(timer) != timer_fingerprint(snapshot["timer"]):
            return "excluded", "timer_effect"
        if provenance.get("settings") != snapshot["native_settings"]:
            return "excluded", "native_policy_changed"
        dhw = status.get("device_action") == "HEATING_WATER" or status.get("direction") == "WATER"
        if dhw != expected["dhw_active"] or (dhw and issue["component"] in {"comfort", "demand"}):
            return "excluded", "dhw_diversion"
        active = status.get("space_heating_active")
        if not isinstance(active, bool) or status.get("space_heating_evidence") == "not_confirmed":
            return "excluded", "unknown_execution"
        if status.get("defrost_active") is not False:
            return "excluded", "unknown_execution"
        if active != expected["space_heating_active"]:
            return "excluded", "native_trajectory_mismatch"
        supply = status.get("zone1_temp")
        if not isinstance(supply, (int, float)) or not math.isfinite(supply):
            return "excluded", "unknown_execution"
        if abs(supply - expected["zone_supply_c"]) > NATIVE_TOLERANCES["supply_resolution_c"]:
            return "excluded", "native_trajectory_mismatch"
    return None


def reconcile_native_issue(
    issue: Mapping[str, Any],
    trace: Mapping[str, Any],
    *,
    observed: float | None,
    observed_at: dt.datetime | None,
    now: dt.datetime,
) -> dict[str, Any]:
    result = {"status": "pending", "exclusion_reason": None, "observed_at": None, "result": {}}
    if issue["target_at"] > now:
        return result
    exclusion = native_trace_exclusion(issue, trace)
    if exclusion:
        result.update(status=exclusion[0], exclusion_reason=exclusion[1])
        return result
    if (
        observed is None
        or observed_at is None
        or not math.isfinite(observed)
        or observed_at > now
        or abs((observed_at - issue["target_at"]).total_seconds()) > 600
    ):
        result.update(status="incomplete", exclusion_reason="missing_or_invalid_target")
        return result
    snapshot = issue["input_snapshot"]
    weather = min(
        snapshot["weather"],
        key=lambda row: abs((as_utc(row["ts"]) - issue["target_at"]).total_seconds()),
    )
    predictions = issue["predictions"]
    result.update(
        status="valid",
        observed_at=observed_at,
        result={
            "observed": observed,
            "observed_at": observed_at.isoformat(),
            "issued_at": issue["issued_at"].isoformat(),
            "target_at": issue["target_at"].isoformat(),
            "device_id": issue["device_id"],
            "cohort_key": issue["cohort_key"],
            "canonical_key": issue["cohort_key"],
            "horizon_minutes": issue["horizon_minutes"],
            "issue_kind": issue["issue_kind"],
            "policy_schema": issue["policy_schema"],
            "candidate_hash": issue["candidate_hash"],
            "incumbent_hash": issue["incumbent_hash"],
            "outdoor_bin": math.floor(weather["temperature"] / 2) * 2,
            "scope": snapshot["scope"],
            "capability": snapshot["capability"],
            **predictions,
        },
    )
    return result


def decision_checkpoint(
    frozen_at: dt.datetime,
    now: dt.datetime,
    completed: tuple[int, ...] = (),
) -> int | None:
    for index, hours in enumerate(DECISION_OFFSETS_HOURS):
        if index not in completed and now >= frozen_at + dt.timedelta(hours=hours):
            return index
    return None


def paired_block_bootstrap(
    pairs: list[dict[str, Any]],
    comparator: str,
    *,
    seed: int,
    alpha: float,
    cluster_days: set[tuple[str, dt.date]] | None = None,
) -> dict[str, float | int]:
    if len(pairs) > MAX_QUALIFICATION_PAIRS or not 0 < alpha <= 0.05:
        raise ValueError("Bootstrap resource or alpha bound exceeded")
    days = {}
    for pair in pairs:
        day = as_utc(pair["issued_at"]).date()
        key = (pair["device_id"], day)
        candidate = abs(pair["candidate"] - pair["observed"])
        improvement = abs(pair[comparator] - pair["observed"]) - candidate
        total, count = days.get(key, (0.0, 0))
        days[key] = (total + improvement, count + 1)
    for key in cluster_days or ():
        days.setdefault(key, (0.0, 0))
    blocks = [
        (
            days[key][0] + days[(key[0], key[1] + dt.timedelta(days=1))][0],
            days[key][1] + days[(key[0], key[1] + dt.timedelta(days=1))][1],
        )
        for key in sorted(days)
        if (key[0], key[1] + dt.timedelta(days=1)) in days
        and days[key][1] + days[(key[0], key[1] + dt.timedelta(days=1))][1] > 0
    ]
    if len(blocks) < 7:
        raise ValueError("Insufficient contiguous 48-hour blocks")
    totals = np.asarray([value[0] for value in blocks])
    counts = np.asarray([value[1] for value in blocks])
    draws = math.ceil(len(days) / BOOTSTRAP_BLOCK_DAYS)
    rng = np.random.default_rng(seed)
    resampled = np.empty(BOOTSTRAP_RESAMPLES)
    for offset in range(0, BOOTSTRAP_RESAMPLES, BOOTSTRAP_CHUNK):
        size = min(BOOTSTRAP_CHUNK, BOOTSTRAP_RESAMPLES - offset)
        indices = rng.integers(0, len(blocks), size=(size, draws))
        resampled[offset : offset + size] = totals[indices].sum(axis=1) / counts[indices].sum(
            axis=1
        )
    ordered = np.sort(resampled)
    support_bound = float(np.min(totals / counts))
    allocated_bound = float(ordered[max(0, math.floor(alpha * BOOTSTRAP_RESAMPLES) - 1)])
    return {
        "lb95": float(ordered[max(0, math.floor(0.05 * BOOTSTRAP_RESAMPLES) - 1)]),
        "sequential_lower_bound": min(support_bound, allocated_bound),
        "finite_resample_support_bound": support_bound,
        "alpha": alpha,
        "resamples": BOOTSTRAP_RESAMPLES,
        "moving_block_hours": 48,
        "blocks": len(blocks),
    }


def qualify_prospective(
    pairs: list[dict[str, Any]],
    *,
    component: str,
    candidate_hash: str,
    frozen_at: dt.datetime,
    data_cutoff: dt.datetime,
    now: dt.datetime,
    horizons: tuple[int, ...],
    scope: dict[str, Any],
    capabilities: tuple[str, ...],
    coverage_complete: bool,
    checkpoint: int | None = None,
    completed: tuple[int, ...] = (),
) -> dict[str, Any]:
    result = {
        "qualified": False,
        "control_ready": False,
        "ordinary_writes_allowed": False,
        "unit": "kW" if component == "demand" else "degC",
        "policy": QUALIFICATION_POLICY_SCHEMA,
        "reason": "insufficient_evidence",
        "exclusions": {},
        "counts": {},
    }
    if not coverage_complete or len(pairs) > MAX_QUALIFICATION_PAIRS:
        return {**result, "reason": "incomplete_coverage"}
    checkpoint = (
        decision_checkpoint(frozen_at, now, completed) if checkpoint is None else checkpoint
    )
    if (
        checkpoint is None
        or checkpoint in completed
        or not 0 <= checkpoint < len(DECISION_OFFSETS_HOURS)
        or now < frozen_at + dt.timedelta(hours=DECISION_OFFSETS_HOURS[checkpoint])
    ):
        return {**result, "reason": "decision_not_due"}
    result["checkpoint"] = checkpoint
    issue_cutoff = frozen_at + dt.timedelta(hours=DECISION_OFFSETS_HOURS[checkpoint] - 24)
    excluded = defaultdict(int)
    by_horizon = defaultdict(lambda: defaultdict(int))
    by_bin = defaultdict(lambda: defaultdict(int))
    valid = []
    seen = {}
    for pair in pairs:
        reason = pair.get("exclusion_reason")
        issued = as_utc(pair.get("issued_at"))
        target = as_utc(pair.get("target_at"))
        observed_at = as_utc(pair.get("observed_at"))
        if (
            pair.get("issue_kind") != "prospective_native"
            or pair.get("policy_schema") != NATIVE_POLICY_SCHEMA
        ):
            reason = "not_prospective_native"
        elif pair.get("candidate_hash") != candidate_hash or pair.get("scope") != scope:
            return {**result, "reason": "incompatible_scope"}
        elif pair.get("status", "valid") in {"pending", "incomplete"}:
            return {**result, "reason": "incomplete_coverage"}
        elif issued is None or target is None or issued < frozen_at or issued < data_cutoff:
            reason = "fit_or_holdout_overlap"
        elif issued > issue_cutoff or target > now:
            continue
        elif (
            observed_at is None
            or observed_at > now
            or abs((observed_at - target).total_seconds()) > 600
        ):
            reason = "invalid_target_time"
        elif (
            pair.get("horizon_minutes") not in horizons
            or pair.get("capability") not in capabilities
        ):
            reason = "undeclared_capability"
        elif any(
            not isinstance(pair.get(key), (int, float)) or not math.isfinite(pair[key])
            for key in ("observed", "candidate", "incumbent", "baseline")
        ):
            reason = "non_finite_pair"
        if reason or pair.get("status") == "excluded":
            reason = pair.get("exclusion_reason") or reason or "excluded"
            excluded[reason] += 1
            by_horizon[str(pair.get("horizon_minutes"))][reason] += 1
            by_bin[str(pair.get("outdoor_bin"))][reason] += 1
            continue
        key = (
            pair["device_id"],
            pair["canonical_key"],
            pair["horizon_minutes"],
            pair["capability"],
        )
        if key in seen:
            if evidence_hash(pair) != evidence_hash(seen[key]):
                return {**result, "reason": "conflicting_duplicate"}
            continue
        seen[key] = pair
        valid.append(pair)
    result["exclusions"] = dict(excluded)
    result["exclusions_by_horizon"] = {key: dict(value) for key, value in by_horizon.items()}
    result["exclusions_by_outdoor_bin"] = {key: dict(value) for key, value in by_bin.items()}
    clusters = {(pair["device_id"], as_utc(pair["issued_at"]).date()) for pair in valid}
    cohorts = {(pair["device_id"], pair["cohort_key"]) for pair in valid}
    horizon_counts = {str(h): sum(pair["horizon_minutes"] == h for pair in valid) for h in horizons}
    bin_counts = {
        str(outdoor): sum(pair["outdoor_bin"] == outdoor for pair in valid)
        for outdoor in sorted({pair["outdoor_bin"] for pair in valid})
    }
    result["counts"] = {
        "pairs": len(valid),
        "device_day_clusters": len(clusters),
        "issue_cohorts": len(cohorts),
        "horizons": horizon_counts,
        "outdoor_bins": bin_counts,
    }
    if (
        now - frozen_at < dt.timedelta(days=14)
        or frozen_at < data_cutoff
        or len(valid) < MIN_PROSPECTIVE_PAIRS
        or len(clusters) < MIN_DEVICE_DAY_CLUSTERS
        or len(cohorts) < MIN_ISSUE_COHORTS
        or not horizons
        or not capabilities
        or any(count < 12 for count in horizon_counts.values())
        or sum(count >= 12 for count in bin_counts.values()) < 3
    ):
        return result
    latest = max(as_utc(pair["observed_at"]) for pair in valid)
    if now >= latest + dt.timedelta(hours=72):
        return {**result, "reason": "outcomes_expired", "last_valid_outcome_at": latest.isoformat()}
    groups = {"overall": valid}
    for capability in capabilities:
        selected = [pair for pair in valid if pair["capability"] == capability]
        if len(selected) < 60 or any(
            sum(pair["horizon_minutes"] == h for pair in selected) < 12 for h in horizons
        ):
            return {**result, "reason": "capability_insufficient_evidence"}
        groups[capability] = selected
        for horizon in horizons:
            groups[f"{capability}:horizon:{horizon}"] = [
                pair for pair in selected if pair["horizon_minutes"] == horizon
            ]
    for outdoor, count in bin_counts.items():
        if count >= 12:
            groups[f"outdoor:{outdoor}"] = [
                pair for pair in valid if str(pair["outdoor_bin"]) == outdoor
            ]
    alpha = QUALIFICATION_FAMILY_ALPHA / (len(DECISION_OFFSETS_HOURS) * len(groups) * 2)
    evidence = {}
    for name, selected in groups.items():
        actual = np.asarray([pair["observed"] for pair in selected])
        prediction = np.asarray([pair["candidate"] for pair in selected])
        candidate_errors = np.abs(prediction - actual)
        if np.var(actual) <= 1e-12:
            return {**result, "reason": "flat_evidence"}
        if component != "demand":
            bucket = score_bucket(
                candidate_errors.tolist(),
                (prediction - actual).tolist(),
                prediction.tolist(),
                actual.tolist(),
                [pair["baseline"] - pair["observed"] for pair in selected],
            )
            maximum_mae = 0.60 if component == "comfort" else MAX_GATE_MAE_C
            if (
                bucket["mae"] > maximum_mae
                or bucket["r2"] is None
                or bucket["r2"] < 0.15
                or abs(bucket["bias"]) > 0.50
                or bucket["p90_abs_error"] > 2.0
            ):
                return {**result, "reason": "temperature_reporting_gate"}
        evidence[name] = {}
        for comparator in ("incumbent", "baseline"):
            other_errors = np.asarray(
                [abs(pair[comparator] - pair["observed"]) for pair in selected]
            )
            improvement = float(np.mean(other_errors - candidate_errors))
            other_mae = float(np.mean(other_errors))
            relative = improvement / other_mae if other_mae > 0 else 0.0
            if np.var(other_errors - candidate_errors) <= 1e-12:
                return {**result, "reason": "flat_evidence"}
            seed = int(
                evidence_hash(
                    {
                        "candidate": candidate_hash,
                        "group": name,
                        "comparator": comparator,
                        "checkpoint": checkpoint,
                    }
                )[:16],
                16,
            )
            try:
                interval = paired_block_bootstrap(
                    selected, comparator, seed=seed, alpha=alpha, cluster_days=clusters
                )
            except ValueError:
                return {**result, "reason": "insufficient_contiguous_blocks"}
            evidence[name][comparator] = {
                **interval,
                "mean_improvement": improvement,
                "candidate_mae": float(np.mean(candidate_errors)),
                "relative_mae_improvement": relative,
            }
            absolute = (
                MIN_DEMAND_MAE_IMPROVEMENT_RELATIVE * other_mae
                if component == "demand"
                else MIN_TEMPERATURE_MAE_IMPROVEMENT_C
            )
            evidence[name][comparator]["absolute_mae_requirement"] = absolute
            if (
                relative < MIN_RELATIVE_MAE_IMPROVEMENT
                or improvement < absolute
                or interval["lb95"] <= 0
                or interval["sequential_lower_bound"] <= 0
            ):
                return {**result, "reason": "no_prospective_skill", "evidence": evidence}
    return {
        **result,
        "qualified": True,
        "reason": "qualified_for_reporting",
        "evidence": evidence,
        "last_valid_outcome_at": latest.isoformat(),
        "expires_at": (latest + dt.timedelta(hours=72)).isoformat(),
        "scope": {
            **scope,
            "horizons": list(horizons),
            "capabilities": list(capabilities),
            "outdoor_bins": [int(key) for key, count in bin_counts.items() if count >= 12],
            "qualification_policy": QUALIFICATION_POLICY_SCHEMA,
        },
        "evidence_hash": evidence_hash(valid),
    }


def _plain_record(row: Any, model: Any) -> dict[str, Any]:
    result = {}
    for column in model.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = (
            value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else value
        )
    return result


@asynccontextmanager
async def locked_demand_scope(device_id: str, *, session=None):
    """Keep dependency settings stable through the caller's publication boundary."""
    from packages.core.house_contract import _lock_contract

    async with get_session() if session is None else nullcontext(session) as session:
        await _lock_contract(session)
        inputs = await materialize_native_inputs(
            "demand", now=dt.datetime.now(dt.timezone.utc), device_id=device_id, session=session
        )
        if inputs is None:
            raise ValueError("current_demand_scope_unavailable")
        yield native_scope(inputs), session


@asynccontextmanager
async def locked_native_scope(
    component: str,
    *,
    device_id: str,
    capabilities: tuple[str, ...] = (),
    timer_reader: Any | None = None,
    session=None,
):
    """Freeze component dependencies under the same lock used by settings writers."""
    from packages.core.house_contract import _lock_contract

    async with get_session() if session is None else nullcontext(session) as session:
        await _lock_contract(session)
        inputs = await materialize_native_inputs(
            component,
            now=dt.datetime.now(dt.timezone.utc),
            device_id=device_id,
            capabilities=capabilities,
            timer_reader=timer_reader,
            session=session,
        )
        if inputs is None:
            raise ValueError(f"current_{component}_scope_unavailable")
        yield native_scope(inputs), session


async def materialize_native_inputs(
    component: str,
    *,
    now: dt.datetime,
    device_id: str | None = None,
    timer_reader: Any | None = None,
    capabilities: tuple[str, ...] = (),
    session=None,
    context: dict | None = None,
    raw: bool = False,
) -> dict[str, Any] | None:
    from packages.core.price_scope import UNAVAILABLE_AREA, resolve_price_area
    from packages.core.settings_service import get_all_settings
    from packages.poller.smartthings import get_selected_device_ids

    values = (
        context["settings"]
        if context is not None
        else (
            await get_all_settings(session=session)
            if session is not None
            else await get_all_settings()
        )
    )
    selected_ids = await get_selected_device_ids(values=values)
    reference_sensor = str(values.get("comfort_reference_sensor_id") or "").strip()
    reference_blocked = bool(
        reference_sensor and selected_ids and reference_sensor not in selected_ids
    )
    if reference_blocked and (
        component == "comfort"
        or component == "thermal"
        and any(name.startswith("indoor_") for name in capabilities)
    ):
        return None
    query_sensor_ids = [reference_sensor] if reference_sensor else selected_ids
    if timer_reader is not None:
        timer = timer_reader.cached_native_timer_provenance()
    else:
        timer = None
    demand_contract = None
    demand_dependency_generation = None
    indoor_dependency_generation = None
    installation_id = None
    since = now - dt.timedelta(hours=2)
    async with get_session() if session is None else nullcontext(session) as session:
        query = select(DeviceStatusRecord).where(
            DeviceStatusRecord.ts >= since, DeviceStatusRecord.ts <= now
        )
        if device_id is not None:
            query = query.where(DeviceStatusRecord.device_id == device_id)
        statuses = (
            (await session.execute(query.order_by(DeviceStatusRecord.ts).limit(1001)))
            .scalars()
            .all()
        )
        if not statuses or len(statuses) > 1000:
            return None
        devices = {row.device_id for row in statuses}
        if len(devices) != 1:
            return None
        latest = statuses[-1]
        device_id = latest.device_id
        if component == "demand":
            from packages.core.house_contract import ensure_current_contract

            demand_contract = await ensure_current_contract(session)
            if demand_contract.contract_json.get("timezone") != str(
                values.get("timezone") or "Europe/Amsterdam"
            ):
                return None
            installation = await session.get(HouseInstallationRecord, 1)
            if installation is None:
                return None
            demand_dependency_generation = installation.demand_dependency_generation
            installation_id = str(installation.installation_id)
        elif component == "comfort" or component == "thermal":
            installation = await session.get(HouseInstallationRecord, 1)
            if installation is None:
                return None
            indoor_dependency_generation = installation.indoor_dependency_generation
            installation_id = str(installation.installation_id)
        weather_provider = str(values.get("weather_provider") or "open-meteo")
        weather = (
            (
                await session.execute(
                    select(WeatherRecord)
                    .where(
                        WeatherRecord.source == weather_provider,
                        WeatherRecord.ts >= now - dt.timedelta(hours=1),
                        WeatherRecord.ts <= now + dt.timedelta(hours=24),
                        WeatherRecord.forecast_issued_at <= now,
                        WeatherRecord.forecast_issued_at >= now - dt.timedelta(hours=2),
                    )
                    .order_by(WeatherRecord.ts)
                    .limit(101)
                )
            )
            .scalars()
            .all()
        )
        price_area = await resolve_price_area(values, session=session)
        prices = (
            []
            if price_area == UNAVAILABLE_AREA
            else (
                (
                    await session.execute(
                        select(PriceRecord)
                        .where(
                            PriceRecord.area == price_area,
                            PriceRecord.ts >= now - dt.timedelta(hours=1),
                            PriceRecord.ts <= now + dt.timedelta(hours=24),
                            PriceRecord.fetched_at <= now,
                        )
                        .order_by(PriceRecord.ts)
                        .limit(101)
                    )
                )
                .scalars()
                .all()
            )
        )
        indoor_query = (
            select(IndoorTempReading)
            .where(
                IndoorTempReading.timestamp >= since,
                IndoorTempReading.timestamp <= now,
                IndoorTempReading.is_stale.is_(False),
            )
            .order_by(IndoorTempReading.timestamp)
            .limit(5001)
        )
        if query_sensor_ids:
            indoor_query = indoor_query.where(IndoorTempReading.device_id.in_(query_sensor_ids))
        raw_indoor = (
            [] if reference_blocked else (await session.execute(indoor_query)).scalars().all()
        )
        counters = (
            (
                await session.execute(
                    select(ConsumptionRecord)
                    .where(
                        ConsumptionRecord.device_id == device_id,
                        ConsumptionRecord.ts >= since,
                        ConsumptionRecord.ts <= now,
                    )
                    .order_by(ConsumptionRecord.ts)
                    .limit(101)
                )
            )
            .scalars()
            .all()
        )
        if max(len(weather), len(prices), len(counters)) > 100 or len(raw_indoor) > 5000:
            return None
        data = {
            "component": component,
            "now": now,
            "device_id": device_id,
            "values": dict(values),
            "selected_ids": list(selected_ids),
            "reference_sensor": reference_sensor,
            "reference_blocked": reference_blocked,
            "weather_provider": weather_provider,
            "price_area": price_area,
            "timer": timer,
            "capabilities": capabilities,
            "house_contract_context": (
                {
                    "installation_id": installation_id,
                    "tzdata_version": (
                        demand_contract.contract_json.get("tzdata_version")
                        if demand_contract is not None
                        else None
                    ),
                    "demand_dependency_generation": demand_dependency_generation,
                    "indoor_dependency_generation": indoor_dependency_generation,
                }
                if installation_id is not None
                else {}
            ),
        }
        for name, rows, model in (
            ("statuses", statuses, DeviceStatusRecord),
            ("weather", weather, WeatherRecord),
            ("prices", prices, PriceRecord),
            ("raw_indoor", raw_indoor, IndoorTempReading),
            ("counters", counters, ConsumptionRecord),
        ):
            data[name] = await copy_materialized_rows(
                rows, [column.name for column in model.__table__.columns], context=context
            )
    return data if raw else _prepare_native_inputs(data)


def _prepare_native_inputs(data: dict, *, component=None, capabilities=None) -> dict | None:
    from packages.core.device_status_snapshot import native_policy_settings
    from packages.ml.comfort_model import ComfortModel, SENSOR_SELECTION_SCHEMA
    from packages.ml.lifecycle import LIFECYCLE_SCHEMAS

    component = component or data["component"]
    capabilities = data["capabilities"] if capabilities is None else capabilities
    now, values, device_id = data["now"], data["values"], data["device_id"]
    selected_ids, reference_sensor = data["selected_ids"], data["reference_sensor"]
    reference_blocked = data["reference_blocked"]
    if reference_blocked and (
        component == "comfort"
        or component == "thermal"
        and any(name.startswith("indoor_") for name in capabilities)
    ):
        return None
    statuses, weather, prices, counters = (
        data[name] for name in ("statuses", "weather", "prices", "counters")
    )
    latest, since, timer = statuses[-1], now - dt.timedelta(hours=2), data["timer"]
    weather_provider = data["weather_provider"]
    house_contract_context = data.get("house_contract_context", {})
    raw_indoor = [
        row
        for row in data["raw_indoor"]
        if (not selected_ids or row.device_id in selected_ids)
        and (not reference_sensor or row.device_id == reference_sensor)
        and _finite_number(row.temperature)
        and 5 <= row.temperature <= 35
        and not row.is_stale
        and (timestamp := as_utc(row.timestamp)) is not None
        and since <= timestamp <= now
    ]
    readings, strategy, _ = ComfortModel._select_indoor_observations(
        raw_indoor, reference_sensor, selected_ids=selected_ids, as_of=now
    )
    sensor_ids = (
        [reference_sensor]
        if reference_sensor
        else sorted(set(selected_ids) or {row.device_id for row in raw_indoor})
    )
    fresh_ids = {
        row.device_id
        for row in raw_indoor
        if 0 <= (now - as_utc(row.timestamp)).total_seconds() <= 900
    }
    indoor_available = bool(
        readings
        and sensor_ids
        and set(sensor_ids) <= fresh_ids
        and not reference_blocked
        and 0 <= (now - as_utc(readings[-1].timestamp)).total_seconds() <= 900
    )
    indoor_basis = {
        "kind": strategy,
        "sensor_ids": sensor_ids,
        "resolution_c": 0.1,
        "selection_schema": SENSOR_SELECTION_SCHEMA,
        "selected_sensor_ids": sorted(set(selected_ids)),
    }
    indoor_history = [
        {"timestamp": row.timestamp.isoformat(), "temperature": row.temperature} for row in readings
    ]
    status_history = [_plain_record(row, DeviceStatusRecord) for row in statuses]
    native = native_policy_settings(latest)
    provenance = latest.native_policy_provenance or {}
    timer = (
        timer
        if isinstance(timer, dict) and timer.get("status") == "known"
        else provenance.get("timer", {"status": "unknown"})
    )
    dhw = latest.direction == "WATER" or latest.device_action == "HEATING_WATER"
    if component == "thermal":
        tank_capability = "tank_heating" if dhw else "tank_cooling"
        indoor_capability = "indoor_heating" if latest.space_heating_active else "indoor_cooling"
        capability = (
            tank_capability
            if not capabilities or tank_capability in capabilities
            else indoor_capability
        )
        if capabilities and capability not in capabilities:
            return None
    else:
        capability = "indoor" if component == "comfort" else "demand"
    if component == "comfort" or capability.startswith("indoor_"):
        if not indoor_available:
            return None
        basis, history = indoor_basis, indoor_history
    elif component == "demand":
        basis = {"kind": "heatpump_counter", "sensor_ids": [device_id], "counter": "heat_kwh"}
        history = [_plain_record(row, ConsumptionRecord) for row in counters]
    else:
        basis = {
            "kind": "heatpump_temperature",
            "sensor_ids": [device_id],
            "field": "tank_temp",
            "resolution_c": 1.0,
        }
        history = [
            {"timestamp": row.ts.isoformat(), "temperature": row.tank_temp} for row in statuses
        ]
    current_indoor = readings[-1].temperature if indoor_available else None
    older = [row for row in readings if as_utc(row.timestamp) <= now - dt.timedelta(hours=1)]
    trend = (
        (
            (current_indoor - older[-1].temperature)
            / max(
                (as_utc(readings[-1].timestamp) - as_utc(older[-1].timestamp)).total_seconds()
                / 3600,
                1.0,
            )
        )
        if indoor_available and older
        else None
    )
    if component == "comfort" and not _finite_number(trend):
        return None
    max_rate_kw = None
    if component == "demand":
        from packages.ml.demand_model_core import DemandModel

        max_rate_kw = DemandModel._max_plausible_rate_kw()
    return {
        "issued_at": now.isoformat(),
        "device_id": device_id,
        "capability": capability,
        "sensor_basis": basis,
        "sensor_history": history,
        "status_history": status_history,
        "native_settings": native,
        "timer": timer,
        "weather": [_plain_record(row, WeatherRecord) for row in weather],
        "pricing": {
            "area": data.get("price_area"),
            "available": bool(prices),
            "points": [_plain_record(row, PriceRecord) for row in prices],
        },
        "indoor_temperature": current_indoor,
        "indoor_trend": trend,
        "tank_temperature": latest.tank_temp,
        "schemas": {
            "input": "frozen_native_inputs_v2"
            if component == "comfort" or capability.startswith("indoor_")
            else "frozen_native_inputs_v1",
            "artifact": LIFECYCLE_SCHEMAS[component],
            "weather_provider": weather_provider,
            "price_provider": str(values.get("price_provider") or "unknown"),
        },
        "reference_sensor_id": reference_sensor,
        "configured_sensor_ids": selected_ids,
        "indoor_sensor_basis": indoor_basis if indoor_available else None,
        "indoor_sensor_history": indoor_history if indoor_available else [],
        "weather_provider": weather_provider,
        "timezone": str(values.get("timezone") or "Europe/Amsterdam"),
        "max_rate_kw": max_rate_kw,
        "tzdata_version": house_contract_context.get("tzdata_version"),
        "installation_id": house_contract_context.get("installation_id"),
        "demand_dependency_generation": house_contract_context.get("demand_dependency_generation"),
        "indoor_dependency_generation": house_contract_context.get("indoor_dependency_generation"),
        "capabilities": tuple(capabilities),
    }


def predict_native(
    component: str,
    payload: dict[str, Any] | None,
    snapshot: dict[str, Any],
    horizons: tuple[int, ...],
    *,
    baseline: bool = False,
) -> dict[int, float]:
    from packages.ml.comfort_model import ComfortModel
    from packages.ml.demand_model_core import DemandModel
    from packages.ml.thermal import ThermalModel, _RATE_COMPONENTS

    issued = as_utc(snapshot["issued_at"])
    expected = snapshot["expected_native_trajectory"]["steps"][0]
    predictions = {}
    for horizon in horizons:
        target = issued + dt.timedelta(minutes=horizon)
        weather = min(
            snapshot["weather"], key=lambda row: abs((as_utc(row["ts"]) - target).total_seconds())
        )
        outdoor = float(weather["temperature"])
        if component == "demand":
            if baseline:
                prediction = float(payload["baseline_kw"])
            elif payload is None:
                prediction = max(0.5, 3.0 - 0.1 * outdoor)
            else:
                features = DemandModel._make_features(
                    outdoor,
                    weather.get("wind_speed") or 0.0,
                    weather.get("irradiance") or 0.0,
                    target.hour,
                    target.weekday(),
                    weather.get("precipitation") or 0.0,
                    weather.get("humidity") or 60.0,
                    weather.get("cloud_cover") or 0.0,
                )
                prediction = max(0.0, float(payload["median"].predict(features.reshape(1, -1))[0]))
        elif component == "comfort":
            current = float(snapshot["indoor_temperature"])
            if payload is None or baseline:
                prediction = current
            else:
                active = float(expected["space_heating_active"])
                features = ComfortModel._controlled_window_features(
                    mean_supply_lift=max(expected["zone_supply_c"] - current, 0.0) * active,
                    mean_target_lift=max(
                        snapshot["native_settings"]["zone1_target_temp"] - current, 0.0
                    )
                    * active,
                    window_heat_fraction=active,
                    final_hour_heat_fraction=active,
                    outdoor_temp=outdoor,
                    wind_speed=weather.get("wind_speed") or 0.0,
                    irradiance=weather.get("irradiance") or 0.0,
                    precipitation=weather.get("precipitation") or 0.0,
                    humidity=weather.get("humidity") or 60.0,
                    cloud_cover=weather.get("cloud_cover") or 0.0,
                    hour=target.hour,
                    issue_indoor_temp=current,
                    issue_indoor_trend=snapshot["indoor_trend"],
                )
                prediction = current + float(
                    payload["direct_models"][horizon].predict(features.reshape(1, -1))[0]
                )
        else:
            capability = snapshot["capability"]
            current = float(
                snapshot["tank_temperature"]
                if capability.startswith("tank_")
                else snapshot["indoor_temperature"]
            )
            if baseline:
                prediction = current
            else:
                model = ThermalModel()
                if payload is not None:
                    model.params = model._decode_params(payload["params"])
                prediction = (
                    current
                    + getattr(model, _RATE_COMPONENTS[capability][2])(outdoor) * horizon / 60
                )
        predictions[horizon] = prediction
    return predictions


def check_exact_fit(component: str, payload: dict[str, Any]) -> None:
    from packages.ml.thermal import ThermalModel, _RATE_COMPONENTS

    if component == "demand":
        evaluation = payload["evaluation"]
        for name in ("median", "lower", "upper"):
            if not np.array_equal(
                payload[name].predict(evaluation["features"]), evaluation["predictions"][name]
            ):
                raise ValueError("Demand evaluated-fit round trip mismatch")
    elif component == "comfort":
        for name, evaluation in payload["evaluation"].items():
            family, horizon = name.split(":")
            models = (
                payload["direct_models"]
                if family == "controlled"
                else payload["passive_direct_models"]
            )
            if not np.array_equal(
                models[int(horizon)].predict(evaluation["features"]), evaluation["predictions"]
            ):
                raise ValueError("Comfort evaluated-fit round trip mismatch")
    else:
        model = ThermalModel()
        model.params = model._decode_params(payload["params"])
        for capability in payload["declared_components"]:
            evaluation = payload["validation"][capability]
            predicted = [
                getattr(model, _RATE_COMPONENTS[capability][2])(outdoor) * hours
                for outdoor, hours in evaluation["holdout_features"]
            ]
            if not np.array_equal(predicted, evaluation["holdout_predictions"]):
                raise ValueError("Thermal evaluated-fit round trip mismatch")


async def materialize_execution_page(page: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    from packages.core.safety_reverts import (
        historical_revert_overlap_rows,
        unresolved_revert_device_ids,
    )

    issues = [entry["issue"] for entry in page["rows"]]
    if not issues:
        return {
            "complete": True,
            "statuses": [],
            "readings": [],
            "counters": [],
            "actions": [],
            "overrides": [],
            "restores": set(),
            "historical_restores": [],
        }
    start = min(row["issued_at"] for row in issues)
    end = min(now, max(row["target_at"] for row in issues))
    device_ids = {row["device_id"] for row in issues}
    sensor_ids = {
        sensor_id
        for row in issues
        for sensor_id in row["input_snapshot"].get("sensor_basis", {}).get("sensor_ids", [])
        if isinstance(sensor_id, str) and sensor_id
    }
    limit = 30_001
    async with get_session() as session:
        statuses = (
            (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id.in_(device_ids),
                        DeviceStatusRecord.ts >= start - dt.timedelta(minutes=15),
                        DeviceStatusRecord.ts <= min(now, end + dt.timedelta(minutes=10)),
                    )
                    .order_by(DeviceStatusRecord.ts)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        readings = (
            (
                await session.execute(
                    select(IndoorTempReading)
                    .where(
                        IndoorTempReading.device_id.in_(sensor_ids),
                        IndoorTempReading.timestamp >= start - dt.timedelta(minutes=15),
                        IndoorTempReading.timestamp <= min(now, end + dt.timedelta(minutes=10)),
                        IndoorTempReading.is_stale.is_(False),
                    )
                    .order_by(IndoorTempReading.timestamp)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        counters = (
            (
                await session.execute(
                    select(ConsumptionRecord)
                    .where(
                        ConsumptionRecord.device_id.in_(device_ids),
                        ConsumptionRecord.ts >= start - dt.timedelta(hours=2),
                        ConsumptionRecord.ts <= min(now, end + dt.timedelta(minutes=10)),
                    )
                    .order_by(ConsumptionRecord.ts)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        actions = (
            (
                await session.execute(
                    select(PlanActionRecord)
                    .where(
                        PlanActionRecord.device_id.in_(device_ids),
                        or_(
                            and_(
                                PlanActionRecord.executed_at >= start - CARRYOVER_LOOKBACK,
                                PlanActionRecord.executed_at <= end + DISPATCH_DRIFT_MARGIN,
                            ),
                            and_(
                                PlanActionRecord.scheduled_ts >= start - CARRYOVER_LOOKBACK,
                                PlanActionRecord.scheduled_ts <= end + DISPATCH_DRIFT_MARGIN,
                            ),
                        ),
                    )
                    .order_by(PlanActionRecord.id)
                    .limit(MAX_DISPATCH_ACTION_LOOKUP + 1)
                )
            )
            .scalars()
            .all()
        )
        overrides = (
            (
                await session.execute(
                    select(OverrideRecord)
                    .where(
                        OverrideRecord.ts_from <= end,
                        OverrideRecord.ts_to >= start,
                    )
                    .limit(MAX_DISPATCH_ACTION_LOOKUP + 1)
                )
            )
            .scalars()
            .all()
        )
        restores, failed = await unresolved_revert_device_ids(session)
        historical, overflow = await historical_revert_overlap_rows(
            session,
            horizon_start=start,
            horizon_end=end,
            drift_margin=DISPATCH_DRIFT_MARGIN,
        )
        complete = (
            not failed and not overflow and max(len(statuses), len(readings), len(counters)) < limit
        )
        complete = complete and max(len(actions), len(overrides)) <= MAX_DISPATCH_ACTION_LOOKUP
        return {
            "complete": complete,
            "statuses": [_plain_record(row, DeviceStatusRecord) for row in statuses],
            "readings": [_plain_record(row, IndoorTempReading) for row in readings],
            "counters": [_plain_record(row, ConsumptionRecord) for row in counters],
            "actions": [_plain_record(row, PlanActionRecord) for row in actions],
            "overrides": [_plain_record(row, OverrideRecord) for row in overrides],
            "restores": restores,
            "historical_restores": historical,
        }


def reconcile_materialized_page(
    page: dict[str, Any], evidence: dict[str, Any], *, now: dt.datetime, scope: dict[str, Any]
) -> list[dict[str, Any]]:
    from packages.ml.models_common import (
        iter_confirmed_zero_intervals,
        iter_counter_change_intervals,
    )

    outcomes = []
    for entry in page["rows"]:
        issue = entry["issue"]
        prior = entry["outcome"]
        start, target = issue["issued_at"], issue["target_at"]
        all_statuses = [
            row for row in evidence["statuses"] if row["device_id"] == issue["device_id"]
        ]
        before = [row for row in all_statuses if as_utc(row["ts"]) <= start]
        statuses = ([before[-1]] if before else []) + [
            row
            for row in all_statuses
            if start < as_utc(row["ts"]) <= min(now, target + dt.timedelta(minutes=10))
        ]
        actions = []
        for action in evidence["actions"]:
            if action["device_id"] != issue["device_id"]:
                continue
            timestamp = as_utc(action["executed_at"]) or as_utc(action["scheduled_ts"])
            if (
                timestamp is None
                or not start - CARRYOVER_LOOKBACK <= timestamp <= target + DISPATCH_DRIFT_MARGIN
            ):
                continue
            try:
                result = json.loads(action["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            dispatched = _has_persisted_dispatch_proof(
                action["status"], result, action["verify_attempts"]
            )
            never = result.get("never_dispatched") is True or result.get("dispatched") is False
            if (
                action["status"] == "pending"
                and action["executed_at"] is None
                and not action["verify_attempts"]
            ):
                never = action["expected_state_json"] is None and not action["result_json"]
            actions.append({"dispatched": dispatched, "never_dispatched": never and not dispatched})
        overlaps = [
            row
            for row in evidence["overrides"]
            if as_utc(row["ts_from"]) <= target and as_utc(row["ts_to"]) >= start
        ]
        open_restores = issue["device_id"] in evidence["restores"] or any(
            device == issue["device_id"]
            and executed <= target + DISPATCH_DRIFT_MARGIN
            and (restored is None or restored >= start)
            for device, executed, _, restored in evidence["historical_restores"]
        )
        trace = {
            "lookup_complete": evidence["complete"],
            "overflow": False,
            "scope": scope,
            "statuses": statuses,
            "actions": actions,
            "overrides": overlaps,
            "restores": [True] if open_restores else [],
        }
        observed = observed_at = None
        if issue["input_snapshot"].get("unavailable_reason"):
            outcomes.append(
                reconcile_native_issue(issue, trace, observed=None, observed_at=None, now=now)
            )
            continue
        basis = issue["input_snapshot"]["sensor_basis"]
        if issue["component"] == "demand":
            counters = [
                SimpleNamespace(
                    **{
                        **row,
                        "ts": as_utc(row["ts"]),
                        "source_date": dt.date.fromisoformat(row["source_date"])
                        if row["source_date"]
                        else None,
                    }
                )
                for row in evidence["counters"]
                if row["device_id"] == issue["device_id"]
            ]
            status_objects = [
                SimpleNamespace(**{**row, "ts": as_utc(row["ts"])}) for row in all_statuses
            ]
            timezone = ZoneInfo(issue["input_snapshot"]["timezone"])
            intervals = list(iter_counter_change_intervals(counters, "heat_kwh", timezone))
            matches = [
                row
                for row in intervals
                if 0.75 <= row.elapsed_hours <= 1.25
                and abs((row.end_ts - target).total_seconds()) <= 600
            ]
            if matches:
                match = min(matches, key=lambda row: abs((row.end_ts - target).total_seconds()))
                observed, observed_at = match.energy_kwh / match.elapsed_hours, match.end_ts
            else:
                zeros = [
                    row
                    for row in iter_confirmed_zero_intervals(counters, status_objects, timezone)
                    if target - dt.timedelta(hours=1) <= row.start_ts and row.end_ts <= target
                ]
                if zeros and sum(row.elapsed_hours for row in zeros) >= 1.0:
                    observed, observed_at = 0.0, zeros[-1].end_ts
        elif basis["kind"] == "heatpump_temperature":
            matches = [
                row
                for row in statuses
                if abs((as_utc(row["ts"]) - target).total_seconds()) <= 600
                and isinstance(row.get(basis["field"]), (int, float))
            ]
            if matches:
                match = min(
                    matches, key=lambda row: abs((as_utc(row["ts"]) - target).total_seconds())
                )
                observed, observed_at = float(match[basis["field"]]), as_utc(match["ts"])
        else:
            selected = []
            for sensor in basis["sensor_ids"]:
                matches = [
                    row
                    for row in evidence["readings"]
                    if row["device_id"] == sensor
                    and abs((as_utc(row["timestamp"]) - target).total_seconds()) <= 600
                    and isinstance(row.get("temperature"), (int, float))
                    and 5 <= row["temperature"] <= 35
                ]
                if matches:
                    selected.append(
                        min(
                            matches,
                            key=lambda row: abs(
                                (as_utc(row["timestamp"]) - target).total_seconds()
                            ),
                        )
                    )
            if len(selected) == len(basis["sensor_ids"]):
                observed = float(statistics.median(row["temperature"] for row in selected))
                observed_at = max(as_utc(row["timestamp"]) for row in selected)
        if prior and prior["status"] == "valid":
            observed = prior["result"]["observed"]
            observed_at = prior["observed_at"]
        outcomes.append(
            reconcile_native_issue(
                issue, trace, observed=observed, observed_at=observed_at, now=now
            )
        )
    return outcomes


def as_utc(value: str | dt.datetime) -> dt.datetime | None:
    try:
        parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (
        parsed.replace(tzinfo=dt.timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(dt.timezone.utc)
    )


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)]


def _quantile(values: list[float], quantile: float) -> float | None:
    """Return a deterministic empirical quantile without interpolation noise."""

    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def score_bucket(
    abs_errors: list[float],
    signed_errors: list[float],
    predicted: list[float],
    observed: list[float],
    persistence_errors: list[float],
) -> dict[str, float | int | None]:
    if not all(
        _finite_number(value)
        for values in (abs_errors, signed_errors, predicted, observed, persistence_errors)
        for value in values
    ):
        return {
            "samples": 0,
            **dict.fromkeys(
                (
                    "mae",
                    "bias",
                    "p90_abs_error",
                    "p10_signed_error",
                    "p90_signed_error",
                    "r2",
                    "persistence_mae",
                    "persistence_improvement_c",
                )
            ),
        }
    valid_r2_inputs = (
        len(predicted) == len(observed)
        and len(observed) >= 2
        and all(math.isfinite(value) for value in predicted + observed)
    )
    r2: float | None = None
    if valid_r2_inputs:
        try:
            observed_mean = statistics.fmean(observed)
            total_sum_squares = sum((value - observed_mean) ** 2 for value in observed)
            residual_sum_squares = sum(
                (observed_value - predicted_value) ** 2
                for predicted_value, observed_value in zip(predicted, observed)
            )
            if (
                total_sum_squares > 0
                and math.isfinite(total_sum_squares)
                and math.isfinite(residual_sum_squares)
            ):
                r2 = round(1 - residual_sum_squares / total_sum_squares, 3)
        except OverflowError:
            r2 = None
    persistence_mae = (
        sum(abs(value) for value in persistence_errors) / len(persistence_errors)
        if persistence_errors and all(math.isfinite(value) for value in persistence_errors)
        else None
    )
    mae = sum(abs_errors) / len(abs_errors) if abs_errors else None
    result = {
        "samples": len(abs_errors),
        "mae": round(mae, 3) if mae is not None else None,
        # Positive means the forecast was too warm; negative means it was too cold.
        "bias": round(sum(signed_errors) / len(signed_errors), 3) if signed_errors else None,
        "p90_abs_error": round(_p90(abs_errors), 3) if abs_errors else None,
        # Forecast error is predicted minus observed. These two quantiles let
        # us form an empirical 80% interval around the bias-corrected forecast.
        "p10_signed_error": round(_quantile(signed_errors, 0.1), 3) if signed_errors else None,
        "p90_signed_error": round(_quantile(signed_errors, 0.9), 3) if signed_errors else None,
        "r2": r2,
        "persistence_mae": round(persistence_mae, 3) if persistence_mae is not None else None,
        "persistence_improvement_c": (
            round(persistence_mae - mae, 3)
            if persistence_mae is not None and mae is not None
            else None
        ),
    }
    return {
        key: value if value is None or _finite_number(value) else None
        for key, value in result.items()
    }


def _required_horizon_identity(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value in HORIZONS


def _required_horizon_buckets(
    rows: list[dict[str, Any]],
) -> dict[int, dict[str, Any]] | None:
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        horizon = row.get("hours")
        if not _required_horizon_identity(horizon) or horizon in buckets:
            return None
        buckets[horizon] = row
    return buckets


def _quality_gate(
    overall: dict[str, float | int | None],
    *,
    min_r2: float = 0.15,
    min_persistence_improvement_c: float = 0.1,
    max_abs_bias_c: float = MAX_GATE_ABS_BIAS_C,
    required_horizons: dict[int, dict[str, float | int | None]] | None = None,
) -> dict[str, object]:
    """Fail closed unless all configured scorecard evidence passes."""
    if not all(
        _finite_number(value) for value in (min_r2, min_persistence_improvement_c, max_abs_bias_c)
    ):
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": "invalid_quality_thresholds",
        }
    samples = overall.get("samples")
    samples = 0 if samples is None else samples
    if not _finite_number(samples) or samples < 0 or int(samples) != samples:
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": "invalid_scored_forecast_count",
        }
    if samples < MIN_GATE_SAMPLES:
        return {
            "status": "observing",
            "control_allowed": False,
            "reason": "insufficient_scored_forecasts",
            "minimum_samples": MIN_GATE_SAMPLES,
        }

    reasons: list[str] = []
    if overall.get("control_allowed") is False:
        reasons.append("scorecard_control_not_allowed")
    mae = overall.get("mae")
    bias = overall.get("bias")
    p90 = overall.get("p90_abs_error")
    if not _finite_number(mae) or not 0 <= mae <= MAX_GATE_MAE_C:
        reasons.append("mae_above_threshold")
    if not _finite_number(bias) or abs(bias) > max_abs_bias_c:
        reasons.append("bias_above_threshold")
    if not _finite_number(p90) or not 0 <= p90 <= MAX_GATE_P90_ABS_ERROR_C:
        reasons.append("p90_error_above_threshold")
    if reasons:
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": ",".join(reasons),
            "maximum_mae_c": MAX_GATE_MAE_C,
            "maximum_abs_bias_c": max_abs_bias_c,
            "maximum_p90_abs_error_c": MAX_GATE_P90_ABS_ERROR_C,
        }
    r2 = overall.get("r2")
    persistence_improvement = overall.get("persistence_improvement_c")
    if not _finite_number(r2) or r2 < min_r2:
        reasons.append("r2_below_threshold")
    if (
        not _finite_number(persistence_improvement)
        or persistence_improvement < min_persistence_improvement_c
    ):
        reasons.append("persistence_improvement_below_threshold")
    if required_horizons is None:
        reasons.append("required_horizons_missing")
    elif not isinstance(required_horizons, dict) or any(
        not _required_horizon_identity(horizon) for horizon in required_horizons
    ):
        reasons.append("invalid_required_horizon_identity")
    else:
        for horizon in HORIZONS:
            bucket = required_horizons.get(horizon)
            if not isinstance(bucket, dict):
                reasons.append(f"horizon_{horizon}_missing")
                continue
            if "hours" in bucket and (
                not _required_horizon_identity(bucket["hours"]) or bucket["hours"] != horizon
            ):
                reasons.append(f"horizon_{horizon}_invalid_identity")
                continue
            horizon_samples = bucket.get("samples")
            horizon_samples = 0 if horizon_samples is None else horizon_samples
            if (
                not _finite_number(horizon_samples)
                or horizon_samples < MIN_HORIZON_GATE_SAMPLES
                or int(horizon_samples) != horizon_samples
            ):
                reasons.append(f"horizon_{horizon}_insufficient_samples")
            horizon_mae = bucket.get("mae")
            horizon_bias = bucket.get("bias")
            horizon_p90 = bucket.get("p90_abs_error")
            horizon_r2 = bucket.get("r2")
            horizon_persistence_improvement = bucket.get("persistence_improvement_c")
            if not _finite_number(horizon_mae) or not 0 <= horizon_mae <= MAX_GATE_MAE_C:
                reasons.append(f"horizon_{horizon}_mae_above_threshold")
            if not _finite_number(horizon_bias) or abs(horizon_bias) > max_abs_bias_c:
                reasons.append(f"horizon_{horizon}_bias_above_threshold")
            if not _finite_number(horizon_p90) or not 0 <= horizon_p90 <= MAX_GATE_P90_ABS_ERROR_C:
                reasons.append(f"horizon_{horizon}_p90_error_above_threshold")
            if not _finite_number(horizon_r2) or horizon_r2 < min_r2:
                reasons.append(f"horizon_{horizon}_r2_below_threshold")
            if (
                not _finite_number(horizon_persistence_improvement)
                or horizon_persistence_improvement < min_persistence_improvement_c
            ):
                reasons.append(f"horizon_{horizon}_persistence_improvement_below_threshold")
    if reasons:
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": ",".join(reasons),
        }
    return {
        "status": "passed",
        "control_allowed": True,
        "reason": "forecast_quality_within_thresholds",
        "maximum_mae_c": MAX_GATE_MAE_C,
        "maximum_abs_bias_c": max_abs_bias_c,
        "maximum_p90_abs_error_c": MAX_GATE_P90_ABS_ERROR_C,
    }


def apply_control_gate_hysteresis(
    gate: dict[str, object],
    metadata: dict[str, Any] | None,
    *,
    schema: str,
    required_horizons: tuple[int, ...],
    evaluation_context: dict[str, object] | None = None,
    passes_required: int = 2,
    failures_required: int = 1,
) -> dict[str, object]:
    """Persist a fail-closed control gate state in comfort-model metadata."""
    previous = (metadata or {}).get("forecast_quality_gate", {})
    normalized_context = evaluation_context or {"live_baseline_applied": False}
    previous_context = previous.get("evaluation_context", {"live_baseline_applied": False})
    previous_horizons = previous.get("required_horizons", ())
    valid_horizons = (
        bool(required_horizons)
        and all(_required_horizon_identity(horizon) for horizon in required_horizons)
        and len(set(required_horizons)) == len(required_horizons)
    )
    same_schema = (
        valid_horizons
        and previous.get("schema") == schema
        and isinstance(previous_horizons, (list, tuple))
        and all(_required_horizon_identity(horizon) for horizon in previous_horizons)
        and tuple(previous_horizons) == required_horizons
        and previous_context == normalized_context
    )
    passes = int(previous.get("pass_streak", 0)) if same_schema else 0
    failures = int(previous.get("failure_streak", 0)) if same_schema else 0
    raw_status = str(gate.get("status", "error")) if valid_horizons else "failed"
    if raw_status == "passed" and bool(gate.get("control_allowed")):
        passes += 1
        failures = 0
        status = "allowed" if passes >= passes_required else "observing"
    else:
        failures += 1
        passes = 0
        status = "fallback" if failures >= failures_required else "observing"
    return {
        "schema": schema,
        "required_horizons": list(required_horizons) if valid_horizons else [],
        "evaluation_context": normalized_context,
        "pass_streak": passes,
        "failure_streak": failures,
        "status": status,
        "control_allowed": status == "allowed",
        "raw_status": raw_status,
        "reason": gate.get("reason", "scorecard_unavailable")
        if valid_horizons
        else "invalid_required_horizon_identity",
    }


async def evaluate_live_control_gate(
    *,
    model_metrics: dict[str, Any],
    record_gate: Any,
    scorecard_loader: Any | None = None,
    get_float: Any | None = None,
    get_int: Any | None = None,
    get_string: Any | None = None,
) -> dict[str, object]:
    """Evaluate the live learned-forecast gate with injectable I/O dependencies.

    The scorecard can be evaluated by both optimizer layers during one planning
    cycle. Its content-derived id makes the hysteresis update idempotent.
    """
    if scorecard_loader is None:
        scorecard_loader = get_forecast_scorecard
    if get_float is None or get_int is None:
        from packages.core.settings_service import get_float_setting, get_int_setting

        get_float = get_float or get_float_setting
        get_int = get_int or get_int_setting
    if get_string is None:
        from packages.core.settings_service import get_string_setting

        get_string = get_string_setting
    try:
        configured_mode = await get_string("space_heating_baseline_mode")
        effective_mode = configured_mode if configured_mode in {"off", "shadow", "on"} else "shadow"
        evaluation_context = {"live_baseline_applied": effective_mode == "on"}
        try:
            scorecard = await scorecard_loader(evaluation_context=evaluation_context)
        except TypeError as exc:
            if "evaluation_context" not in str(exc):
                raise
            scorecard = await scorecard_loader()
        horizon_buckets = _required_horizon_buckets(scorecard.get("horizons", []))
        raw_gate = _quality_gate(
            scorecard.get("overall", {}),
            min_r2=await get_float("indoor_forecast_min_r2"),
            min_persistence_improvement_c=await get_float(
                "indoor_forecast_min_persistence_improvement_c"
            ),
            max_abs_bias_c=await get_float("indoor_forecast_max_abs_bias_c"),
            required_horizons=horizon_buckets,
        )
        evaluation_id = json.dumps(
            {
                "context": evaluation_context,
                "overall": scorecard.get("overall", {}),
                "horizons": horizon_buckets,
            },
            sort_keys=True,
            default=str,
        )
        return record_gate(
            raw_gate,
            schema="indoor_forecast_v4_delta_window_heat",
            required_horizons=HORIZONS,
            passes_required=await get_int("indoor_forecast_gate_passes_required"),
            failures_required=await get_int("indoor_forecast_gate_failures_required"),
            evaluation_id=evaluation_id,
            evaluation_context=evaluation_context,
        )
    except Exception as exc:  # noqa: BLE001 - planner gate must fail closed
        return {
            "status": "fallback",
            "control_allowed": False,
            "reason": f"forecast_quality_gate_error:{type(exc).__name__}",
        }


def _snapshot_live_baseline_applied(snapshot: dict[str, Any]) -> bool:
    """Map legacy snapshots and malformed metadata to the safe off context."""

    baseline = snapshot.get("space_heating_baseline")
    return bool(baseline.get("live_baseline_applied")) if isinstance(baseline, dict) else False


def _baseline_promotion_summary(
    pairs: list[dict[str, Any]],
    plan_ids: set[int],
    exclusions: dict[str, int],
) -> dict[str, Any]:
    """Score immutable shadow candidate/control pairs without changing settings."""

    def score_pairs(selected: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
        return score_bucket(
            [pair[f"{prefix}_abs_error"] for pair in selected],
            [pair[f"{prefix}_signed_error"] for pair in selected],
            [],
            [],
            [],
        )

    candidate = score_pairs(pairs, "candidate")
    control = score_pairs(pairs, "control")
    horizons: list[dict[str, Any]] = []
    horizon_ready = True
    horizon_worsened = False
    for hour in HORIZONS:
        selected = [pair for pair in pairs if pair["hour"] == hour]
        candidate_bucket = score_pairs(selected, "candidate")
        control_bucket = score_pairs(selected, "control")
        candidate_mae = candidate_bucket["mae"]
        control_mae = control_bucket["mae"]
        improvement = (
            round(float(control_mae) - float(candidate_mae), 3)
            if isinstance(control_mae, (int, float)) and isinstance(candidate_mae, (int, float))
            else None
        )
        horizon_ready &= len(selected) >= MIN_BASELINE_HORIZON_PAIRS
        horizon_worsened |= isinstance(improvement, float) and improvement < -0.10
        horizons.append(
            {
                "hours": hour,
                "samples": len(selected),
                "candidate": candidate_bucket,
                "zero_baseline": control_bucket,
                "mae_improvement_c": improvement,
            }
        )
    candidate_mae = candidate["mae"]
    control_mae = control["mae"]
    improvement = (
        round(float(control_mae) - float(candidate_mae), 3)
        if isinstance(control_mae, (int, float)) and isinstance(candidate_mae, (int, float))
        else None
    )
    dates = {pair["ts"].date().isoformat() for pair in pairs}
    promotion_ready = (
        len(plan_ids) >= MIN_BASELINE_PROMOTION_PLANS
        and len(pairs) >= MIN_BASELINE_PROMOTION_PAIRS
        and len(dates) >= MIN_BASELINE_PROMOTION_DAYS
        and horizon_ready
        and isinstance(improvement, float)
        and improvement >= MIN_BASELINE_MAE_IMPROVEMENT_C
        and isinstance(candidate["bias"], (int, float))
        and abs(float(candidate["bias"])) <= MAX_BASELINE_CANDIDATE_ABS_BIAS_C
        and isinstance(candidate["p90_abs_error"], (int, float))
        and float(candidate["p90_abs_error"]) <= MAX_BASELINE_CANDIDATE_P90_C
        and not horizon_worsened
    )
    return {
        "promotion_ready": promotion_ready,
        "reason": "promotion_thresholds_met"
        if promotion_ready
        else "insufficient_or_failing_paired_shadow_evidence",
        "plans_scored": len(plan_ids),
        "pairs_scored": len(pairs),
        "days_scored": len(dates),
        "date_span": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
        "candidate": candidate,
        "zero_baseline": control,
        "mae_improvement_c": improvement,
        "horizons": horizons,
        "exclusions": dict(sorted(exclusions.items())),
    }


def _collect_baseline_pairs(
    *,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    readings: list[Any],
    sensor_input: dict[str, Any],
    plan_id: int,
    pairs: list[dict[str, Any]],
    plan_ids: set[int],
    exclusions: dict[str, int],
    dispatched_at: dt.datetime | None = None,
) -> None:
    """Append outcome-matched candidate/control pairs from one shadow plan."""

    baseline_evaluation = snapshot.get("baseline_evaluation")
    baseline_metadata = snapshot.get("space_heating_baseline")
    if not (
        isinstance(baseline_evaluation, dict)
        and baseline_evaluation.get("learning_mode") is True
        and baseline_evaluation.get("eligible") is True
        and isinstance(baseline_metadata, dict)
        and baseline_metadata.get("effective_mode") == "shadow"
    ):
        return
    candidate = snapshot.get("forecast_with_plan_baseline")
    control = snapshot.get("forecast_with_plan_zero_baseline")
    if not isinstance(candidate, list) or not isinstance(control, list):
        exclusions["missing_comparison_curves"] = exclusions.get("missing_comparison_curves", 0) + 1
        return
    explicit_at: dt.datetime | None = None
    for action in payload.get("actions", []) if isinstance(payload.get("actions"), list) else []:
        if not isinstance(action, dict) or action.get("type") not in {
            "zone_temp_boost",
            "zone_temp_restore",
            "set_zone_heat_temperature",
            "set_operation_mode",
            "normal_mode_on",
            "eco_mode_on",
            "eco_mode_off",
            "comfort_mode_on",
        }:
            continue
        action_ts = as_utc(action.get("ts"))
        if action_ts is not None and (explicit_at is None or action_ts < explicit_at):
            explicit_at = action_ts
    if dispatched_at is not None and (explicit_at is None or dispatched_at < explicit_at):
        explicit_at = dispatched_at
    control_by_ts = {
        as_utc(point.get("ts")): point
        for point in control
        if isinstance(point, dict) and as_utc(point.get("ts")) is not None
    }
    for point in candidate:
        if not isinstance(point, dict):
            continue
        target_ts = _saved_forecast_instant(point.get("ts"))
        control_point = control_by_ts.get(target_ts)
        hour = point.get("hour")
        candidate_prediction = point.get("predicted_indoor_temp")
        control_prediction = (
            control_point.get("predicted_indoor_temp") if isinstance(control_point, dict) else None
        )
        baseline_fraction = point.get("baseline_heating_fraction")
        if explicit_at is not None and target_ts is not None and target_ts >= explicit_at:
            exclusions["explicit_or_dispatched_window"] = (
                exclusions.get("explicit_or_dispatched_window", 0) + 1
            )
            continue
        if (
            not _required_horizon_identity(hour)
            or target_ts is None
            or not _finite_number(candidate_prediction)
            or not _finite_number(control_prediction)
            or not _finite_number(baseline_fraction)
            or float(baseline_fraction) == 0
        ):
            exclusions["invalid_or_zero_baseline_pair"] = (
                exclusions.get("invalid_or_zero_baseline_pair", 0) + 1
            )
            continue
        observed = _observed_temperature_at(readings, target_ts, sensor_input)
        if observed is None or not math.isfinite(observed):
            exclusions["no_matching_sensor_outcome"] = (
                exclusions.get("no_matching_sensor_outcome", 0) + 1
            )
            continue
        pairs.append(
            {
                "hour": int(hour),
                "ts": target_ts,
                "candidate_abs_error": abs(float(candidate_prediction) - observed),
                "candidate_signed_error": float(candidate_prediction) - observed,
                "control_abs_error": abs(float(control_prediction) - observed),
                "control_signed_error": float(control_prediction) - observed,
            }
        )
        plan_ids.add(plan_id)


async def _dispatched_action_times(
    session: Any,
    plans: list[Any],
    since: dt.datetime,
) -> tuple[dict[int, dt.datetime], bool]:
    """Return same-device command times and whether the bounded query overflowed.

    Plan actions preserve the device linkage that plans do not store in a
    relational column. The originating plan ID is intentionally not used as
    an exclusion key, but a terminal status alone is not proof that a command
    reached the device: lifecycle cancellation records the same timestamp.
    """

    plan_windows: list[tuple[int, str, dt.datetime, dt.datetime]] = []
    for plan in plans:
        try:
            payload = json.loads(plan.plan_json)
        except (TypeError, ValueError):
            continue
        device_id = payload.get("device_id") if isinstance(payload, dict) else None
        horizon_start = as_utc(plan.horizon_start)
        horizon_end = as_utc(plan.horizon_end)
        if isinstance(device_id, str) and horizon_start is not None and horizon_end is not None:
            plan_windows.append((plan.id, device_id, horizon_start, horizon_end))
    if not plan_windows:
        return {}, False

    device_ids = {device_id for _, device_id, _, _ in plan_windows}
    earliest_start = min(start for _, _, start, _ in plan_windows)
    latest_end = max(end for _, _, _, end in plan_windows)
    rows = (
        await session.execute(
            select(
                PlanActionRecord.device_id,
                PlanActionRecord.executed_at,
                PlanActionRecord.status,
                PlanActionRecord.result_json,
                PlanActionRecord.verify_attempts,
            )
            .where(PlanActionRecord.device_id.in_(device_ids))
            .where(PlanActionRecord.status.in_(("dispatched", "executed", "failed", "cancelled")))
            .where(PlanActionRecord.executed_at.is_not(None))
            .where(PlanActionRecord.executed_at >= earliest_start - CARRYOVER_LOOKBACK)
            .where(PlanActionRecord.executed_at <= latest_end + DISPATCH_DRIFT_MARGIN)
            .order_by(PlanActionRecord.executed_at)
            .limit(MAX_DISPATCH_ACTION_LOOKUP + 1)
        )
    ).all()
    overflowed = len(rows) > MAX_DISPATCH_ACTION_LOOKUP
    if overflowed:
        rows = rows[:MAX_DISPATCH_ACTION_LOOKUP]
    dispatched: dict[int, dt.datetime] = {}
    for device_id, executed_at, status, result_json, verify_attempts in rows:
        if not _has_persisted_dispatch_proof(status, result_json, verify_attempts):
            continue
        timestamp = as_utc(executed_at)
        if timestamp is None:
            continue
        for plan_id, plan_device_id, horizon_start, horizon_end in plan_windows:
            if (
                device_id == plan_device_id
                and horizon_start - CARRYOVER_LOOKBACK
                <= timestamp
                <= horizon_end + DISPATCH_DRIFT_MARGIN
                and (plan_id not in dispatched or timestamp < dispatched[plan_id])
            ):
                dispatched[plan_id] = timestamp
    return dispatched, overflowed


def _has_persisted_dispatch_proof(status: Any, result_json: Any, verify_attempts: Any) -> bool:
    """Return whether an action row proves the executor dispatched a command.

    ``cancelled`` rows need a dispatch marker or recorded verification attempt.
    """

    try:
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except (TypeError, ValueError):
        result = None
    result = result if isinstance(result, dict) else {}
    if status == "dispatched":
        return True
    if status == "executed":
        return result.get("success") is True and result.get("verified") is True
    if status == "failed":
        return isinstance(verify_attempts, int) and verify_attempts > 0
    return status == "cancelled" and (
        result.get("dispatched") is True
        or (isinstance(verify_attempts, int) and verify_attempts > 0)
    )


def _regime_quality(bucket: dict[str, float | int | None]) -> dict[str, object]:
    """Describe whether a weather regime is safe to control against.

    This deliberately distinguishes *unobserved* from *failed*: unobserved
    weather receives a conservative reserve, while a statistically supported
    but failing regime makes ML control fall back to rules when that regime is
    present in the planning horizon.
    """

    samples = int(bucket["samples"] or 0)
    if samples < MIN_REGIME_GATE_SAMPLES:
        return {
            "status": "unobserved",
            "control_allowed": True,
            "samples_required": MIN_REGIME_GATE_SAMPLES,
            "uncertainty_margin_c": 0.0,
        }

    failures: list[str] = []
    mae = bucket.get("mae")
    bias = bucket.get("bias")
    p90 = bucket.get("p90_abs_error")
    if not isinstance(mae, (int, float)) or mae > MAX_GATE_MAE_C:
        failures.append("mae_above_threshold")
    if not isinstance(bias, (int, float)) or abs(bias) > MAX_GATE_ABS_BIAS_C:
        failures.append("bias_above_threshold")
    if not isinstance(p90, (int, float)) or p90 > MAX_GATE_P90_ABS_ERROR_C:
        failures.append("p90_error_above_threshold")
    if failures:
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": ",".join(failures),
            "samples_required": MIN_REGIME_GATE_SAMPLES,
            "uncertainty_margin_c": 0.0,
        }
    return {
        "status": "passed",
        "control_allowed": True,
        "reason": "regime_quality_within_thresholds",
        "samples_required": MIN_REGIME_GATE_SAMPLES,
        "uncertainty_margin_c": 0.0,
    }


def _horizon_quality(bucket: dict[str, float | int | None]) -> dict[str, object]:
    """Gate a forecast lead time independently from the aggregate score.

    A good one-hour forecast must not silently approve a poor 6- or 12-hour
    forecast. Sparse horizons remain observational (with their uncertainty
    interval), while a measured failing horizon makes the optimizer fall back
    only when that lead time is part of the requested plan.
    """
    samples = int(bucket["samples"] or 0)
    if samples < MIN_HORIZON_GATE_SAMPLES:
        return {
            "status": "observing",
            "control_allowed": True,
            "samples_required": MIN_HORIZON_GATE_SAMPLES,
            "reason": "insufficient_horizon_samples",
        }
    failures: list[str] = []
    if not isinstance(bucket.get("mae"), (int, float)) or float(bucket["mae"]) > MAX_GATE_MAE_C:
        failures.append("mae_above_threshold")
    if (
        not isinstance(bucket.get("bias"), (int, float))
        or abs(float(bucket["bias"])) > MAX_GATE_ABS_BIAS_C
    ):
        failures.append("bias_above_threshold")
    if (
        not isinstance(bucket.get("p90_abs_error"), (int, float))
        or float(bucket["p90_abs_error"]) > MAX_GATE_P90_ABS_ERROR_C
    ):
        failures.append("p90_error_above_threshold")
    return {
        "status": "failed" if failures else "passed",
        "control_allowed": not failures,
        "samples_required": MIN_HORIZON_GATE_SAMPLES,
        "reason": ",".join(failures) if failures else "horizon_quality_within_thresholds",
    }


def _horizon_bucket_hour(hour: int, available: dict[int, object]) -> int | None:
    """Use the first validated lead time at or beyond an hourly plan slot."""
    candidates = sorted(value for value in available if value >= hour)
    return candidates[0] if candidates else (max(available) if available else None)


def _bias_correction(bucket: dict[str, float | int | None]) -> float:
    """Return a bounded correction to add to a forecasted indoor temperature."""

    samples = int(bucket.get("samples") or 0)
    bias = bucket.get("bias")
    if samples < MIN_BIAS_CORRECTION_SAMPLES or not isinstance(bias, (int, float)):
        return 0.0
    # Signed error is predicted minus observed, so a negative bias means the
    # forecast is too cold and needs a positive correction.
    return round(max(-MAX_BIAS_CORRECTION_C, min(MAX_BIAS_CORRECTION_C, -bias)), 3)


def prediction_interval_for_bucket(
    bucket: dict[str, float | int | None],
    *,
    bias_correction_c: float = 0.0,
) -> dict[str, float | int | str | None]:
    """Build an empirical 80% prediction interval around a corrected forecast.

    The interval is descriptive: it makes uncertainty visible to the user and
    is intentionally separate from the conservative control reserves.  Sparse
    evidence falls back to a symmetric estimated interval rather than being
    misrepresented as calibrated.
    """

    samples = int(bucket.get("samples") or 0)
    p10 = bucket.get("p10_signed_error")
    p90 = bucket.get("p90_signed_error")
    if (
        samples >= MIN_INTERVAL_SAMPLES
        and isinstance(p10, (int, float))
        and isinstance(p90, (int, float))
    ):
        # residual = (raw prediction + correction) - actual
        # actual is therefore prediction - residual.
        lower = -(float(p90) + bias_correction_c)
        upper = -(float(p10) + bias_correction_c)
        return {
            "status": "calibrated",
            "coverage": 0.8,
            "samples": samples,
            "lower_offset_c": round(min(lower, upper), 3),
            "upper_offset_c": round(max(lower, upper), 3),
        }

    width = bucket.get("p90_abs_error")
    fallback_width = max(1.0, float(width)) if isinstance(width, (int, float)) else 1.5
    return {
        "status": "estimated",
        "coverage": 0.8,
        "samples": samples,
        "lower_offset_c": round(-fallback_width, 3),
        "upper_offset_c": round(fallback_width, 3),
    }


def prediction_intervals_for_weather(
    scorecard: dict[str, Any],
    weather_points: list[dict[str, object]],
) -> list[dict[str, float | int | str | None]]:
    """Select horizon- and weather-aware intervals for each planned hour."""

    horizons = _required_horizon_buckets(scorecard.get("horizons", [])) or {}
    regimes = scorecard.get("regimes") if isinstance(scorecard.get("regimes"), dict) else {}
    corrections = scorecard.get("bias_correction", {})
    by_horizon = corrections.get("by_horizon_c", {}) if isinstance(corrections, dict) else {}
    overall = scorecard.get("overall", {}) if isinstance(scorecard.get("overall"), dict) else {}
    overall_correction = _bias_correction(overall)
    intervals: list[dict[str, float | int | str | None]] = []

    for hour, weather in enumerate(weather_points, start=1):
        selected_horizon = _horizon_bucket_hour(hour, horizons)
        bucket: dict[str, float | int | None] = horizons.get(selected_horizon, overall)
        source = f"{selected_horizon}h" if selected_horizon is not None else "overall"
        # A sufficiently observed regime is more representative than the
        # overall score.  Pick the one with most samples when conditions
        # overlap (for example cold rain).
        candidates = [
            (name, regimes.get(name))
            for name in _weather_regimes(weather)
            if isinstance(regimes.get(name), dict)
            and int(regimes[name].get("samples") or 0) >= MIN_INTERVAL_SAMPLES
        ]
        if candidates:
            name, regime_bucket = max(candidates, key=lambda item: int(item[1].get("samples") or 0))
            bucket = regime_bucket
            source = f"{name}_regime"
        correction = by_horizon.get(str(selected_horizon), overall_correction)
        correction = (
            float(correction) if isinstance(correction, (int, float)) else overall_correction
        )
        interval = prediction_interval_for_bucket(bucket, bias_correction_c=correction)
        interval["source"] = source
        intervals.append(interval)
    return intervals


def _weather_regimes(weather: dict[str, object]) -> tuple[str, ...]:
    """Classify one weather point using the same definitions as scoring."""

    regimes: list[str] = []
    try:
        precipitation = float(weather.get("precipitation") or 0)
    except (AttributeError, TypeError, ValueError):
        precipitation = 0.0
    if precipitation > 0:
        regimes.append("rain")
    temperature = weather.get("temperature", weather.get("outdoor_temp"))
    if isinstance(temperature, (int, float)):
        if temperature < 5:
            regimes.append("cold")
        elif temperature >= 10:
            regimes.append("mild")
    try:
        wind_speed = float(weather.get("wind_speed") or 0)
    except (AttributeError, TypeError, ValueError):
        wind_speed = 0.0
    if wind_speed >= 7.0:
        regimes.append("windy")
    try:
        irradiance = float(weather.get("irradiance") or 0)
    except (AttributeError, TypeError, ValueError):
        irradiance = 0.0
    if irradiance >= 350.0:
        regimes.append("sunny")
    try:
        humidity = float(weather.get("humidity") or 0)
    except (AttributeError, TypeError, ValueError):
        humidity = 0.0
    if humidity >= 80.0:
        regimes.append("humid")
    try:
        cloud_cover = float(weather.get("cloud_cover") or 0)
    except (AttributeError, TypeError, ValueError):
        cloud_cover = 0.0
    if cloud_cover >= 0.75:
        regimes.append("cloudy")
    return tuple(regimes)


def control_adjustments_for_weather(
    scorecard: dict[str, Any],
    weather_points: list[dict[str, object]],
) -> dict[str, Any]:
    """Return condition-aware reserves and bias corrections for a plan.

    ``weather_points`` is ordered by planning hour. A poor regime only blocks
    ML control when it is forecast in that plan; otherwise unrelated bad
    weather history cannot unnecessarily disable a valid mild-weather plan.
    """

    regime_quality = scorecard.get("regime_quality")
    if not isinstance(regime_quality, dict):
        regime_quality = {}
    horizon_scores = _required_horizon_buckets(scorecard.get("horizons", []))
    invalid_horizons = horizon_scores is None
    horizon_scores = horizon_scores or {}
    horizon_quality = scorecard.get("horizon_quality")
    if not isinstance(horizon_quality, dict):
        horizon_quality = {}
    overall_correction = _bias_correction(scorecard.get("overall", {}))
    margins: list[float] = []
    corrections: list[float] = []
    hourly_regimes: list[list[str]] = []
    failed_regimes: set[str] = set()
    failed_horizons: set[int] = set()

    for hour, weather in enumerate(weather_points, start=1):
        selected_horizon = _horizon_bucket_hour(hour, horizon_scores)
        if selected_horizon is not None:
            quality = horizon_quality.get(str(selected_horizon), {})
            if isinstance(quality, dict) and quality.get("status") == "failed":
                failed_horizons.add(selected_horizon)
        regimes = list(_weather_regimes(weather))
        hourly_regimes.append(regimes)
        margin = 0.0
        for regime in regimes:
            quality = regime_quality.get(regime, {})
            status = quality.get("status") if isinstance(quality, dict) else None
            if status == "failed":
                failed_regimes.add(regime)
            elif status == "unobserved":
                margin = max(margin, UNOBSERVED_REGIME_MARGIN_C[regime])
        margins.append(round(margin, 3))
        corrections.append(
            _bias_correction(horizon_scores.get(selected_horizon, {})) or overall_correction
        )

    return {
        "control_allowed": not invalid_horizons and not failed_regimes and not failed_horizons,
        "failed_regimes": sorted(failed_regimes),
        "failed_horizons": sorted(failed_horizons),
        "condition_margins_c": margins,
        "bias_corrections_c": corrections,
        "hourly_regimes": hourly_regimes,
    }


def _forecast_source(point: dict[str, Any]) -> str:
    """Return the prediction implementation, never the rule-plan label."""

    source = point.get("model_source")
    return str(source).strip() if isinstance(source, str) and source.strip() else "unknown"


def _source_kind(source: str) -> str:
    if source in {"comfort_model_controlled", "comfort_model_physics_continuation"}:
        return "learned_comfort_model"
    if source in {"comfort_model_passive_direct", "comfort_model_passive_physics_continuation"}:
        return "passive_weather_model"
    return "rule_thermal_fallback"


def _forecast_evidence_role(series: str, source: str) -> str:
    """A learned candidate has one role, regardless of serving slot or source alias."""
    return "learned_forecast" if _source_kind(source) == "learned_comfort_model" else series


def _validation_sensor_input(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Read the immutable sensor provenance needed for a fair comparison.

    Forecasts created before ``indoor_forecast_v2`` do not state which room
    observation they started from. They are useful history, but are not valid
    ML evidence and must not be allowed to contaminate a control gate.
    """

    control_input = snapshot.get("control_input")
    if not isinstance(control_input, dict) or control_input.get("available") is not True:
        return None
    reference_id = control_input.get("reference_sensor_id")
    if reference_id is not None and not isinstance(reference_id, str):
        return None
    sensor_ids = control_input.get("sensor_ids")
    if "sensor_ids" in control_input and (
        not isinstance(sensor_ids, list)
        or not sensor_ids
        or any(not isinstance(sensor_id, str) or not sensor_id.strip() for sensor_id in sensor_ids)
    ):
        return None
    if isinstance(reference_id, str) and reference_id.strip():
        if isinstance(sensor_ids, list) and reference_id.strip() not in {
            sensor_id.strip() for sensor_id in sensor_ids
        }:
            return None
        if "sensor_basis" in snapshot and (
            not isinstance(snapshot["sensor_basis"], dict)
            or snapshot["sensor_basis"].get("kind") != "reference"
        ):
            return None
        return {"method": "reference_sensor", "sensor_ids": [reference_id.strip()]}
    if not isinstance(sensor_ids, list):
        return None
    if "sensor_basis" in snapshot and (
        not isinstance(snapshot["sensor_basis"], dict)
        or snapshot["sensor_basis"].get("kind") != "median"
    ):
        return None
    selected = sorted({sensor_id.strip() for sensor_id in sensor_ids})
    return {"method": "selected_sensor_median", "sensor_ids": selected} if selected else None


def _saved_forecast_instant(value: object) -> dt.datetime | None:
    if not isinstance(value, (str, dt.datetime)):
        return None
    try:
        parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _persistence_origin(snapshot: dict[str, Any]) -> tuple[dt.datetime, float] | None:
    if snapshot.get("scoring_schema") != "forecast_outcome_v2_persistence_origin":
        return None
    observed_history = snapshot.get("observed_history")
    if not isinstance(observed_history, list) or not observed_history:
        return None
    origin = observed_history[0]
    if not isinstance(origin, dict) or type(origin.get("hour")) is not int or origin["hour"] != 0:
        return None
    issue_timestamp = _saved_forecast_instant(snapshot.get("issue_timestamp"))
    origin_timestamp = _saved_forecast_instant(origin.get("ts"))
    temperature = origin.get("temperature")
    if (
        issue_timestamp is None
        or origin_timestamp != issue_timestamp
        or not _finite_number(temperature)
    ):
        return None
    return issue_timestamp, float(temperature)


def _has_learned_family_point(forecast: object) -> bool:
    return isinstance(forecast, list) and any(
        isinstance(point, dict) and _source_kind(_forecast_source(point)) == "learned_comfort_model"
        for point in forecast
    )


def _learned_candidate(forecast: object, shadow_forecast: object) -> list[dict[str, Any]] | None:
    if _has_learned_family_point(forecast):
        return forecast
    if _has_learned_family_point(shadow_forecast):
        return shadow_forecast
    return None


def _observed_temperature_at(
    readings: list[Any],
    target_ts: dt.datetime,
    sensor_input: dict[str, Any],
) -> float | None:
    """Return an outcome measured by the same sensor method as the forecast."""

    sensor_ids = set(sensor_input.get("sensor_ids", []))
    if not sensor_ids:
        return None
    nearby = [
        row
        for row in readings
        if getattr(row, "device_id", None) in sensor_ids
        and (timestamp := as_utc(getattr(row, "timestamp", None))) is not None
        and abs((timestamp - target_ts).total_seconds()) <= 30 * 60
    ]
    if not nearby:
        return None

    unique: dict[tuple[str, dt.datetime], Any] = {}
    conflicts: set[tuple[str, dt.datetime]] = set()
    for row in nearby:
        identity = (row.device_id, as_utc(row.timestamp))
        previous = unique.get(identity)
        if previous is None:
            unique[identity] = row
        elif (
            not _finite_number(previous.temperature)
            or not _finite_number(row.temperature)
            or previous.temperature != row.temperature
        ):
            conflicts.add(identity)
    nearby = list(unique.values())

    if sensor_input.get("method") == "reference_sensor":
        nearest = min(
            nearby,
            key=lambda row: abs((as_utc(row.timestamp) - target_ts).total_seconds()),
        )
        if (nearest.device_id, as_utc(nearest.timestamp)) in conflicts:
            return None
        return float(nearest.temperature)

    # The plan used a robust median across these selected rooms. Reconstruct
    # the same shape of measurement from one closest fresh sample per device.
    closest_by_device: dict[str, Any] = {}
    for row in nearby:
        device_id = str(row.device_id)
        current = closest_by_device.get(device_id)
        if current is None or abs((as_utc(row.timestamp) - target_ts).total_seconds()) < abs(
            (as_utc(current.timestamp) - target_ts).total_seconds()
        ):
            closest_by_device[device_id] = row
    if any(
        (row.device_id, as_utc(row.timestamp)) in conflicts for row in closest_by_device.values()
    ):
        return None
    return float(statistics.median(float(row.temperature) for row in closest_by_device.values()))


def _source_score(
    abs_errors: list[float],
    signed_errors: list[float],
    predicted: list[float],
    observed: list[float],
    persistence_errors: list[float],
    horizon_abs: dict[int, list[float]],
    horizon_signed: dict[int, list[float]],
    horizon_predicted: dict[int, list[float]],
    horizon_observed: dict[int, list[float]],
    horizon_persistence_errors: dict[int, list[float]],
    regime_abs: dict[str, list[float]],
    regime_signed: dict[str, list[float]],
    *,
    plans_scored: int,
) -> dict[str, Any]:
    overall = score_bucket(abs_errors, signed_errors, predicted, observed, persistence_errors)
    horizons = [
        {
            "hours": hour,
            **score_bucket(
                horizon_abs[hour],
                horizon_signed[hour],
                horizon_predicted[hour],
                horizon_observed[hour],
                horizon_persistence_errors[hour],
            ),
        }
        for hour in HORIZONS
    ]
    regimes = {
        name: score_bucket(
            regime_abs[name],
            regime_signed[name],
            [],
            [],
            [],
        )
        for name in WEATHER_REGIMES
    }
    return {
        "plans_scored": plans_scored,
        "overall": overall,
        "quality_gate": _quality_gate(
            overall, required_horizons={int(row["hours"]): row for row in horizons}
        ),
        "horizons": horizons,
        "horizon_quality": {str(row["hours"]): _horizon_quality(row) for row in horizons},
        "regimes": regimes,
        "regime_quality": {name: _regime_quality(bucket) for name, bucket in regimes.items()},
    }


async def complete_plan_window(
    session: Any, *, since: dt.datetime, until: dt.datetime, page_size: int = 100
) -> tuple[list[Any], bool, int | None]:
    if not 1 <= page_size <= 100:
        raise ValueError("Plan pages must contain at most 100 rows")
    collected, cursor, high_water = [], None, None
    while True:
        query = select(PlanRecord).where(
            PlanRecord.created_at >= since, PlanRecord.created_at <= until
        )
        if cursor is not None:
            query = query.where(PlanRecord.id < cursor, PlanRecord.id <= high_water)
        rows = (
            (await session.execute(query.order_by(desc(PlanRecord.id)).limit(page_size)))
            .scalars()
            .all()
        )
        if rows and high_water is None:
            high_water = max(row.id for row in rows)
        collected.extend(rows)
        if len(collected) > MAX_QUALIFICATION_PAIRS:
            return [], False, high_water
        if len(rows) < page_size:
            return collected, True, high_water
        next_cursor = min(row.id for row in rows)
        if cursor is not None and next_cursor >= cursor:
            return [], False, high_water
        cursor = next_cursor


async def get_forecast_scorecard(
    *,
    now: dt.datetime | None = None,
    lookback_days: int = 14,
    max_plans: int = 60,
    evaluation_context: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Score immutable forecasts against outcomes from the same room input.

    A learned comfort-model forecast and a rules-engine thermal fallback are
    different systems. Their evidence is consequently reported and gated
    separately. Only the learned-model gate is consumed by ML control; a poor
    fallback remains visible without falsely claiming that the ML model failed.
    """

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    since = now - dt.timedelta(days=lookback_days)
    if evaluation_context is None:
        try:
            from packages.core.settings_service import get_string_setting

            mode = await get_string_setting("space_heating_baseline_mode")
            effective_mode = mode if mode in {"off", "shadow", "on"} else "shadow"
            evaluation_context = {"live_baseline_applied": effective_mode == "on"}
        except Exception:  # noqa: BLE001 - scorecard context must fail closed
            evaluation_context = {"live_baseline_applied": False}

    def empty_series() -> dict[str, Any]:
        return {
            "outcomes": {},
            "conflicts": set(),
            "abs": [],
            "signed": [],
            "predicted": [],
            "observed": [],
            "persistence_errors": [],
            "horizon_abs": {hour: [] for hour in HORIZONS},
            "horizon_signed": {hour: [] for hour in HORIZONS},
            "horizon_predicted": {hour: [] for hour in HORIZONS},
            "horizon_observed": {hour: [] for hour in HORIZONS},
            "horizon_persistence_errors": {hour: [] for hour in HORIZONS},
            "regime_abs": defaultdict(list),
            "regime_signed": defaultdict(list),
            "plans": set(),
        }

    def record_forecast_target(identity: tuple[object, ...]) -> None:
        issued_horizon = identity[:-1]
        target = identity[-1]
        previous = issued_forecast_targets.get(issued_horizon)
        if previous is not None and previous != target:
            conflicting_forecast_targets.add(issued_horizon)
            conflicting_evidence.add((*issued_horizon, previous))
            conflicting_evidence.add(identity)
        elif previous is None:
            issued_forecast_targets[issued_horizon] = target

    def record_outcome(
        series: dict[str, Any],
        *,
        identity: tuple[object, ...],
        provenance: tuple[object, ...],
        **outcome: Any,
    ) -> None:
        if identity in series["conflicts"]:
            return
        signature = (
            provenance,
            outcome["predicted"],
            outcome["observed"],
            outcome["persistence_error"],
            _weather_regimes(outcome["weather_point"]),
        )
        previous = series["outcomes"].get(identity)
        if previous is not None and previous[0] != signature:
            del series["outcomes"][identity]
            series["conflicts"].add(identity)
            conflicting_evidence.add(identity)
        elif previous is None:
            series["outcomes"][identity] = (signature, outcome)

    def add_outcome(
        series: dict[str, Any],
        *,
        forecast_origin: tuple[object, ...],
        lead_hours: int,
        abs_error: float,
        signed_error: float,
        predicted: float,
        observed: float,
        persistence_error: float,
        weather_point: dict[str, Any],
    ) -> None:
        series["abs"].append(abs_error)
        series["signed"].append(signed_error)
        series["predicted"].append(predicted)
        series["observed"].append(observed)
        series["persistence_errors"].append(persistence_error)
        series["horizon_abs"][lead_hours].append(abs_error)
        series["horizon_signed"][lead_hours].append(signed_error)
        series["horizon_predicted"][lead_hours].append(predicted)
        series["horizon_observed"][lead_hours].append(observed)
        series["horizon_persistence_errors"][lead_hours].append(persistence_error)
        series["plans"].add(forecast_origin)
        for regime in _weather_regimes(weather_point):
            series["regime_abs"][regime].append(abs_error)
            series["regime_signed"][regime].append(signed_error)

    all_series = empty_series()
    by_family = {
        "learned_comfort_model": empty_series(),
        "passive_weather_model": empty_series(),
        "rule_thermal_fallback": empty_series(),
    }
    by_source: dict[str, dict[str, Any]] = defaultdict(empty_series)
    exclusions: dict[str, int] = defaultdict(int)
    conflicting_evidence: set[tuple[object, ...]] = set()
    issued_forecast_targets: dict[tuple[object, ...], object] = {}
    conflicting_forecast_targets: set[tuple[object, ...]] = set()
    baseline_exclusions: dict[str, int] = defaultdict(int)
    baseline_pairs: list[dict[str, Any]] = []
    baseline_plans: set[int] = set()
    baseline_outcomes: dict[tuple[object, ...], tuple[dict[str, Any], int]] = {}
    baseline_predictions: dict[tuple[object, ...], tuple[float, ...]] = {}
    baseline_prediction_targets: dict[tuple[object, ...], dt.datetime] = {}
    baseline_conflicts: set[tuple[object, ...]] = set()
    baseline_plan_by_origin: dict[tuple[object, ...], int] = {}
    ambiguous_baseline_horizons: set[tuple[object, ...]] = set()
    ambiguous_baseline_scopes: set[tuple[object, ...]] = set()
    ambiguous_forecast_points: set[tuple[object, ...]] = set()
    ambiguous_forecast_sources: set[tuple[object, ...]] = set()
    ambiguous_forecast_series: set[tuple[object, ...]] = set()
    unresolved_revert_devices: set[str] = set()
    unresolved_revert_lookup_failed = False
    historical_revert_overlap_rows: list[tuple[str, dt.datetime, str, dt.datetime | None]] = []
    historical_revert_overlap_lookup_failed = False
    historical_revert_overlap_lookup_overflowed = False
    dispatch_lookup_failed = False
    dispatch_lookup_overflowed = False

    async with get_session() as session:
        plans, plan_coverage_complete, plan_high_water = await complete_plan_window(
            session, since=since, until=now, page_size=min(100, max(1, max_plans))
        )
        readings = (
            await session.execute(
                select(
                    IndoorTempReading.timestamp,
                    IndoorTempReading.temperature,
                    IndoorTempReading.device_id,
                )
                .where(IndoorTempReading.timestamp >= since)
                .where(IndoorTempReading.timestamp <= now)
                .where(IndoorTempReading.is_stale.is_(False))
                .order_by(IndoorTempReading.timestamp)
            )
        ).all()
        try:
            dispatched_action_times, dispatch_lookup_overflowed = await _dispatched_action_times(
                session, plans, since
            )
        except Exception:
            logger.exception("forecast_baseline_dispatch_lookup_failed")
            dispatched_action_times = {}
            dispatch_lookup_failed = True

        try:
            from packages.core.safety_reverts import (
                historical_revert_overlap_rows as get_historical_revert_overlap_rows,
                unresolved_revert_device_ids,
            )

            (
                unresolved_revert_devices,
                unresolved_revert_lookup_failed,
            ) = await unresolved_revert_device_ids(session)
        except Exception:
            logger.exception("forecast_baseline_unresolved_revert_lookup_failed")
            unresolved_revert_lookup_failed = True
        try:
            if plans:
                horizon_start = min(plan.horizon_start for plan in plans)
                horizon_end = max(plan.horizon_end for plan in plans)
                (
                    historical_revert_overlap_rows,
                    historical_revert_overlap_lookup_overflowed,
                ) = await get_historical_revert_overlap_rows(
                    session,
                    horizon_start=horizon_start,
                    horizon_end=horizon_end,
                    drift_margin=DISPATCH_DRIFT_MARGIN,
                )
        except Exception:
            logger.exception("forecast_baseline_historical_revert_overlap_lookup_failed")
            historical_revert_overlap_lookup_failed = True

    def saved_targets(snapshot: dict[str, Any]) -> set[tuple[object, ...]]:
        targets: set[tuple[object, ...]] = set()
        for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
            curve = snapshot.get(name)
            if not isinstance(curve, list):
                continue
            for point in curve:
                if not isinstance(point, dict) or not _required_horizon_identity(point.get("hour")):
                    continue
                target = _saved_forecast_instant(point.get("ts"))
                if target is not None:
                    targets.add((name, point["hour"], target))
        return targets

    saved_plans: list[dict[str, Any]] = []
    scopes_by_plan: dict[int, set[tuple[object, ...]]] = defaultdict(set)
    scope_values: dict[tuple[object, ...], dict[str, Any]] = {}
    statuses_by_scope: dict[tuple[object, ...], set[str]] = defaultdict(set)
    targets_by_scope: dict[tuple[object, ...], set[tuple[object, ...]]] = defaultdict(set)
    malformed_plans: set[int] = set()
    malformed_provenance: list[tuple[int | None, dict[str, Any], set[tuple[object, ...]]]] = []
    ambiguous_scopes: set[tuple[object, ...]] = set()
    for plan in plans:
        valid_plan_id = type(plan.id) is int and plan.id > 0
        try:
            payload = json.loads(plan.plan_json)
            snapshot = payload.get("forecast_snapshot") if isinstance(payload, dict) else None
        except (TypeError, ValueError):
            if valid_plan_id:
                malformed_plans.add(plan.id)
            continue
        if not isinstance(snapshot, dict):
            exclusions["forecast_unavailable_or_legacy"] += 1
            if valid_plan_id:
                malformed_plans.add(plan.id)
            continue
        if _snapshot_live_baseline_applied(snapshot) != bool(
            evaluation_context["live_baseline_applied"]
        ):
            exclusions["baseline_mode_mismatch"] += 1
            continue
        declared_targets = saved_targets(snapshot)
        reasons = []
        if not valid_plan_id:
            reasons.append("invalid_plan_identity")
        plan_device_id = payload.get("device_id")
        hints: dict[str, Any] = {}
        if not isinstance(plan_device_id, str) or not plan_device_id.strip():
            reasons.append("missing_device_provenance")
        else:
            hints["device_id"] = plan_device_id.strip()
        sensor_input = _validation_sensor_input(snapshot)
        if sensor_input is None:
            reasons.append("missing_sensor_provenance")
        else:
            hints["sensor_basis"] = (
                sensor_input["method"],
                tuple(sorted(set(sensor_input["sensor_ids"]))),
            )
        issue = _saved_forecast_instant(snapshot.get("issue_timestamp"))
        if issue is not None:
            hints["issue_timestamp"] = issue
        history = snapshot.get("observed_history")
        origin_hint = (
            _saved_forecast_instant(history[0].get("ts"))
            if isinstance(history, list) and history and isinstance(history[0], dict)
            else None
        )
        if issue is None and origin_hint is not None:
            hints["forecast_origin"] = origin_hint
        origin = _persistence_origin(snapshot)
        if origin is None:
            reasons.append(
                "legacy_scoring_schema"
                if snapshot.get("scoring_schema") != "forecast_outcome_v2_persistence_origin"
                else "missing_persistence_origin"
            )
        else:
            hints["forecast_origin"] = origin[0]
        if snapshot.get("forecast_status") not in {"available", "fallback"}:
            reasons.append("forecast_unavailable_or_legacy")
        if reasons:
            for reason in reasons:
                exclusions[reason] += 1
            if valid_plan_id:
                malformed_plans.add(plan.id)
            malformed_provenance.append(
                (plan.id if valid_plan_id else None, hints, declared_targets)
            )
            continue
        scope = tuple(
            hints[name]
            for name in ("device_id", "sensor_basis", "issue_timestamp", "forecast_origin")
        )
        scope_values[scope] = hints
        statuses_by_scope[scope].add(snapshot["forecast_status"])
        targets_by_scope[scope].update(declared_targets)
        scopes_by_plan[plan.id].add(scope)
        saved_plans.append(
            {
                "plan": plan,
                "payload": payload,
                "snapshot": snapshot,
                "sensor_input": sensor_input,
                "origin": origin,
                "scope": scope,
            }
        )

    for plan_id, scopes in scopes_by_plan.items():
        if plan_id in malformed_plans or len(scopes) > 1:
            ambiguous_scopes.update(scopes)
    ambiguous_scopes.update(
        scope for scope, statuses in statuses_by_scope.items() if len(statuses) > 1
    )
    for plan_id, hints, declared_targets in malformed_provenance:
        known_scopes = scopes_by_plan.get(plan_id, set())
        if known_scopes:
            ambiguous_scopes.update(known_scopes)
        else:
            possible_scopes = {
                scope
                for scope, values in scope_values.items()
                if all(values.get(name) == value for name, value in hints.items())
            }
            if "issue_timestamp" not in hints and "forecast_origin" not in hints:
                exclusions["unidentifiable_forecast_provenance"] += 1
                if declared_targets:
                    possible_scopes.intersection_update(
                        scope
                        for scope, targets in targets_by_scope.items()
                        if declared_targets.intersection(targets)
                    )
                elif not hints:
                    possible_scopes.clear()
            ambiguous_scopes.update(possible_scopes)
    if ambiguous_scopes:
        exclusions["ambiguous_forecast_provenance"] = len(ambiguous_scopes)

    peer_roles: dict[tuple[object, ...], set[str]] = defaultdict(set)
    peer_horizon_roles: dict[tuple[object, ...], set[str]] = defaultdict(set)
    for saved in saved_plans:
        for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
            curve = saved["snapshot"].get(name)
            if not isinstance(curve, list):
                continue
            for point in curve:
                if not isinstance(point, dict) or _forecast_source(point) == "unknown":
                    continue
                role = _forecast_evidence_role(name, _forecast_source(point))
                peer_roles[(saved["scope"], name)].add(role)
                if _required_horizon_identity(point.get("hour")):
                    peer_horizon_roles[(saved["scope"], name, point["hour"])].add(role)

    for saved in saved_plans:
        scope = saved["scope"]
        if scope in ambiguous_scopes:
            continue
        plan = saved["plan"]
        payload = saved["payload"]
        snapshot = saved["snapshot"]
        sensor_input = saved["sensor_input"]
        origin = saved["origin"]
        plan_device_id = scope_values[scope]["device_id"]
        forecast = snapshot.get("forecast_with_plan")
        shadow_forecast = snapshot.get("shadow_forecast_with_plan")
        weather = snapshot.get("weather_forecast")
        for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
            if name not in snapshot and name == "shadow_forecast_with_plan":
                continue
            curve = snapshot.get(name)
            roles = peer_roles.get((scope, name)) or {name}
            if not isinstance(curve, list) or not curve:
                ambiguous_forecast_series.update((scope, role) for role in roles)
                continue
            for point in curve:
                if not isinstance(point, dict):
                    ambiguous_forecast_series.update((scope, role) for role in roles)
                    continue
                lead = point.get("hour")
                if _required_horizon_identity(lead):
                    source = _forecast_source(point)
                    if source == "unknown":
                        horizon_roles = peer_horizon_roles.get((scope, name, lead)) or roles
                        ambiguous_forecast_points.update(
                            (scope, role, lead) for role in horizon_roles
                        )
                    elif _saved_forecast_instant(point.get("ts")) is None:
                        ambiguous_forecast_sources.add(
                            (
                                scope,
                                _forecast_evidence_role(name, source),
                                _source_kind(source),
                                lead,
                            )
                        )
        if dispatch_lookup_failed:
            baseline_exclusions["dispatch_contamination_lookup_failed"] += 1
        elif dispatch_lookup_overflowed:
            baseline_exclusions["dispatch_contamination_lookup_overflow"] += 1
        elif unresolved_revert_lookup_failed:
            baseline_exclusions["unresolved_safety_revert_lookup_failed"] += 1
        elif historical_revert_overlap_lookup_failed:
            baseline_exclusions["historical_safety_revert_lookup_failed"] += 1
        elif historical_revert_overlap_lookup_overflowed:
            baseline_exclusions["historical_safety_revert_lookup_overflow"] += 1
        elif plan_device_id in unresolved_revert_devices:
            baseline_exclusions["unresolved_safety_revert_window"] += 1
        elif any(
            device_id == plan_device_id
            and source_executed_at <= plan.horizon_end + DISPATCH_DRIFT_MARGIN
            and (
                restore_status in ("pending", "executing", "dispatched")
                or (restore_executed_at is not None and restore_executed_at >= plan.horizon_start)
            )
            for device_id, source_executed_at, restore_status, restore_executed_at in historical_revert_overlap_rows
        ):
            baseline_exclusions["historical_safety_revert_window"] += 1
        elif plan.id in dispatched_action_times:
            baseline_exclusions["dispatch_in_or_near_window"] += 1
        else:
            current_pairs: list[dict[str, Any]] = []
            _collect_baseline_pairs(
                payload=payload,
                snapshot=snapshot,
                readings=readings,
                sensor_input=sensor_input,
                plan_id=plan.id,
                pairs=current_pairs,
                plan_ids=set(),
                exclusions=baseline_exclusions,
                dispatched_at=None,
            )
            for curve_name in ("forecast_with_plan_baseline", "forecast_with_plan_zero_baseline"):
                curve = snapshot.get(curve_name)
                if not isinstance(curve, list):
                    ambiguous_baseline_scopes.add(scope)
                    continue
                for point in curve:
                    if not isinstance(point, dict) or not _required_horizon_identity(
                        point.get("hour")
                    ):
                        continue
                    lead = point["hour"]
                    target = _saved_forecast_instant(point.get("ts"))
                    if target is None:
                        ambiguous_baseline_horizons.add((scope, lead))
                        continue
                    pair_identity = (scope, lead, target)
                    issued_horizon = (scope, curve_name, lead)
                    previous_target = baseline_prediction_targets.get(issued_horizon)
                    if previous_target is not None and previous_target != target:
                        baseline_conflicts.add((scope, lead, previous_target))
                        baseline_conflicts.add(pair_identity)
                    elif previous_target is None:
                        baseline_prediction_targets[issued_horizon] = target
                    values = [point.get("predicted_indoor_temp")]
                    if curve_name == "forecast_with_plan_baseline":
                        values.append(point.get("baseline_heating_fraction"))
                    if not all(_finite_number(value) for value in values):
                        baseline_conflicts.add(pair_identity)
                        continue
                    signature = tuple(float(value) for value in values)
                    prediction_identity = (scope, curve_name, lead, target)
                    previous = baseline_predictions.get(prediction_identity)
                    if previous is not None and previous != signature:
                        baseline_conflicts.add(pair_identity)
                    else:
                        baseline_predictions[prediction_identity] = signature
            for pair in current_pairs:
                pair_identity = (scope, pair["hour"], pair["ts"])
                previous = baseline_outcomes.get(pair_identity)
                if previous is not None and previous[0] != pair:
                    baseline_conflicts.add(pair_identity)
                else:
                    baseline_outcomes[pair_identity] = (
                        pair,
                        min(plan.id, previous[1]) if previous is not None else plan.id,
                    )
        if snapshot.get("forecast_status") not in {"available", "fallback"}:
            exclusions["forecast_unavailable_or_legacy"] += 1
            continue
        if not isinstance(forecast, list):
            exclusions["missing_forecast"] += 1
            ambiguous_forecast_series.add((scope, "forecast_with_plan"))
            continue

        fallback_only = snapshot.get("forecast_status") == "fallback"
        learned_forecast = _learned_candidate(None, shadow_forecast)
        _, issue_temperature = origin
        provenance = (
            origin[0],
            plan_device_id,
            sensor_input["method"],
            tuple(sorted(set(sensor_input["sensor_ids"]))),
            snapshot["forecast_status"],
        )

        for index, point in enumerate(forecast):
            if not isinstance(point, dict):
                continue
            lead_hours = point.get("hour")
            predicted = point.get("predicted_indoor_temp")
            target_ts = _saved_forecast_instant(point.get("ts"))
            if not _required_horizon_identity(lead_hours):
                continue
            if target_ts is None:
                exclusions["missing_target_provenance"] += 1
                continue
            source = _forecast_source(point)
            if source == "unknown":
                exclusions["missing_prediction_source"] += 1
                ambiguous_forecast_points.add((scope, "forecast_with_plan", lead_hours))
                continue
            if fallback_only and _source_kind(source) == "learned_comfort_model":
                exclusions["fallback_not_learned_evidence"] += 1
                continue
            identity = (
                scope,
                _forecast_evidence_role("forecast_with_plan", source),
                _source_kind(source),
                lead_hours,
                target_ts,
            )
            record_forecast_target(identity)
            if not _finite_number(predicted):
                conflicting_evidence.add(identity)
                exclusions["non_finite_outcome"] += 1
                continue
            if target_ts > now - dt.timedelta(minutes=15) or not readings:
                continue
            observed = _observed_temperature_at(readings, target_ts, sensor_input)
            if observed is None:
                exclusions["no_matching_sensor_outcome"] += 1
                continue
            if not math.isfinite(float(predicted)) or not math.isfinite(observed):
                exclusions["non_finite_outcome"] += 1
                continue
            signed_error = float(predicted) - observed
            abs_error = abs(signed_error)
            persistence_error = issue_temperature - observed
            weather_point = (
                weather[index] if isinstance(weather, list) and index < len(weather) else {}
            )
            weather_input = weather_point if isinstance(weather_point, dict) else {}
            lead = lead_hours
            record_outcome(
                all_series,
                identity=identity,
                provenance=provenance,
                forecast_origin=scope,
                lead_hours=lead,
                abs_error=abs_error,
                signed_error=signed_error,
                predicted=float(predicted),
                observed=observed,
                persistence_error=persistence_error,
                weather_point=weather_input,
            )
            family = _source_kind(source)
            if not fallback_only and family == "learned_comfort_model":
                record_outcome(
                    by_family[family],
                    identity=identity,
                    provenance=provenance,
                    forecast_origin=scope,
                    lead_hours=lead,
                    abs_error=abs_error,
                    signed_error=signed_error,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=persistence_error,
                    weather_point=weather_input,
                )
            elif family != "learned_comfort_model":
                record_outcome(
                    by_family[family],
                    identity=identity,
                    provenance=provenance,
                    forecast_origin=scope,
                    lead_hours=lead,
                    abs_error=abs_error,
                    signed_error=signed_error,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=persistence_error,
                    weather_point=weather_input,
                )
            record_outcome(
                by_source[source],
                identity=identity,
                provenance=provenance,
                forecast_origin=scope,
                lead_hours=lead,
                abs_error=abs_error,
                signed_error=signed_error,
                predicted=float(predicted),
                observed=observed,
                persistence_error=persistence_error,
                weather_point=weather_input,
            )

        if learned_forecast is not None and learned_forecast is shadow_forecast:
            for index, point in enumerate(shadow_forecast):
                if not isinstance(point, dict):
                    continue
                lead_hours = point.get("hour")
                predicted = point.get("predicted_indoor_temp")
                target_ts = _saved_forecast_instant(point.get("ts"))
                if not _required_horizon_identity(lead_hours):
                    continue
                if target_ts is None:
                    exclusions["missing_target_provenance"] += 1
                    continue
                source = _forecast_source(point)
                if source == "unknown":
                    exclusions["missing_prediction_source"] += 1
                    ambiguous_forecast_points.add((scope, "shadow_forecast_with_plan", lead_hours))
                    continue
                if _source_kind(source) != "learned_comfort_model":
                    continue
                identity = (
                    scope,
                    _forecast_evidence_role("shadow_forecast_with_plan", source),
                    _source_kind(source),
                    lead_hours,
                    target_ts,
                )
                record_forecast_target(identity)
                if not _finite_number(predicted):
                    conflicting_evidence.add(identity)
                    exclusions["non_finite_outcome"] += 1
                    continue
                if target_ts > now - dt.timedelta(minutes=15):
                    continue
                observed = _observed_temperature_at(readings, target_ts, sensor_input)
                if observed is None or not math.isfinite(observed):
                    continue
                weather_point = (
                    weather[index] if isinstance(weather, list) and index < len(weather) else {}
                )
                record_outcome(
                    by_family["learned_comfort_model"],
                    identity=identity,
                    provenance=provenance,
                    forecast_origin=scope,
                    lead_hours=lead_hours,
                    abs_error=abs(float(predicted) - observed),
                    signed_error=float(predicted) - observed,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=issue_temperature - observed,
                    weather_point=weather_point if isinstance(weather_point, dict) else {},
                )

    if conflicting_evidence:
        exclusions["conflicting_forecast_identity"] = len(conflicting_evidence)
    if conflicting_forecast_targets:
        exclusions["conflicting_forecast_target"] = len(conflicting_forecast_targets)
    if ambiguous_forecast_points or ambiguous_forecast_sources:
        exclusions["ambiguous_forecast_point"] = len(ambiguous_forecast_points) + len(
            ambiguous_forecast_sources
        )
    if ambiguous_forecast_series:
        exclusions["ambiguous_forecast_series"] = len(ambiguous_forecast_series)
    for identity, (pair, plan_id) in baseline_outcomes.items():
        scope, lead, _ = identity
        if (
            identity not in baseline_conflicts
            and (scope, lead) not in ambiguous_baseline_horizons
            and scope not in ambiguous_baseline_scopes
        ):
            baseline_pairs.append(pair)
            baseline_plan_by_origin[scope] = min(
                plan_id, baseline_plan_by_origin.get(scope, plan_id)
            )
    baseline_plans.update(baseline_plan_by_origin.values())
    if baseline_conflicts:
        baseline_exclusions["conflicting_forecast_identity"] = len(baseline_conflicts)
    if ambiguous_baseline_horizons or ambiguous_baseline_scopes:
        baseline_exclusions["ambiguous_forecast_provenance"] = len(
            ambiguous_baseline_horizons
        ) + len(ambiguous_baseline_scopes)

    def score(series: dict[str, Any]) -> dict[str, Any]:
        for identity, (_, outcome) in series["outcomes"].items():
            if (
                identity not in conflicting_evidence
                and (identity[0], identity[1], identity[3]) not in ambiguous_forecast_points
                and identity[:4] not in ambiguous_forecast_sources
                and (identity[0], identity[1]) not in ambiguous_forecast_series
            ):
                add_outcome(series, **outcome)
        return _source_score(
            series["abs"],
            series["signed"],
            series["predicted"],
            series["observed"],
            series["persistence_errors"],
            series["horizon_abs"],
            series["horizon_signed"],
            series["horizon_predicted"],
            series["horizon_observed"],
            series["horizon_persistence_errors"],
            series["regime_abs"],
            series["regime_signed"],
            plans_scored=len(series["plans"]),
        )

    learned = score(by_family["learned_comfort_model"])
    passive = score(by_family["passive_weather_model"])
    fallback = score(by_family["rule_thermal_fallback"])
    all_forecasts = score(all_series)
    sources = {
        source: {"kind": _source_kind(source), **score(series)}
        for source, series in sorted(by_source.items())
    }
    overall = learned["overall"]
    regimes = learned["regimes"]
    horizons = learned["horizons"]
    return {
        # Backwards-compatible top-level data is deliberately *only* the
        # learned comfort model, because it is the only forecast family that
        # may authorise ML control.
        "plans_scored": learned["plans_scored"],
        "forecast_kind": "counterfactual",
        "prospective_qualification_eligible": False,
        "evidence_coverage_complete": plan_coverage_complete,
        "high_water_plan_id": plan_high_water,
        "overall": overall,
        "horizons": horizons,
        "horizon_quality": learned["horizon_quality"],
        "regimes": regimes,
        "regime_quality": learned["regime_quality"],
        "bias_correction": {
            "overall_c": _bias_correction(overall),
            "by_horizon_c": {
                str(hour): _bias_correction(
                    score_bucket(
                        by_family["learned_comfort_model"]["horizon_abs"][hour],
                        by_family["learned_comfort_model"]["horizon_signed"][hour],
                        by_family["learned_comfort_model"]["horizon_predicted"][hour],
                        by_family["learned_comfort_model"]["horizon_observed"][hour],
                        by_family["learned_comfort_model"]["horizon_persistence_errors"][hour],
                    )
                )
                for hour in HORIZONS
            },
            "maximum_abs_c": MAX_BIAS_CORRECTION_C,
            "minimum_samples": MIN_BIAS_CORRECTION_SAMPLES,
        },
        "prediction_interval": {
            "coverage": 0.8,
            "minimum_samples": MIN_INTERVAL_SAMPLES,
            "overall": prediction_interval_for_bucket(
                overall, bias_correction_c=_bias_correction(overall)
            ),
        },
        "quality_gate": learned["quality_gate"],
        "baseline_comparison": _baseline_promotion_summary(
            baseline_pairs, baseline_plans, baseline_exclusions
        ),
        "evaluation_context": evaluation_context,
        "fallback": fallback,
        "passive_weather_model": passive,
        "all_forecasts": all_forecasts,
        "sources": sources,
        "exclusions": dict(sorted(exclusions.items())),
        "coverage": {
            "observed_regimes": [
                name
                for name, bucket in regimes.items()
                if int(bucket["samples"] or 0) >= MIN_REGIME_GATE_SAMPLES
            ],
            "unobserved_regimes": [
                name
                for name, bucket in regimes.items()
                if int(bucket["samples"] or 0) < MIN_REGIME_GATE_SAMPLES
            ],
            "minimum_regime_samples": MIN_REGIME_GATE_SAMPLES,
        },
        "note": (
            "Counterfactual plan forecasts are reporting-only and cannot qualify a model. "
            "Prospective native-policy qualification uses separate immutable issues and outcomes."
        ),
    }

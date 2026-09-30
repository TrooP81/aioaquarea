"""Out-of-sample quality scoring for immutable plan forecasts."""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections import defaultdict
from typing import Any

import structlog
from sqlalchemy import desc, select

from packages.core.database import get_session
from packages.core.models import IndoorTempReading, PlanActionRecord, PlanRecord
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
    valid_r2_inputs = (
        len(predicted) == len(observed)
        and len(observed) >= 2
        and all(math.isfinite(value) for value in predicted + observed)
    )
    r2: float | None = None
    if valid_r2_inputs:
        observed_mean = statistics.fmean(observed)
        total_sum_squares = sum((value - observed_mean) ** 2 for value in observed)
        residual_sum_squares = sum(
            (observed_value - predicted_value) ** 2
            for predicted_value, observed_value in zip(predicted, observed)
        )
        if total_sum_squares > 0 and math.isfinite(residual_sum_squares):
            r2 = round(1 - residual_sum_squares / total_sum_squares, 3)
    persistence_mae = (
        sum(abs(value) for value in persistence_errors) / len(persistence_errors)
        if persistence_errors and all(math.isfinite(value) for value in persistence_errors)
        else None
    )
    mae = sum(abs_errors) / len(abs_errors) if abs_errors else None
    return {
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


def _quality_gate(
    overall: dict[str, float | int | None],
    *,
    min_r2: float = 0.15,
    min_persistence_improvement_c: float = 0.1,
    max_abs_bias_c: float = MAX_GATE_ABS_BIAS_C,
    required_horizons: dict[int, dict[str, float | int | None]] | None = None,
) -> dict[str, object]:
    """Fail closed unless all configured scorecard evidence passes."""
    samples = int(overall.get("samples") or 0)
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
    if not isinstance(mae, (int, float)) or mae > MAX_GATE_MAE_C:
        reasons.append("mae_above_threshold")
    if not isinstance(bias, (int, float)) or abs(bias) > max_abs_bias_c:
        reasons.append("bias_above_threshold")
    if not isinstance(p90, (int, float)) or p90 > MAX_GATE_P90_ABS_ERROR_C:
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
    if not isinstance(r2, (int, float)) or r2 < min_r2:
        reasons.append("r2_below_threshold")
    if (
        not isinstance(persistence_improvement, (int, float))
        or persistence_improvement < min_persistence_improvement_c
    ):
        reasons.append("persistence_improvement_below_threshold")
    if required_horizons is None:
        reasons.append("required_horizons_missing")
    else:
        for horizon in HORIZONS:
            bucket = required_horizons.get(horizon)
            if not isinstance(bucket, dict):
                reasons.append(f"horizon_{horizon}_missing")
                continue
            if int(bucket.get("samples") or 0) < MIN_HORIZON_GATE_SAMPLES:
                reasons.append(f"horizon_{horizon}_insufficient_samples")
            horizon_mae = bucket.get("mae")
            horizon_bias = bucket.get("bias")
            horizon_p90 = bucket.get("p90_abs_error")
            horizon_r2 = bucket.get("r2")
            horizon_persistence_improvement = bucket.get("persistence_improvement_c")
            if not isinstance(horizon_mae, (int, float)) or horizon_mae > MAX_GATE_MAE_C:
                reasons.append(f"horizon_{horizon}_mae_above_threshold")
            if not isinstance(horizon_bias, (int, float)) or abs(horizon_bias) > max_abs_bias_c:
                reasons.append(f"horizon_{horizon}_bias_above_threshold")
            if not isinstance(horizon_p90, (int, float)) or horizon_p90 > MAX_GATE_P90_ABS_ERROR_C:
                reasons.append(f"horizon_{horizon}_p90_error_above_threshold")
            if not isinstance(horizon_r2, (int, float)) or horizon_r2 < min_r2:
                reasons.append(f"horizon_{horizon}_r2_below_threshold")
            if (
                not isinstance(horizon_persistence_improvement, (int, float))
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
    same_schema = (
        previous.get("schema") == schema
        and tuple(previous.get("required_horizons", ())) == required_horizons
        and previous_context == normalized_context
    )
    passes = int(previous.get("pass_streak", 0)) if same_schema else 0
    failures = int(previous.get("failure_streak", 0)) if same_schema else 0
    raw_status = str(gate.get("status", "error"))
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
        "required_horizons": list(required_horizons),
        "evaluation_context": normalized_context,
        "pass_streak": passes,
        "failure_streak": failures,
        "status": status,
        "control_allowed": status == "allowed",
        "raw_status": raw_status,
        "reason": gate.get("reason", "scorecard_unavailable"),
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
        if scorecard_loader is not None:

            async def get_string(_key: str) -> str:
                return "shadow"

        else:
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
        horizon_buckets = {
            int(row["hours"]): row
            for row in scorecard.get("horizons", [])
            if isinstance(row, dict) and isinstance(row.get("hours"), int)
        }
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
        target_ts = as_utc(point.get("ts"))
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
            hour not in HORIZONS
            or target_ts is None
            or not isinstance(candidate_prediction, (int, float))
            or not isinstance(control_prediction, (int, float))
            or not isinstance(baseline_fraction, (int, float))
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

    horizons = {
        int(row["hours"]): row
        for row in scorecard.get("horizons", [])
        if isinstance(row, dict) and isinstance(row.get("hours"), int)
    }
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
    horizon_scores = {
        int(row["hours"]): row
        for row in scorecard.get("horizons", [])
        if isinstance(row, dict) and isinstance(row.get("hours"), int)
    }
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
        "control_allowed": not failed_regimes and not failed_horizons,
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


def _validation_sensor_input(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Read the immutable sensor provenance needed for a fair comparison.

    Forecasts created before ``indoor_forecast_v2`` do not state which room
    observation they started from. They are useful history, but are not valid
    ML evidence and must not be allowed to contaminate a control gate.
    """

    control_input = snapshot.get("control_input")
    if not isinstance(control_input, dict) or not control_input.get("available"):
        return None
    reference_id = control_input.get("reference_sensor_id")
    if isinstance(reference_id, str) and reference_id.strip():
        return {"method": "reference_sensor", "sensor_ids": [reference_id.strip()]}
    sensor_ids = control_input.get("sensor_ids")
    if not isinstance(sensor_ids, list):
        return None
    selected = [str(sensor_id).strip() for sensor_id in sensor_ids if str(sensor_id).strip()]
    return {"method": "selected_sensor_median", "sensor_ids": selected} if selected else None


def _persistence_origin(snapshot: dict[str, Any]) -> tuple[dt.datetime, float] | None:
    if snapshot.get("scoring_schema") != "forecast_outcome_v2_persistence_origin":
        return None
    observed_history = snapshot.get("observed_history")
    if not isinstance(observed_history, list) or not observed_history:
        return None
    origin = observed_history[0]
    if not isinstance(origin, dict) or origin.get("hour") != 0:
        return None
    issue_timestamp = as_utc(snapshot.get("issue_timestamp"))
    origin_timestamp = as_utc(origin.get("ts"))
    temperature = origin.get("temperature")
    if (
        issue_timestamp is None
        or origin_timestamp != issue_timestamp
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
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

    if sensor_input.get("method") == "reference_sensor":
        nearest = min(
            nearby,
            key=lambda row: abs((as_utc(row.timestamp) - target_ts).total_seconds()),
        )
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

    def add_outcome(
        series: dict[str, Any],
        *,
        plan_id: int,
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
        series["plans"].add(plan_id)
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
    baseline_exclusions: dict[str, int] = defaultdict(int)
    baseline_pairs: list[dict[str, Any]] = []
    baseline_plans: set[int] = set()
    unresolved_revert_devices: set[str] = set()
    unresolved_revert_lookup_failed = False
    historical_revert_overlap_rows: list[tuple[str, dt.datetime, str, dt.datetime | None]] = []
    historical_revert_overlap_lookup_failed = False
    historical_revert_overlap_lookup_overflowed = False
    dispatch_lookup_failed = False
    dispatch_lookup_overflowed = False

    async with get_session() as session:
        plans = (
            (
                await session.execute(
                    select(PlanRecord)
                    .where(PlanRecord.created_at >= since)
                    .order_by(desc(PlanRecord.created_at))
                    .limit(max_plans)
                )
            )
            .scalars()
            .all()
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
    for plan in plans:
        try:
            payload = json.loads(plan.plan_json)
            snapshot = payload.get("forecast_snapshot") if isinstance(payload, dict) else None
            forecast = snapshot.get("forecast_with_plan") if isinstance(snapshot, dict) else None
            shadow_forecast = (
                snapshot.get("shadow_forecast_with_plan") if isinstance(snapshot, dict) else None
            )
            weather = snapshot.get("weather_forecast") if isinstance(snapshot, dict) else None
        except (TypeError, ValueError):
            continue
        if not isinstance(snapshot, dict):
            exclusions["forecast_unavailable_or_legacy"] += 1
            continue
        if _snapshot_live_baseline_applied(snapshot) != bool(
            evaluation_context["live_baseline_applied"]
        ):
            exclusions["baseline_mode_mismatch"] += 1
            continue
        sensor_input = _validation_sensor_input(snapshot)
        if sensor_input is None:
            exclusions["missing_sensor_provenance"] += 1
            continue
        origin = _persistence_origin(snapshot)
        if origin is None:
            exclusions[
                "legacy_scoring_schema"
                if snapshot.get("scoring_schema") != "forecast_outcome_v2_persistence_origin"
                else "missing_persistence_origin"
            ] += 1
            continue
        plan_device_id = payload.get("device_id") if isinstance(payload, dict) else None
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
            _collect_baseline_pairs(
                payload=payload,
                snapshot=snapshot,
                readings=readings,
                sensor_input=sensor_input,
                plan_id=plan.id,
                pairs=baseline_pairs,
                plan_ids=baseline_plans,
                exclusions=baseline_exclusions,
                dispatched_at=None,
            )
        if snapshot.get("forecast_status") != "available":
            exclusions["forecast_unavailable_or_legacy"] += 1
            continue
        if not isinstance(forecast, list):
            exclusions["missing_forecast"] += 1
            continue

        learned_forecast = _learned_candidate(forecast, shadow_forecast)
        _, issue_temperature = origin

        for index, point in enumerate(forecast):
            if not isinstance(point, dict):
                continue
            lead_hours = point.get("hour")
            predicted = point.get("predicted_indoor_temp")
            target_ts = as_utc(point.get("ts")) if point.get("ts") else None
            if (
                lead_hours not in HORIZONS
                or not isinstance(predicted, (int, float))
                or target_ts is None
            ):
                continue
            source = _forecast_source(point)
            if source == "unknown":
                exclusions["missing_prediction_source"] += 1
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
            lead = int(lead_hours)
            add_outcome(
                all_series,
                plan_id=plan.id,
                lead_hours=lead,
                abs_error=abs_error,
                signed_error=signed_error,
                predicted=float(predicted),
                observed=observed,
                persistence_error=persistence_error,
                weather_point=weather_input,
            )
            family = _source_kind(source)
            if learned_forecast is forecast and family == "learned_comfort_model":
                add_outcome(
                    by_family[family],
                    plan_id=plan.id,
                    lead_hours=lead,
                    abs_error=abs_error,
                    signed_error=signed_error,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=persistence_error,
                    weather_point=weather_input,
                )
            elif family != "learned_comfort_model":
                add_outcome(
                    by_family[family],
                    plan_id=plan.id,
                    lead_hours=lead,
                    abs_error=abs_error,
                    signed_error=signed_error,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=persistence_error,
                    weather_point=weather_input,
                )
            add_outcome(
                by_source[source],
                plan_id=plan.id,
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
                target_ts = as_utc(point.get("ts")) if point.get("ts") else None
                if (
                    lead_hours not in HORIZONS
                    or not isinstance(predicted, (int, float))
                    or target_ts is None
                    or target_ts > now - dt.timedelta(minutes=15)
                    or not math.isfinite(float(predicted))
                ):
                    continue
                observed = _observed_temperature_at(readings, target_ts, sensor_input)
                if observed is None or not math.isfinite(observed):
                    continue
                source = _forecast_source(point)
                if _source_kind(source) != "learned_comfort_model":
                    continue
                weather_point = (
                    weather[index] if isinstance(weather, list) and index < len(weather) else {}
                )
                add_outcome(
                    by_family["learned_comfort_model"],
                    plan_id=plan.id,
                    lead_hours=int(lead_hours),
                    abs_error=abs(float(predicted) - observed),
                    signed_error=float(predicted) - observed,
                    predicted=float(predicted),
                    observed=observed,
                    persistence_error=issue_temperature - observed,
                    weather_point=weather_point if isinstance(weather_point, dict) else {},
                )

    def score(series: dict[str, Any]) -> dict[str, Any]:
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
            "Learned-model control evidence uses only v2 plan forecasts and outcomes from the same "
            "selected room sensor method within 30 minutes. Rules fallback evidence is reported separately."
        ),
    }

"""Out-of-sample quality scoring for immutable plan forecasts."""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import desc, select

from packages.core.database import get_session
from packages.core.models import IndoorTempReading, PlanRecord


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
    abs_errors: list[float], signed_errors: list[float]
) -> dict[str, float | int | None]:
    return {
        "samples": len(abs_errors),
        "mae": round(sum(abs_errors) / len(abs_errors), 3) if abs_errors else None,
        # Positive means the forecast was too warm; negative means it was too cold.
        "bias": round(sum(signed_errors) / len(signed_errors), 3) if signed_errors else None,
        "p90_abs_error": round(_p90(abs_errors), 3) if abs_errors else None,
        # Forecast error is predicted minus observed. These two quantiles let
        # us form an empirical 80% interval around the bias-corrected forecast.
        "p10_signed_error": round(_quantile(signed_errors, 0.1), 3) if signed_errors else None,
        "p90_signed_error": round(_quantile(signed_errors, 0.9), 3) if signed_errors else None,
    }


def _quality_gate(overall: dict[str, float | int | None]) -> dict[str, object]:
    samples = int(overall["samples"] or 0)
    if samples < MIN_GATE_SAMPLES:
        return {
            "status": "observing",
            "control_allowed": True,
            "reason": "insufficient_scored_forecasts",
            "minimum_samples": MIN_GATE_SAMPLES,
        }

    reasons: list[str] = []
    mae = overall.get("mae")
    bias = overall.get("bias")
    p90 = overall.get("p90_abs_error")
    if not isinstance(mae, (int, float)) or mae > MAX_GATE_MAE_C:
        reasons.append("mae_above_threshold")
    if not isinstance(bias, (int, float)) or abs(bias) > MAX_GATE_ABS_BIAS_C:
        reasons.append("bias_above_threshold")
    if not isinstance(p90, (int, float)) or p90 > MAX_GATE_P90_ABS_ERROR_C:
        reasons.append("p90_error_above_threshold")
    if reasons:
        return {
            "status": "failed",
            "control_allowed": False,
            "reason": ",".join(reasons),
            "maximum_mae_c": MAX_GATE_MAE_C,
            "maximum_abs_bias_c": MAX_GATE_ABS_BIAS_C,
            "maximum_p90_abs_error_c": MAX_GATE_P90_ABS_ERROR_C,
        }
    return {
        "status": "passed",
        "control_allowed": True,
        "reason": "forecast_quality_within_thresholds",
        "maximum_mae_c": MAX_GATE_MAE_C,
        "maximum_abs_bias_c": MAX_GATE_ABS_BIAS_C,
        "maximum_p90_abs_error_c": MAX_GATE_P90_ABS_ERROR_C,
    }


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
    if source == "comfort_model_controlled":
        return "learned_comfort_model"
    if source == "comfort_model_passive_direct":
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
    horizon_abs: dict[int, list[float]],
    horizon_signed: dict[int, list[float]],
    regime_abs: dict[str, list[float]],
    regime_signed: dict[str, list[float]],
    *,
    plans_scored: int,
) -> dict[str, Any]:
    overall = score_bucket(abs_errors, signed_errors)
    horizons = [
        {"hours": hour, **score_bucket(horizon_abs[hour], horizon_signed[hour])}
        for hour in HORIZONS
    ]
    regimes = {
        name: score_bucket(regime_abs[name], regime_signed[name]) for name in WEATHER_REGIMES
    }
    return {
        "plans_scored": plans_scored,
        "overall": overall,
        "quality_gate": _quality_gate(overall),
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

    def empty_series() -> dict[str, Any]:
        return {
            "abs": [],
            "signed": [],
            "horizon_abs": {hour: [] for hour in HORIZONS},
            "horizon_signed": {hour: [] for hour in HORIZONS},
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
        weather_point: dict[str, Any],
    ) -> None:
        series["abs"].append(abs_error)
        series["signed"].append(signed_error)
        series["horizon_abs"][lead_hours].append(abs_error)
        series["horizon_signed"][lead_hours].append(signed_error)
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

    for plan in plans:
        try:
            payload = json.loads(plan.plan_json)
            snapshot = payload.get("forecast_snapshot") if isinstance(payload, dict) else None
            forecast = snapshot.get("forecast_with_plan") if isinstance(snapshot, dict) else None
            weather = snapshot.get("weather_forecast") if isinstance(snapshot, dict) else None
        except (TypeError, ValueError):
            continue
        if not isinstance(snapshot, dict) or snapshot.get("forecast_status") != "available":
            exclusions["forecast_unavailable_or_legacy"] += 1
            continue
        sensor_input = _validation_sensor_input(snapshot)
        if sensor_input is None:
            exclusions["missing_sensor_provenance"] += 1
            continue
        if not isinstance(forecast, list):
            exclusions["missing_forecast"] += 1
            continue

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
            signed_error = float(predicted) - observed
            abs_error = abs(signed_error)
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
                weather_point=weather_input,
            )
            family = _source_kind(source)
            add_outcome(
                by_family[family],
                plan_id=plan.id,
                lead_hours=lead,
                abs_error=abs_error,
                signed_error=signed_error,
                weather_point=weather_input,
            )
            add_outcome(
                by_source[source],
                plan_id=plan.id,
                lead_hours=lead,
                abs_error=abs_error,
                signed_error=signed_error,
                weather_point=weather_input,
            )

    def score(series: dict[str, Any]) -> dict[str, Any]:
        return _source_score(
            series["abs"],
            series["signed"],
            series["horizon_abs"],
            series["horizon_signed"],
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

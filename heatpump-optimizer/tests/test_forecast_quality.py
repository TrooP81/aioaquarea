import datetime as dt
from types import SimpleNamespace

from packages.ml.forecast_quality import (
    _observed_temperature_at,
    _source_kind,
    _validation_sensor_input,
    _horizon_quality,
    _quality_gate,
    control_adjustments_for_weather,
    prediction_interval_for_bucket,
    prediction_intervals_for_weather,
    score_bucket,
)


def test_scorecard_uses_the_same_reference_sensor_as_the_saved_plan():
    target = dt.datetime(2026, 7, 28, 12, tzinfo=dt.timezone.utc)
    sensor_input = _validation_sensor_input(
        {
            "control_input": {
                "available": True,
                "reference_sensor_id": "living-room",
                "sensor_ids": ["living-room", "bedroom"],
            }
        }
    )

    observed = _observed_temperature_at(
        [
            SimpleNamespace(device_id="living-room", temperature=22.4, timestamp=target),
            # This deliberately different room must not silently replace the
            # configured reference just because it was sampled at the same time.
            SimpleNamespace(device_id="bedroom", temperature=19.0, timestamp=target),
        ],
        target,
        sensor_input,
    )

    assert observed == 22.4


def test_legacy_or_unavailable_plan_has_no_validation_sensor_input():
    assert _validation_sensor_input({}) is None
    assert _validation_sensor_input({"control_input": {"available": False}}) is None
    assert _source_kind("comfort_model_controlled") == "learned_comfort_model"
    assert _source_kind("comfort_model_passive_direct") == "passive_weather_model"
    assert _source_kind("linear_controlled") == "rule_thermal_fallback"


def test_quality_gate_observes_until_enough_saved_forecasts():
    gate = _quality_gate(score_bucket([0.2] * 29, [0.0] * 29))

    assert gate["status"] == "observing"
    assert gate["control_allowed"] is True


def test_quality_gate_falls_back_for_large_bias_or_tail_error():
    bucket = score_bucket([0.1] * 29 + [2.5], [0.6] * 30)
    gate = _quality_gate(bucket)

    assert gate["status"] == "failed"
    assert gate["control_allowed"] is False
    assert "bias_above_threshold" in gate["reason"]


def test_score_bucket_exposes_signed_bias_and_p90():
    bucket = score_bucket([0.2, 0.4, 1.4], [0.2, -0.4, 1.4])

    assert bucket == {
        "samples": 3,
        "mae": 0.667,
        "bias": 0.4,
        "p90_abs_error": 1.4,
        "p10_signed_error": -0.4,
        "p90_signed_error": 1.4,
    }


def test_prediction_interval_is_calibrated_from_signed_forecast_errors():
    bucket = score_bucket([0.1] * 12, [-0.4, -0.2, -0.1, 0.0, 0.0, 0.1] * 2)

    interval = prediction_interval_for_bucket(bucket, bias_correction_c=0.1)

    assert interval["status"] == "calibrated"
    assert interval["coverage"] == 0.8
    assert interval["lower_offset_c"] < 0
    assert interval["upper_offset_c"] > 0


def test_prediction_intervals_prefer_an_observed_weather_regime():
    overall = score_bucket([0.2] * 30, [0.0] * 30)
    rainy = score_bucket([0.7] * 12, [0.7] * 12)
    intervals = prediction_intervals_for_weather(
        {
            "overall": overall,
            "horizons": [],
            "regimes": {"rain": rainy},
            "bias_correction": {"by_horizon_c": {}},
        },
        [{"temperature": 12.0, "precipitation": 1.0}],
    )

    assert intervals[0]["source"] == "rain_regime"
    assert intervals[0]["status"] == "calibrated"


def test_unobserved_rain_and_cold_add_a_condition_reserve():
    scorecard = {
        "overall": score_bucket([0.2] * 30, [-0.2] * 30),
        "horizons": [],
        "regime_quality": {
            "rain": {"status": "unobserved"},
            "cold": {"status": "unobserved"},
            "mild": {"status": "passed"},
        },
    }

    adjustments = control_adjustments_for_weather(
        scorecard, [{"temperature": 2.0, "precipitation": 1.5}]
    )

    assert adjustments["control_allowed"] is True
    assert adjustments["condition_margins_c"] == [0.35]
    assert adjustments["bias_corrections_c"] == [0.2]
    assert adjustments["hourly_regimes"] == [["rain", "cold"]]


def test_failed_regime_only_blocks_when_it_is_in_the_plan_weather():
    scorecard = {
        "overall": score_bucket([0.2] * 30, [0.0] * 30),
        "horizons": [],
        "regime_quality": {
            "rain": {"status": "failed"},
            "cold": {"status": "passed"},
            "mild": {"status": "passed"},
        },
    }

    dry = control_adjustments_for_weather(scorecard, [{"temperature": 12.0, "precipitation": 0.0}])
    rainy = control_adjustments_for_weather(
        scorecard, [{"temperature": 12.0, "precipitation": 0.2}]
    )

    assert dry["control_allowed"] is True
    assert rainy["control_allowed"] is False
    assert rainy["failed_regimes"] == ["rain"]


def test_failed_measured_horizon_blocks_only_a_plan_reaching_that_lead_time():
    good = score_bucket([0.2] * 12, [0.0] * 12)
    poor = score_bucket([2.2] * 12, [2.2] * 12)
    scorecard = {
        "overall": score_bucket([0.2] * 30, [0.0] * 30),
        "horizons": [{"hours": 1, **good}, {"hours": 6, **poor}],
        "horizon_quality": {
            "1": _horizon_quality(good),
            "6": _horizon_quality(poor),
        },
        "regime_quality": {},
    }

    near_term = control_adjustments_for_weather(scorecard, [{"temperature": 12.0}])
    through_six_hours = control_adjustments_for_weather(scorecard, [{"temperature": 12.0}] * 6)

    assert near_term["control_allowed"] is True
    assert through_six_hours["control_allowed"] is False
    assert through_six_hours["failed_horizons"] == [6]

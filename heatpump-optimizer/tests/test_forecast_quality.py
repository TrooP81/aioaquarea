import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import packages.optimizer.rules_engine as rules_engine
from packages.core.heat_curve import HeatCurveConfig
from packages.core.models import PlanRecord
from packages.ml.forecast_quality import (
    MAX_DISPATCH_ACTION_LOOKUP,
    _baseline_promotion_summary,
    _collect_baseline_pairs,
    _dispatched_action_times,
    _has_persisted_dispatch_proof,
    _learned_candidate,
    apply_control_gate_hysteresis,
    _observed_temperature_at,
    _persistence_origin,
    _source_kind,
    _validation_sensor_input,
    _horizon_quality,
    _quality_gate,
    control_adjustments_for_weather,
    prediction_interval_for_bucket,
    prediction_intervals_for_weather,
    score_bucket,
)
from packages.core.settings_service import SETTINGS_SCHEMA


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


def _baseline_scorecard_plan(plan_id: int, device_id: str, target: dt.datetime) -> SimpleNamespace:
    issue = target - dt.timedelta(hours=1)
    point = {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 20.2}
    snapshot = {
        "version": "indoor_forecast_v5",
        "forecast_status": "available",
        "scoring_schema": "forecast_outcome_v2_persistence_origin",
        "issue_timestamp": issue.isoformat(),
        "observed_history": [{"hour": 0, "ts": issue.isoformat(), "temperature": 20.0}],
        "control_input": {"available": True, "reference_sensor_id": "room"},
        "space_heating_baseline": {"effective_mode": "shadow", "live_baseline_applied": False},
        "baseline_evaluation": {"learning_mode": True, "eligible": True},
        "forecast_with_plan_baseline": [{**point, "baseline_heating_fraction": 0.35}],
        "forecast_with_plan_zero_baseline": [{**point, "predicted_indoor_temp": 19.5}],
        "forecast_with_plan": [{**point, "model_source": "rule_thermal_fallback"}],
        "weather_forecast": [{}],
    }
    return SimpleNamespace(
        id=plan_id,
        created_at=issue,
        horizon_start=issue,
        horizon_end=target + dt.timedelta(hours=1),
        plan_json=json.dumps(
            {"device_id": device_id, "actions": [], "forecast_snapshot": snapshot}
        ),
    )


def _scorecard_session(plans, readings, unresolved):
    session = SimpleNamespace()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: plans)),
            SimpleNamespace(all=lambda: readings),
            SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: []),
        ]
    )
    return session


def _bucket(abs_errors: list[float], signed_errors: list[float]) -> dict:
    observed = [20.0 + index * 0.1 for index in range(len(abs_errors))]
    predicted = [value + error for value, error in zip(observed, signed_errors)]
    return score_bucket(
        abs_errors,
        signed_errors,
        predicted,
        observed,
        [0.5] * len(abs_errors),
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
    gate = _quality_gate(_bucket([0.2] * 29, [0.0] * 29))

    assert gate["status"] == "observing"
    assert gate["control_allowed"] is False


class TestControlGateHysteresis:
    def test_configurable_persistence_threshold_is_required(self):
        gate = _quality_gate(
            _bucket([0.2] * 30, [0.0] * 30),
            min_persistence_improvement_c=0.4,
        )

        assert gate["control_allowed"] is False
        assert "persistence_improvement_below_threshold" in gate["reason"]

    def test_two_passes_promote_and_one_failure_demotes(self):
        passed = {"status": "passed", "control_allowed": True, "reason": "ok"}
        failed = {"status": "failed", "control_allowed": False, "reason": "bad"}
        first = apply_control_gate_hysteresis(
            passed, {}, schema="v3", required_horizons=(1, 3), passes_required=2
        )
        second = apply_control_gate_hysteresis(
            passed, {"forecast_quality_gate": first}, schema="v3", required_horizons=(1, 3)
        )
        demoted = apply_control_gate_hysteresis(
            failed, {"forecast_quality_gate": second}, schema="v3", required_horizons=(1, 3)
        )

        assert first["control_allowed"] is False
        assert second["control_allowed"] is True
        assert demoted["status"] == "fallback"
        assert demoted["control_allowed"] is False

    def test_schema_change_resets_pass_streak_and_observing_never_permits(self):
        state = apply_control_gate_hysteresis(
            {"status": "passed", "control_allowed": True, "reason": "ok"},
            {"forecast_quality_gate": {"schema": "v2", "required_horizons": [1], "pass_streak": 9}},
            schema="v3",
            required_horizons=(1,),
        )
        observing = apply_control_gate_hysteresis(
            {"status": "observing", "control_allowed": False, "reason": "missing"},
            {},
            schema="v3",
            required_horizons=(1,),
        )

        assert state["pass_streak"] == 1
        assert state["control_allowed"] is False
        assert observing["control_allowed"] is False


def test_quality_gate_falls_back_for_large_bias_or_tail_error():
    bucket = _bucket([0.1] * 29 + [2.5], [0.6] * 30)
    gate = _quality_gate(bucket)

    assert gate["status"] == "failed"
    assert gate["control_allowed"] is False
    assert "bias_above_threshold" in gate["reason"]


def _passing_horizon(hours: int) -> dict:
    return {
        "hours": hours,
        "samples": 12,
        "mae": 0.2,
        "bias": 0.0,
        "p90_abs_error": 0.4,
        "r2": 0.3,
        "persistence_improvement_c": 0.2,
    }


def test_quality_gate_requires_per_horizon_r2_and_persistence_improvement():
    horizons = {hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)}
    horizons[6]["r2"] = 0.1
    horizons[6]["persistence_improvement_c"] = 0.0

    gate = _quality_gate(
        _bucket([0.2] * 30, [0.0] * 30),
        required_horizons=horizons,
    )

    assert gate["status"] == "failed"
    assert gate["control_allowed"] is False
    assert "horizon_6" in gate["reason"]


def test_quality_gate_requires_scorecard_control_allowed():
    overall = _bucket([0.2] * 30, [0.0] * 30)
    overall["control_allowed"] = False
    gate = _quality_gate(
        overall,
        required_horizons={hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)},
    )

    assert gate["status"] == "failed"
    assert gate["control_allowed"] is False


@pytest.mark.parametrize(
    "raw_gate", [{"status": "observing"}, {}, {"status": "failed"}, {"status": "error"}]
)
def test_observing_missing_failed_and_error_gate_to_fallback_and_degraded(raw_gate):
    gate = apply_control_gate_hysteresis(
        raw_gate,
        {},
        schema="indoor_forecast_v3",
        required_horizons=(1, 3, 6, 12, 24),
        failures_required=1,
    )

    assert gate["status"] == "fallback"
    assert gate["control_allowed"] is False

    from packages.core.comfort_assessment import build_comfort_assessment
    from packages.core.heat_curve import HeatCurveConfig

    assessment = build_comfort_assessment(
        forecast=[],
        targets=[],
        weather=[],
        planned_actions=[],
        heat_curve=HeatCurveConfig(),
        forecast_status=gate["status"],
    )
    assert assessment["state"] == "degraded"
    assert assessment["state"] != "on_target"


def test_gate_horizon_change_resets_streak():
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True, "reason": "ok"},
        {
            "forecast_quality_gate": {
                "schema": "indoor_forecast_v3",
                "required_horizons": [1, 3, 6],
                "pass_streak": 9,
            }
        },
        schema="indoor_forecast_v3",
        required_horizons=(1, 3, 6, 12),
    )

    assert state["pass_streak"] == 1
    assert state["control_allowed"] is False


@pytest.mark.asyncio
async def test_same_scorecard_is_recorded_once_and_new_scorecard_advances_streak():
    from packages.ml.comfort_model import ComfortModel
    from packages.ml.forecast_quality import evaluate_live_control_gate

    model = ComfortModel()
    scorecard_loader = AsyncMock(
        side_effect=[
            {"overall": {"samples": 0}, "horizons": []},
            {"overall": {"samples": 0}, "horizons": []},
            {"overall": {"samples": 0, "revision": 2}, "horizons": []},
        ]
    )

    async def get_float(_name):
        return 0.15

    async def get_int(name):
        return 2 if name.endswith("passes_required") else 1

    first = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
    )
    duplicate = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
    )
    new_scorecard = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
    )

    assert first["failure_streak"] == 1
    assert duplicate["failure_streak"] == 1
    assert new_scorecard["failure_streak"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_gate", "expected_status", "control_allowed"),
    [
        ({"status": "passed", "control_allowed": True, "reason": "ok"}, "allowed", True),
        ({"status": "failed", "control_allowed": False, "reason": "bad"}, "fallback", False),
    ],
)
async def test_api_quality_gate_reports_shared_result_without_mutating_streak(
    monkeypatch, raw_gate, expected_status, control_allowed
):
    from packages.api.routers import models_router
    from packages.ml.comfort_model import comfort_model

    comfort_model._metrics = {
        "forecast_quality_feature_schema": "causal_v4_hourly_heat_and_trend",
        "forecast_quality_gate": {
            "schema": "indoor_forecast_v3",
            "required_horizons": [1, 3, 6, 12, 24],
            "pass_streak": 1,
            "failure_streak": 0,
            "status": "observing",
        },
        "forecast_quality_gate_evaluation_id": "existing-scorecard",
    }
    before = comfort_model.metrics

    async def evaluate_gate(**kwargs):
        return kwargs["record_gate"](
            raw_gate,
            schema="indoor_forecast_v3",
            required_horizons=(1, 3, 6, 12, 24),
            passes_required=2,
            failures_required=1,
            evaluation_id="new-scorecard",
        )

    monkeypatch.setattr("packages.ml.forecast_quality.evaluate_live_control_gate", evaluate_gate)
    reported = await models_router._read_live_forecast_quality()

    assert reported["status"] == expected_status
    assert reported["control_allowed"] is control_allowed
    assert comfort_model.metrics == before


def test_recorded_pass_is_demoted_by_a_distinct_failure():
    from packages.ml.comfort_model import ComfortModel

    model = ComfortModel()
    passed = {"status": "passed", "control_allowed": True, "reason": "ok"}
    failed = {"status": "failed", "control_allowed": False, "reason": "bad"}

    model.record_forecast_quality_gate(
        passed,
        schema="v3",
        required_horizons=(1, 3),
        passes_required=2,
        failures_required=1,
        evaluation_id="pass-1",
    )
    allowed = model.record_forecast_quality_gate(
        passed,
        schema="v3",
        required_horizons=(1, 3),
        passes_required=2,
        failures_required=1,
        evaluation_id="pass-2",
    )
    demoted = model.record_forecast_quality_gate(
        failed,
        schema="v3",
        required_horizons=(1, 3),
        passes_required=2,
        failures_required=1,
        evaluation_id="fail-1",
    )

    assert allowed["status"] == "allowed"
    assert demoted["status"] == "fallback"
    assert demoted["control_allowed"] is False
    assert demoted["pass_streak"] == 0
    assert demoted["failure_streak"] == 1


def test_indoor_forecast_gate_settings_are_registered_with_approved_defaults():
    expected = {
        "indoor_forecast_min_r2": "0.15",
        "indoor_forecast_min_persistence_improvement_c": "0.1",
        "indoor_forecast_max_abs_bias_c": "0.5",
        "indoor_forecast_gate_passes_required": "2",
        "indoor_forecast_gate_failures_required": "1",
    }

    assert {key: SETTINGS_SCHEMA[key]["default"] for key in expected} == expected


def test_score_bucket_exposes_signed_bias_and_p90():
    bucket = _bucket([0.2, 0.4, 1.4], [0.2, -0.4, 1.4])

    assert bucket == {
        "samples": 3,
        "mae": 0.667,
        "bias": 0.4,
        "p90_abs_error": 1.4,
        "p10_signed_error": -0.4,
        "p90_signed_error": 1.4,
        "r2": -107.0,
        "persistence_mae": 0.5,
        "persistence_improvement_c": -0.167,
    }


def test_prediction_interval_is_calibrated_from_signed_forecast_errors():
    bucket = _bucket([0.1] * 12, [-0.4, -0.2, -0.1, 0.0, 0.0, 0.1] * 2)

    interval = prediction_interval_for_bucket(bucket, bias_correction_c=0.1)

    assert interval["status"] == "calibrated"
    assert interval["coverage"] == 0.8
    assert interval["lower_offset_c"] < 0
    assert interval["upper_offset_c"] > 0


def test_prediction_intervals_prefer_an_observed_weather_regime():
    overall = _bucket([0.2] * 30, [0.0] * 30)
    rainy = _bucket([0.7] * 12, [0.7] * 12)
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
        "overall": _bucket([0.2] * 30, [-0.2] * 30),
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
        "overall": _bucket([0.2] * 30, [0.0] * 30),
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
    good = _bucket([0.2] * 12, [0.0] * 12)
    poor = _bucket([2.2] * 12, [2.2] * 12)
    scorecard = {
        "overall": _bucket([0.2] * 30, [0.0] * 30),
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


def test_score_bucket_computes_r2_and_persistence_improvement():
    bucket = score_bucket(
        [0.1, 0.1, 0.1],
        [0.1, -0.1, 0.1],
        [20.1, 20.9, 22.1],
        [20.0, 21.0, 22.0],
        [0.5, -0.5, 0.5],
    )

    assert bucket["r2"] == 0.985
    assert bucket["persistence_mae"] == 0.5
    assert bucket["persistence_improvement_c"] == 0.4


def test_constant_observations_have_no_r2():
    bucket = score_bucket([0.1, 0.1], [0.1, -0.1], [20.1, 19.9], [20.0, 20.0], [0.5, 0.5])

    assert bucket["r2"] is None


def test_horizon_metrics_use_issue_temperature_baseline():
    bucket = score_bucket([0.2], [0.2], [20.2], [20.0], [1.0])

    assert bucket["persistence_mae"] == 1.0
    assert bucket["persistence_improvement_c"] == 0.8


def test_legacy_snapshot_without_origin_is_excluded():
    assert _persistence_origin({"observed_history": []}) is None


def test_live_learned_series_precedes_shadow():
    live = [{"model_source": "comfort_model_controlled"}]
    shadow = [{"model_source": "comfort_model_physics_continuation"}]

    assert _learned_candidate(live, shadow) is live


def test_shadow_no_heating_is_not_promotion_evidence():
    snapshot = {
        "forecast_with_plan": [],
        "shadow_forecast_no_heating": [{"model_source": "comfort_model_controlled"}],
    }

    assert (
        _learned_candidate(
            snapshot["forecast_with_plan"], snapshot.get("shadow_forecast_with_plan")
        )
        is None
    )


def test_continuation_tag_maps_to_learned_family():
    assert _source_kind("comfort_model_physics_continuation") == "learned_comfort_model"


def _promotion_pairs() -> list[dict]:
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return [
        {
            "hour": hour,
            "ts": start + dt.timedelta(days=index % 7, hours=hour),
            "candidate_abs_error": 0.2,
            "candidate_signed_error": 0.0,
            "control_abs_error": 0.4,
            "control_signed_error": 0.0,
        }
        for hour in (1, 3, 6, 12, 24)
        for index in range(12)
    ]


def test_baseline_comparison_scores_fallback_shadow_pairs():
    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    pairs: list[dict] = []
    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 1,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions={},
    )

    summary = _baseline_promotion_summary(pairs, {1}, {})

    assert summary["candidate"]["mae"] == 0.2
    assert summary["zero_baseline"]["mae"] == 0.5
    assert summary["mae_improvement_c"] == 0.3


@pytest.mark.parametrize(
    ("learning_mode", "eligible", "expected_pairs"),
    [(False, False, 0), (True, True, 1), (False, True, 0), (True, False, 0)],
)
def test_baseline_comparison_requires_learning_mode_and_eligibility(
    learning_mode, eligible, expected_pairs
):
    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    pairs: list[dict] = []
    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot={
            "baseline_evaluation": {"learning_mode": learning_mode, "eligible": eligible},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 1,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions={},
    )

    assert len(pairs) == expected_pairs


@pytest.mark.asyncio
async def test_learning_mode_lookup_error_does_not_admit_baseline_comparison():
    async def unavailable() -> bool:
        raise RuntimeError("settings unavailable")

    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    with patch("packages.optimizer.executor_core.is_learning_mode_active", unavailable):
        learning_mode_active = await rules_engine._resolve_learning_mode()
    snapshot = rules_engine.RulesOptimizer._build_forecast_snapshot(
        prices=[(target - dt.timedelta(hours=1), 0.1)],
        weather=[(target - dt.timedelta(hours=1), 5.0)],
        weather_full=[],
        actions=[],
        horizon_start=target - dt.timedelta(hours=1),
        current_indoor=20.0,
        current_water_temp=35.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule={},
        comfort_temp_target=20.0,
        comfort_temp_min=18.0,
        tz_name="UTC",
        effective_baseline_mode="shadow",
        learning_mode_active=learning_mode_active,
    )
    snapshot["forecast_with_plan_baseline"] = [
        {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 20.2}
    ]
    snapshot["forecast_with_plan_zero_baseline"] = [
        {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
    ]
    pairs: list[dict] = []

    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot=snapshot,
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions={},
    )

    assert snapshot["baseline_evaluation"] == {"learning_mode": False, "eligible": False}
    assert pairs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_rows", "offset", "expected_dispatched", "expected_pairs"),
    [
        (["device-a"], dt.timedelta(hours=2), True, 0),
        (["device-a"], dt.timedelta(hours=-23), True, 0),
        (["device-a"], dt.timedelta(hours=24, seconds=90), True, 1),
        (["device-a"], dt.timedelta(hours=-25), False, 1),
        (["device-b"], dt.timedelta(hours=2), False, 1),
        ([], dt.timedelta(hours=2), False, 1),
    ],
    ids=[
        "in_window",
        "carryover_within_lookback",
        "terminal_timestamp_within_drift_margin",
        "before_carryover_lookback",
        "unrelated_device",
        "no_dispatch",
    ],
)
async def test_baseline_comparison_dispatch_exclusion_uses_device_and_plan_window(
    action_rows, offset, expected_dispatched, expected_pairs
):
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    dispatched_at = start + offset
    scored_plan = SimpleNamespace(
        id=2,
        plan_json='{"device_id": "device-a"}',
        horizon_start=start,
        horizon_end=start + dt.timedelta(hours=24),
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        all=lambda: [
            (
                device_id,
                dispatched_at,
                "executed",
                json.dumps({"success": True, "verified": True}),
                1,
            )
            for device_id in action_rows
        ]
    )

    dispatched, overflowed = await _dispatched_action_times(session, [scored_plan], start)
    pairs: list[dict] = []
    exclusions: dict[str, int] = {}
    target = start + dt.timedelta(hours=3)
    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 3,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 3, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=scored_plan.id,
        pairs=pairs,
        plan_ids=set(),
        exclusions=exclusions,
        dispatched_at=dispatched.get(scored_plan.id),
    )

    assert (scored_plan.id in dispatched) is expected_dispatched
    assert overflowed is False
    assert len(pairs) == expected_pairs


@pytest.mark.asyncio
async def test_dispatched_action_lookup_reports_overflow():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    plan = SimpleNamespace(
        id=1,
        plan_json='{"device_id": "device-a"}',
        horizon_start=start,
        horizon_end=start + dt.timedelta(hours=24),
    )
    rows = [
        (
            "device-a",
            start + dt.timedelta(minutes=index),
            "executed",
            json.dumps({"success": True, "verified": True}),
            1,
        )
        for index in range(MAX_DISPATCH_ACTION_LOOKUP + 1)
    ]
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=lambda: rows)

    dispatched, overflowed = await _dispatched_action_times(session, [plan], start)

    assert overflowed is True
    assert dispatched == {1: start}


@pytest.mark.parametrize(
    ("status", "result_json", "verify_attempts", "expected"),
    [
        ("executed", json.dumps({"success": True, "verified": True}), 1, True),
        ("failed", json.dumps({"success": False}), 1, True),
        ("cancelled", json.dumps({"dispatched": True}), 0, True),
        ("cancelled", json.dumps({"reason": "shutdown_cancelled"}), 1, True),
        ("cancelled", json.dumps({"reason": "superseded"}), 0, False),
        ("pending", None, 0, False),
    ],
    ids=[
        "verified",
        "verification_failed",
        "dispatched_then_cancelled",
        "shutdown_cancelled_after_verification",
        "superseded",
        "unclaimed",
    ],
)
def test_dispatch_contamination_requires_persisted_executor_proof(
    status, result_json, verify_attempts, expected
):
    assert _has_persisted_dispatch_proof(status, result_json, verify_attempts) is expected


def test_baseline_comparison_excludes_dispatched_or_explicit_windows():
    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    pairs: list[dict] = []
    exclusions: dict[str, int] = {}
    _collect_baseline_pairs(
        payload={"actions": [{"type": "zone_temp_boost", "ts": target.isoformat()}]},
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 1,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions=exclusions,
    )

    assert pairs == []
    assert exclusions == {"explicit_or_dispatched_window": 1}


def test_promotion_requires_all_sample_and_accuracy_thresholds():
    pairs = _promotion_pairs()
    passing = _baseline_promotion_summary(pairs, set(range(20)), {})
    assert passing["promotion_ready"] is True

    cases = [
        (pairs, set(range(19))),
        (pairs[:-1], set(range(20))),
        (pairs, set(range(20)), 6),
        (pairs[:-1], set(range(20))),
        ([{**pair, "candidate_abs_error": 0.35} for pair in pairs], set(range(20))),
        ([{**pair, "candidate_signed_error": 0.51} for pair in pairs], set(range(20))),
        ([{**pair, "candidate_abs_error": 2.1} for pair in pairs], set(range(20))),
        (
            [
                {**pair, "candidate_abs_error": 0.6} if pair["hour"] == 24 else pair
                for pair in pairs
            ],
            set(range(20)),
        ),
    ]
    for case in cases:
        case_pairs, plans, *day_limit = case
        if day_limit:
            case_pairs = [
                {**pair, "ts": pair["ts"].replace(day=1 + index % day_limit[0])}
                for index, pair in enumerate(case_pairs)
            ]
        assert _baseline_promotion_summary(case_pairs, plans, {})["promotion_ready"] is False


def test_baseline_comparison_retains_shadow_specific_evidence():
    pairs: list[dict] = []
    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "off"},
        },
        readings=[],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions={},
    )

    assert pairs == []


def test_off_to_shadow_preserves_gate_streaks():
    previous = {
        "forecast_quality_gate": {
            "schema": "v5",
            "required_horizons": [1],
            "evaluation_context": {"live_baseline_applied": False},
            "pass_streak": 1,
        }
    }
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True}, previous, schema="v5", required_horizons=(1,)
    )

    assert state["pass_streak"] == 2


@pytest.mark.parametrize(
    "context", [{"live_baseline_applied": True}, {"live_baseline_applied": False}]
)
def test_not_on_to_on_resets_gate_streaks(context):
    previous = {
        "forecast_quality_gate": {
            "schema": "v5",
            "required_horizons": [1],
            "evaluation_context": {"live_baseline_applied": not context["live_baseline_applied"]},
            "pass_streak": 4,
        }
    }
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True},
        previous,
        schema="v5",
        required_horizons=(1,),
        evaluation_context=context,
    )

    assert state["pass_streak"] == 1


def test_on_to_shadow_resets_gate_streaks():
    previous = {
        "forecast_quality_gate": {
            "schema": "v5",
            "required_horizons": [1],
            "evaluation_context": {"live_baseline_applied": True},
            "pass_streak": 4,
        }
    }
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True}, previous, schema="v5", required_horizons=(1,)
    )

    assert state["pass_streak"] == 1


@pytest.mark.parametrize(
    "version",
    ["indoor_forecast_v1", "indoor_forecast_v2", "indoor_forecast_v3", "indoor_forecast_v4"],
)
def test_v1_through_v4_map_live_baseline_applied_false(version):
    from packages.ml.forecast_quality import _snapshot_live_baseline_applied

    assert _snapshot_live_baseline_applied({"version": version}) is False


def test_pre_migration_gate_state_without_context_carries_forward():
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True},
        {"forecast_quality_gate": {"schema": "v5", "required_horizons": [1], "pass_streak": 1}},
        schema="v5",
        required_horizons=(1,),
    )

    assert state["pass_streak"] == 2


def test_missing_gate_context_defaults_live_baseline_applied_false():
    state = apply_control_gate_hysteresis(
        {"status": "passed", "control_allowed": True},
        {"forecast_quality_gate": {"schema": "v5", "required_horizons": [1], "pass_streak": 1}},
        schema="v5",
        required_horizons=(1,),
    )

    assert state["evaluation_context"] == {"live_baseline_applied": False}


def test_baseline_comparison_excludes_dispatched_mode_action():
    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    pairs: list[dict] = []
    exclusions: dict[str, int] = {}
    _collect_baseline_pairs(
        payload={
            "actions": [
                {"type": "normal_mode_on", "ts": target.isoformat(), "status": "dispatched"}
            ]
        },
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 1,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions=exclusions,
    )

    assert pairs == []
    assert exclusions == {"explicit_or_dispatched_window": 1}


def test_baseline_comparison_excludes_persisted_dispatch_window():
    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    pairs: list[dict] = []
    exclusions: dict[str, int] = {}

    _collect_baseline_pairs(
        payload={"actions": []},
        snapshot={
            "baseline_evaluation": {"learning_mode": True, "eligible": True},
            "space_heating_baseline": {"effective_mode": "shadow"},
            "forecast_with_plan_baseline": [
                {
                    "hour": 1,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": 20.2,
                    "baseline_heating_fraction": 0.35,
                }
            ],
            "forecast_with_plan_zero_baseline": [
                {"hour": 1, "ts": target.isoformat(), "predicted_indoor_temp": 19.5}
            ],
        },
        readings=[SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)],
        sensor_input={"method": "reference_sensor", "sensor_ids": ["room"]},
        plan_id=1,
        pairs=pairs,
        plan_ids=set(),
        exclusions=exclusions,
        dispatched_at=target,
    )

    assert pairs == []
    assert exclusions == {"explicit_or_dispatched_window": 1}


@pytest.mark.asyncio
async def test_scorecard_excludes_only_devices_with_unresolved_safety_reverts():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plans = [
        _baseline_scorecard_plan(1, "device-with-open-revert", target),
        _baseline_scorecard_plan(2, "unrelated-device", target),
        _baseline_scorecard_plan(3, "device-with-resolved-revert", target),
    ]
    readings = [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)]
    session = _scorecard_session(plans, readings, {"device-with-open-revert"})

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=({"device-with-open-revert"}, False)),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    comparison = scorecard["baseline_comparison"]
    assert comparison["pairs_scored"] == 2
    assert comparison["plans_scored"] == 2
    assert comparison["exclusions"] == {"unresolved_safety_revert_window": 1}


@pytest.mark.asyncio
async def test_scorecard_fails_closed_when_unresolved_revert_lookup_fails():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plans = [_baseline_scorecard_plan(1, "device-a", target)]
    readings = [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)]
    session = _scorecard_session(plans, readings, set())

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    comparison = scorecard["baseline_comparison"]
    assert comparison["pairs_scored"] == 0
    assert comparison["plans_scored"] == 0
    assert comparison["exclusions"] == {"unresolved_safety_revert_lookup_failed": 1}


@pytest.mark.asyncio
async def test_scorecard_fails_closed_when_dispatch_lookup_overflows():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plans = [_baseline_scorecard_plan(1, "device-a", target)]
    readings = [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)]
    session = _scorecard_session(plans, readings, set())

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.ml.forecast_quality._dispatched_action_times",
            new=AsyncMock(return_value=({}, True)),
        ),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=(set(), False)),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    comparison = scorecard["baseline_comparison"]
    assert comparison["pairs_scored"] == 0
    assert comparison["plans_scored"] == 0
    assert comparison["exclusions"] == {"dispatch_contamination_lookup_overflow": 1}


@pytest.mark.asyncio
async def test_scorecard_fails_closed_when_dispatch_lookup_errors():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plans = [_baseline_scorecard_plan(1, "device-a", target)]
    readings = [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)]
    session = _scorecard_session(plans, readings, set())

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.ml.forecast_quality._dispatched_action_times",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=(set(), False)),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    comparison = scorecard["baseline_comparison"]
    assert comparison["pairs_scored"] == 0
    assert comparison["plans_scored"] == 0
    assert comparison["exclusions"] == {"dispatch_contamination_lookup_failed": 1}


@pytest.mark.asyncio
async def test_scorecard_excludes_historical_revert_overlap():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plan = _baseline_scorecard_plan(1, "device-a", target)
    session = _scorecard_session(
        [plan], [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)], set()
    )

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=(set(), False)),
        ),
        patch(
            "packages.core.safety_reverts.historical_revert_overlap_rows",
            new=AsyncMock(
                return_value=(
                    [
                        (
                            "device-a",
                            plan.horizon_start - dt.timedelta(hours=1),
                            "executed",
                            plan.horizon_start + dt.timedelta(minutes=30),
                        )
                    ],
                    False,
                ),
            ),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    assert scorecard["baseline_comparison"]["exclusions"] == {"historical_safety_revert_window": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (([], True), "historical_safety_revert_lookup_overflow"),
        (RuntimeError("database unavailable"), "historical_safety_revert_lookup_failed"),
    ],
    ids=["overflow", "error"],
)
async def test_scorecard_fails_closed_when_historical_revert_lookup_is_unavailable(
    result, expected_reason
):
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plan = _baseline_scorecard_plan(1, "device-a", target)
    session = _scorecard_session(
        [plan], [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)], set()
    )
    historical_lookup = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=(set(), False)),
        ),
        patch(
            "packages.core.safety_reverts.historical_revert_overlap_rows",
            new=historical_lookup,
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    assert scorecard["baseline_comparison"]["pairs_scored"] == 0
    assert scorecard["baseline_comparison"]["exclusions"] == {expected_reason: 1}


@pytest.mark.asyncio
async def test_scorecard_accepts_real_plan_records():
    from packages.ml.forecast_quality import get_forecast_scorecard

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    source = _baseline_scorecard_plan(1, "device-a", target)
    plan = PlanRecord(
        id=source.id,
        created_at=source.created_at,
        horizon_start=source.horizon_start,
        horizon_end=source.horizon_end,
        plan_json=source.plan_json,
        optimizer_version="rules_v5",
        status="active",
    )
    readings = [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)]
    session = _scorecard_session([plan], readings, set())

    with (
        patch("packages.ml.forecast_quality.get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_device_ids",
            new=AsyncMock(return_value=(set(), False)),
        ),
    ):
        scorecard = await get_forecast_scorecard(
            now=now,
            evaluation_context={"live_baseline_applied": False},
        )

    assert scorecard["baseline_comparison"]["pairs_scored"] == 1
    assert scorecard["baseline_comparison"]["plans_scored"] == 1

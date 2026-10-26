import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

import packages.optimizer.rules_engine as rules_engine
from packages.core.heat_curve import HeatCurveConfig
from packages.core.models import PlanRecord
from packages.ml.forecast_quality import (
    DISPATCH_DRIFT_MARGIN,
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
    native_scope,
)
from packages.core.settings_service import SETTINGS_SCHEMA


class TestProspectiveNativeEvidence:
    BASE = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)

    @classmethod
    def _inputs(cls):
        native = {
            "mode": "1",
            "operation_status": 1,
            "zone1_target_temp": 35.0,
            "tank_target_temp": 45,
            "force_dhw": 0,
            "force_heater": 0,
            "holiday_mode": 0,
        }
        timer = {
            "status": "known",
            "enabled": False,
            "slots": [],
            "timezone": "UTC",
            "fetched_at": cls.BASE.isoformat(),
        }
        history = [
            {
                "ts": (cls.BASE - dt.timedelta(minutes=60 - 5 * index)).isoformat(),
                "device_id": "heatpump",
                "space_heating_active": False,
                "space_heating_evidence": "idle",
                "zone1_temp": 30.0,
                "device_action": "IDLE",
                "direction": "IDLE",
                "defrost_active": False,
            }
            for index in range(13)
        ]
        return {
            "issued_at": cls.BASE.isoformat(),
            "device_id": "heatpump",
            "capability": "indoor",
            "native_settings": native,
            "timer": timer,
            "status_history": history,
            "sensor_basis": {"kind": "reference", "sensor_ids": ["room"], "resolution_c": 0.1},
            "sensor_history": [
                {"timestamp": cls.BASE.isoformat(), "temperature": 21.0, "device_id": "room"}
            ],
            "weather": [
                {
                    "ts": (cls.BASE + dt.timedelta(hours=hour)).isoformat(),
                    "temperature": 8.0,
                    "forecast_issued_at": cls.BASE.isoformat(),
                }
                for hour in range(25)
            ],
            "pricing": {"available": False, "source": "unavailable", "points": []},
            "schemas": {"input": "test_inputs_v1", "output": "comfort_v8"},
        }

    @classmethod
    def _issue(cls):
        from packages.ml.forecast_quality import freeze_native_pair

        return freeze_native_pair(
            cls._inputs(),
            epoch="epoch",
            component="comfort",
            candidate_hash="c" * 64,
            incumbent_hash="b" * 64,
            horizons=(60,),
            candidate_predictor=lambda _, horizons: {h: 21.1 for h in horizons},
            incumbent_predictor=lambda _, horizons: {h: 22.0 for h in horizons},
            baseline_predictor=lambda _, horizons: {h: 21.0 for h in horizons},
        ).rows()[0]

    @classmethod
    def _trace(cls, issue):
        snapshot = issue["input_snapshot"]
        return {
            "lookup_complete": True,
            "overflow": False,
            "actions": [],
            "overrides": [],
            "restores": [],
            "scope": snapshot["scope"],
            "statuses": [
                {
                    **snapshot["status_history"][-1],
                    "ts": (cls.BASE + dt.timedelta(minutes=5 * index)).isoformat(),
                    "native_policy_provenance": {
                        "schema": "native_unchanged_v1",
                        "settings": snapshot["native_settings"],
                        "timer": dict(snapshot["timer"]),
                    },
                }
                for index in range(13)
            ],
        }

    @classmethod
    def _pairs(cls):
        pairs = []
        scope = {
            "policy_schema": "native_unchanged_v1",
            "sensor_fingerprint": "sensor",
            "policy_fingerprint": "native",
            "schema_fingerprint": "schema",
        }
        for day in range(14):
            for cohort in range(4):
                issued = cls.BASE + dt.timedelta(days=day, hours=cohort * 6)
                observed = 20.0 + (day % 4) * 0.6 + cohort * 0.5
                for horizon in (60, 180, 360, 720, 1440):
                    target = issued + dt.timedelta(minutes=horizon)
                    pairs.append(
                        {
                            "device_id": "heatpump",
                            "canonical_key": f"{day}:{cohort}",
                            "cohort_key": f"{day}:{cohort}",
                            "horizon_minutes": horizon,
                            "issued_at": issued.isoformat(),
                            "target_at": target.isoformat(),
                            "observed_at": target.isoformat(),
                            "status": "valid",
                            "candidate_hash": "c" * 64,
                            "incumbent_hash": "b" * 64,
                            "issue_kind": "prospective_native",
                            "policy_schema": "native_unchanged_v1",
                            "capability": "indoor",
                            "scope": scope,
                            "outdoor_bin": (8, 10, 12)[day % 3],
                            "observed": observed,
                            "candidate": observed + 0.10 + 0.01 * ((day + cohort) % 4),
                            "incumbent": observed + 1.0 + 0.02 * day + 0.03 * cohort,
                            "baseline": observed + 1.2 + 0.03 * day + 0.02 * cohort,
                        }
                    )
        return pairs, scope

    @classmethod
    def _qualify(cls, pairs=None, **options):
        from packages.ml.forecast_quality import qualify_prospective

        default_pairs, scope = cls._pairs()
        arguments = {
            "component": "comfort",
            "candidate_hash": "c" * 64,
            "frozen_at": cls.BASE,
            "data_cutoff": cls.BASE - dt.timedelta(hours=1),
            "now": cls.BASE + dt.timedelta(days=15),
            "horizons": (60, 180, 360, 720, 1440),
            "scope": scope,
            "capabilities": ("indoor",),
            "coverage_complete": True,
        }
        arguments.update(options)
        return qualify_prospective(default_pairs if pairs is None else pairs, **arguments)

    def test_frozen_native_pair_shares_inputs(self):
        from packages.ml.forecast_quality import freeze_native_pair

        inputs = self._inputs()
        seen = []

        def predict(snapshot, horizons):
            seen.append(json.dumps(snapshot, sort_keys=True))
            snapshot["sensor_history"][0]["temperature"] = 99.0
            return {h: 21.0 for h in horizons}

        issue = freeze_native_pair(
            inputs,
            epoch="epoch",
            component="comfort",
            candidate_hash="c" * 64,
            incumbent_hash="b" * 64,
            horizons=(60,),
            candidate_predictor=predict,
            incumbent_predictor=predict,
            baseline_predictor=predict,
        )
        assert issue is not None
        assert seen[0] == seen[1] == seen[2]
        inputs["native_settings"]["zone1_target_temp"] = 99.0
        assert issue.rows()[0]["input_snapshot"]["native_settings"]["zone1_target_temp"] == 35.0
        assert issue.rows()[0]["input_snapshot"]["sensor_history"][0]["temperature"] == 21.0
        assert (
            issue.rows()[0]["input_snapshot"]["expected_native_trajectory"]["schema"]
            == "native_persistence_15m_v1"
        )

    @pytest.mark.parametrize(
        "case,reason,status",
        [
            ("action", "unmatched_action", "excluded"),
            ("override", "override", "excluded"),
            ("timer", "timer_effect", "excluded"),
            ("unknown_timer", "unknown_timer", "excluded"),
            ("dhw", "dhw_diversion", "excluded"),
            ("restore", "unresolved_restore", "excluded"),
            ("unknown", "unknown_execution", "excluded"),
            ("gap", "stale_or_gapped_telemetry", "excluded"),
            ("overflow", "execution_lookup_incomplete", "incomplete"),
            ("lookup", "execution_lookup_incomplete", "incomplete"),
            ("uncertain_dispatch", "unknown_dispatch", "incomplete"),
        ],
    )
    def test_action_timer_override_dhw_unknown_exclusions_apply_to_both(self, case, reason, status):
        from packages.ml.forecast_quality import reconcile_native_issue

        issue = self._issue()
        trace = self._trace(issue)
        if case == "action":
            trace["actions"] = [{"dispatched": True}]
        elif case == "override":
            trace["overrides"] = [{"active": True}]
        elif case in {"timer", "unknown_timer"}:
            timer = trace["statuses"][-1]["native_policy_provenance"]["timer"]
            timer["enabled"] = True
            if case == "unknown_timer":
                timer["status"] = "unknown"
        elif case == "dhw":
            trace["statuses"][-1]["direction"] = "WATER"
        elif case == "restore":
            trace["restores"] = [{"status": "pending"}]
        elif case == "unknown":
            trace["statuses"][-1]["native_policy_provenance"] = None
        elif case == "gap":
            trace["statuses"] = trace["statuses"][:1] + trace["statuses"][-1:]
        elif case == "overflow":
            trace["overflow"] = True
        elif case == "lookup":
            trace["lookup_complete"] = False
        else:
            trace["actions"] = [{"never_dispatched": False}]
        outcome = reconcile_native_issue(
            issue, trace, observed=21.0, observed_at=issue["target_at"], now=issue["target_at"]
        )
        assert outcome["status"] == status
        assert outcome["exclusion_reason"] == reason
        assert outcome["result"] == {}
        assert set(issue["predictions"]) == {"candidate", "incumbent", "baseline"}

    @pytest.mark.parametrize(
        "case,reason",
        [
            ("trajectory", "native_trajectory_mismatch"),
            ("timer", "timer_effect"),
            ("override", "override"),
        ],
    )
    def test_reconciled_exclusion_reason_survives_scoring(self, case, reason):
        from packages.ml.forecast_quality import reconcile_native_issue

        issue = self._issue()
        trace = self._trace(issue)
        if case == "trajectory":
            trace["statuses"][-1]["zone1_temp"] += 1.01
        elif case == "timer":
            trace["statuses"][-1]["native_policy_provenance"]["timer"]["enabled"] = True
        else:
            trace["overrides"] = [{"active": True}]
        outcome = reconcile_native_issue(
            issue, trace, observed=21.0, observed_at=issue["target_at"], now=issue["target_at"]
        )
        assert outcome["status"] == "excluded" and outcome["exclusion_reason"] == reason
        assert outcome["observed_at"] is None
        pair = {
            **issue,
            **issue["predictions"],
            **outcome,
            "scope": issue["input_snapshot"]["scope"],
            "capability": issue["input_snapshot"]["capability"],
            "outdoor_bin": 8,
            "canonical_key": issue["cohort_key"],
        }
        result = self._qualify([pair], scope=pair["scope"])
        assert result["qualified"] is False and result["counts"]["pairs"] == 0
        assert result["exclusions"] == {reason: 1}
        assert result["exclusions_by_horizon"] == {"60": {reason: 1}}
        assert result["exclusions_by_outdoor_bin"] == {"8": {reason: 1}}

    def test_proven_never_dispatched_supersession_is_not_contamination(self):
        from packages.ml.forecast_quality import reconcile_native_issue

        issue = self._issue()
        trace = self._trace(issue)
        trace["actions"] = [{"never_dispatched": True, "reason": "superseded"}]
        outcome = reconcile_native_issue(
            issue, trace, observed=21.0, observed_at=issue["target_at"], now=issue["target_at"]
        )
        assert outcome["status"] == "valid"
        assert outcome["result"]["observed"] == 21.0

    @pytest.mark.parametrize("kind", ["counterfactual", "retrospective_actual_action"])
    def test_counterfactual_and_reconditioned_are_not_prospective(self, kind):
        from packages.ml.forecast_quality import reconcile_native_issue

        issue = self._issue()
        issue["issue_kind"] = kind
        outcome = reconcile_native_issue(
            issue,
            self._trace(issue),
            observed=21.0,
            observed_at=issue["target_at"],
            now=issue["target_at"],
        )
        assert outcome["exclusion_reason"] == "not_prospective_native"
        pairs, _ = self._pairs()
        for pair in pairs:
            pair["issue_kind"] = kind
        assert self._qualify(pairs)["qualified"] is False

    def test_disjoint_evidence_and_clustered_bootstrap_gate(self):
        pairs, _ = self._pairs()
        result = self._qualify([*pairs, *pairs[:10]])
        assert result["qualified"] is True
        assert result["control_ready"] is False
        assert result["ordinary_writes_allowed"] is False
        assert result["counts"]["pairs"] == 280
        assert result["counts"]["device_day_clusters"] == 14
        assert result["counts"]["issue_cohorts"] == 56
        bounds = result["evidence"]["overall"]["baseline"]
        assert bounds["resamples"] == 10_000
        assert bounds["moving_block_hours"] == 48
        assert bounds["lb95"] > 0 and bounds["sequential_lower_bound"] > 0
        overlap = [
            dict(pair, issued_at=(self.BASE - dt.timedelta(days=1)).isoformat()) for pair in pairs
        ]
        rejected = self._qualify(overlap)
        assert rejected["qualified"] is False
        assert rejected["exclusions"]["fit_or_holdout_overlap"] == 280
        assert rejected["exclusions_by_horizon"]["60"]["fit_or_holdout_overlap"] == 56
        assert rejected["exclusions_by_outdoor_bin"]["8"]["fit_or_holdout_overlap"] > 0

    @pytest.mark.parametrize("case", ["sparse", "clusters", "bins", "non_finite", "flat"])
    def test_sparse_invalid_or_flat_evidence_cannot_qualify(self, case):
        pairs, _ = self._pairs()
        if case == "sparse":
            pairs = pairs[:30]
        elif case == "clusters":
            pairs = [
                pair
                for pair in pairs
                if pair["issued_at"] < (self.BASE + dt.timedelta(days=13)).isoformat()
            ]
        elif case == "bins":
            pairs = [dict(pair, outdoor_bin=8) for pair in pairs]
        elif case == "non_finite":
            pairs = [dict(pair, candidate=float("nan")) for pair in pairs]
        else:
            pairs = [dict(pair, observed=21.0) for pair in pairs]
        assert self._qualify(pairs)["qualified"] is False

    def test_expiry_uses_outcomes_not_fit_time(self):
        result = self._qualify()
        assert result["qualified"] is True
        latest = dt.datetime.fromisoformat(result["last_valid_outcome_at"])
        assert dt.datetime.fromisoformat(result["expires_at"]) == latest + dt.timedelta(hours=72)
        expired = self._qualify(now=self.BASE + dt.timedelta(days=18))
        assert expired["qualified"] is False
        assert expired["reason"] == "outcomes_expired"
        pairs, _ = self._pairs()
        for pair in pairs:
            pair["fit_at"] = (self.BASE + dt.timedelta(days=18)).isoformat()
        assert (
            self._qualify(pairs, now=self.BASE + dt.timedelta(days=18))["reason"]
            == "outcomes_expired"
        )

    def test_incomplete_coverage_blocks_qualification(self):
        assert self._qualify(coverage_complete=False)["reason"] == "incomplete_coverage"
        pairs, _ = self._pairs()
        pairs[0]["status"] = "incomplete"
        assert self._qualify(pairs)["reason"] == "incomplete_coverage"

    def test_sequential_testing_policy_consumes_only_predeclared_looks(self):
        from packages.ml.forecast_quality import DECISION_OFFSETS_HOURS, decision_checkpoint

        assert len(DECISION_OFFSETS_HOURS) == 71
        assert decision_checkpoint(self.BASE, self.BASE + dt.timedelta(days=14)) is None
        assert decision_checkpoint(self.BASE, self.BASE + dt.timedelta(days=15)) == 0
        assert decision_checkpoint(self.BASE, self.BASE + dt.timedelta(days=15), (0,)) is None
        repeated = self._qualify(completed=(0,))
        assert repeated["reason"] == "decision_not_due"
        result = self._qualify()
        alpha = result["evidence"]["overall"]["baseline"]["alpha"]
        assert alpha * 71 * len(result["evidence"]) * 2 == pytest.approx(0.05)

    async def test_bootstrap_runtime_benchmark(self):
        import asyncio
        import time

        started = time.perf_counter()
        result = await asyncio.to_thread(self._qualify)
        elapsed = time.perf_counter() - started
        assert result["qualified"] is True
        assert result["evidence"]["overall"]["baseline"]["resamples"] == 10_000
        print(
            f"BOOTSTRAP_BENCHMARK pairs=280 looks=1 resamples=10000 runtime_seconds={elapsed:.6f}"
        )

    async def test_high_water_all_pages_dedup_late_targets_and_retry(self):
        from sqlalchemy.dialects import postgresql
        from packages.ml.forecast_quality import complete_plan_window, reconcile_native_issue

        rows = [SimpleNamespace(id=index, created_at=self.BASE) for index in range(250, 0, -1)]
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(scalars=lambda page=page: SimpleNamespace(all=lambda: page))
                    for page in (rows[:100], rows[100:200], rows[200:])
                ]
            )
        )
        collected, complete, high_water = await complete_plan_window(
            session,
            since=self.BASE,
            until=self.BASE + dt.timedelta(days=1),
            page_size=100,
        )
        assert complete is True and high_water == 250
        assert [row.id for row in collected] == list(range(250, 0, -1))
        assert len({row.id for row in collected}) == 250
        statements = [
            call.args[0].compile(dialect=postgresql.dialect())
            for call in session.execute.call_args_list
        ]
        assert all(
            "created_at <=" in str(statement) and "LIMIT" in str(statement)
            for statement in statements
        )
        assert statements[1].params["id_1"] == 151
        assert statements[1].params["id_2"] == 250
        assert statements[2].params["id_1"] == 51
        issue = self._issue()
        pending = reconcile_native_issue(
            issue, self._trace(issue), observed=None, observed_at=None, now=self.BASE
        )
        assert pending["status"] == "pending"
        missing = reconcile_native_issue(
            issue, self._trace(issue), observed=None, observed_at=None, now=issue["target_at"]
        )
        assert missing["status"] == "incomplete"
        late = reconcile_native_issue(
            issue,
            self._trace(issue),
            observed=21.0,
            observed_at=issue["target_at"],
            now=issue["target_at"] + dt.timedelta(hours=6),
        )
        retry = reconcile_native_issue(
            issue,
            self._trace(issue),
            observed=21.0,
            observed_at=issue["target_at"],
            now=issue["target_at"] + dt.timedelta(hours=6),
        )
        assert late == retry and late["status"] == "valid"
        pairs, _ = self._pairs()
        assert self._qualify([*pairs, *pairs])["counts"]["pairs"] == len(pairs)

    def test_demand_qualification_uses_kw_relative_threshold_not_degrees(self):
        pairs, _ = self._pairs()
        for pair in pairs:
            for key in ("observed", "candidate", "incumbent", "baseline"):
                pair[key] *= 0.05
        result = self._qualify(pairs, component="demand")
        assert result["qualified"] is True
        assert result["unit"] == "kW"
        assert result["evidence"]["overall"]["baseline"]["mean_improvement"] < 0.10
        assert result["evidence"]["overall"]["baseline"]["relative_mae_improvement"] >= 0.05

    def test_sensor_resolution_does_not_relax_policy_changes(self):
        from packages.ml.forecast_quality import native_trace_exclusion

        issue = self._issue()
        trace = self._trace(issue)
        trace["statuses"][-1]["zone1_temp"] += 1.0
        assert native_trace_exclusion(issue, trace) is None
        trace["statuses"][-1]["zone1_temp"] += 0.01
        assert native_trace_exclusion(issue, trace)[1] == "native_trajectory_mismatch"
        trace = self._trace(issue)
        trace["statuses"][-1]["native_policy_provenance"]["settings"] = {
            **issue["input_snapshot"]["native_settings"],
            "zone1_target_temp": 36.0,
        }
        assert native_trace_exclusion(issue, trace)[1] == "native_policy_changed"

    def test_tiny_allocated_tail_never_hides_an_adverse_supported_block(self):
        from packages.ml.forecast_quality import paired_block_bootstrap

        pairs, _ = self._pairs()
        for pair in pairs:
            if pair["issued_at"][:10] in {
                self.BASE.date().isoformat(),
                (self.BASE + dt.timedelta(days=1)).date().isoformat(),
            }:
                pair["candidate"] = pair["observed"] + 3.0
        result = paired_block_bootstrap(pairs, "baseline", seed=17, alpha=0.000001)
        assert result["resamples"] == 10_000
        assert result["finite_resample_support_bound"] < 0
        assert result["sequential_lower_bound"] <= result["finite_resample_support_bound"]


def test_dispatch_drift_margin_covers_both_verification_windows():
    assert DISPATCH_DRIFT_MARGIN == dt.timedelta(seconds=180)


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


@pytest.mark.parametrize("cache_state", ["unknown", "expired", "fresh"])
async def test_R11_native_materialization_never_refreshes_remote_timer(monkeypatch, cache_state):
    from packages.core.services import aquarea
    from packages.ml import forecast_quality

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    wrapper = aquarea.AquareaWrapper()
    timer = SimpleNamespace(enabled=False, slots=(), active_slots=lambda *_args: ())
    remote = AsyncMock(return_value=timer)
    wrapper.get_device = AsyncMock(return_value=SimpleNamespace(get_weekly_timer=remote))
    wrapper._distributed_quota_enabled = Mock(return_value=False)
    wrapper._read_limiter.acquire = AsyncMock()
    wrapper._reserve_background_read = AsyncMock()
    if cache_state != "unknown":
        wrapper._weekly_timer = timer
        wrapper._weekly_timer_fetched_at = aquarea.time.monotonic() - (
            4000 if cache_state == "expired" else 0
        )
        wrapper._weekly_timer_fetched_wall_at = now
    monkeypatch.setattr(
        "packages.core.settings_service.get_all_settings", AsyncMock(return_value={})
    )
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))
    session = SimpleNamespace(execute=AsyncMock(return_value=empty))
    monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))

    result = await forecast_quality.materialize_native_inputs(
        "demand", now=now, timer_reader=wrapper
    )

    assert result is None
    remote.assert_not_awaited()
    wrapper.get_device.assert_not_awaited()
    wrapper._read_limiter.acquire.assert_not_awaited()
    wrapper._reserve_background_read.assert_not_awaited()
    assert wrapper.cached_native_timer_provenance()["status"] == (
        "known" if cache_state == "fresh" else "unknown"
    )


async def test_R19_fallback_only_history_has_descriptive_not_learned_scores(monkeypatch):
    from packages.core import safety_reverts
    from packages.ml import forecast_quality

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    target = now - dt.timedelta(hours=1)
    plan = _baseline_scorecard_plan(101, "audit-device", target)
    payload = json.loads(plan.plan_json)
    payload["forecast_snapshot"]["forecast_status"] = "fallback"
    plan.plan_json = json.dumps(payload)
    session = _scorecard_session(
        [plan], [SimpleNamespace(device_id="room", timestamp=target, temperature=20.0)], set()
    )
    monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))
    monkeypatch.setattr(
        safety_reverts, "unresolved_revert_device_ids", AsyncMock(return_value=(set(), False))
    )
    monkeypatch.setattr(
        safety_reverts, "historical_revert_overlap_rows", AsyncMock(return_value=([], False))
    )

    scorecard = await forecast_quality.get_forecast_scorecard(
        now=now, evaluation_context={"live_baseline_applied": False}
    )

    assert scorecard["fallback"]["overall"]["samples"] == 1
    assert scorecard["fallback"]["overall"]["mae"] == pytest.approx(0.2)
    assert scorecard["all_forecasts"]["overall"]["samples"] == 1
    assert scorecard["overall"]["samples"] == 0
    assert scorecard["quality_gate"]["control_allowed"] is False
    assert scorecard["prospective_qualification_eligible"] is False
    assert scorecard["forecast_kind"] == "counterfactual"
    json.dumps(scorecard, allow_nan=False)


@pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
@pytest.mark.parametrize(
    "first_hours,valid_first",
    [
        pytest.param((True,), False, id="boolean-true"),
        pytest.param((False,), False, id="boolean-false"),
        pytest.param(("1",), False, id="string-hour"),
        pytest.param((1.0,), False, id="float-hour"),
        pytest.param((True, 1), True, id="boolean-before-integer"),
        pytest.param((1, True), True, id="integer-before-boolean"),
        pytest.param((False, "1", 1.0, True, 1), True, id="invalid-parallel-rows"),
        pytest.param((1,), True, id="canonical-integers"),
    ],
)
async def test_R03_saved_history_requires_canonical_integer_hours(
    monkeypatch, reader, first_hours, valid_first
):
    from packages.core import safety_reverts
    from packages.core.models import IndoorTempReading, PlanRecord
    from packages.ml import forecast_quality

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    plans, readings = [], []
    for plan_index in range(12):
        issue = now - dt.timedelta(hours=36 + 25 * plan_index)
        canonical = []
        for lead in (1, 3, 6, 12, 24):
            target = issue + dt.timedelta(hours=lead)
            observed = 20.0 + 0.2 * plan_index + 0.01 * lead
            canonical.append(
                {
                    "hour": lead,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": observed + 0.1,
                    "model_source": "comfort_model_controlled",
                }
            )
            readings.append(
                IndoorTempReading(
                    timestamp=target, device_id="room", temperature=observed, is_stale=False
                )
            )
        tested = [{**canonical[0], "hour": hour} for hour in first_hours] + canonical[1:]
        snapshot = json.loads(
            _baseline_scorecard_plan(plan_index + 1, "audit-device", issue).plan_json
        )["forecast_snapshot"]
        snapshot["issue_timestamp"] = issue.isoformat()
        snapshot["observed_history"] = [{"hour": 0, "ts": issue.isoformat(), "temperature": 18.0}]
        snapshot["forecast_with_plan"] = tested
        if reader == "shadow":
            snapshot["forecast_with_plan"] = [
                {**point, "model_source": "rule_thermal_fallback"} for point in canonical
            ]
            snapshot["shadow_forecast_with_plan"] = tested
        elif reader == "fallback":
            snapshot["forecast_status"] = "fallback"
            snapshot["forecast_with_plan"] = [
                {**point, "model_source": "rule_thermal_fallback"} for point in tested
            ]
        snapshot["weather_forecast"] = [{} for _ in snapshot["forecast_with_plan"]]
        plans.append(
            PlanRecord(
                id=plan_index + 1,
                created_at=issue,
                horizon_start=issue,
                horizon_end=issue + dt.timedelta(hours=24),
                plan_json=json.dumps(
                    {"device_id": "audit-device", "actions": [], "forecast_snapshot": snapshot}
                ),
            )
        )
    session = _scorecard_session(plans, sorted(readings, key=lambda row: row.timestamp), set())
    monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))
    monkeypatch.setattr(
        safety_reverts, "unresolved_revert_device_ids", AsyncMock(return_value=(set(), False))
    )
    monkeypatch.setattr(
        safety_reverts, "historical_revert_overlap_rows", AsyncMock(return_value=([], False))
    )

    scorecard = await forecast_quality.get_forecast_scorecard(
        now=now, evaluation_context={"live_baseline_applied": False}
    )

    scored = scorecard["fallback"] if reader == "fallback" else scorecard
    assert scored["overall"]["samples"] == (60 if valid_first else 48)
    assert scored["overall"]["mae"] == pytest.approx(0.1)
    assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
        1: 12 if valid_first else 0,
        3: 12,
        6: 12,
        12: 12,
        24: 12,
    }
    if reader == "fallback":
        assert scorecard["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
    else:
        assert scorecard["quality_gate"]["control_allowed"] is valid_first
    if reader != "shadow":
        assert scorecard["all_forecasts"]["overall"]["samples"] == scored["overall"]["samples"]
        assert (
            sum(source["overall"]["samples"] for source in scorecard["sources"].values())
            == (scored["overall"]["samples"])
        )
    assert scorecard["forecast_kind"] == "counterfactual"
    assert scorecard["prospective_qualification_eligible"] is False
    serialized = json.loads(json.dumps(scorecard, allow_nan=False))
    assert set(serialized["horizon_quality"]) == {"1", "3", "6", "12", "24"}


@pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
async def test_R03_saved_history_duplicate_outcomes_cannot_inflate_quorum(monkeypatch, reader):
    from packages.core import safety_reverts
    from packages.core.models import IndoorTempReading, PlanRecord
    from packages.ml import forecast_quality

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    plans, readings = [], []
    for plan_index in range(6):
        issue = now - dt.timedelta(hours=36 + 25 * plan_index)
        canonical = []
        for lead in (1, 3, 6, 12, 24):
            target = issue + dt.timedelta(hours=lead)
            observed = 20.0 + 0.2 * plan_index + 0.01 * lead
            canonical.append(
                {
                    "hour": lead,
                    "ts": target.isoformat(),
                    "predicted_indoor_temp": observed + 0.1,
                    "model_source": "comfort_model_controlled",
                }
            )
            readings.append(
                IndoorTempReading(
                    timestamp=target, device_id="room", temperature=observed, is_stale=False
                )
            )
        snapshot = json.loads(
            _baseline_scorecard_plan(plan_index + 1, "audit-device", issue).plan_json
        )["forecast_snapshot"]
        snapshot["issue_timestamp"] = issue.isoformat()
        snapshot["observed_history"] = [{"hour": 0, "ts": issue.isoformat(), "temperature": 18.0}]
        snapshot["forecast_with_plan"] = canonical * 2
        if reader == "shadow":
            snapshot["forecast_with_plan"] = [
                {**point, "model_source": "rule_thermal_fallback"} for point in canonical
            ]
            snapshot["shadow_forecast_with_plan"] = canonical * 2
        elif reader == "fallback":
            snapshot["forecast_status"] = "fallback"
            snapshot["forecast_with_plan"] = [
                {**point, "model_source": "rule_thermal_fallback"} for point in canonical * 2
            ]
        snapshot["weather_forecast"] = [{} for _point in snapshot["forecast_with_plan"]]
        plans.append(
            PlanRecord(
                id=plan_index + 1,
                created_at=issue,
                horizon_start=issue,
                horizon_end=issue + dt.timedelta(hours=24),
                plan_json=json.dumps(
                    {"device_id": "audit-device", "actions": [], "forecast_snapshot": snapshot}
                ),
            )
        )
    session = _scorecard_session(plans, sorted(readings, key=lambda row: row.timestamp), set())
    monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))
    monkeypatch.setattr(
        safety_reverts, "unresolved_revert_device_ids", AsyncMock(return_value=(set(), False))
    )
    monkeypatch.setattr(
        safety_reverts, "historical_revert_overlap_rows", AsyncMock(return_value=([], False))
    )

    scorecard = await forecast_quality.get_forecast_scorecard(
        now=now, evaluation_context={"live_baseline_applied": False}
    )

    scored = scorecard["fallback"] if reader == "fallback" else scorecard
    assert scored["overall"]["samples"] <= 30
    assert all(row["samples"] <= 6 for row in scored["horizons"])
    assert scorecard["quality_gate"]["control_allowed"] is False
    assert scorecard["prospective_qualification_eligible"] is False
    json.dumps(scorecard, allow_nan=False)


class TestR03SavedEvidenceDeduplication:
    @staticmethod
    def history(reader, plan_count=6):
        now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
        plans, readings = [], []
        for index in range(plan_count):
            issue = now - dt.timedelta(hours=36 + 25 * index)
            plan = _baseline_scorecard_plan(
                index + 1, "audit-device", issue + dt.timedelta(hours=1)
            )
            payload = json.loads(plan.plan_json)
            snapshot = payload["forecast_snapshot"]
            points = []
            for lead in (1, 3, 6, 12, 24):
                target = issue + dt.timedelta(hours=lead)
                observed = 20.0 + 0.2 * index + 0.01 * lead
                points.append(
                    {
                        "hour": lead,
                        "ts": target.isoformat(),
                        "predicted_indoor_temp": observed + 0.1,
                        "model_source": "comfort_model_controlled",
                    }
                )
                readings.append(
                    SimpleNamespace(device_id="room", timestamp=target, temperature=observed)
                )
            snapshot["observed_history"][0]["temperature"] = 18.0
            snapshot["forecast_with_plan"] = points
            if reader in {"shadow", "fallback"}:
                snapshot["forecast_with_plan"] = [
                    {**point, "model_source": "rule_thermal_fallback"} for point in points
                ]
            if reader == "shadow":
                snapshot["shadow_forecast_with_plan"] = points
            elif reader == "fallback":
                snapshot["forecast_status"] = "fallback"
            snapshot["weather_forecast"] = [{} for _ in points]
            plan.horizon_end = issue + dt.timedelta(hours=24)
            plan.plan_json = json.dumps(payload)
            plans.append(plan)
        return now, plans, readings

    @staticmethod
    async def score(monkeypatch, now, plans, readings, pages=None):
        from packages.core import safety_reverts
        from packages.ml import forecast_quality

        session = _scorecard_session(plans, readings, set())
        if pages is not None:
            session.execute.side_effect = [
                SimpleNamespace(scalars=lambda rows=rows: SimpleNamespace(all=lambda: rows))
                for rows in pages
            ] + [SimpleNamespace(all=lambda: readings), SimpleNamespace(all=list)]
        monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))
        monkeypatch.setattr(
            safety_reverts, "unresolved_revert_device_ids", AsyncMock(return_value=(set(), False))
        )
        monkeypatch.setattr(
            safety_reverts, "historical_revert_overlap_rows", AsyncMock(return_value=([], False))
        )
        return await forecast_quality.get_forecast_scorecard(
            now=now,
            max_plans=4 if pages is not None else 60,
            evaluation_context={"live_baseline_applied": False},
        )

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_clone_ids_do_not_create_independent_forecasts(self, monkeypatch, reader):
        now, plans, readings = self.history(reader)
        plans += [SimpleNamespace(**{**vars(plan), "id": plan.id + 100}) for plan in plans]

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 6
        assert scored["overall"]["samples"] == 30
        assert all(row["samples"] == 6 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_missing_device_provenance_cannot_authorize(self, monkeypatch, reader):
        now, plans, readings = self.history(reader, plan_count=12)
        for plan in plans:
            payload = json.loads(plan.plan_json)
            payload.pop("device_id")
            plan.plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 0
        assert scored["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["missing_device_provenance"] == 12
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_malformed_sensor_copy_invalidates_origin(self, monkeypatch, reader):
        now, plans, readings = self.history(reader, plan_count=12)
        for plan in list(plans):
            payload = json.loads(plan.plan_json)
            payload["forecast_snapshot"].pop("control_input")
            plans.append(SimpleNamespace(**{**vars(plan), "plan_json": json.dumps(payload)}))

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 0
        assert scored["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["missing_sensor_provenance"] == 12
        assert scorecard["exclusions"]["ambiguous_forecast_provenance"] == 12
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("replay", ["pages", "utc-offsets"])
    async def test_clone_ids_across_pages_and_offsets(self, monkeypatch, reader, replay):
        now, plans, readings = self.history(reader)
        copies = []
        for plan in plans:
            payload = json.loads(plan.plan_json)
            if replay == "utc-offsets":
                snapshot = payload["forecast_snapshot"]
                offset = dt.timezone(dt.timedelta(hours=-4))
                for row in [snapshot, *snapshot["observed_history"]]:
                    key = "issue_timestamp" if row is snapshot else "ts"
                    row[key] = dt.datetime.fromisoformat(row[key]).astimezone(offset).isoformat()
                for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
                    for point in snapshot.get(name, []):
                        point["ts"] = (
                            dt.datetime.fromisoformat(point["ts"]).astimezone(offset).isoformat()
                        )
            copies.append(
                SimpleNamespace(
                    **{**vars(plan), "id": plan.id + 100, "plan_json": json.dumps(payload)}
                )
            )
        replayed = plans + copies
        pages = [replayed[8:], replayed[4:8], replayed[:4], []] if replay == "pages" else None

        scorecard = await self.score(monkeypatch, now, replayed, readings, pages)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 6
        assert scored["overall"]["samples"] == 30
        assert all(row["samples"] == 6 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["evidence_coverage_complete"] is True

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("device_id", [None, "", "  ", True, 42, 1.0, [], {}])
    async def test_invalid_device_provenance_is_not_coerced(self, monkeypatch, reader, device_id):
        now, plans, readings = self.history(reader, plan_count=12)
        for plan in plans:
            payload = json.loads(plan.plan_json)
            payload["device_id"] = device_id
            plan.plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["missing_device_provenance"] == 12

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize(
        "control_input,sensor_basis",
        [
            (None, None),
            ({"available": True}, None),
            ({"available": "true", "reference_sensor_id": "room"}, None),
            ({"available": True, "reference_sensor_id": True}, None),
            ({"available": True, "sensor_ids": ["room", None]}, None),
            ({"available": True, "sensor_ids": ["room", {}]}, None),
            ({"available": True, "sensor_ids": ["room", ""]}, None),
            ({"available": True, "sensor_ids": "room"}, None),
            ({"available": True, "reference_sensor_id": "", "sensor_ids": []}, None),
            ({"available": True, "reference_sensor_id": "room", "sensor_ids": ["office"]}, None),
            ({"available": True, "reference_sensor_id": "room"}, {}),
            ({"available": True, "reference_sensor_id": "room"}, {"kind": "median"}),
        ],
    )
    async def test_incomplete_sensor_provenance_cannot_authorize(
        self, monkeypatch, reader, control_input, sensor_basis
    ):
        now, plans, readings = self.history(reader, plan_count=12)
        for plan in plans:
            payload = json.loads(plan.plan_json)
            snapshot = payload["forecast_snapshot"]
            snapshot["control_input"] = control_input
            if sensor_basis is not None:
                snapshot["sensor_basis"] = sensor_basis
            plan.plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["missing_sensor_provenance"] == 12

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("different_id", [False, True])
    @pytest.mark.parametrize("ordering", ["bad-first", "bad-last", "third-copy"])
    @pytest.mark.parametrize("missing", ["device", "sensor", "issue"])
    async def test_malformed_origin_peers_never_restore_evidence(
        self, monkeypatch, reader, different_id, ordering, missing
    ):
        now, plans, readings = self.history(reader)
        payload = json.loads(plans[0].plan_json)
        if missing == "device":
            payload["device_id"] = ""
        elif missing == "sensor":
            payload["forecast_snapshot"]["control_input"]["sensor_ids"] = ["room", None]
        else:
            payload["forecast_snapshot"].pop("issue_timestamp")
        bad = SimpleNamespace(
            **{
                **vars(plans[0]),
                "id": 101 if different_id else plans[0].id,
                "plan_json": json.dumps(payload),
            }
        )
        plans.insert(0 if ordering != "bad-last" else len(plans), bad)
        if ordering == "third-copy":
            plans += [SimpleNamespace(**vars(plans[1])), SimpleNamespace(**vars(bad))]

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 5
        assert scored["overall"]["samples"] == 25
        assert all(row["samples"] == 5 for row in scored["horizons"])
        assert scorecard["exclusions"]["ambiguous_forecast_provenance"] == 1
        assert scorecard["quality_gate"]["control_allowed"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("malformed", ["prediction", "source", "target"])
    async def test_different_id_malformed_point_invalidates_only_its_peer(
        self, monkeypatch, reader, bad_first, malformed
    ):
        now, plans, readings = self.history(reader)
        payload = json.loads(plans[0].plan_json)
        name = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        point = payload["forecast_snapshot"][name][0]
        if malformed == "prediction":
            point["predicted_indoor_temp"] += 2.0
        elif malformed == "source":
            point.pop("model_source")
        else:
            point["ts"] = dt.datetime.fromisoformat(point["ts"]).replace(tzinfo=None).isoformat()
        bad = SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        plans.insert(0 if bad_first else len(plans), bad)
        plans.append(SimpleNamespace(**{**vars(plans[1 if bad_first else 0]), "id": 201}))

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 29
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            1: 5,
            3: 6,
            6: 6,
            12: 6,
            24: 6,
        }
        assert scorecard["quality_gate"]["control_allowed"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_unrelated_invalid_cohort_does_not_block_valid_origins(self, monkeypatch, reader):
        now, plans, readings = self.history(reader, plan_count=12)
        payload = json.loads(plans[0].plan_json)
        payload["device_id"] = "unrelated-device"
        payload["forecast_snapshot"].pop("control_input")
        plans.append(
            SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 12
        assert scored["overall"]["samples"] == 60
        assert all(row["samples"] == 12 for row in scored["horizons"])
        assert scored["quality_gate"]["control_allowed"] is True
        assert scorecard["quality_gate"]["control_allowed"] is (reader != "fallback")
        assert not scorecard["exclusions"].get("ambiguous_forecast_provenance")

    @pytest.mark.parametrize("conflict", [False, True])
    async def test_baseline_comparison_counts_unique_origins(self, monkeypatch, conflict):
        now, plans, readings = self.history("primary")
        original = await self.score(monkeypatch, now, plans, readings)
        copies = [SimpleNamespace(**{**vars(plan), "id": plan.id + 100}) for plan in plans]
        if conflict:
            payload = json.loads(copies[0].plan_json)
            payload["forecast_snapshot"]["forecast_with_plan_baseline"][0][
                "predicted_indoor_temp"
            ] += 2.0
            copies[0].plan_json = json.dumps(payload)

        replayed = await self.score(monkeypatch, now, plans + copies, readings)

        assert original["baseline_comparison"]["plans_scored"] == 6
        assert original["baseline_comparison"]["pairs_scored"] == 6
        assert replayed["baseline_comparison"]["plans_scored"] == (5 if conflict else 6)
        assert replayed["baseline_comparison"]["pairs_scored"] == (5 if conflict else 6)
        assert replayed["overall"]["samples"] == 30
        assert replayed["quality_gate"]["control_allowed"] is False

    @pytest.mark.parametrize("learned", [False, True])
    async def test_actual_rules_serializer_origins_score_without_invented_fields(
        self, monkeypatch, learned
    ):
        from packages.core.control_temperature import ControlTemperature, SensorTemperature
        from packages.core.heat_curve import HeatCurveConfig
        from packages.optimizer import rules_engine

        now, plans, readings = self.history("primary" if learned else "fallback", plan_count=12)
        predictor = Mock()
        monkeypatch.setattr(
            rules_engine.thermal_model, "predict_indoor_controlled_curve", predictor
        )
        monkeypatch.setattr(
            rules_engine.thermal_model, "predict_indoor_candidate_curve", Mock(return_value=None)
        )
        for index, plan in enumerate(plans):
            issue = plan.created_at
            predictor.return_value = [
                {
                    "hour": lead,
                    "predicted_indoor_temp": 20.0 + 0.2 * index + 0.01 * lead + 0.1,
                    "source": "comfort_model_controlled" if learned else "linear_controlled",
                }
                for lead in range(1, 25)
            ]
            control = ControlTemperature(
                value=18.0,
                confidence="high",
                sensor_count=1,
                sample_count=1,
                latest_reading=issue,
                reference_sensor_id="room",
                sensors=(SensorTemperature("room", "Room", "Room", 18.0, issue),),
            )
            prices = [(issue + dt.timedelta(hours=hour), 0.1) for hour in range(24)]
            snapshot = rules_engine.RulesOptimizer._build_forecast_snapshot(
                prices=prices,
                weather=[(timestamp, 5.0) for timestamp, _ in prices],
                weather_full=[],
                actions=[],
                horizon_start=issue,
                current_indoor=18.0,
                current_water_temp=35.0,
                heat_curve=HeatCurveConfig(),
                comfort_schedule={},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
                quality_gate={"control_allowed": learned},
                control_temperature=control,
                control_input={
                    "available": control.is_usable,
                    "reference_sensor_id": control.reference_sensor_id,
                    "sensor_ids": [sensor.device_id for sensor in control.sensors],
                },
            )
            assert snapshot["issue_timestamp"] == issue.isoformat()
            assert snapshot["observed_history"][0]["ts"] == issue.isoformat()
            assert snapshot["sensor_basis"]["kind"] == "reference"
            assert snapshot["forecast_status"] == ("available" if learned else "fallback")
            assert "generation_id" not in snapshot
            plan.plan_json = json.dumps(
                {"device_id": "audit-device", "actions": [], "forecast_snapshot": snapshot},
                allow_nan=False,
            )

        scorecard = await self.score(monkeypatch, now, plans, readings)
        clones = [SimpleNamespace(**{**vars(plan), "id": plan.id + 100}) for plan in plans]
        replayed = await self.score(monkeypatch, now, plans + clones, readings)

        for result in (scorecard, replayed):
            scored = result if learned else result["fallback"]
            assert scored["plans_scored"] == 12
            assert scored["overall"]["samples"] == 60
            assert scored["overall"]["mae"] == pytest.approx(0.1)
            assert all(row["samples"] == 12 for row in scored["horizons"])
            assert result["quality_gate"]["control_allowed"] is learned
            assert result["prospective_qualification_eligible"] is False
            json.dumps(result, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("related", [False, True])
    async def test_missing_origin_uses_only_identifiable_target_cohort(
        self, monkeypatch, reader, related
    ):
        now, plans, readings = self.history(reader, plan_count=12)
        payload = json.loads(plans[0].plan_json)
        snapshot = payload["forecast_snapshot"]
        snapshot.pop("issue_timestamp")
        snapshot.pop("observed_history")
        if not related:
            for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
                for point in snapshot.get(name, []):
                    point["ts"] = (
                        dt.datetime.fromisoformat(point["ts"]) - dt.timedelta(days=1)
                    ).isoformat()
        plans.append(
            SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == (11 if related else 12)
        assert scored["overall"]["samples"] == (55 if related else 60)
        assert all(row["samples"] == (11 if related else 12) for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is (
            not related and reader != "fallback"
        )
        assert scorecard["exclusions"]["missing_persistence_origin"] == 1
        assert scorecard["exclusions"]["unidentifiable_forecast_provenance"] == 1
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    async def test_one_issued_horizon_cannot_have_conflicting_targets(
        self, monkeypatch, reader, bad_first
    ):
        now, plans, readings = self.history(reader)
        copies = []
        for plan in plans:
            payload = json.loads(plan.plan_json)
            name = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
            for point in payload["forecast_snapshot"][name]:
                point["ts"] = (
                    dt.datetime.fromisoformat(point["ts"]) + dt.timedelta(minutes=1)
                ).isoformat()
            copies.append(
                SimpleNamespace(
                    **{**vars(plan), "id": plan.id + 100, "plan_json": json.dumps(payload)}
                )
            )
        replayed = copies + plans if bad_first else plans + copies
        replayed += [SimpleNamespace(**{**vars(plan), "id": plan.id + 200}) for plan in plans]

        scorecard = await self.score(monkeypatch, now, replayed, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 0
        assert scored["overall"]["samples"] == 0
        assert all(row["samples"] == 0 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["conflicting_forecast_target"] == 30

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("field", ["schema", "issue", "origin", "origin-hour", "origin-value"])
    async def test_missing_required_origin_never_earns_quorum(self, monkeypatch, reader, field):
        now, plans, readings = self.history(reader, plan_count=12)
        for plan in plans:
            payload = json.loads(plan.plan_json)
            snapshot = payload["forecast_snapshot"]
            if field == "schema":
                snapshot.pop("scoring_schema")
            elif field == "issue":
                snapshot["issue_timestamp"] = "not-an-issue"
            elif field == "origin":
                snapshot["observed_history"][0]["ts"] = None
            elif field == "origin-hour":
                snapshot["observed_history"][0]["hour"] = False
            else:
                snapshot["observed_history"][0]["temperature"] = float("nan")
            plan.plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        reason = "legacy_scoring_schema" if field == "schema" else "missing_persistence_origin"
        assert scorecard["exclusions"][reason] == 12
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("malformed_target", [False, True])
    async def test_source_labels_do_not_split_immutable_origin(
        self, monkeypatch, reader, malformed_target
    ):
        now, plans, readings = self.history(reader, plan_count=1)
        payload = json.loads(plans[0].plan_json)
        name = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        for point in payload["forecast_snapshot"][name]:
            point["model_source"] = (
                "linear_controlled"
                if reader == "fallback"
                else "comfort_model_physics_continuation"
            )
        plans.append(
            SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        )
        if malformed_target:
            bad_payload = json.loads(plans[0].plan_json)
            bad_payload["forecast_snapshot"][name][0]["ts"] = None
            plans.append(
                SimpleNamespace(
                    **{**vars(plans[0]), "id": 201, "plan_json": json.dumps(bad_payload)}
                )
            )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == (4 if malformed_target else 5)
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            1: 0 if malformed_target else 1,
            3: 1,
            6: 1,
            12: 1,
            24: 1,
        }
        assert scorecard["quality_gate"]["control_allowed"] is False

    @pytest.mark.parametrize("reader", ["shadow", "fallback"])
    async def test_equal_values_at_real_distinct_origins_remain_independent(
        self, monkeypatch, reader
    ):
        now, plans, readings = self.history(reader, plan_count=2)
        name = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        first = json.loads(plans[0].plan_json)["forecast_snapshot"][name]
        payload = json.loads(plans[1].plan_json)
        for index, point in enumerate(payload["forecast_snapshot"][name]):
            point["predicted_indoor_temp"] = first[index]["predicted_indoor_temp"]
        plans[1].plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 2
        assert scored["overall"]["samples"] == 10
        assert all(row["samples"] == 2 for row in scored["horizons"])

    async def test_actual_milp_serializer_does_not_invent_learned_provenance(self, monkeypatch):
        from packages.optimizer.milp import MILPOptimizer

        now, plans, readings = self.history("primary", plan_count=1)
        issue = plans[0].created_at
        produced = MILPOptimizer()._solve(
            [(issue, 0.1)],
            [(issue, 5.0)],
            cop_fn=lambda temperature, hour=12: 3.0,
            demand_per_hour=[4.0],
            current_tank_temp=48.0,
        )
        assert produced is not None
        snapshot = produced["forecast_snapshot"]
        assert snapshot["forecast_with_plan"][0]["source"] == "milp_solution"
        assert "model_source" not in snapshot["forecast_with_plan"][0]
        plans[0].plan_json = json.dumps(
            {"device_id": "audit-device", "actions": [], "forecast_snapshot": snapshot},
            allow_nan=False,
        )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        assert scorecard["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["exclusions"]["legacy_scoring_schema"] == 1
        assert scorecard["prospective_qualification_eligible"] is False

    @classmethod
    def serialized_history(cls, monkeypatch, reader, plan_count=12):
        from packages.core.control_temperature import ControlTemperature, SensorTemperature

        now, plans, readings = cls.history(reader, plan_count=plan_count)
        controlled = Mock()
        candidate = Mock(return_value=None)
        monkeypatch.setattr(
            rules_engine.thermal_model, "predict_indoor_controlled_curve", controlled
        )
        monkeypatch.setattr(rules_engine.thermal_model, "predict_indoor_candidate_curve", candidate)
        for index, plan in enumerate(plans):
            issue = plan.created_at
            learned = [
                {
                    "hour": lead,
                    "predicted_indoor_temp": 20.0 + 0.2 * index + 0.01 * lead + 0.1,
                    "source": "comfort_model_controlled",
                    "segment_kind": "direct",
                }
                for lead in range(1, 25)
            ]
            controlled.return_value = (
                learned
                if reader == "primary"
                else [{**point, "source": "linear_controlled"} for point in learned]
            )
            candidate.return_value = learned if reader == "shadow" else None
            control = ControlTemperature(
                value=18.0,
                confidence="high",
                sensor_count=1,
                sample_count=1,
                latest_reading=issue,
                reference_sensor_id="room",
                sensors=(SensorTemperature("room", "Room", "Room", 18.0, issue),),
            )
            prices = [(issue + dt.timedelta(hours=hour), 0.1) for hour in range(24)]
            snapshot = rules_engine.RulesOptimizer._build_forecast_snapshot(
                prices=prices,
                weather=[(timestamp, 5.0) for timestamp, _ in prices],
                weather_full=[],
                actions=[],
                horizon_start=issue,
                current_indoor=18.0,
                current_water_temp=35.0,
                heat_curve=HeatCurveConfig(),
                comfort_schedule={},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
                quality_gate={"control_allowed": reader == "primary"},
                control_temperature=control,
                control_input={
                    "available": control.is_usable,
                    "reference_sensor_id": control.reference_sensor_id,
                    "sensor_ids": [sensor.device_id for sensor in control.sensors],
                },
            )
            plan.plan_json = json.dumps(
                {"device_id": "audit-device", "actions": [], "forecast_snapshot": snapshot},
                allow_nan=False,
            )
        return now, plans, readings

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_repair4_independent_serializer_clones_across_pages(self, monkeypatch, reader):
        now, plans, readings = self.serialized_history(monkeypatch, reader, plan_count=6)
        copies = [SimpleNamespace(**{**vars(plan), "id": plan.id + 100}) for plan in plans]
        pages = [copies[2:], copies[:2] + plans[4:], plans[:4], []]

        scorecard = await self.score(monkeypatch, now, plans + copies, readings, pages)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 6
        assert scored["overall"]["samples"] == 30
        assert all(row["samples"] == 6 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        assert scorecard["evidence_coverage_complete"] is True
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("missing", ["device", "sensor", "origin"])
    async def test_repair4_independent_serializer_malformed_peer(
        self, monkeypatch, reader, bad_first, missing
    ):
        now, plans, readings = self.serialized_history(monkeypatch, reader)
        payload = json.loads(plans[0].plan_json)
        if missing == "device":
            payload.pop("device_id")
        elif missing == "sensor":
            payload["forecast_snapshot"].pop("control_input")
        else:
            payload["forecast_snapshot"]["observed_history"] = []
        bad = SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        plans.insert(0 if bad_first else len(plans), bad)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 11
        assert scored["overall"]["samples"] == 55
        assert all(row["samples"] == 11 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        assert scorecard["exclusions"]["ambiguous_forecast_provenance"] == 1
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_repair4_independent_serializer_valid_quorum(self, monkeypatch, reader):
        now, plans, readings = self.serialized_history(monkeypatch, reader)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 12
        assert scored["overall"]["samples"] == 60
        assert all(row["samples"] == 12 for row in scored["horizons"])
        assert scored["quality_gate"]["control_allowed"] is True
        assert scorecard["quality_gate"]["control_allowed"] is (reader != "fallback")
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("malformed", ["all_sources_missing", "empty", "not_list"])
    async def test_QR_F1_identifiable_shadow_peer_invalidates_only_its_learned_origin(
        self, monkeypatch, bad_first, malformed
    ):
        now, plans, readings = self.serialized_history(monkeypatch, "shadow")
        payload = json.loads(plans[0].plan_json)
        snapshot = payload["forecast_snapshot"]
        if malformed == "all_sources_missing":
            for point in snapshot["shadow_forecast_with_plan"]:
                point.pop("model_source")
        else:
            snapshot["shadow_forecast_with_plan"] = [] if malformed == "empty" else None
        bad = SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        plans.insert(0 if bad_first else len(plans), bad)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        assert scorecard["overall"]["samples"] == 55
        assert all(row["samples"] == 11 for row in scorecard["horizons"])
        assert scorecard["fallback"]["overall"]["samples"] == 60
        assert all(row["samples"] == 12 for row in scorecard["fallback"]["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("conflict", [False, True])
    @pytest.mark.parametrize("copies_first", [False, True])
    async def test_R03_serializer_source_aliases_are_not_independent(
        self, monkeypatch, reader, conflict, copies_first
    ):
        now, plans, readings = self.serialized_history(monkeypatch, reader, plan_count=6)
        copies = []
        name = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        for plan in plans:
            payload = json.loads(plan.plan_json)
            for point in payload["forecast_snapshot"][name]:
                point["model_source"] = (
                    "comfort_model_physics_continuation"
                    if reader != "fallback"
                    else "another_unproven_fallback"
                )
                if conflict:
                    point["predicted_indoor_temp"] += 1.0
            copies.append(
                SimpleNamespace(
                    **{**vars(plan), "id": plan.id + 100, "plan_json": json.dumps(payload)}
                )
            )
        rows = copies + plans if copies_first else plans + copies

        scorecard = await self.score(monkeypatch, now, rows, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == (0 if conflict else 30)
        assert all(row["samples"] == (0 if conflict else 6) for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_conflicting_saved_roles_cannot_retain_favorable_peer(self, monkeypatch, reader):
        now, plans, readings = self.history(reader)
        payload = json.loads(plans[0].plan_json)
        payload["forecast_snapshot"]["forecast_status"] = (
            "available" if reader == "fallback" else "fallback"
        )
        plans.append(
            SimpleNamespace(**{**vars(plans[0]), "id": 101, "plan_json": json.dumps(payload)})
        )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 5
        assert scored["overall"]["samples"] == 25
        assert all(row["samples"] == 5 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize(
        "copies", ["offsets", "scope-offsets", "plan-rows", "plan-pages", "outcomes"]
    )
    async def test_replayed_evidence_counts_once(self, monkeypatch, reader, copies):
        now, plans, readings = self.history(reader)
        pages = None
        if copies == "offsets":
            for plan in plans:
                payload = json.loads(plan.plan_json)
                snapshot = payload["forecast_snapshot"]
                series = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
                snapshot[series] += [
                    {
                        **point,
                        "ts": dt.datetime.fromisoformat(point["ts"])
                        .astimezone(dt.timezone(dt.timedelta(hours=2)))
                        .isoformat(),
                    }
                    for point in snapshot[series]
                ]
                plan.plan_json = json.dumps(payload)
        elif copies == "scope-offsets":
            for plan in list(plans):
                payload = json.loads(plan.plan_json)
                snapshot = payload["forecast_snapshot"]
                offset = dt.timezone(dt.timedelta(hours=2))
                snapshot["issue_timestamp"] = (
                    dt.datetime.fromisoformat(snapshot["issue_timestamp"])
                    .astimezone(offset)
                    .isoformat()
                )
                snapshot["observed_history"][0]["ts"] = snapshot["issue_timestamp"]
                for series in ("forecast_with_plan", "shadow_forecast_with_plan"):
                    for point in snapshot.get(series, []):
                        point["ts"] = (
                            dt.datetime.fromisoformat(point["ts"]).astimezone(offset).isoformat()
                        )
                plans.append(SimpleNamespace(**{**vars(plan), "plan_json": json.dumps(payload)}))
        elif copies == "plan-rows":
            plans = plans + [SimpleNamespace(**vars(plan)) for plan in plans]
        elif copies == "plan-pages":
            pages = [list(reversed(plans[2:])), list(reversed(plans[:4])), []]
        else:
            readings += [SimpleNamespace(**vars(row)) for row in readings]
        scorecard = await self.score(monkeypatch, now, plans, readings, pages)
        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 6
        assert scored["overall"]["samples"] == 30
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            hour: 6 for hour in (1, 3, 6, 12, 24)
        }
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["evidence_coverage_complete"] is True
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("copies", ["prediction", "plan-row", "outcome", "nonfinite"])
    async def test_conflicting_identity_is_excluded(self, monkeypatch, reader, bad_first, copies):
        now, plans, readings = self.history(reader)
        if copies == "outcome":
            bad = SimpleNamespace(**vars(readings[0]))
            bad.temperature += 2.0
            bad.timestamp = bad.timestamp.astimezone(dt.timezone(dt.timedelta(hours=2)))
            readings.insert(0 if bad_first else len(readings), bad)
        else:
            plan = plans[0]
            payload = json.loads(plan.plan_json)
            snapshot = payload["forecast_snapshot"]
            series = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
            point = snapshot[series][0]
            bad = {
                **point,
                "predicted_indoor_temp": float("nan")
                if copies == "nonfinite"
                else point["predicted_indoor_temp"] + 2.0,
            }
            if copies == "plan-row":
                snapshot[series][0] = bad
                plans.insert(
                    0 if bad_first else len(plans),
                    SimpleNamespace(**{**vars(plan), "plan_json": json.dumps(payload)}),
                )
            else:
                snapshot[series][:1] = [bad, point] if bad_first else [point, bad]
                plan.plan_json = json.dumps(payload)
        scorecard = await self.score(monkeypatch, now, plans, readings)
        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 29
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            1: 5,
            3: 6,
            6: 6,
            12: 6,
            24: 6,
        }
        assert scorecard["quality_gate"]["control_allowed"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("invalid_prediction", [{"value": 20.11}, [20.11]])
    async def test_unhashable_prediction_conflict_is_excluded(
        self, monkeypatch, reader, bad_first, invalid_prediction
    ):
        now, plans, readings = self.history(reader)
        payload = json.loads(plans[0].plan_json)
        snapshot = payload["forecast_snapshot"]
        series = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        point = snapshot[series][0]
        bad = {**point, "predicted_indoor_temp": invalid_prediction}
        snapshot[series][:1] = [bad, point] if bad_first else [point, bad]
        plans[0].plan_json = json.dumps(payload)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 29
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            1: 5,
            3: 6,
            6: 6,
            12: 6,
            24: 6,
        }
        assert scorecard["exclusions"]["conflicting_forecast_identity"] == 1
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("scope", ["device", "sensor", "issue"])
    async def test_conflicting_persisted_scope_copies_are_excluded(
        self, monkeypatch, reader, bad_first, scope
    ):
        now, plans, readings = self.history(reader)
        plan = plans[0]
        payload = json.loads(plan.plan_json)
        snapshot = payload["forecast_snapshot"]
        if scope == "device":
            payload["device_id"] = "other-device"
        elif scope == "sensor":
            snapshot["control_input"]["reference_sensor_id"] = "other-room"
            snapshot["control_input"]["sensor_ids"] = ["other-room"]
            readings += [
                SimpleNamespace(**{**vars(row), "device_id": "other-room"})
                for row in list(readings)
            ]
        else:
            issue = dt.datetime.fromisoformat(snapshot["issue_timestamp"]) + dt.timedelta(hours=1)
            snapshot["issue_timestamp"] = issue.isoformat()
            snapshot["observed_history"][0]["ts"] = issue.isoformat()
        plans.insert(
            0 if bad_first else len(plans),
            SimpleNamespace(**{**vars(plan), "plan_json": json.dumps(payload)}),
        )

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 5
        assert scored["overall"]["samples"] == 25
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert all(row["samples"] == 5 for row in scored["horizons"])
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    @pytest.mark.parametrize("bad_first", [False, True])
    @pytest.mark.parametrize("method", ["reference_sensor", "selected_sensor_median"])
    async def test_nan_sensor_conflict_is_excluded_by_saved_reader(
        self, monkeypatch, reader, bad_first, method
    ):
        now, plans, readings = self.history(reader)
        if method == "selected_sensor_median":
            readings += [
                SimpleNamespace(
                    **{**vars(row), "device_id": "kitchen", "temperature": row.temperature + 2.0}
                )
                for row in list(readings)
            ]
            for plan in plans:
                payload = json.loads(plan.plan_json)
                snapshot = payload["forecast_snapshot"]
                snapshot["control_input"]["reference_sensor_id"] = ""
                snapshot["control_input"]["sensor_ids"] = ["room", "kitchen"]
                for series in ("forecast_with_plan", "shadow_forecast_with_plan"):
                    for point in snapshot.get(series, []):
                        point["predicted_indoor_temp"] += 1.0
                plan.plan_json = json.dumps(payload)
        bad = SimpleNamespace(**vars(readings[0]))
        bad.temperature = float("nan")
        bad.timestamp = bad.timestamp.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        readings.insert(0 if bad_first else len(readings), bad)

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 29
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert {row["hours"]: row["samples"] for row in scored["horizons"]} == {
            1: 5,
            3: 6,
            6: 6,
            12: 6,
            24: 6,
        }
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_distinct_issues_share_target_without_cross_collapse(self, monkeypatch, reader):
        now, plans, readings = self.history(reader, plan_count=2)
        first = json.loads(plans[0].plan_json)["forecast_snapshot"]
        first_issue = dt.datetime.fromisoformat(first["issue_timestamp"])
        second_issue = first_issue - dt.timedelta(hours=2)
        payload = json.loads(plans[1].plan_json)
        snapshot = payload["forecast_snapshot"]
        snapshot["issue_timestamp"] = second_issue.isoformat()
        snapshot["observed_history"][0]["ts"] = second_issue.isoformat()
        for index, lead in enumerate((1, 3, 6, 12, 24)):
            readings[5 + index].timestamp = second_issue + dt.timedelta(hours=lead)
            if lead == 3:
                readings[5 + index].temperature = readings[0].temperature
            for series in ("forecast_with_plan", "shadow_forecast_with_plan"):
                if series in snapshot:
                    point = snapshot[series][index]
                    point["ts"] = readings[5 + index].timestamp.isoformat()
                    point["predicted_indoor_temp"] = readings[5 + index].temperature + 0.1
        plans[1].created_at = second_issue
        plans[1].horizon_start = second_issue
        plans[1].horizon_end = second_issue + dt.timedelta(hours=24)
        plans[1].plan_json = json.dumps(payload)
        assert first_issue != second_issue
        assert plans[0].id != plans[1].id
        assert readings[0].timestamp == readings[6].timestamp
        assert first["forecast_with_plan"][0]["hour"] == 1
        assert snapshot["forecast_with_plan"][1]["hour"] == 3

        scorecard = await self.score(monkeypatch, now, plans, readings)

        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["plans_scored"] == 2
        assert scored["overall"]["samples"] == 10
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert all(row["samples"] == 2 for row in scored["horizons"])
        assert not scorecard["exclusions"].get("conflicting_forecast_identity")
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard["prospective_qualification_eligible"] is False
        json.dumps(scorecard, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_replay_permutation_preserves_scoring_metrics(self, monkeypatch, reader):
        now, plans, readings = self.history(reader)
        payload = json.loads(plans[0].plan_json)
        snapshot = payload["forecast_snapshot"]
        series = "shadow_forecast_with_plan" if reader == "shadow" else "forecast_with_plan"
        point = snapshot[series][0]
        snapshot[series].append({**point, "predicted_indoor_temp": 99.0})
        plans[0].plan_json = json.dumps(payload)
        replayed = plans + [SimpleNamespace(**vars(plan)) for plan in plans]
        outcomes = readings + [SimpleNamespace(**vars(row)) for row in readings]
        forward = await self.score(monkeypatch, now, replayed, outcomes)
        reversed_plans = []
        for plan in reversed(replayed):
            payload = json.loads(plan.plan_json)
            snapshot = payload["forecast_snapshot"]
            for name in ("forecast_with_plan", "shadow_forecast_with_plan"):
                if name in snapshot:
                    snapshot[name].reverse()
            reversed_plans.append(
                SimpleNamespace(**{**vars(plan), "plan_json": json.dumps(payload)})
            )

        backward = await self.score(monkeypatch, now, reversed_plans, list(reversed(outcomes)))

        for name in ("overall", "horizons", "quality_gate", "bias_correction"):
            assert forward[name] == backward[name]
        for name in ("fallback", "passive_weather_model", "all_forecasts", "sources"):
            assert forward[name] == backward[name]
        scored = backward["fallback"] if reader == "fallback" else backward
        assert scored["overall"]["samples"] == 29
        assert scored["overall"]["mae"] == pytest.approx(0.1)
        assert backward["quality_gate"]["control_allowed"] is False
        assert backward["prospective_qualification_eligible"] is False
        json.dumps(backward, allow_nan=False)

    @pytest.mark.parametrize("reader", ["primary", "shadow", "fallback"])
    async def test_unique_quorum_remains_available(self, monkeypatch, reader):
        now, plans, readings = self.history(reader, plan_count=12)
        scorecard = await self.score(monkeypatch, now, plans, readings)
        scored = scorecard["fallback"] if reader == "fallback" else scorecard
        assert scored["overall"]["samples"] == 60
        assert all(row["samples"] == 12 for row in scored["horizons"])
        assert scored["quality_gate"]["control_allowed"] is True
        assert scorecard["quality_gate"]["control_allowed"] is (reader != "fallback")

    @pytest.mark.parametrize("plan_id", [True, 1.0, "1"])
    async def test_malformed_plan_identity_is_not_coerced(self, monkeypatch, plan_id):
        now, plans, readings = self.history("primary", plan_count=1)
        plans[0].id = plan_id
        scorecard = await self.score(monkeypatch, now, plans, readings)
        assert scorecard["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False

    async def test_equal_values_from_distinct_issues_remain_distinct(self, monkeypatch):
        now, plans, readings = self.history("primary", plan_count=2)
        first = json.loads(plans[0].plan_json)["forecast_snapshot"]["forecast_with_plan"]
        payload = json.loads(plans[1].plan_json)
        for index, point in enumerate(payload["forecast_snapshot"]["forecast_with_plan"]):
            point["predicted_indoor_temp"] = first[index]["predicted_indoor_temp"]
            readings[5 + index].temperature = readings[index].temperature
        plans[1].plan_json = json.dumps(payload)
        scorecard = await self.score(monkeypatch, now, plans, readings)
        assert scorecard["overall"]["samples"] == 10
        assert all(row["samples"] == 2 for row in scorecard["horizons"])

    @pytest.mark.parametrize(
        "source,family",
        [
            ("rule_thermal_fallback", "fallback"),
            ("comfort_model_passive_direct", "passive_weather_model"),
        ],
    )
    async def test_conflicting_learned_shadow_preserves_only_unrelated_family(
        self, monkeypatch, source, family
    ):
        now, plans, readings = self.history("primary", plan_count=1)
        payload = json.loads(plans[0].plan_json)
        snapshot = payload["forecast_snapshot"]
        points = snapshot["forecast_with_plan"]
        snapshot["shadow_forecast_with_plan"] = [
            {**point, "predicted_indoor_temp": 99.0} for point in points
        ]
        snapshot["forecast_with_plan"] = points + [
            {**point, "model_source": source} for point in points
        ]
        plans[0].plan_json = json.dumps(payload)
        scorecard = await self.score(monkeypatch, now, plans, readings)
        assert scorecard["overall"]["samples"] == 0
        assert scorecard["quality_gate"]["control_allowed"] is False
        assert scorecard[family]["overall"]["samples"] == 5
        assert scorecard["all_forecasts"]["overall"]["samples"] == 5
        assert scorecard["sources"][source]["overall"]["samples"] == 5
        assert scorecard["sources"]["comfort_model_controlled"]["overall"]["samples"] == 0
        assert scorecard["exclusions"]["conflicting_forecast_identity"] == 5

    @pytest.mark.parametrize("method", ["reference_sensor", "selected_sensor_median"])
    @pytest.mark.parametrize("conflicting", [False, True])
    def test_sensor_copies_do_not_change_outcomes(self, method, conflicting):
        target = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="room", timestamp=target, temperature=20.0),
            SimpleNamespace(
                device_id="room",
                timestamp=target.astimezone(dt.timezone(dt.timedelta(hours=2))),
                temperature=22.0 if conflicting else 20.0,
            ),
            SimpleNamespace(device_id="kitchen", timestamp=target, temperature=22.0),
        ]
        sensor_ids = ["room"] if method == "reference_sensor" else ["room", "kitchen"]
        observed = _observed_temperature_at(
            rows, target, {"method": method, "sensor_ids": sensor_ids}
        )
        assert observed == (None if conflicting else 20.0 if method == "reference_sensor" else 21.0)


def test_demand_scope_tracks_timezone_and_filter_but_not_comfort_schedule():
    from packages.ml.forecast_quality import native_scope

    base = TestProspectiveNativeEvidence._inputs()
    base["schemas"] = {
        **base["schemas"],
        "artifact": "demand_lifecycle_v1",
        "weather_provider": "open-meteo",
    }
    base["installation_id"] = "installation-a"
    base["timezone"] = "Europe/Stockholm"
    base["tzdata_version"] = "2026a"
    base["weather_provider"] = "open-meteo"
    base["max_rate_kw"] = 12.0
    base["demand_dependency_generation"] = 1
    baseline = native_scope(base)

    schedule_changed = {
        **base,
        "comfort_schedule": {"weekday": [6, 7], "weekend": [9]},
        "comfort_temp_target": 21.0,
        "fixed_monthly_fee": 300,
    }
    assert native_scope(schedule_changed) == baseline
    assert native_scope({**base, "timezone": "UTC"}) != baseline
    assert (
        native_scope(
            {
                **base,
                "timezone": "Europe/Stockholm",
                "demand_dependency_generation": 3,
            }
        )
        != baseline
    )
    assert native_scope({**base, "max_rate_kw": 13.0}) != baseline
    assert native_scope({**base, "demand_dependency_generation": 2}) != baseline

    comfort = {
        **base,
        "schemas": {**base["schemas"], "artifact": "comfort_v8"},
    }
    assert native_scope({**comfort, "timezone": "UTC"}) == native_scope(comfort)


def test_indoor_dependency_scope_tracks_sensor_generation_but_tank_scope_does_not():
    from packages.ml.forecast_quality import native_scope

    base = TestProspectiveNativeEvidence._inputs()
    base.update(
        installation_id="installation-a",
        configured_sensor_ids=["sensor-a", "sensor-b"],
        reference_sensor_id="sensor-a",
        indoor_dependency_generation=1,
        capabilities=("indoor_heating", "indoor_cooling"),
    )
    indoor = {**base, "schemas": {**base["schemas"], "artifact": "thermal_v5"}}
    first = native_scope(indoor)
    second = native_scope({**indoor, "indoor_dependency_generation": 2})
    assert first != second
    assert native_scope({**indoor, "indoor_dependency_generation": 3}) != first

    tank = {
        **base,
        "capabilities": ("tank_heating",),
        "schemas": {**base["schemas"], "artifact": "thermal_v5"},
    }
    assert native_scope({**tank, "indoor_dependency_generation": 2}) == native_scope(tank)

    comfort = {**indoor, "schemas": {**indoor["schemas"], "artifact": "comfort_v8"}}
    assert native_scope(comfort) != native_scope({**comfort, "indoor_dependency_generation": 2})


@pytest.mark.parametrize("reference", ["", "selected", "deselected"])
async def test_R07_native_scope_and_median_only_use_frozen_selected_sensors(monkeypatch, reference):
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import BinaryExpression

    from packages.core.models import DeviceStatusRecord, IndoorTempReading, WeatherRecord
    from packages.ml import forecast_quality

    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    status = DeviceStatusRecord(
        ts=now,
        device_id="audit-device",
        tank_temp=45,
        tank_target_temp=50,
        zone1_temp=35,
        zone1_target_temp=35,
        direction="PUMP",
        device_action="HEATING",
        space_heating_active=True,
        native_policy_provenance={
            "timer": {
                "status": "known",
                "enabled": False,
                "slots": [],
                "fetched_at": now.isoformat(),
                "timezone": "UTC",
            }
        },
    )
    readings = [
        IndoorTempReading(
            timestamp=now - dt.timedelta(minutes=65),
            device_id="selected",
            temperature=20,
            is_stale=False,
        ),
        IndoorTempReading(
            timestamp=now - dt.timedelta(seconds=4),
            device_id="selected",
            temperature=20,
            is_stale=False,
        ),
        IndoorTempReading(
            timestamp=now + dt.timedelta(seconds=1),
            device_id="selected",
            temperature=30,
            is_stale=False,
        ),
        IndoorTempReading(
            timestamp=now, device_id="selected", temperature=float("nan"), is_stale=False
        ),
    ]
    readings += [
        IndoorTempReading(
            timestamp=now - dt.timedelta(seconds=offset),
            device_id="deselected",
            temperature=28,
            is_stale=False,
        )
        for offset in (3, 2, 1)
    ]
    weather = WeatherRecord(
        ts=now, source="open-meteo", temperature=5, forecast_issued_at=now - dt.timedelta(hours=1)
    )
    values = {
        "comfort_reference_sensor_id": reference,
        "smartthings_device_ids": "selected",
        "weather_provider": "open-meteo",
        "price_provider": "manual",
        "timezone": "UTC",
    }
    monkeypatch.setattr(
        "packages.core.settings_service.get_all_settings", AsyncMock(return_value=values)
    )
    statements = []

    async def execute(statement):
        statements.append(statement)
        model = statement.column_descriptions[0]["entity"]
        if model is IndoorTempReading:
            predicates = [
                node
                for criterion in statement._where_criteria
                for node in visitors.iterate(criterion)
                if isinstance(node, BinaryExpression)
                and getattr(node.left, "name", None) == "device_id"
                and node.operator is operators.in_op
            ]
            assert len(predicates) == 1
            assert set(predicates[0].right.value) == {"selected"}
            assert statement._limit_clause.value == 5001
            rows = readings
        else:
            rows = (
                [status]
                if model is DeviceStatusRecord
                else [weather]
                if model is WeatherRecord
                else []
            )
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    monkeypatch.setattr(
        forecast_quality,
        "get_session",
        lambda: _AsyncContext(
            SimpleNamespace(
                execute=execute,
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        installation_id="installation-1",
                        indoor_dependency_generation=1,
                    )
                ),
            )
        ),
    )

    result = await forecast_quality.materialize_native_inputs("comfort", now=now)

    if reference == "deselected":
        assert result is None
        assert statements == []
    else:
        assert result["sensor_basis"]["sensor_ids"] == ["selected"]
        assert result["sensor_basis"]["selection_schema"] == "selected_indoor_sensors_v2"
        assert result["indoor_temperature"] == 20
        assert result["indoor_trend"] == 0
        assert result["schemas"]["input"] == "frozen_native_inputs_v2"
        assert result["installation_id"] == "installation-1"
        assert result["indoor_dependency_generation"] == 1
        assert "component_dependency_fingerprint" in native_scope(result)
        assert all(row["temperature"] == 20 for row in result["sensor_history"])
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("case", ["frozen_union", "empty", "overflow"])
async def test_R34_execution_page_sensor_scope_is_sql_bounded(monkeypatch, case):
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import BinaryExpression

    from packages.core import safety_reverts
    from packages.core.models import IndoorTempReading
    from packages.ml import forecast_quality

    issue = TestProspectiveNativeEvidence._issue()
    second = {
        **issue,
        "input_snapshot": {
            **issue["input_snapshot"],
            "sensor_basis": {"kind": "reference", "sensor_ids": ["other"]},
        },
    }
    statements = []

    async def execute(statement):
        statements.append(statement)
        rows = []
        if statement.column_descriptions[0]["entity"] is IndoorTempReading:
            predicates = [
                node
                for criterion in statement._where_criteria
                for node in visitors.iterate(criterion)
                if isinstance(node, BinaryExpression)
                and getattr(node.left, "name", None) == "device_id"
                and node.operator is operators.in_op
            ]
            assert len(predicates) == 1
            assert set(predicates[0].right.value) == {"room", "other"}
            assert statement._limit_clause.value == 30_001
            if case == "overflow":
                rows = [
                    IndoorTempReading(
                        device_id="room",
                        timestamp=issue["target_at"],
                        temperature=20,
                        is_stale=False,
                    )
                ] * 30_001
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    session = SimpleNamespace(execute=execute)
    monkeypatch.setattr(forecast_quality, "get_session", lambda: _AsyncContext(session))
    unresolved = AsyncMock(return_value=(set(), False))
    historical = AsyncMock(return_value=([], False))
    monkeypatch.setattr(safety_reverts, "unresolved_revert_device_ids", unresolved)
    monkeypatch.setattr(safety_reverts, "historical_revert_overlap_rows", historical)
    page = {
        "rows": [] if case == "empty" else [{"issue": issue}, {"issue": second}],
        "high_water": 5,
        "cursor": 5,
        "complete": True,
    }

    result = await forecast_quality.materialize_execution_page(page, now=issue["target_at"])

    assert result["complete"] is (case != "overflow")
    assert page["high_water"] == page["cursor"] == 5
    if case == "empty":
        assert statements == []
        unresolved.assert_not_awaited()
        historical.assert_not_awaited()
    else:
        assert len(statements) == 5
        unresolved.assert_awaited_once_with(session)
        historical.assert_awaited_once()


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


class TestR03RequiredHorizonIdentity:
    @staticmethod
    async def evaluate(horizons):
        from packages.ml.forecast_quality import evaluate_live_control_gate

        return await evaluate_live_control_gate(
            model_metrics={},
            record_gate=lambda gate, **context: {**gate, **context},
            scorecard_loader=AsyncMock(
                return_value={
                    "overall": {**_passing_horizon(0), "samples": 30},
                    "horizons": horizons,
                }
            ),
            get_float=AsyncMock(side_effect=lambda key: 0.5 if key.endswith("bias_c") else 0.15),
            get_int=AsyncMock(return_value=2),
            get_string=AsyncMock(return_value="off"),
        )

    @pytest.mark.parametrize(
        "hours", [True, False, "1", 1.0, 6.5, float("nan"), float("inf"), float("-inf"), None, 0, 2]
    )
    async def test_invalid_identity_fails_closed(self, hours):
        horizons = [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)]
        horizons[0]["hours"] = hours

        result = await self.evaluate(horizons)

        assert result["control_allowed"] is False
        assert result["status"] in {"failed", "fallback"}
        json.dumps(result, allow_nan=False)

    @pytest.mark.parametrize("hours", [True, False, 1, "1", 1.0, 2])
    @pytest.mark.parametrize("extra_first", [False, True])
    async def test_duplicate_or_unknown_identity_cannot_complete_coverage(self, hours, extra_first):
        horizons = [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)]
        horizons.insert(0 if extra_first else len(horizons), _passing_horizon(hours))

        result = await self.evaluate(horizons)

        assert result["control_allowed"] is False
        json.dumps(result, allow_nan=False)

    async def test_integer_horizons_and_serialized_keys_keep_positive_control(self):
        horizons = json.loads(json.dumps([_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)]))

        result = await self.evaluate(horizons)

        assert result["control_allowed"] is True and result["status"] == "passed"
        assert set(json.loads(result["evaluation_id"])["horizons"]) == {"1", "3", "6", "12", "24"}
        json.dumps(result, allow_nan=False)

    @pytest.mark.parametrize("location", ["key", "row"])
    @pytest.mark.parametrize(
        "hours", [True, False, "1", 1.0, float("nan"), float("inf"), float("-inf"), None, 0, 2]
    )
    def test_mapped_identity_is_validated_before_lookup(self, location, hours):
        horizons = {hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)}
        if location == "key":
            horizons[hours] = horizons.pop(1)
        else:
            horizons[1]["hours"] = hours

        result = _quality_gate({**_passing_horizon(0), "samples": 30}, required_horizons=horizons)

        assert result["control_allowed"] is False
        json.dumps(result, allow_nan=False)

    @pytest.mark.parametrize("missing", [1, 3, 6, 12, 24])
    async def test_missing_horizon_does_not_complete_coverage(self, missing):
        result = await self.evaluate(
            [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24) if hour != missing]
        )

        assert result["control_allowed"] is False
        assert f"horizon_{missing}_missing" in result["reason"]

    @pytest.mark.parametrize("hours", [True, False, "1", 1.0, float("nan"), float("inf"), 0, 2])
    def test_invalid_current_hysteresis_horizon_never_allows_control(self, hours):
        required = (hours, 3, 6, 12, 24)

        result = apply_control_gate_hysteresis(
            {"status": "passed", "control_allowed": True},
            {},
            schema="R03",
            required_horizons=required,
            passes_required=1,
        )

        assert result["control_allowed"] is False and result["raw_status"] == "failed"
        json.dumps(result, allow_nan=False)

    @pytest.mark.parametrize("hours", [True, 1.0])
    def test_noncanonical_previous_hysteresis_horizon_cannot_carry_streak(self, hours):
        result = apply_control_gate_hysteresis(
            {"status": "passed", "control_allowed": True},
            {
                "forecast_quality_gate": {
                    "schema": "R03",
                    "required_horizons": [hours, 3, 6, 12, 24],
                    "pass_streak": 1,
                }
            },
            schema="R03",
            required_horizons=(1, 3, 6, 12, 24),
        )

        assert result["pass_streak"] == 1 and result["control_allowed"] is False

    @pytest.mark.parametrize("hours", [True, False, "1", 1.0, float("nan"), float("inf"), 0, 2])
    def test_weather_readers_cannot_alias_invalid_horizon(self, hours):
        horizons = [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)]
        horizons[0]["hours"] = hours
        scorecard = {"overall": _passing_horizon(0), "horizons": horizons}

        adjustments = control_adjustments_for_weather(scorecard, [{"temperature": 8.0}])
        intervals = prediction_intervals_for_weather(scorecard, [{"temperature": 8.0}])

        assert adjustments["control_allowed"] is False
        assert intervals[0]["source"] == "overall"

    def test_weather_reader_keeps_legitimate_string_json_keys(self):
        scorecard = json.loads(
            json.dumps(
                {
                    "overall": _passing_horizon(0),
                    "horizons": [
                        {**_passing_horizon(1), "p10_signed_error": -0.5, "p90_signed_error": 0.8}
                    ],
                    "horizon_quality": {"1": {"status": "passed"}},
                    "bias_correction": {"by_horizon_c": {"1": 0.25}},
                }
            )
        )

        adjustments = control_adjustments_for_weather(scorecard, [{"temperature": 8.0}])
        intervals = prediction_intervals_for_weather(scorecard, [{"temperature": 8.0}])

        assert adjustments["control_allowed"] is True
        assert intervals[0]["source"] == "1h"
        assert intervals[0]["lower_offset_c"] == pytest.approx(-1.05)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
@pytest.mark.parametrize(
    "field", ["samples", "mae", "bias", "p90_abs_error", "r2", "persistence_improvement_c"]
)
@pytest.mark.parametrize("scope", ["overall", "horizon"])
def test_R03_nonfinite_score_gate_fails_closed(value, field, scope):
    overall = {**_passing_horizon(0), "samples": 30}
    horizons = {hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)}
    (overall if scope == "overall" else horizons[6])[field] = value

    gate = _quality_gate(overall, required_horizons=horizons)

    assert gate["control_allowed"] is False
    assert gate["status"] in {"failed", "observing"}
    json.dumps(gate, allow_nan=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
@pytest.mark.parametrize("threshold", ["min_r2", "min_persistence_improvement_c", "max_abs_bias_c"])
def test_R03_nonfinite_gate_threshold_fails_closed(value, threshold):
    gate = _quality_gate(
        {**_passing_horizon(0), "samples": 30},
        required_horizons={hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)},
        **{threshold: value},
    )

    assert gate["control_allowed"] is False
    json.dumps(gate, allow_nan=False)


def test_R03_finite_complete_score_gate_passes():
    gate = _quality_gate(
        {**_passing_horizon(0), "samples": 30},
        required_horizons={hour: _passing_horizon(hour) for hour in (1, 3, 6, 12, 24)},
    )

    assert gate["control_allowed"] is True
    json.dumps(gate, allow_nan=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
@pytest.mark.parametrize(
    "field", ["abs_errors", "signed_errors", "predicted", "observed", "persistence_errors"]
)
def test_R03_nonfinite_score_bucket_is_serializable_unavailable(value, field):
    inputs = {
        "abs_errors": [0.2, 0.2, 0.2],
        "signed_errors": [0.2, 0.2, 0.2],
        "predicted": [20.2, 21.2, 22.2],
        "observed": [20.0, 21.0, 22.0],
        "persistence_errors": [0.5, 0.5, 0.5],
    }
    inputs[field][1] = value

    score = score_bucket(**inputs)

    assert score["samples"] == 0
    assert score["mae"] is None
    json.dumps(score, allow_nan=False)


def test_R03_score_bucket_overflow_is_serializable_unavailable():
    score = score_bucket([1e308] * 3, [1e308] * 3, [1e308] * 3, [-1e308] * 3, [1e308] * 3)

    assert score["mae"] is None
    json.dumps(score, allow_nan=False)


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
@pytest.mark.parametrize("injected", [False, True])
async def test_R20_default_and_injected_gate_read_actual_baseline_mode(monkeypatch, mode, injected):
    from packages.ml.forecast_quality import evaluate_live_control_gate

    loader = AsyncMock(
        return_value={
            "overall": {**_passing_horizon(0), "samples": 30},
            "horizons": [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)],
        }
    )
    reader = AsyncMock(return_value=mode)
    monkeypatch.setattr("packages.ml.forecast_quality.get_forecast_scorecard", loader)
    monkeypatch.setattr("packages.core.settings_service.get_string_setting", reader)
    monkeypatch.setattr(
        "packages.core.settings_service.get_float_setting",
        AsyncMock(side_effect=lambda key: 0.5 if key.endswith("bias_c") else 0.15),
    )
    monkeypatch.setattr("packages.core.settings_service.get_int_setting", AsyncMock(return_value=2))

    result = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=lambda gate, **context: {**gate, **context},
        **({"scorecard_loader": loader} if injected else {}),
    )

    reader.assert_awaited_once_with("space_heating_baseline_mode")
    expected_context = {"live_baseline_applied": mode == "on"}
    loader.assert_awaited_once_with(evaluation_context=expected_context)
    assert result["evaluation_context"] == expected_context
    assert result["status"] == "passed"


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

    async def get_string(_name):
        return "shadow"

    first = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
        get_string=get_string,
    )
    duplicate = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
        get_string=get_string,
    )
    new_scorecard = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=model.record_forecast_quality_gate,
        scorecard_loader=scorecard_loader,
        get_float=get_float,
        get_int=get_int,
        get_string=get_string,
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
    async def unavailable():
        raise RuntimeError("settings unavailable")

    from packages.optimizer.executor_core import LearningModeState

    target = dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)
    with patch("packages.optimizer.executor_core.resolve_learning_mode_state", unavailable):
        learning_mode_state = await rules_engine._resolve_learning_mode()
    assert learning_mode_state is LearningModeState.UNKNOWN
    learning_mode_active = learning_mode_state is LearningModeState.ACTIVE
    assert learning_mode_active is False
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

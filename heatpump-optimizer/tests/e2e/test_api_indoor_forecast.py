"""Contract tests for the unified indoor-comfort forecast API."""

import datetime as dt
import json

import pytest
from httpx import AsyncClient

from packages.core.models import IndoorTempReading, PlanActionRecord, PlanRecord, SettingRecord
from packages.core.plan_lifecycle import activate_plan


def _scorecard_snapshot(issue_at: dt.datetime, *, shadow: bool) -> dict:
    target_at = issue_at + dt.timedelta(hours=1)
    forecast = [
        {
            "hour": 1,
            "ts": target_at.isoformat(),
            "predicted_indoor_temp": 20.2,
            "model_source": "rule_thermal_fallback",
        }
    ]
    snapshot = {
        "version": "indoor_forecast_v5",
        "forecast_status": "available",
        "scoring_schema": "forecast_outcome_v2_persistence_origin",
        "issue_timestamp": issue_at.isoformat(),
        "observed_history": [{"hour": 0, "ts": issue_at.isoformat(), "temperature": 20.0}],
        "control_input": {
            "available": True,
            "reference_sensor_id": "scorecard-room",
        },
        "forecast_with_plan": forecast,
        "weather_forecast": [{}],
    }
    if shadow:
        snapshot.update(
            {
                "space_heating_baseline": {
                    "effective_mode": "shadow",
                    "live_baseline_applied": False,
                },
                "baseline_evaluation": {"learning_mode": True, "eligible": True},
                "forecast_with_plan_baseline": [
                    {
                        "hour": 1,
                        "ts": target_at.isoformat(),
                        "predicted_indoor_temp": 20.2,
                        "baseline_heating_fraction": 0.5,
                    }
                ],
                "forecast_with_plan_zero_baseline": [
                    {
                        "hour": 1,
                        "ts": target_at.isoformat(),
                        "predicted_indoor_temp": 19.8,
                    }
                ],
                "shadow_forecast_with_plan": [
                    {
                        "hour": 1,
                        "ts": target_at.isoformat(),
                        "predicted_indoor_temp": 20.1,
                        "model_source": "comfort_model_controlled",
                    }
                ],
            }
        )
    return snapshot


def _passing_scorecard(revision: int) -> dict:
    return {
        "overall": {
            "samples": 30,
            "mae": 0.2,
            "bias": 0.0,
            "p90_abs_error": 0.4,
            "r2": 0.3,
            "persistence_improvement_c": 0.2,
            "revision": revision,
        },
        "horizons": [
            {
                "hours": hours,
                "samples": 12,
                "mae": 0.2,
                "bias": 0.0,
                "p90_abs_error": 0.4,
                "r2": 0.3,
                "persistence_improvement_c": 0.2,
            }
            for hours in (1, 3, 6, 12, 24)
        ],
    }


@pytest.mark.asyncio(loop_scope="session")
class TestIndoorForecast:
    async def test_scorecard_excludes_persisted_dispatched_action_outcomes(
        self,
        db_session,
    ):
        from packages.ml.forecast_quality import get_forecast_scorecard

        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        issue_at = now - dt.timedelta(hours=2)
        target_at = issue_at + dt.timedelta(hours=1)
        baseline = await get_forecast_scorecard(now=now)
        plans: list[PlanRecord] = []
        for device_id in (
            "dispatch-filter-executed",
            "dispatch-filter-failed",
            "dispatch-filter-cancelled",
            "dispatch-filter-pending",
            "dispatch-filter-other-device",
            "dispatch-filter-carryover",
            "dispatch-filter-drift",
            "dispatch-filter-before-lookback",
        ):
            plan = PlanRecord(
                created_at=now - dt.timedelta(minutes=30),
                horizon_start=issue_at,
                horizon_end=issue_at + dt.timedelta(hours=2),
                plan_json=json.dumps(
                    {
                        "device_id": device_id,
                        "forecast_snapshot": _scorecard_snapshot(issue_at, shadow=True),
                    }
                ),
                optimizer_version="rules_v5",
                status="superseded",
            )
            db_session.add(plan)
            plans.append(plan)
        older_plan = PlanRecord(
            created_at=issue_at - dt.timedelta(hours=2),
            horizon_start=issue_at - dt.timedelta(hours=2),
            horizon_end=issue_at,
            plan_json=json.dumps({"device_id": "dispatch-filter-carryover"}),
            optimizer_version="rules_v5",
            status="superseded",
        )
        db_session.add(older_plan)
        db_session.add(
            IndoorTempReading(
                timestamp=target_at,
                device_id="scorecard-room",
                temperature=20.0,
                is_stale=False,
            )
        )
        await db_session.flush()
        carryover_source = PlanActionRecord(
            plan_id=older_plan.id,
            scheduled_ts=issue_at - dt.timedelta(hours=1),
            action_type="zone_temp_boost",
            payload_json="{}",
            device_id="dispatch-filter-carryover",
            status="executed",
            executed_at=issue_at - dt.timedelta(hours=1),
        )
        db_session.add(carryover_source)
        await db_session.flush()
        db_session.add_all(
            [
                PlanActionRecord(
                    plan_id=plans[0].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-executed",
                    status="executed",
                    executed_at=target_at,
                    result_json=json.dumps({"success": True, "verified": True}),
                ),
                PlanActionRecord(
                    plan_id=plans[1].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-failed",
                    status="failed",
                    executed_at=target_at,
                    verify_attempts=1,
                ),
                PlanActionRecord(
                    plan_id=plans[2].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-cancelled",
                    status="cancelled",
                    executed_at=target_at,
                    result_json=json.dumps({"dispatched": True}),
                ),
                PlanActionRecord(
                    plan_id=plans[3].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-pending",
                    status="pending",
                ),
                PlanActionRecord(
                    plan_id=plans[4].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="other-device",
                    status="executed",
                    executed_at=target_at,
                ),
                PlanActionRecord(
                    plan_id=older_plan.id,
                    reverts_action_id=carryover_source.id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_restore",
                    payload_json="{}",
                    device_id="dispatch-filter-carryover",
                    status="executed",
                    executed_at=target_at,
                ),
                PlanActionRecord(
                    plan_id=plans[6].id,
                    scheduled_ts=target_at,
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-drift",
                    status="executed",
                    executed_at=plans[6].horizon_end + dt.timedelta(seconds=90),
                ),
                PlanActionRecord(
                    plan_id=plans[7].id,
                    scheduled_ts=issue_at - dt.timedelta(hours=25),
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id="dispatch-filter-before-lookback",
                    status="executed",
                    executed_at=issue_at - dt.timedelta(hours=25),
                ),
            ]
        )
        await db_session.commit()

        scorecard = await get_forecast_scorecard(now=now)

        comparison = scorecard["baseline_comparison"]
        baseline_comparison = baseline["baseline_comparison"]
        assert comparison["pairs_scored"] == baseline_comparison["pairs_scored"] + 4
        assert comparison["plans_scored"] == baseline_comparison["plans_scored"] + 4
        assert comparison["exclusions"]["historical_safety_revert_window"] == 1
        assert comparison["exclusions"]["dispatch_in_or_near_window"] == 3

    async def test_superseded_pending_action_does_not_contaminate_next_plan(self, db_session):
        from packages.ml.forecast_quality import get_forecast_scorecard

        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        issue_at = now - dt.timedelta(hours=2)
        target_at = issue_at + dt.timedelta(hours=1)
        plan_payload = json.dumps(
            {
                "device_id": "supersession-filter-device",
                "forecast_snapshot": _scorecard_snapshot(issue_at, shadow=True),
            }
        )
        first = PlanRecord(
            created_at=now - dt.timedelta(minutes=30),
            horizon_start=issue_at,
            horizon_end=issue_at + dt.timedelta(hours=2),
            plan_json=plan_payload,
            optimizer_version="rules_v5",
        )
        db_session.add(first)
        await activate_plan(db_session, first)
        db_session.add(
            PlanActionRecord(
                plan_id=first.id,
                scheduled_ts=target_at,
                action_type="zone_temp_boost",
                payload_json="{}",
                device_id="supersession-filter-device",
                status="pending",
            )
        )
        second = PlanRecord(
            created_at=now - dt.timedelta(minutes=20),
            horizon_start=issue_at,
            horizon_end=issue_at + dt.timedelta(hours=2),
            plan_json=plan_payload,
            optimizer_version="rules_v5",
        )
        await activate_plan(db_session, second)
        db_session.add(
            IndoorTempReading(
                timestamp=target_at,
                device_id="scorecard-room",
                temperature=20.0,
                is_stale=False,
            )
        )
        await db_session.commit()

        scorecard = await get_forecast_scorecard(now=now)

        comparison = scorecard["baseline_comparison"]
        assert comparison["pairs_scored"] >= 2
        assert comparison["exclusions"].get("dispatch_in_or_near_window", 0) == 0

    async def test_historical_restore_inside_horizon_excludes_old_source_and_skips_old_rows(
        self, db_session
    ):
        from packages.core.safety_reverts import historical_revert_overlap_rows

        horizon_start = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        old_source_at = horizon_start - dt.timedelta(hours=25)
        plan = PlanRecord(
            horizon_start=old_source_at,
            horizon_end=horizon_start + dt.timedelta(hours=2),
            plan_json="{}",
            optimizer_version="rules_v5",
            status="superseded",
        )
        db_session.add(plan)
        await db_session.flush()
        db_session.add_all(
            [
                PlanActionRecord(
                    plan_id=plan.id,
                    scheduled_ts=old_source_at - dt.timedelta(days=index + 2),
                    action_type="zone_temp_boost",
                    payload_json="{}",
                    device_id=f"old-resolved-{index}",
                    status="executed",
                    executed_at=old_source_at - dt.timedelta(days=index + 2),
                )
                for index in range(501)
            ]
        )
        source = PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=old_source_at,
            action_type="zone_temp_boost",
            payload_json="{}",
            device_id="old-source-new-restore",
            status="executed",
            executed_at=old_source_at,
        )
        db_session.add(source)
        await db_session.flush()
        db_session.add(
            PlanActionRecord(
                plan_id=plan.id,
                reverts_action_id=source.id,
                scheduled_ts=horizon_start + dt.timedelta(minutes=30),
                action_type="zone_temp_restore",
                payload_json="{}",
                device_id="old-source-new-restore",
                status="executed",
                executed_at=horizon_start + dt.timedelta(minutes=30),
            )
        )
        await db_session.commit()

        rows, overflowed = await historical_revert_overlap_rows(
            db_session,
            horizon_start=horizon_start,
            horizon_end=horizon_start + dt.timedelta(hours=2),
            drift_margin=dt.timedelta(minutes=2),
        )

        assert overflowed is False
        assert rows == [
            (
                "old-source-new-restore",
                old_source_at,
                "executed",
                horizon_start + dt.timedelta(minutes=30),
            )
        ]

    async def test_persisted_plans_do_not_break_scorecard_consumers(
        self,
        client: AsyncClient,
        db_session,
    ):
        from packages.api.routers.settings import get_baseline_promotion_readiness
        from packages.core.operational_alerts import get_operational_alerts
        from packages.core.settings_service import get_setting, set_setting
        from packages.ml.forecast_quality import get_forecast_scorecard

        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        issue_at = now - dt.timedelta(hours=3)
        plans = [
            PlanRecord(
                created_at=now - dt.timedelta(hours=1),
                horizon_start=issue_at,
                horizon_end=issue_at + dt.timedelta(hours=2),
                plan_json=json.dumps(
                    {
                        "device_id": "scorecard-device",
                        "forecast_snapshot": _scorecard_snapshot(issue_at, shadow=False),
                    }
                ),
                optimizer_version="rules_v4",
                status="superseded",
            ),
            PlanRecord(
                created_at=now - dt.timedelta(minutes=30),
                horizon_start=issue_at,
                horizon_end=issue_at + dt.timedelta(hours=2),
                plan_json=json.dumps(
                    {
                        "device_id": "scorecard-device",
                        "forecast_snapshot": _scorecard_snapshot(issue_at, shadow=True),
                    }
                ),
                optimizer_version="rules_v5",
            ),
        ]
        db_session.add_all(plans)
        db_session.add_all(
            [
                IndoorTempReading(
                    timestamp=issue_at + dt.timedelta(hours=1),
                    device_id="scorecard-room",
                    temperature=20.0,
                    is_stale=False,
                ),
            ]
        )
        await db_session.commit()

        scorecard = await get_forecast_scorecard(now=now)
        route_response = await client.get("/api/thermal/forecast-scorecard")
        alerts = await get_operational_alerts(now=now)
        readiness = await get_baseline_promotion_readiness()
        await set_setting("_heat_curve_verification_state", "{}")

        assert scorecard["plans_scored"] >= 1
        assert scorecard["baseline_comparison"]["pairs_scored"] == 1
        assert scorecard["baseline_comparison"]["plans_scored"] == 1
        assert route_response.status_code == 200
        assert isinstance(alerts["alerts"], list)
        assert readiness is False
        assert await get_setting("_heat_curve_verification_state") == "{}"

    async def test_forecast_without_scorecard_evidence_falls_back_and_is_not_on_target(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
        seed_weather,
    ):
        db_session.add(
            IndoorTempReading(
                timestamp=dt.datetime.now(dt.timezone.utc),
                device_id="forecast-reference",
                temperature=21.2,
                is_stale=False,
            )
        )
        await db_session.commit()
        response = await client.get("/api/thermal/indoor-forecast?hours=4")

        assert response.status_code == 200
        data = response.json()
        assert [point["hour"] for point in data["forecast_with_plan"]] == [1, 2, 3, 4]
        assert [point["hour"] for point in data["forecast_no_heating"]] == [1, 2, 3, 4]
        assert [target["hour"] for target in data["target_schedule"]] == [1, 2, 3, 4]
        assert len(data["weather_forecast"]) == 4

        for weather in data["weather_forecast"]:
            assert set(weather) == {
                "ts",
                "hour",
                "outdoor_temp",
                "wind_speed",
                "irradiance",
                "precipitation",
                "input_status",
                "imputed_fields",
            }
            assert 0 <= weather["hour"] <= 23
            assert weather["input_status"] in {"observed", "imputed"}
            assert isinstance(weather["imputed_fields"], list)

        timestamps = [weather["ts"] for weather in data["weather_forecast"]]
        assert timestamps == sorted(timestamps)
        assert len(data["price_forecast"]) == 4
        assert data["forecast_source"] == "live_estimate"
        assert data["forecast_status"] == "fallback"
        assert data["comfort_assessment"]["state"] != "on_target"
        assert data["display_status"] in {"fresh", "degraded"}

    async def test_forecast_with_two_passing_scorecards_is_available(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
        seed_weather,
        monkeypatch,
    ):
        from packages.ml import forecast_quality
        from packages.ml.comfort_model import comfort_model

        db_session.add(
            IndoorTempReading(
                timestamp=dt.datetime.now(dt.timezone.utc),
                device_id="forecast-reference",
                temperature=21.2,
                is_stale=False,
            )
        )
        await db_session.commit()

        monkeypatch.setattr(
            comfort_model,
            "_metrics",
            {"r2": 0.3, "persistence_improvement_c": 0.2},
        )
        scorecards = iter([_passing_scorecard(1), _passing_scorecard(2)])

        async def next_scorecard():
            return next(scorecards)

        from packages.ml.forecast_quality import evaluate_live_control_gate

        first = await evaluate_live_control_gate(
            model_metrics=comfort_model.metrics,
            record_gate=comfort_model.record_forecast_quality_gate,
            scorecard_loader=next_scorecard,
        )
        second = await evaluate_live_control_gate(
            model_metrics=comfort_model.metrics,
            record_gate=comfort_model.record_forecast_quality_gate,
            scorecard_loader=next_scorecard,
        )

        async def current_scorecard():
            return _passing_scorecard(2)

        monkeypatch.setattr(
            forecast_quality,
            "get_forecast_scorecard",
            current_scorecard,
        )

        response = await client.get("/api/thermal/indoor-forecast?hours=4")

        assert first["status"] == "observing"
        assert second["status"] == "allowed"
        assert response.status_code == 200
        assert response.json()["forecast_status"] == "available"

    async def test_active_plan_returns_its_saved_forecast_snapshot(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
    ):
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        snapshot = {
            "version": "indoor_forecast_v1",
            "current_indoor": 21.2,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": [],
            "price_forecast": [],
        }
        for hour in range(1, 5):
            slot_start = now + dt.timedelta(hours=hour - 1)
            state_ts = slot_start + dt.timedelta(hours=1)
            snapshot["forecast"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "predicted_indoor_temp": 21.0 + hour}
            )
            snapshot["forecast_with_plan"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 21.0 + hour,
                    "source": "milp_solution",
                    "space_heating_fraction": 0.5,
                }
            )
            snapshot["forecast_no_heating"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 20.5 - hour,
                    "source": "milp_counterfactual",
                }
            )
            snapshot["target_schedule"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "target": 20.5}
            )
            snapshot["weather_forecast"].append(
                {
                    "ts": slot_start.isoformat(),
                    "hour": slot_start.hour,
                    "outdoor_temp": 4.0,
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "precipitation": 1.2,
                }
            )
            snapshot["price_forecast"].append(
                {"ts": slot_start.isoformat(), "price_eur_per_kwh": 0.12}
            )

        plan = PlanRecord(
            horizon_start=now,
            horizon_end=now + dt.timedelta(hours=4),
            plan_json=json.dumps({"forecast_snapshot": snapshot}),
            optimizer_version="milp_v1",
        )
        db_session.add(plan)
        await db_session.commit()

        response = await client.get("/api/thermal/indoor-forecast?hours=2")

        assert response.status_code == 200
        data = response.json()
        assert data["forecast_source"] == "active_plan"
        assert data["plan_id"] == plan.id
        assert data["current_indoor"] == 21.2
        assert data["forecast_with_plan"][0]["predicted_indoor_temp"] == 22.0
        assert data["forecast_no_heating"][0]["predicted_indoor_temp"] == 19.5
        assert data["price_forecast"][0]["price_eur_per_kwh"] == 0.12

    async def test_active_plan_snapshot_shorter_than_requested_horizon_falls_back_to_live_forecast(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
        seed_weather,
    ):
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        db_session.add(
            IndoorTempReading(
                timestamp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
                device_id="forecast-reference",
                temperature=21.2,
                is_stale=False,
            )
        )
        db_session.add(SettingRecord(key="smartthings_device_ids", value="forecast-reference"))
        db_session.add(SettingRecord(key="comfort_reference_sensor_id", value="forecast-reference"))
        await db_session.commit()
        snapshot = {
            "version": "indoor_forecast_v1",
            "current_indoor": 21.2,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": [],
            "price_forecast": [],
        }
        for hour in range(1, 15):
            slot_start = now + dt.timedelta(hours=hour - 1)
            state_ts = slot_start + dt.timedelta(hours=1)
            snapshot["forecast"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "predicted_indoor_temp": 21.0 + hour}
            )
            snapshot["forecast_with_plan"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 21.0 + hour,
                    "source": "milp_solution",
                    "space_heating_fraction": 0.5,
                }
            )
            snapshot["forecast_no_heating"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 20.5 - hour,
                    "source": "milp_counterfactual",
                }
            )
            snapshot["target_schedule"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "target": 20.5}
            )
            snapshot["weather_forecast"].append(
                {
                    "ts": slot_start.isoformat(),
                    "hour": slot_start.hour,
                    "outdoor_temp": 4.0,
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "precipitation": 1.2,
                }
            )
            snapshot["price_forecast"].append(
                {"ts": slot_start.isoformat(), "price_eur_per_kwh": 0.12}
            )

        plan = PlanRecord(
            created_at=now - dt.timedelta(hours=1),
            horizon_start=now - dt.timedelta(hours=1),
            horizon_end=now + dt.timedelta(hours=23),
            plan_json=json.dumps({"forecast_snapshot": snapshot}),
            optimizer_version="milp_v1",
        )
        db_session.add(plan)
        await db_session.commit()

        response = await client.get("/api/thermal/indoor-forecast?hours=24")

        assert response.status_code == 200
        data = response.json()
        assert data["forecast_source"] == "live_estimate"
        assert len(data["forecast_with_plan"]) == 24
        assert len(data["forecast_no_heating"]) == 24
        assert len(data["target_schedule"]) == 24
        assert len(data["weather_forecast"]) == 24

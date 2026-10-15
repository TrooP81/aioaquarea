"""Offline represented-strategy and resource-ordering repair controls."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest

from packages.optimizer import DataIncompleteError, InfeasibleError
from packages.optimizer.represented_strategy import (
    replay_serialized_actions,
    solve_represented_trace,
)


def trace(hours=3):
    start = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    return declare_native(
        {
            "schema": "native_sh_trace_v1",
            "native_sh_basis": "assumed",
            "device_id": "offline-device",
            "observation_context": {
                "device_id": "offline-device",
                "observed_at": start.isoformat(),
                "captured_at": start.isoformat(),
                "max_age_seconds": 900,
            },
            "observed_tank_target_c": 55.0,
            "observed_force_dhw": 0,
            "initial_tank_c": 48.0,
            "initial_indoor_c": 20.0,
            "tank_ceiling_c": 55.0,
            "tank_hardware_floor_c": 40.0,
            "auxiliary_power_kw": 0.0,
            "indoor_ceiling_c": 22.0,
            "deadline_hours": [hours],
            "deadline_tank_floors_c": [48.0],
            "price_currency": "SEK",
            "hours": [
                {
                    "ts": (start + dt.timedelta(hours=hour)).isoformat(),
                    "price_per_kwh": [-0.2, 0.3, 0.1][hour % 3],
                    "gate_state": "ALLOWED",
                    "native_sh_fraction": 0.5,
                    "native_sh_power_kw": 4.0,
                    "dhw_power_kw": 1.0,
                    "tank_net_heating_c_per_h": 5.0,
                    "tank_loss_c_per_h": -0.5,
                    "indoor_full_heat_c_per_h": 0.2,
                    "indoor_passive_c_per_h": -0.1,
                    "tank_floor_c": 48.0,
                    "indoor_floor_c": 19.0,
                }
                for hour in range(hours)
            ],
        }
    )


def declare_native(value):
    """Explicit synthetic assumption; never used to infer a real installation's duty."""
    value["native_sh_assumption"] = "unchanged_hourly_load_over_declared_max_electrical_power"
    value["native_sh_provenance"] = {
        "kind": "explicit_conditional_assumption",
        "method": value["native_sh_assumption"],
        "device_id": value["device_id"],
        "captured_at": value["observation_context"]["captured_at"],
        "horizon_start": value["hours"][0]["ts"],
        "horizon_end": (
            dt.datetime.fromisoformat(value["hours"][-1]["ts"]) + dt.timedelta(hours=1)
        ).isoformat(),
        "maximum_electrical_power_kw": value["hours"][0]["native_sh_power_kw"],
        "hourly_electrical_kw": [
            row["native_sh_fraction"] * row["native_sh_power_kw"] for row in value["hours"]
        ],
    }
    return value


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "48"])
def test_R28_missing_actual_tank_never_defaults(value):
    from packages.optimizer.milp import _observed_tank_temperature

    with pytest.raises(DataIncompleteError, match="Observed finite tank"):
        _observed_tank_temperature(SimpleNamespace(tank_temp=value))


def owner_context(value):
    from packages.core.config import settings

    provenance = {
        **value["native_sh_provenance"],
        "maximum_electrical_power_kw": settings.sh_max_power_kw,
        "hourly_electrical_kw": [
            row["native_sh_fraction"] * settings.sh_max_power_kw for row in value["hours"]
        ],
    }
    return {
        **value["observation_context"],
        "tank_target_c": value["observed_tank_target_c"],
        "force_dhw": value["observed_force_dhw"],
        "tank_hardware_floor_c": value["tank_hardware_floor_c"],
        "auxiliary_power_kw": value["auxiliary_power_kw"],
        "native_sh_provenance": provenance,
    }


@pytest.mark.parametrize(
    "case",
    [
        "at_target",
        "crossing_target",
        "missing_target",
        "unknown_state",
        "boolean_state",
        "active_state",
        "future_clock",
        "wrong_device",
    ],
)
def test_S4_QR1_target_and_observation_semantics_refuse_unproven(case):
    value = trace(1)
    if case == "at_target":
        value["initial_tank_c"] = 50
        value["observed_tank_target_c"] = 50
    elif case == "crossing_target":
        value["observed_tank_target_c"] = 50
    elif case == "missing_target":
        value["observed_tank_target_c"] = None
    elif case == "unknown_state":
        value["observed_force_dhw"] = None
    elif case == "boolean_state":
        value["observed_force_dhw"] = False
    elif case == "active_state":
        value["observed_force_dhw"] = 1
    elif case == "future_clock":
        value["observation_context"]["observed_at"] = (
            dt.datetime.fromisoformat(value["hours"][0]["ts"]) + dt.timedelta(seconds=1)
        ).isoformat()
    else:
        value["observation_context"]["device_id"] = "other"
    with pytest.raises(DataIncompleteError):
        replay_serialized_actions(value, pulse(value, 0, 30))


@pytest.mark.parametrize("tank", [48.0, 50.0])
async def test_S4_QR1_actual_command_handler_contract_without_device_IO(tank):
    from packages.optimizer.actions.registry import _dispatch_force_dhw_on

    wrapper = SimpleNamespace(
        get_device=AsyncMock(
            return_value=SimpleNamespace(
                tank=SimpleNamespace(temperature=tank, target_temperature=50.0)
            )
        ),
        force_dhw=AsyncMock(return_value=True),
    )
    result = await _dispatch_force_dhw_on(wrapper, {})
    if tank == 50:
        assert result["skip"] and result["reason"] == "tank_at_target"
        wrapper.force_dhw.assert_not_awaited()
    else:
        assert result["force_dhw"] == "ON"
        wrapper.force_dhw.assert_awaited_once()


def test_S4_QR1_at_target_negative_price_never_credits_suppressed_DHW():
    value = trace(1)
    value["initial_tank_c"] = value["observed_tank_target_c"] = 50
    value["hours"][0].update(gate_state="BLOCKED", native_sh_fraction=0)
    declare_native(value)
    actions, result = solve_represented_trace(value, timeout_seconds=10)
    assert actions == []
    assert result["electricity_kwh"] == result["cost"] == 0


def test_R28d_pulse_ceiling_is_not_masked_by_observed_target_admission():
    value = trace(1)
    value["initial_tank_c"] = 48.9
    value["tank_ceiling_c"] = 49.0
    peak = 48.9 + 5 * (5 / 60)
    endpoint = peak - 0.5 * (55 / 60)
    assert 48 <= endpoint < 49 < peak < value["observed_tank_target_c"] - 0.5
    with pytest.raises(InfeasibleError, match="tank floor or ceiling"):
        replay_serialized_actions(value, pulse(value, 0, 5))


@pytest.mark.parametrize("offset", [0, -0.0001])
async def test_S4_QR1_exact_dispatch_target_boundary(monkeypatch, offset):
    from packages.optimizer.actions.registry import _dispatch_force_dhw_on

    tank = 49.5 + offset
    wrapper = SimpleNamespace(
        get_device=AsyncMock(
            return_value=SimpleNamespace(
                tank=SimpleNamespace(temperature=tank, target_temperature=50)
            )
        ),
        force_dhw=AsyncMock(return_value=True),
    )
    result = await _dispatch_force_dhw_on(wrapper, {})
    if offset == 0:
        assert result["reason"] == "tank_at_target"
        wrapper.force_dhw.assert_not_awaited()
    else:
        assert result["force_dhw"] == "ON"
        wrapper.force_dhw.assert_awaited_once()
    value = trace(1)
    value["initial_tank_c"] = tank
    value["observed_tank_target_c"] = 50
    # Below-threshold admission alone cannot prove a five-minute pulse remains below target.
    with pytest.raises(DataIncompleteError, match="suppressed|termination"):
        replay_serialized_actions(value, pulse(value, 0, 5))
    value["hours"][0].update(gate_state="BLOCKED", native_sh_fraction=0)
    declare_native(value)
    actions, result = solve_represented_trace(value, timeout_seconds=10)
    assert actions == [] and result["electricity_kwh"] == 0


@pytest.mark.parametrize("case", ["valid", "stale", "future_native", "malformed_native"])
async def test_S4_QR1_capture_uses_real_cached_clocks_and_existing_freshness(monkeypatch, case):
    from packages.optimizer.represented_strategy import observation_context

    captured = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    observed = captured - dt.timedelta(seconds=20 if case != "stale" else 901)
    status = SimpleNamespace(
        device_id="clock-device", ts=observed, tank_target_temp=55, force_dhw=0
    )
    if case == "future_native":
        status.native_policy_provenance = {
            "captured_at": (captured + dt.timedelta(seconds=1)).isoformat()
        }
    elif case == "malformed_native":
        status.native_policy_provenance = "not-a-provenance-dict"
    monkeypatch.setattr(
        "packages.core.settings_service.get_setting",
        AsyncMock(
            side_effect=lambda name: {
                "device_status_max_age_minutes": 15,
                "poll_interval_seconds": 300,
            }[name]
        ),
    )
    if case == "valid":
        context = await observation_context(status, captured)
        assert context["observed_at"] == observed
        assert context["captured_at"] == captured and context["max_age_seconds"] == 900
    else:
        with pytest.raises(DataIncompleteError, match="Cached device status clocks"):
            await observation_context(status, captured)


@pytest.mark.parametrize("invalid_pair", [False, True])
async def test_S4_QR2_actual_rules_generation_without_optional_solver(monkeypatch, invalid_pair):
    import builtins
    import sys
    from packages.optimizer import rules_engine as rules
    from packages.optimizer.executor_core import LearningModeState
    from packages.core.control_temperature import ControlTemperature
    from packages.core.heat_curve import HeatCurveConfig

    real_import = builtins.__import__

    def deny_solver(name, *args, **kwargs):
        if name == "pulp" or name.startswith("pulp.") or name == "packages.optimizer.milp":
            raise AssertionError("Rules generation must not import solver owner")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_solver)
    monkeypatch.setitem(sys.modules, "pulp", None)
    optimizer = rules.RulesOptimizer()
    now = dt.datetime.now(dt.timezone.utc)
    start = rules.next_hour_boundary(now)
    value = trace(1)
    value["hours"][0]["ts"] = start.isoformat()
    actions = pulse(value, 0, 30)
    if invalid_pair:
        actions[1]["reverts_action_key"] = "wrong"
    status = SimpleNamespace(
        device_id="offline-device",
        ts=now,
        tank_temp=49,
        tank_target_temp=55,
        force_dhw=0,
        tank_heat_max=55,
        tank_heat_min=40,
        holiday_mode=0,
        zone1_temp=35,
        zone1_target_temp=None,
        zone1_heat_min=None,
        zone1_heat_max=None,
        heat_pump_outdoor_temp=5,
        outdoor_temp=5,
    )
    declared = trace(1)
    declared["hours"][0]["ts"] = start.isoformat()
    declared["hours"][0]["native_sh_fraction"] = 0
    declared["observation_context"]["captured_at"] = now.isoformat()
    declare_native(declared)
    status.native_policy_provenance = {
        "diagnostic_assumptions": {
            "native_sh_provenance": owner_context(declared)["native_sh_provenance"],
            "auxiliary_power_kw": 0,
        }
    }
    session = SimpleNamespace(
        info={}, execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    )

    @asynccontextmanager
    async def sessions(**kwargs):
        yield session

    monkeypatch.setattr(rules, "get_session", sessions)
    monkeypatch.setattr(
        rules, "_resolve_learning_mode", AsyncMock(return_value=LearningModeState.ACTIVE)
    )
    for name, result in (
        ("_get_prices", [(start, -0.2)]),
        ("_get_weather", [(start, 5)]),
        ("_get_weather_full", []),
        ("_get_last_status", status),
    ):
        monkeypatch.setattr(optimizer, name, AsyncMock(return_value=result))
    monkeypatch.setattr(
        rules,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(
                effective_c=5,
                heat_pump_c=5,
                weather_c=5,
                source="cached",
                weather_provider="offline",
                compensation_c=0,
                fallback_reason=None,
            )
        ),
    )
    monkeypatch.setattr(rules, "load_recent_heating_evidence", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(rules.thermal_model, "arefresh_if_changed", AsyncMock())
    monkeypatch.setattr(rules.thermal_model.params, "last_calibrated", None)
    monkeypatch.setattr(rules.thermal_model, "_indoor_heating_rate", lambda *a: 0.2)
    monkeypatch.setattr(rules.thermal_model, "_indoor_cooling_rate", lambda *a: -0.1)
    monkeypatch.setattr(
        rules,
        "get_control_temperature",
        AsyncMock(
            return_value=ControlTemperature(
                value=20, confidence="high", sensor_count=1, sample_count=1, latest_reading=now
            )
        ),
    )
    monkeypatch.setattr(
        rules, "evaluate_live_control_gate", AsyncMock(return_value={"control_allowed": False})
    )
    monkeypatch.setattr(rules, "get_heat_curve_config", AsyncMock(return_value=HeatCurveConfig()))
    monkeypatch.setattr(rules, "get_space_heating_gate_config", AsyncMock(return_value={}))
    gate = SimpleNamespace(
        **dict.fromkeys(
            (
                "reason_code",
                "profile_id",
                "base_c",
                "on_threshold_c",
                "off_threshold_c",
                "last_raw_outdoor_c",
                "fingerprint_matches",
            ),
            None,
        ),
        state="BLOCKED",
    )
    monkeypatch.setattr(rules, "resolve_effective_gate", lambda *a: gate)
    monkeypatch.setattr(rules, "project_gate_states", lambda *a: [SimpleNamespace(state="BLOCKED")])
    monkeypatch.setattr(
        rules,
        "build_baseline_duty_profile",
        lambda *a, **k: SimpleNamespace(
            points=[SimpleNamespace(expected_fraction=0, floor_fraction=0)]
        ),
    )
    monkeypatch.setattr(
        rules,
        "get_setting",
        AsyncMock(
            side_effect=lambda name: {
                "space_heating_baseline_mode": "off",
                "space_heating_default_fraction": 0,
            }[name]
        ),
    )
    monkeypatch.setattr(
        "packages.core.settings_service.get_setting",
        AsyncMock(
            side_effect=lambda name: {
                "device_status_max_age_minutes": 15,
                "poll_interval_seconds": 300,
            }[name]
        ),
    )
    monkeypatch.setattr(
        rules,
        "get_float_setting",
        AsyncMock(
            side_effect=lambda name: {
                "learned_schedule_threshold": 0.2,
                "comfort_temp_target": 20,
                "comfort_temp_min": 19,
                "comfort_temp_max": 22,
                "indoor_forecast_max_passive_change_c_per_hour": 0.5,
                "tank_min_temp": 48,
                "tank_min_temp_offpeak": 48,
                "tank_max_temp": 55,
            }[name]
        ),
    )
    monkeypatch.setattr(rules, "get_int_setting", AsyncMock(return_value=0))
    monkeypatch.setattr(rules, "get_effective_schedule", AsyncMock(return_value={}))
    monkeypatch.setattr(rules, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(rules, "panasonic_tank_heating_available", lambda *a: True)
    monkeypatch.setattr(rules, "panasonic_zone_heating_available", lambda *a: False)
    monkeypatch.setattr(optimizer, "_plan_dhw", lambda *a, **k: actions)
    monkeypatch.setattr(optimizer, "_plan_peak_avoidance", lambda *a: [])
    monkeypatch.setattr(optimizer, "_plan_quiet_mode", lambda *a, **k: [])
    monkeypatch.setattr(optimizer, "_estimate_unshifted_baseline", AsyncMock(return_value={}))
    monkeypatch.setattr(optimizer, "_build_forecast_snapshot", lambda **k: {})
    plan = await optimizer.generate_plan()
    if invalid_pair:
        assert plan is None
    else:
        assert plan["diagnostic_status"] == "conditional_feasible"
        assert plan["actions"][0]["payload"]["dhw_minutes"] == 30
        assert plan["represented_strategy"]["observed_tank_target_c"] == 55
        assert plan["serialized_replay"]["electricity_kwh"] > 0
        assert plan["cost_estimate"] < 0


def _publication_plan(monkeypatch):
    """Six-hour represented diagnostic consuming one producer-built frozen batch."""
    from packages.core.config import settings
    from packages.core.planning_data_quality import (
        MINIMUM_CONTROL_HOURS,
        PlanningInputBatch,
        PriceInput,
        WeatherInput,
        planning_batch_quality,
    )
    from packages.ml.thermal import thermal_model
    from packages.optimizer import represented_strategy

    monkeypatch.setattr(thermal_model.params, "last_calibrated", None)
    monkeypatch.setattr(settings, "sh_max_power_kw", 12.0)
    monkeypatch.setattr(settings, "tank_max_temp", 55)
    monkeypatch.setattr(settings, "comfort_temp_max", 22.0)
    hours = MINIMUM_CONTROL_HOURS
    assert hours == 6
    captured = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).replace(microsecond=0)
    start = captured.replace(minute=0, second=0) + dt.timedelta(hours=1)
    fetched, issued = captured - dt.timedelta(minutes=10), captured - dt.timedelta(hours=1)
    price_values = [-0.2, 0.3, 0.1, -0.2, 0.3, 0.1]
    batch = PlanningInputBatch(
        captured,
        start,
        start + dt.timedelta(hours=hours),
        "manual",
        "manual",
        "manual",
        tuple(
            PriceInput(
                start + dt.timedelta(hours=hour),
                price_values[hour],
                "SEK",
                "manual",
                fetched,
                "hourly_duration_weighted_v1",
            )
            for hour in range(hours)
        ),
        tuple(
            WeatherInput(
                start + dt.timedelta(hours=hour),
                5.0,
                2.0,
                50.0,
                0.0,
                80.0,
                40.0,
                "manual",
                issued,
                "weather_c_mps_wm2_mm_v1",
            )
            for hour in range(hours)
        ),
    )
    quality = planning_batch_quality(batch)
    assert quality["control_allowed"] is True, quality["reasons"]
    context = {
        "device_id": "offline-device",
        "observed_at": (captured - dt.timedelta(minutes=1)).isoformat(),
        "captured_at": captured.isoformat(),
        "max_age_seconds": 900,
        "tank_target_c": 55.0,
        "force_dhw": 0,
        "tank_hardware_floor_c": 40,
        "auxiliary_power_kw": 0,
    }
    declared = trace(hours)
    declared["observation_context"]["captured_at"] = captured.isoformat()
    for hour, row in enumerate(declared["hours"]):
        row["ts"] = (start + dt.timedelta(hours=hour)).isoformat()
        row["native_sh_fraction"] = 0
    declare_native(declared)
    context["native_sh_provenance"] = owner_context(declared)["native_sh_provenance"]
    last_hour = {"hours": [{"ts": (start + dt.timedelta(hours=hours - 1)).isoformat()}]}
    plan = represented_strategy.build_represented_plan(
        prices=[(row.ts, row.value) for row in batch.prices],
        weather=[(row.ts, row.temperature) for row in batch.weather],
        cop_fn=lambda *a: 3,
        demand_per_hour=[0] * hours,
        current_tank_temp=48,
        current_indoor_temp=20,
        gate_states=["BLOCKED"] * hours,
        hardware_tank_ceiling=55,
        context=context,
        indoor_rates=[(0.2, -0.1)] * hours,
        indoor_targets=[19] * hours,
        tank_min_per_hour=[48] * hours,
        # The initial pulse is retained; the linked final pulse keeps the tank floor.
        serialized_actions=pulse({"hours": [{"ts": start.isoformat()}]}, 0, 30)
        + pulse(last_hour, 0, 10),
    )
    plan["planning_data_quality"] = quality
    return plan, batch


class _PublicationSession:
    def __init__(self, main, *, flush_error=None, commit_error=None):
        self.main, self.flush_error, self.commit_error = main, flush_error, commit_error
        self.records, self.committed, self.events, self.opened = [], [], [], 0

    def add(self, record):
        record.id = 321
        self.records.append(record)

    async def flush(self):
        self.events.append(("flush", self.main._last_plan_generated_at))
        if self.flush_error:
            raise self.flush_error

    def factory(self):
        @asynccontextmanager
        async def sessions(**kwargs):
            self.opened += 1
            try:
                yield self
            except BaseException:
                self.events.append(("rollback", self.main._last_plan_generated_at))
                raise
            self.events.append(("commit", self.main._last_plan_generated_at))
            if self.commit_error:
                raise self.commit_error
            self.committed.extend(self.records)

        return sessions


_COOLDOWN_SENTINEL = -123456.0


def _publication_harness(monkeypatch, plan, session):
    from packages.optimizer import main
    from packages.optimizer.executor_core import LearningModeState

    monkeypatch.setattr(main, "_last_plan_generated_at", _COOLDOWN_SENTINEL)
    monkeypatch.setattr(main, "get_session", session.factory())
    monkeypatch.setattr(main, "get_setting", AsyncMock(return_value="rules_only"))
    monkeypatch.setattr(
        main, "resolve_learning_mode_state", AsyncMock(return_value=LearningModeState.ACTIVE)
    )
    monkeypatch.setattr(
        main,
        "_select_optimizer",
        AsyncMock(
            return_value=("rules", SimpleNamespace(generate_plan=AsyncMock(return_value=plan)))
        ),
    )
    monkeypatch.setattr(main, "get_device_data_quality", AsyncMock(return_value={"ready": True}))
    monkeypatch.setattr(main._demand_model, "arefresh_committed", AsyncMock())
    monkeypatch.setattr(main.comfort_model, "arefresh_if_changed", AsyncMock())
    activate = AsyncMock(side_effect=AssertionError("Diagnostic must never activate"))
    monkeypatch.setattr("packages.core.plan_lifecycle.activate_plan", activate)
    return main, activate


async def test_S4_QR3_product_publication_preserves_priced_diagnostic(monkeypatch):
    from packages.optimizer import main as optimizer_main

    plan, batch = _publication_plan(monkeypatch)
    session = _PublicationSession(optimizer_main)
    main, activate = _publication_harness(monkeypatch, plan, session)
    assert await main.run_optimization() == 321
    assert session.opened == 1
    assert session.events == [
        ("flush", _COOLDOWN_SENTINEL),
        ("commit", _COOLDOWN_SENTINEL),
    ]
    assert main._last_plan_generated_at != _COOLDOWN_SENTINEL
    records = session.committed
    assert len(records) == 1
    record = records[0]
    saved = json.loads(record.plan_json)
    provenance = json.loads(record.input_provenance_json)
    assert record.status == "diagnostic" and record.status != "active"
    assert saved["actions"] == [] and saved["diagnostic"] is True
    assert saved["diagnostic_strategy_actions"] and record.price_currency == "SEK"
    assert provenance["planning_horizon"]["hours"] == 6
    assert provenance["price_risk"]["status"] == "published_prices"
    assert provenance["price_risk"]["hours"] == 6
    assert provenance["input_quality"]["horizon_hours"] == 6
    assert provenance["input_quality"]["effective_horizon_hours"] == 6
    assert provenance["input_quality"]["horizon_start"] == batch.start.isoformat()
    assert record.horizon_start == batch.start and record.horizon_end == batch.end
    assert len(saved["represented_strategy"]["hours"]) == 6
    assert [row["ts"] for row in saved["forecast_snapshot"]["price_forecast"]] == [
        row.ts.isoformat() for row in batch.prices
    ]
    assert saved["represented_strategy"]["price_currency"] == "SEK"
    assert saved["serialized_replay"]["price_currency"] == "SEK"
    assert saved["cost_accounting"]["price_currency"] == "SEK"
    from packages.optimizer.represented_strategy import validate_represented_plan

    assert validate_represented_plan(saved)["price_currency"] == "SEK"
    assert saved["forecast_snapshot"]["price_forecast"][0]["price_per_kwh"] == -0.2
    assert saved["forecast_snapshot"]["forecast_no_heating"][0][
        "predicted_indoor_temp"
    ] == pytest.approx(19.9)
    activate.assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["missing_status", "nonliteral_true", "horizon_mismatch", "context_mismatch"]
)
async def test_S4_QR3_invalid_publication_quality_opens_no_session(monkeypatch, case):
    from packages.optimizer import main as optimizer_main

    plan, _batch = _publication_plan(monkeypatch)
    quality = plan["planning_data_quality"]
    if case == "missing_status":
        del quality["status"]
    elif case == "nonliteral_true":
        quality["control_allowed"] = 1
    elif case == "horizon_mismatch":
        plan["horizon_end"] = plan["horizon_end"] + dt.timedelta(hours=1)
    else:
        quality["price"]["area"] = "SE3"
    session = _PublicationSession(optimizer_main)
    main, activate = _publication_harness(monkeypatch, plan, session)
    assert await main.run_optimization() is None
    assert session.opened == 0 and session.records == [] and session.committed == []
    assert main._last_plan_generated_at == _COOLDOWN_SENTINEL
    activate.assert_not_awaited()


@pytest.mark.parametrize("failure", ["flush", "commit"])
async def test_S4_QR3_transaction_failure_never_advances_cooldown(monkeypatch, failure):
    from packages.optimizer import main as optimizer_main

    plan, _batch = _publication_plan(monkeypatch)
    error = RuntimeError(f"offline {failure} failure")
    session = _PublicationSession(
        optimizer_main,
        flush_error=error if failure == "flush" else None,
        commit_error=error if failure == "commit" else None,
    )
    main, activate = _publication_harness(monkeypatch, plan, session)
    assert await main.run_optimization() is None
    assert session.opened == 1 and session.committed == []
    expected = "rollback" if failure == "flush" else "commit"
    assert session.events[-1] == (expected, _COOLDOWN_SENTINEL)
    assert main._last_plan_generated_at == _COOLDOWN_SENTINEL
    activate.assert_not_awaited()


def test_R28_actual_initial_tank_is_not_clamped():
    from packages.optimizer.milp import _observed_tank_temperature

    assert _observed_tank_temperature(SimpleNamespace(tank_temp=35.0)) == 35.0
    assert _observed_tank_temperature(SimpleNamespace(tank_temp=62.0)) == 62.0


def pulse(value, hour, minutes):
    instant = dt.datetime.fromisoformat(value["hours"][hour]["ts"])
    key = f"dhw:{instant.isoformat()}"
    return [
        {
            "ts": instant.isoformat(),
            "type": "force_dhw_on",
            "action_key": key,
            "payload": {"dhw_minutes": minutes},
        },
        {
            "ts": (instant + dt.timedelta(minutes=minutes)).isoformat(),
            "type": "force_dhw_off",
            "reverts_action_key": key,
            "payload": {},
        },
    ]


@pytest.mark.parametrize("case", ["negative_price", "recovery", "blocked_native", "zero_dhw"])
def test_R28_feasible_integer_solver_and_independent_replay(case):
    value = trace()
    if case == "recovery":
        value["initial_tank_c"] = 47.0
    elif case == "blocked_native":
        for row in value["hours"]:
            row["gate_state"], row["native_sh_fraction"] = "BLOCKED", 0.0
    elif case == "zero_dhw":
        value["initial_tank_c"] = 50.0
        for row in value["hours"]:
            row["price_per_kwh"] = 0.2
            row["native_sh_fraction"] = 0.0
            row["gate_state"] = "BLOCKED"
    initial = value["initial_tank_c"]
    declare_native(value)
    actions, result = solve_represented_trace(value, timeout_seconds=10)
    serialized_trace = json.loads(json.dumps(value, allow_nan=False))
    serialized = json.loads(json.dumps(actions, allow_nan=False))
    replay = replay_serialized_actions(serialized_trace, serialized)
    assert replay == result
    assert result["states"][0]["tank_c"] == initial
    assert result["initial_violations"]["tank_below_floor"] is (case == "recovery")
    assert result["assurance"] == "modeled_conditional_not_physical_or_global_optimality"
    assert all(action["type"] in {"force_dhw_on", "force_dhw_off"} for action in actions)
    assert all(
        type(action["payload"]["dhw_minutes"]) is int
        and 5 <= action["payload"]["dhw_minutes"] <= 60
        for action in actions
        if action["type"] == "force_dhw_on"
    )
    assert all(
        state["space_heating_fraction"] == 0.0
        for state in replay["states"][1:]
        if state["dhw_active"]
    )
    if case == "zero_dhw":
        assert actions == []
    else:
        assert actions


@pytest.mark.parametrize(
    "case", ["unrecoverable_tank", "upper_indoor", "lower_indoor", "pulse_ceiling"]
)
def test_R28_true_infeasible_controls(case):
    value = trace(1)
    if case == "unrecoverable_tank":
        value["initial_tank_c"] = 20.0
    elif case == "upper_indoor":
        value["initial_indoor_c"] = 30.0
    elif case == "lower_indoor":
        value["initial_indoor_c"] = 10.0
    else:
        value["initial_tank_c"] = 55.0
        value["hours"][0]["tank_floor_c"] = 55.0
    with pytest.raises(InfeasibleError):
        solve_represented_trace(value, timeout_seconds=10)


@pytest.mark.parametrize(
    "case", ["unknown_gate", "missing_gate", "gap", "nan_price", "bad_deadline", "wrong_schema"]
)
def test_R28_incomplete_or_unknown_trace_fails_closed(case):
    value = trace()
    if case == "unknown_gate":
        value["hours"][1]["gate_state"] = "UNKNOWN"
    elif case == "missing_gate":
        value["hours"][1].pop("gate_state")
    elif case == "gap":
        value["hours"].pop(1)
    elif case == "nan_price":
        value["hours"][0]["price_per_kwh"] = float("nan")
    elif case == "bad_deadline":
        value["deadline_hours"] = [4]
    else:
        value["schema"] = "guessed_target_duty"
    with pytest.raises(DataIncompleteError):
        solve_represented_trace(value, timeout_seconds=10)


@pytest.mark.parametrize(
    "case", ["fractional_runtime", "wrong_link", "unrestored", "quiet", "ceiling_extremum"]
)
def test_R28_serialized_negative_controls(case):
    value = trace(1)
    actions = pulse(value, 0, 10)
    if case == "fractional_runtime":
        actions[1]["ts"] = (
            dt.datetime.fromisoformat(actions[1]["ts"]) + dt.timedelta(seconds=1)
        ).isoformat()
    elif case == "wrong_link":
        actions[1]["reverts_action_key"] = "foreign"
    elif case == "unrestored":
        actions.pop()
    elif case == "quiet":
        actions[0]["type"] = "quiet_mode_on"
    else:
        value["initial_tank_c"] = 54.9
        actions = pulse(value, 0, 5)
        # Endpoint is below 55; the earlier pulse peak exceeds 55.
        assert 54.9 + 5 * (5 / 60) - 0.5 * (55 / 60) < 55
        assert 54.9 + 5 * (5 / 60) > 55
        # The observed 55 C target suppresses the command even before the unsafe peak.
        with pytest.raises(DataIncompleteError, match="suppressed at observed tank target"):
            replay_serialized_actions(value, actions)
        return
    with pytest.raises(InfeasibleError):
        replay_serialized_actions(value, actions)


def test_R15_replay_prices_elapsed_electrical_energy_once_with_diversion():
    value = trace(1)
    value["hours"][0]["price_per_kwh"] = -0.2
    actions = pulse(value, 0, 30)
    replay = replay_serialized_actions(value, actions)
    assert replay["electricity_kwh"] == pytest.approx(1.5)  # 1kW DHW * .5h + 2kW SH * .5h
    assert replay["cost"] == pytest.approx(-0.3)
    assert replay["states"][-1]["tank_c"] == pytest.approx(50.25)
    assert replay["states"][-1]["indoor_c"] == pytest.approx(19.975)
    assert replay["price_currency"] == "SEK"
    assert value["initial_tank_c"] == 48


def test_R15_rules_long_integer_pulse_replays_across_price_boundaries():
    from packages.optimizer.represented_strategy import represented_rules_commands

    value = trace(2)
    commands = pulse(value, 0, 70)
    commands[0]["payload"] = {"predicted_minutes": 65}
    normalized = represented_rules_commands(commands)
    assert normalized[0]["payload"]["dhw_minutes"] == 70
    assert normalized[0]["ts"] == commands[0]["ts"]
    assert normalized[1]["ts"] == commands[1]["ts"]
    assert "dhw_minutes" not in commands[0]["payload"]
    result = replay_serialized_actions(value, normalized)
    assert result["states"][-1]["tank_c"] == pytest.approx(48 + 5 * (70 / 60) - 0.5 * (50 / 60))
    assert result["cost"] == pytest.approx(-0.2 + 0.3 * (1 / 6 + 2 * 5 / 6))
    assert result["states"][1]["space_heating_fraction"] == 0.0


def test_R28_MILP_owner_preserves_actual_states_and_only_represented_commands(monkeypatch):
    from packages.optimizer import milp

    value = trace()
    prices = [
        (dt.datetime.fromisoformat(row["ts"]), row["price_per_kwh"]) for row in value["hours"]
    ]
    monkeypatch.setattr(milp.thermal_model.params, "last_calibrated", None)
    plan = milp.MILPOptimizer()._solve(
        prices,
        [(instant, 5.0) for instant, _ in prices],
        lambda *args: 3.0,
        [milp.settings.sh_max_power_kw * 0.5] * 3,
        47.0,
        20.0,
        [3],
        [19.0] * 3,
        tank_min_per_hour=[48] * 3,
        indoor_rates=[(0.2, -0.1)] * 3,
        gate_states=["ALLOWED"] * 3,
        indoor_max_temp_setting=22,
        hardware_tank_ceiling=55,
        observation_context=owner_context(value),
        dhw_deadline_tank_floors_c=[48],
    )
    assert plan["diagnostic_status"] == "conditional_feasible"
    assert plan["represented_strategy"]["initial_tank_c"] == 47.0
    assert plan["serialized_replay"]["initial_violations"]["tank_below_floor"] is True
    assert plan["cost_estimate"] == plan["serialized_replay"]["cost"]
    assert plan["diagnostic_strategy_actions"] == plan["actions"]
    assert {action["type"] for action in plan["actions"]} == {"force_dhw_on", "force_dhw_off"}
    snapshot = plan["forecast_snapshot"]
    assert len(snapshot["price_forecast"]) == len(snapshot["weather_forecast"]) == 3
    assert [row["price_eur_per_kwh"] for row in snapshot["price_forecast"]] == [
        price for _, price in prices
    ]
    assert snapshot["forecast_no_heating"][-1]["predicted_indoor_temp"] == pytest.approx(19.7)
    assert snapshot["no_heating_basis"] == "fully_passive_no_SH_no_DHW"
    assert len(snapshot["target_schedule"]) == 3
    assert [row["ts"] for row in snapshot["target_schedule"]] == [
        row["ts"] for row in snapshot["forecast_with_plan"]
    ]
    assert [row["ts"] for row in snapshot["weather_forecast"]] == [
        row["ts"] for row in snapshot["forecast_with_plan"]
    ]


def test_R15_rules_replay_is_independent_of_solver_and_unsupported_actions(monkeypatch):
    from packages.optimizer import milp
    from packages.optimizer import represented_strategy

    value = trace(1)
    prices = [(dt.datetime.fromisoformat(value["hours"][0]["ts"]), 0.1)]
    actions = pulse(value, 0, 30)
    monkeypatch.setattr(milp.thermal_model.params, "last_calibrated", None)

    def forbidden_solver(*args, **kwargs):
        raise AssertionError("Rules fallback must replay actual commands without solving")

    monkeypatch.setattr(represented_strategy, "solve_represented_trace", forbidden_solver)
    inputs = dict(
        prices=prices,
        weather=[(prices[0][0], 5.0)],
        cop_fn=lambda *args: 3.0,
        demand_per_hour=[milp.settings.sh_max_power_kw * 0.5],
        current_tank_temp=48.0,
        current_indoor_temp=20.0,
        tank_min_per_hour=[48],
        indoor_targets=[19.0],
        indoor_rates=[(0.2, -0.1)],
        gate_states=["ALLOWED"],
        hardware_tank_ceiling=55,
        serialized_actions=actions,
        observation_context=owner_context(value),
    )
    result = milp.MILPOptimizer()._solve(**inputs)
    assert result["diagnostic_status"] == "conditional_feasible"
    assert result["actions"] == actions
    assert result["cost_estimate"] == pytest.approx(
        0.1 * (milp.settings.sh_max_power_kw * 0.25 + 5.5 * milp.settings.tank_kwh_per_degree / 6)
    )
    bad = {
        **inputs,
        "serialized_actions": [{"type": "eco_mode_on", "ts": prices[0][0].isoformat()}],
    }
    with pytest.raises(InfeasibleError, match="unproven"):
        milp.MILPOptimizer()._solve(**bad)


@pytest.mark.parametrize(
    "case,message",
    [
        ("missing_context", "observed DHW context"),
        ("missing_gate", "Incomplete physical, gate or forecast horizon"),
        ("unknown_gate", "Missing or unknown native SH gate"),
        ("missing_hardware", "finite numeric inputs"),
        ("invalid_power", "finite numeric inputs"),
    ],
)
def test_R28_owner_rejects_incomplete_physical_evidence(case, message, monkeypatch):
    from packages.optimizer import milp
    from packages.optimizer import represented_strategy

    value = trace(1)
    prices = [(dt.datetime.fromisoformat(value["hours"][0]["ts"]), 0.1)]
    monkeypatch.setattr(milp.thermal_model.params, "last_calibrated", None)

    def forbidden_solver(*args, **kwargs):
        raise AssertionError("Physical evidence checks must not reach the solver")

    monkeypatch.setattr(represented_strategy, "solve_represented_trace", forbidden_solver)
    valid = dict(
        prices=prices,
        weather=[(prices[0][0], 5.0)],
        cop_fn=lambda *args: 3.0,
        demand_per_hour=[milp.settings.sh_max_power_kw * 0.5],
        current_tank_temp=48.0,
        current_indoor_temp=20.0,
        tank_min_per_hour=[48],
        indoor_targets=[19.0],
        indoor_rates=[(0.2, -0.1)],
        gate_states=["ALLOWED"],
        hardware_tank_ceiling=55,
        serialized_actions=pulse(value, 0, 30),
        observation_context=owner_context(value),
    )
    assert milp.MILPOptimizer()._solve(**valid)["diagnostic_status"] == "conditional_feasible"
    mutation = {
        "missing_context": ("observation_context", None),
        "missing_gate": ("gate_states", []),
        "unknown_gate": ("gate_states", ["UNKNOWN"]),
        "missing_hardware": ("hardware_tank_ceiling", None),
        "invalid_power": ("demand_per_hour", [float("nan")]),
    }[case]
    inputs = {**valid, mutation[0]: mutation[1]}
    with pytest.raises(DataIncompleteError, match=message):
        milp.MILPOptimizer()._solve(**inputs)


async def test_R23_R35_cleanup_orders_owned_drain_logs_before_wrapper(monkeypatch):
    from packages.core import runtime_ownership

    calls = []

    async def logs():
        calls.append("logs")

    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", logs)

    async def scheduler_close():
        calls.append("scheduler")

    async def maintenance_close():
        calls.append("maintenance")

    async def stop():
        calls.append("wrapper")

    await runtime_ownership.close_service_resources(
        scheduler=SimpleNamespace(close=scheduler_close),
        maintenance=SimpleNamespace(close=maintenance_close),
        wrapper=SimpleNamespace(stop=stop),
    )
    assert calls == ["scheduler", "maintenance", "logs", "wrapper"]


@pytest.mark.parametrize("failed_owner", ["scheduler", "maintenance", "logs"])
async def test_R23_R35_unsettled_owner_blocks_resource_disposal(monkeypatch, failed_owner):
    from packages.core import runtime_ownership

    async def close(name):
        if name == failed_owner:
            raise RuntimeError("unsettled owner")

    async def scheduler_close():
        await close("scheduler")

    async def maintenance_close():
        await close("maintenance")

    async def logs():
        await close("logs")

    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", logs)
    wrapper = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(RuntimeError, match="unsettled"):
        await runtime_ownership.close_service_resources(
            scheduler=SimpleNamespace(close=scheduler_close),
            maintenance=SimpleNamespace(close=maintenance_close),
            wrapper=wrapper,
        )
    wrapper.stop.assert_not_awaited()


@pytest.mark.parametrize("service", ["optimizer", "poller"])
@pytest.mark.parametrize("failure", ["startup_error", "startup_cancel"])
async def test_R23_service_first_await_failure_owns_cleanup(monkeypatch, service, failure):
    import asyncio
    import importlib
    from packages.core import runtime_ownership

    module = importlib.import_module(f"packages.{service}.main")
    calls = []

    async def fail(*args):
        calls.append("first_await")
        if failure == "startup_cancel":
            raise asyncio.CancelledError
        raise RuntimeError("startup")

    async def stop():
        calls.append("wrapper")

    async def close_scheduler():
        calls.append("scheduler")

    async def close_maintenance():
        calls.append("maintenance")

    async def logs():
        calls.append("logs")

    async def dispose():
        calls.append("engine")

    async def immediate_cleanup(cleanup):
        # No workers exist in this fake startup case; this does not test cancellation shielding.
        await cleanup()

    wrapper = SimpleNamespace(stop=stop)
    monkeypatch.setattr(module, "AquareaWrapper", lambda **kwargs: wrapper)
    monkeypatch.setattr(module, "create_scheduler", lambda: SimpleNamespace(close=close_scheduler))
    monkeypatch.setattr(module, "_run_service", fail)
    monkeypatch.setattr(runtime_ownership, "finish_owned_cleanup", immediate_cleanup)
    monkeypatch.setattr("packages.core.log_sink.shutdown_log_sink", logs)
    monkeypatch.setattr("packages.core.database.engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(
        "packages.ml.lifecycle.MaintenanceRunner",
        lambda **kwargs: SimpleNamespace(close=close_maintenance),
    )
    expected = asyncio.CancelledError if failure == "startup_cancel" else RuntimeError
    with pytest.raises(expected):
        await module.main()
    assert calls == [
        "first_await",
        "scheduler",
        *(("maintenance",) if service == "poller" else ()),
        "logs",
        "wrapper",
        "engine",
    ]


async def test_R23_wrapper_http_close_failure_still_closes_owned_redis():
    from packages.core.services.aquarea import AquareaWrapper

    wrapper = AquareaWrapper(read_only=True)
    wrapper._session = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failure")))
    wrapper._redis = SimpleNamespace(aclose=AsyncMock())
    redis = wrapper._redis
    with pytest.raises(RuntimeError, match="close failure"):
        await wrapper.stop()
    redis.aclose.assert_awaited_once()
    assert wrapper._session is not None  # Retain ownership when completion is not proven.


async def test_R35_empty_shutdown_closes_admission_without_database_IO(monkeypatch):
    from packages.core import log_sink

    monkeypatch.setattr(log_sink, "_LOG_BUFFER", [])
    monkeypatch.setattr(log_sink, "_INFLIGHT_TASK", None)
    monkeypatch.setattr(log_sink, "_FLUSH_TASK", None)
    monkeypatch.setattr(log_sink, "_CLOSING", False)
    await log_sink.shutdown_log_sink()
    assert log_sink.log_sink_status()["accepting"] is False
    assert log_sink.log_sink_status()["owned_rows"] == 0


def test_R23_R35_entrypoints_protect_startup_and_schedule_all_async_jobs():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[1] / "packages"
    for name in ("optimizer", "poller"):
        tree = ast.parse((root / name / "main.py").read_text(encoding="utf-8"))
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
        )
        owner = next(node for node in main.body if isinstance(node, ast.Try))
        assert any(
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "_run_service"
            for node in ast.walk(ast.Module(body=owner.body, type_ignores=[]))
        )
        assert any(
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "finish_owned_cleanup"
            for node in ast.walk(ast.Module(body=owner.finalbody, type_ignores=[]))
        )
    api = ast.parse((root / "api" / "main.py").read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in api.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    owner = next(node for node in lifespan.body if isinstance(node, ast.Try))
    assert any(isinstance(node, ast.Yield) for node in ast.walk(owner))
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "finish_owned_cleanup"
        for node in ast.walk(ast.Module(body=owner.finalbody, type_ignores=[]))
    )


def _flat_dhw_trace(prices):
    value = trace(len(prices))
    value["deadline_hours"] = []
    value["deadline_tank_floors_c"] = []
    value["initial_tank_c"] = 46.0
    for row, price in zip(value["hours"], prices):
        row.update(
            price_per_kwh=price,
            gate_state="BLOCKED",
            native_sh_fraction=0.0,
            tank_net_heating_c_per_h=6.0,
            tank_loss_c_per_h=-1.0,
            tank_floor_c=45.0,
        )
    return declare_native(value)


def _enumerate_schedules(value):
    """Independent brute-force of every integer runtime schedule (blocked native SH)."""
    from itertools import product

    from packages.optimizer.represented_strategy import TOLERANCE

    slack = 1e-9
    rows = value["hours"]
    target = value["observed_tank_target_c"] - 0.5 - TOLERANCE
    feasible = []
    for minutes in product([0, *range(5, 61)], repeat=len(rows)):
        tank, indoor, cost, valid = value["initial_tank_c"], value["initial_indoor_c"], 0.0, True
        for runtime, row in zip(minutes, rows):
            fraction = runtime / 60
            peak = tank + row["tank_net_heating_c_per_h"] * fraction
            end = peak + row["tank_loss_c_per_h"] * (1 - fraction)
            room = indoor + row["indoor_passive_c_per_h"]
            pulse_room = indoor + row["indoor_passive_c_per_h"] * fraction
            if (
                max(peak, end) > value["tank_ceiling_c"] + slack
                or end < row["tank_floor_c"] - slack
                or not row["indoor_floor_c"] - slack <= room <= value["indoor_ceiling_c"] + slack
                or (
                    runtime
                    and (
                        max(tank, peak) > target + slack
                        or not row["indoor_floor_c"] - slack
                        <= pulse_room
                        <= value["indoor_ceiling_c"] + slack
                    )
                )
            ):
                valid = False
                break
            cost += row["price_per_kwh"] * fraction * row["dhw_power_kw"]
            tank, indoor = end, room
        if valid:
            feasible.append((minutes, cost))
    assert feasible
    return feasible


def _recorded_solver(monkeypatch):
    import pulp

    calls, real = [], pulp.PULP_CBC_CMD

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pulp, "PULP_CBC_CMD", recording)
    return calls


@pytest.mark.parametrize("price", [0.1, -0.2, 0.0])
def test_B1_equal_monetary_coefficients_prefer_fewer_pulses_without_cost(monkeypatch, price):
    calls = _recorded_solver(monkeypatch)
    value = _flat_dhw_trace([price] * 3)
    actions, replay = solve_represented_trace(value, timeout_seconds=7)
    assert len(calls) == 1 and calls[0]["timeLimit"] == 7
    runtimes = [item["payload"]["dhw_minutes"] for item in actions if "action_key" in item]
    feasible = _enumerate_schedules(value)
    best_cost = min(cost for _minutes, cost in feasible)
    cost_optimal = [minutes for minutes, cost in feasible if cost <= best_cost + 1e-9]
    if price:
        totals = {sum(minutes) for minutes in cost_optimal}
        assert len(totals) == 1 and sum(runtimes) == totals.pop()
    fewest = min(sum(1 for runtime in minutes if runtime) for minutes in cost_optimal)
    assert len(runtimes) == fewest
    assert replay["cost"] == pytest.approx(best_cost, abs=1e-6)


def test_B1_unequal_coefficients_keep_monetary_optimum(monkeypatch):
    calls = _recorded_solver(monkeypatch)
    value = _flat_dhw_trace([0.1, 0.3, 0.2])
    _actions, replay = solve_represented_trace(value, timeout_seconds=5)
    assert len(calls) == 1 and calls[0]["timeLimit"] == 5
    best_cost = min(cost for _minutes, cost in _enumerate_schedules(value))
    assert replay["cost"] == pytest.approx(best_cost, abs=1e-6)


def test_B1_objective_specialization_is_exact_equality_only():
    import math

    import pulp

    from packages.optimizer.represented_strategy import _represented_objective

    minutes = [pulp.LpVariable(f"m{hour}", 0, 60, cat="Integer") for hour in range(2)]
    active = [pulp.LpVariable(f"a{hour}", cat="Binary") for hour in range(2)]

    def monetary(prices):
        cost = 0
        for runtime, price in zip(minutes, prices):
            cost += price * (runtime / 60 * 1.5)
        return cost

    for prices in ([0.1, 0.3], [0.1, 0.1 + 1e-12]):
        cost = monetary(prices)
        coefficients = [cost.get(item) for item in minutes]
        assert coefficients[0] != coefficients[1]
        assert math.isclose(*coefficients, rel_tol=1e-9) is (prices[1] != 0.3)
        assert _represented_objective(cost, minutes, active) is cost
    # One runtime minute weighs M + 1 = 3 and outranks any activation difference (<= M = 2).
    for price, weight in ((0.1, 3), (-0.2, -3)):
        objective = _represented_objective(monetary([price, price]), minutes, active)
        assert [objective.get(item) for item in minutes] == [weight, weight]
        assert [objective.get(item) for item in active] == [1, 1]
    objective = _represented_objective(monetary([0.0, 0.0]), minutes, active)
    assert [objective.get(item, 0) for item in minutes] == [0, 0]
    assert [objective.get(item) for item in active] == [1, 1]

"""S4 admission regressions and independently calculated physical/cost controls."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.optimizer import DataIncompleteError, InfeasibleError
from packages.optimizer.represented_strategy import (
    observation_context,
    replay_serialized_actions,
    solve_represented_trace,
    validate_represented_plan,
)
from tests.test_milp import _Case
from tests.test_remaining_scope import (
    _PublicationSession,
    _publication_harness,
    _publication_plan,
    pulse,
    trace,
    declare_native,
)


@pytest.mark.parametrize("basis", ["assumed", "history"])
def test_bare_native_basis_is_not_provenance(basis):
    value = trace(1)
    value["initial_tank_c"] = 49
    value["native_sh_basis"] = basis
    value.pop("native_sh_provenance")
    with pytest.raises(DataIncompleteError, match="provenance"):
        replay_serialized_actions(value, [])


@pytest.mark.parametrize("consumer", ["solve", "replay"])
@pytest.mark.parametrize(
    "fault",
    [
        "history",
        "missing",
        "method",
        "device",
        "clock",
        "start",
        "end",
        "short_load",
        "nonfinite_load",
        "negative_load",
        "wrong_load",
        "wrong_power",
    ],
)
def test_provenance_bound_to_explicit_load_device_clock_and_horizon(consumer, fault):
    value = trace(1)
    provenance = value["native_sh_provenance"]
    if fault == "history":
        value["native_sh_basis"] = "history"
    elif fault == "missing":
        value.pop("native_sh_provenance")
    elif fault == "method":
        provenance["method"] = "invented_target_to_duty"
    elif fault == "device":
        provenance["device_id"] = "another-device"
    elif fault in {"clock", "start", "end"}:
        field = {"clock": "captured_at", "start": "horizon_start", "end": "horizon_end"}[fault]
        provenance[field] = (
            dt.datetime.fromisoformat(provenance[field]) + dt.timedelta(hours=1)
        ).isoformat()
    elif fault == "short_load":
        provenance["hourly_electrical_kw"] = []
    elif fault == "wrong_power":
        provenance["maximum_electrical_power_kw"] = 5
    else:
        provenance["hourly_electrical_kw"] = [
            {
                "nonfinite_load": float("nan"),
                "negative_load": -1,
                "wrong_load": 3,
            }[fault]
        ]
    with pytest.raises(DataIncompleteError):
        if consumer == "solve":
            solve_represented_trace(value, timeout_seconds=5)
        else:
            replay_serialized_actions(value, pulse(value, 0, 30))


@pytest.mark.parametrize("auxiliary", [None, False, True, -1, 0.01, 1, "0", float("nan")])
@pytest.mark.parametrize("consumer", ["solve", "replay"])
def test_only_explicit_numeric_zero_auxiliary_is_supported(auxiliary, consumer):
    value = trace(1)
    value["auxiliary_power_kw"] = auxiliary
    with pytest.raises(DataIncompleteError):
        if consumer == "solve":
            solve_represented_trace(value, timeout_seconds=5)
        else:
            replay_serialized_actions(value, pulse(value, 0, 30))


@pytest.mark.parametrize("minutes", [0, 5, 30, 60, 70, 120])
def test_R15_R28f_json_commands_exact_independent_two_tariff_oracle(minutes):
    value = trace(2)
    value.update(initial_tank_c=49, observed_tank_target_c=80, tank_ceiling_c=70)
    value["hours"][0]["price_per_kwh"] = 0.2
    value["hours"][1]["price_per_kwh"] = -0.4
    actions = pulse(value, 0, minutes) if minutes else []
    result = replay_serialized_actions(
        json.loads(json.dumps(value)), json.loads(json.dumps(actions))
    )
    on_hours = minutes / 60
    # Constants are independent of the replay/model implementation:
    # 1 kW DHW or 0.5 * 4 kW native SH; never both.
    first_on, second_on = min(1, on_hours), max(0, on_hours - 1)
    first_energy = first_on + 2 * (1 - first_on)
    second_energy = second_on + 2 * (1 - second_on)
    assert result["electricity_kwh"] == pytest.approx(first_energy + second_energy)
    assert result["cost"] == pytest.approx(0.2 * first_energy - 0.4 * second_energy)
    assert result["states"][-1]["tank_c"] == pytest.approx(49 + 5 * on_hours - 0.5 * (2 - on_hours))
    assert result["states"][-1]["indoor_c"] == pytest.approx(
        20 - 0.1 * 2 + 0.5 * 0.3 * (2 - on_hours)
    )
    assert result["states"][0]["tank_c"] == 49
    assert result["states"][-1]["ts"] == value["native_sh_provenance"]["horizon_end"]
    assert all(
        state["space_heating_fraction"] == 0
        for state in result["states"][1:]
        if state["dhw_active"]
    )
    assert result["price_currency"] == "SEK"


def test_R28_hardware_floor_overrides_lower_operational_floor_without_changing_it():
    case = _Case([0.2], tank=44, tank_floors=[30])
    context = case.solve()["represented_strategy"]["observation_context"]
    # Supply an explicit hardware floor through the real MILP producer context.
    from tests.test_milp import _context

    owner = _context(case.price_rows, demand=case.demand, tank_hardware_floor_c=45)
    plan = case.solve(observation_context=owner)
    assert context["device_id"] == owner["device_id"]
    assert plan["represented_strategy"]["initial_tank_c"] == 44
    assert plan["represented_strategy"]["hours"][0]["tank_floor_c"] == 45
    assert case.tank_floors == [30]
    # 44 + 5*m/60 - .5*(1-m/60) >= 45 => m >= 16.3636, hence 17 minutes.
    assert plan["actions"][0]["payload"]["dhw_minutes"] == 17
    assert plan["serialized_replay"]["states"][-1]["tank_c"] == pytest.approx(
        44 + 5 * 17 / 60 - 0.5 * 43 / 60
    )
    owner["tank_hardware_floor_c"] = 56
    with pytest.raises(DataIncompleteError, match="physical bounds"):
        case.solve(observation_context=owner)


@pytest.mark.parametrize(
    "room,passive,violation",
    [(17.5, 1.0, "indoor_below_floor"), (24.5, -1, "indoor_above_ceiling")],
)
def test_R27_R28_actual_initial_room_recovery_is_reported_never_clamped(room, passive, violation):
    case = _Case([0.2], room=room, rates=[(1, passive)], room_floors=[18], room_ceiling=24)
    plan = case.solve()
    assert plan["serialized_replay"]["states"][0]["indoor_c"] == room
    assert plan["initial_violations"][violation] is True
    assert plan["forecast_snapshot"]["forecast_with_plan"][0][
        "predicted_indoor_temp"
    ] == pytest.approx(room + passive)
    case.rates = [(0, 0)]
    with pytest.raises(InfeasibleError):
        case.solve()


@pytest.mark.parametrize("fault", ["missing_auxiliary", "nonzero_auxiliary", "missing_native"])
async def test_real_observation_context_does_not_invent_assumptions(monkeypatch, fault):
    value = trace(1)
    start = dt.datetime.fromisoformat(value["hours"][0]["ts"])
    declarations = {"native_sh_provenance": value["native_sh_provenance"], "auxiliary_power_kw": 0}
    if fault == "missing_auxiliary":
        declarations.pop("auxiliary_power_kw")
    elif fault == "nonzero_auxiliary":
        declarations["auxiliary_power_kw"] = 1
    else:
        declarations.pop("native_sh_provenance")
    status = SimpleNamespace(
        device_id=value["device_id"],
        ts=start,
        tank_target_temp=55,
        force_dhw=0,
        tank_heat_min=40,
        native_policy_provenance={"diagnostic_assumptions": declarations},
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
    context = await observation_context(status, start)
    assert context["auxiliary_power_kw"] == declarations.get("auxiliary_power_kw")
    assert context["native_sh_provenance"] == declarations.get("native_sh_provenance")
    from tests.test_milp import _context

    case = _Case([0.2])
    owner = _context(case.price_rows)
    owner.update({key: context[key] for key in ("auxiliary_power_kw", "native_sh_provenance")})
    with pytest.raises(DataIncompleteError):
        case.solve(observation_context=owner)


@pytest.mark.parametrize(
    "fault",
    [
        "cost",
        "energy",
        "actions",
        "stored_replay",
        "forecast",
        "horizon",
        "native",
        "auxiliary",
        "snapshot_trace",
        "passive_counterfactual",
        "price_value",
        "price_timestamp",
        "currency",
    ],
)
async def test_R15_real_publication_refuses_inconsistent_serialized_DTO_before_write(
    monkeypatch, fault
):
    from packages.optimizer import main as optimizer_main

    plan, _ = _publication_plan(monkeypatch)
    if fault == "cost":
        plan["cost_estimate"] += 1
    elif fault == "energy":
        plan["cost_accounting"]["electrical_kwh"] += 1
    elif fault == "actions":
        plan["diagnostic_strategy_actions"][0]["payload"]["dhw_minutes"] += 1
    elif fault == "stored_replay":
        plan["serialized_replay"]["states"][-1]["tank_c"] += 1
    elif fault == "forecast":
        plan["forecast_snapshot"]["forecast_with_plan"][0]["predicted_indoor_temp"] += 1
    elif fault == "horizon":
        plan["horizon_end"] += dt.timedelta(hours=1)
    elif fault == "native":
        plan["represented_strategy"]["native_sh_basis"] = "history"
    elif fault == "auxiliary":
        plan["represented_strategy"]["auxiliary_power_kw"] = 1
    elif fault == "passive_counterfactual":
        plan["forecast_snapshot"]["forecast_no_heating"][0]["predicted_indoor_temp"] += 1
    elif fault == "price_value":
        plan["forecast_snapshot"]["price_forecast"][0]["price_per_kwh"] += 1
    elif fault == "price_timestamp":
        plan["forecast_snapshot"]["price_forecast"][0]["ts"] = plan["horizon_end"].isoformat()
    elif fault == "currency":
        plan["cost_accounting"]["price_currency"] = "different-currency"
    else:
        plan["forecast_snapshot"]["represented_strategy"] = {}
    session = _PublicationSession(optimizer_main)
    main, activate = _publication_harness(monkeypatch, plan, session)
    assert await main.run_optimization() is None
    assert session.opened == 0 and session.committed == []
    activate.assert_not_awaited()


def test_serialized_conditional_DTO_remains_valid_after_observe_only_publication(monkeypatch):
    plan, _ = _publication_plan(monkeypatch)
    validate_represented_plan(json.loads(json.dumps(plan, default=str)))
    plan.update(actions=[], diagnostic=True)
    result = validate_represented_plan(json.loads(json.dumps(plan, default=str)))
    assert result["cost"] == plan["cost_estimate"]
    assert result["assurance"] == "modeled_conditional_not_physical_or_global_optimality"


def test_R28_passive_tank_recovery_has_no_nonexistent_pulse_peak():
    value = trace(1)
    value.update(initial_tank_c=55.2, tank_ceiling_c=55, observed_tank_target_c=60)
    value["hours"][0]["price_per_kwh"] = 0.2
    passive = replay_serialized_actions(value, [])
    assert passive["states"][-1]["tank_c"] == pytest.approx(54.7)
    actions, result = solve_represented_trace(value, timeout_seconds=5)
    assert actions == []
    assert result == passive
    assert result["initial_violations"]["tank_above_ceiling"] is True
    assert result["states"][0]["tank_c"] == 55.2


def test_R28_final_priced_deadline_is_included_by_shared_producer_extractor():
    from packages.optimizer.rule_mixins import local_dhw_deadlines_in_horizon

    start = dt.datetime(2026, 10, 12, 6, tzinfo=dt.timezone.utc)
    assert local_dhw_deadlines_in_horizon({"weekday": [6], "weekend": [6]}, start, "UTC") == [
        (start + dt.timedelta(hours=24), 6)
    ]


@pytest.mark.parametrize("initial", [55.6, 56.0])
def test_R28_unrecoverable_above_ceiling_is_not_relaxed_at_endpoint(initial):
    value = trace(1)
    value.update(initial_tank_c=initial, tank_ceiling_c=55, observed_tank_target_c=65)
    with pytest.raises(InfeasibleError):
        replay_serialized_actions(value, [])
    with pytest.raises(InfeasibleError):
        solve_represented_trace(value, timeout_seconds=5)


def test_R28_active_pulse_still_cannot_cross_ceiling_during_passive_recovery():
    value = trace(1)
    value.update(initial_tank_c=55.2, tank_ceiling_c=55, observed_tank_target_c=65)
    value["hours"][0]["dhw_power_kw"] = 4
    with pytest.raises(InfeasibleError, match="tank floor or ceiling"):
        replay_serialized_actions(value, pulse(value, 0, 5))
    # A negative tariff must not persuade the solver to use the forbidden pulse.
    actions, result = solve_represented_trace(value, timeout_seconds=5)
    assert actions == []
    assert result["states"][-1]["tank_c"] == pytest.approx(54.7)


@pytest.mark.parametrize("consumer", ["solve", "replay"])
def test_R28_endpoint_only_comfort_floor_is_independent_of_hourly_floors(consumer):
    value = trace(6)
    value.update(
        initial_tank_c=43,
        tank_hardware_floor_c=30,
        deadline_tank_floors_c=[48],
    )
    for row in value["hours"]:
        row.update(tank_floor_c=40, gate_state="BLOCKED", native_sh_fraction=0, price_per_kwh=0.2)
    declare_native(value)
    if consumer == "replay":
        with pytest.raises(InfeasibleError, match="deadline"):
            replay_serialized_actions(value, [])
    else:
        actions, result = solve_represented_trace(value, timeout_seconds=5)
        assert actions
        assert result["states"][-1]["tank_c"] >= 48 - 1e-5


def _deadline_generator_inputs(
    monkeypatch,
    producer,
    hours,
    ready,
    *,
    tank=55,
    loss=0,
    heat=5,
    dhw_minutes=None,
):
    """Real-generator setup with explicit synthetic physics and declarations."""
    from packages.core.config import settings
    from packages.core.control_temperature import ControlTemperature
    from packages.core.heat_curve import HeatCurveConfig
    from packages.ml import forecast_quality
    from packages.ml.thermal import thermal_model
    from packages.optimizer import milp, rules_engine as rules
    from packages.optimizer.executor_core import LearningModeState
    from tests.test_milp import _context

    module = rules if producer == "rules" else milp
    optimizer = rules.RulesOptimizer() if producer == "rules" else milp.MILPOptimizer()
    now = dt.datetime.now(dt.timezone.utc)
    start = (now + dt.timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    prices = [(start + dt.timedelta(hours=h), 0.2) for h in range(hours)]
    weather = [(ts, 5) for ts, _ in prices]
    context = _context(
        prices,
        demand=[0] * hours,
        power=settings.sh_max_power_kw,
        observed_at=now,
        captured_at=now,
        tank_target_c=55,
    )
    context["native_sh_provenance"]["captured_at"] = now.isoformat()
    status = SimpleNamespace(
        device_id=context["device_id"],
        ts=now,
        tank_temp=tank,
        tank_target_temp=55,
        force_dhw=0,
        tank_heat_min=30,
        tank_heat_max=55,
        holiday_mode=0,
        zone1_temp=35,
        zone1_target_temp=None,
        zone1_heat_min=None,
        zone1_heat_max=None,
        heat_pump_outdoor_temp=5,
        outdoor_temp=5,
        native_policy_provenance={
            "diagnostic_assumptions": {
                "native_sh_provenance": context["native_sh_provenance"],
                "auxiliary_power_kw": 0,
            }
        },
    )
    session = SimpleNamespace(
        info={}, execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    )

    @asynccontextmanager
    async def sessions(**kwargs):
        yield session

    monkeypatch.setattr(module, "get_session", sessions)
    monkeypatch.setattr(module, "next_hour_boundary", lambda captured: start)
    for name, result in (
        ("_get_prices", prices),
        ("_get_weather", weather),
        ("_get_weather_full", []),
        ("_get_last_status", status),
    ):
        monkeypatch.setattr(optimizer, name, AsyncMock(return_value=result))
    values = {
        "device_status_max_age_minutes": 15,
        "poll_interval_seconds": 300,
        "comfort_temp_target": 20,
        "comfort_temp_min": 19,
        "comfort_temp_max": 22,
        "tank_min_temp": 48,
        "tank_min_temp_offpeak": 40,
        "tank_max_temp": 55,
        "space_heating_baseline_mode": "off",
        "space_heating_default_fraction": 0,
        "learned_schedule_threshold": 0.2,
        "indoor_forecast_max_passive_change_c_per_hour": 0.5,
    }
    monkeypatch.setattr(module, "get_setting", AsyncMock(side_effect=lambda name: values[name]))
    if producer == "milp":
        monkeypatch.setattr(module, "assumed_heating_efficiency", lambda *a: 3)
    else:
        monkeypatch.setattr("packages.ml.thermal.assumed_heating_efficiency", lambda *a: 3)
    monkeypatch.setattr(
        "packages.core.settings_service.get_setting",
        AsyncMock(side_effect=lambda name: values[name]),
    )
    monkeypatch.setattr(module, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(module, "get_heat_curve_config", AsyncMock(return_value=HeatCurveConfig()))
    monkeypatch.setattr(module, "get_space_heating_gate_config", AsyncMock(return_value={}))
    control = ControlTemperature(
        value=20, confidence="high", sensor_count=1, sample_count=1, latest_reading=now
    )
    monkeypatch.setattr(module, "get_control_temperature", AsyncMock(return_value=control))
    gate = SimpleNamespace(
        state="BLOCKED",
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
    )
    monkeypatch.setattr(module, "resolve_effective_gate", lambda *a: gate)
    monkeypatch.setattr(module, "project_gate_states", lambda *a: [gate] * hours)
    monkeypatch.setattr(thermal_model, "arefresh_if_changed", AsyncMock())
    monkeypatch.setattr(thermal_model.params, "last_calibrated", now)
    for name, value in (
        ("_tank_heating_rate", heat),
        ("_tank_loss_rate", loss),
        ("_indoor_heating_rate", 0),
        ("_indoor_cooling_rate", 0),
    ):
        monkeypatch.setattr(thermal_model, name, lambda *a, value=value: value)
    schedule = {"weekday": ready, "weekend": ready}
    if producer == "rules":
        monkeypatch.setattr(
            module, "_resolve_learning_mode", AsyncMock(return_value=LearningModeState.ACTIVE)
        )
        monkeypatch.setattr(module, "get_effective_schedule", AsyncMock(return_value=schedule))
        monkeypatch.setattr(
            module, "get_float_setting", AsyncMock(side_effect=lambda name: values[name])
        )
        monkeypatch.setattr(module, "get_int_setting", AsyncMock(return_value=0))
        monkeypatch.setattr(
            module, "evaluate_live_control_gate", AsyncMock(return_value={"control_allowed": False})
        )
        monkeypatch.setattr(
            module,
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
        monkeypatch.setattr(
            module, "load_recent_heating_evidence", AsyncMock(return_value=([], False))
        )
        monkeypatch.setattr(
            module,
            "build_baseline_duty_profile",
            lambda *a, **k: SimpleNamespace(
                points=[SimpleNamespace(expected_fraction=0, floor_fraction=0)] * hours
            ),
        )
        monkeypatch.setattr(module, "panasonic_tank_heating_available", lambda *a: True)
        monkeypatch.setattr(module, "panasonic_zone_heating_available", lambda *a: False)
        # Rules keep their existing no-candidates early return. Supply an unsupported
        # candidate as metadata so the real represented producer path is exercised;
        # it must never become an executable or credited thermal command.
        monkeypatch.setattr(
            optimizer,
            "_plan_peak_avoidance",
            lambda *a: [
                {
                    "type": "peak_avoidance",
                    "ts": start.isoformat(),
                    "payload": {},
                }
            ],
        )
        monkeypatch.setattr(optimizer, "_plan_quiet_mode", lambda *a, **k: [])
        monkeypatch.setattr(optimizer, "_estimate_unshifted_baseline", AsyncMock(return_value={}))
        # Deliberately stale legacy reporting must be replaced by the represented DTO.
        monkeypatch.setattr(
            optimizer,
            "_build_forecast_snapshot",
            lambda **k: {
                "forecast_no_heating": [{"predicted_indoor_temp": -999}],
                "price_forecast": [{"price_per_kwh": -999}],
            },
        )
        monkeypatch.setattr(optimizer, "_represented_indoor_rates", lambda **k: [(0, 0)] * hours)
        if dhw_minutes is not None:
            commands = pulse(
                {"hours": [{"ts": (start + dt.timedelta(hours=4)).isoformat()}]}, 0, dhw_minutes
            )
            monkeypatch.setattr(optimizer, "_plan_dhw", lambda *a, **k: commands)
    else:
        monkeypatch.setattr(module, "get_comfort_schedule", AsyncMock(return_value=schedule))
        monkeypatch.setattr(forecast_quality, "get_forecast_scorecard", AsyncMock(return_value={}))
        monkeypatch.setattr(
            forecast_quality,
            "evaluate_live_control_gate",
            AsyncMock(return_value={"control_allowed": True}),
        )
        monkeypatch.setattr(
            forecast_quality,
            "control_adjustments_for_weather",
            lambda *a: {
                "control_allowed": True,
                "condition_margins_c": [0] * hours,
                "bias_corrections_c": [0] * hours,
                "hourly_regimes": [{}] * hours,
            },
        )
        monkeypatch.setattr(
            forecast_quality, "prediction_intervals_for_weather", lambda *a: [{}] * hours
        )
    return SimpleNamespace(optimizer=optimizer, start=start, prices=prices)


@pytest.mark.parametrize("producer", ["rules", "milp"])
@pytest.mark.parametrize(
    "hours,ready,expected",
    [(24, [6], [24]), (6, [6, 12], [6]), (23, [6], [])],
)
async def test_R28_actual_generators_keep_only_deadlines_through_priced_endpoint(
    monkeypatch, producer, hours, ready, expected
):
    inputs = _deadline_generator_inputs(monkeypatch, producer, hours, ready)
    start = inputs.start
    plan = await inputs.optimizer.generate_plan()
    assert plan is not None
    assert plan["represented_strategy"]["deadline_hours"] == expected
    assert plan["horizon_start"] == start
    assert plan["horizon_end"] == start + dt.timedelta(hours=hours)
    assert plan["actions"] == []
    assert all(
        start <= dt.datetime.fromisoformat(action["ts"]) <= plan["horizon_end"]
        for action in plan["actions"]
    )
    validate_represented_plan(json.loads(json.dumps(plan, default=str)))


@pytest.mark.parametrize("producer", ["rules", "milp"])
async def test_R28_actual_endpoint_only_comfort_requires_feasible_heating(monkeypatch, producer):
    from packages.core.config import settings

    inputs = _deadline_generator_inputs(
        monkeypatch,
        producer,
        6,
        [12],
        tank=43,
        loss=-0.5,
        heat=5,
        dhw_minutes=90,
    )
    plan = await inputs.optimizer.generate_plan()
    assert plan is not None
    saved = json.loads(json.dumps(plan, default=str))
    value = saved["represented_strategy"]
    assert value["deadline_hours"] == [6]
    assert value["deadline_tank_floors_c"] == [48]
    assert [row["tank_floor_c"] for row in value["hours"]] == [40] * 6
    assert value["tank_hardware_floor_c"] == 30
    assert value["initial_tank_c"] == 43
    assert all(row["tank_loss_c_per_h"] == -0.5 for row in value["hours"])
    assert inputs.start.hour == 6 and plan["horizon_end"].hour == 12
    assert plan["horizon_end"] - inputs.start == dt.timedelta(hours=6)
    on = [
        action
        for action in saved["diagnostic_strategy_actions"]
        if action["type"] == "force_dhw_on"
    ]
    runtime = sum(action["payload"]["dhw_minutes"] for action in on)
    assert runtime == (90 if producer == "rules" else 88)
    # Without heating: 43 - .5*6 = 40, not the required 48.
    # Each on-hour replaces -.5 C/h loss with +5 C/h net heat exactly once.
    expected_tank = 40 + 5.5 * runtime / 60
    assert saved["serialized_replay"]["states"][-1]["tank_c"] == pytest.approx(expected_tank)
    assert expected_tank >= 48
    expected_kwh = (5.5 * settings.tank_kwh_per_degree / 3) * runtime / 60
    assert saved["cost_accounting"]["electrical_kwh"] == pytest.approx(expected_kwh)
    assert saved["cost_estimate"] == pytest.approx(expected_kwh * 0.2)
    assert validate_represented_plan(saved) == saved["serialized_replay"]
    with pytest.raises(InfeasibleError, match="deadline"):
        replay_serialized_actions(value, [])
    forged = json.loads(json.dumps(saved))
    forged["actions"] = forged["diagnostic_strategy_actions"] = []
    with pytest.raises(InfeasibleError, match="deadline"):
        validate_represented_plan(forged)
    for action in saved["diagnostic_strategy_actions"]:
        assert inputs.start <= dt.datetime.fromisoformat(action["ts"]) <= plan["horizon_end"]


@pytest.mark.parametrize("producer", ["rules", "milp"])
async def test_R28_actual_endpoint_only_comfort_refuses_unrecoverable_tank(monkeypatch, producer):
    from unittest.mock import Mock
    from packages.optimizer import represented_strategy

    inputs = _deadline_generator_inputs(
        monkeypatch,
        producer,
        6,
        [12],
        tank=43,
        loss=-0.5,
        heat=0.5,
        dhw_minutes=90,
    )
    build = Mock(wraps=represented_strategy.build_represented_plan)
    monkeypatch.setattr(represented_strategy, "build_represented_plan", build)
    # Even six full on-hours reach only 43 + .5*6 = 46 C < 48 C.
    if producer == "rules":
        assert await inputs.optimizer.generate_plan() is None
    else:
        with pytest.raises(InfeasibleError):
            await inputs.optimizer.generate_plan()
    build.assert_called_once()
    assert build.call_args.kwargs["dhw_deadlines"] == [6]
    assert build.call_args.kwargs["dhw_deadline_tank_floors_c"] == [48]
    assert build.call_args.kwargs["tank_min_per_hour"] == [40] * 6


@pytest.mark.parametrize("consumer", ["solve", "replay"])
def test_R28_missing_explicit_deadline_floor_is_unproven(consumer):
    value = trace(1)
    value.pop("deadline_tank_floors_c")
    with pytest.raises(DataIncompleteError, match="explicit DHW deadline floors"):
        if consumer == "solve":
            solve_represented_trace(value, timeout_seconds=5)
        else:
            replay_serialized_actions(value, pulse(value, 0, 30))


@pytest.mark.parametrize("floors", [None, [], [48, 48]])
def test_R28_deadline_floor_coverage_must_be_complete(floors):
    value = trace(1)
    value["deadline_tank_floors_c"] = floors
    with pytest.raises(DataIncompleteError, match="explicit DHW deadline floors"):
        replay_serialized_actions(value, pulse(value, 0, 30))


def test_R28_duplicate_deadline_cannot_hide_a_requirement():
    value = trace(1)
    value["deadline_hours"] = [1, 1]
    value["deadline_tank_floors_c"] = [48, 49]
    with pytest.raises(DataIncompleteError, match="Duplicate DHW deadline"):
        replay_serialized_actions(value, pulse(value, 0, 30))


def test_R28_builder_binds_deadline_to_observed_hardware_floor():
    from tests.test_milp import _context

    case = _Case([0.2], tank=44, tank_floors=[30], deadlines=[1])
    owner = _context(case.price_rows, demand=case.demand, tank_hardware_floor_c=45)
    plan = case.solve(observation_context=owner, dhw_deadline_tank_floors_c=[44])
    assert plan["represented_strategy"]["deadline_tank_floors_c"] == [45]
    assert plan["actions"][0]["payload"]["dhw_minutes"] == 17
    assert plan["serialized_replay"]["states"][-1]["tank_c"] == pytest.approx(
        44 + 5 * 17 / 60 - 0.5 * 43 / 60
    )
    with pytest.raises(DataIncompleteError, match="explicit DHW deadline floors"):
        case.solve(observation_context=owner, dhw_deadline_tank_floors_c=None)


@pytest.mark.parametrize("floor", [None, True, float("nan"), 29, 56])
def test_R28_deadline_floor_must_be_finite_and_hardware_bounded(floor):
    value = trace(1)
    value["deadline_tank_floors_c"] = [floor]
    with pytest.raises(DataIncompleteError):
        replay_serialized_actions(value, pulse(value, 0, 30))


def test_R28_each_serialized_deadline_uses_its_own_explicit_floor():
    value = trace(6)
    value.update(
        initial_tank_c=43,
        tank_hardware_floor_c=30,
        deadline_hours=[2, 6],
        deadline_tank_floors_c=[44, 48],
    )
    for hour, row in enumerate(value["hours"]):
        row.update(
            tank_floor_c=40,
            gate_state="BLOCKED",
            native_sh_fraction=0,
            price_per_kwh=0.3 if hour < 2 else 0.1,
        )
    declare_native(value)
    commands, result = solve_represented_trace(value, timeout_seconds=5)
    result = replay_serialized_actions(
        json.loads(json.dumps(value)), json.loads(json.dumps(commands))
    )
    end2 = (dt.datetime.fromisoformat(value["hours"][0]["ts"]) + dt.timedelta(hours=2)).isoformat()
    tank2 = next(state["tank_c"] for state in result["states"] if state["ts"] == end2)
    assert 44 - 1e-5 <= tank2 < 48
    assert result["states"][-1]["tank_c"] >= 48 - 1e-5


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "compatibility_temperature",
        "compatibility_fraction",
        "native_fraction",
        "delivered_fraction",
        "passive_counterfactual",
        "initial_disclosure",
    ],
)
def test_R15_json_MILP_reporting_is_bound_to_replay_and_passive_trace(fault):
    case = _Case(
        [-0.2, 0.3],
        demand=[4, 2],
        rates=[(0.2, -0.1), (0.3, -0.2)],
    )
    plan = case.solve(forecast_bias_corrections_c=[0.1, -0.2])
    plan = json.loads(json.dumps(plan, default=str))
    assert [
        point["predicted_indoor_temp"] for point in plan["forecast_snapshot"]["forecast_no_heating"]
    ] == pytest.approx([20.0, 19.5])
    if fault == "compatibility_temperature":
        plan["indoor_forecast"][0]["predicted_indoor_temp"] += 1
    elif fault == "compatibility_fraction":
        plan["indoor_forecast"][0]["space_heating_fraction"] += 0.1
    elif fault == "native_fraction":
        plan["forecast_snapshot"]["forecast_with_plan"][0]["native_space_heating_fraction"] += 0.1
    elif fault == "delivered_fraction":
        plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"] += 0.1
    elif fault == "passive_counterfactual":
        plan["forecast_snapshot"]["forecast_no_heating"][0]["predicted_indoor_temp"] += 0.1
    elif fault == "initial_disclosure":
        plan["initial_violations"]["tank_above_ceiling"] = True
    if fault is None:
        assert validate_represented_plan(plan)["cost"] == plan["cost_estimate"]
    else:
        with pytest.raises(DataIncompleteError):
            validate_represented_plan(plan)

"""Tests for the MILP optimizer."""

import contextlib
import datetime as dt
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.space_heating_gate import SpaceHeatingGateState
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError
from packages.optimizer.milp import MILPOptimizer


def _make_prices(hours=24, base_price=0.10):
    """Generate hourly prices with a day/night pattern."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    hourly = [
        0.05,
        0.04,
        0.03,
        0.03,
        0.04,
        0.06,
        0.08,
        0.12,
        0.15,
        0.18,
        0.20,
        0.22,
        0.20,
        0.18,
        0.15,
        0.14,
        0.16,
        0.25,
        0.30,
        0.28,
        0.20,
        0.12,
        0.08,
        0.06,
    ]
    return [(base + dt.timedelta(hours=h), hourly[h % 24]) for h in range(hours)]


def _make_weather(hours=24):
    """Generate hourly weather (mild spring day)."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    temps = [
        5,
        4,
        3,
        3,
        3,
        4,
        5,
        7,
        9,
        11,
        13,
        14,
        15,
        15,
        14,
        13,
        12,
        10,
        8,
        7,
        6,
        6,
        5,
        5,
    ]
    return [(base + dt.timedelta(hours=h), temps[h % 24]) for h in range(hours)]


def _make_freezing_weather(hours=24):
    """Generate freezing weather."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    return [(base + dt.timedelta(hours=h), -5.0) for h in range(hours)]


_BASE = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
_DEVICE = "synthetic-milp-device"
_TOLERANCE = 1e-6
ALLOWED = SpaceHeatingGateState.ALLOWED
BLOCKED = SpaceHeatingGateState.BLOCKED
UNKNOWN = SpaceHeatingGateState.UNKNOWN


def _context(prices, *, demand=None, power=12.0, **overrides):
    """Aware frozen observation bound to the synthetic device and horizon start."""
    context = {
        "device_id": _DEVICE,
        "observed_at": prices[0][0],
        "captured_at": prices[0][0],
        "max_age_seconds": 900,
        "tank_target_c": 60.0,
        "force_dhw": 0,
        "tank_hardware_floor_c": 30,
        "auxiliary_power_kw": 0,
    }
    context.update(overrides)
    method = "unchanged_hourly_load_over_declared_max_electrical_power"
    context["native_sh_provenance"] = {
        "kind": "explicit_conditional_assumption",
        "method": method,
        "device_id": context["device_id"],
        "captured_at": prices[0][0].isoformat(),
        "horizon_start": prices[0][0].isoformat(),
        "horizon_end": (prices[-1][0] + dt.timedelta(hours=1)).isoformat(),
        "maximum_electrical_power_kw": power,
        "hourly_electrical_kw": list(demand if demand is not None else [0] * len(prices)),
    }
    return context


def _pulse(hour, minutes):
    """One serialized DHW pulse with its linked restore."""
    start = _BASE + dt.timedelta(hours=hour)
    key = f"dhw:{start.isoformat()}"
    return [
        {
            "ts": start.isoformat(),
            "type": "force_dhw_on",
            "action_key": key,
            "payload": {"dhw_minutes": minutes},
            "device_id": _DEVICE,
        },
        {
            "ts": (start + dt.timedelta(minutes=minutes)).isoformat(),
            "type": "force_dhw_off",
            "reverts_action_key": key,
            "payload": {},
            "device_id": _DEVICE,
        },
    ]


@dataclass
class _Case:
    """Admitted frozen fixture (F) whose physics the test replays independently (B)."""

    prices: list
    demand: list | None = None
    gates: list | None = None
    outdoor: list | None = None
    power: float = 12.0
    tank_on: float = 5.0
    tank_off: float = -0.5
    capacity: float = 0.2
    cop: float = 2.0
    tank: float = 48.0
    room: float = 20.0
    tank_floors: list | None = None
    tank_ceiling: float = 55.0
    target: float = 60.0
    deadlines: list | None = None
    rates: list | None = None
    room_floors: list | None = None
    room_ceiling: float = 24.0
    default_tank_floor: bool = False

    def __post_init__(self):
        from packages.core.config import settings

        hours = len(self.prices)
        self.demand = list(self.demand if self.demand is not None else [0.0] * hours)
        self.gates = list(self.gates if self.gates is not None else [ALLOWED] * hours)
        self.outdoor = list(self.outdoor if self.outdoor is not None else [10.0] * hours)
        if self.default_tank_floor:
            self.tank_floors = [float(settings.tank_min_temp)] * hours
        self.tank_floors = list(
            self.tank_floors if self.tank_floors is not None else [43.0] * hours
        )
        self.deadlines = list(self.deadlines or [])
        self.rates = list(self.rates if self.rates is not None else [(0.0, 0.0)] * hours)
        self.room_floors = list(
            self.room_floors if self.room_floors is not None else [18.0] * hours
        )

    @property
    def price_rows(self):
        return [(_BASE + dt.timedelta(hours=h), price) for h, price in enumerate(self.prices)]

    @property
    def weather_rows(self):
        return [(_BASE + dt.timedelta(hours=h), temp) for h, temp in enumerate(self.outdoor)]

    @contextlib.contextmanager
    def physics(self):
        from packages.core.config import settings
        from packages.ml.thermal import thermal_model

        with (
            patch.object(thermal_model.params, "last_calibrated", _BASE),
            patch.object(thermal_model, "_tank_heating_rate", return_value=self.tank_on),
            patch.object(thermal_model, "_tank_loss_rate", return_value=self.tank_off),
            patch.object(settings, "sh_max_power_kw", self.power),
            patch.object(
                type(settings), "tank_kwh_per_degree", property(lambda _settings: self.capacity)
            ),
        ):
            yield

    def solve(self, optimizer=None, **overrides):
        prices = self.price_rows
        kwargs = {
            "cop_fn": lambda temperature, hour=12: self.cop,
            "demand_per_hour": list(self.demand),
            "current_tank_temp": self.tank,
            "current_indoor_temp": self.room,
            "dhw_deadlines": list(self.deadlines),
            "dhw_deadline_tank_floors_c": [max(self.tank_floors)] * len(self.deadlines),
            "indoor_targets": list(self.room_floors),
            "tank_min_per_hour": None if self.default_tank_floor else list(self.tank_floors),
            "tank_max_temp_setting": self.tank_ceiling,
            "indoor_rates": list(self.rates),
            "gate_states": list(self.gates),
            "indoor_max_temp_setting": self.room_ceiling,
            "hardware_tank_ceiling": self.tank_ceiling,
            "observation_context": _context(
                prices, tank_target_c=self.target, demand=self.demand, power=self.power
            ),
        }
        kwargs.update(overrides)
        with self.physics():
            return (optimizer or MILPOptimizer())._solve(prices, self.weather_rows, **kwargs)

    def replay(self, actions):
        """Hand-calculate piecewise states from JSON-serialized commands only."""
        actions = json.loads(json.dumps(actions))
        hours = len(self.prices)
        assert {action["type"] for action in actions} <= {"force_dhw_on", "force_dhw_off"}
        sources = [action for action in actions if action["type"] == "force_dhw_on"]
        by_key = {action["action_key"]: action for action in sources}
        restores = [action for action in actions if action["type"] == "force_dhw_off"]
        assert len(by_key) == len(sources) == len(restores)
        assert {action["reverts_action_key"] for action in restores} == set(by_key)
        pulses = {}
        for restore in restores:
            source = by_key[restore["reverts_action_key"]]
            start = dt.datetime.fromisoformat(source["ts"])
            minutes = source["payload"]["dhw_minutes"]
            assert type(minutes) is int and 5 <= minutes <= 60
            assert dt.datetime.fromisoformat(restore["ts"]) - start == dt.timedelta(minutes=minutes)
            offset = (start - _BASE) / dt.timedelta(hours=1)
            assert offset == int(offset) and 0 <= offset < hours
            assert int(offset) not in pulses
            pulses[int(offset)] = minutes
        dhw_power = (self.tank_on - self.tank_off) * self.capacity / self.cop
        tank, room = self.tank, self.room
        energy = cost = 0.0
        peaks, tank_ends, room_ends, delivered = {}, [], [], []
        deadline_floor = max(self.tank_floors)
        for hour in range(hours):
            native = self.demand[hour] / self.power
            full, passive = self.rates[hour]
            on = pulses.get(hour, 0) / 60
            segments = ([(on, True)] if on else []) + ([(1 - on, False)] if on < 1 else [])
            hour_energy = 0.0
            for duration, dhw in segments:
                fraction = 0.0 if dhw else native
                tank += duration * (self.tank_on if dhw else self.tank_off)
                room += duration * (passive + fraction * (full - passive))
                hour_energy += duration * (dhw_power if dhw else self.power * fraction)
                assert self.tank_floors[hour] - _TOLERANCE <= tank
                assert tank <= self.tank_ceiling + _TOLERANCE
                assert self.room_floors[hour] - _TOLERANCE <= room
                assert room <= self.room_ceiling + _TOLERANCE
                if dhw:
                    peaks[hour] = tank
                    assert tank < self.target - 0.5
            energy += hour_energy
            cost += hour_energy * self.prices[hour]
            delivered.append((1 - on) * native)
            tank_ends.append(tank)
            room_ends.append(room)
            if hour + 1 in self.deadlines:
                assert tank >= deadline_floor - _TOLERANCE
        return SimpleNamespace(
            pulses=pulses,
            peaks=peaks,
            tank_ends=tank_ends,
            room_ends=room_ends,
            energy=energy,
            cost=cost,
            delivered=delivered,
        )


def _assert_replayed(case, plan):
    """Compare the returned plan with the independent hand replay (B)."""
    hand = case.replay(plan["actions"])
    assert plan["diagnostic_status"] == "conditional_feasible"
    assert plan["diagnostic_strategy_actions"] == plan["actions"]
    assert plan["cost_estimate"] == pytest.approx(hand.cost, abs=_TOLERANCE)
    assert plan["cost_accounting"]["electrical_kwh"] == pytest.approx(hand.energy, abs=_TOLERANCE)
    points = plan["forecast_snapshot"]["forecast_with_plan"]
    assert [point["predicted_indoor_temp"] for point in points] == pytest.approx(
        hand.room_ends, abs=_TOLERANCE
    )
    assert [point["space_heating_fraction"] for point in points] == pytest.approx(
        hand.delivered, abs=_TOLERANCE
    )
    assert [point["native_space_heating_fraction"] for point in points] == pytest.approx(
        [demand / case.power for demand in case.demand]
    )
    states = {state["ts"]: state for state in plan["serialized_replay"]["states"]}
    for hour, minutes in hand.pulses.items():
        pulse_end = (_BASE + dt.timedelta(hours=hour, minutes=minutes)).isoformat()
        assert states[pulse_end]["tank_c"] == pytest.approx(hand.peaks[hour], abs=_TOLERANCE)
    return hand


def _k_case(**overrides):
    """K: independently calculated two-hour trace with a unique early 30-minute pulse."""
    values = {
        "prices": [0.1, 0.4],
        "demand": [0.5, 0.5],
        "power": 2.0,
        "rates": [(1.1, -0.1)] * 2,
        "room": 20.0,
        "room_floors": [19.5, 19.5],
        "room_ceiling": 20.5,
        "tank": 44.0,
        "tank_on": 9.5,
        "tank_off": -0.5,
        "tank_floors": [43.0, 48.0],
        "tank_ceiling": 49.0,
        "deadlines": [2],
        "capacity": 0.2,
        "cop": 2.0,
        "target": 55.0,
    }
    values.update(overrides)
    return _Case(**values)


def _sh_case(**overrides):
    """Space-heating-only trace: the observed tank already sits at its target, so no pulse."""
    values = {"tank": 48.0, "target": 48.0}
    values.update(overrides)
    return _Case(**values)


def _day_case(**overrides):
    """24-hour admitted fixture with a lossy tank that needs DHW pulses."""
    values = {
        "prices": [price for _, price in _make_prices()],
        "outdoor": [temp for _, temp in _make_weather()],
        "demand": [3.0] * 24,
        "rates": [(0.3, -0.1)] * 24,
    }
    values.update(overrides)
    return _Case(**values)


def _fast_case(heating_rate, standby_loss):
    return _Case(
        prices=[0.3, 0.1, 0.3],
        tank=46.0,
        tank_on=heating_rate,
        tank_off=standby_loss,
        tank_floors=[43.0, 43.0, 45.0],
        deadlines=[3],
    )


def _dhw_actions(plan):
    return [action for action in plan["actions"] if action["type"] == "force_dhw_on"]


class TestMILPSolver:
    """Tests for the MILP solve logic (synchronous, no DB)."""

    def test_milp_picks_cheapest_dhw_slot(self):
        """The integer solver places the required DHW pulse in the cheaper hour."""
        case = _k_case()
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert plan["version"] == "milp_v1"
        assert plan["cost_estimate"] == pytest.approx(0.275)
        assert hand.pulses == {0: 30}
        assert hand.peaks[0] == pytest.approx(48.75)
        assert hand.tank_ends == pytest.approx([48.5, 48.0])
        assert hand.room_ends == pytest.approx([20.05, 20.25])
        assert hand.energy == pytest.approx(1.25)

        late = case.replay(_pulse(1, 30))
        assert late.peaks[1] == pytest.approx(48.25)
        assert late.tank_ends[-1] == pytest.approx(48.0)
        assert late.room_ends[-1] == pytest.approx(20.25)
        assert late.energy == pytest.approx(1.25)
        assert late.cost == pytest.approx(0.35)
        assert case.solve(serialized_actions=_pulse(1, 30))["cost_estimate"] == pytest.approx(0.35)

        dhw_on = _dhw_actions(plan)
        assert len(dhw_on) > 0
        for action in dhw_on:
            price = action["payload"]["price"]
            assert price == pytest.approx(0.1)
            assert price <= 0.20

    def test_milp_solution_duty_is_not_augmented_by_baseline(self):
        case = _k_case()
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        point = plan["forecast_snapshot"]["forecast_with_plan"][0]
        assert point["source"] == "milp_solution"
        assert "baseline_heating_fraction" not in point
        assert "space_heating_source" not in point
        assert [row["native_sh_fraction"] for row in plan["represented_strategy"]["hours"]] == [
            pytest.approx(0.5 / 2.0)
        ] * 2
        assert hand.delivered == pytest.approx([0.125, 0.25])
        states = plan["serialized_replay"]["states"][1:]
        assert any(state["dhw_active"] for state in states)
        for state in states:
            expected = 0.0 if state["dhw_active"] else 0.25
            assert state["space_heating_fraction"] == pytest.approx(expected)

    def test_milp_snapshot_preserves_its_solved_and_no_heating_trajectories(self):
        """The UI must be able to display the exact scenario the MILP solved."""
        from packages.optimizer.milp import MILPOptimizer

        prices = _make_prices()
        plan = MILPOptimizer()._solve(
            prices,
            _make_weather(),
            cop_fn=lambda t, h=12: 3.5 + 0.1 * t,
            demand_per_hour=[1.0] * 24,
            current_tank_temp=48.0,
            current_indoor_temp=20.0,
            indoor_targets=[15.0] * 24,
            indoor_rates=[(0.2, -0.1)] * 24,
            gate_states=[SpaceHeatingGateState.ALLOWED] * 24,
            hardware_tank_ceiling=55,
            tank_max_temp_setting=55,
            tank_min_per_hour=[48] * 24,
            observation_context=_context(
                prices, device_id="snapshot-device", tank_target_c=55, demand=[1.0] * 24
            ),
        )

        snapshot = plan["forecast_snapshot"]
        planned = snapshot["forecast_with_plan"]
        no_heating = snapshot["forecast_no_heating"]

        assert len(planned) == len(prices) == len(no_heating)
        assert planned[0]["hour"] == 1
        assert planned[0]["ts"] == (prices[0][0] + dt.timedelta(hours=1)).isoformat()
        assert planned[0]["source"] == "milp_solution"
        assert no_heating[0]["source"] == "milp_counterfactual"
        assert all(
            plan_row["predicted_indoor_temp"] >= baseline_row["predicted_indoor_temp"]
            for plan_row, baseline_row in zip(planned, no_heating)
        )
        assert snapshot["price_forecast"][0]["price_eur_per_kwh"] == prices[0][1]
        assert snapshot["version"] == "indoor_forecast_v4"

    def test_overheat_cap_does_not_create_a_hard_floor(self):
        case = _Case(prices=[0.1], room=20.0, room_floors=[18.0], rates=[(1.0, -0.5)])
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=[1.0])

        hand = _assert_replayed(case, plan)
        fraction = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert 0.0 <= fraction <= 1.0
        assert fraction == 0.0
        assert hand.room_ends == pytest.approx([19.5])
        assert plan["actions"] == []

    def test_overheat_cap_with_blocked_gate_remains_feasible(self):
        case = _Case(
            prices=[0.1], gates=[BLOCKED], room=19.0, room_floors=[18.0], rates=[(1.0, -0.5)]
        )
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=[0.5])

        hand = _assert_replayed(case, plan)
        fraction = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert fraction == pytest.approx(0.0)
        assert hand.room_ends == pytest.approx([18.5])
        assert plan["actions"] == []

        too_cold = _Case(
            prices=[0.1], gates=[BLOCKED], room=17.0, room_floors=[18.0], rates=[(1.0, -0.5)]
        )
        with pytest.raises(InfeasibleError):
            too_cold.solve(room_overheat_active=[True], floor_protection_duty=[0.5])

    def test_overheat_cap_handles_mixed_allowed_and_blocked_gate_hours(self):
        case = _sh_case(
            prices=[0.1, 0.1],
            demand=[9.0, 0.0],
            gates=[ALLOWED, BLOCKED],
            rates=[(1.0, -0.5)] * 2,
            room_ceiling=21.0,
        )
        plan = case.solve(room_overheat_active=[True, True], floor_protection_duty=[0.5, 0.5])

        hand = _assert_replayed(case, plan)
        fractions = [
            row["space_heating_fraction"] for row in plan["forecast_snapshot"]["forecast_with_plan"]
        ]
        assert fractions == pytest.approx([0.75, 0.0])
        assert hand.room_ends == pytest.approx([20.625, 20.125])
        assert plan["actions"] == []

    def test_non_overheat_solution_remains_feasible_and_unconstrained(self):
        case = _sh_case(prices=[0.1], demand=[6.0], rates=[(1.0, -0.5)])
        plan = case.solve(room_overheat_active=[False], floor_protection_duty=[0.0])

        hand = _assert_replayed(case, plan)
        assert plan["forecast_snapshot"]["forecast_with_plan"][0][
            "space_heating_fraction"
        ] == pytest.approx(0.5)
        assert hand.room_ends == pytest.approx([20.25])
        assert plan["cost_estimate"] == pytest.approx(0.6)

    def test_overheat_cap_blocks_heat_above_floor_requirement(self):
        case = _Case(prices=[0.1], room=20.0, rates=[(1.0, -0.5)])
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=[0.25])

        _assert_replayed(case, plan)
        fraction = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert fraction == 0.0

        # A positive native load is not reduced to the 0.25 duty, which would end at 19.875.
        overheating = _sh_case(
            prices=[0.1], demand=[12.0], room=20.4, rates=[(1.0, -0.5)], room_ceiling=20.5
        )
        with pytest.raises(InfeasibleError):
            overheating.solve(room_overheat_active=[True], floor_protection_duty=[0.25])

    def test_generate_plan_envelope_overheat_sets_cap(self):
        floor_duties = [0.25]
        case = _sh_case(prices=[0.1], demand=[6.0], rates=[(1.0, -0.5)], room_ceiling=21.0)
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=floor_duties)

        hand = _assert_replayed(case, plan)
        retained_heat = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert retained_heat == pytest.approx(0.5)
        assert retained_heat > floor_duties[0]
        assert hand.room_ends == pytest.approx([20.25])

        unchanged_trace_too_warm = _sh_case(
            prices=[0.1], demand=[6.0], rates=[(1.0, -0.5)], room_ceiling=20.2
        )
        with pytest.raises(InfeasibleError):
            unchanged_trace_too_warm.solve(
                room_overheat_active=[True], floor_protection_duty=floor_duties
            )

    def test_envelope_failure_disables_cap(self):
        case = _sh_case(prices=[0.1], demand=[6.0], rates=[(1.0, -0.5)])
        plan = case.solve(room_overheat_active=[False], floor_protection_duty=[0.0])

        _assert_replayed(case, plan)
        fraction = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert fraction == pytest.approx(0.5)

    def test_cumulative_demand_vs_cap_feasible_via_slack(self):
        case = _sh_case(prices=[0.05], demand=[12.0], rates=[(1.1, -0.1)], room_ceiling=22.0)
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=[0.0])

        hand = _assert_replayed(case, plan)
        assert plan["forecast_snapshot"]["forecast_with_plan"][0][
            "space_heating_fraction"
        ] == pytest.approx(1.0)
        assert hand.room_ends == pytest.approx([21.1])
        assert plan["cost_estimate"] == pytest.approx(12.0 * 0.05)

        impossible = _sh_case(prices=[0.05], demand=[12.0], rates=[(1.1, -0.1)], room_ceiling=20.5)
        with pytest.raises(InfeasibleError):
            impossible.solve(room_overheat_active=[True], floor_protection_duty=[0.0])

    def test_fractional_retry_uses_remaining_shared_budget(self):
        import pulp

        calls = []

        def finish_with(status):
            def solve(problem, solver=None, **_kwargs):
                calls.append(solver)
                problem.status = status
                return status

            return solve

        with (
            patch("pulp.LpProblem.solve", new=finish_with(pulp.LpStatusNotSolved)),
            pytest.raises(SolverTimeoutError),
        ):
            _k_case().solve()
        assert len(calls) == 1
        assert calls[0].timeLimit == MILPOptimizer.SOLVER_TIMEOUT_SECONDS

        calls.clear()
        with (
            patch("pulp.LpProblem.solve", new=finish_with(pulp.LpStatusInfeasible)),
            pytest.raises(InfeasibleError),
        ):
            _k_case().solve()
        assert len(calls) == 1

    def test_no_third_cbc_call(self):
        import pulp

        calls, problems = [], []
        original = pulp.LpProblem.solve

        def record_solve(problem, solver=None, **kwargs):
            calls.append(solver)
            problems.append(problem)
            return original(problem, solver, **kwargs)

        case = _k_case()
        with patch("pulp.LpProblem.solve", record_solve):
            plan = case.solve()

        assert len(calls) == 1
        minutes = [var for var in problems[0].variables() if var.name.startswith("dhw_minutes_")]
        assert len(minutes) == 2
        assert all(var.cat == pulp.LpInteger for var in minutes)
        hand = _assert_replayed(case, plan)
        assert hand.pulses == {0: 30}
        assert [action["type"] for action in plan["actions"]] == ["force_dhw_on", "force_dhw_off"]

    def test_cold_hour_overheat_remains_feasible(self):
        case = _Case(
            prices=[0.1],
            outdoor=[-5.0],
            room=20.0,
            room_floors=[19.5],
            rates=[(1.1, -0.2)],
        )
        plan = case.solve(room_overheat_active=[True], floor_protection_duty=[0.0])

        hand = _assert_replayed(case, plan)
        fraction = plan["forecast_snapshot"]["forecast_with_plan"][0]["space_heating_fraction"]
        assert fraction == pytest.approx(0.0)
        assert hand.room_ends == pytest.approx([19.8])

        cooling_through_floor = _Case(
            prices=[0.1],
            outdoor=[-5.0],
            room=20.0,
            room_floors=[19.5],
            rates=[(1.1, -1.0)],
        )
        with pytest.raises(InfeasibleError):
            cooling_through_floor.solve(room_overheat_active=[True], floor_protection_duty=[0.0])

    def test_milp_respects_tank_deadline(self):
        """Tank should be warm enough by deadline hours."""
        case = _k_case()
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert len(_dhw_actions(plan)) > 0
        assert hand.tank_ends[1] >= 48.0 - _TOLERANCE

        with pytest.raises(InfeasibleError):
            _k_case(tank_on=1.0).solve()

    def test_milp_freeze_protection(self):
        """A freezing admitted day keeps its declared native SH and a valid DHW schedule."""
        case = _day_case(outdoor=[-5.0] * 24)
        plan = case.solve()

        _assert_replayed(case, plan)
        assert plan["cost_estimate"] > 0
        assert [row["native_sh_fraction"] for row in plan["represented_strategy"]["hours"]] == [
            pytest.approx(0.25)
        ] * 24

    def test_milp_rate_limit_constraint(self):
        """No more than 20 DHW activations per day."""
        case = _day_case()
        plan = case.solve()

        _assert_replayed(case, plan)
        dhw_on = _dhw_actions(plan)
        assert 0 < len(dhw_on) <= 20

    def test_demand_forecast_becomes_an_energy_reserve(self):
        """Declared native demand is reported and costed once, without a COP division."""
        common = {
            "prices": [0.1] * 24,
            "tank_off": 0.0,
            "rates": [(0.0, 0.0)] * 24,
        }
        without_case = _sh_case(demand=[0.0] * 24, **common)
        with_case = _sh_case(demand=[3.0] * 24, **common)
        without_demand = without_case.solve()
        with_demand = with_case.solve()

        _assert_replayed(without_case, without_demand)
        _assert_replayed(with_case, with_demand)
        assert with_demand["space_heating_demand_kwh"] == pytest.approx(72.0)
        assert without_demand["space_heating_demand_kwh"] == pytest.approx(0.0)
        assert without_demand["cost_estimate"] == pytest.approx(0.0)
        assert with_demand["cost_estimate"] == pytest.approx(7.2)
        assert with_demand["cost_accounting"]["electrical_kwh"] == pytest.approx(72.0)
        assert with_demand["cost_estimate"] > without_demand["cost_estimate"]

    def test_demand_profile_clamps_invalid_values(self):
        from packages.optimizer.milp import MILPOptimizer

        assert MILPOptimizer._normalise_demand_profile(
            [-1.0, None, "invalid", 99.0], hours=5, max_power_kw=12.0
        ) == [0.0, 0.0, 0.0, 12.0, 0.0]

    def test_untrained_demand_fallback_respects_gate_projection(self):
        """Fallback load follows known gate evidence; unknown gates refuse admission."""
        from packages.optimizer.milp import MILPOptimizer

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        weather = [
            (base, 15.0),
            (base + dt.timedelta(hours=1), 13.0),
            (base + dt.timedelta(hours=2), 5.0),
            (base + dt.timedelta(hours=3), -15.0),
            (base + dt.timedelta(hours=4), -25.0),
            (base + dt.timedelta(hours=5), None),
        ]

        with patch("packages.optimizer.milp.settings") as mock_settings:
            mock_settings.sh_max_power_kw = 12.0
            profile = MILPOptimizer()._build_demand_estimates(
                weather,
                gate_states=["BLOCKED", "BLOCKED", "ALLOWED", "ALLOWED", "ALLOWED", "BLOCKED"],
            )
            with pytest.raises(DataIncompleteError, match="known gate"):
                MILPOptimizer()._build_demand_estimates(
                    weather,
                    gate_states=["BLOCKED", "UNKNOWN", "ALLOWED", "ALLOWED", "ALLOWED", "UNKNOWN"],
                )

        assert profile == pytest.approx([0.0, 0.0, 36.0 / 7.0, 12.0, 12.0, 0.0])
        assert all(0.0 <= demand <= 12.0 for demand in profile)

    def test_trained_demand_respects_gate_projection(self):
        from packages.core.space_heating_gate import SpaceHeatingGateState
        from packages.optimizer.milp import MILPOptimizer

        weather = _make_weather(hours=3)
        demand = MagicMock(is_trained=True)
        demand.predict_hourly.return_value = [2.0, 3.0, 4.0]

        profile = MILPOptimizer(demand_model=demand)._build_demand_estimates(
            weather,
            gate_states=[
                SpaceHeatingGateState.ALLOWED,
                SpaceHeatingGateState.BLOCKED,
                SpaceHeatingGateState.BLOCKED,
            ],
        )

        assert profile == [2.0, 0.0, 0.0]

        refused = MagicMock(is_trained=True)
        with pytest.raises(DataIncompleteError, match="known gate"):
            MILPOptimizer(demand_model=refused)._build_demand_estimates(
                weather,
                gate_states=[
                    SpaceHeatingGateState.ALLOWED,
                    SpaceHeatingGateState.BLOCKED,
                    SpaceHeatingGateState.UNKNOWN,
                ],
            )
        refused.predict_hourly.assert_not_called()

    def test_milp_zeroes_blocked_and_unknown_hours_and_emits_no_sh_modes(self):
        case = _Case(
            prices=[price for _, price in _make_prices(hours=4)],
            outdoor=[-5.0] * 4,
            demand=[2.0, 0.0, 0.0, 2.0],
            gates=[ALLOWED, BLOCKED, BLOCKED, ALLOWED],
            tank=44.0,
            deadlines=[3],
        )
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        fractions = [
            point["space_heating_fraction"]
            for point in plan["forecast_snapshot"]["forecast_with_plan"]
        ]
        assert fractions[1:3] == [0.0, 0.0]
        assert hand.delivered[1:3] == [0.0, 0.0]
        assert [row["native_sh_fraction"] for row in plan["represented_strategy"]["hours"]] == (
            pytest.approx([2.0 / 12.0, 0.0, 0.0, 2.0 / 12.0])
        )
        sh_modes = {
            "eco_mode_on",
            "normal_mode_on",
            "comfort_mode_on",
            "quiet_mode_on",
            "quiet_mode_off",
        }
        assert not any(action["type"] in sh_modes for action in plan["actions"])
        assert any(action["type"] == "force_dhw_on" for action in plan["actions"])

        unknown = _Case(
            prices=case.prices,
            outdoor=case.outdoor,
            demand=[2.0, 0.0, 0.0, 2.0],
            gates=[ALLOWED, BLOCKED, UNKNOWN, ALLOWED],
            tank=44.0,
            deadlines=[3],
        )
        with pytest.raises(DataIncompleteError, match="unknown native SH gate"):
            unknown.solve()

    @pytest.mark.parametrize("trained", [False, True], ids=["fallback", "trained_ml"])
    def test_milp_gate_bounds_and_action_emission_preserve_dhw(self, trained):
        from packages.optimizer.milp import MILPOptimizer

        prices = [0.50, 0.50, 0.01, 0.50]
        weather = [(_BASE + dt.timedelta(hours=h), 10.0) for h in range(len(prices))]
        gates = [ALLOWED, BLOCKED, BLOCKED, ALLOWED]
        demand_model = MagicMock(is_trained=True)
        demand_model.predict_hourly.return_value = [2.0] * len(weather)
        optimizer = MILPOptimizer(demand_model=demand_model if trained else None)
        demand = optimizer._build_demand_estimates(weather, gate_states=gates)
        if trained:
            demand_model.predict_hourly.assert_called_once()
            assert demand == [2.0, 0.0, 0.0, 2.0]
        else:
            demand_model.predict_hourly.assert_not_called()
            assert demand == pytest.approx([24.0 / 7.0, 0.0, 0.0, 24.0 / 7.0])

        native = [value / 12.0 for value in demand]
        # Native load holds the room steady; diverting it in an ALLOWED hour would breach comfort.
        rates = [(1.0 - share, -share) if share else (0.0, 0.0) for share in native]
        case = _Case(
            prices=prices,
            demand=demand,
            gates=gates,
            room=19.5,
            room_floors=[19.5] * 4,
            rates=rates,
            tank=44.0,
            tank_on=9.5,
            tank_off=-0.5,
            tank_floors=[43.0, 43.0, 43.0, 48.0],
            tank_ceiling=49.0,
            deadlines=[4],
        )
        plan = case.solve(optimizer)

        hand = _assert_replayed(case, plan)
        assert hand.pulses == {2: 36}
        fractions = [
            point["space_heating_fraction"]
            for point in plan["forecast_snapshot"]["forecast_with_plan"]
        ]
        assert fractions[0] > 0
        assert fractions[1:3] == [0.0, 0.0]
        assert all(0.0 <= fraction <= 1.0 for fraction in fractions)
        room_heating_actions = {
            "eco_mode_on",
            "normal_mode_on",
            "comfort_mode_on",
            "quiet_mode_on",
            "quiet_mode_off",
        }
        assert not any(action["type"] in room_heating_actions for action in plan["actions"])
        assert any(action["type"] == "force_dhw_on" for action in plan["actions"])

        with pytest.raises(DataIncompleteError, match="known gate"):
            optimizer._build_demand_estimates(
                weather, gate_states=[ALLOWED, BLOCKED, UNKNOWN, ALLOWED]
            )

    def test_demand_weather_is_aligned_to_horizon_timestamps(self):
        """Out-of-order full weather rows must not shift demand features by an hour."""
        from packages.optimizer.milp import MILPOptimizer

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        weather = [(base, 1.0), (base + dt.timedelta(hours=1), 2.0)]
        weather_full = [
            {
                "ts": base + dt.timedelta(hours=1),
                "temperature": 11.0,
                "wind_speed": 9.0,
                "irradiance": 200.0,
            },
            {
                "ts": base,
                "temperature": 10.0,
                "wind_speed": 8.0,
                "irradiance": 100.0,
            },
            {
                "ts": base + dt.timedelta(hours=5),
                "temperature": 99.0,
                "wind_speed": 99.0,
                "irradiance": 999.0,
            },
        ]
        demand = MagicMock(is_trained=True)
        demand.predict_hourly.return_value = [1.0, 2.0]

        result = MILPOptimizer(demand_model=demand)._build_demand_estimates(
            weather, weather_full, gate_states=[ALLOWED, ALLOWED]
        )

        assert result == [1.0, 2.0]
        forecast, hours = demand.predict_hourly.call_args.args
        assert hours == 2
        assert [row["ts"] for row in forecast] == [weather[0][0], weather[1][0]]
        assert [row["temperature"] for row in forecast] == [10.0, 11.0]
        assert [row["wind_speed"] for row in forecast] == [8.0, 9.0]

    def test_comfort_rates_use_all_forecast_weather_features(self):
        """Wind and sun learned by the comfort model must reach the MILP."""
        from packages.optimizer.milp import MILPOptimizer

        ts = _make_prices(hours=1)[0][0]
        comfort = MagicMock()
        comfort.predict_indoor_temp.side_effect = [19.8, 20.2]

        with patch("packages.optimizer.milp.comfort_model", comfort):
            rates = MILPOptimizer._precompute_indoor_rates(
                prices=[(ts, 0.1)],
                weather=[(ts, 5.0)],
                current_indoor=20.0,
                heat_curve_water_temp=35.0,
                weather_full=[
                    {
                        "ts": ts,
                        "temperature": 7.0,
                        "wind_speed": 9.0,
                        "irradiance": 450.0,
                        "precipitation": 1.25,
                    }
                ],
            )

        assert rates == [(pytest.approx(0.2), pytest.approx(-0.2))]
        first_call = comfort.predict_indoor_temp.call_args_list[0].kwargs
        assert first_call["outdoor_temp"] == 7.0
        assert first_call["wind_speed"] == 9.0
        assert first_call["irradiance"] == 450.0
        assert first_call["precipitation"] == 1.25

    def test_comfort_rates_use_target_timestamp_weather(self):
        ts = _make_prices(hours=1)[0][0]
        comfort = MagicMock()
        comfort.predict_indoor_temp.side_effect = [19.8, 20.2]

        with patch("packages.optimizer.milp.comfort_model", comfort):
            MILPOptimizer._precompute_indoor_rates(
                prices=[(ts, 0.1)],
                weather=[(ts, 5.0)],
                current_indoor=20.0,
                heat_curve_water_temp=35.0,
                weather_full=[
                    {"ts": ts, "temperature": 1.0},
                    {"ts": ts + dt.timedelta(hours=1), "temperature": 9.0},
                ],
            )

        first_call = comfort.predict_indoor_temp.call_args_list[0].kwargs
        assert first_call["outdoor_temp"] == 9.0
        assert first_call["hour"] == (ts + dt.timedelta(hours=1)).hour

    def test_milp_infeasible_raises(self):
        """MILP should raise InfeasibleError when constraints are impossible."""
        import pulp

        calls = []
        original = pulp.LpProblem.solve

        def record_solve(problem, solver=None, **kwargs):
            calls.append(solver)
            return original(problem, solver, **kwargs)

        # A slow 1 °C/h heater cannot offset an absurd 50 °C/h standby loss for a whole
        # day within the 20-activation limit.
        common = {
            "prices": [price for _, price in _make_prices()],
            "demand": [3.0] * 24,
            "tank_on": 1.0,
            "cop": 0.5,
            "tank_floors": [45.0] * 24,
        }
        case = _Case(tank_off=-50.0, **common)
        with patch("pulp.LpProblem.solve", record_solve), pytest.raises(InfeasibleError):
            case.solve()
        assert len(calls) == 1

        ordinary_loss = _Case(tank_off=-0.1, **common)
        _assert_replayed(ordinary_loss, ordinary_loss.solve())

    def test_milp_solver_timeout_raises(self):
        """A CBC NotSolved status is a bounded timeout, never a returned plan."""
        import pulp

        calls = []

        def not_solved(problem, solver=None, **_kwargs):
            calls.append(solver)
            problem.status = pulp.LpStatusNotSolved
            return problem.status

        optimizer = MILPOptimizer()
        optimizer.SOLVER_TIMEOUT_SECONDS = 3
        with patch("pulp.LpProblem.solve", new=not_solved), pytest.raises(SolverTimeoutError):
            _day_case().solve(optimizer)
        assert len(calls) == 1
        assert calls[0].timeLimit == 3

    def test_milp_plan_actions_are_well_formed(self):
        """All plan actions must have ts, type, and payload keys."""
        case = _day_case()
        plan = case.solve()

        _assert_replayed(case, plan)
        assert plan["actions"]
        for action in plan["actions"]:
            assert "ts" in action, f"Action missing 'ts': {action}"
            assert "type" in action, f"Action missing 'type': {action}"
            assert "payload" in action, f"Action missing 'payload': {action}"
            # ts should be ISO-parseable
            dt.datetime.fromisoformat(action["ts"])
            # type must be a known action
            assert action["type"] in (
                "force_dhw_on",
                "force_dhw_off",
                "quiet_mode_on",
                "quiet_mode_off",
            ), f"Unknown action type: {action['type']}"

    def test_milp_ignores_retired_cop_model(self):
        """A supplied legacy COP object never enables inference or the ML tag."""
        from packages.ml.thermal import assumed_heating_efficiency
        from packages.optimizer.milp import MILPOptimizer

        class RetiredCOP:
            def __getattr__(self, name):
                raise AssertionError(f"COP access: {name}")

        milp = MILPOptimizer(cop_model=RetiredCOP())
        case = _k_case(cop=assumed_heating_efficiency(10.0))
        plan = case.solve(milp, cop_fn=milp._build_cop_function(None))

        _assert_replayed(case, plan)
        assert plan is not None
        assert plan["version"] == "milp_v1"
        assert plan["cop_source"] == "assumed"
        assert plan["cop_measured"] is False
        assert plan["heating_efficiency"] == {"source": "assumed", "measured": False}
        dhw_on = _dhw_actions(plan)
        assert dhw_on
        for action in dhw_on:
            assert action["payload"]["cop"] == pytest.approx(case.cop)
            assert action["payload"]["cop_source"] == "assumed"
            assert action["payload"]["cop_measured"] is False

    def test_assumed_efficiency_does_not_infer_cop_from_forecast(self):
        """Weather/tank arguments remain compatible but never enter COP inference."""
        from packages.optimizer.milp import MILPOptimizer

        mock_cop = MagicMock()
        mock_cop.is_trained = True
        mock_cop.predict_cop = MagicMock(side_effect=AssertionError("COP inference"))
        milp = MILPOptimizer(cop_model=mock_cop)
        ts = _make_prices()[0][0]

        cop_fn = milp._build_cop_function(None, [{"ts": ts, "precipitation": 2.5}])
        assert cop_fn(5.0, ts.hour) == 4.0
        mock_cop.predict_cop.assert_not_called()

    @pytest.mark.parametrize("trained", [False, True])
    def test_ml_suffix_only_reflects_retained_demand(self, trained):
        demand = MagicMock(is_trained=trained)
        milp = MILPOptimizer(demand_model=demand)
        case = _sh_case(prices=[0.1])
        plan = case.solve(milp, cop_fn=milp._build_cop_function(None))

        _assert_replayed(case, plan)
        assert plan["version"] == ("milp_v1+ml" if trained else "milp_v1")
        demand.predict_hourly.assert_not_called()

    def test_dhw_thermal_divides_once_and_sh_stays_electrical(self):
        """DHW heat is divided by COP once; native SH stays electrical and pauses during DHW."""
        case = _k_case()
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert hand.pulses == {0: 30}
        # DHW: (9.5 - -0.5) * 0.2 / 2 = 1 kW for 0.5 h; SH: 0.25 * 2 kW for 0.5 h + 1 h.
        assert hand.energy == pytest.approx(0.5 + 0.25 + 0.5)
        assert plan["cost_estimate"] == pytest.approx(0.275)
        assert plan["space_heating_demand_kwh"] == pytest.approx(1.0)
        dhw = _dhw_actions(plan)[0]
        assert dhw["payload"]["cop"] == pytest.approx(2.0)
        assert dhw["payload"]["dhw_power_kw"] == pytest.approx(1.0)
        assert dhw["payload"]["dhw_fraction"] == pytest.approx(0.5)
        assert dhw["payload"]["cop_source"] == "assumed"
        assert dhw["payload"]["cop_measured"] is False

    def test_milp_version_without_ml(self):
        """Version should be plain milp_v1 without ML models."""
        from packages.optimizer.milp import MILPOptimizer

        case = _day_case()
        plan = case.solve(MILPOptimizer())

        _assert_replayed(case, plan)
        assert plan is not None
        assert plan["version"] == "milp_v1"

    def test_milp_fast_heating_tank_feasible(self):
        """MILP should remain feasible with fast-heating tanks (20+ °C/h)."""
        # 20.59 °C/h heating, -3 °C/h standby loss (real-world values)
        case = _fast_case(20.59, -3.0)
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert plan["cost_estimate"] is not None
        assert hand.pulses == {1: 21}
        dhw_on = _dhw_actions(plan)
        assert len(dhw_on) > 0
        for action in dhw_on:
            minutes = action["payload"]["dhw_minutes"]
            assert type(minutes) is int
            assert 5 <= minutes <= 60

    def test_milp_fast_tank_dhw_fraction_payload(self):
        """DHW actions from fast-tank MILP should include fraction and duration."""
        case = _fast_case(20.59, -3.0)
        plan = case.solve()

        _assert_replayed(case, plan)
        dhw_on = _dhw_actions(plan)
        assert dhw_on
        dhw_off_timestamps = {
            action["ts"] for action in plan["actions"] if action["type"] == "force_dhw_off"
        }
        for action in dhw_on:
            assert 0 < action["payload"]["dhw_fraction"] <= 1.0
            assert 5 <= action["payload"]["dhw_minutes"] <= 60
            assert action["payload"]["dhw_fraction"] == pytest.approx(
                action["payload"]["dhw_minutes"] / 60
            )
            assert (
                dt.datetime.fromisoformat(action["ts"])
                + dt.timedelta(minutes=action["payload"]["dhw_minutes"])
            ).isoformat() in dhw_off_timestamps

    @pytest.mark.parametrize(
        ("heating_rate", "standby_loss", "expected_minutes"),
        [(20.59, -3.0, 21), (25.0, -2.0, 12), (30.0, -1.0, 5)],
        ids=["measured_fast_tank", "high_gain", "very_high_gain"],
    )
    def test_milp_fast_tank_regressions_use_strict_fractional_dhw(
        self, heating_rate, standby_loss, expected_minutes
    ):
        """Former fast-tank infeasibilities solve once with a partial-hour integer pulse."""
        import pulp

        calls = []
        original = pulp.LpProblem.solve

        def record_solve(problem, solver=None, **kwargs):
            calls.append(solver)
            return original(problem, solver, **kwargs)

        case = _fast_case(heating_rate, standby_loss)
        with patch("pulp.LpProblem.solve", record_solve):
            plan = case.solve()

        assert len(calls) == 1
        hand = _assert_replayed(case, plan)
        assert hand.pulses == {1: expected_minutes}
        fractions = [action["payload"]["dhw_fraction"] for action in _dhw_actions(plan)]
        assert fractions == pytest.approx([expected_minutes / 60])
        assert all(0 < fraction < 1 for fraction in fractions)

    def test_milp_fractional_fallback_avoids_tied_price_micro_activations(self):
        """A flat-price schedule is not split into many tied-price micro activations."""
        case = _Case(
            prices=[0.10] * 24,
            outdoor=[5.0] * 24,
            tank_on=20.59,
            tank_off=-3.0,
            default_tank_floor=True,
        )
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        dhw_on = _dhw_actions(plan)
        assert dhw_on
        assert len(hand.pulses) == len(dhw_on)
        assert len(dhw_on) <= 9

    def test_milp_flat_price_matches_independent_eight_pulse_176_minute_witness(self):
        """The original 24h flat-price fixture admits an independently checked witness.

        Runtime lower bound: the tank does not lose heat while DHW runs, so ending at
        or above the floor needs 48 + 20.59 t - 3 (24 - t) >= 45, i.e. t >= 175.5 min.
        The witness reaches that bound with eight pulses; the solver must match its
        runtime and monetary cost without exceeding nine activations.
        """
        import math

        import pulp

        from packages.core.config import settings

        case = _Case(
            prices=[0.10] * 24,
            outdoor=[5.0] * 24,
            tank_on=20.59,
            tank_off=-3.0,
            default_tank_floor=True,
        )
        assert case.tank_floors == [45.0] * 24 == [float(settings.tank_min_temp)] * 24
        minimum_minutes = math.ceil(
            60 * (case.tank_floors[-1] - case.tank - 24 * case.tank_off) / (20.59 - case.tank_off)
        )
        assert minimum_minutes == 176

        schedule = {hour: 23 for hour in (1, 4, 7, 10, 13, 16, 19)} | {22: 15}
        witness = [action for hour, minutes in schedule.items() for action in _pulse(hour, minutes)]
        dhw_power = (20.59 - case.tank_off) * case.capacity / case.cop
        witness_cost = sum(
            dhw_power * minutes / 60 * case.prices[hour] for hour, minutes in schedule.items()
        )
        # Independent replay checks every segment end against floor, ceiling and target.
        witness_hand = case.replay(witness)
        assert witness_hand.pulses == schedule
        assert sum(schedule.values()) == minimum_minutes
        assert witness_hand.cost == pytest.approx(witness_cost, abs=_TOLERANCE)
        assert min(witness_hand.tank_ends) >= 45.0 - _TOLERANCE
        assert max(witness_hand.peaks.values()) <= case.tank_ceiling

        calls = []
        original = pulp.LpProblem.solve

        def counted(problem, solver=None, **kwargs):
            calls.append(solver)
            return original(problem, solver, **kwargs)

        with patch("pulp.LpProblem.solve", counted):
            plan = case.solve()

        assert len(calls) == 1
        hand = _assert_replayed(case, plan)
        assert sum(hand.pulses.values()) == minimum_minutes
        assert hand.cost == pytest.approx(witness_cost, abs=_TOLERANCE)
        assert plan["cost_estimate"] == pytest.approx(witness_cost, abs=_TOLERANCE)
        assert len(hand.pulses) <= 9

    def test_milp_offpeak_lower_tank_floor(self):
        """Off-peak hours should allow tank to drop below normal min."""
        # Hours 0-6 and 22-23 are off-peak (41°C), rest comfort (45°C)
        tank_min_per_hour = [41.0] * 7 + [45.0] * 15 + [41.0] * 2
        # The tank starts at 42°C: above the off-peak floor, below the comfort floor.
        case = _day_case(tank=42.0, tank_floors=tank_min_per_hour, tank_ceiling=55.0)
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert plan["version"] == "milp_v1"
        assert min(hand.tank_ends[:7]) < 45.0
        assert all(tank >= 45.0 - _TOLERANCE for tank in hand.tank_ends[7:22])

    def test_milp_comfort_hours_use_normal_floor(self):
        """During comfort hours the normal tank_min_temp should be enforced."""
        case = _day_case(tank_floors=[45.0] * 24, tank_ceiling=55.0)
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        # DHW should still be scheduled to maintain the 45°C floor
        assert len(_dhw_actions(plan)) > 0
        assert all(45.0 - _TOLERANCE <= tank <= 55.0 for tank in hand.tank_ends)
        assert all(45.0 - _TOLERANCE <= peak <= 55.0 for peak in hand.peaks.values())

    def test_milp_offpeak_backward_compat_no_per_hour(self):
        """When tank_min_per_hour is None, falls back to settings constants."""
        from packages.core.config import settings

        case = _day_case(default_tank_floor=True)
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert case.tank_floors == [float(settings.tank_min_temp)] * 24
        assert plan["version"] == "milp_v1"
        assert all(tank >= settings.tank_min_temp - _TOLERANCE for tank in hand.tank_ends)

    def test_milp_cold_house_below_comfort_stays_feasible(self):
        """A cold start is certified only when the declared native trace can recover it.

        Starting at 16°C against a 21°C floor cannot be recovered within the first hour,
        so the represented trace is refused rather than clamped. A reachable cold start
        is accepted and discloses its initial violation.
        """
        unreachable = _sh_case(
            prices=[0.1],
            demand=[12.0],
            rates=[(1.2, -0.1)],
            room=16.0,
            room_floors=[21.0],
        )
        with pytest.raises(InfeasibleError):
            unreachable.solve()

        case = _sh_case(
            prices=[0.1],
            demand=[12.0],
            rates=[(1.2, -0.1)],
            room=19.0,
            room_floors=[20.0],
        )
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert plan["engine"] == "milp"
        assert hand.room_ends == pytest.approx([20.2])
        assert plan["cost_estimate"] == pytest.approx(1.2)
        assert plan["initial_violations"]["indoor_below_floor"] is True
        assert plan["comfort_shortfall"] == 0.0
        temps_forecast = [f["predicted_indoor_temp"] for f in plan["indoor_forecast"]]
        assert temps_forecast == pytest.approx([20.2])
        assert temps_forecast[-1] >= case.room

    def test_milp_comfort_floor_respected_when_reachable(self):
        """When the house already meets the target, no shortfall should remain."""
        case = _sh_case(prices=[0.1, 0.1], room=21.5, room_floors=[20.0, 20.0])
        plan = case.solve()

        hand = _assert_replayed(case, plan)
        assert min(hand.room_ends) >= 20.0
        assert plan["comfort_shortfall"] == 0.0
        assert [point["target"] for point in plan["indoor_forecast"]] == [20.0, 20.0]


class TestDhwDeadlineOffsets:
    @pytest.mark.parametrize(
        ("horizon_start", "expected_offset"),
        [
            (dt.datetime(2026, 6, 1, 14, tzinfo=dt.timezone.utc), 1),
            (dt.datetime(2026, 1, 5, 14, tzinfo=dt.timezone.utc), 2),
        ],
    )
    def test_local_deadline_uses_utc_horizon_offset(self, horizon_start, expected_offset):
        from packages.optimizer.milp import _dhw_deadline_offsets

        assert _dhw_deadline_offsets(
            {"weekday": [17], "weekend": [17]}, horizon_start, "Europe/Amsterdam"
        ) == [expected_offset]

    def test_deadline_after_local_midnight_uses_next_day_offset(self):
        from packages.optimizer.milp import _dhw_deadline_offsets

        horizon_start = dt.datetime(2026, 6, 1, 21, tzinfo=dt.timezone.utc)

        assert _dhw_deadline_offsets(
            {"weekday": [0], "weekend": [0]}, horizon_start, "Europe/Amsterdam"
        ) == [1]


class TestMILPGeneratePlan:
    """Tests for the async generate_plan interface."""

    @pytest.mark.asyncio
    async def test_missing_prices_raises_data_incomplete(self):
        """An empty frozen planning batch refuses the plan before the solver is reached."""
        from sqlalchemy.sql import Select

        from packages.optimizer.milp import MILPOptimizer

        class _Rows:
            def scalars(self):
                return self

            def all(self):
                return []

            def scalar_one_or_none(self):
                return None

        class _ReadOnlySession:
            def __init__(self):
                self.info = {}
                self.statements = []

            async def execute(self, statement, *args, **kwargs):
                assert isinstance(statement, Select), statement
                self.statements.append(statement)
                return _Rows()

        session = _ReadOnlySession()
        sessions = []

        @contextlib.asynccontextmanager
        async def fake_get_session(**kwargs):
            sessions.append(kwargs)
            yield session

        milp = MILPOptimizer()
        with (
            patch("packages.optimizer.milp.get_session", fake_get_session),
            patch(
                "packages.core.planning_data_quality.get_all_settings",
                new=AsyncMock(
                    return_value={
                        "price_provider": "tibber",
                        "entsoe_area": "SE3",
                        "weather_provider": "open-meteo",
                    }
                ),
            ),
            patch.object(
                MILPOptimizer, "_solve", side_effect=AssertionError("solver must not run")
            ),
            pytest.raises(DataIncompleteError, match="No price data"),
        ):
            await milp.generate_plan()

        assert sessions == [{"read_only": True, "isolation_level": "REPEATABLE READ"}]
        batch = session.info["planning_input_batch"]
        assert batch.prices == ()
        assert batch.weather == ()
        assert session.statements

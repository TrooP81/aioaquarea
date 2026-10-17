"""Conditional DHW timing with an unchanged, explicitly assumed native SH trace."""

from __future__ import annotations

import datetime as dt
import math
from copy import deepcopy

from packages.core.device_status_values import DeviceStatusValues

from packages.optimizer import DataIncompleteError, InfeasibleError, SolverTimeoutError

SCHEMA = "native_sh_trace_v1"
TOLERANCE = 1e-5
NATIVE_ASSUMPTION = "unchanged_hourly_load_over_declared_max_electrical_power"


async def observation_context(status: DeviceStatusValues | None, captured_at):
    """Bind cached tank evidence to one device and the caller's captured clock."""
    from packages.core.settings_service import get_setting
    from packages.core.device_data_quality import device_status_clock_issue

    try:
        minutes = min(60, max(5, int(await get_setting("device_status_max_age_minutes"))))
        interval = max(0, int(await get_setting("poll_interval_seconds")))
    except (ValueError, TypeError) as error:
        raise DataIncompleteError("Device status freshness policy unavailable") from error
    max_age = max(minutes * 60, 3 * interval)
    if device_status_clock_issue(status, as_of=captured_at, max_age_seconds=max_age):
        raise DataIncompleteError("Cached device status clocks are not admissible")
    provenance = getattr(status, "native_policy_provenance", None)
    declarations = (
        provenance.get("diagnostic_assumptions", {}) if isinstance(provenance, dict) else {}
    )
    if not isinstance(declarations, dict):
        raise DataIncompleteError("Diagnostic assumptions unavailable")
    return {
        "device_id": getattr(status, "device_id", None),
        "observed_at": getattr(status, "ts", None),
        "captured_at": captured_at,
        "tank_target_c": getattr(status, "tank_target_temp", None),
        "force_dhw": getattr(status, "force_dhw", None),
        "max_age_seconds": max_age,
        "tank_hardware_floor_c": getattr(status, "tank_heat_min", None),
        # Observations do not establish compressor duty or absence of auxiliary load.
        "native_sh_provenance": deepcopy(declarations.get("native_sh_provenance")),
        "auxiliary_power_kw": declarations.get("auxiliary_power_kw"),
    }


def assumed_native_demand(weather, gate_states, provenance):
    from packages.core.config import settings

    if len(gate_states) != len(weather) or any(
        state not in {"ALLOWED", "BLOCKED"} for state in gate_states
    ):
        raise DataIncompleteError("Native SH demand requires complete known gate coverage")
    demand = provenance.get("hourly_electrical_kw") if isinstance(provenance, dict) else None
    if not isinstance(demand, list) or len(demand) != len(weather):
        raise DataIncompleteError("Native SH provenance requires explicit hourly electrical load")
    if any(
        not 0 <= _number(load) <= settings.sh_max_power_kw or (state == "BLOCKED" and load != 0)
        for load, state in zip(demand, gate_states)
    ):
        raise DataIncompleteError("Declared native SH load contradicts power or gate")
    return list(demand)


def build_represented_plan(
    *,
    prices,
    weather,
    cop_fn,
    demand_per_hour,
    current_tank_temp,
    current_indoor_temp,
    gate_states,
    hardware_tank_ceiling,
    context,
    dhw_deadlines=None,
    dhw_deadline_tank_floors_c=None,
    indoor_targets=None,
    tank_min_per_hour=None,
    tank_max_temp_setting=None,
    indoor_rates=None,
    indoor_max_temp_setting=None,
    forecast_bias_corrections_c=None,
    reported_indoor_targets=None,
    serialized_actions=None,
    timeout_seconds=40,
    version="milp_v2",
):
    """Solver-independent trace construction and serialized-command reporting."""
    from packages.core.config import settings
    from packages.ml.thermal import thermal_model

    hours = len(prices)
    if not hours or any(len(values) != hours for values in (weather, demand_per_hour, gate_states)):
        raise DataIncompleteError("Incomplete physical, gate or forecast horizon")
    for values in (
        indoor_rates,
        indoor_targets,
        tank_min_per_hour,
        forecast_bias_corrections_c,
        reported_indoor_targets,
    ):
        if values is not None and len(values) != hours:
            raise DataIncompleteError("Incomplete physical, gate or forecast horizon")
    power = _number(settings.sh_max_power_kw)
    if power <= 0:
        raise DataIncompleteError("Native SH electrical power unavailable")
    ceiling = min(
        _number(hardware_tank_ceiling),
        _number(
            tank_max_temp_setting if tank_max_temp_setting is not None else settings.tank_max_temp
        ),
    )
    hardware_floor = _number(context.get("tank_hardware_floor_c"))
    room_ceiling = _number(
        indoor_max_temp_setting
        if indoor_max_temp_setting is not None
        else settings.comfort_temp_max
    )
    rows = []
    for hour, ((timestamp, price), (weather_ts, outdoor), demand, gate) in enumerate(
        zip(prices, weather, demand_per_hour, gate_states)
    ):
        outdoor, demand = _number(outdoor), _number(demand)
        if timestamp != weather_ts or not 0 <= demand <= power:
            raise DataIncompleteError("Unaligned weather or invalid native demand")
        heat = (
            thermal_model._tank_heating_rate(outdoor)
            if thermal_model.params.last_calibrated
            else 5.0
        )
        loss = (
            thermal_model._tank_loss_rate(outdoor) if thermal_model.params.last_calibrated else -0.5
        )
        gain, passive = (
            indoor_rates[hour]
            if indoor_rates is not None
            else (
                thermal_model._indoor_heating_rate(outdoor),
                thermal_model._indoor_cooling_rate(outdoor),
            )
        )
        cop = _number(cop_fn(outdoor, timestamp.hour))
        if cop <= 0:
            raise DataIncompleteError("Assumed heating efficiency must be positive")
        rows.append(
            {
                "ts": timestamp.isoformat(),
                "price_per_kwh": _number(price),
                "gate_state": str(gate),
                "native_sh_fraction": demand / power,
                "native_sh_power_kw": power,
                "dhw_power_kw": (_number(heat) - _number(loss))
                * settings.tank_kwh_per_degree
                / cop,
                "tank_net_heating_c_per_h": _number(heat),
                "tank_loss_c_per_h": _number(loss),
                "indoor_full_heat_c_per_h": _number(gain),
                "indoor_passive_c_per_h": _number(passive),
                "tank_floor_c": max(
                    hardware_floor,
                    _number(
                        tank_min_per_hour[hour]
                        if tank_min_per_hour is not None
                        else settings.tank_min_temp
                    ),
                ),
                "indoor_floor_c": _number(
                    indoor_targets[hour]
                    if indoor_targets is not None
                    else settings.comfort_temp_min
                ),
                "indoor_ceiling_c": room_ceiling
                - (
                    _number(forecast_bias_corrections_c[hour]) if forecast_bias_corrections_c else 0
                ),
                "forecast_bias_correction_c": (
                    _number(forecast_bias_corrections_c[hour]) if forecast_bias_corrections_c else 0
                ),
            }
        )
    trace = {
        "schema": SCHEMA,
        "native_sh_basis": "assumed",
        "native_sh_assumption": NATIVE_ASSUMPTION,
        "native_sh_provenance": deepcopy(context.get("native_sh_provenance")),
        "auxiliary_power_kw": context.get("auxiliary_power_kw"),
        "device_id": context.get("device_id"),
        "observation_context": {
            key: (
                _instant(context.get(key)).isoformat()
                if key in {"observed_at", "captured_at"}
                else context.get(key)
            )
            for key in ("device_id", "observed_at", "captured_at", "max_age_seconds")
        },
        "observed_tank_target_c": _number(context.get("tank_target_c")),
        "observed_force_dhw": context.get("force_dhw"),
        "initial_tank_c": _number(current_tank_temp),
        "initial_indoor_c": _number(current_indoor_temp),
        "tank_ceiling_c": ceiling,
        "tank_hardware_floor_c": hardware_floor,
        "indoor_ceiling_c": room_ceiling,
        "deadline_hours": list(dhw_deadlines or []),
        # Ready-by requirements are not sampled from hourly slot-start floors:
        # comfort may begin only at the final priced endpoint.
        "deadline_tank_floors_c": (
            [max(hardware_floor, _number(floor)) for floor in dhw_deadline_tank_floors_c]
            if dhw_deadline_tank_floors_c is not None
            else ([] if not dhw_deadlines else None)
        ),
        "price_currency": None,
        "hours": rows,
    }
    if serialized_actions is None:
        actions, replay = solve_represented_trace(trace, timeout_seconds=timeout_seconds)
    else:
        actions = serialized_actions
        if any(
            action.get("device_id", trace["device_id"]) != trace["device_id"] for action in actions
        ):
            raise DataIncompleteError("Serialized command device differs from observation")
        replay = replay_serialized_actions(trace, actions)
    forecast = []
    passive = _number(current_indoor_temp)
    counterfactual = []
    for hour, row in enumerate(rows):
        instant = (_instant(row["ts"]) + dt.timedelta(hours=1)).isoformat()
        state = next(item for item in replay["states"] if item["ts"] == instant)
        bias = forecast_bias_corrections_c[hour] if forecast_bias_corrections_c else 0
        forecast.append(
            {
                "hour": hour + 1,
                "ts": instant,
                "predicted_indoor_temp": state["indoor_c"] + bias,
                "source": "milp_solution",
            }
        )
        passive += row["indoor_passive_c_per_h"]
        counterfactual.append(
            {
                "hour": hour + 1,
                "ts": instant,
                "predicted_indoor_temp": passive + bias,
                "source": "milp_counterfactual",
            }
        )
    snapshot = {
        "version": "indoor_forecast_v4",
        "current_indoor": current_indoor_temp,
        "forecast": forecast,
        "forecast_with_plan": forecast,
        "forecast_no_heating": counterfactual,
        "no_heating_basis": "fully_passive_no_SH_no_DHW",
        "price_forecast": [
            {
                "ts": row["ts"],
                "price_eur_per_kwh": row["price_per_kwh"],
                "price_per_kwh": row["price_per_kwh"],
            }
            for row in rows
        ],
        "weather_forecast": [
            {"ts": (timestamp + dt.timedelta(hours=1)).isoformat(), "outdoor_temp": temp}
            for timestamp, temp in weather
        ],
        "target_schedule": [
            {
                "ts": forecast[hour]["ts"],
                "hour": hour + 1,
                "target": (
                    reported_indoor_targets[hour]
                    if reported_indoor_targets is not None
                    else row["indoor_floor_c"]
                ),
            }
            for hour, row in enumerate(rows)
        ],
        "represented_strategy": trace,
        "serialized_replay": replay,
    }
    return {
        "horizon_start": prices[0][0],
        "horizon_end": prices[-1][0] + dt.timedelta(hours=1),
        "actions": actions,
        "version": version,
        "engine": "milp",
        "diagnostic_status": "conditional_feasible",
        "represented_strategy": trace,
        "serialized_replay": replay,
        "diagnostic_strategy_actions": actions,
        "cost_estimate": replay["cost"],
        "cost_accounting": {
            "basis": "independent_serialized_event_replay",
            "electrical_kwh": replay["electricity_kwh"],
            "price_currency": None,
            "assurance": replay["assurance"],
        },
        "cop_source": "assumed",
        "cop_measured": False,
        "heating_efficiency": {"source": "assumed", "measured": False},
        "forecast_snapshot": snapshot,
    }


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DataIncompleteError("Represented strategy requires finite numeric inputs")
    return float(value)


def _instant(value):
    try:
        result = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise DataIncompleteError("Represented strategy requires aware instants") from error
    if not isinstance(result, dt.datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise DataIncompleteError("Represented strategy requires aware instants")
    return result.astimezone(dt.timezone.utc)


def validate_trace(trace):
    if not isinstance(trace, dict) or trace.get("schema") != SCHEMA:
        raise DataIncompleteError("Native SH trace provenance unavailable")
    # No historical trace reader currently proves selection, coverage and native-only duty.
    # A history label alone must not acquire the conditional assumed-trace contract.
    provenance = trace.get("native_sh_provenance")
    if (
        trace.get("native_sh_basis") != "assumed"
        or trace.get("native_sh_assumption") != NATIVE_ASSUMPTION
        or not isinstance(provenance, dict)
        or provenance.get("kind") != "explicit_conditional_assumption"
        or provenance.get("method") != NATIVE_ASSUMPTION
    ):
        raise DataIncompleteError("Native SH trace provenance unavailable or history unproven")
    rows = trace.get("hours")
    if not isinstance(rows, list) or not rows:
        raise DataIncompleteError("Complete native SH horizon required")
    if any(not isinstance(row, dict) for row in rows):
        raise DataIncompleteError("Native SH horizon requires physical rows")
    if trace.get("auxiliary_power_kw") is None or _number(trace.get("auxiliary_power_kw")) != 0:
        raise DataIncompleteError("Only explicit ZERO auxiliary load is represented")
    declared_loads = provenance.get("hourly_electrical_kw")
    if not isinstance(declared_loads, list) or len(declared_loads) != len(rows):
        raise DataIncompleteError("Native SH provenance requires complete hourly electrical load")
    declared_power = _number(provenance.get("maximum_electrical_power_kw"))
    for name in (
        "initial_tank_c",
        "initial_indoor_c",
        "tank_ceiling_c",
        "tank_hardware_floor_c",
        "indoor_ceiling_c",
    ):
        _number(trace.get(name))
    target = _number(trace.get("observed_tank_target_c"))
    context = trace.get("observation_context", {})
    if not isinstance(context, dict):
        raise DataIncompleteError("Represented observation context unavailable")
    if not context.get("device_id") or context.get("device_id") != trace.get("device_id"):
        raise DataIncompleteError("Represented observation device identity unavailable")
    observed = _instant(context.get("observed_at"))
    captured = _instant(context.get("captured_at"))
    if (
        provenance.get("device_id") != trace.get("device_id")
        or not observed <= _instant(provenance.get("captured_at")) <= captured
        or _instant(provenance.get("horizon_start")) != _instant(rows[0].get("ts"))
        or _instant(provenance.get("horizon_end"))
        != _instant(rows[-1].get("ts")) + dt.timedelta(hours=1)
    ):
        raise DataIncompleteError("Native SH provenance differs from device, clock or horizon")
    max_age = _number(context.get("max_age_seconds"))
    if max_age <= 0 or not dt.timedelta(0) <= captured - observed <= dt.timedelta(seconds=max_age):
        raise DataIncompleteError("Represented observation clock is future or stale")
    if (
        captured > _instant(rows[0]["ts"])
        or type(trace.get("observed_force_dhw")) is not int
        or trace["observed_force_dhw"] != 0
    ):
        raise DataIncompleteError("Represented DHW state is unknown, active or not horizon-bound")
    if target <= 0:
        raise DataIncompleteError("Observed tank target unavailable")
    for index, row in enumerate(rows):
        start = _instant(row.get("ts"))
        if start.minute or start.second or start.microsecond:
            raise DataIncompleteError("Trace requires complete market-price hours")
        if index and start != _instant(rows[index - 1]["ts"]) + dt.timedelta(hours=1):
            raise DataIncompleteError("Native SH trace must contain contiguous priced hours")
        for name in (
            "price_per_kwh",
            "native_sh_fraction",
            "native_sh_power_kw",
            "dhw_power_kw",
            "tank_net_heating_c_per_h",
            "tank_loss_c_per_h",
            "indoor_full_heat_c_per_h",
            "indoor_passive_c_per_h",
            "tank_floor_c",
            "indoor_floor_c",
        ):
            _number(row.get(name))
        if row.get("gate_state") not in {"ALLOWED", "BLOCKED"}:
            raise DataIncompleteError("Missing or unknown native SH gate")
        if (
            declared_power <= 0
            or row["native_sh_power_kw"] != declared_power
            or abs(
                _number(declared_loads[index])
                - row["native_sh_fraction"] * row["native_sh_power_kw"]
            )
            > TOLERANCE
        ):
            raise DataIncompleteError(
                "Native SH provenance contradicts represented electrical load"
            )
        if not 0 <= row["native_sh_fraction"] <= 1 or (
            row["gate_state"] != "ALLOWED" and row["native_sh_fraction"] != 0
        ):
            raise DataIncompleteError("Native SH trace contradicts controller gate")
        if (
            row["native_sh_power_kw"] <= 0
            or row["dhw_power_kw"] <= 0
            or row["tank_net_heating_c_per_h"] <= 0
            or row["tank_loss_c_per_h"] > 0
            or row["tank_floor_c"] > trace["tank_ceiling_c"]
            or row["tank_floor_c"] < trace["tank_hardware_floor_c"]
            or row["indoor_floor_c"] > trace["indoor_ceiling_c"]
        ):
            raise DataIncompleteError("Invalid represented physical bounds")
        ceiling = _number(row.get("indoor_ceiling_c", trace["indoor_ceiling_c"]))
        if row["indoor_floor_c"] > ceiling:
            raise DataIncompleteError("Indoor floor exceeds corrected upper comfort bound")
    deadlines = trace.get("deadline_hours")
    floors = trace.get("deadline_tank_floors_c")
    if (
        not isinstance(deadlines, list)
        or not isinstance(floors, list)
        or len(deadlines) != len(floors)
    ):
        raise DataIncompleteError("Complete explicit DHW deadline floors required")
    for deadline, floor in zip(deadlines, floors):
        if type(deadline) is not int or not 0 < deadline <= len(rows):
            raise DataIncompleteError("DHW deadline outside represented horizon")
        if not trace["tank_hardware_floor_c"] <= _number(floor) <= trace["tank_ceiling_c"]:
            raise DataIncompleteError("DHW deadline floor outside represented hardware bounds")
    if len(set(deadlines)) != len(deadlines):
        raise DataIncompleteError("Duplicate DHW deadline requirement")
    return rows


def replay_serialized_actions(trace, actions):
    """Rebuild continuous piecewise-linear states solely from serialized on/off commands."""
    rows = validate_trace(trace)
    if not isinstance(actions, list) or any(
        not isinstance(action, dict)
        or not isinstance(action.get("payload", {}), dict)
        or not isinstance(action.get("type"), str)
        or action.get("ts") is None
        for action in actions
    ):
        raise DataIncompleteError("Serialized command contract unavailable")
    horizon_start = _instant(rows[0]["ts"])
    horizon_end = horizon_start + dt.timedelta(hours=len(rows))
    pulses = []
    active = None
    seen = set()
    for action in sorted(
        actions,
        key=lambda item: (_instant(item["ts"]), 0 if item["type"] == "force_dhw_off" else 1),
    ):
        if action.get("device_id", trace["device_id"]) != trace["device_id"]:
            raise DataIncompleteError("Serialized command device differs from observation")
        instant = _instant(action["ts"])
        if not horizon_start <= instant <= horizon_end:
            raise InfeasibleError("Serialized action outside complete priced horizon")
        if action["type"] == "force_dhw_on":
            key = action.get("action_key")
            if active is not None or not key or key in seen:
                raise InfeasibleError("Overlapping or unidentified DHW pulse")
            if (instant - horizon_start).total_seconds() % 3600:
                raise InfeasibleError("DHW pulse must start on its represented hour")
            active = (instant, key, action.get("payload", {}).get("dhw_minutes"))
            seen.add(key)
        elif action["type"] == "force_dhw_off":
            if active is None or action.get("reverts_action_key") != active[1]:
                raise InfeasibleError("DHW restore is not linked to its serialized source")
            minutes = (instant - active[0]).total_seconds() / 60
            if (
                minutes != round(minutes)
                or not 5 <= minutes <= len(rows) * 60
                or type(active[2]) is not int
                or active[2] != int(minutes)
            ):
                raise InfeasibleError("DHW pulse is not an exact represented integer runtime")
            pulses.append((active[0], instant))
            active = None
        else:
            raise InfeasibleError("ECO, quiet and target strategies have unproven duty semantics")
    if active is not None or len(pulses) > 20:
        raise InfeasibleError("Unrestored or excessive DHW pulses")
    tank, indoor = trace["initial_tank_c"], trace["initial_indoor_c"]
    states = [{"ts": horizon_start.isoformat(), "tank_c": tank, "indoor_c": indoor}]
    cost = electricity = 0.0
    initial = {
        "tank_below_floor": tank < rows[0]["tank_floor_c"],
        "tank_above_ceiling": tank > trace["tank_ceiling_c"],
        "indoor_below_floor": indoor < rows[0]["indoor_floor_c"],
        "indoor_above_ceiling": indoor > rows[0].get("indoor_ceiling_c", trace["indoor_ceiling_c"]),
    }
    deadline_floors = dict(zip(trace["deadline_hours"], trace["deadline_tank_floors_c"]))
    for index, row in enumerate(rows):
        start = _instant(row["ts"])
        end = start + dt.timedelta(hours=1)
        pulse = next((item for item in pulses if item[0] <= start < item[1]), None)
        boundaries = [pulse[1], end] if pulse and pulse[1] < end else [end]
        segment_start = start
        for boundary in boundaries:
            dhw = pulse is not None and segment_start < pulse[1]
            duration = (boundary - segment_start).total_seconds() / 3600
            if dhw and tank >= trace["observed_tank_target_c"] - 0.5:
                raise DataIncompleteError("DHW command suppressed at observed tank target")
            sh_fraction = 0.0 if dhw else row["native_sh_fraction"]
            tank += duration * (
                row["tank_net_heating_c_per_h"] if dhw else row["tank_loss_c_per_h"]
            )
            if dhw and tank >= trace["observed_tank_target_c"] - 0.5:
                raise DataIncompleteError("DHW target termination makes pulse effect unproven")
            indoor += duration * (
                row["indoor_passive_c_per_h"]
                + sh_fraction * (row["indoor_full_heat_c_per_h"] - row["indoor_passive_c_per_h"])
            )
            power = row["dhw_power_kw"] if dhw else (row["native_sh_power_kw"] * sh_fraction)
            energy = power * duration
            electricity += energy
            cost += energy * row["price_per_kwh"]
            if not row["tank_floor_c"] - TOLERANCE <= tank <= trace["tank_ceiling_c"] + TOLERANCE:
                raise InfeasibleError("Serialized pulse violates tank floor or ceiling")
            if (
                not row["indoor_floor_c"] - TOLERANCE
                <= indoor
                <= (row.get("indoor_ceiling_c", trace["indoor_ceiling_c"]) + TOLERANCE)
            ):
                raise InfeasibleError("Serialized diversion violates indoor comfort bounds")
            states.append(
                {
                    "ts": boundary.isoformat(),
                    "tank_c": tank,
                    "indoor_c": indoor,
                    "dhw_active": dhw,
                    "space_heating_fraction": sh_fraction,
                    "electricity_kwh": energy,
                }
            )
            segment_start = boundary
        if index + 1 in deadline_floors and tank < deadline_floors[index + 1] - TOLERANCE:
            raise InfeasibleError("Serialized strategy misses DHW deadline")
    return {
        "schema": SCHEMA,
        "states": states,
        "electricity_kwh": electricity,
        "cost": cost,
        "initial_violations": initial,
        "assurance": "modeled_conditional_not_physical_or_global_optimality",
        "native_sh_basis": trace["native_sh_basis"],
        "price_currency": trace.get("price_currency"),
        "strategy_supported": True,
    }


def validate_represented_plan(plan):
    """Recheck the serialized diagnostic DTO before it can be persisted as priced evidence."""
    if plan.get("diagnostic_status") != "conditional_feasible":
        raise DataIncompleteError("Diagnostic strategy is unproven")
    trace = plan.get("represented_strategy")
    replay = replay_serialized_actions(trace, plan.get("diagnostic_strategy_actions"))
    if (
        _instant(plan.get("horizon_start")) != _instant(trace["hours"][0]["ts"])
        or _instant(plan.get("horizon_end"))
        != _instant(trace["hours"][-1]["ts"]) + dt.timedelta(hours=1)
        or (
            plan.get("actions") != plan["diagnostic_strategy_actions"]
            and not (plan.get("diagnostic") is True and plan.get("actions") == [])
        )
    ):
        raise DataIncompleteError("Diagnostic commands or horizon differ from represented trace")
    stored = plan.get("serialized_replay")
    if not isinstance(stored, dict) or stored != replay:
        raise DataIncompleteError("Diagnostic serialized replay differs from commands")
    if abs(_number(plan.get("cost_estimate")) - replay["cost"]) > TOLERANCE:
        raise DataIncompleteError("Diagnostic cost differs from serialized electrical energy")
    accounting = plan.get("cost_accounting", {})
    if (
        not isinstance(accounting, dict)
        or abs(_number(accounting.get("electrical_kwh")) - replay["electricity_kwh"]) > TOLERANCE
        or accounting.get("price_currency") != replay["price_currency"]
    ):
        raise DataIncompleteError("Diagnostic accounting differs from serialized electrical energy")
    snapshot = plan.get("forecast_snapshot", {})
    if not isinstance(snapshot, dict):
        raise DataIncompleteError("Diagnostic trajectory unavailable")
    if snapshot.get("represented_strategy") != trace or snapshot.get("serialized_replay") != stored:
        raise DataIncompleteError("Diagnostic snapshot differs from represented trace or replay")
    for name in ("forecast", "forecast_with_plan"):
        points = snapshot.get(name)
        if not isinstance(points, list) or len(points) != len(trace["hours"]):
            raise DataIncompleteError("Diagnostic trajectory has incomplete horizon")
        for row, point in zip(trace["hours"], points):
            end = _instant(row["ts"]) + dt.timedelta(hours=1)
            state = next(item for item in replay["states"] if _instant(item["ts"]) == end)
            if (
                not isinstance(point, dict)
                or _instant(point.get("ts")) != end
                or abs(
                    _number(point.get("predicted_indoor_temp"))
                    - state["indoor_c"]
                    - _number(row.get("forecast_bias_correction_c", 0))
                )
                > TOLERANCE
            ):
                raise DataIncompleteError("Diagnostic trajectory differs from serialized commands")
    if snapshot.get("no_heating_basis") != "fully_passive_no_SH_no_DHW":
        raise DataIncompleteError("Diagnostic passive counterfactual basis unavailable")
    for name in ("forecast_no_heating", "price_forecast"):
        points = snapshot.get(name)
        if not isinstance(points, list) or len(points) != len(trace["hours"]):
            raise DataIncompleteError("Diagnostic counterfactual or prices have incomplete horizon")
    passive = trace["initial_indoor_c"]
    delivered = [0.0] * len(trace["hours"])
    previous = _instant(replay["states"][0]["ts"])
    start = previous
    for state in replay["states"][1:]:
        end = _instant(state["ts"])
        index = int((previous - start).total_seconds() // 3600)
        delivered[index] += (
            (end - previous).total_seconds() / 3600 * state["space_heating_fraction"]
        )
        previous = end
    for index, row in enumerate(trace["hours"]):
        end = _instant(row["ts"]) + dt.timedelta(hours=1)
        passive += row["indoor_passive_c_per_h"]
        point = snapshot["forecast_no_heating"][index]
        price = snapshot["price_forecast"][index]
        if (
            not isinstance(point, dict)
            or _instant(point.get("ts")) != end
            or abs(
                _number(point.get("predicted_indoor_temp"))
                - passive
                - _number(row.get("forecast_bias_correction_c", 0))
            )
            > TOLERANCE
        ):
            raise DataIncompleteError("Diagnostic passive counterfactual differs from trace")
        if (
            not isinstance(price, dict)
            or _instant(price.get("ts")) != _instant(row["ts"])
            or any(
                abs(_number(price.get(key)) - row["price_per_kwh"]) > TOLERANCE
                for key in ("price_per_kwh", "price_eur_per_kwh")
            )
        ):
            raise DataIncompleteError("Diagnostic price forecast differs from priced trace")
        point = snapshot["forecast_with_plan"][index]
        for key, expected in (
            ("native_space_heating_fraction", row["native_sh_fraction"]),
            ("space_heating_fraction", delivered[index]),
        ):
            if key in point and abs(_number(point[key]) - expected) > TOLERANCE:
                raise DataIncompleteError("Diagnostic compatibility fraction differs from replay")
    if "indoor_forecast" in plan:
        points = plan["indoor_forecast"]
        if not isinstance(points, list) or len(points) != len(trace["hours"]):
            raise DataIncompleteError("Diagnostic compatibility trajectory has incomplete horizon")
        for index, point in enumerate(points):
            canonical = snapshot["forecast_with_plan"][index]
            if (
                not isinstance(point, dict)
                or _instant(point.get("ts")) != _instant(canonical["ts"])
                or abs(
                    _number(point.get("predicted_indoor_temp")) - canonical["predicted_indoor_temp"]
                )
                > TOLERANCE
                or abs(_number(point.get("space_heating_fraction")) - delivered[index]) > TOLERANCE
            ):
                raise DataIncompleteError("Diagnostic compatibility trajectory differs from replay")
    if "initial_violations" in plan and plan["initial_violations"] != replay["initial_violations"]:
        raise DataIncompleteError("Diagnostic initial-state disclosure differs from replay")
    return replay


def represented_rules_commands(actions):
    """Annotate exact serialized event duration; never infer duty from a target."""
    sources = {
        action.get("action_key"): action for action in actions if action["type"] == "force_dhw_on"
    }
    restores = {}
    for action in actions:
        if action["type"] != "force_dhw_off":
            continue
        key = action.get("reverts_action_key")
        if key in restores or key not in sources:
            raise InfeasibleError("Rules DHW restore identity ambiguous")
        restores[key] = action
    if None in sources or set(sources) != set(restores):
        raise InfeasibleError("Rules DHW command is missing its paired restore")
    result = []
    for action in actions:
        copied = {**action, "payload": dict(action.get("payload", {}))}
        if action["type"] == "force_dhw_on":
            duration = (
                _instant(restores[action["action_key"]]["ts"]) - _instant(action["ts"])
            ).total_seconds() / 60
            if duration != round(duration) or duration < 5:
                raise InfeasibleError("Rules DHW command has nonrepresentable runtime")
            copied["payload"]["dhw_minutes"] = int(duration)
        result.append(copied)
    return result


def _represented_objective(cost, minutes, active):
    """Return the solver objective while ``cost`` stays the monetary accounting expression.

    Only when every runtime-minute coefficient of the constructed monetary expression is
    exactly equal are activation counts used as a tie-break: one integer minute of primary
    objective is weighted ``M + 1`` and outranks every possible activation-count difference
    (at most ``M``), so monetary optimality is never traded for fewer pulses.
    """
    import pulp

    coefficients = [
        cost.get(variable, 0) if isinstance(cost, pulp.LpAffineExpression) else 0
        for variable in minutes
    ]
    if not coefficients or any(value != coefficients[0] for value in coefficients):
        return cost
    common = coefficients[0]
    if common == 0:
        return pulp.lpSum(active)
    bound = min(20, len(active))
    sign = 1 if common > 0 else -1
    return sign * (bound + 1) * pulp.lpSum(minutes) + pulp.lpSum(active)


def solve_represented_trace(trace, *, timeout_seconds):
    import pulp

    rows = validate_trace(trace)
    problem = pulp.LpProblem("NativeSHIntegerDHW", pulp.LpMinimize)
    minutes = [
        pulp.LpVariable(f"dhw_minutes_{hour}", 0, 60, cat="Integer") for hour in range(len(rows))
    ]
    active = [pulp.LpVariable(f"dhw_active_{hour}", cat="Binary") for hour in range(len(rows))]
    tank, indoor = trace["initial_tank_c"], trace["initial_indoor_c"]
    tank_ends, room_ends = [], []
    cost = 0
    deadline_floors = dict(zip(trace["deadline_hours"], trace["deadline_tank_floors_c"]))
    for hour, row in enumerate(rows):
        fraction = minutes[hour] / 60
        problem += minutes[hour] <= 60 * active[hour]
        problem += minutes[hour] >= 5 * active[hour]
        tank_peak = tank + row["tank_net_heating_c_per_h"] * fraction
        room_pulse_end = indoor + row["indoor_passive_c_per_h"] * fraction
        tank_end = tank_peak + row["tank_loss_c_per_h"] * (1 - fraction)
        room_end = (
            indoor
            + row["indoor_passive_c_per_h"]
            + (
                row["native_sh_fraction"]
                * (1 - fraction)
                * (row["indoor_full_heat_c_per_h"] - row["indoor_passive_c_per_h"])
            )
        )
        # The actual initial state may require recovery; endpoint and pulse extrema are hard.
        # An inactive pulse has no intermediate peak. Only the first hour can
        # start above the ceiling: every prior endpoint is already constrained.
        # This exact initial excess (not an arbitrary big-M) removes only that
        # nonexistent boundary; active pulses and all endpoints keep the ceiling.
        initial_excess = (
            max(0, trace["initial_tank_c"] - trace["tank_ceiling_c"]) if hour == 0 else 0
        )
        problem += tank_peak <= trace["tank_ceiling_c"] + initial_excess * (1 - active[hour])
        target_limit = trace["observed_tank_target_c"] - 0.5 - TOLERANCE
        target_relaxation = (
            abs(trace["initial_tank_c"])
            + abs(target_limit)
            + sum(
                abs(item["tank_net_heating_c_per_h"]) + abs(item["tank_loss_c_per_h"])
                for item in rows
            )
            + 1
        )
        problem += tank <= target_limit + target_relaxation * (1 - active[hour])
        problem += tank_peak <= target_limit + target_relaxation * (1 - active[hour])
        problem += tank_end <= trace["tank_ceiling_c"]
        problem += tank_end >= row["tank_floor_c"]
        problem += room_end >= row["indoor_floor_c"]
        indoor_ceiling = row.get("indoor_ceiling_c", trace["indoor_ceiling_c"])
        problem += room_end <= indoor_ceiling
        # With no pulse there is no intermediate boundary; activated pulse checks use
        # a derived relaxation sufficient only to remove that nonexistent boundary.
        lower_relaxation = (
            max(0, row["indoor_floor_c"] - trace["initial_indoor_c"])
            + sum(
                abs(item["indoor_passive_c_per_h"]) + abs(item["indoor_full_heat_c_per_h"])
                for item in rows
            )
            + 1
        )
        problem += room_pulse_end >= row["indoor_floor_c"] - lower_relaxation * (1 - active[hour])
        problem += room_pulse_end <= indoor_ceiling + lower_relaxation * (1 - active[hour])
        problem += tank_peak >= row["tank_floor_c"] - (
            abs(trace["initial_tank_c"])
            + sum(
                abs(item["tank_net_heating_c_per_h"]) + abs(item["tank_loss_c_per_h"])
                for item in rows
            )
            + abs(row["tank_floor_c"])
            + 1
        ) * (1 - active[hour])
        if hour + 1 in deadline_floors:
            problem += tank_end >= deadline_floors[hour + 1]
        cost += row["price_per_kwh"] * (
            fraction * row["dhw_power_kw"]
            + (1 - fraction) * row["native_sh_fraction"] * row["native_sh_power_kw"]
        )
        tank, indoor = tank_end, room_end
        tank_ends.append(tank_end)
        room_ends.append(room_end)
    problem += pulp.lpSum(active) <= 20
    problem += _represented_objective(cost, minutes, active)
    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=timeout_seconds))
    if problem.status == pulp.LpStatusNotSolved:
        raise SolverTimeoutError("Represented DHW solver timeout")
    if problem.status != pulp.LpStatusOptimal:
        raise InfeasibleError("No feasible integer DHW schedule under declared native SH trace")
    actions = []
    for hour, variable in enumerate(minutes):
        value = variable.value()
        if value is None or abs(value - round(value)) > TOLERANCE:
            raise InfeasibleError("Solver runtime is not integral")
        runtime = int(round(value))
        if runtime == 0:
            continue
        instant = _instant(rows[hour]["ts"])
        key = f"dhw:{instant.isoformat()}"
        actions.extend(
            [
                {
                    "ts": instant.isoformat(),
                    "type": "force_dhw_on",
                    "action_key": key,
                    "payload": {"reason": SCHEMA, "dhw_minutes": runtime},
                    "device_id": trace["device_id"],
                },
                {
                    "ts": (instant + dt.timedelta(minutes=runtime)).isoformat(),
                    "type": "force_dhw_off",
                    "reverts_action_key": key,
                    "payload": {"reason": "represented_pulse_end"},
                    "device_id": trace["device_id"],
                },
            ]
        )
    replay = replay_serialized_actions(trace, actions)
    for hour, (tank_state, room_state) in enumerate(zip(tank_ends, room_ends)):
        end = _instant(rows[hour]["ts"]) + dt.timedelta(hours=1)
        state = next(item for item in replay["states"] if _instant(item["ts"]) == end)
        if (
            abs(state["tank_c"] - pulp.value(tank_state)) > TOLERANCE
            or abs(state["indoor_c"] - pulp.value(room_state)) > TOLERANCE
        ):
            raise InfeasibleError("Serialized replay differs from solved state")
    if abs(replay["cost"] - (pulp.value(cost) or 0)) > TOLERANCE:
        raise InfeasibleError("Serialized replay differs from solved electricity cost")
    return actions, replay

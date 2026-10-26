from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
import structlog

from packages.api._helpers import get_price_area
from packages.api.schemas import IndoorForecastResponse
from packages.core.database import get_session
from packages.core.plan_lifecycle import active_plan_query, current_diagnostic_query
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    PlanActionRecord,
    PlanRecord,
    HouseInstallationRecord,
    PriceRecord,
    SpaceHeatingGateRecord,
    WeatherRecord,
)
from packages.ml.forecast_quality import get_forecast_scorecard as build_forecast_scorecard
from packages.ml.lifecycle import Lifecycle, LifecycleUnavailable, enqueue_maintenance_request
from packages.ml.models_common import is_finite_number
from packages.core.settings_service import get_bool_setting

router = APIRouter()
logger = structlog.get_logger(__name__)


def _forecast_status_from_quality(forecast_quality: dict[str, object]) -> str:
    """Keep presentation fail-closed when learned control is not permitted."""
    return "available" if forecast_quality.get("control_allowed") else "fallback"


def _safe_room_comfort_envelope(control_temperature, *, comfort_temp_max: float):
    """Treat room-envelope failures as absent evidence, without exposing readings."""
    from packages.core.control_temperature import build_room_comfort_envelope

    try:
        return build_room_comfort_envelope(control_temperature, comfort_temp_max=comfort_temp_max)
    except Exception as exc:  # noqa: BLE001 - presentation must fail closed
        logger.warning("room_comfort_envelope_unavailable", error_type=type(exc).__name__)
        return None


def _build_active_plan_comfort_assessment(
    *,
    plan_snapshot: dict[str, Any],
    planned_actions: list[dict[str, Any]],
    heat_curve,
    gate_projections,
    room_envelope,
    comfort_temp_min: float,
) -> dict[str, Any]:
    """Assess an active plan against fresh room evidence, not saved room state."""
    from packages.core.comfort_assessment import build_comfort_assessment

    quality = plan_snapshot.get("forecast_quality")
    quality_reason = quality.get("reason") if isinstance(quality, dict) else None
    return build_comfort_assessment(
        forecast=plan_snapshot.get("forecast_with_plan", []),
        targets=plan_snapshot.get("target_schedule", []),
        weather=plan_snapshot.get("weather_forecast", []),
        planned_actions=planned_actions,
        heat_curve=heat_curve,
        gate_projections=gate_projections,
        forecast_status=plan_snapshot.get("forecast_status", "unavailable"),
        forecast_reason=plan_snapshot.get("forecast_unavailable_reason") or quality_reason,
        room_envelope=room_envelope,
        comfort_temp_min=comfort_temp_min,
    )


async def _read_live_forecast_quality() -> dict[str, object]:
    """Evaluate the shared gate without advancing its persisted hysteresis."""
    from packages.ml.comfort_model import comfort_model
    from packages.ml.forecast_quality import (
        apply_control_gate_hysteresis,
        evaluate_live_control_gate,
    )

    metrics = comfort_model.metrics

    def read_only_record_gate(
        gate: dict[str, object],
        *,
        schema: str,
        required_horizons: tuple[int, ...],
        passes_required: int,
        failures_required: int,
        evaluation_id: str | None = None,
        evaluation_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del evaluation_id
        return apply_control_gate_hysteresis(
            gate,
            metrics,
            schema=schema,
            required_horizons=required_horizons,
            passes_required=passes_required,
            failures_required=failures_required,
            evaluation_context=evaluation_context,
        )

    return await evaluate_live_control_gate(
        model_metrics=metrics,
        record_gate=read_only_record_gate,
    )


def _reporting_forecast_quality(forecast_quality: dict) -> dict:
    """Expose validation evidence without advertising write permission."""
    return {
        **forecast_quality,
        "control_allowed": False,
        "control_reason": "observe_only_build",
    }


@router.get("/api/thermal/forecast-scorecard")
async def get_forecast_scorecard():
    """Score immutable indoor forecasts against later sensor observations."""
    from copy import deepcopy

    scorecard = deepcopy(await build_forecast_scorecard())
    for family in [
        scorecard,
        *(scorecard.get(name) for name in ("fallback", "passive_weather_model", "all_forecasts")),
    ]:
        if isinstance(family, dict) and isinstance(family.get("quality_gate"), dict):
            family["quality_gate"]["control_allowed"] = False
    scorecard["ordinary_writes_allowed"] = False
    return scorecard


@router.get("/api/sensors/diagnostics")
async def get_sensor_diagnostics(hours: int = Query(168, ge=1, le=720)):
    """Return observation-only SmartThings sensor diagnostics."""
    from packages.core.sensor_diagnostics import summarize_sensor_diagnostics
    from packages.core.settings_service import get_setting

    now = dt.datetime.now(dt.timezone.utc)
    reference_sensor_id = (await get_setting("comfort_reference_sensor_id")).strip()
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(IndoorTempReading)
                    .where(IndoorTempReading.timestamp >= now - dt.timedelta(hours=hours))
                    .order_by(IndoorTempReading.timestamp)
                )
            )
            .scalars()
            .all()
        )
    return summarize_sensor_diagnostics(rows, reference_sensor_id=reference_sensor_id, now=now)


@router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Expose a tiny Prometheus-compatible operational surface.

    It intentionally contains aggregate health signals only: no device ids,
    credentials, raw indoor readings, or plan payloads leave the API here.
    """

    from packages.core.operational_alerts import get_operational_alerts

    alerts = await get_operational_alerts()
    summary = alerts.get("summary", {}) if isinstance(alerts, dict) else {}
    critical = int(summary.get("critical", 0)) if isinstance(summary, dict) else 0
    warning = int(summary.get("warning", 0)) if isinstance(summary, dict) else 0
    enabled = 1 if alerts.get("enabled") else 0
    return PlainTextResponse(
        "\n".join(
            (
                "# HELP heatpump_optimizer_operational_alerts Active operational alerts by severity.",
                "# TYPE heatpump_optimizer_operational_alerts gauge",
                f'heatpump_optimizer_operational_alerts{{severity="critical"}} {critical}',
                f'heatpump_optimizer_operational_alerts{{severity="warning"}} {warning}',
                "# HELP heatpump_optimizer_operational_alerting_enabled Whether in-app operational alerts are enabled.",
                "# TYPE heatpump_optimizer_operational_alerting_enabled gauge",
                f"heatpump_optimizer_operational_alerting_enabled {enabled}",
                "",
            )
        ),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _numeric_forecast_unavailable(reason: str) -> dict[str, Any]:
    return IndoorForecastResponse(
        forecast=[],
        forecast_with_plan=[],
        forecast_no_heating=[],
        target_schedule=[],
        weather_forecast=[],
        price_forecast=[],
        planned_actions=[],
        forecast_source="unavailable",
        forecast_status="unavailable",
        forecast_unavailable_reason=reason,
        forecast_quality={"status": "unavailable", "control_allowed": False, "reason": reason},
        room_comfort={"status": "unavailable", "reason": reason},
    ).model_dump(mode="json")


async def _forecast_dependency_context(session, revision) -> tuple:
    installation = await session.get(HouseInstallationRecord, 1)
    generation = installation.indoor_dependency_generation
    if type(generation) is not int or generation < 0 or installation.installation_id is None:
        raise ValueError("unqualified indoor dependency generation")
    return (revision.id, revision.contract_hash, installation.installation_id, generation)


async def _forecast_dependency_context_is_current(captured: tuple) -> bool:
    from packages.core.house_contract import ensure_current_contract

    try:
        # A new session avoids reusing the initial installation identity map.
        async with get_session() as session:
            revision = await ensure_current_contract(session)
            return await _forecast_dependency_context(session, revision) == captured
    except (AttributeError, TypeError, ValueError, SQLAlchemyError):
        return False


def _forecast_instant(value: str) -> dt.datetime:
    instant = dt.datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(dt.timezone.utc)


def _forecast_interval(point: dict) -> tuple[dt.datetime, dt.datetime]:
    end = _forecast_instant(point["ts"])
    start_value, end_value = point.get("interval_start_utc"), point.get("interval_end_utc")
    if start_value is None and end_value is None:
        return end - dt.timedelta(hours=1), end
    start, explicit_end = _forecast_instant(start_value), _forecast_instant(end_value)
    if explicit_end != end or end - start != dt.timedelta(hours=1):
        raise ValueError("invalid forecast interval")
    return start, end


def _target_policy_identity(row: dict) -> dict:
    return {
        key: _forecast_instant(value)
        if key in {"ts", "interval_start_utc", "interval_end_utc"}
        else value
        for key, value in row.items()
        if key != "hour"
    }


def _qualified_forecast_indexes(plan, payload, snapshot, forecast_start, hours):
    from packages.core.comfort_schedule import CONTROL_SAMPLING_POLICY, sample_control_target
    from packages.core.house_contract import (
        canonical_target_schedule,
        validate_plan_contract_snapshot,
    )

    authority = payload.get("house_contract_snapshot")
    if not validate_plan_contract_snapshot(plan, authority):
        return None
    canonical = canonical_target_schedule(authority)
    targets = snapshot.get("target_schedule")
    if not isinstance(targets, list):
        return None
    if snapshot.get("forecast_status") == "unavailable":
        # Unavailable snapshots may omit numeric policy, but cannot carry a forged one.
        if targets and [_target_policy_identity(row) for row in targets] != [
            _target_policy_identity(row) for row in canonical
        ]:
            return None
        return []
    curves = [
        snapshot.get(key) for key in ("forecast", "forecast_with_plan", "forecast_no_heating")
    ]
    if not all(isinstance(curve, list) and curve for curve in curves):
        return None
    intervals = [_forecast_interval(point) for point in curves[1]]
    if any(end != next_start for (_, end), (next_start, _) in zip(intervals, intervals[1:])):
        return None
    if any([_forecast_interval(point) for point in curve] != intervals for curve in curves):
        return None
    explicit_targets = any(
        target.get("interval_start_utc") is not None or target.get("interval_end_utc") is not None
        for target in targets
    )
    exact = bool(targets) and all(
        target.get("interval_start_utc") is not None and target.get("interval_end_utc") is not None
        for target in targets
    )
    if explicit_targets and not exact:
        return None
    if exact:
        expected = [
            row
            for row in canonical
            if _forecast_instant(row["interval_end_utc"]) > intervals[0][0]
            and _forecast_instant(row["interval_start_utc"]) < intervals[-1][1]
        ]
        # Presentation indexes may be rebased; every policy/binding field must agree.
        if [_target_policy_identity(row) for row in targets] != [
            _target_policy_identity(row) for row in expected
        ]:
            return None
    elif len(targets) != len(intervals):
        return None
    for index, (start, end) in enumerate(intervals):
        sampled = sample_control_target(canonical, start)
        if not sampled:
            return None
        if not exact:
            target = targets[index]
            if (
                _forecast_instant(target["ts"]) != end
                or target.get("target") != sampled["target"]
                or target.get("comfort_hour") != sampled["comfort_hour"]
            ):
                return None
            for key in (
                "contract_revision_id",
                "contract_hash",
                "policy_dependency_fingerprint",
                "resolver_schema",
                "timezone_policy",
                "tzdata_version",
            ):
                if key in target and target[key] != sampled.get(key):
                    return None
        for curve in curves:
            point = curve[index]
            if "control_target_c" in point and (
                not is_finite_number(point["control_target_c"])
                or point["control_target_c"] != sampled["target"]
            ):
                return None
            # Represented-strategy v4/v5 producers use the earlier spelling of
            # the same slot-start rule. Qualify its target above; reject other policies.
            if "control_sampling_policy" in point and point["control_sampling_policy"] not in {
                CONTROL_SAMPLING_POLICY,
                "slot_start_utc",
            }:
                return None
        for key in ("weather_forecast", "price_forecast"):
            if _forecast_instant(snapshot[key][index]["ts"]) != start:
                return None
    indexes = []
    for offset in range(hours):
        start = forecast_start + dt.timedelta(hours=offset)
        expected = (start, start + dt.timedelta(hours=1))
        if expected not in intervals:
            return None
        indexes.append(intervals.index(expected))
    return indexes


def _plan_forecast_window(plan: PlanRecord, forecast_start: dt.datetime, hours: int) -> dict | None:
    """Qualify saved data without allowing corrupt JSON to escape as a response error."""
    try:
        window = _read_plan_forecast_window(plan, forecast_start, hours)
        if window is not None:
            IndoorForecastResponse.model_validate(
                {**window, "planned_actions": [], "forecast_source": "active_plan"}
            )
        return window
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError):
        return None


def _read_plan_forecast_window(
    plan: PlanRecord,
    forecast_start: dt.datetime,
    hours: int,
) -> dict[str, Any] | None:
    """Return the stored plan snapshot as a window relative to the current hour."""
    try:
        payload = json.loads(plan.plan_json)
        snapshot = payload.get("forecast_snapshot") if isinstance(payload, dict) else None
        if isinstance(snapshot, dict):
            json.dumps(snapshot, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get("version") not in {
        "indoor_forecast_v1",
        "indoor_forecast_v2",
        "indoor_forecast_v3",
        "indoor_forecast_v4",
        "indoor_forecast_v5",
    }:
        return None

    try:
        indexes = _qualified_forecast_indexes(plan, payload, snapshot, forecast_start, hours)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    if indexes is None:
        return None

    if snapshot.get("forecast_status") == "unavailable":
        return {
            "current_indoor": None,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": snapshot.get("target_schedule", []),
            "weather_forecast": snapshot.get("weather_forecast", []),
            "price_forecast": snapshot.get("price_forecast", []),
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": snapshot.get("forecast_unavailable_reason"),
        }

    policy_targets = snapshot.get("target_schedule")
    exact_policy = (
        isinstance(policy_targets, list)
        and bool(policy_targets)
        and all(
            isinstance(target, dict)
            and "interval_start_utc" in target
            and "interval_end_utc" in target
            for target in policy_targets
        )
    )
    keys = (
        "forecast",
        "forecast_with_plan",
        "forecast_no_heating",
        "weather_forecast",
        "price_forecast",
    )
    if not exact_policy:
        keys += ("target_schedule",)
    sequences = [snapshot.get(key) for key in keys]
    if not all(isinstance(sequence, list) for sequence in sequences):
        return None

    if len(indexes) < hours or any(
        index >= len(sequence) for index in indexes for sequence in sequences
    ):
        return None

    def curve(key: str) -> list[dict[str, Any]]:
        result = []
        for relative_hour, index in enumerate(indexes, start=1):
            source = snapshot[key][index]
            if not isinstance(source, dict) or not is_finite_number(
                source.get("predicted_indoor_temp")
            ):
                return []
            if any(
                source.get(field) is not None and not is_finite_number(source[field])
                for field in (
                    "space_heating_fraction",
                    "baseline_heating_fraction",
                    "prediction_lower_c",
                    "prediction_upper_c",
                )
            ):
                return []
            result.append(
                {
                    "hour": relative_hour,
                    "ts": source.get("ts"),
                    **{
                        key: source[key]
                        for key in (
                            "interval_start_utc",
                            "interval_end_utc",
                            "control_sampling_policy",
                            "control_target_c",
                        )
                        if key in source
                    },
                    "predicted_indoor_temp": source["predicted_indoor_temp"],
                    "source": source.get("source"),
                    "model_source": source.get("model_source"),
                    "space_heating_fraction": source.get("space_heating_fraction"),
                    "baseline_heating_fraction": source.get("baseline_heating_fraction"),
                    "baseline_heating_source": source.get("baseline_heating_source"),
                    "space_heating_source": source.get("space_heating_source"),
                    "prediction_lower_c": source.get("prediction_lower_c"),
                    "prediction_upper_c": source.get("prediction_upper_c"),
                    "prediction_interval_status": source.get("prediction_interval_status"),
                }
            )
        return result

    forecast = curve("forecast")
    with_plan = curve("forecast_with_plan")
    no_heating = curve("forecast_no_heating")
    if not forecast or not with_plan or not no_heating:
        return None

    targets = []
    weather = []
    prices = []
    for relative_hour, index in enumerate(indexes, start=1):
        target = snapshot["target_schedule"][0 if exact_policy else index]
        weather_row = snapshot["weather_forecast"][index]
        price = snapshot["price_forecast"][index]
        if not all(isinstance(row, dict) for row in (target, weather_row, price)):
            return None
        if (
            not is_finite_number(target.get("target"))
            or weather_row.get("ts") is None
            or price.get("ts") is None
        ):
            return None
        if any(
            row.get(field) is not None and not is_finite_number(row[field])
            for row, fields in (
                (weather_row, ("outdoor_temp", "wind_speed", "irradiance", "precipitation")),
                (price, ("price_per_kwh", "price_eur_per_kwh")),
            )
            for field in fields
        ):
            return None
        targets.append(
            {
                "hour": relative_hour,
                "ts": target.get("ts"),
                "target": target["target"],
                "comfort_hour": bool(target.get("comfort_hour", target["target"] >= 20.0)),
            }
        )
        weather.append(
            {
                "ts": weather_row["ts"],
                "hour": weather_row.get("hour", 0),
                "outdoor_temp": weather_row.get("outdoor_temp"),
                "wind_speed": weather_row.get("wind_speed"),
                "irradiance": weather_row.get("irradiance"),
                "precipitation": weather_row.get("precipitation"),
                "input_status": weather_row.get("input_status", "observed"),
                "imputed_fields": weather_row.get("imputed_fields", []),
            }
        )
        prices.append(
            {
                "ts": price["ts"],
                "price_eur_per_kwh": price.get("price_eur_per_kwh"),
                "price_per_kwh": price.get("price_per_kwh", price.get("price_eur_per_kwh")),
                "currency": price.get("currency"),
            }
        )

    if exact_policy:
        # Exact local-hour policy boundaries need not align with the hourly
        # forecast/control grid. Never zip or truncate them by forecast index.
        try:
            window_start = dt.datetime.fromisoformat(
                with_plan[0].get("interval_start_utc") or with_plan[0]["ts"]
            )
            if not with_plan[0].get("interval_start_utc"):
                window_start -= dt.timedelta(hours=1)
            window_end = dt.datetime.fromisoformat(with_plan[-1]["ts"])
            targets = [
                dict(target)
                for target in policy_targets
                if dt.datetime.fromisoformat(target["interval_end_utc"]) > window_start
                and dt.datetime.fromisoformat(target["interval_start_utc"]) < window_end
            ]
            if not targets or any(not is_finite_number(target.get("target")) for target in targets):
                return None
        except (TypeError, ValueError):
            return None

    try:
        current_indoor = float(snapshot.get("current_indoor"))
    except (TypeError, ValueError):
        current_indoor = None
    if current_indoor is not None and not is_finite_number(current_indoor):
        return None

    legacy_snapshot = snapshot.get("version") in {
        "indoor_forecast_v1",
        "indoor_forecast_v2",
        "indoor_forecast_v3",
    }
    return {
        "current_indoor": current_indoor,
        "forecast": forecast,
        "forecast_with_plan": with_plan,
        "forecast_no_heating": no_heating,
        "target_schedule": targets,
        "weather_forecast": weather,
        "price_forecast": prices,
        "forecast_status": "legacy"
        if legacy_snapshot
        else snapshot.get("forecast_status", "unavailable"),
        "forecast_unavailable_reason": None,
        "forecast_provenance": {
            "control_input": snapshot.get("control_input", {}),
            "heat_curve": snapshot.get("heat_curve", {}),
        },
        "observed_history": snapshot.get("observed_history", []),
        "sensor_basis": snapshot.get("sensor_basis", {}),
        "forecast_quality": snapshot.get(
            "forecast_quality", {"status": "legacy"} if legacy_snapshot else {}
        ),
        "space_heating_baseline": snapshot.get("space_heating_baseline", {}),
        "room_comfort": snapshot.get(
            "room_comfort", {"status": "legacy"} if legacy_snapshot else {}
        ),
    }


async def _saved_plan_model_dependencies_are_current(plan) -> bool:
    """Check saved model and sensor dependencies against current persisted authority."""
    from packages.core.house_contract import _lock_contract
    from packages.core.models import HouseInstallationRecord, MLLifecycleStateRecord
    from packages.ml.forecast_quality import locked_demand_scope, locked_native_scope
    from packages.ml.lifecycle import reference_is_qualified

    try:
        provenance = json.loads(getattr(plan, "input_provenance_json", None) or "{}")
        if not isinstance(provenance, dict):
            return False
        saved_generation = provenance.get("indoor_dependency_generation")
        if (
            isinstance(saved_generation, bool)
            or not isinstance(saved_generation, int)
            or saved_generation < 0
        ):
            return False
        model_provenance = provenance.get("models")
        dependencies = (
            model_provenance.get("native_dependencies")
            if isinstance(model_provenance, dict)
            else None
        )
        if not isinstance(model_provenance, dict) or not isinstance(dependencies, list):
            return False
        by_component = {
            item.get("component"): item
            for item in dependencies
            if isinstance(item, dict) and item.get("component") in {"comfort", "thermal"}
        }
        if set(by_component) != {"comfort", "thermal"}:
            return False
        async with get_session() as session:
            await _lock_contract(session)
            installation = await session.get(HouseInstallationRecord, 1)
            if (
                installation is None
                or installation.indoor_dependency_generation != saved_generation
            ):
                return False
            lifecycle_row = await session.scalar(
                select(MLLifecycleStateRecord)
                .where(MLLifecycleStateRecord.id == 1)
                .with_for_update()
            )
            if lifecycle_row is None:
                return False
            lifecycle = Lifecycle()
            state = await lifecycle.read(session=session)
            for component in ("comfort", "thermal"):
                dependency = by_component[component]
                reference = state.reference(component)
                expected_hash = dependency.get("artifact_digest")
                if expected_hash is None:
                    if reference is not None and reference_is_qualified(reference):
                        capabilities = tuple(reference.manifest.get("capabilities", ()))
                        async with locked_native_scope(
                            component,
                            device_id=reference.manifest["device_id"],
                            capabilities=capabilities,
                            session=session,
                        ) as (scope, _):
                            if reference_is_qualified(reference, scope=scope):
                                return False
                    continue
                if reference is None or reference.artifact_hash != expected_hash:
                    return False
                capabilities = tuple(reference.manifest.get("capabilities", ()))
                async with locked_native_scope(
                    component,
                    device_id=reference.manifest["device_id"],
                    capabilities=capabilities,
                    session=session,
                ) as (scope, _):
                    current = (await lifecycle.read(session=session)).reference(component)
                    if (
                        current != reference
                        or scope != dependency.get("scope")
                        or not reference_is_qualified(reference, scope=scope)
                    ):
                        return False
            demand_dependency = model_provenance.get("demand_serving_dependency")
            if isinstance(demand_dependency, dict):
                digest = demand_dependency.get("artifact_digest")
                dependency_schema = demand_dependency.get("dependency_schema")
                fingerprint = demand_dependency.get("dependency_fingerprint")
                dependency_generation = demand_dependency.get("dependency_generation")
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or not isinstance(dependency_schema, str)
                    or not dependency_schema
                    or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in fingerprint)
                    or isinstance(dependency_generation, bool)
                    or not isinstance(dependency_generation, int)
                    or dependency_generation < 0
                    or type(demand_dependency.get("used_for_plan")) is not bool
                ):
                    return False
            elif demand_dependency is not None:
                return False
            if isinstance(demand_dependency, dict) and demand_dependency["used_for_plan"]:
                demand_generation = provenance.get("planning_dependency_generation")
                if (
                    isinstance(demand_generation, bool)
                    or not isinstance(demand_generation, int)
                    or installation.demand_dependency_generation != demand_generation
                ):
                    return False
                demand_reference = state.reference("demand")
                if (
                    demand_reference is None
                    or demand_reference.artifact_hash != demand_dependency.get("artifact_digest")
                ):
                    return False
                async with locked_demand_scope(
                    demand_reference.manifest["device_id"], session=session
                ) as (demand_scope, _):
                    current_demand = (await lifecycle.read(session=session)).reference("demand")
                    if (
                        current_demand != demand_reference
                        or not reference_is_qualified(demand_reference, scope=demand_scope)
                        or demand_scope.get("component_dependency_schema")
                        != demand_dependency.get("dependency_schema")
                        or demand_scope.get("component_dependency_fingerprint")
                        != demand_dependency.get("dependency_fingerprint")
                        or demand_scope.get("component_dependency_generation")
                        != demand_dependency.get("dependency_generation")
                    ):
                        return False
            elif (
                demand_dependency is None
                and bool(model_provenance.get("demand_trained"))
                and str(getattr(plan, "optimizer_version", "")).startswith("milp")
            ):
                return False
        return True
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        LifecycleUnavailable,
        SQLAlchemyError,
        OSError,
    ) as error:
        logger.warning(
            "saved_plan_model_dependency_check_failed",
            plan_id=getattr(plan, "id", None),
            error_type=type(error).__name__,
        )
        return False


def _enforce_physical_ordering(
    forecast: list[dict],
    forecast_with_plan: list[dict],
    forecast_no_heating: list[dict],
) -> None:
    """Clamp indoor-temperature curves in place to a physically valid ordering.

    Active heating can never leave the house cooler than the no-heating
    baseline, so both the base forecast and the schedule-managed "with plan"
    forecast are clamped to never fall below ``forecast_no_heating``. This is a
    cheap safety net so the chart can never show the nonsensical "predicted
    indoor below no-heating" case (e.g. when the model is untrained and the
    linear fallback is used).

    Note: the managed forecast is intentionally allowed to dip *below* the base
    forecast overnight (it reflects the comfort-schedule setback), so it is not
    clamped up to the base — only to the no-heating floor.
    """
    key = "predicted_indoor_temp"
    n = min(len(forecast), len(forecast_with_plan), len(forecast_no_heating))
    for h in range(n):
        floor = forecast_no_heating[h][key]
        forecast[h][key] = round(max(forecast[h][key], floor), 1)
        forecast_with_plan[h][key] = round(max(forecast_with_plan[h][key], floor), 1)


@router.get("/api/comfort-model/status")
async def get_comfort_model_status():
    from packages.ml.comfort_model import comfort_model
    from packages.ml.models import MODEL_DIR
    from packages.ml.model_status import get_model_status

    await comfort_model.arefresh_if_changed()
    lifecycle_status = await get_model_status("comfort", MODEL_DIR)
    return {
        "lifecycle": lifecycle_status,
        "reporting_state": lifecycle_status["status"],
        "trained": comfort_model.is_trained,
        "control_ready": False,
        "control_readiness": {"ready": False, "reason": "observe_only_build"},
        "ordinary_writes_allowed": False,
        "last_trained": comfort_model.last_trained.isoformat()
        if comfort_model.last_trained
        else None,
        "training_samples": comfort_model.training_samples,
        "metrics": comfort_model.metrics,
        "training_notice": comfort_model.training_notice,
        "artifact_refresh_reason": comfort_model.artifact_refresh_reason,
        "control_margin_c": comfort_model.control_margin_c,
        "passive_forecast": {
            str(horizon): comfort_model.passive_forecast_readiness(horizon)
            for horizon in (60, 180, 360, 720)
        },
    }


async def _queue_training(component: str, action: str) -> dict[str, Any]:
    try:
        result = await enqueue_maintenance_request(component, "api")
        if result is None:
            raise LifecycleUnavailable("maintenance_request_unavailable")
    except (SQLAlchemyError, LifecycleUnavailable) as error:
        logger.warning(
            "ml_maintenance_request_unavailable",
            component=component,
            error_type=type(error).__name__,
        )
        raise HTTPException(
            status_code=503, detail={"code": "ml_maintenance_unavailable"}
        ) from error
    result["control_ready"] = False
    result["control_readiness"] = {"ready": False, "reason": "observe_only_build"}
    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action=action,
                payload_json=json.dumps(result, default=str),
                result="queued",
            )
        )
    return result


@router.post("/api/comfort-model/train", status_code=202)
async def trigger_comfort_model_training():
    return await _queue_training("comfort", "train_comfort_model")


@router.get("/api/ml/requests/{request_id}")
async def get_maintenance_request(request_id: UUID):
    request = await Lifecycle().get_request(str(request_id))
    if request is None:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    return request.response(await get_bool_setting("ml_maintenance_enabled"))


@router.get("/api/comfort-model/predict")
async def predict_indoor_temp(
    water_temp: float = Query(..., description="Zone water supply temperature (C)"),
    outdoor_temp: float = Query(..., description="Outdoor temperature (C)"),
    hour: int = Query(12, ge=0, le=23),
    indoor_temp: float | None = Query(
        None, description="Current indoor temperature (C) from SmartThings"
    ),
    precipitation: float = Query(
        0.0, ge=0, description="Precipitation in mm for the forecast hour"
    ),
    humidity: float = Query(60.0, ge=0, le=100, description="Relative humidity (%)"),
    cloud_cover: float = Query(0.5, ge=0, le=1, description="Cloud cover fraction (0-1)"),
):
    from packages.ml.comfort_model import comfort_model

    await comfort_model.arefresh_if_changed()
    if not comfort_model.is_trained:
        raise HTTPException(status_code=409, detail="Comfort model not yet trained")

    indoor = comfort_model.predict_indoor_temp(
        zone_water_temp=water_temp,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
        precipitation=precipitation,
        humidity=humidity,
        cloud_cover=cloud_cover,
    )
    required_water = comfort_model.required_zone_temp(
        target_indoor=21.0,
        outdoor_temp=outdoor_temp,
        hour=hour,
        indoor_temp=indoor_temp,
        precipitation=precipitation,
        humidity=humidity,
        cloud_cover=cloud_cover,
    )

    return {
        "predicted_indoor_temp": round(indoor, 1) if indoor is not None else None,
        "required_water_temp_for_21c": round(required_water, 1)
        if required_water is not None
        else None,
    }


@router.post("/api/ml/train", status_code=202)
async def trigger_ml_training():
    cop_result = {
        "status": "retired",
        "supported": False,
        "error": "cop_model_retired",
        "reason": "unverified_thermal_output",
    }
    demand_result = await _queue_training("demand", "train_ml_models")
    return {"cop": cop_result, "demand": demand_result}


@router.get("/api/thermal/status")
async def get_thermal_status():
    from packages.core.control_temperature import get_control_temperature
    from packages.ml.models import MODEL_DIR
    from packages.ml.model_status import get_model_status
    from packages.ml.thermal import thermal_model

    await thermal_model.arefresh_if_changed()
    thermal_dependency_context = thermal_model.capture_dependency_context()
    lifecycle_status = await get_model_status("thermal", MODEL_DIR)
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()

    if not status:
        return {
            "lifecycle": lifecycle_status,
            "reporting_state": lifecycle_status["status"],
            "control_ready": False,
            "control_readiness": {"ready": False, "reason": "observe_only_build"},
            "ordinary_writes_allowed": False,
            "current_indoor": None,
            "outdoor_temp": None,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": [],
            "price_forecast": [],
            "planned_actions": [],
            "forecast_source": "unavailable",
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": "no_device_data",
            "comfort_assessment": {
                "state": "unavailable",
                "summary": "No recent heat-pump status is available.",
                "misses": [],
                "recommendations": [],
            },
            "forecast_provenance": {"device_status": "missing"},
            "display_status": "unavailable",
        }

    current_tank = status.tank_temp if status.tank_temp is not None else 48.0
    tank_target = status.tank_target_temp if status.tank_target_temp is not None else 52
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    current_zone = status.zone1_temp if status.zone1_temp is not None else 20.0

    heating_pred = thermal_model.predict_tank_heating_time(
        current_tank, float(tank_target), outdoor
    )
    cooling_pred = thermal_model.predict_tank_cooling_time(
        current_tank, float(tank_target - 7), outdoor
    )
    zone_pred = thermal_model.predict_zone_heating_time(current_zone, current_zone + 2, outdoor)

    async with get_session() as session:
        control_temperature = await get_control_temperature(session=session)

    latest_indoor = float(control_temperature.value) if control_temperature.is_usable else None
    indoor_cooling_pred = (
        thermal_model.predict_indoor_cooling_time(latest_indoor, latest_indoor - 2.0, outdoor)
        if latest_indoor is not None
        else None
    )
    indoor_heating_pred = (
        thermal_model.predict_indoor_heating_time(latest_indoor, latest_indoor + 1.0, outdoor)
        if latest_indoor is not None
        else None
    )
    if not await thermal_model.dependency_context_is_current(thermal_dependency_context):
        raise HTTPException(
            status_code=503,
            detail="Thermal model dependencies changed during prediction; retry",
        )

    return {
        "lifecycle": lifecycle_status,
        "reporting_state": lifecycle_status["status"],
        "control_ready": False,
        "control_readiness": {"ready": False, "reason": "observe_only_build"},
        "ordinary_writes_allowed": False,
        "current": {
            "tank_temp": current_tank,
            "tank_target": tank_target,
            "outdoor_temp": outdoor,
            "zone1_temp": current_zone,
            "indoor_temp": latest_indoor,
            "timestamp": status.ts.isoformat(),
        },
        "predictions": {
            "tank_heating": {
                "minutes_to_target": round(heating_pred.estimated_minutes, 1),
                "heating_rate_per_hour": round(heating_pred.heating_rate_per_hour, 2),
                "confidence": heating_pred.confidence,
            },
            "tank_cooling": {
                "minutes_until_min": round(cooling_pred.estimated_minutes, 1)
                if cooling_pred.estimated_minutes != float("inf")
                else None,
                "loss_rate_per_hour": round(cooling_pred.heating_rate_per_hour, 2),
                "confidence": cooling_pred.confidence,
            },
            "zone_boost": {
                "minutes_for_2deg": round(zone_pred.estimated_minutes, 1),
                "heating_rate_per_hour": round(zone_pred.heating_rate_per_hour, 2),
                "confidence": zone_pred.confidence,
            },
            "indoor": {
                "current_indoor_temp": latest_indoor,
                "minutes_to_cool_2deg": round(indoor_cooling_pred.estimated_minutes, 1)
                if indoor_cooling_pred is not None
                and indoor_cooling_pred.estimated_minutes != float("inf")
                else None,
                "minutes_to_heat_1deg": (
                    round(indoor_heating_pred.estimated_minutes, 1)
                    if indoor_heating_pred is not None
                    else None
                ),
                "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
                "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
                "indoor_heating_samples": thermal_model.params.indoor_heating_samples,
                "indoor_cooling_samples": thermal_model.params.indoor_cooling_samples,
                "confidence": (
                    indoor_heating_pred.confidence
                    if indoor_heating_pred is not None
                    else "unavailable"
                ),
                "reason": control_temperature.reason if latest_indoor is None else None,
                "heating_confidence": thermal_model.confidence_for("indoor_heating"),
                "cooling_confidence": thermal_model.confidence_for("indoor_cooling"),
            },
        },
        "model_params": {
            "tank_heating_rate": round(thermal_model.params.tank_heating_rate, 3),
            "tank_heating_outdoor_factor": round(
                thermal_model.params.tank_heating_outdoor_factor, 4
            ),
            "tank_standby_loss": round(thermal_model.params.tank_standby_loss, 3),
            "zone_heating_rate": round(thermal_model.params.zone_heating_rate, 3),
            "zone_standby_loss": round(thermal_model.params.zone_standby_loss, 3),
            "indoor_heating_rate": round(thermal_model.params.indoor_heating_rate, 3),
            "indoor_cooling_rate": round(thermal_model.params.indoor_cooling_rate, 3),
            "last_calibrated": thermal_model.params.last_calibrated.isoformat()
            if thermal_model.params.last_calibrated
            else None,
            "sample_count": thermal_model.params.sample_count,
            "calibration_status": thermal_model.params.calibration_status,
            "component_confidence": {
                "tank_heating": thermal_model.confidence_for("tank_heating"),
                "tank_cooling": thermal_model.confidence_for("tank_cooling"),
                "zone_heating": thermal_model.confidence_for("zone_heating"),
                "zone_cooling": thermal_model.confidence_for("zone_cooling"),
                "indoor_heating": thermal_model.confidence_for("indoor_heating"),
                "indoor_cooling": thermal_model.confidence_for("indoor_cooling"),
            },
        },
    }


@router.post("/api/thermal/calibrate", status_code=202)
async def calibrate_thermal_model():
    return await _queue_training("thermal", "calibrate_thermal_model")


@router.get("/api/thermal/curve")
async def get_thermal_curve(hours: int = Query(24, ge=1, le=72)):
    from packages.core.config import settings
    from packages.core.settings_service import (
        get_all_settings,
        is_comfort_hour,
    )
    from packages.ml.thermal import thermal_model
    from packages.optimizer.executor_core import LearningModeState, resolve_learning_mode_state

    await thermal_model.arefresh_if_changed()
    thermal_dependency_context = thermal_model.capture_dependency_context()
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = result.scalar_one_or_none()
        from packages.core.house_contract import ensure_current_contract

        contract_revision = await ensure_current_contract(session)
        planning_settings = await get_all_settings(session=session)

    if not status:
        return {"error": "No device data available"}

    current_tank = status.tank_temp if status.tank_temp is not None else 48.0
    tank_target = status.tank_target_temp if status.tank_target_temp is not None else 52
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    current_zone = status.zone1_temp if status.zone1_temp is not None else 20.0

    now = dt.datetime.now(dt.timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)

    # Per-hour tank reheat floor (deadband fallback), matching the optimizer
    # (milp.py): the tank may coast lower overnight/off-peak than during comfort
    # hours. Only used when there is no executable plan to follow.
    contract_projection = contract_revision.contract_json
    comfort_schedule = contract_projection["comfort_schedule"]
    tz_name = contract_projection["timezone"]
    tank_min_temp = int(planning_settings.get("tank_min_temp") or settings.tank_min_temp)
    tank_min_temp_offpeak = int(
        planning_settings.get("tank_min_temp_offpeak") or settings.tank_min_temp_offpeak
    )
    tank_min_per_hour = []
    for h in range(hours):
        hour_ts = hour_start + dt.timedelta(hours=h)
        if is_comfort_hour(comfort_schedule, hour_ts, tz_name=tz_name):
            tank_min_per_hour.append(float(tank_min_temp))
        else:
            tank_min_per_hour.append(float(tank_min_temp_offpeak))

    # Ordinary plans never drive the heat pump in this build.
    try:
        learning_mode_state = await resolve_learning_mode_state()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - curve projection must fail closed
        logger.warning("thermal_curve_learning_mode_check_failed", error_type=type(exc).__name__)
        learning_mode_state = LearningModeState.UNKNOWN
    learning_mode = learning_mode_state is LearningModeState.ACTIVE
    dhw_minutes_per_hour = [0.0] * hours
    plan_id = None
    plan_driven = False
    async with get_session() as session:
        plan_result = await session.execute(active_plan_query(now))
        active_plan = plan_result.scalar_one_or_none()
        if active_plan is None:
            diagnostic_result = await session.execute(current_diagnostic_query(now))
            active_plan = diagnostic_result.scalar_one_or_none()
        if active_plan:
            plan_id = active_plan.id

    tank_standby_curve = thermal_model.predict_temperature_curve(
        current_temp=current_tank,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=None,
        is_tank=True,
    )
    if plan_driven:
        # "With heating" follows the optimizer's actual DHW schedule: reheat
        # during planned hot-water cycles, coast on standby loss in between.
        tank_heating_curve = thermal_model.predict_planned_tank_curve(
            current_temp=current_tank,
            outdoor_temp=outdoor,
            tank_target=float(tank_target),
            dhw_minutes_per_hour=dhw_minutes_per_hour,
            hours=hours,
        )
    else:
        # No executable plan (or learning mode): fall back to the comfort-schedule
        # deadband — coast to the per-hour floor and reheat to target.
        tank_heating_curve = thermal_model.predict_managed_tank_curve(
            current_temp=current_tank,
            outdoor_temp=outdoor,
            tank_target=float(tank_target),
            tank_min_per_hour=tank_min_per_hour,
            hours=hours,
        )
    zone_standby_curve = thermal_model.predict_temperature_curve(
        current_temp=current_zone,
        outdoor_temp=outdoor,
        hours=hours,
        target_temp=None,
        is_tank=False,
    )

    from packages.core.house_contract import current_revision_matches

    async with get_session() as session:
        if not await current_revision_matches(
            session, contract_revision.id, contract_revision.contract_hash
        ):
            return {"error": "settings_changed_during_forecast"}
    if not await thermal_model.dependency_context_is_current(thermal_dependency_context):
        raise HTTPException(
            status_code=503,
            detail="Thermal model dependencies changed during forecast; retry",
        )

    return {
        "current": {
            "tank_temp": current_tank,
            "tank_target": tank_target,
            "outdoor_temp": outdoor,
            "zone1_temp": current_zone,
            "tank_min_temp": tank_min_temp,
            "tank_min_temp_offpeak": tank_min_temp_offpeak,
            "plan_driven": plan_driven,
            "learning_mode": learning_mode,
            "learning_mode_state": learning_mode_state.value,
            "learning_mode_reliable": learning_mode_state is not LearningModeState.UNKNOWN,
            "plan_id": plan_id,
        },
        "planning_contract": {
            "revision_id": contract_revision.id,
            "contract_hash": contract_revision.contract_hash,
            "ordinary_writes_allowed": False,
        },
        "curves": {
            "tank_standby": tank_standby_curve,
            "tank_heating": tank_heating_curve,
            "zone_standby": zone_standby_curve,
        },
    }


@router.get("/api/thermal/heat-curve-advice")
async def get_heat_curve_advice():
    """Explain a small, manual Panasonic curve adjustment from live comfort data."""
    from packages.core.control_temperature import get_control_temperature
    from packages.core.device_data_quality import get_device_data_quality
    from packages.core.heat_curve import (
        HEAT_CURVE_SETTING_KEYS,
        evaluate_heat_curve_verification,
        heat_curve_advice,
        start_heat_curve_verification,
    )
    from packages.core.settings_service import (
        get_float_setting,
        get_heat_curve_config,
        get_heat_curve_verification_state,
        get_space_heating_gate_config,
    )
    from packages.core.space_heating_gate import resolve_effective_gate

    heat_curve = await get_heat_curve_config()
    gate_config = await get_space_heating_gate_config()
    comfort_target = await get_float_setting("comfort_temp_target")
    verification_state = await get_heat_curve_verification_state()
    now = dt.datetime.now(dt.timezone.utc)

    async with get_session(read_only=True) as session:
        status = (
            await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts <= now)
                .order_by(desc(DeviceStatusRecord.ts))
                .limit(1)
            )
        ).scalar_one_or_none()
        status_quality = await get_device_data_quality(now=now, session=session, status=status)
        control_temperature = await get_control_temperature(now=now, session=session)
        indoor_temp = (
            control_temperature.value
            if control_temperature.is_usable and is_finite_number(control_temperature.value)
            else None
        )
        gate_row = (
            (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == status.device_id
                    )
                )
            ).scalar_one_or_none()
            if status is not None
            else None
        )

        # The first deployment of this feature may happen just after a user
        # saved a curve. Recover a recent matching audit entry so that change
        # begins its evidence window instead of receiving another suggestion.
        if not verification_state:
            audit_rows = (
                (
                    await session.execute(
                        select(AuditLogRecord)
                        .where(AuditLogRecord.action == "update_settings")
                        .order_by(desc(AuditLogRecord.ts))
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            recent_curve_audit = None
            for audit in audit_rows:
                if audit.ts < now - dt.timedelta(days=7):
                    break
                try:
                    payload = json.loads(audit.payload_json or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and any(
                    key in payload for key in HEAT_CURVE_SETTING_KEYS
                ):
                    recent_curve_audit = audit
                    break
            if recent_curve_audit is not None:
                baseline_indoor = (
                    await session.execute(
                        select(IndoorTempReading.temperature)
                        .where(IndoorTempReading.timestamp <= recent_curve_audit.ts)
                        .order_by(IndoorTempReading.timestamp.desc())
                        .limit(1)
                    )
                ).scalar()
                baseline_status = (
                    await session.execute(
                        select(DeviceStatusRecord)
                        .where(DeviceStatusRecord.ts <= recent_curve_audit.ts)
                        .order_by(desc(DeviceStatusRecord.ts))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                verification_state = start_heat_curve_verification(
                    started_at=recent_curve_audit.ts.isoformat(),
                    previous_curve=None,
                    applied_curve=heat_curve,
                    baseline_indoor_temp=(
                        float(baseline_indoor) if baseline_indoor is not None else None
                    ),
                    baseline_outdoor_temp=(
                        float(baseline_status.outdoor_temp)
                        if baseline_status is not None and baseline_status.outdoor_temp is not None
                        else None
                    ),
                    comfort_target=comfort_target,
                    source="audit_recovery",
                )

        try:
            started_at = dt.datetime.fromisoformat(str(verification_state.get("started_at")))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=dt.timezone.utc)
        except (AttributeError, TypeError, ValueError):
            started_at = None

        indoor_samples: list[float] = []
        heating_condition_samples = 0
        if started_at is not None and verification_state.get("status") != "verified":
            indoor_samples = [
                float(value)
                for value in (
                    await session.execute(
                        select(IndoorTempReading.temperature)
                        .where(IndoorTempReading.timestamp > started_at)
                        .order_by(IndoorTempReading.timestamp)
                    )
                )
                .scalars()
                .all()
            ]
            status_rows = (
                (
                    await session.execute(
                        select(DeviceStatusRecord.outdoor_temp).where(
                            DeviceStatusRecord.ts > started_at,
                            DeviceStatusRecord.outdoor_temp.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            heating_condition_samples = sum(
                1
                for temperature in status_rows
                if float(temperature) < heat_curve.heating_off_outdoor_c
            )

        outdoor_temp = (
            float(status.outdoor_temp)
            if status is not None
            and is_finite_number(status.outdoor_temp)
            and status_quality["ready"]
            else None
        )
        gate_evidence = resolve_effective_gate(gate_row, gate_config)
    elapsed_hours = (now - started_at).total_seconds() / 3600 if started_at is not None else 0.0
    verification = evaluate_heat_curve_verification(
        verification_state,
        elapsed_hours=elapsed_hours,
        indoor_samples=indoor_samples,
        heating_condition_samples=heating_condition_samples,
        current_comfort_target=comfort_target,
    )
    if (
        verification_state
        and verification["status"] == "verified"
        and verification_state.get("status") != "verified"
    ):
        verification["completed_at"] = now.isoformat()

    readings = {
        "indoor_temp_c": float(indoor_temp) if indoor_temp is not None else None,
        "comfort_target_c": comfort_target,
        "outdoor_temp_c": outdoor_temp,
        "curve_supply_target_c": (
            round(heat_curve.supply_temperature(outdoor_temp), 1)
            if outdoor_temp is not None
            else None
        ),
        "controller_heating_enabled": (
            outdoor_temp < heat_curve.heating_off_outdoor_c if outdoor_temp is not None else None
        ),
    }
    advice = heat_curve_advice(
        heat_curve,
        float(indoor_temp) if indoor_temp is not None else None,
        comfort_target,
        outdoor_temp,
        gate_evidence,
    )
    if gate_evidence.state != "ALLOWED" or advice["status"] == "insufficient_data":
        return {
            **advice,
            "readings": readings,
            "verification": verification,
        }
    if not verification["recommendation_available"]:
        return {
            "status": "verification_pending",
            "indoor_error_c": (
                round(float(indoor_temp) - comfort_target, 1) if indoor_temp is not None else None
            ),
            "current": heat_curve.as_dict(),
            "suggested": None,
            "reasons": verification["reasons"],
            "manual_only": True,
            "readings": readings,
            "verification": verification,
        }
    return {
        **advice,
        "readings": readings,
        "verification": verification,
    }


@router.get("/api/thermal/indoor-forecast", response_model=IndoorForecastResponse)
async def get_indoor_forecast(hours: int = Query(24, ge=1, le=48)):
    from packages.core.comfort_assessment import build_comfort_assessment
    from packages.core.control_temperature import (
        get_control_temperature,
    )
    from packages.core.settings_service import (
        get_heat_curve_config,
        get_space_heating_gate_config,
    )
    from packages.ml.thermal import thermal_model

    from packages.core.house_contract import ensure_current_contract
    from packages.core.settings_service import get_setting

    try:
        # Capture persisted authority before any settings-dependent input read.
        # Release the contract lock before refresh/helpers acquire their own sessions.
        async with get_session() as session:
            contract_revision = await ensure_current_contract(session)
            dependency_context = await _forecast_dependency_context(session, contract_revision)
            weather_provider = (
                await get_setting("weather_provider", session=session) or "open-meteo"
            )
    except (AttributeError, TypeError, ValueError, SQLAlchemyError):
        return _numeric_forecast_unavailable("indoor_dependency_changed_during_forecast")

    await thermal_model.arefresh_if_changed()
    thermal_dependency_context = thermal_model.capture_dependency_context()
    forecast_quality = await _read_live_forecast_quality()
    price_area = await get_price_area()
    async with get_session() as session:
        status_result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = status_result.scalar_one_or_none()

        control_temperature = await get_control_temperature(session=session)
        gate_row = (
            (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == status.device_id
                    )
                )
            ).scalar_one_or_none()
            if status is not None
            else None
        )

        now = dt.datetime.now(dt.timezone.utc)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        forecast_start = hour_start + dt.timedelta(hours=1)
        forecast_end = forecast_start + dt.timedelta(hours=hours)
        weather_result = await session.execute(
            select(WeatherRecord)
            .where(
                WeatherRecord.ts >= forecast_start,
                WeatherRecord.ts < forecast_end,
                WeatherRecord.source == weather_provider,
            )
            .order_by(WeatherRecord.ts)
        )
        weather_records = weather_result.scalars().all()
        price_result = await session.execute(
            select(PriceRecord)
            .where(
                PriceRecord.ts >= forecast_start,
                PriceRecord.ts < forecast_end,
                PriceRecord.area == price_area,
            )
            .order_by(PriceRecord.ts)
        )
        price_records = price_result.scalars().all()

    if not status:
        return _numeric_forecast_unavailable("no_device_data")

    if any(
        value is not None and not is_finite_number(value)
        for value in (
            control_temperature.value,
            status.outdoor_temp,
            status.zone1_temp,
        )
    ):
        return _numeric_forecast_unavailable("nonfinite_current_forecast_input")
    if any(
        (value := getattr(record, field, None)) is not None and not is_finite_number(value)
        for record in weather_records
        for field in (
            "temperature",
            "wind_speed",
            "irradiance",
            "precipitation",
            "humidity",
            "cloud_cover",
        )
    ):
        return _numeric_forecast_unavailable("nonfinite_weather_forecast_input")
    if any(
        record.price_eur_per_kwh is not None and not is_finite_number(record.price_eur_per_kwh)
        for record in price_records
    ):
        return _numeric_forecast_unavailable("nonfinite_price_forecast_input")

    current_indoor = float(control_temperature.value) if control_temperature.is_usable else None
    outdoor = status.outdoor_temp if status.outdoor_temp is not None else 7.0
    weather_by_timestamp = {
        record.ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0): record
        for record in weather_records
    }
    price_by_timestamp = {
        record.ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0): record
        for record in price_records
    }
    weather_forecast = []
    for i in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=i)
        w = weather_by_timestamp.get(forecast_ts)
        imputed_fields: list[str] = []
        if w is not None:
            if w.temperature is None:
                imputed_fields.append("outdoor_temp")
            if w.wind_speed is None:
                imputed_fields.append("wind_speed")
            if getattr(w, "irradiance", None) is None:
                imputed_fields.append("irradiance")
            if getattr(w, "precipitation", None) is None:
                imputed_fields.append("precipitation")
            weather_forecast.append(
                {
                    "ts": forecast_ts.isoformat(),
                    "outdoor_temp": float(w.temperature)
                    if w.temperature is not None
                    else float(outdoor),
                    "wind_speed": float(w.wind_speed) if w.wind_speed is not None else 3.0,
                    "irradiance": float(getattr(w, "irradiance", 0.0) or 0.0),
                    "precipitation": (
                        float(w.precipitation)
                        if getattr(w, "precipitation", None) is not None
                        else None
                    ),
                    "hour": forecast_ts.hour,
                    "input_status": "imputed" if imputed_fields else "observed",
                    "imputed_fields": imputed_fields,
                }
            )
        else:
            weather_forecast.append(
                {
                    "ts": forecast_ts.isoformat(),
                    "outdoor_temp": float(outdoor),
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "precipitation": None,
                    "hour": forecast_ts.hour,
                    "input_status": "imputed",
                    "imputed_fields": [
                        "outdoor_temp",
                        "wind_speed",
                        "irradiance",
                        "precipitation",
                    ],
                }
            )

    try:
        heat_curve = await get_heat_curve_config()
        comfort_temp_min = float(contract_revision.contract_json["comfort_temp_min"])
        comfort_temp_max = float(contract_revision.contract_json["comfort_temp_max"])
    except ValueError:
        return _numeric_forecast_unavailable("nonfinite_or_invalid_forecast_setting")
    room_envelope = _safe_room_comfort_envelope(
        control_temperature, comfort_temp_max=comfort_temp_max
    )
    from packages.core.space_heating_gate import project_gate_states, resolve_effective_gate

    try:
        gate_config = await get_space_heating_gate_config()
    except ValueError:
        return _numeric_forecast_unavailable("nonfinite_or_invalid_gate_setting")
    effective_gate = resolve_effective_gate(gate_row, gate_config)
    gate_projections = project_gate_states(
        effective_gate,
        [row["outdoor_temp"] for row in weather_forecast],
        gate_config,
    )
    gate_payload = {
        "state": effective_gate.state,
        "reason": effective_gate.reason_code,
        "profile_id": effective_gate.profile_id,
        "on_operator": effective_gate.on_operator,
        "off_operator": effective_gate.off_operator,
        "base_c": effective_gate.base_c,
        "on_threshold_c": effective_gate.on_threshold_c,
        "off_threshold_c": effective_gate.off_threshold_c,
        "last_raw_outdoor_c": effective_gate.last_raw_outdoor_c,
        "fingerprint_matches": effective_gate.fingerprint_matches,
        "projected_states": [evidence.state for evidence in gate_projections],
    }
    # In the no-plan fallback, use the same outdoor-to-supply curve that the
    # Panasonic controller is set to, rather than freezing today's zone reading
    # across the whole forecast horizon.
    zone_water_temps = [
        heat_curve.planned_supply_temperature(row["outdoor_temp"]) for row in weather_forecast
    ]
    planned_actions = []
    plan_snapshot = None
    active_plan = None
    async with get_session() as session:
        plan_result = await session.execute(active_plan_query(now))
        active_plan = plan_result.scalar_one_or_none()
        if active_plan is None:
            diagnostic_result = await session.execute(current_diagnostic_query(now))
            active_plan = diagnostic_result.scalar_one_or_none()

        if active_plan:
            plan_snapshot = _plan_forecast_window(active_plan, forecast_start, hours)
            if plan_snapshot is not None:
                actions_result = await session.execute(
                    select(PlanActionRecord)
                    .where(PlanActionRecord.plan_id == active_plan.id)
                    .order_by(PlanActionRecord.scheduled_ts)
                )
                plan_actions = actions_result.scalars().all()
                hour_start = now.replace(minute=0, second=0, microsecond=0)
                for action in plan_actions:
                    hour_offset = int((action.scheduled_ts - hour_start).total_seconds() / 3600)
                    if hour_offset < 0 or hour_offset >= hours:
                        continue

                    if action.action_type in (
                        "zone_temp_boost",
                        "comfort_mode_on",
                        "zone_temp_restore",
                        "eco_mode_on",
                        "eco_mode_off",
                        "normal_mode_on",
                        "force_dhw_on",
                        "force_dhw_off",
                        "quiet_mode_on",
                        "quiet_mode_off",
                    ):
                        payload = json.loads(action.payload_json) if action.payload_json else {}
                        planned_actions.append(
                            {
                                "hour": hour_offset,
                                "action_type": action.action_type,
                                "status": action.status,
                                "payload": payload,
                            }
                        )
    if plan_snapshot is not None and active_plan is not None:
        if not await _saved_plan_model_dependencies_are_current(active_plan):
            plan_snapshot = None

    if plan_snapshot is not None and active_plan is not None:
        if not await thermal_model.dependency_context_is_current(thermal_dependency_context):
            return _numeric_forecast_unavailable("thermal_dependency_changed_during_forecast")
        if not await _saved_plan_model_dependencies_are_current(active_plan):
            return _numeric_forecast_unavailable("saved_plan_model_dependencies_changed")
        snapshot_indoor = plan_snapshot.get("current_indoor")
        snapshot_weather = plan_snapshot.get("weather_forecast", [])
        plan_age_seconds = max(0, round((now - active_plan.created_at).total_seconds()))
        sensor_age_seconds = (
            max(0, round((now - control_temperature.latest_reading).total_seconds()))
            if control_temperature.latest_reading is not None
            else None
        )
        current_vs_plan_delta_c = (
            round(float(current_indoor) - float(snapshot_indoor), 1)
            if current_indoor is not None and snapshot_indoor is not None
            else None
        )
        if not control_temperature.is_usable:
            display_status = "stale"
        elif current_vs_plan_delta_c is not None and abs(current_vs_plan_delta_c) >= 1.0:
            display_status = "diverged"
        elif plan_age_seconds > 6 * 3600:
            display_status = "stale"
        elif plan_age_seconds > 2 * 3600:
            display_status = "aging"
        else:
            display_status = "fresh"
        assessment = _build_active_plan_comfort_assessment(
            plan_snapshot=plan_snapshot,
            planned_actions=planned_actions,
            heat_curve=heat_curve,
            gate_projections=gate_projections,
            room_envelope=room_envelope,
            comfort_temp_min=comfort_temp_min,
        )
        if not await _forecast_dependency_context_is_current(dependency_context):
            return _numeric_forecast_unavailable("indoor_dependency_changed_during_forecast")
        return {
            "current_indoor": snapshot_indoor if snapshot_indoor is not None else current_indoor,
            "outdoor_temp": snapshot_weather[0]["outdoor_temp"] if snapshot_weather else outdoor,
            "forecast": plan_snapshot.get("forecast", []),
            "forecast_with_plan": plan_snapshot.get("forecast_with_plan", []),
            "forecast_no_heating": plan_snapshot.get("forecast_no_heating", []),
            "target_schedule": plan_snapshot.get("target_schedule", []),
            "weather_forecast": snapshot_weather,
            "price_forecast": plan_snapshot.get("price_forecast", []),
            "planned_actions": planned_actions,
            "forecast_source": (
                "diagnostic_plan" if active_plan.status == "diagnostic" else "active_plan"
            )
            if plan_snapshot.get("forecast_status") != "unavailable"
            else "unavailable",
            "forecast_status": plan_snapshot.get("forecast_status", "unavailable"),
            "forecast_unavailable_reason": plan_snapshot.get("forecast_unavailable_reason"),
            "plan_id": active_plan.id,
            "plan_created_at": active_plan.created_at,
            "comfort_assessment": assessment,
            "forecast_provenance": {
                **(plan_snapshot.get("forecast_provenance", {})),
                "contract_revision_id": active_plan.contract_revision_id,
                "contract_hash": active_plan.contract_hash,
                "contract_state": active_plan.contract_state,
                "current_live_indoor_c": current_indoor,
                "plan_start_indoor_c": snapshot_indoor,
                "plan_created_at": active_plan.created_at.isoformat(),
            },
            "display_status": display_status,
            "plan_age_seconds": plan_age_seconds,
            "sensor_age_seconds": sensor_age_seconds,
            "current_vs_plan_delta_c": current_vs_plan_delta_c,
            "space_heating_gate": gate_payload,
            "forecast_quality": _reporting_forecast_quality(forecast_quality),
            "contract_revision_id": active_plan.contract_revision_id,
            "contract_hash": active_plan.contract_hash,
            "contract_state": active_plan.contract_state,
            "ordinary_writes_allowed": False,
        }

    # Rejected saved data must not contribute actions to the live assessment.
    planned_actions = []
    if current_indoor is None:
        if not await thermal_model.dependency_context_is_current(thermal_dependency_context):
            return _numeric_forecast_unavailable("thermal_dependency_changed_during_forecast")
        if not await _forecast_dependency_context_is_current(dependency_context):
            return _numeric_forecast_unavailable("indoor_dependency_changed_during_forecast")
        return {
            "current_indoor": None,
            "outdoor_temp": outdoor,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": weather_forecast,
            "price_forecast": [],
            "planned_actions": planned_actions,
            "forecast_source": "unavailable",
            "forecast_status": "unavailable",
            "forecast_unavailable_reason": control_temperature.reason
            or "no_trusted_indoor_observation",
            "plan_id": active_plan.id if active_plan else None,
            "plan_created_at": active_plan.created_at if active_plan else None,
            "display_status": "unavailable",
            "sensor_age_seconds": (
                max(0, round((now - control_temperature.latest_reading).total_seconds()))
                if control_temperature.latest_reading is not None
                else None
            ),
            "forecast_quality": _reporting_forecast_quality(forecast_quality),
        }

    # Schedule-aware comfort setpoint per hour, matching the optimizer: the home
    # is held near comfort_temp_target during comfort hours and allowed to set
    # back toward comfort_temp_min overnight/off-peak.
    contract_projection = contract_revision.contract_json
    comfort_schedule = contract_projection["comfort_schedule"]
    comfort_temp_target = float(contract_projection["comfort_temp_target"])
    comfort_temp_min_val = float(contract_projection["comfort_temp_min"])
    tz_name = contract_projection["timezone"]

    from packages.core.comfort_schedule import (
        CONTROL_SAMPLING_POLICY,
        resolve_hourly_targets,
        sample_control_target,
    )
    from packages.core.house_contract import RESOLVER_SCHEMA, policy_dependency_fingerprint

    target_policy_fingerprint = policy_dependency_fingerprint(contract_projection)

    resolved_targets = resolve_hourly_targets(
        comfort_schedule,
        tz_name,
        forecast_start,
        forecast_end,
        scheduled_target_c=comfort_temp_target,
        unscheduled_target_c=comfort_temp_min_val,
        outer_max_c=float(contract_projection["comfort_temp_max"]),
    )
    target_schedule = []
    indoor_target_per_hour = []
    for h, interval in enumerate(resolved_targets):
        interval["contract_revision_id"] = contract_revision.id
        interval["contract_hash"] = contract_revision.contract_hash
        target = float(interval["target_c"])
        in_comfort = bool(interval["scheduled"])
        target_schedule.append(
            {
                "hour": h + 1,
                "ts": interval["end_utc"],
                "target": target,
                "comfort_hour": in_comfort,
                "interval_start_utc": interval["start_utc"],
                "interval_end_utc": interval["end_utc"],
                "local_start": interval["local_start"],
                "local_end": interval["local_end"],
                "local_date": interval["local_date"],
                "local_hour": interval["local_hour"],
                "utc_offset_minutes": interval["utc_offset_minutes"],
                "fold": interval["fold"],
                "preferred_comfort": interval["preferred_comfort"],
                "outer_min_c": interval["outer_min_c"],
                "outer_max_c": interval["outer_max_c"],
                "reason": interval["reason"],
                "contract_revision_id": contract_revision.id,
                "contract_hash": contract_revision.contract_hash,
                "policy_dependency_fingerprint": target_policy_fingerprint,
                "resolver_schema": RESOLVER_SCHEMA,
                "timezone_policy": contract_projection.get("timezone_policy"),
                "tzdata_version": interval["tzdata_version"],
            }
        )

    indoor_target_per_hour = [
        float(
            sample_control_target(target_schedule, forecast_start + dt.timedelta(hours=h))["target"]
        )
        for h in range(hours)
    ]
    # "Predicted Indoor" = the home under the optimizer's schedule-aware control,
    # coasting toward the off-peak setback overnight and reheating during comfort
    # hours, rather than being held flat at the current zone temp.
    forecast_with_plan = thermal_model.predict_managed_indoor_curve(
        current_indoor=current_indoor,
        indoor_target_per_hour=indoor_target_per_hour,
        weather_forecast=weather_forecast,
        hours=hours,
    )
    # Base reference: indoor if the current zone water temp were held constantly
    # (full heating, no setback). Not plotted directly, but used as the upper
    # reference and to drive the frontend's per-hour iteration length.
    forecast = thermal_model.predict_indoor_curve(
        current_indoor=current_indoor,
        zone_water_temps=zone_water_temps,
        weather_forecast=weather_forecast,
        hours=hours,
        use_learned_forecast=bool(forecast_quality.get("control_allowed")),
    )
    # "No heating" baseline: the home with the heat pump fully off, drifting
    # toward outdoor through its envelope. A pure physical free-float, rather
    # than the ML comfort model fed a synthetic water_temp=outdoor (which
    # extrapolated and drifted the baseline upward in summer).
    forecast_no_heating = thermal_model.predict_free_float_curve(
        current_indoor=current_indoor,
        weather_forecast=weather_forecast,
        hours=hours,
    )

    _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

    for index in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=index)
        for curve in (forecast, forecast_with_plan, forecast_no_heating):
            if index < len(curve):
                curve[index]["interval_start_utc"] = forecast_ts.isoformat()
                curve[index]["interval_end_utc"] = (forecast_ts + dt.timedelta(hours=1)).isoformat()
                curve[index]["ts"] = curve[index]["interval_end_utc"]
                curve[index]["control_sampling_policy"] = CONTROL_SAMPLING_POLICY
                curve[index]["control_target_c"] = indoor_target_per_hour[index]

    price_forecast = []
    for index in range(hours):
        forecast_ts = forecast_start + dt.timedelta(hours=index)
        price = price_by_timestamp.get(forecast_ts)
        price_forecast.append(
            {
                "ts": forecast_ts.isoformat(),
                "price_eur_per_kwh": float(price.price_eur_per_kwh) if price else None,
            }
        )

    if not await thermal_model.dependency_context_is_current(thermal_dependency_context):
        return _numeric_forecast_unavailable("thermal_dependency_changed_during_forecast")

    if not await _forecast_dependency_context_is_current(dependency_context):
        return _numeric_forecast_unavailable("indoor_dependency_changed_during_forecast")
    return {
        "current_indoor": current_indoor,
        "outdoor_temp": outdoor,
        "forecast": forecast,
        "forecast_with_plan": forecast_with_plan,
        "forecast_no_heating": forecast_no_heating,
        "target_schedule": target_schedule,
        "weather_forecast": weather_forecast,
        "price_forecast": price_forecast,
        "planned_actions": planned_actions,
        "forecast_source": "live_estimate",
        "forecast_status": _forecast_status_from_quality(forecast_quality),
        "forecast_unavailable_reason": None,
        "plan_id": active_plan.id if active_plan else None,
        "plan_created_at": active_plan.created_at if active_plan else None,
        "comfort_assessment": build_comfort_assessment(
            forecast=forecast_with_plan,
            targets=target_schedule,
            weather=weather_forecast,
            planned_actions=planned_actions,
            heat_curve=heat_curve,
            gate_projections=gate_projections,
            forecast_status=_forecast_status_from_quality(forecast_quality),
            forecast_reason=forecast_quality.get("reason"),
            room_envelope=room_envelope,
            comfort_temp_min=comfort_temp_min,
        ),
        "observed_history": (
            [
                {
                    "hour": 0,
                    "ts": control_temperature.latest_reading.isoformat(),
                    "temperature": current_indoor,
                }
            ]
            if current_indoor is not None and control_temperature.latest_reading is not None
            else []
        ),
        "sensor_basis": {
            "method": "reference_sensor"
            if control_temperature.reference_sensor_id
            else "selected_sensor_median",
            "label": control_temperature.reference_sensor_label or "Selected sensor median",
        },
        "room_comfort": {
            "basis_temperature": room_envelope.basis_temperature if room_envelope else None,
            "fresh_inlier_min": room_envelope.fresh_inlier_min if room_envelope else None,
            "fresh_inlier_max": room_envelope.fresh_inlier_max if room_envelope else None,
            "affected_rooms": list(room_envelope.rooms_above_max) if room_envelope else [],
        },
        "forecast_quality": _reporting_forecast_quality(forecast_quality),
        "forecast_provenance": {
            "contract_revision_id": contract_revision.id,
            "contract_hash": contract_revision.contract_hash,
            "resolver_schema": "comfort_schedule_utc_half_open_v1",
            "ordinary_writes_allowed": False,
            "current_live_indoor_c": current_indoor,
            "control_input": {
                "confidence": control_temperature.confidence,
                "reference_sensor_id": control_temperature.reference_sensor_id,
                "reference_sensor_label": control_temperature.reference_sensor_label,
                "observed_at": (
                    control_temperature.latest_reading.isoformat()
                    if control_temperature.latest_reading
                    else None
                ),
            },
            "heat_curve": heat_curve.as_dict(),
        },
        "display_status": (
            "degraded"
            if any(row.get("input_status") == "imputed" for row in weather_forecast)
            else "fresh"
        ),
        "sensor_age_seconds": (
            max(0, round((now - control_temperature.latest_reading).total_seconds()))
            if control_temperature.latest_reading is not None
            else None
        ),
        "space_heating_gate": gate_payload,
    }


@router.get("/health")
async def health():
    from sqlalchemy import text

    from packages.core.database import engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unreachable")


@router.get("/health/ready")
async def readiness():
    """Readiness includes the data-collection and control service freshness."""

    from packages.core.backup_health import load_backup_health
    from packages.core.models import ServiceHeartbeatRecord
    from packages.core.device_data_quality import get_device_data_quality

    now = dt.datetime.now(dt.timezone.utc)
    service_cutoff = now - dt.timedelta(minutes=3)
    backup = await load_backup_health(now=now)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(ServiceHeartbeatRecord).where(
                        ServiceHeartbeatRecord.service.in_(["poller", "optimizer", "backup"])
                    )
                )
            )
            .scalars()
            .all()
        )

    by_service = {row.service: row.updated_at for row in rows}
    from packages.core.planning_data_quality import get_planning_data_quality

    planning_data_quality = await get_planning_data_quality(now=now)
    device_quality = await get_device_data_quality(now=now)
    stale = [
        service
        for service, cutoff in {
            "poller": service_cutoff,
            "optimizer": service_cutoff,
        }.items()
        if by_service.get(service) is None or by_service[service] < cutoff
    ]
    if not backup["ready"]:
        stale.append("backup")
    if not device_quality["ready"]:
        stale.append("device_status")
    if not planning_data_quality["control_allowed"]:
        stale.append("planning_inputs")
    if stale:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "stale": stale,
                "device_data_quality": _device_data_quality_payload(device_quality),
                "planning_data_quality": planning_data_quality,
                "backup": backup,
            },
        )
    return {
        "status": "ready",
        "services": {service: by_service[service].isoformat() for service in by_service},
        "data": {
            **_device_data_quality_payload(device_quality),
        },
        "backup": backup,
        "planning_data_quality": planning_data_quality,
    }


def _device_data_quality_payload(quality: dict) -> dict:
    timestamp = quality["timestamp"]
    return {
        "credentials_configured": quality["credentials_configured"],
        "latest_device_status": timestamp.isoformat() if timestamp else None,
        "age_seconds": quality["age_seconds"],
        "stale_after_seconds": quality["threshold_seconds"],
        "reasons": quality["reasons"],
    }

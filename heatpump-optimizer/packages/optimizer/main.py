"""Optimizer service: generates cost-optimal heating plans and executes them."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import math
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select, update

from packages.core.database import get_session
from packages.core.models import (
    OptimizationRequestRecord,
    PlanActionRecord,
    PlanRecord,
)
from packages.core.plan_lifecycle import DIAGNOSTIC_PLAN_STATUS
from packages.core.planning_data_quality import (
    MAX_PRICE_AGE,
    MAX_WEATHER_ISSUE_AGE,
    MINIMUM_CONTROL_HOURS,
    get_planning_data_quality,
)
from packages.core.device_data_quality import get_device_data_quality
from packages.core.services import AquareaWrapper
from packages.core.scheduling import create_scheduler, utc_after, utc_now
from packages.core.settings_service import get_setting
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError
from packages.optimizer.rules import RulesOptimizer
from packages.optimizer.milp import MILPOptimizer
from packages.optimizer.executor import PlanExecutor, resolve_learning_mode_state
from packages.optimizer.executor_core import LearningModeState
from packages.ml.models import DemandModel
from packages.ml.comfort_model import comfort_model
from packages.ml.thermal import thermal_model

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.ml.lifecycle import ArtifactReference

logger = structlog.get_logger()

# Cooldown: skip scheduled optimization if a plan was generated recently
_OPTIMIZATION_COOLDOWN_S = 300  # 5 minutes
_last_plan_generated_at: float | None = None
_PLAN_STABILITY_WINDOW_HOURS = 6
_OPTIMIZATION_REQUEST_TIMEOUT = dt.timedelta(minutes=30)
_ABANDONED_REQUEST_ERROR = (
    "Optimizer stopped before the request completed; submit a new optimization request."
)
_executor_shutdown_tasks: set[asyncio.Task[None]] = set()
_EXECUTOR_SHUTDOWN_WAIT_S = 10


def _validate_unique_restore_keys(restore_actions: list[dict]) -> None:
    """Reject multiple restores that point at the same safety source key."""

    seen_revert_keys: set[str] = set()
    for action in restore_actions:
        reverts_action_key = action["reverts_action_key"]
        if reverts_action_key in seen_revert_keys:
            raise ValueError(f"duplicate safety restore key: {reverts_action_key}")
        seen_revert_keys.add(reverts_action_key)


def _action_instant(value: str | dt.datetime) -> dt.datetime:
    instant = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(dt.timezone.utc)


def _planned_action_signature(
    action: dict,
) -> tuple[str, dt.datetime, tuple[tuple[str, object], ...]]:
    """Compare only device-relevant action fields, not changing explanations."""
    payload = action.get("payload") or {}
    relevant = tuple(
        sorted(
            (key, payload[key])
            for key in ("temperature", "offset", "level", "zone_id")
            if key in payload
        )
    )
    return str(action["type"]), _action_instant(action["ts"]), relevant


def _stored_action_signature(
    action: PlanActionRecord,
) -> tuple[str, dt.datetime, tuple[tuple[str, object], ...]]:
    payload = json.loads(action.payload_json) if action.payload_json else {}
    relevant = tuple(
        sorted(
            (key, payload[key])
            for key in ("temperature", "offset", "level", "zone_id")
            if key in payload
        )
    )
    return str(action.action_type), _action_instant(action.scheduled_ts), relevant


async def _has_material_near_term_change(session, plan: dict) -> tuple[bool, int | None]:
    """Avoid replacing a live plan when its next commands are identical."""
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now + dt.timedelta(hours=_PLAN_STABILITY_WINDOW_HOURS)
    active = (
        await session.execute(
            select(PlanRecord)
            .where(PlanRecord.status == "active", PlanRecord.horizon_end > now)
            .order_by(PlanRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is None:
        return True, None

    existing_actions = (
        (
            await session.execute(
                select(PlanActionRecord).where(
                    PlanActionRecord.plan_id == active.id,
                    PlanActionRecord.status == "pending",
                    PlanActionRecord.scheduled_ts >= now,
                    PlanActionRecord.scheduled_ts < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    proposed_actions = []
    for action in plan["actions"]:
        timestamp = dt.datetime.fromisoformat(action["ts"])
        if now <= timestamp < cutoff:
            proposed_actions.append(action)

    return (
        {_stored_action_signature(action) for action in existing_actions}
        != {_planned_action_signature(action) for action in proposed_actions},
        active.id,
    )


_PRICE_SOURCES = frozenset({"entsoe", "tibber", "manual"})
_WEATHER_SOURCES = frozenset({"open-meteo", "smhi", "manual"})
_ADMISSIBLE_DIAGNOSTIC_STATUS = "conditional_feasible"


class _PlanAdmissionRefused(ValueError):
    """Stable refusal reason; never carries the rejected payload."""


def _canonical_contract_target_schedule(snapshot: dict) -> list[dict]:
    """Build the saved hourly schedule from the frozen contract projection."""
    from packages.core.house_contract import canonical_target_schedule

    return canonical_target_schedule(snapshot)


def _target_schedule_matches_contract(plan: dict, canonical: list[dict]) -> bool:
    candidate = plan.get("target_schedule")
    if candidate is None and isinstance(plan.get("forecast_snapshot"), dict):
        candidate = plan["forecast_snapshot"].get("target_schedule")
    if not isinstance(candidate, list):
        return True
    if not candidate:
        return True
    if not canonical:
        return False
    try:
        previous_end = None
        for interval in canonical:
            interval_start = _aware_instant(interval["interval_start_utc"])
            interval_end = _aware_instant(interval["interval_end_utc"])
            if (
                interval_end <= interval_start
                or (previous_end is not None and interval_start != previous_end)
                or not math.isfinite(float(interval["target"]))
                or type(interval["comfort_hour"]) is not bool
            ):
                return False
            _aware_instant(interval["ts"])
            previous_end = interval_end
    except (KeyError, TypeError, ValueError, _PlanAdmissionRefused):
        return False
    # Solvers emit hourly control slots, sampling the exact policy at slot
    # start. Canonical policy may split that slot at local-hour/DST boundaries.
    start = _aware_instant(canonical[0]["interval_start_utc"])
    end = _aware_instant(canonical[-1]["interval_end_utc"])
    slots = []
    cursor = start
    while cursor < end:
        slot_end = min(cursor + dt.timedelta(hours=1), end)
        expected = next(
            interval
            for interval in canonical
            if _aware_instant(interval["interval_start_utc"])
            <= cursor
            < _aware_instant(interval["interval_end_utc"])
        )
        slots.append({**expected, "ts": slot_end.isoformat()})
        cursor = slot_end
    # Fully materialized projections are also accepted, with the same strict
    # target/membership comparison; never silently replace a mismatched target.
    expected_targets = canonical if len(candidate) == len(canonical) else slots
    if len(candidate) != len(expected_targets):
        return False
    for planned, expected in zip(candidate, expected_targets, strict=True):
        if not isinstance(planned, dict):
            return False
        if "ts" in planned:
            try:
                if _aware_instant(planned["ts"]) != _aware_instant(expected["ts"]):
                    return False
            except _PlanAdmissionRefused:
                return False
        try:
            if (
                not math.isfinite(float(planned["target"]))
                or abs(float(planned["target"]) - float(expected["target"])) > 1e-6
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if "comfort_hour" in planned and planned["comfort_hour"] is not expected["comfort_hour"]:
            return False
    return True


def _apply_authoritative_contract_snapshot(plan: dict, snapshot: dict) -> list[dict] | None:
    """Replace planner projections with the revision-bound house contract."""
    canonical = _canonical_contract_target_schedule(snapshot)
    if not _target_schedule_matches_contract(plan, canonical):
        return None
    plan["target_schedule"] = canonical
    plan["house_contract_snapshot"] = snapshot
    forecast_snapshot = plan.get("forecast_snapshot")
    if isinstance(forecast_snapshot, dict):
        forecast_snapshot["target_schedule"] = canonical
        forecast_snapshot["house_contract_snapshot"] = snapshot
    return canonical


@dataclass(frozen=True)
class _FrozenPlanningRevision:
    revision_id: int
    contract_hash: str
    dependency_generation: int
    indoor_dependency_generation: int

    def __iter__(self):
        return iter((self.revision_id, self.contract_hash))


@dataclass(frozen=True)
class _FrozenNativeModelContext:
    component: str
    reference: ArtifactReference | None
    serving_generation: int
    scope: dict[str, Any] | None


async def _capture_native_model_contexts() -> tuple[_FrozenNativeModelContext, ...] | None:
    from packages.ml.forecast_quality import locked_native_scope
    from packages.ml.lifecycle import Lifecycle, reference_is_qualified

    models = (("comfort", comfort_model), ("thermal", thermal_model))
    contexts = []
    for component, model in models:
        reference = copy.deepcopy(getattr(model, "_qualified_reference", None))
        serving_generation = getattr(model, "_serving_generation", 0)
        if reference is None:
            contexts.append(_FrozenNativeModelContext(component, None, serving_generation, None))
            continue
        if not reference_is_qualified(reference):
            return None
        capabilities = tuple(reference.manifest.get("capabilities", ()))
        async with get_session() as session:
            async with locked_native_scope(
                component,
                device_id=reference.manifest["device_id"],
                capabilities=capabilities,
                session=session,
            ) as (scope, _):
                state = await Lifecycle().read(session=session)
                if (
                    state.reference(component) != reference
                    or scope != reference.manifest.get("scope")
                    or not reference_is_qualified(reference, scope=scope)
                ):
                    return None
        contexts.append(
            _FrozenNativeModelContext(
                component, reference, serving_generation, copy.deepcopy(scope)
            )
        )
    return tuple(contexts)


async def _native_model_contexts_are_current(
    contexts: tuple[_FrozenNativeModelContext, ...],
    *,
    session: AsyncSession,
) -> bool:
    from packages.core.house_contract import _lock_contract
    from packages.core.models import MLLifecycleStateRecord
    from packages.ml.forecast_quality import locked_native_scope
    from packages.ml.lifecycle import Lifecycle, reference_is_qualified

    lifecycle_state = None
    artifact_contexts = [context for context in contexts if context.reference is not None]
    if artifact_contexts:
        await _lock_contract(session)
        state_row = await session.scalar(
            select(MLLifecycleStateRecord).where(MLLifecycleStateRecord.id == 1).with_for_update()
        )
        if state_row is None:
            return False
        lifecycle = Lifecycle()
        lifecycle_state = await lifecycle.read(session=session)
    for context in contexts:
        model = comfort_model if context.component == "comfort" else thermal_model
        if (
            getattr(model, "_qualified_reference", None) != context.reference
            or getattr(model, "_serving_generation", 0) != context.serving_generation
        ):
            return False
        if context.reference is None:
            continue
        current_reference = lifecycle_state.reference(context.component)
        if current_reference != context.reference:
            return False
        capabilities = tuple(context.reference.manifest.get("capabilities", ()))
        async with locked_native_scope(
            context.component,
            device_id=context.reference.manifest["device_id"],
            capabilities=capabilities,
            session=session,
        ) as (scope, _):
            if scope != context.scope or not reference_is_qualified(context.reference, scope=scope):
                return False
    return True


async def _freeze_contract_revision() -> _FrozenPlanningRevision:
    """Read the current contract revision before any planner solve."""
    from packages.core.house_contract import ensure_current_contract
    from packages.core.models import HouseInstallationRecord

    async with get_session() as session:
        revision = await ensure_current_contract(session)
        installation = await session.get(HouseInstallationRecord, 1)
        generation = installation.demand_dependency_generation
        indoor_generation = installation.indoor_dependency_generation
    return _FrozenPlanningRevision(
        revision.id, revision.contract_hash, generation, indoor_generation
    )


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _PlanAdmissionRefused("invalid_integer_field")
    return value


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _PlanAdmissionRefused("invalid_boolean_field")
    return value


def _aware_instant(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _PlanAdmissionRefused("malformed_clock") from exc
    else:
        raise _PlanAdmissionRefused("malformed_clock")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise _PlanAdmissionRefused("naive_clock")
    return instant.astimezone(dt.timezone.utc)


def _frozen_clock(value: object) -> dt.datetime:
    """Frozen producer provenance carries ISO strings; plan horizon clocks may be datetimes."""
    if not isinstance(value, str):
        raise _PlanAdmissionRefused("malformed_clock")
    return _aware_instant(value)


def _section(quality: dict, key: str) -> dict:
    value = quality.get(key)
    if not isinstance(value, dict):
        raise _PlanAdmissionRefused(f"missing_{key}_provenance")
    return value


def _feed_admission(
    feed: dict, *, clock_key: str, captured_at: dt.datetime, maximum: dt.timedelta, horizon: int
) -> int:
    contiguous = _strict_int(feed.get("contiguous_hours"))
    if not 0 <= contiguous <= horizon:
        raise _PlanAdmissionRefused("inconsistent_feed_hours")
    if _strict_bool(feed.get("complete_horizon")) is not (contiguous >= horizon):
        raise _PlanAdmissionRefused("inconsistent_feed_completeness")
    if feed.get("fresh") is not True:
        raise _PlanAdmissionRefused("feed_not_fresh")
    clock = _frozen_clock(feed.get(clock_key))
    elapsed = captured_at - clock
    if not dt.timedelta(0) <= elapsed <= maximum:
        raise _PlanAdmissionRefused("feed_clock_out_of_bounds")
    if _strict_int(feed.get("age_seconds")) != round(elapsed.total_seconds()):
        raise _PlanAdmissionRefused("inconsistent_feed_age")
    return contiguous


def _validated_planning_quality(plan: object, *, now: dt.datetime) -> dict:
    """Admit only a ready frozen-batch result whose returned provenance is self-consistent."""
    if not isinstance(plan, dict):
        raise _PlanAdmissionRefused("plan_not_mapping")
    quality = plan.get("planning_data_quality")
    if not isinstance(quality, dict):
        raise _PlanAdmissionRefused("missing_planning_data_quality")
    if quality.get("control_allowed") is not True:
        raise _PlanAdmissionRefused("control_not_allowed")
    if quality.get("status") != "ready" or quality.get("reasons") != []:
        raise _PlanAdmissionRefused("planning_quality_not_ready")
    status = plan.get("diagnostic_status")
    if not isinstance(status, str) or status != _ADMISSIBLE_DIAGNOSTIC_STATUS:
        raise _PlanAdmissionRefused("diagnostic_status_not_admissible")

    captured_at = _frozen_clock(quality.get("captured_at"))
    if captured_at > now:
        raise _PlanAdmissionRefused("capture_after_admission")
    horizon_start = _frozen_clock(quality.get("horizon_start"))
    if horizon_start != horizon_start.replace(minute=0, second=0, microsecond=0):
        raise _PlanAdmissionRefused("horizon_start_not_hour_boundary")
    horizon = _strict_int(quality.get("horizon_hours"))
    minimum = _strict_int(quality.get("minimum_control_hours"))
    effective = _strict_int(quality.get("effective_horizon_hours"))
    if horizon <= 0 or minimum != MINIMUM_CONTROL_HOURS or not minimum <= effective <= horizon:
        raise _PlanAdmissionRefused("inconsistent_horizon_hours")

    price = _section(quality, "price")
    weather = _section(quality, "weather")
    area, source, currency = price.get("area"), price.get("source"), price.get("currency")
    if not isinstance(source, str) or source not in _PRICE_SOURCES:
        raise _PlanAdmissionRefused("invalid_price_context")
    if not isinstance(area, str) or not area.strip():
        raise _PlanAdmissionRefused("invalid_price_context")
    if source == "manual" and area != "manual":
        raise _PlanAdmissionRefused("invalid_price_context")
    if source == "tibber" and not re.fullmatch(r"tb:[0-9a-f]{28}", area):
        raise _PlanAdmissionRefused("invalid_price_context")
    if not isinstance(currency, str) or len(currency) != 3:
        raise _PlanAdmissionRefused("invalid_price_context")
    weather_source = weather.get("source")
    if not isinstance(weather_source, str) or weather_source not in _WEATHER_SOURCES:
        raise _PlanAdmissionRefused("invalid_weather_source")
    price_hours = _feed_admission(
        price,
        clock_key="latest_fetched_at",
        captured_at=captured_at,
        maximum=MAX_PRICE_AGE,
        horizon=horizon,
    )
    weather_hours = _feed_admission(
        weather,
        clock_key="latest_issued_at",
        captured_at=captured_at,
        maximum=MAX_WEATHER_ISSUE_AGE,
        horizon=horizon,
    )
    if effective != min(price_hours, weather_hours):
        raise _PlanAdmissionRefused("inconsistent_effective_horizon")
    limited = price_hours < horizon
    if _strict_bool(quality.get("price_horizon_limited")) is not limited:
        raise _PlanAdmissionRefused("inconsistent_price_limitation")
    if _strict_bool(quality.get("reoptimization_when_prices_extend")) is not limited:
        raise _PlanAdmissionRefused("inconsistent_price_limitation")
    # The producer always emits this key: 900 when price-limited, otherwise null.
    if "next_publication_check_seconds" not in price:
        raise _PlanAdmissionRefused("inconsistent_publication_check")
    publication_check = price["next_publication_check_seconds"]
    if limited:
        if _strict_int(publication_check) != 900:
            raise _PlanAdmissionRefused("inconsistent_publication_check")
    elif publication_check is not None:
        raise _PlanAdmissionRefused("inconsistent_publication_check")

    if _aware_instant(plan.get("horizon_start")) != horizon_start:
        raise _PlanAdmissionRefused("plan_horizon_mismatch")
    if _aware_instant(plan.get("horizon_end")) != horizon_start + dt.timedelta(hours=effective):
        raise _PlanAdmissionRefused("plan_horizon_mismatch")
    return quality


_optimization_lock = asyncio.Lock()

_demand_model = DemandModel()


def _new_rules_optimizer() -> RulesOptimizer:
    """Build rules using planning assumptions, never a COP estimator."""
    return RulesOptimizer()


def _load_ml_models() -> None:
    """Load only retained model checkpoints from disk."""
    _demand_model.load_latest()
    comfort_model.load_latest()
    thermal_model.load_latest()
    logger.info(
        "ml_models_loaded",
        cop_trained=False,
        demand_trained=_demand_model.is_trained,
        comfort_trained=comfort_model.is_trained,
        thermal_calibrated=thermal_model.params.last_calibrated is not None,
    )


def _selected_layer_version(layer_name: str) -> str:
    """Map an optimizer selection to the user-facing layer version string."""
    if layer_name == "milp":
        if _demand_model.is_trained:
            return f"{MILPOptimizer.VERSION}+ml"
        return MILPOptimizer.VERSION
    return RulesOptimizer.VERSION


async def get_optimizer_status_snapshot(
    layer: str,
    reload_models: bool = False,
    *,
    reporting_inputs: dict | None = None,
) -> dict[str, object]:
    """Return the selected layer and loaded model state for the given configuration."""
    if reporting_inputs is not None:
        layer_name = "milp" if layer == "milp_preferred" else "rules"
        demand_trained = bool(reporting_inputs.get("demand_trained"))
        return {
            "active_layer": f"{MILPOptimizer.VERSION}+ml"
            if layer_name == "milp" and demand_trained
            else MILPOptimizer.VERSION
            if layer_name == "milp"
            else RulesOptimizer.VERSION,
            "ordinary_writes_allowed": False,
            "control_ready": False,
            "build_policy": "observe_only",
            "configured_layer": layer,
            "effective_layer": layer_name,
            "selection_reason": "auto_milp_disabled_cop_retired"
            if layer == "auto"
            else "configured_layer",
            "cop_trained": False,
            "cop_source": "assumed",
            "cop_measured": False,
            "demand_trained": demand_trained,
            "planning_data_quality": reporting_inputs.get("planning_data_quality"),
        }
    layer_name, _ = await _select_optimizer(layer, reload_models=reload_models)
    try:
        planning_data_quality = await get_planning_data_quality()
    except Exception as exc:  # noqa: BLE001 - status must remain inspectable during an outage
        logger.warning("planning_data_quality_unavailable", error=str(exc))
        planning_data_quality = {
            "control_allowed": False,
            "status": "unavailable",
            "reasons": ["Planning input quality could not be checked."],
        }
    return {
        "active_layer": _selected_layer_version(layer_name),
        "ordinary_writes_allowed": False,
        "control_ready": False,
        "build_policy": "observe_only",
        "configured_layer": layer,
        "effective_layer": layer_name,
        "selection_reason": (
            "auto_milp_disabled_cop_retired" if layer == "auto" else "configured_layer"
        ),
        "cop_trained": False,
        "cop_source": "assumed",
        "cop_measured": False,
        "demand_trained": _demand_model.is_trained,
        "planning_data_quality": planning_data_quality,
    }


async def _select_optimizer(
    layer: str,
    reload_models: bool = False,
) -> tuple[str, object]:
    """Return (layer_name, optimizer_instance) based on the configured layer setting."""
    if reload_models:
        _load_ml_models()

    if layer == "milp_preferred":
        return "milp", MILPOptimizer(
            demand_model=_demand_model if _demand_model.is_trained else None,
        )

    if layer == "auto":
        logger.info(
            "auto_milp_disabled_cop_retired",
            configured_layer=layer,
            effective_layer="rules",
            cop_source="assumed",
        )
    return "rules", _new_rules_optimizer()


async def run_optimization(
    *,
    scheduled: bool = False,
    force_replace: bool = False,
    expected_contract_revision_id: int | None = None,
    expected_contract_hash: str | None = None,
) -> int | None:
    """Store a non-executable diagnostic with layer selection and fallback.

    Args:
        scheduled: True when called by the hourly scheduler.  If a plan was
            generated recently (within _OPTIMIZATION_COOLDOWN_S), the
            scheduled run is skipped so a manual "Optimize Now" plan isn't
            immediately overwritten.
        force_replace: Retained for request compatibility; diagnostics never
            reuse, activate, or replace executable plans in this build.
    """
    import time as _time

    global _last_plan_generated_at

    if scheduled and _last_plan_generated_at is not None:
        elapsed = _time.monotonic() - _last_plan_generated_at
        if elapsed < _OPTIMIZATION_COOLDOWN_S:
            logger.info(
                "scheduled_optimization_skipped",
                reason="recent plan exists",
                seconds_since_last=round(elapsed),
                cooldown=_OPTIMIZATION_COOLDOWN_S,
            )
            return None

    # Serialize scheduled and queued diagnostic generation within this process.
    async with _optimization_lock:
        if (
            scheduled
            and _last_plan_generated_at is not None
            and _time.monotonic() - _last_plan_generated_at < _OPTIMIZATION_COOLDOWN_S
        ):
            return None
        try:
            learning_mode_state = await resolve_learning_mode_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - planner admission must fail closed
            logger.error(
                "optimization_learning_mode_check_failed",
                error_type=type(exc).__name__,
            )
            learning_mode_state = LearningModeState.UNKNOWN
        if learning_mode_state is LearningModeState.UNKNOWN:
            logger.info("optimization_skipped_learning_state_unknown")
            # Comfort-model refresh is deferred along with planning during UNKNOWN.
            return None
        try:
            frozen_contract = await _freeze_contract_revision()
            frozen_contract_revision_id, frozen_contract_hash = frozen_contract
            if expected_contract_revision_id is not None and (
                frozen_contract_revision_id != expected_contract_revision_id
                or frozen_contract_hash != expected_contract_hash
            ):
                logger.info(
                    "queued_request_contract_refused",
                    reason="refused_contract_changed",
                    requested_revision=expected_contract_revision_id,
                    current_revision=frozen_contract_revision_id,
                )
                return None
            layer = await get_setting("optimizer_layer") or "rules_only"
            await _demand_model.arefresh_committed()
            layer_name, optimizer = await _select_optimizer(layer, reload_models=True)
            await comfort_model.arefresh_if_changed()
            await thermal_model.arefresh_if_changed()
            native_model_contexts = await _capture_native_model_contexts()
            if native_model_contexts is None:
                logger.info(
                    "optimizer_no_plan",
                    reason="native_model_dependency_context_unavailable",
                )
                return None
            try:
                device_quality = await get_device_data_quality()
            except Exception as exc:  # noqa: BLE001 - readiness failures must block control
                logger.error(
                    "optimization_paused_device_data_quality_check_failed",
                    reason="quality_check_failed",
                    error_type=type(exc).__name__,
                )
                return None
            if not device_quality["ready"]:
                logger.warning(
                    "optimization_paused_device_data_quality",
                    reasons=device_quality["reasons"],
                )
                return None

            logger.info("optimization_starting", configured_layer=layer, selected=layer_name)

            plan = None
            if layer_name == "milp":
                try:
                    plan = await optimizer.generate_plan()
                except (InfeasibleError, DataIncompleteError, SolverTimeoutError) as exc:
                    logger.warning(
                        "milp_fallback_to_rules",
                        error=type(exc).__name__,
                        detail=str(exc),
                    )
                    plan = await _new_rules_optimizer().generate_plan()
                    if plan is not None:
                        plan["fallback_diagnostics"] = {
                            "milp_status": "conditional_infeasible"
                            if isinstance(exc, InfeasibleError)
                            else "unproven",
                            "reason": type(exc).__name__,
                        }
                except Exception as exc:
                    logger.error(
                        "milp_unexpected_error_fallback",
                        error=str(exc),
                    )
                    plan = await _new_rules_optimizer().generate_plan()
            else:
                plan = await optimizer.generate_plan()

            if plan is None:
                logger.info("optimizer_no_plan", reason="insufficient data")
                return None

            from packages.optimizer.represented_strategy import validate_represented_plan

            try:
                validate_represented_plan(plan)
            except (DataIncompleteError, InfeasibleError) as refusal:
                logger.info(
                    "optimizer_no_plan", reason="unproven_serialized_strategy", refusal=str(refusal)
                )
                return None

            from packages.core.pricing import PriceContext

            generated_at = dt.datetime.now(dt.timezone.utc)
            try:
                input_quality = _validated_planning_quality(plan, now=generated_at)
            except _PlanAdmissionRefused as refusal:
                logger.info(
                    "optimizer_no_plan",
                    reason="unvalidated_frozen_input_batch",
                    refusal=str(refusal),
                )
                return None
            price_input = input_quality["price"]
            price_context = PriceContext(
                area=price_input["area"],
                source=price_input["source"],
                currency=price_input["currency"],
            )
            demand_context = plan.get("demand_planning_context")
            demand_scope = demand_context["scope"] if demand_context else {}
            provenance = {
                "schema_version": 1,
                "generated_at": generated_at.isoformat(),
                "optimizer": {
                    "configured_layer": layer,
                    "selection_reason": (
                        "auto_milp_disabled_cop_retired" if layer == "auto" else "configured_layer"
                    ),
                },
                "heating_efficiency": {"source": "assumed", "measured": False},
                "price": {
                    "area": price_context.area,
                    "source": price_context.source,
                    "currency": price_context.currency,
                },
                "input_quality": input_quality,
                "models": {
                    "cop_trained": False,
                    "demand_trained": demand_context is not None,
                    "demand_serving_dependency": (
                        {
                            "artifact_digest": demand_context["artifact_hash"],
                            "dependency_schema": demand_scope.get("component_dependency_schema"),
                            "dependency_fingerprint": demand_scope.get(
                                "component_dependency_fingerprint"
                            ),
                            "dependency_generation": demand_scope.get(
                                "component_dependency_generation"
                            ),
                            "used_for_plan": bool(
                                layer_name == "milp" and not plan.get("fallback_diagnostics")
                            ),
                        }
                        if demand_context
                        else None
                    ),
                    "comfort": {
                        "trained": comfort_model.is_trained,
                        "control_readiness": {"ready": False, "reason": "observe_only_build"},
                        "metrics": comfort_model.metrics,
                    },
                    "thermal": {
                        "last_calibrated": (
                            thermal_model.params.last_calibrated.isoformat()
                            if thermal_model.params.last_calibrated
                            else None
                        ),
                        "indoor_heating_confidence": thermal_model.confidence_for("indoor_heating"),
                    },
                    "native_dependencies": [
                        {
                            "component": context.component,
                            "artifact_digest": (
                                context.reference.artifact_hash if context.reference else None
                            ),
                            "scope": context.scope,
                            "dependency_schema": (
                                context.scope.get("component_dependency_schema")
                                if context.scope
                                else None
                            ),
                            "dependency_fingerprint": (
                                context.scope.get("component_dependency_fingerprint")
                                if context.scope
                                else None
                            ),
                            "dependency_generation": (
                                context.scope.get("component_dependency_generation")
                                if context.scope
                                else None
                            ),
                        }
                        for context in native_model_contexts
                    ],
                },
            }
            plan["cop_source"] = "assumed"
            plan["cop_measured"] = False
            plan["input_provenance"] = provenance
            snapshot = plan.get("forecast_snapshot")
            if isinstance(snapshot, dict):
                from packages.core.plan_risk import price_risk_summary

                snapshot["price_context"] = provenance["price"]
                snapshot["input_quality"] = input_quality
                provenance["planning_horizon"] = {
                    "hours": len(snapshot.get("price_forecast", [])),
                    "price_horizon_limited": bool(input_quality.get("price_horizon_limited")),
                    "reoptimization_when_prices_extend": bool(
                        input_quality.get("reoptimization_when_prices_extend")
                    ),
                }
                provenance["price_risk"] = price_risk_summary(
                    snapshot.get("price_forecast", []), input_quality
                )
            plan["actions"] = []
            plan["diagnostic"] = True
            trace = plan.get("represented_strategy")
            if isinstance(trace, dict):
                trace["price_currency"] = price_context.currency
            replay = plan.get("serialized_replay")
            if isinstance(replay, dict):
                replay["price_currency"] = price_context.currency
            accounting = plan.get("cost_accounting")
            if isinstance(accounting, dict):
                accounting["price_currency"] = price_context.currency
            provenance["execution_policy"] = "observe_only_build"
            async with get_session() as session:
                from packages.core.house_contract import (
                    current_revision_matches,
                    snapshot_from_revision,
                )

                if not await current_revision_matches(
                    session, frozen_contract_revision_id, frozen_contract_hash
                ):
                    logger.info(
                        "plan_admission_contract_refused",
                        reason="refused_contract_changed",
                        frozen_revision=frozen_contract_revision_id,
                    )
                    return None
                if demand_context or any(
                    context.reference is not None for context in native_model_contexts
                ):
                    from packages.core.models import MLLifecycleStateRecord

                    lifecycle_row = await session.scalar(
                        select(MLLifecycleStateRecord)
                        .where(MLLifecycleStateRecord.id == 1)
                        .with_for_update()
                    )
                    if lifecycle_row is None:
                        logger.info(
                            "plan_admission_model_refused",
                            reason="lifecycle_authority_unavailable",
                        )
                        return None
                frozen_generation = getattr(frozen_contract, "dependency_generation", None)
                if frozen_generation is not None:
                    from packages.core.models import HouseInstallationRecord

                    installation = await session.get(HouseInstallationRecord, 1)
                    if installation.demand_dependency_generation != frozen_generation:
                        logger.info(
                            "plan_admission_demand_refused",
                            reason="planning_dependency_generation_changed",
                        )
                        return None
                    provenance["planning_dependency_generation"] = frozen_generation
                frozen_indoor_generation = getattr(
                    frozen_contract, "indoor_dependency_generation", None
                )
                if frozen_indoor_generation is not None:
                    from packages.core.models import HouseInstallationRecord

                    installation = await session.get(HouseInstallationRecord, 1)
                    if installation.indoor_dependency_generation != frozen_indoor_generation:
                        logger.info(
                            "plan_admission_indoor_model_refused",
                            reason="indoor_dependency_generation_changed",
                        )
                        return None
                    provenance["indoor_dependency_generation"] = frozen_indoor_generation
                if demand_context:
                    from packages.ml.forecast_quality import locked_demand_scope
                    from packages.ml.lifecycle import Lifecycle, reference_is_qualified

                    async with locked_demand_scope(
                        demand_context["device_id"], session=session
                    ) as (current_demand_scope, _):
                        reference = (await Lifecycle().read(session=session)).reference("demand")
                        if (
                            current_demand_scope != demand_scope
                            or not reference_is_qualified(reference, scope=demand_scope)
                            or reference.artifact_hash != demand_context["artifact_hash"]
                        ):
                            logger.info(
                                "plan_admission_demand_refused",
                                reason="demand_dependency_context_changed",
                            )
                            return None
                if not await _native_model_contexts_are_current(
                    native_model_contexts, session=session
                ):
                    logger.info(
                        "plan_admission_native_model_refused",
                        reason="native_model_dependency_context_changed",
                    )
                    return None
                house_contract_snapshot = await snapshot_from_revision(
                    session,
                    frozen_contract_revision_id,
                    _aware_instant(plan["horizon_start"]),
                    _aware_instant(plan["horizon_end"]),
                )
                canonical_target_schedule = _apply_authoritative_contract_snapshot(
                    plan, house_contract_snapshot
                )
                if canonical_target_schedule is None:
                    logger.info(
                        "optimizer_no_plan",
                        reason="contract_target_projection_mismatch",
                        contract_revision=frozen_contract_revision_id,
                    )
                    return None
                provenance["house_contract"] = {
                    "revision_id": frozen_contract_revision_id,
                    "contract_hash": frozen_contract_hash,
                    "policy_dependency_fingerprint": house_contract_snapshot[
                        "policy_dependency_fingerprint"
                    ],
                    "resolver_schema": house_contract_snapshot["resolver_schema"],
                }
                plan["input_provenance"] = provenance
                plan_record = PlanRecord(
                    horizon_start=plan["horizon_start"],
                    horizon_end=plan["horizon_end"],
                    # Store the complete immutable planning snapshot, not only the
                    # executable actions. This makes the forecast explain the
                    # actual weather, price and temperature assumptions used when
                    # the plan was solved.
                    plan_json=json.dumps(plan, default=str),
                    optimizer_version=plan["version"],
                    cost_estimate_eur=plan.get("cost_estimate"),
                    price_currency=price_context.currency,
                    price_source=price_context.source,
                    input_provenance_json=json.dumps(provenance, default=str),
                    status=DIAGNOSTIC_PLAN_STATUS,
                    status_reason="observe_only_build",
                    contract_revision_id=frozen_contract_revision_id,
                    contract_hash=frozen_contract_hash,
                    contract_state="current",
                )
                session.add(plan_record)
                await session.flush()

            _last_plan_generated_at = _time.monotonic()

            logger.info(
                "plan_generated",
                plan_id=plan_record.id,
                version=plan["version"],
                actions=len(plan["actions"]),
                cost_eur=plan.get("cost_estimate"),
            )
            return plan_record.id
        except Exception as e:
            logger.error("optimization_failed", error=str(e))
            return None


async def _fail_abandoned_optimization_requests(session, now: dt.datetime) -> int:
    """Fail claims that can no longer be owned by a live optimizer run.

    Automatically retrying is unsafe: the previous process may have committed
    a plan immediately before it stopped. A failed terminal state unblocks
    deduplication while making the uncertain outcome explicit to the caller.
    """

    cutoff = now - _OPTIMIZATION_REQUEST_TIMEOUT
    result = await session.execute(
        update(OptimizationRequestRecord)
        .where(
            OptimizationRequestRecord.status == "running",
            func.coalesce(
                OptimizationRequestRecord.started_at,
                OptimizationRequestRecord.requested_at,
            )
            < cutoff,
        )
        .values(
            status="failed",
            completed_at=now,
            error=_ABANDONED_REQUEST_ERROR,
        )
    )
    expired = result.rowcount or 0
    if expired:
        logger.warning(
            "abandoned_optimization_requests_failed",
            count=expired,
            timeout_minutes=int(_OPTIMIZATION_REQUEST_TIMEOUT.total_seconds() / 60),
        )
    return expired


async def process_pending_optimization_requests() -> None:
    """Claim and run queued manual optimizations in the optimizer service."""

    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        await _fail_abandoned_optimization_requests(session, now)
        result = await session.execute(
            select(OptimizationRequestRecord)
            .where(OptimizationRequestRecord.status == "pending")
            .order_by(OptimizationRequestRecord.requested_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        request = result.scalar_one_or_none()
        if request is None:
            return
        from packages.core.house_contract import ensure_current_contract

        current_contract = await ensure_current_contract(session)
        if (
            request.contract_revision_id is None
            or request.contract_hash is None
            or request.contract_revision_id != current_contract.id
            or request.contract_hash != current_contract.contract_hash
        ):
            request.status = "failed"
            request.completed_at = now
            request.error = "refused_contract_changed"
            logger.info(
                "queued_request_contract_refused",
                reason="refused_contract_changed",
                request_id=request.id,
            )
            return
        request.status = "running"
        request.started_at = now
        request_id = request.id
        expected_revision_id = request.contract_revision_id
        expected_contract_hash = request.contract_hash

    plan_id = await run_optimization(
        force_replace=True,
        expected_contract_revision_id=expected_revision_id,
        expected_contract_hash=expected_contract_hash,
    )
    completed_at = dt.datetime.now(dt.timezone.utc)
    async with get_session() as session:
        request = await session.get(OptimizationRequestRecord, request_id)
        if request is None:
            return
        request.completed_at = completed_at
        request.plan_id = plan_id
        if plan_id is None:
            request.status = "failed"
            request.error = "No diagnostic plan could be generated; check data availability."
            from packages.core.house_contract import ensure_current_contract

            current_contract = await ensure_current_contract(session)
            if (
                current_contract.id != expected_revision_id
                or current_contract.contract_hash != expected_contract_hash
            ):
                request.error = "refused_contract_changed"
        else:
            request.status = "completed"
            request.error = None


async def execute_pending_actions(wrapper: AquareaWrapper) -> None:
    """Execute any pending plan actions whose time has come."""
    current = asyncio.current_task()
    if current is not None:
        _executor_shutdown_tasks.add(current)

    executor = PlanExecutor(wrapper)
    try:
        from packages.core.service_health import record_safety_watchdog_result
        from packages.optimizer.shower_mode import reconcile_shower_expiry

        try:
            await reconcile_shower_expiry()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - later cycle steps remain independent
            logger.exception("executor_step_failed", step="reconcile_shower_expiry", error=str(exc))
            try:
                await record_safety_watchdog_result(success=False, reason=type(exc).__name__)
            except Exception as health_exc:  # noqa: BLE001
                logger.exception("safety_watchdog_health_write_failed", error=str(health_exc))
        else:
            try:
                await record_safety_watchdog_result(success=True)
            except Exception as health_exc:  # noqa: BLE001
                logger.exception("safety_watchdog_health_write_failed", error=str(health_exc))

        for step_name, step in (
            ("expire_stale_actions", executor.expire_stale_actions),
            ("execute_due_actions", executor.execute_due_actions),
        ):
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - each cycle phase is isolated
                logger.exception("executor_step_failed", step=step_name, error=str(exc))
    finally:
        if current is not None:
            _executor_shutdown_tasks.discard(current)


async def _shutdown_runtime(scheduler, wrapper: AquareaWrapper) -> None:
    """Stop scheduler and Panasonic client resources during service shutdown.

    ``wait=False`` avoids blocking on long in-flight jobs (for example action
    verification polling). We still explicitly await tracked executor cycles so
    cancellation reconciliation is durable before the process exits.
    """

    try:
        scheduler.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001 - best-effort shutdown path
        logger.warning("optimizer_scheduler_shutdown_failed", error=str(exc))

    try:
        pending = [task for task in _executor_shutdown_tasks if not task.done()]
        if pending:
            logger.info("optimizer_waiting_for_executor_shutdown", pending=len(pending))
            for task in pending:
                task.cancel()

            done, still_pending = await asyncio.wait(
                pending,
                timeout=_EXECUTOR_SHUTDOWN_WAIT_S,
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.warning("optimizer_executor_shutdown_task_failed", error=str(exc))

            if still_pending:
                logger.warning(
                    "optimizer_executor_shutdown_wait_timeout",
                    pending=len(still_pending),
                    timeout_seconds=_EXECUTOR_SHUTDOWN_WAIT_S,
                )
                raise RuntimeError("executor ownership unsettled; wrapper disposal forbidden")

            for task in done:
                _executor_shutdown_tasks.discard(task)
    except BaseException:
        raise
    else:
        await wrapper.stop()


async def _run_service(wrapper, scheduler) -> None:
    """Main entry point for the optimizer service."""
    from packages.core.logging import configure_logging
    from packages.core.service_health import record_service_heartbeat

    configure_logging("optimizer")

    logger.info("optimizer_starting")
    logger.info(
        "cop_retired",
        reason="unverified_thermal_output",
        cop_source="assumed",
        cop_measured=False,
    )

    # Load ML model checkpoints so MILP can use them
    _load_ml_models()

    from packages.ml.lifecycle import enqueue_maintenance_request

    try:
        device_quality = await get_device_data_quality()
    except Exception as exc:  # noqa: BLE001 - training requires verified device readiness
        logger.error(
            "comfort_model_initial_training_quality_check_failed",
            reason="quality_check_failed",
            error_type=type(exc).__name__,
        )
    else:
        if not device_quality["ready"]:
            logger.warning(
                "comfort_model_initial_training_paused", reasons=device_quality["reasons"]
            )
        elif not comfort_model.is_trained:
            logger.info("comfort_model_initial_training_needed")
            comfort_result = await enqueue_maintenance_request(
                "comfort",
                "optimizer_startup",
                automatic=True,
                dedup_key="startup:" + utc_now().strftime("%Y-%m-%dT%H"),
            )
            logger.info("comfort_model_initial_maintenance_requested", request=comfort_result)

    await wrapper.start()

    await record_service_heartbeat("optimizer")

    async def _scheduled_optimization():
        await run_optimization(scheduled=True)

    # Re-optimize every hour
    scheduler.add_job(
        _scheduled_optimization,
        "interval",
        hours=1,
        id="optimize",
        next_run_time=utc_after(seconds=60),
    )

    # Manual UI requests are durable DB jobs and are intentionally executed in
    # this service, never in the API container.
    scheduler.add_job(
        process_pending_optimization_requests,
        "interval",
        seconds=15,
        id="manual_optimization_requests",
        next_run_time=utc_after(seconds=15),
    )

    scheduler.add_job(
        record_service_heartbeat,
        "interval",
        minutes=1,
        args=["optimizer"],
        id="heartbeat",
        next_run_time=utc_after(minutes=1),
    )

    # Check for actions to execute every minute
    scheduler.add_job(
        execute_pending_actions,
        "interval",
        seconds=60,
        args=[wrapper],
        id="executor",
        next_run_time=utc_after(seconds=90),
    )

    scheduler.start()

    shutdown_event = asyncio.Event()

    def _signal_shutdown():
        shutdown_event.set()

    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_shutdown)
        except NotImplementedError:
            pass

    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass


async def main() -> None:
    from packages.core.runtime_ownership import close_service_resources, finish_owned_cleanup

    wrapper, scheduler = None, None
    try:
        wrapper = AquareaWrapper()
        scheduler = create_scheduler()
        await _run_service(wrapper, scheduler)
    finally:

        async def cleanup():
            from packages.core.database import engine

            await close_service_resources(scheduler=scheduler, wrapper=wrapper, engine=engine)

        await finish_owned_cleanup(cleanup)


if __name__ == "__main__":
    asyncio.run(main())

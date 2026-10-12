"""Optimizer service: generates cost-optimal heating plans and executes them."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import structlog
from sqlalchemy import func, select, update

from packages.core.database import get_session
from packages.core.models import (
    OptimizationRequestRecord,
    PlanActionRecord,
    PlanRecord,
)
from packages.core.plan_lifecycle import DIAGNOSTIC_PLAN_STATUS
from packages.core.planning_data_quality import get_planning_data_quality
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

logger = structlog.get_logger()

# Cooldown: skip scheduled optimization if a plan was generated recently
_OPTIMIZATION_COOLDOWN_S = 300  # 5 minutes
_last_plan_generated_at: float = 0.0
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


async def run_optimization(*, scheduled: bool = False, force_replace: bool = False) -> int | None:
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

    if scheduled:
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
        if scheduled and _time.monotonic() - _last_plan_generated_at < _OPTIMIZATION_COOLDOWN_S:
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
            layer = await get_setting("optimizer_layer") or "rules_only"
            await _demand_model.arefresh_committed()
            layer_name, optimizer = await _select_optimizer(layer, reload_models=True)
            await comfort_model.arefresh_if_changed()
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

            from packages.core.pricing import PriceContext

            input_quality = plan.get("planning_data_quality")
            if not isinstance(input_quality, dict) or not input_quality.get("control_allowed"):
                logger.info("optimizer_no_plan", reason="unvalidated_frozen_input_batch")
                return None
            price_input = input_quality["price"]
            price_context = PriceContext(
                area=price_input["area"],
                source=price_input["source"],
                currency=price_input["currency"],
            )
            generated_at = dt.datetime.now(dt.timezone.utc)
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
                    "demand_trained": _demand_model.is_trained,
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
        request.status = "running"
        request.started_at = now
        request_id = request.id

    plan_id = await run_optimization(force_replace=True)
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
            await close_service_resources(scheduler=scheduler, wrapper=wrapper)

        await finish_owned_cleanup(cleanup)


if __name__ == "__main__":
    asyncio.run(main())

"""Optimizer service: generates cost-optimal heating plans and executes them."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import structlog
from sqlalchemy import func, select, update

from packages.core.database import get_session
from packages.core.models import (
    COPRecord,
    ConsumptionRecord,
    OptimizationRequestRecord,
    PlanActionRecord,
    PlanRecord,
)
from packages.core.plan_lifecycle import activate_plan
from packages.core.planning_data_quality import get_planning_data_quality
from packages.core.pricing import get_active_price_context
from packages.core.services import AquareaWrapper
from packages.core.scheduling import create_scheduler, utc_after
from packages.core.settings_service import get_setting
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError
from packages.optimizer.actions import ActionType
from packages.optimizer.rules import RulesOptimizer
from packages.optimizer.milp import MILPOptimizer
from packages.optimizer.executor import PlanExecutor
from packages.ml.models import COPModel, DemandModel
from packages.ml.comfort_model import comfort_model
from packages.ml.thermal import thermal_model

logger = structlog.get_logger()

# Minimum data history (days) before trusting ML models in auto mode
ML_MIN_DATA_DAYS = 14

# Cooldown: skip scheduled optimization if a plan was generated recently
_OPTIMIZATION_COOLDOWN_S = 300  # 5 minutes
_last_plan_generated_at: float = 0.0
_PLAN_STABILITY_WINDOW_HOURS = 6
_OPTIMIZATION_REQUEST_TIMEOUT = dt.timedelta(minutes=30)
_ABANDONED_REQUEST_ERROR = (
    "Optimizer stopped before the request completed; submit a new optimization request."
)


def _planned_action_signature(action: dict) -> tuple[str, str, tuple[tuple[str, object], ...]]:
    """Compare only device-relevant action fields, not changing explanations."""
    payload = action.get("payload") or {}
    relevant = tuple(
        sorted(
            (key, payload[key])
            for key in ("temperature", "offset", "level", "zone_id")
            if key in payload
        )
    )
    return str(action["type"]), str(action["ts"]), relevant


def _stored_action_signature(
    action: PlanActionRecord,
) -> tuple[str, str, tuple[tuple[str, object], ...]]:
    payload = json.loads(action.payload_json) if action.payload_json else {}
    relevant = tuple(
        sorted(
            (key, payload[key])
            for key in ("temperature", "offset", "level", "zone_id")
            if key in payload
        )
    )
    return str(action.action_type), action.scheduled_ts.isoformat(), relevant


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

# Global ML model instances (loaded once, reused across optimization cycles)
_cop_model = COPModel()
_demand_model = DemandModel()


def _load_ml_models() -> None:
    """Load the latest ML model checkpoints from disk."""
    _cop_model.load_latest()
    _demand_model.load_latest()
    comfort_model.load_latest()
    thermal_model.load_latest()
    logger.info(
        "ml_models_loaded",
        cop_trained=_cop_model.is_trained,
        demand_trained=_demand_model.is_trained,
        comfort_trained=comfort_model.is_trained,
        thermal_calibrated=thermal_model.params.last_calibrated is not None,
    )


def _selected_layer_version(layer_name: str) -> str:
    """Map an optimizer selection to the user-facing layer version string."""
    if layer_name == "milp":
        if _cop_model.is_trained or _demand_model.is_trained:
            return f"{MILPOptimizer.VERSION}+ml"
        return MILPOptimizer.VERSION
    return RulesOptimizer.VERSION


async def get_optimizer_status_snapshot(
    layer: str,
    reload_models: bool = False,
) -> dict[str, object]:
    """Return the selected layer and loaded model state for the given configuration."""
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
        "cop_trained": _cop_model.is_trained,
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

    if layer == "rules_only":
        return "rules", RulesOptimizer()

    # milp_preferred or auto: build MILP with ML models if available
    milp = MILPOptimizer(
        cop_model=_cop_model if _cop_model.is_trained else None,
        demand_model=_demand_model if _demand_model.is_trained else None,
    )

    if layer == "milp_preferred":
        return "milp", milp

    # auto: use MILP only when ML models are trained AND have enough data history
    if _cop_model.is_trained and _demand_model.is_trained:
        if await _has_sufficient_ml_data():
            return "milp", milp
        else:
            logger.info(
                "auto_mode_insufficient_data",
                required_days=ML_MIN_DATA_DAYS,
                reason="ML models trained but less than 14 days of data history",
            )

    return "rules", RulesOptimizer()


async def _has_sufficient_ml_data() -> bool:
    """Check that we have at least ML_MIN_DATA_DAYS of COP and consumption history."""
    from sqlalchemy import func, select

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ML_MIN_DATA_DAYS)

    try:
        async with get_session() as session:
            cop_result = await session.execute(
                select(func.count()).select_from(COPRecord).where(COPRecord.ts >= cutoff)
            )
            cop_count = cop_result.scalar() or 0

            cons_result = await session.execute(
                select(func.count())
                .select_from(ConsumptionRecord)
                .where(ConsumptionRecord.ts >= cutoff)
            )
            cons_count = cons_result.scalar() or 0

        # Require at least some records spanning the period
        # 14 days × ~4 COP intervals/day = ~56; ~4 consumption records/day = ~56
        return cop_count >= 50 and cons_count >= 50
    except Exception as exc:
        logger.warning("ml_data_check_failed", error=str(exc))
        return False


async def run_optimization(*, scheduled: bool = False, force_replace: bool = False) -> int | None:
    """Run the optimizer and store the plan, with layer selection and fallback.

    Args:
        scheduled: True when called by the hourly scheduler.  If a plan was
            generated recently (within _OPTIMIZATION_COOLDOWN_S), the
            scheduled run is skipped so a manual "Optimize Now" plan isn't
            immediately overwritten.
        force_replace: True for an explicit manual replan. This bypasses only
            the near-term plan-stability comparison, so a user-requested
            safety correction can replace later pending actions as well.
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

    # The scheduler and a queued manual request share a process.  Serialising
    # the complete solve avoids two plans being calculated from the same state;
    # activate_plan then provides the database-level guard across processes.
    async with _optimization_lock:
        try:
            layer = await get_setting("optimizer_layer") or "rules_only"
            layer_name, optimizer = await _select_optimizer(layer, reload_models=True)

            # Rules are deliberately kept available as the conservative
            # fallback.  The richer ML/MILP plan needs a complete, fresh
            # forecast and price horizon; without it, rules can still make a
            # safe schedule from their own defensive data checks.
            if layer_name == "milp":
                try:
                    input_quality = await get_planning_data_quality()
                except Exception as exc:  # noqa: BLE001 - plan generation has its own data checks
                    logger.warning("planning_data_quality_unavailable", error=str(exc))
                    input_quality = {
                        "control_allowed": True,
                        "status": "unavailable",
                        "reasons": ["Planning input quality could not be checked."],
                        "price": {},
                        "weather": {},
                    }
            else:
                input_quality = {
                    "control_allowed": True,
                    "status": "rules_not_gated",
                    "reasons": [],
                    "price": {},
                    "weather": {},
                }

            if layer_name == "milp" and not input_quality["control_allowed"]:
                logger.warning(
                    "optimization_paused_degraded_input_data",
                    reasons=input_quality["reasons"],
                    price=input_quality["price"],
                    weather=input_quality["weather"],
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
                    plan = await RulesOptimizer().generate_plan()
                except Exception as exc:
                    logger.error(
                        "milp_unexpected_error_fallback",
                        error=str(exc),
                    )
                    plan = await RulesOptimizer().generate_plan()
            else:
                plan = await optimizer.generate_plan()

            if plan is None:
                logger.info("optimizer_no_plan", reason="insufficient data")
                return None

            # The active context opens its own short-lived read session. Fetch
            # it before the lifecycle transaction so no nested DB session is
            # held while a plan is being activated.
            price_context = await get_active_price_context()
            generated_at = dt.datetime.now(dt.timezone.utc)
            provenance = {
                "schema_version": 1,
                "generated_at": generated_at.isoformat(),
                "price": {
                    "area": price_context.area,
                    "source": price_context.source,
                    "currency": price_context.currency,
                },
                "input_quality": input_quality,
                "models": {
                    "cop_trained": _cop_model.is_trained,
                    "demand_trained": _demand_model.is_trained,
                    "comfort": {
                        "trained": comfort_model.is_trained,
                        "control_readiness": comfort_model.control_readiness,
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
            async with get_session() as session:
                if not force_replace:
                    is_material, existing_plan_id = await _has_material_near_term_change(
                        session, plan
                    )
                    if not is_material and existing_plan_id is not None:
                        logger.info(
                            "plan_reuse_no_material_near_term_change",
                            plan_id=existing_plan_id,
                            horizon_hours=_PLAN_STABILITY_WINDOW_HOURS,
                        )
                        return existing_plan_id

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
                    status="active",
                )
                await activate_plan(session, plan_record)

                for action in plan["actions"]:
                    action_record = PlanActionRecord(
                        plan_id=plan_record.id,
                        scheduled_ts=dt.datetime.fromisoformat(action["ts"]),
                        action_type=str(ActionType(action["type"])),
                        payload_json=json.dumps(action.get("payload", {})),
                        status="pending",
                    )
                    add_result = session.add(action_record)
                    if asyncio.iscoroutine(add_result):
                        await add_result

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
            request.error = "No executable plan could be generated; check data availability."
        else:
            request.status = "completed"
            request.error = None


async def execute_pending_actions(wrapper: AquareaWrapper) -> None:
    """Execute any pending plan actions whose time has come."""
    executor = PlanExecutor(wrapper)
    # Never "catch up" a command that belongs to an already-passed price
    # interval. Expire it before claiming newly due actions instead.
    await executor.expire_stale_actions()
    await executor.execute_due_actions()


async def main() -> None:
    """Main entry point for the optimizer service."""
    from packages.core.logging import configure_logging
    from packages.core.service_health import record_service_heartbeat

    configure_logging("optimizer")

    logger.info("optimizer_starting")

    # Load ML model checkpoints so MILP can use them
    _load_ml_models()

    # The comfort model's causal feature schema is versioned separately from
    # COP and demand. Train it once after a schema upgrade instead of running
    # an older, leaky checkpoint or waiting for a manual request.
    if not comfort_model.is_trained:
        logger.info("comfort_model_initial_training_needed")
        comfort_result = await comfort_model.train()
        logger.info("comfort_model_initial_training_finished", **comfort_result)

    wrapper = AquareaWrapper()
    await wrapper.start()

    scheduler = create_scheduler()
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
    finally:
        scheduler.shutdown(wait=True)
        await wrapper.stop()
        logger.info("optimizer_stopped")


if __name__ == "__main__":
    asyncio.run(main())

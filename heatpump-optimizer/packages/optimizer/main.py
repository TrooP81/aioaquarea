"""Optimizer service: generates cost-optimal heating plans and executes them."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.core.database import get_session
from packages.core.models import PlanActionRecord, PlanRecord, COPRecord, ConsumptionRecord
from packages.core.services import AquareaWrapper
from packages.core.settings_service import get_setting
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError
from packages.optimizer.rules import RulesOptimizer
from packages.optimizer.milp import MILPOptimizer
from packages.optimizer.executor import PlanExecutor
from packages.ml.models import COPModel, DemandModel

logger = structlog.get_logger()

# Minimum data history (days) before trusting ML models in auto mode
ML_MIN_DATA_DAYS = 14

# Global ML model instances (loaded once, reused across optimization cycles)
_cop_model = COPModel()
_demand_model = DemandModel()


def _load_ml_models() -> None:
    """Load the latest ML model checkpoints from disk."""
    _cop_model.load_latest()
    _demand_model.load_latest()
    logger.info(
        "ml_models_loaded",
        cop_trained=_cop_model.is_trained,
        demand_trained=_demand_model.is_trained,
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
) -> dict[str, str | bool]:
    """Return the selected layer and loaded model state for the given configuration."""
    layer_name, _ = await _select_optimizer(layer, reload_models=reload_models)
    return {
        "active_layer": _selected_layer_version(layer_name),
        "cop_trained": _cop_model.is_trained,
        "demand_trained": _demand_model.is_trained,
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
                select(func.count()).select_from(ConsumptionRecord).where(
                    ConsumptionRecord.ts >= cutoff
                )
            )
            cons_count = cons_result.scalar() or 0

        # Require at least some records spanning the period
        # 14 days × ~4 COP intervals/day = ~56; ~4 consumption records/day = ~56
        return cop_count >= 50 and cons_count >= 50
    except Exception as exc:
        logger.warning("ml_data_check_failed", error=str(exc))
        return False


async def run_optimization() -> None:
    """Run the optimizer and store the plan, with layer selection and fallback."""
    try:
        layer = await get_setting("optimizer_layer") or "rules_only"
        layer_name, optimizer = await _select_optimizer(layer, reload_models=True)

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
            return

        async with get_session() as session:
            plan_record = PlanRecord(
                horizon_start=plan["horizon_start"],
                horizon_end=plan["horizon_end"],
                plan_json=json.dumps(plan["actions"], default=str),
                optimizer_version=plan["version"],
                cost_estimate_eur=plan.get("cost_estimate"),
            )
            session.add(plan_record)
            await session.flush()

            for action in plan["actions"]:
                action_record = PlanActionRecord(
                    plan_id=plan_record.id,
                    scheduled_ts=dt.datetime.fromisoformat(action["ts"]),
                    action_type=action["type"],
                    payload_json=json.dumps(action.get("payload", {})),
                    status="pending",
                )
                session.add(action_record)

        logger.info(
            "plan_generated",
            plan_id=plan_record.id,
            actions=len(plan["actions"]),
            cost_eur=plan.get("cost_estimate"),
        )
    except Exception as e:
        logger.error("optimization_failed", error=str(e))


async def execute_pending_actions(wrapper: AquareaWrapper) -> None:
    """Execute any pending plan actions whose time has come."""
    executor = PlanExecutor(wrapper)
    await executor.execute_due_actions()
    await executor.expire_stale_actions()


async def main() -> None:
    """Main entry point for the optimizer service."""
    from packages.core.log_sink import configure_structlog_with_db
    configure_structlog_with_db("optimizer")

    logger.info("optimizer_starting")

    # Load ML model checkpoints so MILP can use them
    _load_ml_models()

    wrapper = AquareaWrapper()
    await wrapper.start()

    scheduler = AsyncIOScheduler()

    # Re-optimize every hour
    scheduler.add_job(
        run_optimization,
        "interval",
        hours=1,
        id="optimize",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=60),
    )

    # Check for actions to execute every minute
    scheduler.add_job(
        execute_pending_actions,
        "interval",
        seconds=60,
        args=[wrapper],
        id="executor",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=90),
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

"""Optimizer service: generates cost-optimal heating plans and executes them."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import PlanActionRecord, PlanRecord
from packages.core.services import AquareaWrapper
from packages.core.settings_service import get_setting
from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError
from packages.optimizer.rules import RulesOptimizer
from packages.optimizer.milp import MILPOptimizer
from packages.optimizer.executor import PlanExecutor
from packages.ml.models import COPModel, DemandModel

logger = structlog.get_logger()

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


async def _select_optimizer(layer: str) -> tuple[str, object]:
    """Return (layer_name, optimizer_instance) based on the configured layer setting."""
    if layer == "rules_only":
        return "rules", RulesOptimizer()

    # milp_preferred or auto: build MILP with ML models if available
    milp = MILPOptimizer(
        cop_model=_cop_model if _cop_model.is_trained else None,
        demand_model=_demand_model if _demand_model.is_trained else None,
    )

    if layer == "milp_preferred":
        return "milp", milp

    # auto: use MILP only when ML models are trained (better data = better MILP)
    if _cop_model.is_trained and _demand_model.is_trained:
        return "milp", milp

    return "rules", RulesOptimizer()


async def run_optimization() -> None:
    """Run the optimizer and store the plan, with layer selection and fallback."""
    try:
        layer = await get_setting("optimizer_layer") or "rules_only"
        layer_name, optimizer = await _select_optimizer(layer)

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


async def main() -> None:
    """Main entry point for the optimizer service."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
    )

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

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await wrapper.stop()
        logger.info("optimizer_stopped")


if __name__ == "__main__":
    asyncio.run(main())

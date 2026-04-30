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
from packages.optimizer.rules import RulesOptimizer
from packages.optimizer.executor import PlanExecutor

logger = structlog.get_logger()


async def run_optimization() -> None:
    """Run the optimizer and store the plan."""
    try:
        optimizer = RulesOptimizer()
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

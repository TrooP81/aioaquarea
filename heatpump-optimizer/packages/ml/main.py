"""ML training and serving entrypoint."""

from __future__ import annotations

import asyncio
import logging

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.core.config import settings
from packages.ml.models import COPModel, DemandModel

logger = structlog.get_logger()

# Global model instances
cop_model = COPModel()
demand_model = DemandModel()


async def retrain_models() -> None:
    """Retrain all ML models on latest data."""
    logger.info("ml_retrain_starting")

    cop_result = await cop_model.train()
    logger.info("cop_model_trained", **cop_result)

    demand_result = await demand_model.train()
    logger.info("demand_model_trained", **demand_result)


async def main() -> None:
    """ML service main loop — retrains weekly, serves predictions via the API."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
    )

    # Load latest models on startup
    cop_model.load_latest()
    demand_model.load_latest()
    logger.info("ml_models_loaded", cop=cop_model.is_trained, demand=demand_model.is_trained)

    scheduler = AsyncIOScheduler()

    # Retrain weekly
    scheduler.add_job(
        retrain_models,
        "cron",
        day_of_week="sun",
        hour=3,
        id="retrain",
    )

    scheduler.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

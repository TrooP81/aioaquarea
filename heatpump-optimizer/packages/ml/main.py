"""ML training and serving entrypoint."""

from __future__ import annotations

import asyncio

import structlog

from packages.core.scheduling import create_scheduler
from packages.ml.models import DemandModel

logger = structlog.get_logger()

demand_model = DemandModel()


async def retrain_models(*, automatic: bool = False, source: str = "ml_entrypoint") -> dict | None:
    """Request Demand maintenance; only the poller may fit it."""
    from packages.ml.lifecycle import enqueue_maintenance_request

    result = await enqueue_maintenance_request("demand", source, automatic=automatic)
    logger.info("demand_maintenance_requested", request=result)
    return result


async def main() -> None:
    """Queue startup and weekly Demand requests; never fit inside this service."""
    from packages.core.logging import configure_logging
    from packages.core.runtime_ownership import close_service_resources, finish_owned_cleanup

    scheduler = None
    try:
        configure_logging("ml")

        logger.info("cop_retired", reason="unverified_thermal_output", cop_source="assumed")
        demand_model.load_latest()
        logger.info("ml_models_loaded", demand=demand_model.is_trained)

        await retrain_models(automatic=True, source="ml_startup")

        scheduler = create_scheduler()

        scheduler.add_job(
            retrain_models,
            "cron",
            day_of_week="sun",
            hour=3,
            timezone="UTC",
            id="retrain",
            kwargs={"automatic": True, "source": "ml_schedule"},
        )

        scheduler.start()

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:

        async def cleanup():
            from packages.core.database import engine

            await close_service_resources(scheduler=scheduler, engine=engine)

        await finish_owned_cleanup(cleanup)


if __name__ == "__main__":
    asyncio.run(main())

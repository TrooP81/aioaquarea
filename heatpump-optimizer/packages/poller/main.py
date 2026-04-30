"""Poller service: periodically fetches device status, consumption, prices, and weather."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.core.services import AquareaWrapper
from packages.poller.feeds import fetch_prices, fetch_weather

logger = structlog.get_logger()


async def poll_device_status(wrapper: AquareaWrapper) -> None:
    """Poll device status and persist to DB."""
    try:
        device = await wrapper.refresh_device()
        status = device.status

        zones = status.zones if hasattr(status, "zones") else []
        zone1 = zones[0] if len(zones) > 0 else None
        zone2 = zones[1] if len(zones) > 1 else None

        record = DeviceStatusRecord(
            ts=dt.datetime.now(dt.timezone.utc),
            device_id=device.long_id if hasattr(device, "long_id") else "default",
            mode=str(status.operation_mode) if hasattr(status, "operation_mode") else None,
            operation_status=status.operation_status.value
            if hasattr(status, "operation_status")
            else None,
            outdoor_temp=status.outdoor_temperature
            if hasattr(status, "outdoor_temperature")
            else None,
            tank_temp=status.tank.temperature if hasattr(status, "tank") and status.tank else None,
            tank_target_temp=status.tank.heat_set
            if hasattr(status, "tank") and status.tank
            else None,
            tank_operation_status=status.tank.operation_status.value
            if hasattr(status, "tank") and status.tank
            else None,
            zone1_temp=zone1.temperature if zone1 else None,
            zone1_target_temp=zone1.heat_set if zone1 else None,
            zone2_temp=zone2.temperature if zone2 else None,
            zone2_target_temp=zone2.heat_set if zone2 else None,
            quiet_mode=status.quiet_mode.value if hasattr(status, "quiet_mode") else None,
            powerful_mode=status.powerful_time.value
            if hasattr(status, "powerful_time")
            else None,
            special_status=status.special_status.value
            if hasattr(status, "special_status")
            else None,
        )

        async with get_session() as session:
            session.add(record)

        logger.info("device_status_polled", device_id=record.device_id, outdoor_temp=record.outdoor_temp)

    except Exception as e:
        logger.error("device_poll_failed", error=str(e))


async def poll_consumption(wrapper: AquareaWrapper) -> None:
    """Poll consumption data and persist."""
    try:
        device = await wrapper.get_device()
        consumption = device.consumption

        if consumption:
            for entry in consumption:
                record = ConsumptionRecord(
                    ts=dt.datetime.now(dt.timezone.utc),
                    device_id=device.long_id if hasattr(device, "long_id") else "default",
                    heat_kwh=entry.heat_consumption,
                    cool_kwh=entry.cool_consumption,
                    tank_kwh=entry.tank_consumption,
                    outdoor_temp=entry.outdoor_temp,
                )
                async with get_session() as session:
                    session.add(record)

        logger.info("consumption_polled")
    except Exception as e:
        logger.error("consumption_poll_failed", error=str(e))


async def poll_prices() -> None:
    """Fetch and store electricity prices."""
    try:
        prices = await fetch_prices()
        area = settings.entsoe_area if settings.price_provider == "entsoe" else "tibber"
        async with get_session() as session:
            for ts, price in prices:
                record = PriceRecord(
                    ts=ts,
                    area=area,
                    price_eur_per_kwh=price,
                )
                session.add(record)
        logger.info("prices_fetched", count=len(prices), provider=settings.price_provider)
    except Exception as e:
        logger.error("price_fetch_failed", error=str(e))


async def poll_weather() -> None:
    """Fetch and store weather data."""
    try:
        weather_data = await fetch_weather()
        async with get_session() as session:
            for entry in weather_data:
                record = WeatherRecord(
                    ts=entry["ts"],
                    source="open-meteo",
                    temperature=entry["temperature"],
                    irradiance=entry.get("irradiance"),
                    wind_speed=entry.get("wind_speed"),
                    humidity=entry.get("humidity"),
                )
                session.add(record)
        logger.info("weather_fetched", count=len(weather_data))
    except Exception as e:
        logger.error("weather_fetch_failed", error=str(e))


async def main() -> None:
    """Main entry point for the poller service."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
    )

    logger.info("poller_starting", poll_interval=settings.poll_interval_seconds)

    wrapper = AquareaWrapper()
    await wrapper.start()

    scheduler = AsyncIOScheduler()

    # Device status every poll_interval (default 5 min)
    scheduler.add_job(
        poll_device_status,
        "interval",
        seconds=settings.poll_interval_seconds,
        args=[wrapper],
        id="device_status",
        next_run_time=dt.datetime.now(),
    )

    # Consumption every 15 min
    scheduler.add_job(
        poll_consumption,
        "interval",
        minutes=15,
        args=[wrapper],
        id="consumption",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=30),
    )

    # Prices every hour
    scheduler.add_job(
        poll_prices,
        "interval",
        hours=1,
        id="prices",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=10),
    )

    # Weather every 30 min
    scheduler.add_job(
        poll_weather,
        "interval",
        minutes=30,
        id="weather",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=15),
    )

    scheduler.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await wrapper.stop()
        logger.info("poller_stopped")


if __name__ == "__main__":
    asyncio.run(main())

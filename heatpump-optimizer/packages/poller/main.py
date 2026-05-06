"""Poller service: periodically fetches device status, consumption, prices, and weather."""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    FaultRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.core.services import AquareaWrapper
from packages.poller.feeds import fetch_prices, fetch_weather
from packages.poller.smartthings import poll_smartthings_temps
from packages.core.settings_service import get_setting
from packages.optimizer.shower_mode import ShowerDetector

logger = structlog.get_logger()

# Module-level shower detector instance (reused across polls)
_shower_detector = ShowerDetector()


async def poll_device_status(wrapper: AquareaWrapper) -> None:
    """Poll device status and persist to DB."""
    try:
        device = await wrapper.refresh_device()

        zones = device.zones
        zone1 = zones.get(1)
        zone2 = zones.get(2)

        # Compressor activity
        direction = device.current_direction.name  # IDLE/PUMP/WATER
        device_action = device.current_action.name  # OFF/IDLE/HEATING/COOLING/HEATING_WATER
        defrost_active = device.device_mode_status.name == "DEFROST"

        record = DeviceStatusRecord(
            ts=dt.datetime.now(dt.timezone.utc),
            device_id=device.long_id,
            mode=str(device.mode),
            operation_status=device.operation_status.value,
            outdoor_temp=device.temperature_outdoor,
            tank_temp=device.tank.temperature if device.tank else None,
            tank_target_temp=device.tank.target_temperature if device.tank else None,
            tank_operation_status=device.tank.operation_status.value if device.tank else None,
            zone1_temp=zone1.temperature if zone1 else None,
            zone1_target_temp=zone1.heat_target_temperature if zone1 else None,
            zone2_temp=zone2.temperature if zone2 else None,
            zone2_target_temp=zone2.heat_target_temperature if zone2 else None,
            quiet_mode=device.quiet_mode.value,
            powerful_mode=device.powerful_time.value,
            special_status=device.special_status.value if device.special_status else None,
            # New compressor/activity fields
            direction=direction,
            pump_duty=device.pump_duty,
            device_action=device_action,
            defrost_active=defrost_active,
            force_dhw=device.force_dhw.value,
            force_heater=device.force_heater.value,
            holiday_mode=device.holiday_timer.value,
            # Zone operation status
            zone1_operation_status=zone1.operation_status.value if zone1 else None,
            zone2_operation_status=zone2.operation_status.value if zone2 else None,
            # Tank limits
            tank_heat_max=device.tank.heat_max if device.tank else None,
            tank_heat_min=device.tank.heat_min if device.tank else None,
        )

        async with get_session() as session:
            session.add(record)

        logger.info(
            "device_status_polled",
            device_id=record.device_id,
            outdoor_temp=record.outdoor_temp,
            action=device_action,
            direction=direction,
        )

        # --- Shower mode detection ---
        try:
            await _shower_detector.check(record)
        except Exception as shower_err:
            logger.error("shower_detection_failed", error=str(shower_err))

        # --- Fault detection ---
        if device.is_on_error and device.current_error:
            await _record_fault(device)

    except Exception as e:
        logger.error("device_poll_failed", error=str(e))


async def _record_fault(device) -> None:
    """Record a new fault if not already open."""
    from sqlalchemy import select, and_

    fault = device.current_error
    async with get_session() as session:
        # Check if this fault is already recorded and unresolved
        existing = await session.execute(
            select(FaultRecord).where(
                and_(
                    FaultRecord.device_id == device.long_id,
                    FaultRecord.error_code == fault.error_code,
                    FaultRecord.resolved_at.is_(None),
                )
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                FaultRecord(
                    device_id=device.long_id,
                    error_code=fault.error_code,
                    error_message=fault.error_message,
                    outdoor_temp=device.temperature_outdoor,
                )
            )
            logger.warning(
                "fault_detected",
                device_id=device.long_id,
                error_code=fault.error_code,
                error_message=fault.error_message,
            )


async def poll_consumption(wrapper: AquareaWrapper) -> None:
    """Poll consumption data and persist."""
    from aioaquarea.statistics import ConsumptionType

    try:
        device = await wrapper.get_device()
        now = dt.datetime.now(dt.timezone.utc)

        heat = await device.get_and_refresh_consumption(now, ConsumptionType.HEAT) or 0
        cool = await device.get_and_refresh_consumption(now, ConsumptionType.COOL) or 0
        tank = await device.get_and_refresh_consumption(now, ConsumptionType.WATER_TANK) or 0

        record = ConsumptionRecord(
            ts=now,
            device_id=device.long_id,
            heat_kwh=heat,
            cool_kwh=cool,
            tank_kwh=tank,
            outdoor_temp=device.temperature_outdoor,
        )
        async with get_session() as session:
            session.add(record)

        logger.info("consumption_polled", heat=heat, cool=cool, tank=tank)
    except Exception as e:
        logger.error("consumption_poll_failed", error=str(e))


async def poll_prices() -> None:
    """Fetch and store electricity prices."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    try:
        prices = await fetch_prices()
        provider = await get_setting("price_provider")
        if provider == "entsoe":
            area = (await get_setting("entsoe_area")) or settings.entsoe_area
        elif provider == "manual":
            area = "manual"
        else:
            area = "tibber"
        async with get_session() as session:
            if prices:
                stmt = pg_insert(PriceRecord).values(
                    [{"ts": ts, "area": area, "price_eur_per_kwh": price} for ts, price in prices]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ts", "area"],
                    set_={"price_eur_per_kwh": stmt.excluded.price_eur_per_kwh},
                )
                await session.execute(stmt)
        logger.info("prices_fetched", count=len(prices), provider=settings.price_provider)
    except Exception as e:
        logger.error("price_fetch_failed", error=str(e))


async def poll_weather() -> None:
    """Fetch and store weather data."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    try:
        weather_data = await fetch_weather()
        async with get_session() as session:
            if weather_data:
                stmt = pg_insert(WeatherRecord).values(
                    [
                        {
                            "ts": entry["ts"],
                            "source": "open-meteo",
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                        }
                        for entry in weather_data
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ts", "source"],
                    set_={
                        "temperature": stmt.excluded.temperature,
                        "irradiance": stmt.excluded.irradiance,
                        "wind_speed": stmt.excluded.wind_speed,
                        "humidity": stmt.excluded.humidity,
                    },
                )
                await session.execute(stmt)
        logger.info("weather_fetched", count=len(weather_data))
    except Exception as e:
        logger.error("weather_fetch_failed", error=str(e))


async def poll_indoor_temp() -> None:
    """Fetch indoor air temperatures from SmartThings sensors."""
    try:
        enabled = await get_setting("smartthings_enabled")
        if enabled != "true":
            return

        async with get_session() as session:
            count = await poll_smartthings_temps(session)

        if count:
            logger.info("smartthings_polled", readings=count)
    except Exception as e:
        logger.error("smartthings_poll_failed", error=str(e))


async def retrain_comfort_model() -> None:
    """Periodically retrain the comfort model from accumulated SmartThings data."""
    try:
        enabled = await get_setting("use_comfort_model")
        if enabled != "true":
            return

        from packages.ml.comfort_model import comfort_model

        lag_str = await get_setting("thermal_lag_minutes")
        lag = int(lag_str) if lag_str else None

        result = await comfort_model.train(thermal_lag_minutes=lag)
        logger.info("comfort_model_retrain", **result)
    except Exception as e:
        logger.error("comfort_model_retrain_failed", error=str(e))


async def main() -> None:
    """Main entry point for the poller service."""
    from packages.core.log_sink import configure_structlog_with_db
    configure_structlog_with_db("poller")

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

    # SmartThings indoor temp (uses smartthings_poll_interval setting, default 300s)
    st_interval_str = await get_setting("smartthings_poll_interval")
    st_interval = int(st_interval_str) if st_interval_str else 300
    scheduler.add_job(
        poll_indoor_temp,
        "interval",
        seconds=st_interval,
        id="indoor_temp",
        next_run_time=dt.datetime.now() + dt.timedelta(seconds=20),
    )

    # Comfort model retraining every 6 hours
    scheduler.add_job(
        retrain_comfort_model,
        "interval",
        hours=6,
        id="comfort_model_retrain",
        next_run_time=dt.datetime.now() + dt.timedelta(minutes=5),
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
            # Windows doesn't support add_signal_handler
            pass

    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=True)
        await wrapper.stop()
        logger.info("poller_stopped")


if __name__ == "__main__":
    asyncio.run(main())

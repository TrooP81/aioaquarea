"""Poller service: periodically fetches device status, consumption, prices, and weather."""

from __future__ import annotations

import asyncio
import datetime as dt

import structlog

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    FaultRecord,
    OptimizationRequestRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.core.services import AquareaWrapper
from packages.core.scheduling import create_scheduler, utc_after, utc_now
from packages.poller.feeds import fetch_price_feed, fetch_weather
from packages.poller.smartthings import poll_smartthings_temps
from packages.core.settings_service import get_bool_setting, get_int_setting, get_string_setting
from packages.core.heating_evidence import classify_space_heating
from packages.core.outdoor_temperature import resolve_outdoor_temperature
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
        heating_evidence = classify_space_heating(
            operation_status=device.operation_status.value,
            direction=direction,
            device_action=device_action,
            defrost_active=defrost_active,
        )
        raw_outdoor_temp = device.temperature_outdoor
        async with get_session() as session:
            outdoor = await resolve_outdoor_temperature(
                session,
                heat_pump_c=raw_outdoor_temp,
            )

        record = DeviceStatusRecord(
            ts=dt.datetime.now(dt.timezone.utc),
            device_id=device.long_id,
            mode=str(device.mode),
            operation_status=device.operation_status.value,
            outdoor_temp=outdoor.effective_c,
            heat_pump_outdoor_temp=outdoor.heat_pump_c,
            outdoor_temp_source=outdoor.source,
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
            space_heating_active=heating_evidence.active,
            space_heating_evidence=heating_evidence.code,
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
            heat_pump_outdoor_temp=record.heat_pump_outdoor_temp,
            outdoor_temp_source=record.outdoor_temp_source,
            outdoor_compensation_c=outdoor.compensation_c,
            action=device_action,
            direction=direction,
            space_heating_active=heating_evidence.active,
            heating_evidence=heating_evidence.code,
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
        raw_outdoor_temp = device.temperature_outdoor
        async with get_session() as session:
            outdoor = await resolve_outdoor_temperature(
                session,
                heat_pump_c=raw_outdoor_temp,
                at=now,
            )

        heat = await device.get_and_refresh_consumption(now, ConsumptionType.HEAT) or 0
        cool = await device.get_and_refresh_consumption(now, ConsumptionType.COOL) or 0
        tank = await device.get_and_refresh_consumption(now, ConsumptionType.WATER_TANK) or 0

        record = ConsumptionRecord(
            ts=now,
            device_id=device.long_id,
            heat_kwh=heat,
            cool_kwh=cool,
            tank_kwh=tank,
            outdoor_temp=outdoor.effective_c,
            heat_pump_outdoor_temp=outdoor.heat_pump_c,
            outdoor_temp_source=outdoor.source,
        )
        async with get_session() as session:
            session.add(record)

        logger.info("consumption_polled", heat=heat, cool=cool, tank=tank)
    except Exception as e:
        logger.error("consumption_poll_failed", error=str(e))


async def poll_prices() -> None:
    """Fetch and store electricity prices."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from packages.core.planning_data_quality import DEFAULT_HORIZON_HOURS, get_planning_data_quality

    try:
        feed = await fetch_price_feed()
        prices = feed.prices
        before_quality = await get_planning_data_quality() if prices else None
        fetched_at = dt.datetime.now(dt.timezone.utc)
        provider = await get_string_setting("price_provider")
        if provider == "entsoe":
            area = (await get_string_setting("entsoe_area")) or settings.entsoe_area
        elif provider == "manual":
            area = "manual"
        else:
            area = "tibber"
        async with get_session() as session:
            if prices:
                stmt = pg_insert(PriceRecord).values(
                    [
                        {
                            "ts": ts,
                            "area": area,
                            "price_eur_per_kwh": price,
                            "price_currency": feed.currency,
                            "price_source": feed.source,
                            "fetched_at": fetched_at,
                        }
                        for ts, price in prices
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ts", "area"],
                    set_={
                        "price_eur_per_kwh": stmt.excluded.price_eur_per_kwh,
                        "price_currency": stmt.excluded.price_currency,
                        "price_source": stmt.excluded.price_source,
                        "fetched_at": stmt.excluded.fetched_at,
                    },
                )
                await session.execute(stmt)
        if before_quality is not None:
            after_quality = await get_planning_data_quality()
            await _queue_price_horizon_reoptimization(
                before_quality,
                after_quality,
                full_horizon_hours=DEFAULT_HORIZON_HOURS,
            )
        logger.info(
            "prices_fetched",
            count=len(prices),
            provider=settings.price_provider,
            currency=feed.currency,
        )
    except Exception as e:
        logger.error("price_fetch_failed", error=str(e))


async def _queue_price_horizon_reoptimization(
    before_quality: dict[str, object],
    after_quality: dict[str, object],
    *,
    full_horizon_hours: int,
) -> None:
    """Queue one plan refresh when newly published prices complete tomorrow.

    The poller never solves a plan itself. It only creates the same durable
    request consumed by the singleton optimizer, and only on the meaningful
    partial-to-full price-horizon transition.
    """

    before_price = before_quality.get("price", {})
    after_price = after_quality.get("price", {})
    before_hours = (
        int(before_price.get("contiguous_hours", 0)) if isinstance(before_price, dict) else 0
    )
    after_hours = (
        int(after_price.get("contiguous_hours", 0)) if isinstance(after_price, dict) else 0
    )
    if before_hours >= full_horizon_hours or after_hours < full_horizon_hours:
        return
    if not after_quality.get("control_allowed"):
        return

    from sqlalchemy import select

    async with get_session() as session:
        existing = (
            await session.execute(
                select(OptimizationRequestRecord.id)
                .where(
                    OptimizationRequestRecord.requested_by == "price_horizon",
                    OptimizationRequestRecord.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(OptimizationRequestRecord(requested_by="price_horizon"))
    logger.info(
        "price_horizon_reoptimization_queued",
        previous_hours=before_hours,
        available_hours=after_hours,
    )


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
                            "source": entry.get("source", "open-meteo"),
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                            "cloud_cover": entry.get("cloud_cover"),
                            "precipitation": entry.get("precipitation"),
                            "forecast_issued_at": entry.get("forecast_issued_at"),
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
                        "cloud_cover": stmt.excluded.cloud_cover,
                        "precipitation": stmt.excluded.precipitation,
                        "forecast_issued_at": stmt.excluded.forecast_issued_at,
                    },
                )
                await session.execute(stmt)
        logger.info(
            "weather_fetched",
            count=len(weather_data),
            source=weather_data[0].get("source") if weather_data else None,
        )
    except Exception as e:
        logger.error("weather_fetch_failed", error=str(e))


async def poll_indoor_temp() -> None:
    """Fetch indoor air temperatures from SmartThings sensors."""
    try:
        enabled = await get_bool_setting("smartthings_enabled")
        if not enabled:
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
        enabled = await get_bool_setting("use_comfort_model")
        if not enabled:
            return

        from packages.ml.comfort_model import comfort_model

        lag_str = await get_string_setting("thermal_lag_minutes")
        lag = int(lag_str) if lag_str else None

        result = await comfort_model.train(thermal_lag_minutes=lag)
        logger.info("comfort_model_retrain", **result)
    except Exception as e:
        logger.error("comfort_model_retrain_failed", error=str(e))


async def run_seasonal_calibration() -> None:
    """Advance an opted-in heating-season model-evidence campaign safely."""

    try:
        from packages.ml.seasonal_learning import run_seasonal_calibration_cycle

        result = await run_seasonal_calibration_cycle()
        logger.info("seasonal_calibration_cycle", **result)
    except Exception as exc:  # noqa: BLE001 - background diagnostics must not stop polling
        logger.error("seasonal_calibration_cycle_failed", error=str(exc))


async def deliver_operational_alerts() -> None:
    """Send changed alerts only when the user configured an HTTPS webhook."""

    try:
        from packages.core.operational_alerts import deliver_operational_alert_webhook

        result = await deliver_operational_alert_webhook()
        logger.info("operational_alert_delivery", webhook=result.get("webhook"))
    except Exception as exc:  # noqa: BLE001 - alerts must not stop the poller
        logger.error("operational_alert_delivery_failed", error=str(exc))


async def main() -> None:
    """Main entry point for the poller service."""
    from packages.core.logging import configure_logging
    from packages.core.service_health import record_service_heartbeat

    configure_logging("poller")

    logger.info("poller_starting", poll_interval=settings.poll_interval_seconds)

    wrapper = AquareaWrapper()
    await wrapper.start()

    scheduler = create_scheduler()
    await record_service_heartbeat("poller", poll_interval_seconds=settings.poll_interval_seconds)

    scheduler.add_job(
        record_service_heartbeat,
        "interval",
        minutes=1,
        args=["poller"],
        kwargs={"poll_interval_seconds": settings.poll_interval_seconds},
        id="heartbeat",
        next_run_time=utc_after(minutes=1),
    )

    # Device status every poll_interval (default 5 min)
    scheduler.add_job(
        poll_device_status,
        "interval",
        seconds=settings.poll_interval_seconds,
        args=[wrapper],
        id="device_status",
        next_run_time=utc_now(),
    )

    # Consumption every 15 min
    scheduler.add_job(
        poll_consumption,
        "interval",
        minutes=15,
        args=[wrapper],
        id="consumption",
        next_run_time=utc_after(seconds=30),
    )

    # Prices every 15 minutes. This makes a partial same-day plan refresh soon
    # after Tibber publishes tomorrow's day-ahead prices; the queue helper
    # below still deduplicates the resulting reoptimization request.
    scheduler.add_job(
        poll_prices,
        "interval",
        minutes=15,
        id="prices",
        next_run_time=utc_after(seconds=10),
    )

    # Weather every 30 min
    scheduler.add_job(
        poll_weather,
        "interval",
        minutes=30,
        id="weather",
        next_run_time=utc_after(seconds=15),
    )

    # SmartThings indoor temp (uses smartthings_poll_interval setting, default 300s)
    st_interval = await get_int_setting("smartthings_poll_interval")
    scheduler.add_job(
        poll_indoor_temp,
        "interval",
        seconds=st_interval,
        id="indoor_temp",
        next_run_time=utc_after(seconds=20),
    )

    # Comfort model retraining every 6 hours
    scheduler.add_job(
        retrain_comfort_model,
        "interval",
        hours=6,
        id="comfort_model_retrain",
        next_run_time=utc_after(minutes=5),
    )

    # Seasonal learning is opt-in and may only train after the data-quality
    # checks pass.  A six-hour cadence avoids repeated heavy calibration.
    scheduler.add_job(
        run_seasonal_calibration,
        "interval",
        hours=6,
        id="seasonal_calibration",
        next_run_time=utc_after(minutes=7),
    )

    scheduler.add_job(
        deliver_operational_alerts,
        "interval",
        minutes=5,
        id="operational_alert_delivery",
        next_run_time=utc_after(minutes=2),
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

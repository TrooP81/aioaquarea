"""Poller service: periodically fetches device status, consumption, prices, and weather."""

from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import structlog

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import (
    ConsumptionRecord,
    FaultRecord,
    OptimizationRequestRecord,
    PriceRecord,
    WeatherRecord,
)
from packages.core.services import (
    AquareaWrapper,
    PanasonicAdapterBackoffError,
    PanasonicAdapterUnavailableError,
)
from packages.core.scheduling import create_scheduler, utc_after, utc_now
from packages.poller.feeds import fetch_price_feed, fetch_weather
from packages.poller.smartthings import poll_smartthings_temps
from packages.core.settings_service import (
    get_bool_setting,
    get_int_setting,
    get_user_tz,
)
from packages.core.device_status_snapshot import build_device_status_record
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.device_status_ingestion import ingest_device_status
from packages.core.panasonic_diagnostics import (
    build_panasonic_adapter_state,
    classify_panasonic_adapter_reason,
)
from packages.core.service_health import record_service_heartbeat
from packages.core.external_heartbeat import ExternalHeartbeatRunner
from packages.optimizer.shower_mode import ShowerDetector

logger = structlog.get_logger()

# Module-level shower detector instance (reused across polls)
_shower_detector = ShowerDetector()


async def _record_panasonic_adapter_state(
    *,
    status: str,
    device_id: str | None,
    reason: str | None = None,
    consecutive_failures: int = 0,
    retry_after_seconds: int = 0,
) -> None:
    """Publish adaptor diagnostics without making status polling depend on them."""

    state = build_panasonic_adapter_state(
        status=status,
        device_id=device_id,
        reason=classify_panasonic_adapter_reason(reason),
        consecutive_failures=consecutive_failures,
        retry_after_seconds=retry_after_seconds,
    )
    try:
        await record_service_heartbeat(
            "poller",
            poll_interval_seconds=settings.poll_interval_seconds,
            panasonic_adapter=state,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not stop polling
        logger.warning("panasonic_adapter_diagnostics_failed", error=str(exc))


async def poll_device_status(wrapper: AquareaWrapper) -> None:
    """Poll device status and persist to DB."""
    try:
        device = await wrapper.refresh_device()

        direction = device.current_direction.name
        device_action = device.current_action.name
        if await get_bool_setting("ml_maintenance_enabled"):
            await wrapper.get_active_weekly_timer_slots()
        record = build_device_status_record(
            device, timer_provenance=wrapper.cached_native_timer_provenance()
        )
        raw_outdoor_temp = device.temperature_outdoor

        async with get_session() as session:
            outdoor = await resolve_outdoor_temperature(session, heat_pump_c=raw_outdoor_temp)
            record.outdoor_temp = outdoor.effective_c
            record.heat_pump_outdoor_temp = outdoor.heat_pump_c
            record.outdoor_temp_source = outdoor.source
            await ingest_device_status(session, record)

        logger.info(
            "device_status_polled",
            device_id=record.device_id,
            outdoor_temp=record.outdoor_temp,
            heat_pump_outdoor_temp=record.heat_pump_outdoor_temp,
            outdoor_temp_source=record.outdoor_temp_source,
            outdoor_compensation_c=outdoor.compensation_c,
            action=device_action,
            direction=direction,
            space_heating_active=record.space_heating_active,
            heating_evidence=record.space_heating_evidence,
        )

        # --- Shower mode detection ---
        try:
            await _shower_detector.check(record)
        except Exception as shower_err:
            logger.error("shower_detection_failed", error=str(shower_err))

        # --- Fault detection ---
        if device.is_on_error and device.current_error:
            await _record_fault(device)

        await _record_panasonic_adapter_state(
            status="available",
            device_id=record.device_id,
        )
    except PanasonicAdapterBackoffError as exc:
        await _record_panasonic_adapter_state(
            status="backoff",
            device_id=exc.device_id,
            reason=exc.reason,
            consecutive_failures=exc.consecutive_failures,
            retry_after_seconds=exc.retry_after_seconds,
        )
        logger.info(
            "panasonic_adapter_backoff",
            device_id=exc.device_id,
            consecutive_failures=exc.consecutive_failures,
            retry_after_seconds=exc.retry_after_seconds,
            reason=exc.reason,
        )
    except PanasonicAdapterUnavailableError as exc:
        await _record_panasonic_adapter_state(
            status="unavailable",
            device_id=exc.device_id,
            reason=exc.reason,
            consecutive_failures=exc.consecutive_failures,
            retry_after_seconds=exc.retry_after_seconds,
        )
        logger.warning(
            "panasonic_adapter_unavailable",
            device_id=exc.device_id,
            consecutive_failures=exc.consecutive_failures,
            retry_after_seconds=exc.retry_after_seconds,
            reason=exc.reason,
        )
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
    now = dt.datetime.now(dt.timezone.utc)
    timezone_name = await get_user_tz()
    local_now = now.astimezone(ZoneInfo(timezone_name))
    if settings.panasonic_distributed_read_quota_enabled:
        try:
            device = await wrapper.get_device()
            raw_outdoor_temp = device.temperature_outdoor
            async with get_session() as session:
                outdoor = await resolve_outdoor_temperature(
                    session,
                    heat_pump_c=raw_outdoor_temp,
                    at=now,
                )
            snapshot = await wrapper.refresh_consumption(local_now)
            record = ConsumptionRecord(
                ts=now,
                device_id=device.long_id,
                source_date=snapshot.date,
                source_timezone=timezone_name,
                heat_kwh=snapshot.heat_kwh,
                cool_kwh=snapshot.cool_kwh,
                tank_kwh=snapshot.tank_kwh,
                outdoor_temp=outdoor.effective_c,
                heat_pump_outdoor_temp=outdoor.heat_pump_c,
                outdoor_temp_source=outdoor.source,
            )
            async with get_session() as session:
                session.add(record)
            logger.info(
                "consumption_polled",
                heat=record.heat_kwh,
                cool=record.cool_kwh,
                tank=record.tank_kwh,
                poll_instant=now.isoformat(),
                timezone=timezone_name,
                source_date=snapshot.date.isoformat(),
            )
        except Exception as e:
            logger.error("consumption_poll_failed", error=str(e))
        return

    from aioaquarea.statistics import ConsumptionType

    try:
        device = await wrapper.get_device()
        raw_outdoor_temp = device.temperature_outdoor
        async with get_session() as session:
            outdoor = await resolve_outdoor_temperature(
                session,
                heat_pump_c=raw_outdoor_temp,
                at=now,
            )

        heat = await device.get_and_refresh_consumption(local_now, ConsumptionType.HEAT)
        cool = await device.get_and_refresh_consumption(local_now, ConsumptionType.COOL)
        tank = await device.get_and_refresh_consumption(local_now, ConsumptionType.WATER_TANK)

        record = ConsumptionRecord(
            ts=now,
            device_id=device.long_id,
            source_date=local_now.date(),
            source_timezone=timezone_name,
            heat_kwh=heat,
            cool_kwh=cool,
            tank_kwh=tank,
            outdoor_temp=outdoor.effective_c,
            heat_pump_outdoor_temp=outdoor.heat_pump_c,
            outdoor_temp_source=outdoor.source,
        )
        async with get_session() as session:
            session.add(record)

        logger.info(
            "consumption_polled",
            heat=heat,
            cool=cool,
            tank=tank,
            poll_instant=now.isoformat(),
            timezone=timezone_name,
            source_date=local_now.date().isoformat(),
        )
    except Exception as e:
        logger.error("consumption_poll_failed", error=str(e))


async def poll_prices() -> None:
    """Fetch and store electricity prices."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from packages.core.planning_data_quality import DEFAULT_HORIZON_HOURS, get_planning_data_quality

    try:
        feed = await fetch_price_feed()
        if feed.rejection_reason is not None:
            logger.error(
                "price_fetch_failed",
                error="provider_validation_failed",
                provider=feed.source,
                reason=feed.rejection_reason,
            )
            return
        prices = feed.prices
        if prices and (
            feed.context is None or not feed.context.area or feed.context.provider != feed.source
        ):
            raise ValueError("Price feed has no coherent fetch context")
        before_quality = await get_planning_data_quality() if prices else None
        async with get_session() as session:
            if prices:
                if feed.source == "tibber":
                    from packages.core.price_scope import ensure_tibber_scope

                    await ensure_tibber_scope(
                        session,
                        area=feed.context.area,
                        home_id=feed.context.tibber_home_id,
                        currency=feed.currency,
                    )
                stmt = pg_insert(PriceRecord).values(
                    [
                        {
                            "ts": ts,
                            "area": feed.context.area,
                            "price_eur_per_kwh": price,
                            "price_currency": feed.currency,
                            "price_source": feed.source,
                            "fetched_at": feed.context.started_at,
                            "interval_schema": feed.interval_schema,
                        }
                        for ts, price in prices
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ts", "area"],
                    set_={
                        "price_eur_per_kwh": stmt.excluded.price_eur_per_kwh,
                        "price_currency": stmt.excluded.price_currency,
                        "interval_schema": stmt.excluded.interval_schema,
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
            provider=feed.source,
            currency=feed.currency,
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "price_fetch_failed", error="http_status_error", status_code=e.response.status_code
        )
    except Exception as e:
        logger.error("price_fetch_failed", error=type(e).__name__)


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
                            "source": entry["source"],
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                            "cloud_cover": entry.get("cloud_cover"),
                            "precipitation": entry.get("precipitation"),
                            "forecast_issued_at": entry.get("forecast_issued_at"),
                            "units_schema": entry.get("units_schema"),
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
                        "units_schema": stmt.excluded.units_schema,
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
    """Compatibility entrypoint: queue automatic comfort maintenance only."""
    try:
        from packages.ml.lifecycle import enqueue_maintenance_request

        result = await enqueue_maintenance_request("comfort", "poller_request", automatic=True)
        logger.info("comfort_maintenance_requested", request=result)
    except Exception as e:
        logger.error("comfort_maintenance_request_failed", error_type=type(e).__name__)


async def run_seasonal_calibration() -> None:
    """Advance an opted-in heating-season model-evidence campaign safely."""

    try:
        from packages.ml.seasonal_learning import run_seasonal_calibration_cycle

        result = await run_seasonal_calibration_cycle()
        logger.info("seasonal_calibration_cycle", **result)
    except Exception as exc:  # noqa: BLE001 - background diagnostics must not stop polling
        logger.error("seasonal_calibration_cycle_failed", error=str(exc))


async def run_ml_maintenance(owner) -> dict:
    if not await get_bool_setting("ml_maintenance_enabled"):
        return {"status": "disabled", "reason": "maintenance_disabled"}
    await run_seasonal_calibration()
    result = await owner.run()
    logger.info("ml_maintenance_cycle", **result)
    return result


async def deliver_operational_alerts() -> None:
    """Send changed alerts only when the user configured an HTTPS webhook."""

    try:
        from packages.core.operational_alerts import deliver_operational_alert_webhook

        result = await deliver_operational_alert_webhook()
        logger.info("operational_alert_delivery", webhook=result.get("webhook"))
    except Exception as exc:  # noqa: BLE001 - alerts must not stop the poller
        logger.error("operational_alert_delivery_failed", error=str(exc))


async def _run_service(wrapper, maintenance, scheduler) -> None:
    """Main entry point for the poller service."""
    from packages.core.logging import configure_logging
    from packages.ml.lifecycle import enqueue_maintenance_request

    configure_logging("poller")

    logger.info("poller_starting", poll_interval=settings.poll_interval_seconds)

    await wrapper.start()

    startup_slot = int(utc_now().timestamp() // (6 * 3600))
    for component in ("demand", "comfort", "thermal"):
        await enqueue_maintenance_request(
            component,
            "poller_startup",
            automatic=True,
            dedup_key=f"startup:{startup_slot}",
        )

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

    scheduler.add_job(
        run_ml_maintenance,
        "cron",
        hour="0,6,12,18",
        minute=0,
        args=[maintenance],
        timezone="UTC",
        coalesce=True,
        max_instances=1,
        id="ml_maintenance",
        next_run_time=utc_after(minutes=5),
    )

    scheduler.add_job(
        ExternalHeartbeatRunner().tick,
        "interval",
        seconds=30,
        id="external_heartbeat",
        coalesce=True,
        max_instances=1,
        next_run_time=utc_after(seconds=30),
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


async def main() -> None:
    from packages.core.runtime_ownership import close_service_resources, finish_owned_cleanup
    from packages.ml.lifecycle import MaintenanceRunner

    wrapper, maintenance, scheduler = None, None, None
    try:
        wrapper = AquareaWrapper(read_only=True)
        maintenance = MaintenanceRunner(timer_reader=wrapper)
        scheduler = create_scheduler()
        await _run_service(wrapper, maintenance, scheduler)
    finally:

        async def cleanup():
            from packages.core.database import engine

            await close_service_resources(
                scheduler=scheduler, maintenance=maintenance, wrapper=wrapper, engine=engine
            )

        await finish_owned_cleanup(cleanup)


if __name__ == "__main__":
    asyncio.run(main())

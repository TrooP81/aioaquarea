from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request

from packages.api._helpers import get_price_area
from packages.core.config import settings
from packages.core.database import get_session
from packages.core.resilience import DistributedReadQuotaExhausted
from packages.core.services import AquareaWrapper
from packages.core.models import ConsumptionRecord, PriceRecord, WeatherRecord
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.device_status_snapshot import build_device_status_record
from packages.core.device_status_ingestion import ingest_device_status

router = APIRouter()


async def get_polling_wrapper(request: Request) -> AquareaWrapper | None:
    if not settings.panasonic_distributed_read_quota_enabled:
        return None
    wrapper = getattr(request.app.state, "aquarea_wrapper", None)
    if wrapper is None:
        raise HTTPException(
            status_code=503,
            headers={"Retry-After": "30"},
            detail={"code": "panasonic_read_quota_unavailable", "retry_after_seconds": 30},
        )
    return wrapper


async def _poll_prices_and_weather_legacy(results: dict[str, object]) -> None:
    """Populate poll-now feed results with the legacy persistence semantics."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from packages.poller.feeds import fetch_price_feed, fetch_weather

    try:
        feed = await fetch_price_feed()
        prices = feed.prices
        if prices:
            area = await get_price_area()
            fetched_at = dt.datetime.now(dt.timezone.utc)
            async with get_session() as db:
                for ts, price in prices:
                    stmt = pg_insert(PriceRecord).values(
                        ts=ts,
                        area=area,
                        price_eur_per_kwh=price,
                        price_currency=feed.currency,
                        price_source=feed.source,
                        fetched_at=fetched_at,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "area"],
                        set_={
                            "price_eur_per_kwh": price,
                            "price_currency": feed.currency,
                            "price_source": feed.source,
                            "fetched_at": fetched_at,
                        },
                    )
                    await db.execute(stmt)
            results["prices"] = {"success": True, "message": f"Fetched {len(prices)} price points"}
        else:
            results["prices"] = {"success": False, "message": "No price data returned"}
    except Exception as exc:
        results["prices"] = {"success": False, "message": str(exc)}

    try:
        weather_data = await fetch_weather()
        if weather_data:
            async with get_session() as db:
                for entry in weather_data:
                    stmt = pg_insert(WeatherRecord).values(
                        ts=entry["ts"],
                        source=entry.get("source", "open-meteo"),
                        temperature=entry["temperature"],
                        irradiance=entry.get("irradiance"),
                        wind_speed=entry.get("wind_speed"),
                        humidity=entry.get("humidity"),
                        cloud_cover=entry.get("cloud_cover"),
                        precipitation=entry.get("precipitation"),
                        forecast_issued_at=entry.get("forecast_issued_at"),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "source"],
                        set_={
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                            "cloud_cover": entry.get("cloud_cover"),
                            "precipitation": entry.get("precipitation"),
                            "forecast_issued_at": entry.get("forecast_issued_at"),
                        },
                    )
                    await db.execute(stmt)
            results["weather"] = {
                "success": True,
                "message": f"Fetched {len(weather_data)} weather entries",
            }
        else:
            results["weather"] = {"success": False, "message": "No weather data returned"}
    except Exception as exc:
        results["weather"] = {"success": False, "message": str(exc)}


async def _poll_now_with_wrapper(wrapper: AquareaWrapper):
    results = {"device": None, "prices": None, "weather": None}
    now = dt.datetime.now(dt.timezone.utc)
    try:
        device, snapshot = await wrapper.refresh_status_and_consumption(now)
    except DistributedReadQuotaExhausted as exc:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(exc.status.retry_after_seconds)},
            detail={
                "code": "panasonic_read_quota_exhausted",
                "retry_after_seconds": exc.status.retry_after_seconds,
            },
        ) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            headers={"Retry-After": "30"},
            detail={"code": "panasonic_read_quota_unavailable", "retry_after_seconds": 30},
        ) from exc

    try:
        device_action = device.current_action.name
        record = build_device_status_record(device)
        async with get_session() as db:
            outdoor = await resolve_outdoor_temperature(db, heat_pump_c=device.temperature_outdoor)
            record.outdoor_temp = outdoor.effective_c
            record.heat_pump_outdoor_temp = outdoor.heat_pump_c
            record.outdoor_temp_source = outdoor.source
            await ingest_device_status(db, record)
        if snapshot is not None:
            async with get_session() as db:
                db.add(
                    ConsumptionRecord(
                        ts=now,
                        device_id=device.long_id,
                        heat_kwh=snapshot.heat_kwh or 0,
                        cool_kwh=snapshot.cool_kwh or 0,
                        tank_kwh=snapshot.tank_kwh or 0,
                        outdoor_temp=outdoor.effective_c,
                        heat_pump_outdoor_temp=outdoor.heat_pump_c,
                        outdoor_temp_source=outdoor.source,
                    )
                )
        message = (
            f"Device polled: outdoor={record.outdoor_temp}degC "
            f"({record.outdoor_temp_source}; pump={record.heat_pump_outdoor_temp}degC), "
            f"tank={record.tank_temp}degC, action={device_action}"
        )
        if snapshot is None:
            message += " (consumption not yet available: Panasonic consumption data is unavailable)"
        else:
            total = (snapshot.heat_kwh or 0) + (snapshot.cool_kwh or 0) + (snapshot.tank_kwh or 0)
            message += f", consumption={total:.1f} kWh"
        results["device"] = {"success": True, "message": message}
    except Exception as exc:
        results["device"] = {"success": False, "message": str(exc)}

    await _poll_prices_and_weather_legacy(results)
    all_success = all(result and result["success"] for result in results.values() if result)
    return {"status": "ok" if all_success else "partial", "results": results}


@router.post("/api/poll-now")
async def poll_now(wrapper: AquareaWrapper | None = Depends(get_polling_wrapper)):
    if wrapper is not None:
        return await _poll_now_with_wrapper(wrapper)

    import aiohttp
    from aioaquarea import AquareaEnvironment, Client
    from packages.core.settings_service import get_setting
    from packages.poller.feeds import fetch_price_feed, fetch_weather

    results = {"device": None, "prices": None, "weather": None}

    username = await get_setting("aquarea_username")
    password = await get_setting("aquarea_password")

    if username and password:
        try:
            async with aiohttp.ClientSession() as session:
                client = Client(
                    session=session,
                    username=username,
                    password=password,
                    device_direct=True,
                    refresh_login=False,
                    environment=AquareaEnvironment.PRODUCTION,
                )
                await client.login()
                devices = await client.get_devices()
                if devices:
                    from datetime import timedelta

                    device = await client.get_device(
                        device_info=devices[0],
                        consumption_refresh_interval=timedelta(minutes=5),
                    )
                    await device.refresh_data()

                    device_action = device.current_action.name
                    raw_outdoor_temp = device.temperature_outdoor
                    record = build_device_status_record(device)

                    async with get_session() as db:
                        outdoor = await resolve_outdoor_temperature(
                            db, heat_pump_c=raw_outdoor_temp
                        )
                        record.outdoor_temp = outdoor.effective_c
                        record.heat_pump_outdoor_temp = outdoor.heat_pump_c
                        record.outdoor_temp_source = outdoor.source
                        await ingest_device_status(db, record)

                    from aioaquarea.statistics import ConsumptionType

                    now = dt.datetime.now(dt.timezone.utc)
                    try:
                        heat = (
                            await device.get_and_refresh_consumption(now, ConsumptionType.HEAT) or 0
                        )
                        cool = (
                            await device.get_and_refresh_consumption(now, ConsumptionType.COOL) or 0
                        )
                        tank = (
                            await device.get_and_refresh_consumption(
                                now, ConsumptionType.WATER_TANK
                            )
                            or 0
                        )

                        cons_record = ConsumptionRecord(
                            ts=now,
                            device_id=device.long_id,
                            heat_kwh=heat,
                            cool_kwh=cool,
                            tank_kwh=tank,
                            outdoor_temp=outdoor.effective_c,
                            heat_pump_outdoor_temp=outdoor.heat_pump_c,
                            outdoor_temp_source=outdoor.source,
                        )
                        async with get_session() as db:
                            db.add(cons_record)

                        total = heat + cool + tank
                        results["device"] = {
                            "success": True,
                            "message": (
                                f"Device polled: outdoor={record.outdoor_temp}degC "
                                f"({record.outdoor_temp_source}; pump={record.heat_pump_outdoor_temp}degC), "
                                f"tank={record.tank_temp}degC, action={device_action}, "
                                f"consumption={total:.1f} kWh"
                            ),
                        }
                    except Exception as ce:
                        results["device"] = {
                            "success": True,
                            "message": (
                                f"Device polled: outdoor={record.outdoor_temp}degC "
                                f"({record.outdoor_temp_source}; pump={record.heat_pump_outdoor_temp}degC), "
                                f"tank={record.tank_temp}degC, action={device_action} "
                                f"(consumption not yet available: {ce})"
                            ),
                        }
                else:
                    results["device"] = {"success": False, "message": "No devices found"}
        except Exception as e:
            results["device"] = {"success": False, "message": str(e)}
    else:
        results["device"] = {"success": False, "message": "Credentials not configured"}

    try:
        feed = await fetch_price_feed()
        prices = feed.prices
        if prices:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            area = await get_price_area()
            fetched_at = dt.datetime.now(dt.timezone.utc)
            async with get_session() as db:
                for ts, price in prices:
                    stmt = pg_insert(PriceRecord).values(
                        ts=ts,
                        area=area,
                        price_eur_per_kwh=price,
                        price_currency=feed.currency,
                        price_source=feed.source,
                        fetched_at=fetched_at,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "area"],
                        set_={
                            "price_eur_per_kwh": price,
                            "price_currency": feed.currency,
                            "price_source": feed.source,
                            "fetched_at": fetched_at,
                        },
                    )
                    await db.execute(stmt)
            results["prices"] = {"success": True, "message": f"Fetched {len(prices)} price points"}
        else:
            results["prices"] = {"success": False, "message": "No price data returned"}
    except Exception as e:
        results["prices"] = {"success": False, "message": str(e)}

    try:
        weather_data = await fetch_weather()
        if weather_data:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            async with get_session() as db:
                for entry in weather_data:
                    stmt = pg_insert(WeatherRecord).values(
                        ts=entry["ts"],
                        source=entry.get("source", "open-meteo"),
                        temperature=entry["temperature"],
                        irradiance=entry.get("irradiance"),
                        wind_speed=entry.get("wind_speed"),
                        humidity=entry.get("humidity"),
                        cloud_cover=entry.get("cloud_cover"),
                        precipitation=entry.get("precipitation"),
                        forecast_issued_at=entry.get("forecast_issued_at"),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "source"],
                        set_={
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                            "cloud_cover": entry.get("cloud_cover"),
                            "precipitation": entry.get("precipitation"),
                            "forecast_issued_at": entry.get("forecast_issued_at"),
                        },
                    )
                    await db.execute(stmt)
            results["weather"] = {
                "success": True,
                "message": f"Fetched {len(weather_data)} weather entries",
            }
        else:
            results["weather"] = {"success": False, "message": "No weather data returned"}
    except Exception as e:
        results["weather"] = {"success": False, "message": str(e)}

    all_success = all(r and r["success"] for r in results.values() if r)
    return {"status": "ok" if all_success else "partial", "results": results}

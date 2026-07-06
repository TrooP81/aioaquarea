from __future__ import annotations

import datetime as dt

from fastapi import APIRouter

from packages.api._helpers import get_price_area
from packages.core.database import get_session
from packages.core.models import ConsumptionRecord, DeviceStatusRecord, PriceRecord, WeatherRecord

router = APIRouter()


@router.post("/api/poll-now")
async def poll_now():
    import aiohttp
    from aioaquarea import AquareaEnvironment, Client
    from packages.core.settings_service import get_setting
    from packages.poller.feeds import fetch_prices, fetch_weather

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

                    zones = device.zones
                    zone1 = zones.get(1)
                    zone2 = zones.get(2)
                    direction = device.current_direction.name
                    device_action = device.current_action.name
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
                        direction=direction,
                        pump_duty=device.pump_duty,
                        device_action=device_action,
                        defrost_active=defrost_active,
                        force_dhw=device.force_dhw.value,
                        force_heater=device.force_heater.value,
                        holiday_mode=device.holiday_timer.value,
                        zone1_operation_status=zone1.operation_status.value if zone1 else None,
                        zone2_operation_status=zone2.operation_status.value if zone2 else None,
                        tank_heat_max=device.tank.heat_max if device.tank else None,
                        tank_heat_min=device.tank.heat_min if device.tank else None,
                    )

                    async with get_session() as db:
                        db.add(record)

                    from aioaquarea.statistics import ConsumptionType

                    now = dt.datetime.now(dt.timezone.utc)
                    try:
                        heat = await device.get_and_refresh_consumption(now, ConsumptionType.HEAT) or 0
                        cool = await device.get_and_refresh_consumption(now, ConsumptionType.COOL) or 0
                        tank = await device.get_and_refresh_consumption(now, ConsumptionType.WATER_TANK) or 0

                        cons_record = ConsumptionRecord(
                            ts=now,
                            device_id=device.long_id,
                            heat_kwh=heat,
                            cool_kwh=cool,
                            tank_kwh=tank,
                            outdoor_temp=device.temperature_outdoor,
                        )
                        async with get_session() as db:
                            db.add(cons_record)

                        total = heat + cool + tank
                        results["device"] = {
                            "success": True,
                            "message": f"Device polled: outdoor={record.outdoor_temp}degC, tank={record.tank_temp}degC, action={device_action}, consumption={total:.1f} kWh",
                        }
                    except Exception as ce:
                        results["device"] = {
                            "success": True,
                            "message": f"Device polled: outdoor={record.outdoor_temp}degC, tank={record.tank_temp}degC, action={device_action} (consumption not yet available: {ce})",
                        }
                else:
                    results["device"] = {"success": False, "message": "No devices found"}
        except Exception as e:
            results["device"] = {"success": False, "message": str(e)}
    else:
        results["device"] = {"success": False, "message": "Credentials not configured"}

    try:
        prices = await fetch_prices()
        if prices:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            area = await get_price_area()
            async with get_session() as db:
                for ts, price in prices:
                    stmt = pg_insert(PriceRecord).values(ts=ts, area=area, price_eur_per_kwh=price)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "area"],
                        set_={"price_eur_per_kwh": price},
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
                        source="open-meteo",
                        temperature=entry["temperature"],
                        irradiance=entry.get("irradiance"),
                        wind_speed=entry.get("wind_speed"),
                        humidity=entry.get("humidity"),
                        cloud_cover=entry.get("cloud_cover"),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ts", "source"],
                        set_={
                            "temperature": entry["temperature"],
                            "irradiance": entry.get("irradiance"),
                            "wind_speed": entry.get("wind_speed"),
                            "humidity": entry.get("humidity"),
                            "cloud_cover": entry.get("cloud_cover"),
                        },
                    )
                    await db.execute(stmt)
            results["weather"] = {"success": True, "message": f"Fetched {len(weather_data)} weather entries"}
        else:
            results["weather"] = {"success": False, "message": "No weather data returned"}
    except Exception as e:
        results["weather"] = {"success": False, "message": str(e)}

    all_success = all(r and r["success"] for r in results.values() if r)
    return {"status": "ok" if all_success else "partial", "results": results}

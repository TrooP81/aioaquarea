from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query
from sqlalchemy import and_, desc, select

from packages.api._helpers import get_price_area
from packages.api.schemas import PriceResponse, WeatherResponse
from packages.core.database import get_session
from packages.core.models import COPRecord, DeviceStatusRecord, FaultRecord, PriceRecord, WeatherRecord

router = APIRouter()


@router.get("/api/prices", response_model=list[PriceResponse])
async def get_prices(hours: int = Query(48, ge=1, le=168)):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours // 2)
    until = now + dt.timedelta(hours=hours // 2)
    area = await get_price_area()

    async with get_session() as session:
        result = await session.execute(
            select(PriceRecord)
            .where(
                and_(
                    PriceRecord.ts >= since,
                    PriceRecord.ts <= until,
                    PriceRecord.area == area,
                )
            )
            .order_by(PriceRecord.ts)
        )
        rows = result.scalars().all()

    return [PriceResponse(ts=r.ts, price_eur_per_kwh=r.price_eur_per_kwh) for r in rows]


@router.get("/api/weather", response_model=list[WeatherResponse])
async def get_weather(hours: int = Query(48, ge=1, le=168)):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=12)
    until = now + dt.timedelta(hours=hours)

    async with get_session() as session:
        result = await session.execute(
            select(WeatherRecord)
            .where(and_(WeatherRecord.ts >= since, WeatherRecord.ts <= until))
            .order_by(WeatherRecord.ts)
        )
        rows = result.scalars().all()

    return [
        WeatherResponse(
            ts=r.ts,
            temperature=r.temperature,
            wind_speed=r.wind_speed,
            humidity=r.humidity,
        )
        for r in rows
    ]


@router.get("/api/faults")
async def get_faults(hours: int = Query(168, ge=1, le=8760), active_only: bool = Query(False)):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        query = select(FaultRecord).where(FaultRecord.ts >= since)
        if active_only:
            query = query.where(FaultRecord.resolved_at.is_(None))
        result = await session.execute(query.order_by(desc(FaultRecord.ts)))
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "device_id": r.device_id,
            "error_code": r.error_code,
            "error_message": r.error_message,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "outdoor_temp": r.outdoor_temp,
            "active": r.resolved_at is None,
        }
        for r in rows
    ]


@router.post("/api/faults/{fault_id}/resolve")
async def resolve_fault(fault_id: int):
    from sqlalchemy import update

    async with get_session() as session:
        await session.execute(
            update(FaultRecord)
            .where(FaultRecord.id == fault_id)
            .values(resolved_at=dt.datetime.now(dt.timezone.utc))
        )
    return {"status": "resolved"}


@router.get("/api/cop/history")
async def get_cop_history(hours: int = Query(168, ge=1, le=8760)):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(COPRecord).where(COPRecord.ts >= since).order_by(COPRecord.ts)
        )
        rows = result.scalars().all()

    return [
        {
            "ts": r.ts.isoformat(),
            "cop_value": r.cop_value,
            "mode": r.mode,
            "outdoor_temp": r.outdoor_temp,
            "electrical_kwh": r.electrical_kwh,
            "thermal_kwh": r.thermal_kwh,
        }
        for r in rows
    ]


@router.get("/api/cop/stats")
async def get_cop_stats(hours: int = Query(168, ge=1, le=8760), mode: str | None = Query(None)):
    from packages.ml.models import direction_cop

    return await direction_cop.get_average_cop(hours=hours, mode=mode)


@router.post("/api/cop/compute")
async def compute_cop(hours: int = Query(24, ge=1, le=168)):
    from packages.ml.models import direction_cop

    intervals = await direction_cop.compute_cop_intervals(hours=hours)
    return {"status": "computed", "intervals_found": len(intervals), "intervals": intervals[:20]}


@router.get("/api/compressor/activity")
async def get_compressor_activity(hours: int = Query(24, ge=1, le=168)):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(
                DeviceStatusRecord.ts,
                DeviceStatusRecord.direction,
                DeviceStatusRecord.device_action,
                DeviceStatusRecord.defrost_active,
                DeviceStatusRecord.force_dhw,
                DeviceStatusRecord.force_heater,
                DeviceStatusRecord.outdoor_temp,
            )
            .where(DeviceStatusRecord.ts >= since)
            .order_by(DeviceStatusRecord.ts)
        )
        rows = result.all()

    return [
        {
            "ts": r.ts.isoformat(),
            "direction": r.direction,
            "action": r.device_action,
            "defrost": r.defrost_active,
            "force_dhw": r.force_dhw,
            "force_heater": r.force_heater,
            "outdoor_temp": r.outdoor_temp,
        }
        for r in rows
    ]

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, delete, desc, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from packages.api._helpers import get_price_area
from packages.api.schemas import (
    ManualCOPListResponse,
    ManualCOPObservationResponse,
    ManualCOPPut,
    ManualCOPRevision,
    PriceResponse,
    WeatherResponse,
)
from packages.core.cop_monitoring import (
    InvalidStoredPeriod,
    ManualCOPObservation,
    forecast_snapshot,
    monitoring_result,
    snapshot_from_stored,
    validate_observation_period,
)
from packages.core.database import get_session
from packages.core.models import (
    COPRecord,
    DeviceStatusRecord,
    FaultRecord,
    ManualCOPObservationRecord,
    PriceRecord,
    WeatherRecord,
    manual_cop_revision_seq,
)
from packages.core.settings_service import get_bool_setting, get_setting, get_user_tz

router = APIRouter()
logger = structlog.get_logger(__name__)


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

    return [
        PriceResponse(
            ts=r.ts,
            price_eur_per_kwh=r.price_eur_per_kwh,
            price_currency=r.price_currency,
            price_source=r.price_source,
            fetched_at=r.fetched_at,
        )
        for r in rows
    ]


@router.get("/api/weather", response_model=list[WeatherResponse])
async def get_weather(hours: int = Query(48, ge=1, le=168)):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=12)
    until = now + dt.timedelta(hours=hours)

    provider = (await get_setting("weather_provider")) or "open-meteo"
    async with get_session() as session:
        result = await session.execute(
            select(WeatherRecord)
            .where(
                and_(
                    WeatherRecord.ts >= since,
                    WeatherRecord.ts <= until,
                    WeatherRecord.source == provider,
                )
            )
            .order_by(WeatherRecord.ts)
        )
        rows = result.scalars().all()

    return [
        WeatherResponse(
            ts=r.ts,
            temperature=r.temperature,
            wind_speed=r.wind_speed,
            humidity=r.humidity,
            cloud_cover=r.cloud_cover,
            irradiance=r.irradiance,
            precipitation=r.precipitation,
            source=r.source,
            forecast_issued_at=r.forecast_issued_at,
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


def _finite_manual_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Manual COP JSON numbers must be finite")
    return number


async def _validate_manual_json(request: Request) -> None:
    try:
        json.loads(
            await request.body(),
            parse_float=_finite_manual_json_number,
            parse_constant=_finite_manual_json_number,
        )
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Invalid or nonfinite manual COP JSON"
        ) from None


def _manual_snapshot(row: ManualCOPObservationRecord) -> ManualCOPObservation:
    return snapshot_from_stored(row)


_INVALID_STORED_DETAIL = "Manual COP stored period is invalid"


async def _known_manual_device(session, device_id: str) -> None:
    if not device_id or len(device_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid device key")
    result = await session.execute(
        select(
            or_(
                exists().where(DeviceStatusRecord.device_id == device_id),
                exists().where(ManualCOPObservationRecord.device_id == device_id),
            )
        )
    )
    if not result.scalar():
        raise HTTPException(status_code=404, detail="Unknown local device")


@router.get("/api/cop/manual", response_model=ManualCOPListResponse)
async def get_manual_cop(
    response: Response,
    device_id: str | None = Query(None, min_length=1, max_length=128),
    granularity: Literal["day", "month"] = Query("day"),
    before: dt.date | None = Query(None),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        enabled = await get_bool_setting("manual_cop_monitoring_enabled")
        default_timezone = await get_user_tz()
        candidates = ()
        invalid_snapshot = False
        observations = []
        next_before = None
        async with get_session() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            issue_at = dt.datetime.now(dt.timezone.utc)
            result = await session.execute(
                select(DeviceStatusRecord.device_id)
                .union(select(ManualCOPObservationRecord.device_id))
                .order_by("device_id")
            )
            devices = list(result.scalars().all())
            if device_id is not None:
                if device_id not in devices:
                    raise HTTPException(status_code=404, detail="Unknown local device")
                model = ManualCOPObservationRecord
                page = select(model).where(
                    model.device_id == device_id, model.granularity == granularity
                )
                if before is not None:
                    page = page.where(model.period_date < before)
                result = await session.execute(page.order_by(model.period_date.desc()).limit(91))
                rows = result.scalars().all()
                try:
                    observations = [
                        ManualCOPObservationResponse(**asdict(_manual_snapshot(row)))
                        for row in rows[:90]
                    ]
                except InvalidStoredPeriod:
                    logger.warning("manual_cop_invalid_stored_period", outcome="page_refused")
                    raise HTTPException(status_code=503, detail=_INVALID_STORED_DETAIL) from None
                if len(rows) > 90:
                    next_before = rows[89].period_date
                available_at = func.greatest(model.observed_at, model.created_at, model.updated_at)
                daily = select(model).where(
                    model.device_id == device_id, model.granularity == "day"
                )
                result = await session.execute(
                    daily.where(available_at <= issue_at)
                    .order_by(model.period_date.desc())
                    .limit(1)
                )
                latest = result.scalar_one_or_none()
                if latest is None:
                    result = await session.execute(
                        daily.order_by(model.period_date.desc()).limit(1)
                    )
                    latest = result.scalar_one_or_none()
                if latest is not None:
                    try:
                        today = issue_at.astimezone(ZoneInfo(latest.timezone)).date()
                    except (ValueError, ZoneInfoNotFoundError):
                        today = None
                    snapshot_rows = [latest]
                    if today is not None:
                        result = await session.execute(
                            daily.where(
                                model.period_date >= today - dt.timedelta(days=28),
                                model.period_date < today,
                            ).order_by(model.period_date)
                        )
                        snapshot_rows += list(result.scalars().all())
                        # Closed attestation (observed at or after the derived period
                        # end) is re-validated for every candidate by the application.
                        result = await session.execute(
                            daily.where(
                                model.completeness == "closed",
                                model.timezone == latest.timezone,
                                model.period_date < today,
                                available_at <= issue_at,
                            )
                            .order_by(model.period_date.desc())
                            .limit(1)
                        )
                        latest_closed = result.scalar_one_or_none()
                        if latest_closed is not None:
                            snapshot_rows.append(latest_closed)
                    try:
                        candidates = tuple(
                            {
                                row.period_date: _manual_snapshot(row) for row in snapshot_rows
                            }.values()
                        )
                    except InvalidStoredPeriod:
                        logger.warning(
                            "manual_cop_invalid_stored_period", outcome="forecast_refused"
                        )
                        candidates = ()
                        invalid_snapshot = True
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Manual COP storage unavailable") from None
    if invalid_snapshot and enabled:
        monitoring = monitoring_result((), issue_at) | {
            "status": "unavailable",
            "reason": "invalid_stored_period",
        }
    else:
        monitoring = await forecast_snapshot(candidates, issue_at, enabled)
    return ManualCOPListResponse(
        devices=devices,
        default_timezone=default_timezone,
        device_id=device_id,
        granularity=granularity,
        issue_at=issue_at,
        observations=observations,
        next_before=next_before,
        forecasting_enabled=enabled,
        monitoring=monitoring,
    )


@router.put(
    "/api/cop/manual/{device_id}/{granularity}/{period_date}",
    response_model=ManualCOPObservationResponse,
    dependencies=[Depends(_validate_manual_json)],
)
async def put_manual_cop(
    device_id: str,
    granularity: Literal["day", "month"],
    period_date: dt.date,
    body: ManualCOPPut,
    response: Response,
):
    try:
        validate_observation_period(
            granularity,
            period_date,
            body.timezone,
            body.observed_at,
            body.completeness,
            dt.datetime.now(dt.timezone.utc),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    mutation = {
        "device_id": device_id,
        "granularity": granularity,
        "period_date": period_date.isoformat(),
        "expected_revision": body.expected_revision,
    }
    values = {
        "timezone": body.timezone,
        "value": body.value,
        "observed_at": body.observed_at.astimezone(dt.timezone.utc),
        "completeness": body.completeness,
    }
    model = ManualCOPObservationRecord
    try:
        async with get_session() as session:
            await _known_manual_device(session, device_id)
            if body.expected_revision == "0":
                statement = (
                    pg_insert(model)
                    .values(
                        device_id=device_id,
                        granularity=granularity,
                        period_date=period_date,
                        **values,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[model.device_id, model.granularity, model.period_date]
                    )
                    .returning(model)
                )
            else:
                statement = (
                    update(model)
                    .where(
                        model.device_id == device_id,
                        model.granularity == granularity,
                        model.period_date == period_date,
                        model.revision == int(body.expected_revision),
                    )
                    .values(
                        **values,
                        revision=manual_cop_revision_seq.next_value(),
                        updated_at=func.clock_timestamp(),
                    )
                    .returning(model)
                )
            result = await session.execute(statement)
            row = result.scalar_one_or_none()
            if row is None:
                logger.info("manual_cop_mutation", **mutation, outcome="conflict")
                raise HTTPException(status_code=409, detail="Manual observation revision conflict")
            try:
                committed = ManualCOPObservationResponse(**asdict(_manual_snapshot(row)))
            except InvalidStoredPeriod:
                logger.warning("manual_cop_mutation", **mutation, outcome="invalid_stored_period")
                raise HTTPException(status_code=503, detail=_INVALID_STORED_DETAIL) from None
    except SQLAlchemyError:
        logger.warning("manual_cop_mutation", **mutation, outcome="storage_unavailable")
        raise HTTPException(status_code=503, detail="Manual COP storage unavailable") from None
    created = body.expected_revision == "0"
    response.status_code = 201 if created else 200
    logger.info(
        "manual_cop_mutation",
        **mutation,
        revision=committed.revision,
        outcome="created" if created else "updated",
    )
    return committed


@router.delete(
    "/api/cop/manual/{device_id}/{granularity}/{period_date}",
    status_code=204,
    dependencies=[Depends(_validate_manual_json)],
)
async def delete_manual_cop(
    device_id: str,
    granularity: Literal["day", "month"],
    period_date: dt.date,
    body: ManualCOPRevision,
):
    if granularity == "month" and period_date.day != 1:
        raise HTTPException(status_code=422, detail="A monthly period must start on the first day")
    mutation = {
        "device_id": device_id,
        "granularity": granularity,
        "period_date": period_date.isoformat(),
        "expected_revision": body.expected_revision,
    }
    model = ManualCOPObservationRecord
    try:
        async with get_session() as session:
            await _known_manual_device(session, device_id)
            result = await session.execute(
                delete(model)
                .where(
                    model.device_id == device_id,
                    model.granularity == granularity,
                    model.period_date == period_date,
                    model.revision == int(body.expected_revision),
                )
                .returning(model.revision)
            )
            if result.scalar_one_or_none() is None:
                logger.info("manual_cop_mutation", **mutation, outcome="conflict")
                raise HTTPException(status_code=409, detail="Manual observation revision conflict")
    except SQLAlchemyError:
        logger.warning("manual_cop_mutation", **mutation, outcome="storage_unavailable")
        raise HTTPException(status_code=503, detail="Manual COP storage unavailable") from None
    logger.info("manual_cop_mutation", **mutation, outcome="deleted")
    return Response(status_code=204)


@router.get("/api/cop/history")
async def get_cop_history(response: Response, hours: int = Query(168, ge=1, le=8760)):
    response.headers["X-COP-Status"] = "retired"
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
            "provenance": "legacy_unverified_estimate",
            "confidence": "estimated",
            "measured": False,
        }
        for r in rows
    ]


@router.get("/api/cop/stats")
async def get_cop_stats(
    response: Response, hours: int = Query(168, ge=1, le=8760), mode: str | None = Query(None)
):
    from packages.ml.models import direction_cop

    response.headers["X-COP-Status"] = "retired"
    return await direction_cop.get_average_cop(hours=hours, mode=mode)


@router.post("/api/cop/compute")
async def compute_cop(hours: int = Query(24, ge=1, le=168)):
    raise HTTPException(
        status_code=410,
        detail={"code": "cop_retired", "reason": "unverified_thermal_output"},
    )


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

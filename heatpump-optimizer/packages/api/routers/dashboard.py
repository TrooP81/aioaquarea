from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, desc, func, select

from packages.api._helpers import get_price_area
from packages.api.schemas import (
    ConsumptionResponse,
    DashboardResponse,
    DeviceSettingsResponse,
    DeviceStatusResponse,
    PlanResponse,
    StatsResponse,
)
from packages.core.database import get_session
from packages.core.plan_lifecycle import active_plan_query
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PriceRecord,
)

router = APIRouter()


@router.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get dashboard overview data."""
    now = dt.datetime.now(dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        status_result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = status_result.scalar_one_or_none()
        outdoor = (
            await resolve_outdoor_temperature(
                session,
                heat_pump_c=(
                    status.heat_pump_outdoor_temp
                    if status is not None and status.heat_pump_outdoor_temp is not None
                    else status.outdoor_temp
                    if status is not None
                    else None
                ),
                at=status.ts if status is not None else now,
            )
            if status is not None
            else None
        )

        area = await get_price_area()
        price_result = await session.execute(
            select(
                PriceRecord.price_eur_per_kwh,
                PriceRecord.price_currency,
                PriceRecord.price_source,
            )
            .where(
                and_(
                    PriceRecord.ts <= now,
                    PriceRecord.ts > now - dt.timedelta(hours=1),
                    PriceRecord.area == area,
                )
            )
            .order_by(desc(PriceRecord.ts))
            .limit(1)
        )
        current_price_row = price_result.one_or_none()

        consumption_result = await session.execute(
            select(
                func.max(ConsumptionRecord.heat_kwh),
                func.max(ConsumptionRecord.cool_kwh),
                func.max(ConsumptionRecord.tank_kwh),
            ).where(ConsumptionRecord.ts >= today_start)
        )
        consumption_row = consumption_result.one_or_none()

        consumption_records_result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= today_start)
            .order_by(ConsumptionRecord.ts)
        )
        consumption_records = consumption_records_result.scalars().all()

        prices_result = await session.execute(
            select(
                PriceRecord.ts,
                PriceRecord.price_eur_per_kwh,
                PriceRecord.price_currency,
                PriceRecord.price_source,
            ).where(
                and_(
                    PriceRecord.ts >= today_start,
                    PriceRecord.ts <= now,
                    PriceRecord.area == area,
                )
            )
        )
        price_by_hour: dict[dt.datetime, tuple[float, str, str]] = {
            ts.replace(minute=0, second=0, microsecond=0): (price, currency, source)
            for ts, price, currency, source in prices_result.all()
        }

        plan_result = await session.execute(active_plan_query(now))
        active_plan = plan_result.scalar_one_or_none()

        active_plan_actions_count = 0
        if active_plan is not None:
            active_plan_actions_count = (
                await session.execute(
                    select(func.count())
                    .select_from(PlanActionRecord)
                    .where(PlanActionRecord.plan_id == active_plan.id)
                )
            ).scalar() or 0

        override_result = await session.execute(
            select(OverrideRecord.id)
            .where(
                and_(
                    OverrideRecord.active,
                    OverrideRecord.ts_from <= now,
                    OverrideRecord.ts_to >= now,
                )
            )
            .order_by(desc(OverrideRecord.id))
            .limit(1)
        )
        active_override_id = override_result.scalar_one_or_none()

    today_kwh = 0.0
    if consumption_row and consumption_row[0] is not None:
        today_kwh = (
            (consumption_row[0] or 0) + (consumption_row[1] or 0) + (consumption_row[2] or 0)
        )

    current_price = current_price_row[0] if current_price_row is not None else None
    price_currency = current_price_row[1] if current_price_row is not None else "EUR"
    price_source = current_price_row[2] if current_price_row is not None else "unavailable"
    priced_cost = 0.0
    priced_kwh = 0.0
    unpriced_kwh = 0.0
    prev_record = None
    for record in consumption_records:
        if prev_record is not None and record.ts.date() == prev_record.ts.date():
            heat_delta = max(0.0, (record.heat_kwh or 0) - (prev_record.heat_kwh or 0))
            cool_delta = max(0.0, (record.cool_kwh or 0) - (prev_record.cool_kwh or 0))
            tank_delta = max(0.0, (record.tank_kwh or 0) - (prev_record.tank_kwh or 0))
            delta_kwh = heat_delta + cool_delta + tank_delta
            if delta_kwh > 0:
                hour_key = record.ts.replace(minute=0, second=0, microsecond=0)
                price_entry = price_by_hour.get(hour_key)
                if (
                    price_entry is not None
                    and price_entry[1] == price_currency
                    and price_entry[2] == price_source
                ):
                    priced_cost += delta_kwh * price_entry[0]
                    priced_kwh += delta_kwh
                else:
                    unpriced_kwh += delta_kwh
        prev_record = record

    # The first cumulative meter sample of the day has no preceding sample in
    # this query. Treat that energy as unpriced rather than attributing it to a
    # guessed market hour. This makes the displayed coverage truthful.
    unpriced_kwh += max(0.0, today_kwh - priced_kwh - unpriced_kwh)
    today_cost_complete = today_kwh <= 0.0001 or unpriced_kwh <= 0.0001
    today_cost = priced_cost if today_cost_complete else None
    coverage_pct = 100.0 if today_kwh <= 0.0001 else (priced_kwh / today_kwh) * 100.0

    return DashboardResponse(
        current_status=DeviceStatusResponse(
            ts=status.ts,
            device_id=status.device_id,
            mode=status.mode,
            operation_status=status.operation_status,
            outdoor_temp=outdoor.effective_c if outdoor is not None else status.outdoor_temp,
            heat_pump_outdoor_temp=(
                outdoor.heat_pump_c if outdoor is not None else status.heat_pump_outdoor_temp
            ),
            weather_outdoor_temp=outdoor.weather_c if outdoor is not None else None,
            outdoor_temp_source=outdoor.source
            if outdoor is not None
            else status.outdoor_temp_source,
            outdoor_temp_provider=outdoor.weather_provider if outdoor is not None else None,
            outdoor_temp_compensation_c=outdoor.compensation_c if outdoor is not None else None,
            outdoor_temp_fallback_reason=outdoor.fallback_reason if outdoor is not None else None,
            tank_temp=status.tank_temp,
            tank_target_temp=status.tank_target_temp,
            zone1_temp=status.zone1_temp,
            zone1_target_temp=status.zone1_target_temp,
            zone1_heat_min=status.zone1_heat_min,
            zone1_heat_max=status.zone1_heat_max,
            quiet_mode=status.quiet_mode,
            powerful_mode=status.powerful_mode,
            special_status=status.special_status,
            special_status_supported=status.special_status_supported,
            direction=status.direction,
            device_action=status.device_action,
            defrost_active=status.defrost_active,
            space_heating_active=status.space_heating_active,
            space_heating_evidence=status.space_heating_evidence,
            force_dhw=status.force_dhw,
            force_heater=status.force_heater,
            holiday_mode=status.holiday_mode,
        )
        if status
        else None,
        current_price=current_price,
        price_currency=price_currency,
        price_source=price_source,
        today_kwh=today_kwh,
        today_cost_eur=today_cost,
        today_cost_currency=price_currency,
        today_cost_priced_kwh=priced_kwh,
        today_cost_unpriced_kwh=unpriced_kwh,
        today_cost_priced_amount=priced_cost,
        today_cost_coverage_pct=round(max(0.0, min(100.0, coverage_pct)), 1),
        today_cost_complete=today_cost_complete,
        active_plan=PlanResponse(
            id=active_plan.id,
            created_at=active_plan.created_at,
            horizon_start=active_plan.horizon_start,
            horizon_end=active_plan.horizon_end,
            optimizer_version=active_plan.optimizer_version,
            cost_estimate_eur=active_plan.cost_estimate_eur,
            price_currency=active_plan.price_currency,
            price_source=active_plan.price_source,
            actions_count=active_plan_actions_count,
            status=active_plan.status,
            status_reason=active_plan.status_reason,
            superseded_at=active_plan.superseded_at,
            superseded_by_plan_id=active_plan.superseded_by_plan_id,
        )
        if active_plan
        else None,
        has_override=active_override_id is not None,
        override_id=active_override_id,
    )


@router.get("/api/status/history", response_model=list[DeviceStatusResponse])
async def get_status_history(hours: int = Query(24, ge=1, le=720)):
    """Get device status history."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord)
            .where(DeviceStatusRecord.ts >= since)
            .order_by(DeviceStatusRecord.ts)
        )
        rows = result.scalars().all()

    return [
        DeviceStatusResponse(
            ts=r.ts,
            device_id=r.device_id,
            mode=r.mode,
            operation_status=r.operation_status,
            outdoor_temp=r.outdoor_temp,
            heat_pump_outdoor_temp=r.heat_pump_outdoor_temp,
            outdoor_temp_source=r.outdoor_temp_source,
            tank_temp=r.tank_temp,
            tank_target_temp=r.tank_target_temp,
            zone1_temp=r.zone1_temp,
            zone1_target_temp=r.zone1_target_temp,
            zone1_heat_min=r.zone1_heat_min,
            zone1_heat_max=r.zone1_heat_max,
            quiet_mode=r.quiet_mode,
            powerful_mode=r.powerful_mode,
            special_status=r.special_status,
            special_status_supported=r.special_status_supported,
            direction=r.direction,
            device_action=r.device_action,
            defrost_active=r.defrost_active,
            space_heating_active=r.space_heating_active,
            space_heating_evidence=r.space_heating_evidence,
            force_dhw=r.force_dhw,
            force_heater=r.force_heater,
            holiday_mode=r.holiday_mode,
        )
        for r in rows
    ]


@router.get("/api/device/settings", response_model=DeviceSettingsResponse)
async def get_device_settings():
    """Return the latest polled settings/state from the heat pump."""
    async with get_session() as session:
        result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="No device data available yet")

    return DeviceSettingsResponse(
        polled_at=row.ts,
        device_id=row.device_id,
        mode=row.mode,
        operation_status=row.operation_status,
        tank_temp=row.tank_temp,
        tank_target_temp=row.tank_target_temp,
        tank_heat_max=row.tank_heat_max,
        tank_heat_min=row.tank_heat_min,
        tank_operation_status=row.tank_operation_status,
        zone1_temp=row.zone1_temp,
        zone1_target_temp=row.zone1_target_temp,
        zone1_heat_min=row.zone1_heat_min,
        zone1_heat_max=row.zone1_heat_max,
        zone1_operation_status=row.zone1_operation_status,
        zone2_temp=row.zone2_temp,
        zone2_target_temp=row.zone2_target_temp,
        zone2_heat_min=row.zone2_heat_min,
        zone2_heat_max=row.zone2_heat_max,
        zone2_operation_status=row.zone2_operation_status,
        quiet_mode=row.quiet_mode,
        powerful_mode=row.powerful_mode,
        special_status=row.special_status,
        special_status_supported=row.special_status_supported,
        force_dhw=row.force_dhw,
        force_heater=row.force_heater,
        holiday_mode=row.holiday_mode,
        outdoor_temp=row.outdoor_temp,
        heat_pump_outdoor_temp=row.heat_pump_outdoor_temp,
        outdoor_temp_source=row.outdoor_temp_source,
        direction=row.direction,
        device_action=row.device_action,
        defrost_active=row.defrost_active,
        space_heating_active=row.space_heating_active,
        space_heating_evidence=row.space_heating_evidence,
        pump_duty=row.pump_duty,
    )


@router.get("/api/consumption/history", response_model=list[ConsumptionResponse])
async def get_consumption_history(hours: int = Query(24, ge=1, le=720)):
    """Get consumption history as per-interval deltas (not cumulative totals)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= since)
            .order_by(ConsumptionRecord.ts)
        )
        rows = result.scalars().all()

    responses = []
    prev = None
    for r in rows:
        if prev is not None and r.ts.date() == prev.ts.date():
            heat_delta = max(0.0, (r.heat_kwh or 0) - (prev.heat_kwh or 0))
            cool_delta = max(0.0, (r.cool_kwh or 0) - (prev.cool_kwh or 0))
            tank_delta = max(0.0, (r.tank_kwh or 0) - (prev.tank_kwh or 0))
            responses.append(
                ConsumptionResponse(
                    ts=r.ts,
                    heat_kwh=heat_delta,
                    cool_kwh=cool_delta,
                    tank_kwh=tank_delta,
                    total_kwh=heat_delta + cool_delta + tank_delta,
                    outdoor_temp=r.outdoor_temp,
                )
            )
        prev = r

    return responses


@router.get("/api/stats", response_model=StatsResponse)
async def get_stats(period: str = Query("day", pattern="^(day|week|month)$")):
    """Get aggregated statistics."""
    now = dt.datetime.now(dt.timezone.utc)
    if period == "day":
        since = now - dt.timedelta(days=1)
    elif period == "week":
        since = now - dt.timedelta(weeks=1)
    else:
        since = now - dt.timedelta(days=30)

    async with get_session() as session:
        daily_max = (
            select(
                func.max(ConsumptionRecord.heat_kwh).label("heat"),
                func.max(ConsumptionRecord.cool_kwh).label("cool"),
                func.max(ConsumptionRecord.tank_kwh).label("tank"),
            )
            .where(ConsumptionRecord.ts >= since)
            .group_by(func.date(ConsumptionRecord.ts))
            .subquery()
        )
        cons_result = await session.execute(
            select(
                func.sum(daily_max.c.heat),
                func.sum(daily_max.c.cool),
                func.sum(daily_max.c.tank),
            )
        )
        cons = cons_result.one()
        total_kwh = (cons[0] or 0) + (cons[1] or 0) + (cons[2] or 0)

        price_result = await session.execute(
            select(func.avg(PriceRecord.price_eur_per_kwh)).where(PriceRecord.ts >= since)
        )
        avg_price = price_result.scalar() or 0.10

    total_cost = total_kwh * avg_price

    return StatsResponse(
        period=period,
        total_kwh=total_kwh,
        total_cost_eur=total_cost,
        avg_price_eur_kwh=avg_price,
    )

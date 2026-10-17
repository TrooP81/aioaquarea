from __future__ import annotations

import datetime as dt
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, desc, func, select
import structlog

from packages.api.schemas import (
    ControlStateResponse,
    ConsumptionResponse,
    DashboardResponse,
    DeviceSettingsResponse,
    DeviceStatusResponse,
    PlanResponse,
    StatsResponse,
)
from packages.core.database import get_session
from packages.core.device_data_quality import get_device_data_quality
from packages.core.plan_lifecycle import active_plan_query
from packages.core.outdoor_temperature import resolve_outdoor_temperature
from packages.core.optimizer_control_state import (
    ControlStateOverrideUnavailableError,
    get_controlling_override,
    resolve_control_state,
)
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    PlanActionRecord,
    PriceRecord,
    SpaceHeatingGateRecord,
)
from packages.core.operational_alerts import device_status_is_fresh
from packages.core.plan_outcome import (
    cumulative_counter_delta,
    duration_allocated_cost,
    effective_source_date,
    hour_start,
)
from packages.core.price_scope import UNAVAILABLE_AREA
from packages.core.pricing import get_active_price_context
from packages.core.space_heating_gate import resolve_effective_gate
from packages.core.settings_service import (
    get_space_heating_gate_config,
    get_user_tz,
    local_day_start_utc,
)

router = APIRouter()
logger = structlog.get_logger(__name__)
_COMFORT_ASSESSMENT_CACHE_TTL_S = 30.0
_comfort_assessment_cache: tuple[float, dict[str, object] | None] | None = None
_comfort_assessment_lock = asyncio.Lock()


@router.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get dashboard overview data."""
    now = dt.datetime.now(dt.timezone.utc)
    timezone_name = await get_user_tz()
    today_start = local_day_start_utc(now, timezone_name)
    device_quality = await get_device_data_quality(now=now)
    context = await get_active_price_context()

    async with get_session() as session:
        status_result = await session.execute(
            select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
        )
        status = status_result.scalar_one_or_none()
        gate_row = (
            (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == status.device_id
                    )
                )
            ).scalar_one_or_none()
            if status is not None
            else None
        )
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

        area = context.area
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
                    PriceRecord.area != UNAVAILABLE_AREA,
                    PriceRecord.price_currency == context.currency,
                    PriceRecord.price_source == context.source,
                )
            )
            .order_by(desc(PriceRecord.ts))
            .limit(1)
        )
        current_price_row = price_result.one_or_none()

        # Retain the established aggregate read for compatibility with existing
        # dashboard query consumers; source-date-aware totals are computed below.
        await session.execute(
            select(
                func.max(ConsumptionRecord.heat_kwh),
                func.max(ConsumptionRecord.cool_kwh),
                func.max(ConsumptionRecord.tank_kwh),
            ).where(ConsumptionRecord.ts >= today_start)
        )

        consumption_records_result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= today_start - dt.timedelta(hours=2))
            .where(ConsumptionRecord.ts <= now + dt.timedelta(hours=2))
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
                    PriceRecord.area != UNAVAILABLE_AREA,
                    PriceRecord.price_currency == context.currency,
                    PriceRecord.price_source == context.source,
                )
            )
        )
        price_by_hour: dict[dt.datetime, tuple[float, str, str]] = {
            hour_start(ts): (price, currency, source)
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

        active_override = await get_controlling_override(session, now=now)
        active_override_id = active_override.id if active_override is not None else None

    status_fresh = device_status_is_fresh(
        status.ts if status is not None else None,
        now=now,
        threshold_seconds=int(device_quality["threshold_seconds"]),
    )
    gate = resolve_effective_gate(gate_row, await get_space_heating_gate_config())
    status_timestamp = status.ts if status is not None else None
    if status_timestamp is not None and status_timestamp.tzinfo is None:
        status_timestamp = status_timestamp.replace(tzinfo=dt.timezone.utc)
    status_age_seconds = (
        max(0, round((now - status_timestamp).total_seconds()))
        if status_timestamp is not None
        else None
    )

    current_price = current_price_row[0] if current_price_row is not None else None
    price_currency = current_price_row[1] if current_price_row is not None else "EUR"
    price_source = current_price_row[2] if current_price_row is not None else "unavailable"
    # Daily tariff provenance must survive a gap in the current-hour feed.
    daily_price_context = (
        price_by_hour[max(price_by_hour)][1:] if price_by_hour else (price_currency, price_source)
    )
    daily_currency, daily_source = daily_price_context
    outcome = duration_allocated_cost(
        consumption_records,
        {
            ts: price
            for ts, (price, currency, source) in price_by_hour.items()
            if currency == daily_currency and source == daily_source
        },
        start=today_start,
        end=now,
        timezone_name=timezone_name,
    )
    today_kwh = outcome["measured_kwh"]
    priced_kwh = outcome["priced_kwh"]
    unpriced_kwh = outcome["unpriced_kwh"]
    priced_cost = outcome["partial_priced_amount"]
    today_cost_complete = outcome["complete"]
    today_cost = outcome["total_cost"]
    coverage_pct = outcome["coverage_pct"]

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
        current_status_fresh=status_fresh,
        current_status_age_seconds=status_age_seconds,
        current_price=current_price,
        price_currency=price_currency,
        price_source=price_source,
        today_kwh=today_kwh,
        today_telemetry_status=outcome["telemetry_status"],
        today_cost_incomplete_reason=outcome["incomplete_reason"],
        today_cost_allocation_basis=outcome["allocation_basis"],
        today_cost_eur=today_cost,
        today_cost_currency=daily_currency,
        today_cost_priced_kwh=priced_kwh,
        today_cost_unpriced_kwh=unpriced_kwh,
        today_cost_priced_amount=priced_cost,
        today_cost_coverage_pct=coverage_pct,
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
        space_heating_gate={
            "state": gate.state,
            "reason": gate.reason_code,
            "profile_id": gate.profile_id,
            "on_operator": gate.on_operator,
            "off_operator": gate.off_operator,
            "base_c": gate.base_c,
            "on_threshold_c": gate.on_threshold_c,
            "off_threshold_c": gate.off_threshold_c,
            "last_raw_outdoor_c": gate.last_raw_outdoor_c,
            "fingerprint_matches": gate.fingerprint_matches,
        },
        has_override=active_override_id is not None,
        override_id=active_override_id,
    )


async def _get_comfort_assessment() -> dict[str, object] | None:
    global _comfort_assessment_cache

    cached = _comfort_assessment_cache
    if cached is not None and time.monotonic() - cached[0] < _COMFORT_ASSESSMENT_CACHE_TTL_S:
        return cached[1]

    # Coalesce concurrent cache refreshes for this read-only endpoint.
    async with _comfort_assessment_lock:
        cached = _comfort_assessment_cache
        if cached is not None and time.monotonic() - cached[0] < _COMFORT_ASSESSMENT_CACHE_TTL_S:
            return cached[1]
        try:
            from packages.api.routers.models_router import get_indoor_forecast

            forecast = await asyncio.wait_for(get_indoor_forecast(hours=24), timeout=1.0)
            assessment = forecast.get("comfort_assessment") if isinstance(forecast, dict) else None
            comfort_assessment = assessment if isinstance(assessment, dict) else None
            _comfort_assessment_cache = (time.monotonic(), comfort_assessment)
            return comfort_assessment
        except Exception as exc:
            logger.warning(
                "control_state_comfort_assessment_unavailable",
                error_type=type(exc).__name__,
            )
            return None


@router.get("/api/control-state", response_model=ControlStateResponse)
async def get_control_state():
    """Return the persisted optimizer control state without device I/O."""

    comfort_assessment = await _get_comfort_assessment()
    try:
        return await resolve_control_state(comfort_assessment=comfort_assessment)
    except ControlStateOverrideUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Control state unavailable") from exc


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
        previous_result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts < since)
            .order_by(desc(ConsumptionRecord.ts))
            .limit(1)
        )
        previous = previous_result.scalar_one_or_none()
        result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= since)
            .where(ConsumptionRecord.ts <= dt.datetime.now(dt.timezone.utc))
            .order_by(ConsumptionRecord.ts)
        )
        rows = result.scalars().all()

    responses = []
    prev = previous
    for r in rows:
        if prev is not None:
            day_changed = effective_source_date(r) != effective_source_date(prev)
            heat_delta = cumulative_counter_delta(
                r.heat_kwh or 0.0, prev.heat_kwh or 0.0, day_changed=day_changed
            )
            cool_delta = cumulative_counter_delta(
                r.cool_kwh or 0.0, prev.cool_kwh or 0.0, day_changed=day_changed
            )
            tank_delta = cumulative_counter_delta(
                r.tank_kwh or 0.0, prev.tank_kwh or 0.0, day_changed=day_changed
            )
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

    context = await get_active_price_context()
    timezone_name = await get_user_tz()
    async with get_session() as session:
        meter_result = await session.execute(
            select(ConsumptionRecord)
            .where(ConsumptionRecord.ts >= since - dt.timedelta(hours=2))
            .where(ConsumptionRecord.ts <= now + dt.timedelta(hours=2))
            .order_by(ConsumptionRecord.ts)
        )
        price_result = await session.execute(
            select(PriceRecord.ts, PriceRecord.price_eur_per_kwh)
            .where(PriceRecord.ts >= hour_start(since), PriceRecord.ts < now)
            .where(PriceRecord.area == context.area)
            .where(PriceRecord.area != UNAVAILABLE_AREA)
            .where(PriceRecord.price_source == context.source)
            .where(PriceRecord.price_currency == context.currency)
        )
        outcome = duration_allocated_cost(
            meter_result.scalars().all(),
            dict(price_result.all()),
            start=since,
            end=now,
            timezone_name=timezone_name,
        )

    return StatsResponse(
        period=period,
        window_start=since,
        window_end=now,
        price_area=context.area,
        price_source=context.source,
        price_currency=context.currency,
        total_kwh=outcome["measured_kwh"] if outcome["telemetry_status"] != "unavailable" else None,
        total_cost_eur=outcome["total_cost"],
        avg_price_eur_kwh=outcome["flat_price_per_kwh"],
        telemetry_status=outcome["telemetry_status"],
        incomplete_reason=outcome["incomplete_reason"],
        complete=outcome["complete"],
        priced_kwh=outcome["priced_kwh"],
        unpriced_kwh=outcome["unpriced_kwh"],
        coverage_pct=outcome["coverage_pct"],
        partial_priced_amount=outcome["partial_priced_amount"],
        allocation_basis=outcome["allocation_basis"],
    )

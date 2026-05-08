"""Shared API request/response models."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel


class DeviceStatusResponse(BaseModel):
    ts: dt.datetime
    device_id: str
    mode: Optional[str] = None
    operation_status: Optional[int] = None
    outdoor_temp: Optional[float] = None
    tank_temp: Optional[float] = None
    tank_target_temp: Optional[int] = None
    zone1_temp: Optional[float] = None
    zone1_target_temp: Optional[float] = None
    quiet_mode: Optional[int] = None
    powerful_mode: Optional[int] = None
    direction: Optional[str] = None
    device_action: Optional[str] = None
    defrost_active: Optional[bool] = None
    force_dhw: Optional[int] = None
    force_heater: Optional[int] = None
    holiday_mode: Optional[int] = None


class DeviceSettingsResponse(BaseModel):
    polled_at: dt.datetime
    device_id: str
    mode: Optional[str] = None
    operation_status: Optional[int] = None
    tank_temp: Optional[float] = None
    tank_target_temp: Optional[int] = None
    tank_heat_max: Optional[int] = None
    tank_heat_min: Optional[int] = None
    tank_operation_status: Optional[int] = None
    zone1_temp: Optional[float] = None
    zone1_target_temp: Optional[float] = None
    zone1_operation_status: Optional[int] = None
    zone2_temp: Optional[float] = None
    zone2_target_temp: Optional[float] = None
    zone2_operation_status: Optional[int] = None
    quiet_mode: Optional[int] = None
    powerful_mode: Optional[int] = None
    special_status: Optional[int] = None
    force_dhw: Optional[int] = None
    force_heater: Optional[int] = None
    holiday_mode: Optional[int] = None
    outdoor_temp: Optional[float] = None
    direction: Optional[str] = None
    device_action: Optional[str] = None
    defrost_active: Optional[bool] = None
    pump_duty: Optional[int] = None


class ConsumptionResponse(BaseModel):
    ts: dt.datetime
    heat_kwh: Optional[float] = None
    cool_kwh: Optional[float] = None
    tank_kwh: Optional[float] = None
    total_kwh: Optional[float] = None
    outdoor_temp: Optional[float] = None


class PriceResponse(BaseModel):
    ts: dt.datetime
    price_eur_per_kwh: float


class WeatherResponse(BaseModel):
    ts: dt.datetime
    temperature: Optional[float] = None
    wind_speed: Optional[float] = None
    humidity: Optional[float] = None


class PlanResponse(BaseModel):
    id: int
    created_at: dt.datetime
    horizon_start: dt.datetime
    horizon_end: dt.datetime
    optimizer_version: str
    cost_estimate_eur: Optional[float] = None
    actions_count: int = 0


class PlanDetailResponse(PlanResponse):
    actions: list[dict]


class OverrideCreate(BaseModel):
    ts_from: dt.datetime
    ts_to: dt.datetime
    action_type: str
    reason: Optional[str] = None


class StatsResponse(BaseModel):
    period: str
    total_kwh: float
    total_cost_eur: float
    avg_cop: Optional[float] = None
    avg_price_eur_kwh: float
    savings_vs_baseline_eur: Optional[float] = None


class DashboardResponse(BaseModel):
    current_status: Optional[DeviceStatusResponse] = None
    current_price: Optional[float] = None
    today_kwh: float = 0
    today_cost_eur: float = 0
    active_plan: Optional[PlanResponse] = None
    has_override: bool = False
    override_id: Optional[int] = None


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


class ComfortScheduleUpdate(BaseModel):
    weekday: list[int]
    weekend: list[int]


class IndoorTempResponse(BaseModel):
    id: int
    timestamp: dt.datetime
    device_id: str
    device_label: Optional[str] = None
    room: Optional[str] = None
    temperature: float


class TestConnectionRequest(BaseModel):
    service: str
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None
    area: Optional[str] = None
    pat: Optional[str] = None


class TestConnectionResponse(BaseModel):
    service: str
    success: bool
    message: str
    details: Optional[dict] = None

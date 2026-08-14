"""Shared API request/response models."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field


class DeviceStatusResponse(BaseModel):
    ts: dt.datetime
    device_id: str
    mode: Optional[str] = None
    operation_status: Optional[int] = None
    outdoor_temp: Optional[float] = None
    heat_pump_outdoor_temp: Optional[float] = None
    weather_outdoor_temp: Optional[float] = None
    outdoor_temp_source: Optional[str] = None
    outdoor_temp_provider: Optional[str] = None
    outdoor_temp_compensation_c: Optional[float] = None
    outdoor_temp_fallback_reason: Optional[str] = None
    tank_temp: Optional[float] = None
    tank_target_temp: Optional[int] = None
    zone1_temp: Optional[float] = None
    zone1_target_temp: Optional[float] = None
    zone1_heat_min: Optional[int] = None
    zone1_heat_max: Optional[int] = None
    quiet_mode: Optional[int] = None
    powerful_mode: Optional[int] = None
    special_status: Optional[int] = None
    special_status_supported: Optional[bool] = None
    direction: Optional[str] = None
    device_action: Optional[str] = None
    defrost_active: Optional[bool] = None
    space_heating_active: Optional[bool] = None
    space_heating_evidence: Optional[str] = None
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
    zone1_heat_min: Optional[int] = None
    zone1_heat_max: Optional[int] = None
    zone1_operation_status: Optional[int] = None
    zone2_temp: Optional[float] = None
    zone2_target_temp: Optional[float] = None
    zone2_heat_min: Optional[int] = None
    zone2_heat_max: Optional[int] = None
    zone2_operation_status: Optional[int] = None
    quiet_mode: Optional[int] = None
    powerful_mode: Optional[int] = None
    special_status: Optional[int] = None
    special_status_supported: Optional[bool] = None
    force_dhw: Optional[int] = None
    force_heater: Optional[int] = None
    holiday_mode: Optional[int] = None
    outdoor_temp: Optional[float] = None
    heat_pump_outdoor_temp: Optional[float] = None
    outdoor_temp_source: Optional[str] = None
    direction: Optional[str] = None
    device_action: Optional[str] = None
    defrost_active: Optional[bool] = None
    space_heating_active: Optional[bool] = None
    space_heating_evidence: Optional[str] = None
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
    price_currency: str = "EUR"
    price_source: str = "legacy"
    fetched_at: Optional[dt.datetime] = None


class WeatherResponse(BaseModel):
    ts: dt.datetime
    temperature: Optional[float] = None
    wind_speed: Optional[float] = None
    humidity: Optional[float] = None
    cloud_cover: Optional[float] = None
    irradiance: Optional[float] = None
    precipitation: Optional[float] = None
    source: Optional[str] = None
    forecast_issued_at: Optional[dt.datetime] = None


class PlanResponse(BaseModel):
    id: int
    created_at: dt.datetime
    horizon_start: dt.datetime
    horizon_end: dt.datetime
    optimizer_version: str
    cost_estimate_eur: Optional[float] = None
    price_currency: str = "EUR"
    price_source: str = "legacy"
    actions_count: int = 0
    status: str = "active"
    status_reason: Optional[str] = None
    superseded_at: Optional[dt.datetime] = None
    superseded_by_plan_id: Optional[int] = None


class PlanDetailResponse(PlanResponse):
    actions: list[dict]
    outcome: dict = Field(default_factory=dict)
    change_summary: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)


class PlanActivityResponse(BaseModel):
    """A completed or attempted action across all plan revisions."""

    id: int
    plan_id: int
    plan_created_at: dt.datetime
    optimizer_version: str
    scheduled_ts: dt.datetime
    action_type: str
    status: str
    executed_at: Optional[dt.datetime] = None
    lateness_seconds: Optional[int] = None
    payload: dict = Field(default_factory=dict)
    result: Optional[dict] = None


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
    price_currency: str = "EUR"
    price_source: str = "legacy"
    today_kwh: float = 0
    # Legacy name retained for wire compatibility. A value is supplied only
    # when every measured consumption interval has a compatible spot price.
    today_cost_eur: Optional[float] = None
    today_cost_currency: str = "EUR"
    today_cost_priced_kwh: float = 0
    today_cost_unpriced_kwh: float = 0
    today_cost_priced_amount: float = 0
    today_cost_coverage_pct: float = 0
    today_cost_complete: bool = False
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


class IndoorForecastPointResponse(BaseModel):
    hour: int
    predicted_indoor_temp: float
    ts: dt.datetime | None = None
    source: str | None = None
    model_source: str | None = None
    space_heating_fraction: float | None = Field(default=None, ge=0, le=1)
    prediction_lower_c: float | None = None
    prediction_upper_c: float | None = None
    prediction_interval_status: str | None = None


class IndoorForecastTargetResponse(BaseModel):
    hour: int
    ts: dt.datetime | None = None
    target: float
    comfort_hour: bool


class IndoorForecastWeatherResponse(BaseModel):
    """Hourly weather conditions used to calculate the indoor forecast."""

    ts: dt.datetime
    hour: int = Field(ge=0, le=23)
    outdoor_temp: float | None = None
    wind_speed: float | None = None
    irradiance: float | None = None
    precipitation: float | None = None
    input_status: str = "observed"
    imputed_fields: list[str] = Field(default_factory=list)


class IndoorForecastPriceResponse(BaseModel):
    """Price in the same optimizer slot as the weather and temperature forecast."""

    ts: dt.datetime
    price_eur_per_kwh: float | None = None
    # Neutral alias for new consumers. The legacy EUR-named field remains for
    # compatibility even when the configured market uses SEK.
    price_per_kwh: float | None = None
    currency: str | None = None


class IndoorForecastActionResponse(BaseModel):
    hour: int
    action_type: str
    status: str
    payload: dict = Field(default_factory=dict)


class IndoorForecastResponse(BaseModel):
    """The versioned, self-contained forecast contract used by all forecast UIs."""

    current_indoor: float | None = None
    outdoor_temp: float | None = None
    forecast: list[IndoorForecastPointResponse]
    forecast_with_plan: list[IndoorForecastPointResponse]
    forecast_no_heating: list[IndoorForecastPointResponse]
    target_schedule: list[IndoorForecastTargetResponse]
    weather_forecast: list[IndoorForecastWeatherResponse]
    price_forecast: list[IndoorForecastPriceResponse]
    planned_actions: list[IndoorForecastActionResponse]
    forecast_source: str
    forecast_status: str = "available"
    forecast_unavailable_reason: str | None = None
    plan_id: int | None = None
    plan_created_at: dt.datetime | None = None
    comfort_assessment: dict = Field(default_factory=dict)
    forecast_provenance: dict = Field(default_factory=dict)
    display_status: str = "fresh"
    plan_age_seconds: int | None = None
    sensor_age_seconds: int | None = None
    current_vs_plan_delta_c: float | None = None

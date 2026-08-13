"""SQLAlchemy ORM models (TimescaleDB hypertables marked in comments)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DeviceStatusRecord(Base):
    """Device status snapshot — hypertable on `ts`."""

    __tablename__ = "device_status"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str | None] = mapped_column(String(32))
    operation_status: Mapped[int | None] = mapped_column(Integer)
    outdoor_temp: Mapped[float | None] = mapped_column(Float)
    # Effective temperature used by planning/learning is stored above. Keep
    # the physical Aquarea sensor and source separately for diagnostics.
    heat_pump_outdoor_temp: Mapped[float | None] = mapped_column(Float)
    outdoor_temp_source: Mapped[str | None] = mapped_column(String(32))
    tank_temp: Mapped[float | None] = mapped_column(Float)
    tank_target_temp: Mapped[int | None] = mapped_column(Integer)
    tank_operation_status: Mapped[int | None] = mapped_column(Integer)
    zone1_temp: Mapped[float | None] = mapped_column(Float)
    zone1_target_temp: Mapped[float | None] = mapped_column(Float)
    zone2_temp: Mapped[float | None] = mapped_column(Float)
    zone2_target_temp: Mapped[float | None] = mapped_column(Float)
    quiet_mode: Mapped[int | None] = mapped_column(Integer)
    powerful_mode: Mapped[int | None] = mapped_column(Integer)
    special_status: Mapped[int | None] = mapped_column(Integer)
    # Phase 1: compressor activity fields
    direction: Mapped[str | None] = mapped_column(String(16))  # IDLE/PUMP/WATER
    pump_duty: Mapped[int | None] = mapped_column(Integer)  # 0=OFF, 1=ON
    device_action: Mapped[str | None] = mapped_column(
        String(24)
    )  # OFF/IDLE/HEATING/COOLING/HEATING_WATER
    defrost_active: Mapped[bool | None] = mapped_column(Boolean)
    # Explicit room-heating evidence. PUMP alone is not proof of compressor heat.
    space_heating_active: Mapped[bool | None] = mapped_column(Boolean)
    space_heating_evidence: Mapped[str | None] = mapped_column(String(48))
    force_dhw: Mapped[int | None] = mapped_column(Integer)  # 0=OFF, 1=ON
    force_heater: Mapped[int | None] = mapped_column(Integer)  # 0=OFF, 1=ON
    holiday_mode: Mapped[int | None] = mapped_column(Integer)  # 0=OFF, 1=ON
    # Zone operation status
    zone1_operation_status: Mapped[int | None] = mapped_column(Integer)
    zone2_operation_status: Mapped[int | None] = mapped_column(Integer)
    # Tank limits
    tank_heat_max: Mapped[int | None] = mapped_column(Integer)
    tank_heat_min: Mapped[int | None] = mapped_column(Integer)


class ConsumptionRecord(Base):
    """Energy consumption — hypertable on `ts`."""

    __tablename__ = "consumption"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    heat_kwh: Mapped[float | None] = mapped_column(Float)
    cool_kwh: Mapped[float | None] = mapped_column(Float)
    tank_kwh: Mapped[float | None] = mapped_column(Float)
    outdoor_temp: Mapped[float | None] = mapped_column(Float)
    heat_pump_outdoor_temp: Mapped[float | None] = mapped_column(Float)
    outdoor_temp_source: Mapped[str | None] = mapped_column(String(32))


class PriceRecord(Base):
    """Electricity spot price in the currency reported by its source."""

    __tablename__ = "prices"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    area: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Legacy column name retained for database compatibility. The amount is in
    # ``price_currency``; callers must never infer EUR from this name.
    price_eur_per_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    price_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EUR", server_default="EUR"
    )
    price_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="legacy", server_default="legacy"
    )
    # When the provider response was retrieved. ``ts`` is the market hour,
    # which can be in the future and therefore cannot prove feed freshness.
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class WeatherRecord(Base):
    """Weather observations/forecast — hypertable on `ts`."""

    __tablename__ = "weather"
    __table_args__ = (Index("ix_weather_source_issued", "source", "forecast_issued_at"),)

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(32), primary_key=True, default="open-meteo")
    temperature: Mapped[float | None] = mapped_column(Float)
    irradiance: Mapped[float | None] = mapped_column(Float)
    wind_speed: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[float | None] = mapped_column(Float)
    cloud_cover: Mapped[float | None] = mapped_column(Float)
    # Precipitation amount for the forecast hour (mm/h).
    precipitation: Mapped[float | None] = mapped_column(Float)
    # When this forecast was retrieved. ``ts`` is the forecasted hour, not the
    # age of the weather information used by a plan.
    forecast_issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PlanRecord(Base):
    """Optimizer plan output."""

    __tablename__ = "plans"
    __table_args__ = (
        Index(
            "ux_plans_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    horizon_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    horizon_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    plan_json: Mapped[str] = mapped_column(Text)
    optimizer_version: Mapped[str] = mapped_column(String(32), default="rules_v1")
    cost_estimate_eur: Mapped[float | None] = mapped_column(Float)
    # Legacy cost field name retained for existing installations. The amount
    # is denominated in this explicit source currency.
    price_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EUR", server_default="EUR"
    )
    price_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="legacy", server_default="legacy"
    )
    # Immutable non-secret record of the source data and model evidence that
    # existed when this plan was created. The plan JSON retains hourly values;
    # this field makes source and freshness visible without re-parsing it.
    input_provenance_json: Mapped[str | None] = mapped_column(Text)
    # Only one plan may be active at a time.  Keeping the lifecycle on the
    # plan itself makes replacements explicit instead of leaving old pending
    # actions executable after a re-optimisation.
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    status_reason: Mapped[str | None] = mapped_column(String(256))
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_plan_id: Mapped[int | None] = mapped_column(Integer)


class PlanActionRecord(Base):
    """Individual action within a plan."""

    __tablename__ = "plan_actions"
    __table_args__ = (
        Index("ix_plan_actions_plan_id", "plan_id"),
        Index("ix_plan_actions_status_scheduled", "status", "scheduled_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    action_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expected_state_json: Mapped[str | None] = mapped_column(Text)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_observed_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)


class OptimizationRequestRecord(Base):
    """Durable request for the optimizer service to generate a new plan.

    API processes never generate a plan themselves.  They enqueue a request
    here and the singleton optimizer service claims it, which avoids competing
    in-memory cooldowns and model instances across processes.
    """

    __tablename__ = "optimization_requests"
    __table_args__ = (Index("ix_optimization_requests_status_requested", "status", "requested_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str] = mapped_column(String(64), default="api")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    plan_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class ServiceHeartbeatRecord(Base):
    """Last successful heartbeat from a long-running control service."""

    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text)


class OverrideRecord(Base):
    """Manual user overrides that block the optimizer."""

    __tablename__ = "overrides"
    __table_args__ = (Index("ix_overrides_active_ts", "active", "ts_from", "ts_to"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ts_to: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    action_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLogRecord(Base):
    """Audit log for all write actions."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_device: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String(32))


class SettingRecord(Base):
    """Key-value settings configurable via GUI."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FaultRecord(Base):
    """Device fault/error log — tracks equipment errors over time."""

    __tablename__ = "faults"
    __table_args__ = (
        Index("ix_faults_device_ts", "device_id", "ts"),
        Index("ix_faults_device_resolved", "device_id", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    device_id: Mapped[str] = mapped_column(String(128))
    error_code: Mapped[str] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(256))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    outdoor_temp: Mapped[float | None] = mapped_column(Float)


class COPRecord(Base):
    """Computed COP snapshots — derived from direction + consumption intervals."""

    __tablename__ = "cop_history"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cop_value: Mapped[float | None] = mapped_column(Float)
    mode: Mapped[str | None] = mapped_column(String(24))  # HEATING/COOLING/HEATING_WATER
    outdoor_temp: Mapped[float | None] = mapped_column(Float)
    electrical_kwh: Mapped[float | None] = mapped_column(Float)
    thermal_kwh: Mapped[float | None] = mapped_column(Float)


class IndoorTempReading(Base):
    """Actual indoor air temperature from SmartThings sensors."""

    __tablename__ = "indoor_temp_reading"
    __table_args__ = (Index("ix_indoor_temp_reading_device_ts", "device_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    device_id: Mapped[str] = mapped_column(String(100))
    device_label: Mapped[str | None] = mapped_column(String(200))
    room: Mapped[str | None] = mapped_column(String(200))
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    device_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    is_stale: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)


class ShowerEventRecord(Base):
    """Reactive shower detection events — tracks DHW boost triggered by rapid tank temp drop."""

    __tablename__ = "shower_events"
    __table_args__ = (Index("ix_shower_events_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    pre_shower_temp: Mapped[float] = mapped_column(Float, nullable=False)
    recovered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(24), default="active"
    )  # active / recovered / timeout / skipped_peak
    peak_price_skipped: Mapped[bool] = mapped_column(Boolean, default=False)


class AppLogRecord(Base):
    """Application log entries captured from structlog for dashboard display."""

    __tablename__ = "app_logs"
    __table_args__ = (Index("ix_app_logs_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(16))
    logger_name: Mapped[str | None] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(256))
    details_json: Mapped[str | None] = mapped_column(Text)
    service: Mapped[str] = mapped_column(String(32))


class SmartThingsToken(Base):
    """Persisted SmartThings OAuth 2.0 tokens (single-row, id=1)."""

    __tablename__ = "smartthings_oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(String(32), default="bearer")
    scope: Mapped[str | None] = mapped_column(String(256))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

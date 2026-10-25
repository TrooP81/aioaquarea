"""SQLAlchemy ORM models (TimescaleDB hypertables marked in comments)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
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
    operation_status_present: Mapped[bool | None] = mapped_column(Boolean)
    operation_status_valid: Mapped[bool | None] = mapped_column(Boolean)
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
    zone1_heat_min: Mapped[int | None] = mapped_column(Integer)
    zone1_heat_max: Mapped[int | None] = mapped_column(Integer)
    zone2_temp: Mapped[float | None] = mapped_column(Float)
    zone2_target_temp: Mapped[float | None] = mapped_column(Float)
    zone2_heat_min: Mapped[int | None] = mapped_column(Integer)
    zone2_heat_max: Mapped[int | None] = mapped_column(Integer)
    quiet_mode: Mapped[int | None] = mapped_column(Integer)
    powerful_mode: Mapped[int | None] = mapped_column(Integer)
    special_status: Mapped[int | None] = mapped_column(Integer)
    special_status_supported: Mapped[bool | None] = mapped_column(Boolean)
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
    native_policy_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SpaceHeatingGateRecord(Base):
    """Last authoritative room-heating eligibility evidence per device."""

    __tablename__ = "space_heating_gate"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    base_c: Mapped[float] = mapped_column(Float, nullable=False)
    on_threshold_c: Mapped[float] = mapped_column(Float, nullable=False)
    off_threshold_c: Mapped[float] = mapped_column(Float, nullable=False)
    last_raw_outdoor_c: Mapped[float | None] = mapped_column(Float)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_status_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    evaluated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    transitioned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_evaluation_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ConsumptionRecord(Base):
    """Energy consumption — hypertable on `ts`."""

    __tablename__ = "consumption"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_date: Mapped[dt.date | None] = mapped_column(Date)
    source_timezone: Mapped[str | None] = mapped_column(String(64))
    heat_kwh: Mapped[float | None] = mapped_column(Float)
    cool_kwh: Mapped[float | None] = mapped_column(Float)
    tank_kwh: Mapped[float | None] = mapped_column(Float)
    outdoor_temp: Mapped[float | None] = mapped_column(Float)
    heat_pump_outdoor_temp: Mapped[float | None] = mapped_column(Float)
    outdoor_temp_source: Mapped[str | None] = mapped_column(String(32))


class PriceScopeRecord(Base):
    """Immutable installation identity; prices remain in their original scope."""

    __tablename__ = "price_scopes"
    __table_args__ = (UniqueConstraint("provider", "home_id", name="uq_price_scope_identity"),)

    area: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    home_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_schema: Mapped[str] = mapped_column(String(64), nullable=False)
    tariff_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    application_extra_fee: Mapped[float] = mapped_column(Float, nullable=False)


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
    interval_schema: Mapped[str | None] = mapped_column(String(64))


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
    units_schema: Mapped[str | None] = mapped_column(String(64))


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
    contract_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("house_contract_revisions.id")
    )
    contract_hash: Mapped[str | None] = mapped_column(String(64))
    contract_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="legacy_unknown", server_default="legacy_unknown"
    )
    contract_invalidated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_by_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("house_contract_revisions.id")
    )
    contract_invalidation_reason: Mapped[str | None] = mapped_column(String(64))


class PlanActionRecord(Base):
    """Individual action within a plan."""

    __tablename__ = "plan_actions"
    __table_args__ = (
        Index("ix_plan_actions_plan_id", "plan_id"),
        Index("ix_plan_actions_status_scheduled", "status", "scheduled_ts"),
        Index(
            "uq_plan_actions_reverts_action_id",
            "reverts_action_id",
            unique=True,
            postgresql_where=text("reverts_action_id IS NOT NULL"),
        ),
        Index(
            "ix_plan_actions_safety_due",
            "safety_next_retry_at",
            "scheduled_ts",
            "id",
            postgresql_where=text("reverts_action_id IS NOT NULL AND status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reverts_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_actions.id", ondelete="RESTRICT")
    )
    scheduled_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    action_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    device_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expected_state_json: Mapped[str | None] = mapped_column(Text)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_observed_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    safety_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    safety_next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    safety_claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    safety_claim_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class ExecutorVerificationReadRecord(Base):
    """Durable reservation made before an executor verification read."""

    __tablename__ = "executor_verification_reads"
    __table_args__ = (
        Index("ix_executor_verification_reads_reserved_at", "reserved_at"),
        Index("ix_executor_verification_reads_lane_reserved_at", "lane", "reserved_at"),
        CheckConstraint(
            "lane IN ('ordinary', 'safety')", name="ck_executor_verification_reads_lane"
        ),
        CheckConstraint(
            "phase IN ('initial', 'redispatch', 'safety')",
            name="ck_executor_verification_reads_phase",
        ),
        CheckConstraint(
            "checkpoint_seconds IN (15, 60)",
            name="ck_executor_verification_reads_checkpoint",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reserved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("plan_actions.id", ondelete="RESTRICT"), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    checkpoint_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


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
    contract_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("house_contract_revisions.id")
    )
    contract_hash: Mapped[str | None] = mapped_column(String(64))


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


class HouseInstallationRecord(Base):
    """The single supported house identity for this deployment."""

    __tablename__ = "house_installations"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_house_installations_singleton"),
        CheckConstraint(
            "demand_dependency_generation >= 1",
            name="ck_house_installations_demand_generation",
        ),
        CheckConstraint(
            "indoor_dependency_generation >= 1",
            name="ck_house_installations_indoor_generation",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True
    )
    house_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    demand_dependency_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    indoor_dependency_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    current_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("house_contract_revisions.id")
    )


class HouseContractRevisionRecord(Base):
    """Append-only, canonical revision of the P2.1 planning contract."""

    __tablename__ = "house_contract_revisions"
    __table_args__ = (Index("ix_house_contract_revision_installation", "installation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    installation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("house_installations.installation_id"),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


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


manual_cop_revision_seq = Sequence(
    "manual_cop_revision_seq",
    start=1,
    minvalue=1,
    maxvalue=9223372036854775807,
    cycle=False,
    metadata=Base.metadata,
)


class ManualCOPObservationRecord(Base):
    """Retained manual controller estimates, unrelated to derived COP archives."""

    __tablename__ = "manual_cop_observations"
    __table_args__ = (
        CheckConstraint("granularity IN ('day', 'month')", name="ck_manual_cop_granularity"),
        CheckConstraint(
            "granularity <> 'month' OR EXTRACT(DAY FROM period_date) = 1",
            name="ck_manual_cop_month_start",
        ),
        CheckConstraint("timezone <> ''", name="ck_manual_cop_timezone"),
        CheckConstraint(
            "value > 0 AND value < 'Infinity'::double precision", name="ck_manual_cop_value"
        ),
        CheckConstraint("revision > 0", name="ck_manual_cop_revision"),
        CheckConstraint("observed_at <= CURRENT_TIMESTAMP", name="ck_manual_cop_observed_at"),
        CheckConstraint(
            "completeness IN ('partial', 'closed', 'unknown')",
            name="ck_manual_cop_completeness",
        ),
        CheckConstraint("created_at <= updated_at", name="ck_manual_cop_server_times"),
    )

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    granularity: Mapped[str] = mapped_column(String(5), primary_key=True)
    period_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completeness: Mapped[str] = mapped_column(String(7), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        manual_cop_revision_seq,
        nullable=False,
        server_default=manual_cop_revision_seq.next_value(),
    )


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
    __table_args__ = (
        Index("ix_shower_events_status", "status"),
        Index(
            "ix_shower_events_open_expiry",
            "expires_at",
            postgresql_where=text("status IN ('active', 'recovery_pending', 'timeout_pending')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    pre_shower_temp: Mapped[float] = mapped_column(Float, nullable=False)
    recovered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), default="active"
    )  # active / recovered / timeout / skipped_peak
    peak_price_skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    device_id: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activation_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_actions.id", ondelete="RESTRICT")
    )


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


ML_LIFECYCLE_TOKEN_SEQUENCE = Sequence(
    "ml_lifecycle_token_seq",
    metadata=Base.metadata,
    start=1,
    minvalue=1,
    maxvalue=9223372036854775807,
    cycle=False,
)


class MLLifecycleStateRecord(Base):
    """Installation authority; component references are the artifact roles below."""

    __tablename__ = "ml_lifecycle_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_ml_lifecycle_singleton"),
        CheckConstraint("revision > 0 AND fence > 0", name="ck_ml_lifecycle_tokens"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="ck_ml_lifecycle_lease"
        ),
        CheckConstraint("jsonb_typeof(checkpoints) = 'object'", name="ck_ml_lifecycle_checkpoints"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default="1")
    epoch: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, server_default=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(), nullable=False
    )
    fence: Mapped[int] = mapped_column(
        BigInteger, server_default=ML_LIFECYCLE_TOKEN_SEQUENCE.next_value(), nullable=False
    )
    maintenance_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default="false", nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_maintenance_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_demand_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoints: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class MLArtifactRecord(Base):
    __tablename__ = "ml_artifacts"
    __table_args__ = (
        CheckConstraint("artifact_hash ~ '^[0-9a-f]{64}$'", name="ck_ml_artifact_hash"),
        CheckConstraint("committed_revision > 0", name="ck_ml_artifact_revision"),
        CheckConstraint(
            "role IN ('candidate', 'incumbent', 'rollback', 'retained', 'unreferenced')",
            name="ck_ml_artifact_role",
        ),
        CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_ml_artifact_manifest"),
        CheckConstraint(
            "jsonb_typeof(qualification_scope) = 'object'", name="ck_ml_artifact_scope"
        ),
        CheckConstraint(
            "(qualified_at IS NULL AND qualification_expires_at IS NULL AND last_valid_outcome_at IS NULL) OR "
            "(qualified_at IS NOT NULL AND last_valid_outcome_at IS NOT NULL AND "
            "qualification_expires_at IS NOT NULL AND last_valid_outcome_at <= qualified_at AND "
            "qualified_at < qualification_expires_at AND "
            "qualification_expires_at <= last_valid_outcome_at + INTERVAL '72 hours')",
            name="ck_ml_artifact_qualification",
        ),
        UniqueConstraint("epoch", "component", "fit_id", name="uq_ml_artifact_fit"),
        Index(
            "uq_ml_artifact_candidate",
            "epoch",
            "component",
            unique=True,
            postgresql_where=text("role = 'candidate'"),
        ),
        Index(
            "uq_ml_artifact_incumbent",
            "epoch",
            "component",
            unique=True,
            postgresql_where=text("role = 'incumbent'"),
        ),
        Index("ix_ml_artifact_pruning", "role", "created_at", "retained_until"),
    )

    artifact_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    epoch: Mapped[str] = mapped_column(ForeignKey("ml_lifecycle_state.epoch"), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    committed_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    qualified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_valid_outcome_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
    retained_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class MLMaintenanceRequestRecord(Base):
    __tablename__ = "ml_maintenance_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ml_request_status",
        ),
        CheckConstraint("fence IS NULL OR fence > 0", name="ck_ml_request_fence"),
        UniqueConstraint("epoch", "component", "dedup_key", name="uq_ml_request_dedup"),
        Index(
            "uq_ml_request_running",
            "epoch",
            "component",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_ml_request_queue", "status", "requested_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch: Mapped[str] = mapped_column(ForeignKey("ml_lifecycle_state.epoch"), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="queued", nullable=False)
    fence: Mapped[int | None] = mapped_column(BigInteger)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    attempted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)


class MLForecastIssueRecord(Base):
    __tablename__ = "ml_forecast_issues"
    __table_args__ = (
        CheckConstraint("horizon_minutes > 0", name="ck_ml_issue_horizon"),
        CheckConstraint("issued_at < target_at", name="ck_ml_issue_times"),
        CheckConstraint(
            "issue_kind IN ('prospective_native', 'counterfactual', 'retrospective_actual_action')",
            name="ck_ml_issue_kind",
        ),
        CheckConstraint("snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_ml_issue_snapshot"),
        CheckConstraint(
            "incumbent_hash IS NOT NULL OR candidate_hash IS NOT NULL", name="ck_ml_issue_pair"
        ),
        UniqueConstraint(
            "epoch",
            "device_id",
            "component",
            "cohort_key",
            "horizon_minutes",
            name="uq_ml_issue_cohort",
        ),
        UniqueConstraint(
            "id", "epoch", "device_id", "component", "horizon_minutes", name="uq_ml_issue_identity"
        ),
        Index("ix_ml_issue_collection", "epoch", "component", "id"),
        Index("ix_ml_issue_target", "target_at", "id"),
        Index("ix_ml_issue_incumbent", "incumbent_hash"),
        Index("ix_ml_issue_candidate", "candidate_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    epoch: Mapped[str] = mapped_column(ForeignKey("ml_lifecycle_state.epoch"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(128), nullable=False)
    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_schema: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    device_day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    incumbent_hash: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.artifact_hash"))
    candidate_hash: Mapped[str | None] = mapped_column(ForeignKey("ml_artifacts.artifact_hash"))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    predictions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_horizons: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class MLForecastOutcomeRecord(Base):
    __tablename__ = "ml_forecast_outcomes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["issue_id", "epoch", "device_id", "component", "horizon_minutes"],
            [
                "ml_forecast_issues.id",
                "ml_forecast_issues.epoch",
                "ml_forecast_issues.device_id",
                "ml_forecast_issues.component",
                "ml_forecast_issues.horizon_minutes",
            ],
            name="fk_ml_outcome_issue",
        ),
        CheckConstraint(
            "status IN ('pending', 'valid', 'excluded', 'incomplete')", name="ck_ml_outcome_status"
        ),
        CheckConstraint(
            "status <> 'excluded' OR exclusion_reason IS NOT NULL", name="ck_ml_outcome_exclusion"
        ),
        UniqueConstraint(
            "epoch",
            "device_id",
            "component",
            "canonical_key",
            "horizon_minutes",
            name="uq_ml_outcome_canonical",
        ),
        Index("ix_ml_outcome_reconcile", "status", "updated_at", "issue_id"),
    )

    issue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    epoch: Mapped[str] = mapped_column(String(36), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

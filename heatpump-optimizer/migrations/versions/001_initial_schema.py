"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable TimescaleDB extension
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # Device status
    op.create_table(
        "device_status",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32)),
        sa.Column("operation_status", sa.Integer),
        sa.Column("outdoor_temp", sa.Float),
        sa.Column("tank_temp", sa.Float),
        sa.Column("tank_target_temp", sa.Integer),
        sa.Column("tank_operation_status", sa.Integer),
        sa.Column("zone1_temp", sa.Float),
        sa.Column("zone1_target_temp", sa.Float),
        sa.Column("zone2_temp", sa.Float),
        sa.Column("zone2_target_temp", sa.Float),
        sa.Column("quiet_mode", sa.Integer),
        sa.Column("powerful_mode", sa.Integer),
        sa.Column("special_status", sa.Integer),
        sa.PrimaryKeyConstraint("ts", "device_id"),
    )
    op.execute("SELECT create_hypertable('device_status', 'ts', if_not_exists => TRUE)")

    # Consumption
    op.create_table(
        "consumption",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("heat_kwh", sa.Float),
        sa.Column("cool_kwh", sa.Float),
        sa.Column("tank_kwh", sa.Float),
        sa.Column("outdoor_temp", sa.Float),
        sa.PrimaryKeyConstraint("ts", "device_id"),
    )
    op.execute("SELECT create_hypertable('consumption', 'ts', if_not_exists => TRUE)")

    # Prices
    op.create_table(
        "prices",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("area", sa.String(32), nullable=False),
        sa.Column("price_eur_per_kwh", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("ts", "area"),
    )
    op.execute("SELECT create_hypertable('prices', 'ts', if_not_exists => TRUE)")

    # Weather
    op.create_table(
        "weather",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source", sa.String(32), nullable=False, server_default="open-meteo"),
        sa.Column("temperature", sa.Float),
        sa.Column("irradiance", sa.Float),
        sa.Column("wind_speed", sa.Float),
        sa.Column("humidity", sa.Float),
        sa.PrimaryKeyConstraint("ts", "source"),
    )
    op.execute("SELECT create_hypertable('weather', 'ts', if_not_exists => TRUE)")

    # Plans
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("horizon_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_json", sa.Text, nullable=False),
        sa.Column("optimizer_version", sa.String(32), server_default="rules_v1"),
        sa.Column("cost_estimate_eur", sa.Float),
    )

    # Plan actions
    op.create_table(
        "plan_actions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer, nullable=False),
        sa.Column("scheduled_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), server_default="pending"),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", sa.Text),
    )

    # Overrides
    op.create_table(
        "overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text),
        sa.Column("reason", sa.String(256)),
        sa.Column("active", sa.Boolean, server_default="true"),
    )

    # Audit log
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_device", sa.String(128)),
        sa.Column("payload_json", sa.Text),
        sa.Column("result", sa.String(32)),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("overrides")
    op.drop_table("plan_actions")
    op.drop_table("plans")
    op.drop_table("weather")
    op.drop_table("prices")
    op.drop_table("consumption")
    op.drop_table("device_status")

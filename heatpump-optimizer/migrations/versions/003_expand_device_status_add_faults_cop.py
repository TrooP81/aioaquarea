"""Expand device_status with compressor fields; add faults and cop_history tables.

Revision ID: 003
Revises: 002
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Expand device_status table ---
    op.add_column("device_status", sa.Column("direction", sa.String(16)))
    op.add_column("device_status", sa.Column("pump_duty", sa.Integer()))
    op.add_column("device_status", sa.Column("device_action", sa.String(24)))
    op.add_column("device_status", sa.Column("defrost_active", sa.Boolean()))
    op.add_column("device_status", sa.Column("force_dhw", sa.Integer()))
    op.add_column("device_status", sa.Column("force_heater", sa.Integer()))
    op.add_column("device_status", sa.Column("holiday_mode", sa.Integer()))
    op.add_column("device_status", sa.Column("zone1_operation_status", sa.Integer()))
    op.add_column("device_status", sa.Column("zone2_operation_status", sa.Integer()))
    op.add_column("device_status", sa.Column("tank_heat_max", sa.Integer()))
    op.add_column("device_status", sa.Column("tank_heat_min", sa.Integer()))

    # --- Create faults table ---
    op.create_table(
        "faults",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=False),
        sa.Column("error_message", sa.String(256)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("outdoor_temp", sa.Float()),
    )
    op.create_index("ix_faults_device_ts", "faults", ["device_id", "ts"])

    # --- Create cop_history table ---
    op.create_table(
        "cop_history",
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True, server_default=sa.func.now()),
        sa.Column("device_id", sa.String(128), primary_key=True),
        sa.Column("cop_value", sa.Float()),
        sa.Column("mode", sa.String(24)),
        sa.Column("outdoor_temp", sa.Float()),
        sa.Column("electrical_kwh", sa.Float()),
        sa.Column("thermal_kwh", sa.Float()),
    )


def downgrade() -> None:
    op.drop_table("cop_history")
    op.drop_index("ix_faults_device_ts", table_name="faults")
    op.drop_table("faults")

    op.drop_column("device_status", "tank_heat_min")
    op.drop_column("device_status", "tank_heat_max")
    op.drop_column("device_status", "zone2_operation_status")
    op.drop_column("device_status", "zone1_operation_status")
    op.drop_column("device_status", "holiday_mode")
    op.drop_column("device_status", "force_heater")
    op.drop_column("device_status", "force_dhw")
    op.drop_column("device_status", "defrost_active")
    op.drop_column("device_status", "device_action")
    op.drop_column("device_status", "pump_duty")
    op.drop_column("device_status", "direction")

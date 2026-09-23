"""Persist stateful room-heating eligibility evidence.

Revision ID: 026
Revises: 025
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_heating_gate",
        sa.Column("device_id", sa.String(length=128), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_c", sa.Float(), nullable=False),
        sa.Column("on_threshold_c", sa.Float(), nullable=False),
        sa.Column("off_threshold_c", sa.Float(), nullable=False),
        sa.Column("last_raw_outdoor_c", sa.Float(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("source_status_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_evaluation_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("failure_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("space_heating_gate")

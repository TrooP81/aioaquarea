"""Create the durable executor verification-read admission ledger.

Revision ID: 030
Revises: 029
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executor_verification_reads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("lane", sa.String(length=16), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_seconds", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "lane IN ('ordinary', 'safety')", name="ck_executor_verification_reads_lane"
        ),
        sa.CheckConstraint(
            "phase IN ('initial', 'redispatch', 'safety')",
            name="ck_executor_verification_reads_phase",
        ),
        sa.CheckConstraint(
            "checkpoint_seconds IN (15, 60)",
            name="ck_executor_verification_reads_checkpoint",
        ),
        sa.ForeignKeyConstraint(["action_id"], ["plan_actions.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_executor_verification_reads_reserved_at",
        "executor_verification_reads",
        ["reserved_at"],
    )
    op.create_index(
        "ix_executor_verification_reads_lane_reserved_at",
        "executor_verification_reads",
        ["lane", "reserved_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_executor_verification_reads_lane_reserved_at",
        table_name="executor_verification_reads",
    )
    op.drop_index(
        "ix_executor_verification_reads_reserved_at", table_name="executor_verification_reads"
    )
    op.drop_table("executor_verification_reads")

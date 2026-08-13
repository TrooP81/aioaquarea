"""Add indoor_temp_reading table for SmartThings air temperature data.

Revision ID: 004
Revises: 003
Create Date: 2026-06-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "indoor_temp_reading",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("device_id", sa.String(100), nullable=False),
        sa.Column("device_label", sa.String(200)),
        sa.Column("room", sa.String(200)),
        sa.Column("temperature", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_indoor_temp_reading_timestamp",
        "indoor_temp_reading",
        ["timestamp"],
    )
    op.create_index(
        "ix_indoor_temp_reading_device_ts",
        "indoor_temp_reading",
        ["device_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_indoor_temp_reading_device_ts", table_name="indoor_temp_reading")
    op.drop_index("ix_indoor_temp_reading_timestamp", table_name="indoor_temp_reading")
    op.drop_table("indoor_temp_reading")

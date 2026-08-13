"""Add device_timestamp and is_stale columns to indoor_temp_reading.

Tracks when a reading is carried forward from a stale SmartThings report
and preserves the original device-reported timestamp.

Revision ID: 009
Revises: 008
Create Date: 2026-05-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "indoor_temp_reading",
        sa.Column("device_timestamp", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "indoor_temp_reading",
        sa.Column("is_stale", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("indoor_temp_reading", "is_stale")
    op.drop_column("indoor_temp_reading", "device_timestamp")

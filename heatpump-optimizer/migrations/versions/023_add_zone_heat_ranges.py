"""Persist Panasonic zone heat target ranges.

Revision ID: 023
Revises: 022
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for zone_id in (1, 2):
        op.add_column(
            "device_status",
            sa.Column(f"zone{zone_id}_heat_min", sa.Integer(), nullable=True),
        )
        op.add_column(
            "device_status",
            sa.Column(f"zone{zone_id}_heat_max", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for zone_id in (2, 1):
        op.drop_column("device_status", f"zone{zone_id}_heat_max")
        op.drop_column("device_status", f"zone{zone_id}_heat_min")

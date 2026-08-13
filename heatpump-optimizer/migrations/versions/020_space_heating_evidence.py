"""Persist conservative evidence for actual room-heating activity.

Revision ID: 020
Revises: 019
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("device_status", sa.Column("space_heating_active", sa.Boolean(), nullable=True))
    op.add_column(
        "device_status", sa.Column("space_heating_evidence", sa.String(length=48), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("device_status", "space_heating_evidence")
    op.drop_column("device_status", "space_heating_active")

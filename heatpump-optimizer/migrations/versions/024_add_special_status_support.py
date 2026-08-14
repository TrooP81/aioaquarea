"""Persist safe Panasonic special-status support.

Revision ID: 024
Revises: 023
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_status",
        sa.Column("special_status_supported", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_status", "special_status_supported")

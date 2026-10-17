"""Persist the planning status device identity on plan actions.

Revision ID: 027
Revises: 026
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plan_actions", sa.Column("device_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_actions", "device_id")

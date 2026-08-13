"""Add service heartbeat table for readiness checks.

Revision ID: 017
Revises: 016
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(32), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")

"""Widen plan_actions.status from VARCHAR(16) to VARCHAR(24).

The 'executed_unverified' status (21 chars) exceeded the old limit.

Revision ID: 008
Revises: 007
Create Date: 2026-05-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "plan_actions",
        "status",
        type_=sa.String(24),
        existing_type=sa.String(16),
    )


def downgrade() -> None:
    op.alter_column(
        "plan_actions",
        "status",
        type_=sa.String(16),
        existing_type=sa.String(24),
    )

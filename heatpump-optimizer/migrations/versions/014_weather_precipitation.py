"""add precipitation column to weather table

Revision ID: 014
Revises: 013
Create Date: 2026-07-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("weather", sa.Column("precipitation", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("weather", "precipitation")

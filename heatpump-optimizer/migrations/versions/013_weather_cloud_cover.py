"""add cloud_cover column to weather table

Revision ID: 013
Revises: 012
Create Date: 2026-07-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("weather", sa.Column("cloud_cover", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("weather", "cloud_cover")

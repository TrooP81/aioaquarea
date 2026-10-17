"""Persist the Panasonic local calendar day for consumption counters.

Revision ID: 031
Revises: 030
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("consumption", sa.Column("source_date", sa.Date(), nullable=True))
    op.execute(
        "UPDATE consumption "
        "SET source_date = (ts AT TIME ZONE 'UTC')::date "
        "WHERE source_date IS NULL"
    )
    # If restored-production timing makes this single transaction too slow, run
    # equivalent per-chunk batched UPDATE statements over show_chunks('consumption').


def downgrade() -> None:
    op.drop_column("consumption", "source_date")

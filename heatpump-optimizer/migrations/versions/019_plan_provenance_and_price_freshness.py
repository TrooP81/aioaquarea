"""Persist price retrieval time and plan-time input provenance.

Revision ID: 019
Revises: 018
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("prices", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "plans",
        sa.Column(
            "price_source",
            sa.String(length=16),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.add_column("plans", sa.Column("input_provenance_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "input_provenance_json")
    op.drop_column("plans", "price_source")
    op.drop_column("prices", "fetched_at")

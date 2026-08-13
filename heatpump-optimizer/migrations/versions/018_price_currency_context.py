"""Persist source and currency alongside price and plan amounts.

Revision ID: 018
Revises: 017
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prices",
        sa.Column("price_currency", sa.String(3), nullable=False, server_default="EUR"),
    )
    op.add_column(
        "prices",
        sa.Column("price_source", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "plans",
        sa.Column("price_currency", sa.String(3), nullable=False, server_default="EUR"),
    )


def downgrade() -> None:
    op.drop_column("plans", "price_currency")
    op.drop_column("prices", "price_source")
    op.drop_column("prices", "price_currency")

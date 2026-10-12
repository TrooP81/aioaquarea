"""Add nullable feed-unit and source-day provenance without rewriting history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("consumption", sa.Column("source_timezone", sa.String(64), nullable=True))
    op.add_column("weather", sa.Column("units_schema", sa.String(64), nullable=True))
    op.add_column("prices", sa.Column("interval_schema", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("prices", "interval_schema")
    op.drop_column("weather", "units_schema")
    op.drop_column("consumption", "source_timezone")

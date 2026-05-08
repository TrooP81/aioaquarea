"""add app_logs table for dashboard log viewer

Revision ID: 012
Revises: 011
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("logger_name", sa.String(128)),
        sa.Column("event", sa.String(256), nullable=False),
        sa.Column("details_json", sa.Text),
        sa.Column("service", sa.String(32), nullable=False),
    )
    op.create_index("ix_app_logs_ts", "app_logs", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_app_logs_ts", "app_logs")
    op.drop_table("app_logs")

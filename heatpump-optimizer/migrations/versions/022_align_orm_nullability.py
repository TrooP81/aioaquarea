"""Align database nullability with the ORM contract.

Revision ID: 022
Revises: 021
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TIMESTAMP_COLUMNS = (
    ("app_logs", "ts"),
    ("audit_log", "ts"),
    ("faults", "ts"),
    ("indoor_temp_reading", "timestamp"),
    ("plans", "created_at"),
    ("settings", "updated_at"),
    ("smartthings_oauth_token", "updated_at"),
)


def upgrade() -> None:
    for table_name, column_name in _TIMESTAMP_COLUMNS:
        op.execute(
            sa.text(
                f'UPDATE "{table_name}" SET "{column_name}" = now() WHERE "{column_name}" IS NULL'
            )
        )
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )

    replacements = (
        ("overrides", "active", "true"),
        ("plan_actions", "status", "'pending'"),
        ("plans", "optimizer_version", "'rules_v1'"),
        ("smartthings_oauth_token", "token_type", "'bearer'"),
    )
    for table_name, column_name, replacement in replacements:
        op.execute(
            sa.text(
                f'UPDATE "{table_name}" SET "{column_name}" = {replacement} '
                f'WHERE "{column_name}" IS NULL'
            )
        )

    op.alter_column("overrides", "active", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("plan_actions", "status", existing_type=sa.String(length=24), nullable=False)
    op.alter_column(
        "plans", "optimizer_version", existing_type=sa.String(length=32), nullable=False
    )
    op.alter_column(
        "smartthings_oauth_token",
        "token_type",
        existing_type=sa.String(length=32),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "smartthings_oauth_token",
        "token_type",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.alter_column("plans", "optimizer_version", existing_type=sa.String(length=32), nullable=True)
    op.alter_column("plan_actions", "status", existing_type=sa.String(length=24), nullable=True)
    op.alter_column("overrides", "active", existing_type=sa.Boolean(), nullable=True)
    for table_name, column_name in reversed(_TIMESTAMP_COLUMNS):
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )

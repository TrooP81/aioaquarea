"""Add action verification tracking columns.

Revision ID: 010
Revises: 009
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plan_actions", sa.Column("expected_state_json", sa.Text(), nullable=True))
    op.add_column(
        "plan_actions",
        sa.Column("verify_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("plan_actions", sa.Column("last_observed_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_actions", "last_observed_json")
    op.drop_column("plan_actions", "verify_attempts")
    op.drop_column("plan_actions", "expected_state_json")

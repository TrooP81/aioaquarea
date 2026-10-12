"""Add nullable restore attempt authority without changing legacy obligations.

Revision ID: 034
Revises: 033
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "plan_actions", sa.Column("safety_claim_token", UUID(as_uuid=True), nullable=True)
    )


def downgrade() -> None:
    unresolved = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM plan_actions WHERE reverts_action_id IS NOT NULL "
                "AND status IN ('pending', 'executing', 'dispatched')"
            )
        )
        .scalar_one()
    )
    if unresolved:
        raise RuntimeError(
            "cannot remove attempt fencing while unresolved safety obligations exist"
        )
    op.drop_column("plan_actions", "safety_claim_token")

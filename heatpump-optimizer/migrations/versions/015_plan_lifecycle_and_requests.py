"""Add atomic plan lifecycle and durable optimizer requests.

Revision ID: 015
Revises: 014
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
    )
    op.add_column("plans", sa.Column("status_reason", sa.String(256), nullable=True))
    op.add_column("plans", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("plans", sa.Column("superseded_by_plan_id", sa.Integer(), nullable=True))

    # Existing installations can have many overlapping historical plans.  The
    # newest still-valid one becomes active; all remaining rows are history.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (ORDER BY created_at DESC, id DESC) AS row_num
            FROM plans
            WHERE horizon_end > now()
        )
        UPDATE plans
        SET status = CASE WHEN ranked.row_num = 1 THEN 'active' ELSE 'superseded' END,
            status_reason = CASE WHEN ranked.row_num = 1 THEN NULL ELSE 'migration_superseded' END,
            superseded_at = CASE WHEN ranked.row_num = 1 THEN NULL ELSE now() END
        FROM ranked
        WHERE plans.id = ranked.id
        """
    )
    op.execute(
        "UPDATE plans SET status = 'completed' WHERE horizon_end <= now() AND status = 'active'"
    )
    op.create_index(
        "ux_plans_one_active",
        "plans",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "optimization_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False, server_default="api"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_optimization_requests_status_requested",
        "optimization_requests",
        ["status", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_optimization_requests_status_requested", table_name="optimization_requests")
    op.drop_table("optimization_requests")
    op.drop_index("ux_plans_one_active", table_name="plans")
    op.drop_column("plans", "superseded_by_plan_id")
    op.drop_column("plans", "superseded_at")
    op.drop_column("plans", "status_reason")
    op.drop_column("plans", "status")

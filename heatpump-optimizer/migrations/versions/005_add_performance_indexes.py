"""Add performance indexes for plan_actions, overrides, and faults.

Revision ID: 005
Create Date: 2026-05-01
"""

from alembic import op


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_plan_actions_plan_id", "plan_actions", ["plan_id"])
    op.create_index("ix_plan_actions_status_scheduled", "plan_actions", ["status", "scheduled_ts"])
    op.create_index("ix_overrides_active_ts", "overrides", ["active", "ts_from", "ts_to"])
    op.create_index("ix_faults_device_resolved", "faults", ["device_id", "resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_faults_device_resolved", table_name="faults")
    op.drop_index("ix_overrides_active_ts", table_name="overrides")
    op.drop_index("ix_plan_actions_status_scheduled", table_name="plan_actions")
    op.drop_index("ix_plan_actions_plan_id", table_name="plan_actions")

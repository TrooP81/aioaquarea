"""Backfill unambiguous component-based room-heating evidence.

Revision ID: 028
Revises: 027
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_status", sa.Column("operation_status_present", sa.Boolean(), nullable=True)
    )
    op.add_column("device_status", sa.Column("operation_status_valid", sa.Boolean(), nullable=True))
    op.execute(
        """
        UPDATE device_status
        SET space_heating_active = TRUE,
            space_heating_evidence = 'component_space_heating'
        WHERE space_heating_active IS NOT TRUE
          AND mode IN ('1', '3')
          AND direction = 'PUMP'
          AND pump_duty = 1
                    AND COALESCE(device_action, '') NOT IN ('HEATING_WATER', 'COOLING', 'IDLE')
          AND COALESCE(defrost_active, FALSE) = FALSE
          AND (zone1_operation_status = 1 OR zone2_operation_status = 1)
        """
    )


def downgrade() -> None:
    op.drop_column("device_status", "operation_status_valid")
    op.drop_column("device_status", "operation_status_present")

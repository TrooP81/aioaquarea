"""Preserve the pump sensor while weather becomes the effective outdoor input.

Revision ID: 021
Revises: 020
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_status",
        sa.Column("heat_pump_outdoor_temp", sa.Float(), nullable=True),
    )
    op.add_column(
        "device_status",
        sa.Column("outdoor_temp_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "consumption",
        sa.Column("heat_pump_outdoor_temp", sa.Float(), nullable=True),
    )
    op.add_column(
        "consumption",
        sa.Column("outdoor_temp_source", sa.String(length=32), nullable=True),
    )

    # Existing values came from the physical pump sensor. Preserve that fact;
    # historical ML joins prefer archived weather rows after this migration.
    op.execute(
        """
        UPDATE device_status
        SET heat_pump_outdoor_temp = outdoor_temp,
            outdoor_temp_source = 'heat_pump_legacy'
        WHERE heat_pump_outdoor_temp IS NULL
        """
    )
    op.execute(
        """
        UPDATE consumption
        SET heat_pump_outdoor_temp = outdoor_temp,
            outdoor_temp_source = 'heat_pump_legacy'
        WHERE heat_pump_outdoor_temp IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("consumption", "outdoor_temp_source")
    op.drop_column("consumption", "heat_pump_outdoor_temp")
    op.drop_column("device_status", "outdoor_temp_source")
    op.drop_column("device_status", "heat_pump_outdoor_temp")

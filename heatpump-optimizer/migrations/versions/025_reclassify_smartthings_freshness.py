"""Reclassify SmartThings readings using the battery-sensor freshness default.

Revision ID: 025
Revises: 024
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _reclassify(max_age_minutes: int) -> None:
    op.get_bind().execute(
        text(
            """
            UPDATE indoor_temp_reading
            SET is_stale = CASE
                WHEN device_timestamp IS NULL THEN TRUE
                WHEN timestamp - device_timestamp > make_interval(mins => :max_age_minutes)
                    THEN TRUE
                ELSE FALSE
            END
            """
        ),
        {"max_age_minutes": max_age_minutes},
    )


def upgrade() -> None:
    _reclassify(180)


def downgrade() -> None:
    _reclassify(30)

"""Store selected weather source and forecast issuance time.

Revision ID: 016
Revises: 015
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "weather", sa.Column("forecast_issued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_weather_source_issued", "weather", ["source", "forecast_issued_at"])


def downgrade() -> None:
    op.drop_index("ix_weather_source_issued", table_name="weather")
    op.drop_column("weather", "forecast_issued_at")

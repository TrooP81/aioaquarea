"""Add data retention policy for hypertables.

Revision ID: 006
Create Date: 2026-05-01

Adds a TimescaleDB retention policy to automatically drop chunks older than
90 days for device_status and weather, 365 days for prices and consumption.
Falls back gracefully if TimescaleDB is not available.
"""

from alembic import op
from sqlalchemy import text


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Check if TimescaleDB is available
    result = conn.execute(text(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
    ))
    has_timescale = result.scalar()

    if not has_timescale:
        return

    # Only apply retention to tables that are actually hypertables
    policies = [
        ("device_status", "90 days"),
        ("weather", "90 days"),
        ("indoor_temp_reading", "90 days"),
        ("prices", "365 days"),
        ("consumption", "365 days"),
    ]

    for table, interval in policies:
        # Check if the table is a hypertable before adding a policy;
        # a failed SQL in PostgreSQL aborts the whole transaction.
        is_hypertable = conn.execute(text(
            "SELECT EXISTS("
            "  SELECT 1 FROM timescaledb_information.hypertables"
            "  WHERE hypertable_name = :tbl"
            ")"
        ), {"tbl": table}).scalar()

        if is_hypertable:
            conn.execute(text(
                f"SELECT add_retention_policy('{table}', INTERVAL '{interval}', if_not_exists => true)"
            ))


def downgrade() -> None:
    conn = op.get_bind()

    result = conn.execute(text(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
    ))
    if not result.scalar():
        return

    for table in ("device_status", "weather", "indoor_temp_reading", "prices", "consumption"):
        is_hypertable = conn.execute(text(
            "SELECT EXISTS("
            "  SELECT 1 FROM timescaledb_information.hypertables"
            "  WHERE hypertable_name = :tbl"
            ")"
        ), {"tbl": table}).scalar()

        if is_hypertable:
            conn.execute(text(f"SELECT remove_retention_policy('{table}', if_exists => true)"))

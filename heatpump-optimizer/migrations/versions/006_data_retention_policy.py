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

    # Add retention policies (idempotent — will raise if already exists, which is fine)
    policies = [
        ("device_status", "90 days"),
        ("weather", "90 days"),
        ("indoor_temp_readings", "90 days"),
        ("prices", "365 days"),
        ("consumption", "365 days"),
    ]

    for table, interval in policies:
        try:
            conn.execute(text(
                f"SELECT add_retention_policy('{table}', INTERVAL '{interval}', if_not_exists => true)"
            ))
        except Exception:
            # Table might not be a hypertable
            pass


def downgrade() -> None:
    conn = op.get_bind()

    result = conn.execute(text(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
    ))
    if not result.scalar():
        return

    for table in ("device_status", "weather", "indoor_temp_readings", "prices", "consumption"):
        try:
            conn.execute(text(f"SELECT remove_retention_policy('{table}', if_exists => true)"))
        except Exception:
            pass

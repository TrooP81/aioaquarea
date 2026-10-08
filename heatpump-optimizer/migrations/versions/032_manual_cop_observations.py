"""Add the retained manual controller-COP ledger and non-cycling revision sequence.

Calendar midnights use the earliest round-trip match among whole-second offsets
strictly within 24 hours, matching the application's ZoneInfo datetime contract.
Timezone keys must exactly match the IANA catalog, excluding PostgreSQL pseudozones.
The calendar helper is retained alongside the ledger and sequence.

Revision ID: 032
Revises: 031
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.manual_cop_calendar_midnight(
            local_midnight timestamp without time zone, timezone_name text
        ) RETURNS timestamp with time zone
        LANGUAGE sql STABLE STRICT PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT CASE WHEN (local_midnight AT TIME ZONE timezone_name) IS NOT NULL THEN
                CASE WHEN EXISTS (
                    SELECT 1 FROM pg_timezone_names
                    WHERE name COLLATE "C" = timezone_name COLLATE "C"
                        AND name !~ '^(posix|right)/'
                        AND name NOT IN ('localtime', 'posixrules')
                ) THEN (
                    SELECT min(instant)
                    FROM generate_series(
                        (local_midnight AT TIME ZONE 'UTC') - INTERVAL '86399 seconds',
                        (local_midnight AT TIME ZONE 'UTC') + INTERVAL '86399 seconds',
                        INTERVAL '1 second'
                    ) AS candidates(instant)
                    WHERE (instant AT TIME ZONE timezone_name) = local_midnight
                ) END
            END
        $$
        """
    )
    op.execute(
        sa.schema.CreateSequence(
            sa.Sequence(
                "manual_cop_revision_seq",
                start=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
            )
        )
    )
    op.create_table(
        "manual_cop_observations",
        sa.Column("device_id", sa.String(128), primary_key=True),
        sa.Column("granularity", sa.String(5), primary_key=True),
        sa.Column("period_date", sa.Date(), primary_key=True),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completeness", sa.String(7), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('manual_cop_revision_seq'::regclass)"),
        ),
        sa.CheckConstraint("granularity IN ('day', 'month')", name="ck_manual_cop_granularity"),
        sa.CheckConstraint(
            "granularity <> 'month' OR EXTRACT(DAY FROM period_date) = 1",
            name="ck_manual_cop_month_start",
        ),
        sa.CheckConstraint("timezone <> ''", name="ck_manual_cop_timezone"),
        sa.CheckConstraint(
            "value > 0 AND value < 'Infinity'::double precision", name="ck_manual_cop_value"
        ),
        sa.CheckConstraint("revision > 0", name="ck_manual_cop_revision"),
        sa.CheckConstraint("period_start < period_end", name="ck_manual_cop_period_order"),
        sa.CheckConstraint(
            "period_start IS NOT DISTINCT FROM public.manual_cop_calendar_midnight("
            "period_date::timestamp, timezone) AND "
            "period_end IS NOT DISTINCT FROM public.manual_cop_calendar_midnight("
            "CASE WHEN granularity = 'day' "
            "THEN period_date::timestamp + INTERVAL '1 day' "
            "ELSE period_date::timestamp + INTERVAL '1 month' END, timezone)",
            name="ck_manual_cop_calendar",
        ),
        sa.CheckConstraint(
            "observed_at >= period_start AND observed_at <= CURRENT_TIMESTAMP",
            name="ck_manual_cop_observed_at",
        ),
        sa.CheckConstraint(
            "completeness IN ('partial', 'closed', 'unknown') AND "
            "(completeness <> 'closed' OR observed_at >= period_end)",
            name="ck_manual_cop_completeness",
        ),
        sa.CheckConstraint("created_at <= updated_at", name="ck_manual_cop_server_times"),
    )


def downgrade() -> None:
    raise RuntimeError("manual_cop_retained: disable forecasting; retain ledger and sequence")

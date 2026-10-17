"""Store manual COP periods as local calendar identities, not SQL-derived instants.

Calendar boundaries are derived by the application from ``period_date``,
``granularity`` and the confirmed IANA ``timezone``. This forward-only revision
removes the persisted boundary columns, their exhaustive SQL calendar checks and
the ``public.manual_cop_calendar_midnight`` helper. Ledger rows, actual UTC
timestamps and the non-cycling revision sequence are retained unchanged.
"""

from __future__ import annotations

from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '20s'")
    for name in (
        "ck_manual_cop_calendar",
        "ck_manual_cop_period_order",
        "ck_manual_cop_observed_at",
        "ck_manual_cop_completeness",
    ):
        op.drop_constraint(name, "manual_cop_observations", type_="check")
    op.drop_column("manual_cop_observations", "period_end")
    op.drop_column("manual_cop_observations", "period_start")
    op.execute(
        "DROP FUNCTION public.manual_cop_calendar_midnight("
        "timestamp without time zone, text) RESTRICT"
    )
    op.create_check_constraint(
        "ck_manual_cop_observed_at",
        "manual_cop_observations",
        "observed_at <= CURRENT_TIMESTAMP",
    )
    op.create_check_constraint(
        "ck_manual_cop_completeness",
        "manual_cop_observations",
        "completeness IN ('partial', 'closed', 'unknown')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "manual_cop_local_periods_forward_only: 036 removed stored calendar boundaries; "
        "repair forward instead of downgrading"
    )

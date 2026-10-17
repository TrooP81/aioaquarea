"""Add immutable Tibber identity registry without altering price hypertables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "price_scopes",
        sa.Column("area", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("home_id", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_basis", sa.String(32), nullable=False),
        sa.Column("interval_schema", sa.String(64), nullable=False),
        sa.Column("tariff_schema_version", sa.String(32), nullable=False),
        sa.Column("application_extra_fee", sa.Float(), nullable=False),
        sa.UniqueConstraint("provider", "home_id", name="uq_price_scope_identity"),
    )
    op.execute("""
        CREATE FUNCTION reject_price_scope_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'price scope mappings are immutable';
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER price_scopes_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON price_scopes FOR EACH STATEMENT EXECUTE FUNCTION reject_price_scope_mutation()
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("price_scopes")
    op.execute("DROP FUNCTION reject_price_scope_mutation()")

"""Additive P2.1 house-contract revision and plan freshness foundation."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "house_installations",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("house_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "demand_dependency_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_house_installations_singleton"),
        sa.CheckConstraint(
            "demand_dependency_generation >= 1",
            name="ck_house_installations_demand_generation",
        ),
    )
    op.create_table(
        "house_contract_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("house_installations.installation_id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("contract_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
    )
    op.create_index(
        "ix_house_contract_revision_installation",
        "house_contract_revisions",
        ["installation_id", "id"],
    )
    op.add_column(
        "house_installations",
        sa.Column(
            "current_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("house_contract_revisions.id"),
            nullable=True,
        ),
    )

    op.add_column(
        "plans",
        sa.Column(
            "contract_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("house_contract_revisions.id"),
            nullable=True,
        ),
    )
    op.add_column("plans", sa.Column("contract_hash", sa.String(length=64), nullable=True))
    op.add_column("plans", sa.Column("contract_state", sa.String(length=24), nullable=True))
    op.execute("UPDATE plans SET contract_state = 'legacy_unknown' WHERE contract_state IS NULL")
    op.alter_column(
        "plans",
        "contract_state",
        nullable=False,
        server_default="legacy_unknown",
    )
    op.add_column(
        "plans",
        sa.Column("contract_invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plans",
        sa.Column(
            "invalidated_by_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("house_contract_revisions.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "plans", sa.Column("contract_invalidation_reason", sa.String(length=64), nullable=True)
    )

    op.add_column(
        "optimization_requests",
        sa.Column(
            "contract_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("house_contract_revisions.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "optimization_requests", sa.Column("contract_hash", sa.String(length=64), nullable=True)
    )

    op.execute("""
        CREATE FUNCTION reject_house_contract_revision_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'house contract revisions are immutable';
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER house_contract_revisions_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON house_contract_revisions
        FOR EACH STATEMENT EXECUTE FUNCTION reject_house_contract_revision_mutation()
    """)
    op.execute("""
        CREATE FUNCTION reject_revision_bound_plan_snapshot_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.contract_revision_id IS NOT NULL
               AND OLD.plan_json IS DISTINCT FROM NEW.plan_json THEN
                RAISE EXCEPTION 'revision-bound plan snapshots are immutable';
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER revision_bound_plan_snapshot_immutable
        BEFORE UPDATE OF plan_json ON plans
        FOR EACH ROW EXECUTE FUNCTION reject_revision_bound_plan_snapshot_mutation()
    """)


def downgrade() -> None:
    raise RuntimeError(
        "P2.1 contract revisions and plan history are intentionally not destructively downgraded; "
        "roll back by disabling v2 consumers and retaining additive schema."
    )

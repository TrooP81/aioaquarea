"""Add observe-only ML lifecycle authority and retained prospective evidence.

Revision ID: 033
Revises: 032
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None

_TABLES = (
    "ml_forecast_outcomes",
    "ml_forecast_issues",
    "ml_maintenance_requests",
    "ml_artifacts",
    "ml_lifecycle_state",
)


def upgrade() -> None:
    op.add_column("device_status", sa.Column("native_policy_provenance", JSONB(), nullable=True))
    op.execute(
        sa.schema.CreateSequence(
            sa.Sequence(
                "ml_lifecycle_token_seq",
                start=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
            )
        )
    )
    op.create_table(
        "ml_lifecycle_state",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("epoch", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('ml_lifecycle_token_seq')"),
        ),
        sa.Column(
            "fence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('ml_lifecycle_token_seq')"),
        ),
        sa.Column("maintenance_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_maintenance_at", sa.DateTime(timezone=True)),
        sa.Column("last_demand_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoints", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("id = 1", name="ck_ml_lifecycle_singleton"),
        sa.CheckConstraint("revision > 0 AND fence > 0", name="ck_ml_lifecycle_tokens"),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="ck_ml_lifecycle_lease"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(checkpoints) = 'object'", name="ck_ml_lifecycle_checkpoints"
        ),
    )
    op.create_table(
        "ml_artifacts",
        sa.Column("artifact_hash", sa.String(64), primary_key=True),
        sa.Column(
            "epoch", sa.String(36), sa.ForeignKey("ml_lifecycle_state.epoch"), nullable=False
        ),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("fit_id", sa.String(128), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("committed_revision", sa.BigInteger(), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_valid_outcome_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_scope", JSONB(), nullable=False, server_default="{}"),
        sa.Column("retained_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("artifact_hash ~ '^[0-9a-f]{64}$'", name="ck_ml_artifact_hash"),
        sa.CheckConstraint("committed_revision > 0", name="ck_ml_artifact_revision"),
        sa.CheckConstraint(
            "role IN ('candidate', 'incumbent', 'rollback', 'retained', 'unreferenced')",
            name="ck_ml_artifact_role",
        ),
        sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="ck_ml_artifact_manifest"),
        sa.CheckConstraint(
            "jsonb_typeof(qualification_scope) = 'object'", name="ck_ml_artifact_scope"
        ),
        sa.CheckConstraint(
            "(qualified_at IS NULL AND qualification_expires_at IS NULL AND last_valid_outcome_at IS NULL) OR "
            "(qualified_at IS NOT NULL AND last_valid_outcome_at IS NOT NULL AND "
            "qualification_expires_at IS NOT NULL AND last_valid_outcome_at <= qualified_at AND "
            "qualified_at < qualification_expires_at AND "
            "qualification_expires_at <= last_valid_outcome_at + INTERVAL '72 hours')",
            name="ck_ml_artifact_qualification",
        ),
        sa.UniqueConstraint("epoch", "component", "fit_id", name="uq_ml_artifact_fit"),
    )
    for role in ("candidate", "incumbent"):
        op.create_index(
            f"uq_ml_artifact_{role}",
            "ml_artifacts",
            ["epoch", "component"],
            unique=True,
            postgresql_where=sa.text(f"role = '{role}'"),
        )
    op.create_index(
        "ix_ml_artifact_pruning", "ml_artifacts", ["role", "created_at", "retained_until"]
    )
    op.create_table(
        "ml_maintenance_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "epoch", sa.String(36), sa.ForeignKey("ml_lifecycle_state.epoch"), nullable=False
        ),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("fence", sa.BigInteger()),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("details", JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ml_request_status",
        ),
        sa.CheckConstraint("fence IS NULL OR fence > 0", name="ck_ml_request_fence"),
        sa.UniqueConstraint("epoch", "component", "dedup_key", name="uq_ml_request_dedup"),
    )
    op.create_index(
        "uq_ml_request_running",
        "ml_maintenance_requests",
        ["epoch", "component"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_ml_request_queue", "ml_maintenance_requests", ["status", "requested_at"])
    op.create_table(
        "ml_forecast_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "epoch", sa.String(36), sa.ForeignKey("ml_lifecycle_state.epoch"), nullable=False
        ),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("cohort_key", sa.String(128), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("issue_kind", sa.String(32), nullable=False),
        sa.Column("policy_schema", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_day", sa.Date(), nullable=False),
        sa.Column("incumbent_hash", sa.String(64), sa.ForeignKey("ml_artifacts.artifact_hash")),
        sa.Column("candidate_hash", sa.String(64), sa.ForeignKey("ml_artifacts.artifact_hash")),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("input_snapshot", JSONB(), nullable=False),
        sa.Column("predictions", JSONB(), nullable=False),
        sa.Column("expected_horizons", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("horizon_minutes > 0", name="ck_ml_issue_horizon"),
        sa.CheckConstraint("issued_at < target_at", name="ck_ml_issue_times"),
        sa.CheckConstraint(
            "issue_kind IN ('prospective_native', 'counterfactual', 'retrospective_actual_action')",
            name="ck_ml_issue_kind",
        ),
        sa.CheckConstraint("snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_ml_issue_snapshot"),
        sa.CheckConstraint(
            "incumbent_hash IS NOT NULL OR candidate_hash IS NOT NULL", name="ck_ml_issue_pair"
        ),
        sa.UniqueConstraint(
            "epoch",
            "device_id",
            "component",
            "cohort_key",
            "horizon_minutes",
            name="uq_ml_issue_cohort",
        ),
        sa.UniqueConstraint(
            "id", "epoch", "device_id", "component", "horizon_minutes", name="uq_ml_issue_identity"
        ),
    )
    for name, columns in (
        ("collection", ["epoch", "component", "id"]),
        ("target", ["target_at", "id"]),
        ("incumbent", ["incumbent_hash"]),
        ("candidate", ["candidate_hash"]),
    ):
        op.create_index(f"ix_ml_issue_{name}", "ml_forecast_issues", columns)
    op.create_table(
        "ml_forecast_outcomes",
        sa.Column("issue_id", sa.BigInteger(), primary_key=True),
        sa.Column("epoch", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("canonical_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("exclusion_reason", sa.String(128)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["issue_id", "epoch", "device_id", "component", "horizon_minutes"],
            [
                "ml_forecast_issues.id",
                "ml_forecast_issues.epoch",
                "ml_forecast_issues.device_id",
                "ml_forecast_issues.component",
                "ml_forecast_issues.horizon_minutes",
            ],
            name="fk_ml_outcome_issue",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'valid', 'excluded', 'incomplete')", name="ck_ml_outcome_status"
        ),
        sa.CheckConstraint(
            "status <> 'excluded' OR exclusion_reason IS NOT NULL", name="ck_ml_outcome_exclusion"
        ),
        sa.UniqueConstraint(
            "epoch",
            "device_id",
            "component",
            "canonical_key",
            "horizon_minutes",
            name="uq_ml_outcome_canonical",
        ),
    )
    op.create_index(
        "ix_ml_outcome_reconcile", "ml_forecast_outcomes", ["status", "updated_at", "issue_id"]
    )
    op.execute(
        sa.text(
            "CREATE FUNCTION ml_forecast_issue_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'ml_forecast_issue_immutable'; END; $$"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER ml_forecast_issue_immutable BEFORE UPDATE ON ml_forecast_issues "
            "FOR EACH ROW EXECUTE FUNCTION ml_forecast_issue_immutable()"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    populated = connection.execute(
        sa.text("SELECT " + " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in _TABLES))
    ).scalar_one()
    tokens_issued = connection.execute(
        sa.text("SELECT is_called FROM ml_lifecycle_token_seq")
    ).scalar_one()
    if populated or tokens_issued:
        raise RuntimeError(
            "ml_lifecycle_retained: populated storage or issued tokens cannot be downgraded"
        )
    for table in _TABLES:
        op.drop_table(table)
    op.execute(sa.text("DROP FUNCTION ml_forecast_issue_immutable()"))
    op.drop_column("device_status", "native_policy_provenance")
    op.execute(sa.schema.DropSequence(sa.Sequence("ml_lifecycle_token_seq")))

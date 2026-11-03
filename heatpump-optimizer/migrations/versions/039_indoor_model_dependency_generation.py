"""Track indoor sensor dependency changes without revoking demand or tank fits."""

from alembic import op
import sqlalchemy as sa

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "house_installations",
        sa.Column(
            "indoor_dependency_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_house_installations_indoor_generation",
        "house_installations",
        "indoor_dependency_generation >= 1",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Indoor model dependency history is intentionally not destructively downgraded."
    )

"""Persist linked safety-revert obligations and wall-clock shower expiry.

Operator: Cold starts are ordered automatically; for a running-stack upgrade run `docker compose stop optimizer poller` before `docker compose up -d`, and on a migrate lock timeout stop writers and retry.

Revision ID: 029
Revises: 028
Create Date: 2026-09-25
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPEN_SHOWER_STATUSES = ("active", "recovery_pending", "timeout_pending")
_UNRESOLVED_STATUSES = ("pending", "executing", "dispatched")
logger = logging.getLogger("alembic.runtime.migration")


def _shower_duration_minutes(connection) -> int:
    value = connection.execute(
        sa.text("SELECT value FROM settings WHERE key = 'shower_max_duration_minutes'")
    ).scalar_one_or_none()
    if value is None or not str(value).strip():
        return 60
    try:
        duration = int(str(value).strip())
    except ValueError as exc:
        raise RuntimeError("shower_max_duration_minutes must be a positive integer") from exc
    if duration <= 0:
        raise RuntimeError("shower_max_duration_minutes must be positive")
    return duration


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    logger.info(
        "migration_029_row_counts plan_actions=%s shower_events=%s",
        connection.execute(sa.text("SELECT count(*) FROM plan_actions")).scalar_one(),
        connection.execute(sa.text("SELECT count(*) FROM shower_events")).scalar_one(),
    )
    duration_minutes = _shower_duration_minutes(connection)

    op.alter_column(
        "shower_events",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.add_column("plan_actions", sa.Column("reverts_action_id", sa.Integer(), nullable=True))
    op.add_column(
        "plan_actions", sa.Column("safety_attempt_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("plan_actions", sa.Column("safety_next_retry_at", sa.DateTime(timezone=True)))
    op.add_column("plan_actions", sa.Column("safety_claimed_at", sa.DateTime(timezone=True)))
    # Non-concurrent index/FK creation is intentional for atomicity and sub-second at tens of thousands of rows.
    op.create_foreign_key(
        "fk_plan_actions_reverts_action_id",
        "plan_actions",
        "plan_actions",
        ["reverts_action_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_plan_actions_reverts_action_id",
        "plan_actions",
        ["reverts_action_id"],
        unique=True,
        postgresql_where=sa.text("reverts_action_id IS NOT NULL"),
    )
    op.create_index(
        "ix_plan_actions_safety_due",
        "plan_actions",
        ["safety_next_retry_at", "scheduled_ts", "id"],
        postgresql_where=sa.text("reverts_action_id IS NOT NULL AND status = 'pending'"),
    )

    op.add_column("shower_events", sa.Column("device_id", sa.String(length=128), nullable=True))
    op.add_column("shower_events", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shower_events", sa.Column("activation_action_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_shower_events_activation_action_id",
        "shower_events",
        "plan_actions",
        ["activation_action_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    connection.execute(
        sa.text(
            "UPDATE shower_events SET expires_at = started_at + "
            "(:duration_minutes * INTERVAL '1 minute')"
        ),
        {"duration_minutes": duration_minutes},
    )
    op.alter_column("shower_events", "expires_at", nullable=False)

    active_events = connection.execute(
        sa.text("SELECT id, started_at, expires_at FROM shower_events WHERE status = 'active'")
    ).mappings()
    for event in active_events:
        device_rows = connection.execute(
            sa.text("SELECT device_id FROM device_status WHERE ts = :started_at"),
            {"started_at": event["started_at"]},
        ).scalars().all()
        if len(device_rows) != 1:
            raise RuntimeError(
                f"cannot infer unique device_id for legacy shower event {event['id']}"
            )
        now = dt.datetime.now(dt.timezone.utc)
        plan_id = connection.execute(
            sa.text(
                "INSERT INTO plans (horizon_start, horizon_end, plan_json, optimizer_version, status, "
                "created_at) VALUES (:start, :end, :plan_json, 'migration_029_legacy_shower', "
                "'superseded', :created_at) RETURNING id"
            ),
            {
                "start": event["started_at"],
                "end": event["expires_at"],
                "plan_json": json.dumps({"migration": "029", "event_id": event["id"]}),
                "created_at": now,
            },
        ).scalar_one()
        source_id = connection.execute(
            sa.text(
                "INSERT INTO plan_actions (plan_id, scheduled_ts, action_type, payload_json, device_id, "
                "status, executed_at, result_json) VALUES (:plan_id, :scheduled_ts, 'force_dhw_on', "
                ":payload_json, :device_id, 'executed', :executed_at, :result_json) RETURNING id"
            ),
            {
                "plan_id": plan_id,
                "scheduled_ts": event["started_at"],
                "payload_json": json.dumps({"trigger": "migration_029_legacy_shower"}),
                "device_id": device_rows[0],
                "executed_at": event["started_at"],
                "result_json": json.dumps({"reason": "migration_029_legacy_shower"}),
            },
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO plan_actions (plan_id, reverts_action_id, scheduled_ts, action_type, "
                "payload_json, device_id, status) VALUES (:plan_id, :source_id, :scheduled_ts, "
                "'force_dhw_off', :payload_json, :device_id, 'pending')"
            ),
            {
                "plan_id": plan_id,
                "source_id": source_id,
                "scheduled_ts": event["expires_at"],
                "payload_json": json.dumps({"trigger": "migration_029_legacy_shower"}),
                "device_id": device_rows[0],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE shower_events SET device_id = :device_id, activation_action_id = :source_id "
                "WHERE id = :event_id"
            ),
            {"device_id": device_rows[0], "source_id": source_id, "event_id": event["id"]},
        )

    op.create_index(
        "ix_shower_events_open_expiry",
        "shower_events",
        ["expires_at"],
        postgresql_where=sa.text(
            "status IN ('active', 'recovery_pending', 'timeout_pending')"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    unresolved = connection.execute(
        sa.text(
            "SELECT count(*) FROM plan_actions WHERE reverts_action_id IS NOT NULL "
            "AND status IN ('pending', 'executing', 'dispatched')"
        )
    ).scalar_one()
    if unresolved:
        raise RuntimeError("cannot downgrade while unresolved safety obligations exist")

    connection.execute(
        sa.text(
            "UPDATE shower_events SET status = 'skipped_revert' "
            "WHERE status = 'skipped_unresolved_revert'"
        )
    )
    op.alter_column(
        "shower_events",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.drop_index("ix_shower_events_open_expiry", table_name="shower_events")
    op.drop_constraint("fk_shower_events_activation_action_id", "shower_events", type_="foreignkey")
    op.drop_column("shower_events", "activation_action_id")
    op.drop_column("shower_events", "expires_at")
    op.drop_column("shower_events", "device_id")
    op.drop_index("ix_plan_actions_safety_due", table_name="plan_actions")
    op.drop_index("uq_plan_actions_reverts_action_id", table_name="plan_actions")
    op.drop_constraint("fk_plan_actions_reverts_action_id", "plan_actions", type_="foreignkey")
    op.drop_column("plan_actions", "safety_next_retry_at")
    op.drop_column("plan_actions", "safety_claimed_at")
    op.drop_column("plan_actions", "safety_attempt_count")
    op.drop_column("plan_actions", "reverts_action_id")
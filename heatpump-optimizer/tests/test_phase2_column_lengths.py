from __future__ import annotations

from sqlalchemy import Text

from packages.core.models import (
    AuditLogRecord,
    PlanActionRecord,
    PlanRecord,
    ServiceHeartbeatRecord,
    ShowerEventRecord,
)


def _column_length(model, column_name: str) -> int | None:
    column_type = model.__table__.c[column_name].type
    assert not isinstance(column_type, Text)
    return column_type.length


def test_phase2_persisted_values_fit_model_columns():
    values = {
        ShowerEventRecord: {
            "status": (
                "active",
                "recovery_pending",
                "timeout_pending",
                "skipped_unresolved_revert",
                "resolved",
            ),
        },
        PlanRecord: {
            "optimizer_version": (
                "migration_029_legacy_shower",
                "shower_reactive",
            ),
        },
        PlanActionRecord: {
            "status": (
                "pending",
                "executing",
                "dispatched",
                "executed",
                "skipped",
                "cancelled",
                "failed",
                "expired",
            ),
            "action_type": (
                "force_dhw_on",
                "force_dhw_off",
                "zone_temp_boost",
                "zone_temp_restore",
            ),
        },
        AuditLogRecord: {
            "actor": ("authenticated_api", "shower_detector"),
            "action": ("manual_safety_revert_resolution", "shower_mode_activated"),
            "result": ("manually_resolved", "force_dhw_on_scheduled"),
        },
        ServiceHeartbeatRecord: {"service": ("safety_watchdog",)},
    }

    for model, columns in values.items():
        for column_name, persisted_values in columns.items():
            limit = _column_length(model, column_name)
            assert limit is not None
            for value in persisted_values:
                assert len(value) <= limit, f"{value!r} exceeds {model.__tablename__}.{column_name}"

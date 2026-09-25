from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from packages.core.models import PlanActionRecord, ShowerEventRecord


def _migration():
    path = Path(__file__).parents[1] / "migrations/versions/029_safety_revert_obligations.py"
    spec = importlib.util.spec_from_file_location("migration_029", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Connection:
    def __init__(self, value):
        self.value = value

    def execute(self, _statement):
        return self

    def scalar_one_or_none(self):
        return self.value


class TestSafetyRevertMigration:
    @pytest.mark.parametrize(("value", "expected"), [("45", 45), (None, 60), ("   ", 60)])
    def test_p2_ac12_duration_backfill_uses_configured_or_default(self, value, expected):
        assert _migration()._shower_duration_minutes(_Connection(value)) == expected

    @pytest.mark.parametrize("value", ["nope", "0", "-1"])
    def test_p2_ac12_duration_backfill_rejects_invalid_values(self, value):
        with pytest.raises(RuntimeError, match="positive"):
            _migration()._shower_duration_minutes(_Connection(value))

    def test_p2_ac12_legacy_synthesis_and_open_event_index_are_explicit(self):
        source = (
            Path(__file__).parents[1] / "migrations/versions/029_safety_revert_obligations.py"
        ).read_text(encoding="utf-8")
        assert "migration_029_legacy_shower" in source
        assert "cannot infer unique device_id" in source
        assert "status IN ('active', 'recovery_pending', 'timeout_pending')" in source
        assert 'sa.Column("safety_claimed_at", sa.DateTime(timezone=True))' in source
        assert "migration_029_row_counts" in source
        assert "Non-concurrent index/FK creation is intentional for atomicity" in source
        assert "SET LOCAL lock_timeout = '5s'" in source

    def test_p2_ac12_downgrade_refuses_unresolved_obligations(self):
        source = (
            Path(__file__).parents[1] / "migrations/versions/029_safety_revert_obligations.py"
        ).read_text(encoding="utf-8")
        assert "status IN ('pending', 'executing', 'dispatched')" in source
        assert "cannot downgrade while unresolved safety obligations exist" in source


class TestSafetyRevertModelIndexes:
    def test_p2_indexes_match_migration_029(self):
        plan_indexes = {index.name: index for index in PlanActionRecord.__table__.indexes}
        shower_indexes = {index.name: index for index in ShowerEventRecord.__table__.indexes}

        revert_index = plan_indexes["uq_plan_actions_reverts_action_id"]
        assert [column.name for column in revert_index.columns] == ["reverts_action_id"]
        assert revert_index.unique is True
        assert (
            str(revert_index.dialect_options["postgresql"]["where"])
            == "reverts_action_id IS NOT NULL"
        )

        due_index = plan_indexes["ix_plan_actions_safety_due"]
        assert [column.name for column in due_index.columns] == [
            "safety_next_retry_at",
            "scheduled_ts",
            "id",
        ]
        assert due_index.unique is False
        assert (
            str(due_index.dialect_options["postgresql"]["where"])
            == "reverts_action_id IS NOT NULL AND status = 'pending'"
        )

        expiry_index = shower_indexes["ix_shower_events_open_expiry"]
        assert [column.name for column in expiry_index.columns] == ["expires_at"]
        assert expiry_index.unique is False
        assert (
            str(expiry_index.dialect_options["postgresql"]["where"])
            == "status IN ('active', 'recovery_pending', 'timeout_pending')"
        )

"""Reviewed migration head pins must agree; historical shape revisions stay distinct."""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVIEWED_HEAD = "039"


def _constant(path: str, name: str):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )


def test_reviewed_head_and_historical_shape_are_distinct():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVIEWED_HEAD]
    module = "tests/e2e/test_manual_cop_postgresql.py"
    assert _constant(module, "CURRENT_HEAD_REVISION") == REVIEWED_HEAD
    assert _constant(module, "LOCAL_PERIOD_REVISION") == "036"
    assert _constant("tests/e2e/test_phase2_safety_reverts.py", "CURRENT_HEAD") == REVIEWED_HEAD
    assert scripts.get_revision(REVIEWED_HEAD).down_revision == "038"
    assert scripts.get_revision("036").down_revision == "035"


def test_no_stale_head_receipts_or_qualification_pins():
    module = (ROOT / "tests/e2e/test_manual_cop_postgresql.py").read_text(encoding="utf-8")
    assert '"after": "036"' not in module
    assert '"head": "036"' not in module
    assert 'lambda: "036"' not in module
    assert 'return "036"' not in module
    for path in ("tests/isolated_reset_postgresql.py", "tests/isolated_reset_operator.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert '["036"]' not in source
        assert f'["{REVIEWED_HEAD}"]' in source
    restore = (ROOT / "tests/test_restore_db_integration.py").read_text(encoding="utf-8")
    assert f'"RESTORE_EXPECTED_REVISION": "{REVIEWED_HEAD}"' in restore
    assert f"INSERT INTO alembic_version VALUES ('{REVIEWED_HEAD}')" in restore


def test_p2_1_migration_keeps_dependency_generation_additive_and_non_destructive():
    migration = (ROOT / "migrations/versions/038_house_contract_foundation.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "038"' in migration
    assert 'down_revision = "037"' in migration
    assert '"demand_dependency_generation"' in migration
    assert 'server_default="1"' in migration
    assert "def downgrade()" in migration
    assert "not destructively downgraded" in migration


def test_indoor_dependency_generation_is_additive_and_non_destructive():
    migration = (ROOT / "migrations/versions/039_indoor_model_dependency_generation.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "039"' in migration
    assert 'down_revision = "038"' in migration
    assert '"indoor_dependency_generation"' in migration
    assert 'server_default="1"' in migration
    assert "def downgrade()" in migration
    assert "not destructively downgraded" in migration

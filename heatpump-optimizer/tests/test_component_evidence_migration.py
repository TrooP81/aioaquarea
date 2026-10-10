import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, text


def _load_migration():
    path = Path(__file__).parents[1] / "migrations/versions/028_component_space_heating_evidence.py"
    spec = importlib.util.spec_from_file_location("migration_028", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    return migration


def test_component_evidence_backfill_requires_complete_unambiguous_signature():
    migration = (
        Path(__file__).parents[1] / "migrations/versions/028_component_space_heating_evidence.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert "mode IN ('1', '3')" in source
    assert "direction = 'PUMP'" in source
    assert "pump_duty = 1" in source
    assert "COALESCE(defrost_active, FALSE) = FALSE" in source
    assert "zone1_operation_status = 1 OR zone2_operation_status = 1" in source


def test_component_evidence_backfill_rejects_conflicting_action_states():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE device_status (
                    device_id TEXT PRIMARY KEY,
                    mode TEXT,
                    direction TEXT,
                    pump_duty INTEGER,
                    device_action TEXT,
                    defrost_active BOOLEAN,
                    zone1_operation_status INTEGER,
                    zone2_operation_status INTEGER,
                    space_heating_active BOOLEAN,
                    space_heating_evidence TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO device_status (
                    device_id, mode, direction, pump_duty, device_action,
                    defrost_active, zone1_operation_status, zone2_operation_status,
                    space_heating_active, space_heating_evidence
                ) VALUES
                    ('valid', '1', 'PUMP', 1, 'OFF', 0, 1, 0, 0, 'not_confirmed'),
                    ('cooling', '1', 'PUMP', 1, 'COOLING', 0, 1, 0, 0, 'not_confirmed'),
                    ('dhw', '1', 'PUMP', 1, 'HEATING_WATER', 0, 1, 0, 0, 'not_confirmed'),
                    ('idle', '1', 'PUMP', 1, 'IDLE', 0, 1, 0, 0, 'not_confirmed'),
                    ('defrost', '1', 'PUMP', 1, 'OFF', 1, 1, 0, 0, 'not_confirmed'),
                    ('no-zone', '1', 'PUMP', 1, 'OFF', 0, 0, 0, 0, 'not_confirmed'),
                    ('already-true', '1', 'PUMP', 1, 'OFF', 0, 1, 0, 1, 'manual')
                """
            )
        )

        migration = _load_migration()
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        rows = (
            connection.execute(
                text(
                    """
                SELECT device_id, space_heating_active, space_heating_evidence
                FROM device_status
                ORDER BY device_id
                """
                )
            )
            .mappings()
            .all()
        )

    assert rows == [
        {
            "device_id": "already-true",
            "space_heating_active": 1,
            "space_heating_evidence": "manual",
        },
        {
            "device_id": "cooling",
            "space_heating_active": 0,
            "space_heating_evidence": "not_confirmed",
        },
        {
            "device_id": "defrost",
            "space_heating_active": 0,
            "space_heating_evidence": "not_confirmed",
        },
        {
            "device_id": "dhw",
            "space_heating_active": 0,
            "space_heating_evidence": "not_confirmed",
        },
        {
            "device_id": "idle",
            "space_heating_active": 0,
            "space_heating_evidence": "not_confirmed",
        },
        {
            "device_id": "no-zone",
            "space_heating_active": 0,
            "space_heating_evidence": "not_confirmed",
        },
        {
            "device_id": "valid",
            "space_heating_active": 1,
            "space_heating_evidence": "component_space_heating",
        },
    ]


def _load_lifecycle_migration():
    path = Path(__file__).parents[1] / "migrations/versions/033_ml_observation_lifecycle.py"
    spec = importlib.util.spec_from_file_location("migration_033", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    return migration


def test_ml_lifecycle_migration_upgrade_is_additive_and_matches_models():
    from sqlalchemy import CheckConstraint, MetaData, Table
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from packages.core.models import Base

    migration = _load_lifecycle_migration()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert migration.down_revision == "032"
    assert "NO CYCLE" in sql
    assert "create_hypertable" not in sql
    assert not any(
        line.startswith(("UPDATE ", "INSERT INTO ", "DELETE FROM ")) for line in sql.splitlines()
    )
    assert "ADD COLUMN native_policy_provenance JSONB" in sql
    assert "BEFORE UPDATE ON ml_forecast_issues" in sql
    assert Base.metadata.tables["device_status"].c.native_policy_provenance.nullable

    metadata = MetaData()
    recorder = Mock()

    def create_table(name, *columns, **options):
        return Table(name, metadata, *columns, **options)

    recorder.create_table.side_effect = create_table
    original_op = migration.op
    migration.op = recorder
    try:
        migration.upgrade()
    finally:
        migration.op = original_op
    assert set(metadata.tables) == set(migration._TABLES)
    dialect = postgresql.dialect()
    for table_name, table in metadata.tables.items():
        model = Base.metadata.tables[table_name]
        assert set(table.columns.keys()) == set(model.columns.keys())
        for column in table.columns:
            counterpart = model.columns[column.name]
            assert column.type.compile(dialect=dialect) == counterpart.type.compile(dialect=dialect)
            assert column.nullable == counterpart.nullable
            assert column.primary_key == counterpart.primary_key
        constraints = {constraint.name for constraint in table.constraints if constraint.name}
        assert constraints == {
            constraint.name for constraint in model.constraints if constraint.name
        }
        assert {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        } == {
            constraint.name: str(constraint.sqltext)
            for constraint in model.constraints
            if isinstance(constraint, CheckConstraint)
        }
        rendered = " ".join(str(CreateTable(table).compile(dialect=dialect)).split()) + ";"
        assert rendered in " ".join(sql.split())
    assert {call.args[0] for call in recorder.create_index.call_args_list} == {
        index.name for name in migration._TABLES for index in Base.metadata.tables[name].indexes
    }


@pytest.mark.parametrize("populated,tokens_issued", [(True, True), (True, False), (False, True)])
def test_ml_lifecycle_downgrade_refuses_populated_or_previously_used_storage(
    populated, tokens_issued
):
    migration = _load_lifecycle_migration()
    recorder = Mock()
    results = [Mock(), Mock()]
    results[0].scalar_one.return_value = populated
    results[1].scalar_one.return_value = tokens_issued
    recorder.get_bind.return_value.execute.side_effect = results
    migration.op = recorder

    with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
        migration.downgrade()
    recorder.drop_table.assert_not_called()
    assert "ACCESS EXCLUSIVE" in recorder.execute.call_args_list[0].args[0]


def test_ml_lifecycle_downgrade_drops_only_empty_unused_storage():
    migration = _load_lifecycle_migration()
    recorder = Mock()
    recorder.get_bind.return_value.execute.return_value.scalar_one.return_value = False
    migration.op = recorder

    migration.downgrade()

    assert [call.args[0] for call in recorder.drop_table.call_args_list] == list(migration._TABLES)
    assert recorder.execute.call_args_list[-1].args[0].element.name == "ml_lifecycle_token_seq"

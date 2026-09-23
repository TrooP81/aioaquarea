import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
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

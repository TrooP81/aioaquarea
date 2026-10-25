"""Offline contract regressions for the owned lifecycle fixtures (no DB access)."""

from pathlib import Path
import ast
import asyncio
from contextlib import asynccontextmanager
import importlib
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


SOURCE = Path(__file__).parent / "e2e/test_ml_lifecycle_postgresql.py"


def _function(name, source=None):
    tree = ast.parse(SOURCE.read_text() if source is None else source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


@pytest.mark.parametrize(
    "name",
    [
        "_readiness_source_fixture",
        "test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse",
    ],
)
def test_readiness_temperatures_are_explicit_and_ordered(name):
    node = _function(name)
    values = {
        key.value: value.value
        for mapping in ast.walk(node)
        if isinstance(mapping, ast.Dict)
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant)
        and key.value in {"comfort_temp_min", "comfort_temp_target", "comfort_temp_max"}
        and isinstance(value, ast.Constant)
    }
    assert tuple(
        float(values[key])
        for key in ("comfort_temp_min", "comfort_temp_target", "comfort_temp_max")
    ) == (18, 20.5, 25)


def test_artifact_uses_real_scope_before_signing():
    _assert_artifact_contract(SOURCE.read_text())


def _assert_artifact_contract(source):
    node = _function("_artifact", source)
    assert isinstance(node, ast.AsyncFunctionDef)
    source = ast.unparse(node)
    assert source.index("locked_demand_scope(device_id, session=session)") < source.index(
        "return publish_signed_artifact("
    )
    assert "'device_id': device_id" in source
    assert "'scope': manifest_scope" in source


@pytest.fixture
def fixtures(monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE.parents[2]))
    monkeypatch.setenv("SECRET_KEY", "offline-lifecycle-fixture-contract-test-key")
    return importlib.import_module("tests.e2e.test_ml_lifecycle_postgresql")


def _temperature_values(name, source=None):
    return {
        key.value: value.value
        for mapping in ast.walk(_function(name, source))
        if isinstance(mapping, ast.Dict)
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant)
        and str(key.value).startswith("comfort_temp_")
        and isinstance(value, ast.Constant)
    }


def test_real_settings_validator_accepts_snapshots_rejects_original_blanks(fixtures):
    from packages.core.settings_service import normalize_contract_settings_updates

    for name in (
        "_readiness_source_fixture",
        "test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse",
    ):
        values = _temperature_values(name)
        assert normalize_contract_settings_updates(values) == {
            "comfort_temp_min": "18.0",
            "comfort_temp_target": "20.5",
            "comfort_temp_max": "25.0",
        }
        for key in values:
            with pytest.raises(ValueError, match="finite numeric value"):
                normalize_contract_settings_updates({**values, key: ""})


def _assert_chronology(source):
    node = _function("test_UR2_SR1_repeatable_read_in_place_edit", source)
    writer = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.AsyncWith) and "set_settings_bulk" in ast.unparse(part)
    )
    signing = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "new_artifact" for t in part.targets)
    )
    text = ast.unparse(node)
    assert writer.end_lineno < signing.lineno
    assert "session=session" in ast.unparse(writer)
    assert text.index("old_artifact = await _artifact") < text.index("first = asyncio.create_task")
    assert (
        text.index("new_artifact = await _artifact")
        < text.index("expected_incumbent=old_artifact.artifact_hash")
        < text.index("changed.set()")
    )
    assert "asyncio.wait_for(reached.wait(), 3)" in ast.unparse(
        next(part for part in node.body if isinstance(part, ast.Try))
    )
    assert "finally:" in text and "asyncio.gather(first, return_exceptions=True)" in text
    for model in (
        "IndoorTempReading",
        "DeviceStatusRecord",
        "WeatherRecord",
        "ConsumptionRecord",
    ):
        assert f"update({model})" in ast.unparse(writer)
    assert ".values(ts=" not in ast.unparse(writer)
    assert ".values(timestamp=" not in ast.unparse(writer)


def test_ur2_signs_only_after_coherent_writer_commits():
    source = SOURCE.read_text()
    _assert_chronology(source)
    # Negative sensitivity: restore the old pre-transition signing order.
    node = _function("test_UR2_SR1_repeatable_read_in_place_edit", source)
    signing = next(
        part
        for part in ast.walk(node)
        if isinstance(part, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "new_artifact" for t in part.targets)
    )
    lines = source.splitlines(keepends=True)
    moved = "".join(lines[signing.lineno - 1 : signing.end_lineno])
    lines[signing.lineno - 1 : signing.end_lineno] = []
    index = next(i for i, line in enumerate(lines) if "reached, changed =" in line)
    lines.insert(index, "    " + moved.lstrip())
    with pytest.raises(AssertionError):
        _assert_chronology("".join(lines))


@pytest.mark.asyncio
async def test_artifact_real_signing_keeps_identity_and_scope(fixtures, monkeypatch, tmp_path):
    from packages.ml import forecast_quality, safe_persistence

    monkeypatch.setattr(fixtures.settings, "model_dir", str(tmp_path))
    monkeypatch.setattr(fixtures.settings, "secret_key", "offline-fixture-test-key")
    events = []
    # Windows cannot open directories for fsync. This offline test covers real
    # serialization/HMAC, not durability; the unchanged Linux fsync E2E covers that.
    directory_syncs = []
    monkeypatch.setattr(
        safe_persistence, "_fsync_directory", lambda path: directory_syncs.append(path)
    )
    scope = {"installation_id": "offline", "demand_dependency_generation": 3}
    session = object()

    @asynccontextmanager
    async def sessions():
        yield session
        events.append("committed")

    @asynccontextmanager
    async def locked(device_id, *, session):
        assert device_id == "fixture-device"
        events.append("scope")
        yield dict(scope), session

    monkeypatch.setattr(forecast_quality, "locked_demand_scope", locked)
    artifact = await fixtures._artifact(SimpleNamespace(sessions=sessions), "offline-fit")
    restored, payload = fixtures.verify_signed_artifact(
        artifact.artifact_hash, component="demand", schema_version="demand_lifecycle_v1"
    )
    assert events == ["scope", "committed"]
    assert directory_syncs and all(path.is_dir() for path in directory_syncs)
    assert restored.manifest["device_id"] == "fixture-device"
    assert restored.manifest["scope"] == scope
    assert pickle.loads(payload) == {"fit_id": "offline-fit", "weights": [1.25, 3.5]}
    scope["demand_dependency_generation"] = 4
    assert restored.manifest["scope"]["demand_dependency_generation"] == 3

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise ValueError("current_demand_scope_unavailable")
        yield

    monkeypatch.setattr(forecast_quality, "locked_demand_scope", unavailable)
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="current_demand_scope_unavailable"):
        await fixtures._artifact(SimpleNamespace(sessions=sessions), "must-not-sign")
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("key", ["device_id", "scope"])
def test_missing_signed_manifest_contract_is_detected(key):
    source = SOURCE.read_text()
    line = (
        '            "device_id": device_id,\n'
        if key == "device_id"
        else '            "scope": manifest_scope,\n'
    )
    with pytest.raises(AssertionError):
        _assert_artifact_contract(source.replace(line, ""))


def test_private_metadata_resolves_circular_and_plan_fks_without_global_mutation(fixtures):
    from sqlalchemy import create_mock_engine

    originals = {
        model: (
            model.__table__.schema,
            tuple(str(fk.target_fullname) for fk in model.__table__.foreign_keys),
        )
        for model in fixtures.FIXTURE_MODELS
    }
    metadata = fixtures._fixture_metadata("owned_fixture")
    statements = []
    engine = create_mock_engine(
        "postgresql://",
        lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    metadata.create_all(engine, checkfirst=False)
    ddl = "\n".join(statements)
    assert "CREATE TABLE owned_fixture.settings" in ddl
    assert "REFERENCES owned_fixture.house_installations" in ddl
    assert "REFERENCES owned_fixture.house_contract_revisions" in ddl
    assert "REFERENCES owned_fixture.plan_actions" in ddl
    assert "ALTER TABLE owned_fixture.house_" in ddl
    for model, original in originals.items():
        assert (
            model.__table__.schema,
            tuple(str(fk.target_fullname) for fk in model.__table__.foreign_keys),
        ) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "namespace,kind,resolved,default,owned,allowed",
    [
        ("owned", "S", 10, True, True, True),
        ("public", "S", 10, True, True, False),
        ("owned", "r", 10, True, True, False),
        ("owned", "S", 11, True, True, False),
        ("owned", "S", 10, False, True, False),
        ("owned", "S", 10, True, False, False),
    ],
)
async def test_sequence_ownership_negative_sensitivity(
    fixtures, namespace, kind, resolved, default, owned, allowed
):
    connection = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(one=lambda: (namespace, kind, resolved, default, owned))
        )
    )
    call = fixtures._assert_local_sequence(connection, "owned", 10, 20, "id")
    if allowed:
        await call
    else:
        with pytest.raises(AssertionError):
            await call


@pytest.mark.parametrize("public_only", [False, True])
def test_relation_oid_resolution_rejects_public_only_or_shadowed(fixtures, public_only):
    connection = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                one=lambda: (None, 99) if public_only else (10, 99),
                scalar_one=lambda: "owned",
            )
        )
    )
    with pytest.raises(AssertionError, match="nonlocal fixture table"):
        fixtures._require_local_table(
            SimpleNamespace(execute=lambda *args, **kwargs: connection.execute.return_value),
            fixtures.SettingRecord.__table__,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "public_relation", "public_fk"])
async def test_full_catalog_check_rejects_nonlocal_relations_and_fk_targets(fixtures, fault):
    models = {model.__tablename__: model for model in fixtures.FIXTURE_MODELS}
    oids = {name: index for index, name in enumerate(models, start=10)}

    async def execute(statement, parameters):
        sql = str(statement)
        if "SELECT c.oid, to_regclass(:name)" in sql:
            oid = oids[parameters["name"]]
            row = (oid, oid + 999 if fault == "public_relation" else oid, "r")
        elif "SELECT attname FROM pg_attribute" in sql:
            model = next(model for name, model in models.items() if oids[name] == parameters["oid"])
            columns = list(model.__table__.columns.keys())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: columns))
        elif "FROM pg_constraint c WHERE" in sql:
            model = next(model for name, model in models.items() if oids[name] == parameters["oid"])
            rows = [
                (
                    [element.parent.name for element in fk.elements],
                    oids[fk.referred_table.name] + (999 if fault == "public_fk" else 0),
                    [element.column.name for element in fk.elements],
                )
                for fk in model.__table__.foreign_key_constraints
            ]
            return SimpleNamespace(all=lambda: rows)
        elif "pg_get_serial_sequence" in sql:
            return SimpleNamespace(scalar_one=lambda: 500)
        elif "SELECT to_regclass(:name)::oid" in sql:
            return SimpleNamespace(scalar_one=lambda: 600)
        elif "SELECT n.nspname" in sql:
            row = ("owned", "S", parameters["seq"], True, parameters["seq"] != 600)
        else:
            raise AssertionError(sql)
        return SimpleNamespace(one=lambda: row)

    connection = SimpleNamespace(execute=execute)
    if fault is None:
        await fixtures._assert_fixture_storage(connection, "owned")
    else:
        with pytest.raises(AssertionError, match="nonlocal"):
            await fixtures._assert_fixture_storage(connection, "owned")


def test_public_comparison_failure_cannot_bypass_schema_drop():
    node = _function("lifecycle_database")
    comparisons = [
        part
        for part in ast.walk(node)
        if isinstance(part, ast.Try)
        and any("fixture_public_unchanged =" in ast.unparse(line) for line in part.body)
        and any("DropSchema" in ast.unparse(line) for line in part.finalbody)
    ]
    assert comparisons
    cleanup = ast.unparse(comparisons[-1])
    assert cleanup.index("_public_fixture_state") < cleanup.index("finally:")
    assert cleanup.index("DropSchema") < cleanup.rindex("_public_fixture_state")
    assert cleanup.count("async with control.begin()") == 3
    assert "retained_storage_state" in cleanup
    assert "await control.dispose()" in ast.unparse(node)
    assert "fixture_storage_ownership_checked" in ast.unparse(node)


@pytest.mark.asyncio
async def test_g3_observer_preserves_none_identity_and_exception(fixtures, monkeypatch):
    from packages.ml import forecast_quality, thermal

    result = {"sensor_basis": {"kind": "heatpump_temperature"}, "capability": "tank_cooling"}
    native = AsyncMock(side_effect=[None, result, ValueError("original rejection")])
    history = AsyncMock(return_value=None)
    monkeypatch.setattr(forecast_quality, "materialize_native_inputs", native)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", history)
    observations = []
    with fixtures._trace_thermal_materializers(monkeypatch, observations):
        assert await forecast_quality.materialize_native_inputs("thermal") is None
        assert await forecast_quality.materialize_native_inputs("thermal") is result
        with pytest.raises(ValueError, match="original rejection"):
            await forecast_quality.materialize_native_inputs("thermal")
        assert await thermal.materialize_thermal_inputs("device") is None
    assert forecast_quality.materialize_native_inputs is native
    assert thermal.materialize_thermal_inputs is history
    assert observations[-2]["exception"] == "ValueError"
    assert observations[-1] == {"call": "thermal_history", "result": "None"}


@pytest.mark.asyncio
@pytest.mark.parametrize("history_error", [False, True])
async def test_g3_differential_is_prerequisite_only_and_rolls_back(
    fixtures, monkeypatch, capsys, history_error
):
    from packages.ml import forecast_quality, thermal

    state = {"present": False}
    rolled_back = []
    additions = []
    inputs = {
        "device_id": "audit-device",
        "reference_sensor_id": "selected-room",
        "weather_provider": "open-meteo",
        "configured_sensor_ids": ["selected-room"],
        "sensor_basis": {"kind": "indoor"},
        "capability": "indoor_heating",
    }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return None

        async def execute(self, *args):
            return SimpleNamespace(one=lambda: (10, 10))

        def add(self, row):
            assert isinstance(row, fixtures.HouseInstallationRecord)
            additions.append(row)
            state["present"] = True

        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    class Connection:
        async def begin(self):
            state["present"] = False
            return self

        async def rollback(self):
            rolled_back.append(state["present"])
            state["present"] = False

    @asynccontextmanager
    async def connect():
        yield Connection()

    async def materialize(component, **kwargs):
        assert component == "thermal"
        assert kwargs["now"] is clock
        return inputs if state["present"] else None

    clock = object()
    original_native = AsyncMock(side_effect=materialize)
    original_history = AsyncMock(
        side_effect=ValueError("second boundary") if history_error else None,
        return_value=None,
    )
    monkeypatch.setattr(forecast_quality, "materialize_native_inputs", original_native)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", original_history)
    monkeypatch.setattr(fixtures, "async_sessionmaker", lambda *args, **kwargs: Session)
    database = SimpleNamespace(
        sessions=sessions, schema="owned", engine=SimpleNamespace(connect=connect)
    )
    assert await fixtures._thermal_prerequisite_probe(database, monkeypatch, clock) is None
    assert rolled_back == [False, True] and not state["present"]
    assert len(additions) == 1 and additions[0].current_revision_id is None
    assert original_native.await_count == 4 and original_history.await_count == 1
    assert forecast_quality.materialize_native_inputs is original_native
    assert thermal.materialize_thermal_inputs is original_history
    output = capsys.readouterr().out
    assert output.count('"rolled_back": true') == 2
    if history_error:
        assert '"probe_exception": "ValueError"' in output
    # The diagnostic helper contains no settings/contract initialization or model work.
    text = ast.unparse(_function("_thermal_prerequisite_probe"))
    for forbidden in (
        "set_settings_bulk",
        "ensure_current_contract",
        ".fit(",
        ".publish(",
        "qualify",
    ):
        assert forbidden not in text


def test_all_artifact_callers_await_and_fault_setup_is_opt_in():
    for path in (SOURCE, SOURCE.with_name("test_ml_lifecycle_verification.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_artifact"
            ):
                assert isinstance(parents[node], ast.Await), (path, node.lineno)
    for name in (
        "test_reset_and_status_never_initialize_empty_authority",
        "test_maintenance_off_admin_reset_only_revokes_existing_state",
    ):
        source = SOURCE.with_name("test_ml_lifecycle_verification.py").read_text()
        text = ast.unparse(_function(name, source))
        if name.startswith("test_reset"):
            assert "_owner(" not in text and "_artifact(" not in text
        else:
            branch = next(node for node in _function(name, source).body if isinstance(node, ast.If))
            assert "demand=True" in ast.unparse(branch)


@pytest.mark.asyncio
@pytest.mark.parametrize("demand", [False, True])
async def test_owner_prepares_demand_only_when_requested_and_before_authority(
    fixtures, monkeypatch, demand
):
    events = []
    lease = object()

    async def prepare(database):
        events.append("prepare")

    async def initialize():
        events.append("initialize")

    async def enable(enabled):
        assert enabled is True
        events.append("enable")

    async def acquire(worker):
        assert worker.startswith("worker-")
        events.append("acquire")
        return lease

    monkeypatch.setattr(fixtures, "_prepare_demand_fixture", prepare)
    database = SimpleNamespace(
        lifecycle=SimpleNamespace(
            initialize=initialize, set_maintenance_enabled=enable, acquire=acquire
        )
    )
    assert await fixtures._owner(database, demand=demand) is lease
    assert events == (["prepare"] if demand else []) + ["initialize", "enable", "acquire"]


@pytest.mark.asyncio
async def test_g3_history_cancellation_is_not_swallowed(fixtures, monkeypatch):
    from packages.ml import thermal

    cancellation = asyncio.CancelledError("offline cancellation")
    history = AsyncMock(side_effect=cancellation)
    monkeypatch.setattr(thermal, "materialize_thermal_inputs", history)
    observations = []
    with pytest.raises(asyncio.CancelledError) as caught:
        with fixtures._trace_thermal_materializers(monkeypatch, observations):
            await thermal.materialize_thermal_inputs("fixture-device")
    assert caught.value is cancellation
    assert thermal.materialize_thermal_inputs is history
    assert observations == [{"call": "thermal_history", "exception": "CancelledError"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["row_version", "sequence"])
async def test_public_state_observes_fresh_row_and_sequence_drift_without_writes(fixtures, drift):
    state = {"row_version": "before", "sequence": 7}
    statements = []

    async def execute(statement, parameters=None):
        sql = str(statement)
        statements.append(sql)
        if "SELECT to_regclass" in sql:
            return SimpleNamespace(scalar_one=lambda: 10)
        if "SELECT count(*)" in sql:
            return SimpleNamespace(one=lambda: (1, state["row_version"]))
        if "SELECT relnamespace" in sql:
            return SimpleNamespace(one=lambda: (2200, "r", 1, 10))
        if "JOIN pg_sequence" in sql:
            return SimpleNamespace(all=lambda: [(20, "settings_id_seq", "configuration")])
        if "SELECT last_value,is_called" in sql:
            return SimpleNamespace(one=lambda: (state["sequence"], True))
        return SimpleNamespace(scalar_one=lambda: "catalog-digest")

    connection = SimpleNamespace(
        execute=execute,
        dialect=SimpleNamespace(
            identifier_preparer=SimpleNamespace(quote_identifier=lambda name: f'"{name}"')
        ),
    )
    before = await fixtures._public_fixture_state(connection)
    assert {model.__tablename__ for model in fixtures.FIXTURE_MODELS} <= before.keys()
    state[drift] = "after" if drift == "row_version" else 8
    after = await fixtures._public_fixture_state(connection)
    assert before != after
    key = "sequence:settings_id_seq" if drift == "sequence" else "settings"
    assert before[key] != after[key]
    assert all(sql.lstrip().startswith("SELECT ") for sql in statements)
    assert not any("nextval(" in sql.lower() for sql in statements)

"""Offline contract regressions for the owned lifecycle fixtures (no DB access)."""

from pathlib import Path
import ast
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import datetime as dt
import importlib
import json
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
    cleanup = ast.unparse(_function("_cleanup_lifecycle_storage"))
    assert "_cleanup_lifecycle_storage" in ast.unparse(node)
    assert "('pre-drop', lambda: compare(" in cleanup
    assert "('post-drop', lambda: compare(" in cleanup
    assert cleanup.index("('drop', drop)") < cleanup.index("('absence', verify_absence)")
    assert cleanup.count("async with control.begin()") == 3
    assert "retained_storage_state" in cleanup
    assert "('control-dispose', control.dispose)" in cleanup
    assert "fixture_storage_ownership_checked" in ast.unparse(node)


def test_thermal_prerequisite_is_opt_in_after_locality_before_materialization():
    source = SOURCE.read_text()
    route = ast.unparse(_function("_thermal_route_owner"))
    assert route.index("_require_local_table") < route.index("_initialize_thermal_installation")
    assert route.index("_initialize_thermal_installation") < route.index("hook.materialize")
    assert "HouseInstallationRecord" in route
    assert source.count("await _initialize_thermal_installation(") == 1
    initializer = ast.unparse(_function("_initialize_thermal_installation"))
    for forbidden in ("set_settings_bulk", "ensure_current_contract", "_readiness_source_fixture"):
        assert forbidden not in initializer


@pytest.mark.asyncio
async def test_nonlocal_thermal_table_prevents_initialization(fixtures, monkeypatch):
    initialize = AsyncMock()
    monkeypatch.setattr(fixtures, "_initialize_thermal_installation", initialize)

    class Connection:
        async def run_sync(self, callback):
            callback(
                SimpleNamespace(
                    execute=lambda *args, **kwargs: SimpleNamespace(
                        scalar_one=lambda: "owned", one=lambda: (10, 99)
                    )
                )
            )

    @asynccontextmanager
    async def begin():
        yield Connection()

    with pytest.raises(AssertionError, match="nonlocal fixture table"):
        await fixtures._thermal_route_owner(
            SimpleNamespace(engine=SimpleNamespace(begin=begin)), monkeypatch
        )
    initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "present", "insert", "readback", "committed-readback"])
async def test_thermal_singleton_prerequisite_and_failures(fixtures, fault):
    events, added = [], []
    state = {"row": None}

    class Session:
        async def get(self, model, key):
            assert model is fixtures.HouseInstallationRecord and key == 1
            events.append("get")
            if fault == "present" and not added:
                return object()
            if fault == "committed-readback" and "commit" in events:
                return None
            return state["row"]

        def add(self, row):
            events.append("add")
            added.append(row)
            state["row"] = row

        async def flush(self):
            events.append("flush")
            if fault == "insert":
                raise ValueError("insert failed")

        async def refresh(self, row):
            events.append("refresh")
            if fault == "readback":
                row.indoor_dependency_generation = 9

    @asynccontextmanager
    async def sessions():
        try:
            yield Session()
            events.append("commit")
        except BaseException:
            events.append("rollback")
            if "commit" not in events:
                state["row"] = None
            raise

    call = fixtures._initialize_thermal_installation(SimpleNamespace(sessions=sessions))
    if fault:
        with pytest.raises(ValueError if fault == "insert" else AssertionError):
            await call
        assert "rollback" in events
        if fault != "committed-readback":
            assert state["row"] is None and "commit" not in events
        else:
            assert events.count("commit") == 1  # post-commit read failure is not hidden
    else:
        await call
        assert len(added) == 1 and events.count("commit") == 2
        row = added[0]
        assert row.singleton_id == 1 and row.installation_id != row.house_id
        assert row.current_revision_id is None
        assert (row.demand_dependency_generation, row.indoor_dependency_generation) == (1, 1)
        assert events.index("flush") < events.index("refresh") < events.index("commit")


def _thermal_frozen_inputs():
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute as route

    inputs = route.native_inputs(route.NOW)
    inputs.update(
        installation_id="synthetic-installation",
        indoor_dependency_generation=1,
        capabilities=["indoor_heating", "indoor_cooling"],
    )
    return inputs


@pytest.mark.parametrize("head", ["indoor_heating", "indoor_cooling"])
def test_thermal_snapshot_real_freezing_scope_and_nested_copy(fixtures, head):
    from packages.ml.forecast_quality import freeze_native_pair, native_scope

    frozen = _thermal_frozen_inputs()
    original = deepcopy(frozen)
    issued = dt.datetime.fromisoformat(frozen["issued_at"]) + dt.timedelta(hours=2)
    snapshot = fixtures._thermal_native_snapshot(
        SimpleNamespace(fitted={"inputs": frozen}), issued, head
    )
    assert "scope" not in snapshot
    assert snapshot["capabilities"] == ["indoor_heating", "indoor_cooling"]
    assert native_scope(snapshot) == native_scope(frozen)

    def predictor(snapshot, horizons):
        return dict.fromkeys(horizons, 21.0)

    pair = freeze_native_pair(
        snapshot,
        epoch="offline",
        component="thermal",
        candidate_hash="candidate",
        incumbent_hash=None,
        horizons=(15, 30, 60),
        candidate_predictor=predictor,
        incumbent_predictor=predictor,
        baseline_predictor=predictor,
    )
    assert pair is not None
    assert json.loads(pair.snapshot_json)["scope"] == native_scope(frozen)
    snapshot["sensor_basis"]["sensor_ids"].append("mutated")
    snapshot["native_settings"]["mutated"] = []
    snapshot["schemas"]["artifact"] = "mutated"
    snapshot["configured_sensor_ids"].append("mutated")
    snapshot["capabilities"].clear()
    snapshot["timer"]["slots"].append({"mutated": True})
    assert frozen == original


@pytest.mark.parametrize(
    "key,value",
    [
        ("installation_id", "other"),
        ("indoor_dependency_generation", 2),
        ("configured_sensor_ids", ["other"]),
        ("reference_sensor_id", "other"),
        ("sensor_basis", {"kind": "other"}),
        ("native_settings", {"other": True}),
        ("schemas", {"artifact": "other"}),
        ("timer", {"status": "unknown"}),
        ("capabilities", []),
        ("capabilities", ["tank_cooling"]),
    ],
)
@pytest.mark.parametrize("remove", [False, True])
def test_thermal_scope_dependency_negative_sensitivity(fixtures, key, value, remove):
    from packages.ml.forecast_quality import native_scope

    original = _thermal_frozen_inputs()
    changed = deepcopy(original)
    if remove:
        changed.pop(key)
    else:
        changed[key] = value
    try:
        scope = native_scope(changed)
    except KeyError:
        pass  # required dependency rejected, never manufactured
    else:
        assert scope != native_scope(original)
    if key == "capabilities":
        with pytest.raises((AssertionError, KeyError)):
            fixtures._thermal_native_snapshot(
                SimpleNamespace(fitted={"inputs": changed}),
                dt.datetime.fromisoformat(original["issued_at"]),
                "indoor_heating",
            )


def _fingerprint_fixture(fixtures):
    raw = {
        "settings": (10, (1, "r", 2, 3), (1, "versions"), "catalog", "fk", "defaults"),
        "sequence:settings_id_seq": (20, "SECRET_CONFIGURATION", (1, True), "dependencies"),
    }
    return raw, fixtures._public_fixture_components(
        raw, {"settings": "content"}, {"settings": {"local_oid": 10, "resolved_oid": 10}}
    )


@pytest.mark.parametrize(
    "object_name,component",
    [
        ("public.settings", "content"),
        ("public.settings", "versions"),
        ("public.settings", "catalog"),
        ("public.settings", "constraints"),
        ("public.settings", "defaults"),
        ("public.settings", "identity"),
        ("public.settings", "bindings"),
        ("public.settings_id_seq", "configuration"),
        ("public.settings_id_seq", "value_state"),
        ("public.settings_id_seq", "dependencies"),
    ],
)
def test_named_public_drift_component_sensitivity(fixtures, capsys, object_name, component):
    _, baseline = _fingerprint_fixture(fixtures)
    changed = deepcopy(baseline)
    changed[object_name][component] = "changed"
    for phase in ("pre-drop", "post-drop"):
        fixtures._emit_public_diagnostic(phase, {"commit": "offline"}, baseline, changed, False)
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["phase"] == phase and receipt["writer"] == "UNKNOWN"
        assert receipt["unchanged"] is False
        (delta,) = receipt["deltas"]
        assert delta["object"] == object_name and delta["change"] == "changed"
        assert delta["components"].keys() == {component}
        assert delta["before_hash"] != delta["after_hash"]


def test_public_diagnostic_add_remove_stability_and_redaction(fixtures, capsys):
    raw, baseline = _fingerprint_fixture(fixtures)
    assert fixtures._public_fixture_delta(baseline, deepcopy(baseline)) == []
    for old, new, classification in (({}, baseline, "added"), (baseline, {}, "removed")):
        fixtures._emit_public_diagnostic("pre-drop", {}, old, new, False)
        output = capsys.readouterr().out
        assert "SECRET_CONFIGURATION" not in output
        assert "row_payload" not in output
        assert {delta["change"] for delta in json.loads(output)["deltas"]} == {classification}
    version_changed = deepcopy(raw)
    relation = list(version_changed["settings"])
    relation[2] = (1, "reverted-write-versions")
    version_changed["settings"] = tuple(relation)
    assert version_changed != raw  # advisory equal content cannot excuse reverted writes
    sequence_changed = deepcopy(raw)
    sequence_changed["sequence:settings_id_seq"] = (20, "SECRET_CONFIGURATION", (2, True), "d")
    assert sequence_changed != raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "query",
        "post-query",
        "serialization",
        "drop",
        "absence",
        "engine",
        "control",
        "receipt",
        "drift",
        "post-drift",
    ],
)
async def test_cleanup_attempts_all_obligations_without_swallowing(
    fixtures, monkeypatch, capsys, fault
):
    events, transactions = [], []
    serial = 0
    source = {"commit": "offline"}
    raw, components = _fingerprint_fixture(fixtures)

    class Control:
        @asynccontextmanager
        async def begin(self):
            nonlocal serial
            serial += 1
            transactions.append(serial)
            yield SimpleNamespace(execute=execute, transaction=serial)

        async def dispose(self):
            events.append("control")
            if fault == "control":
                raise ValueError("SECRET_ERROR")

    async def execute(statement, parameters=None):
        if "DROP SCHEMA" in str(statement):
            events.append("drop")
            if fault == "drop":
                raise ValueError("SECRET_ERROR")
        else:
            events.append("absence")
            if fault == "absence":
                raise ValueError("SECRET_ERROR")
        return SimpleNamespace(scalar_one=lambda: None)

    async def fingerprint(connection, *, diagnostics):
        events.append("fingerprint")
        if fault == "query" or (fault == "post-query" and connection.transaction == 4):
            raise ValueError("SECRET_ERROR")
        diagnostics.update(deepcopy(components))
        if (fault == "drift" and connection.transaction == 1) or (
            fault == "post-drift" and connection.transaction == 4
        ):
            return {}
        return raw

    async def dispose():
        events.append("engine")
        if fault == "engine":
            raise ValueError("SECRET_ERROR")

    def emit(state):
        events.append("receipt")
        assert state["schema_removed"] == (fault != "absence")
        if fault in {"drift", "query", "post-query", "post-drift"}:
            assert state["public_fixture_storage_unchanged"] is False
        if fault == "receipt":
            raise ValueError("SECRET_ERROR")

    monkeypatch.setattr(fixtures, "guard_owned_database", AsyncMock())
    monkeypatch.setattr(fixtures, "retained_storage_state", AsyncMock(return_value="retained"))
    monkeypatch.setattr(fixtures, "_public_fixture_state", fingerprint)
    if fault == "serialization":

        def fail_serialization(*args, **kwargs):
            raise TypeError("SECRET_ERROR")

        monkeypatch.setattr(fixtures, "json", SimpleNamespace(dumps=fail_serialization))
    call = fixtures._cleanup_lifecycle_storage(
        engine=SimpleNamespace(dispose=dispose),
        control=Control(),
        schema="owned",
        created=True,
        retained="retained",
        public_baseline=raw,
        diagnostic_baseline=components,
        source=source,
        emit_receipt=emit,
    )
    if fault:
        with pytest.raises(BaseExceptionGroup) as failure:
            await call
        assert failure.value.exceptions
    else:
        await call
    assert events == [
        "fingerprint",
        "drop",
        "absence",
        "fingerprint",
        "engine",
        "control",
        "receipt",
    ]
    assert transactions == [1, 2, 3, 4]
    assert "SECRET_ERROR" not in capsys.readouterr().out


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
            return SimpleNamespace(one=lambda: (1, state["row_version"], "content"))
        if "SELECT DISTINCT n.nspname" in sql:
            return SimpleNamespace(all=lambda: [("owned", "related", 42)])
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

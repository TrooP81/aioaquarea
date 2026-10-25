"""Offline ordering/failure contracts; real PostgreSQL coverage lives in E2E."""

from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.sql.dml import Delete, Insert

from tests.e2e import ordinary_cleanup as cleanup


def test_immutable_empty_probe_explicitly_covers_the_model_fk_closure():
    from packages.core.models import Base

    targets = {"house_contract_revisions"}
    while True:
        expanded = targets | {
            table.name
            for table in Base.metadata.tables.values()
            if any(foreign_key.column.table.name in targets for foreign_key in table.foreign_keys)
        }
        if expanded == targets:
            break
        targets = expanded
    assert targets == {
        "house_contract_revisions",
        "house_installations",
        "plans",
        "optimization_requests",
    }
    # plan_actions.plan_id and optimization_requests.plan_id are not FKs;
    # do not add unrelated tables or silently switch this rejection probe to CASCADE.
    module = ast.parse(
        Path(cleanup.__file__).with_name("test_settings.py").read_text(encoding="utf-8")
    )
    probe = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name
        == "test_ordinary_first_initialization_retains_fallback_absence_and_immutable_empty_trigger"
    )
    sql = [
        node.value
        for node in ast.walk(probe)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("TRUNCATE ")
    ]
    assert sql == [
        "TRUNCATE public.house_contract_revisions, public.house_installations, "
        "public.plans, public.optimization_requests"
    ]
    assert {
        target.removeprefix("public.") for target in sql[0].removeprefix("TRUNCATE ").split(", ")
    } == targets


@pytest.mark.parametrize("orphan_history", [False, True])
async def test_first_capture_locks_validates_and_initializes_without_materializing_fallbacks(
    monkeypatch, orphan_history
):
    events = []
    rows = ({"key": "latitude", "value": "51.5", "updated_at": "original"},)
    effective = {"latitude": "51.5", "timezone": "fallback"}

    async def lock(session):
        events.append("lock")

    async def settings(*, session):
        events.append("effective")
        return effective

    async def ensure(session, settings, *, lock):
        assert lock is False and settings is effective
        events.append("initialize")

    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: (1,) if orphan_history else None)
        ),
    )
    monkeypatch.setattr(cleanup.house_contract, "_lock_contract", lock)
    monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", ensure)
    monkeypatch.setattr(cleanup.settings_service, "get_all_settings", settings)
    monkeypatch.setattr(cleanup, "_settings_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(cleanup, "_validate_effective", lambda *args: events.append("validate"))
    monkeypatch.setattr(cleanup, "assert_ordinary_contract_consistent", AsyncMock())
    monkeypatch.setattr(cleanup, "_house_boundary", AsyncMock(return_value=boundary()))
    if orphan_history:
        with pytest.raises(AssertionError, match="history without installation"):
            await cleanup.capture_ordinary_baseline(session)
        assert "initialize" not in events
    else:
        result = await cleanup.capture_ordinary_baseline(session)
        assert result.rows == rows and result.effective == effective
        assert "timezone" not in {row["key"] for row in result.rows}
        assert events == ["lock", "effective", "validate", "initialize"]


def test_generic_cleanup_excludes_all_migrated_authority_storage():
    source = Path(cleanup.__file__).with_name("conftest.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    expression = next(
        statement.value
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "LEGACY_TABLES"
            for target in statement.targets
        )
    )
    names = cleanup.PRESERVED_TABLES | {
        "manual_cop_observations",
        "price_scopes",
        "app_logs",
        "plans",
    }
    tables = [SimpleNamespace(name=name) for name in sorted(names)]
    actual = eval(
        compile(ast.Expression(expression), "<real legacy cleanup expression>", "eval"),
        {
            "Base": SimpleNamespace(metadata=SimpleNamespace(sorted_tables=tables)),
            "PRESERVED_TABLES": cleanup.PRESERVED_TABLES,
        },
    )
    assert {table.name for table in actual} == {"app_logs", "plans"}
    # The existing retention guards remain controlling, not replaced by exclusions.
    assert "manual_sql.guard_owned_database(connection)" in source
    assert "assert not dependencies" in source
    assert "assert await manual_sql.retained_storage_state(connection) == retained" in source
    assert "finish_owned_cleanup(" in source


@pytest.mark.parametrize("missing_pointer", [True, False])
async def test_existing_baseline_rejects_missing_pointer_or_mismatch(monkeypatch, missing_pointer):
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                current_revision_id=None if missing_pointer else 9,
                installation_id="installation",
                house_id="house",
            )
        )
    )
    monkeypatch.setattr(cleanup, "_validate_effective", lambda *args: None)
    ensure = AsyncMock(side_effect=cleanup.house_contract.ContractSettingsMismatch("mismatch"))
    monkeypatch.setattr(cleanup.house_contract, "ensure_current_contract", ensure)
    with pytest.raises(
        AssertionError if missing_pointer else cleanup.house_contract.ContractSettingsMismatch
    ):
        await cleanup.assert_ordinary_contract_consistent(session, {})
    assert ensure.await_count == (0 if missing_pointer else 1)


def boundary(*, revision=7, demand=3, indoor=5):
    return {
        "installation": {
            "singleton_id": 1,
            "installation_id": "installation",
            "house_id": "house",
            "current_revision_id": revision,
            "demand_dependency_generation": demand,
            "indoor_dependency_generation": indoor,
        },
        "history": ({"id": 7, "contract_hash": "A"},),
        "catalog": ("unchanged definitions",),
        "sequence": 7,
    }


@pytest.mark.parametrize("change", ["identity", "history", "catalog", "sequence", "generation"])
def test_boundary_rejects_retained_storage_corruption(change):
    before = boundary()
    after = deepcopy(before)
    if change == "identity":
        after["installation"]["house_id"] = "other"
    elif change == "history":
        after["history"][0]["contract_hash"] = "tampered"
    elif change == "catalog":
        after["catalog"] = ()
    elif change == "sequence":
        after["sequence"] = 6
    else:
        after["installation"]["demand_dependency_generation"] = 2
    with pytest.raises(AssertionError):
        cleanup._assert_conservation(before, after)


def test_boundary_accepts_forward_history_generations_and_sequence_gaps():
    before = boundary()
    after = boundary(revision=23, demand=8, indoor=10)
    after["history"] += ({"id": 23, "contract_hash": "A"},)
    after["sequence"] = 29  # rollback gaps are not corruption
    cleanup._assert_conservation(before, after)


@pytest.mark.parametrize(
    ("weather", "sensor", "timezone"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
        (True, True, True),
    ],
)
async def test_restore_orders_dependency_accounting_rows_publication_and_truncate(
    monkeypatch, weather, sensor, timezone
):
    events = []
    effective = {
        "weather_provider": "open-meteo",
        "smartthings_device_ids": "",
        "comfort_reference_sensor_id": "",
        **{key: "A" for key in cleanup.house_contract.CONTRACT_SETTING_KEYS},
    }
    pre = effective | {
        "weather_provider": "smhi" if weather else "open-meteo",
        "smartthings_device_ids": "sensor" if sensor else "",
        "timezone": "B" if timezone else "A",
    }
    before = boundary()
    after = boundary(
        revision=8 if timezone else 7,
        demand=3 + int(weather) + int(timezone),
        indoor=5 + int(weather or sensor),
    )
    if timezone:
        after["history"] += ({"id": 8, "contract_hash": "restored"},)
        after["sequence"] = 8
    baseline = cleanup.OrdinaryBaseline(
        ({"key": "retained", "value": "exact", "updated_at": "original"},),
        effective,
        before,
    )
    installation = SimpleNamespace(**before["installation"])
    final_installation = SimpleNamespace(**after["installation"])
    previous = SimpleNamespace(
        id=7, contract_hash="old", contract_json={"timezone": pre["timezone"]}
    )
    current = SimpleNamespace(
        id=after["installation"]["current_revision_id"],
        contract_hash="restored" if timezone else "old",
        contract_json={"timezone": "A"},
    )

    async def lock(session):
        events.append("lock")

    async def settings(*, session):
        events.append("settings")
        return pre if events.count("settings") == 1 else effective

    async def consistent(session, values):
        events.append("validate")
        return (installation, previous) if values is pre else (final_installation, current)

    async def bulk(updates, *, session, expected_revision_id, source):
        assert updates == {key: effective[key] for key in cleanup.DEPENDENCIES}
        assert expected_revision_id == 7 and source == cleanup.SOURCE
        assert "delete" not in events
        events.append("dependencies")

    async def execute(statement, params=None):
        if isinstance(statement, Delete):
            assert statement.table.name == "settings"
            events.append("delete")
        elif isinstance(statement, Insert):
            assert statement.table.name == "settings"
            assert params == list(baseline.rows)
            events.append("insert")
        else:
            pytest.fail(f"unexpected mutation {statement}")

    async def revise(session, values, *, expected_revision_id, source, changed_keys):
        assert values == effective and expected_revision_id == 7
        assert changed_keys == (["timezone"] if timezone else [])
        assert source == cleanup.SOURCE
        assert events[-1] == "settings"
        events.append("publish")
        return current

    async def truncate(connection):
        events.append("truncate")

    session = SimpleNamespace(
        execute=execute,
        flush=AsyncMock(),
        expire_all=lambda: events.append("expire"),
        connection=AsyncMock(return_value="outer connection"),
    )
    monkeypatch.setattr(cleanup.house_contract, "_lock_contract", lock)
    monkeypatch.setattr(cleanup.settings_service, "get_all_settings", settings)
    monkeypatch.setattr(cleanup.settings_service, "set_settings_bulk", bulk)
    monkeypatch.setattr(cleanup.house_contract, "revise_contract", revise)
    monkeypatch.setattr(cleanup, "assert_ordinary_contract_consistent", consistent)
    monkeypatch.setattr(cleanup, "_house_boundary", AsyncMock(side_effect=[before, after]))
    monkeypatch.setattr(cleanup, "_settings_rows", AsyncMock(return_value=baseline.rows))
    assert await cleanup.restore_ordinary_baseline(session, baseline, truncate) == after
    assert events == [
        "lock",
        "settings",
        "validate",
        "dependencies",
        "delete",
        "insert",
        "expire",
        "settings",
        "publish",
        "truncate",
        "expire",
        "settings",
        "validate",
    ]
    assert baseline.boundary is before, "uncommitted boundary must not advance"


@pytest.mark.parametrize(
    "failure", ["guard", "dependencies", "rows", "revision", "truncate", "commit", "cancel", None]
)
async def test_one_outer_transaction_poison_on_failure_and_advance_only_after_commit(
    monkeypatch, failure
):
    events = []
    baseline = cleanup.OrdinaryBaseline((), {}, boundary())
    original = baseline.boundary
    candidate = boundary(revision=9)

    class Transaction:
        async def __aenter__(self):
            events.append("begin")
            return "connection"

        async def __aexit__(self, kind, error, tb):
            assert baseline.boundary is original
            events.append("rollback" if kind else "commit")
            if failure == "commit":
                raise RuntimeError("commit")

    class Session:
        def __init__(self, **kwargs):
            assert kwargs == dict(
                bind="connection", expire_on_commit=False, join_transaction_mode="rollback_only"
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def guard(connection):
        events.append("guard")
        if failure == "guard":
            raise RuntimeError("guard")

    async def restore(session, baseline, truncate):
        for phase in ("dependencies", "rows", "revision", "truncate"):
            events.append(phase)
            if failure == phase:
                raise RuntimeError(phase)
        if failure == "cancel":
            raise asyncio.CancelledError
        return candidate

    monkeypatch.setattr(cleanup, "AsyncSession", Session)
    monkeypatch.setattr(cleanup, "restore_ordinary_baseline", restore)
    engine = SimpleNamespace(begin=Transaction)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert baseline.boundary is original and baseline.poisoned
        with pytest.raises(AssertionError, match="unusable"):
            await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert events.count("begin") == 1
    else:
        await cleanup.reset_ordinary_profile(engine, baseline, guard=guard, truncate=None)
        assert baseline.boundary is candidate and not baseline.poisoned
        assert events[-1] == "commit"

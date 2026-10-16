"""P17: real queried ORM rows expire on rollback, while planning values survive."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm.exc import DetachedInstanceError

from packages.core import database
from packages.core.device_status_values import DeviceStatusSnapshot
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    ShowerEventRecord,
    SpaceHeatingGateRecord,
    WeatherRecord,
)
from packages.core.plan_outcome import observed_rate_baseline
from packages.optimizer import data_access
from packages.optimizer.rules_engine import RulesOptimizer
from tests.planner_orm_support import (
    DEVICE,
    UTC,
    consumption_rows,
    isolate_model_evidence,
    planning_rows,
)
from tests.test_g03_readonly_orm_regressions import orm_db as orm_db


@pytest.fixture
def planner_db(orm_db, monkeypatch):
    for model in (
        ConsumptionRecord,
        PriceRecord,
        WeatherRecord,
        ShowerEventRecord,
        PlanRecord,
        PlanActionRecord,
    ):
        model.__table__.create(orm_db.engine)
    factory = database.async_session_factory

    def facade(**kwargs):
        session = factory(**kwargs)
        session.info = {}
        event.listen(
            session.sync, "loaded_as_persistent", lambda sync, row: session.rows.append(row)
        )

        async def scalars(statement):
            return (await session.execute(statement)).scalars()

        session.scalars = scalars
        session.add = session.sync.add
        session.flush = AsyncMock(side_effect=session.sync.flush)

        def assign_ids(sync, *_args):
            for index, row in enumerate(sync.new, start=1):
                if isinstance(row, (PlanRecord, PlanActionRecord)) and row.id is None:
                    row.id = index

        event.listen(session.sync, "before_flush", assign_ids)
        return session

    monkeypatch.setattr(database, "async_session_factory", facade)
    isolate_model_evidence(monkeypatch)
    return orm_db


def assert_expired(db, model):
    rows = [
        row
        for session in db.sessions
        if session.options
        for row in session.rows
        if isinstance(row, model)
    ]
    assert rows
    for row in rows:
        assert inspect(row).detached
        assert inspect(row).expired_attributes
        with pytest.raises(DetachedInstanceError):
            getattr(row, inspect(row).mapper.primary_key[0].key)
    for session in db.sessions:
        assert session.closed
        if session.options:
            assert session.options[0]["postgresql_readonly"] is True
            assert session.timeouts == ["SET LOCAL statement_timeout = '2s'"]
            assert session.rollbacks == 1
            assert session.commits == 0


async def test_status_contract_and_nested_provenance_after_rollback(planner_db):
    planner_db.seed(*planning_rows())
    async with database.get_session(read_only=True) as session:
        row = (await session.execute(select(DeviceStatusRecord))).scalar_one()
        assert inspect(row).persistent
        source = row.native_policy_provenance
        expected = deepcopy(
            {field.name: getattr(row, field.name) for field in fields(DeviceStatusSnapshot)}
        )
        snapshot = await data_access.get_last_status(session)
        source["diagnostic_assumptions"]["auxiliary_power_kw"] = 99
    assert isinstance(snapshot, DeviceStatusSnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.tank_temp = 99
    assert {field.name for field in fields(snapshot)} == {
        "device_id",
        "ts",
        "outdoor_temp",
        "heat_pump_outdoor_temp",
        "holiday_mode",
        "tank_temp",
        "tank_target_temp",
        "tank_heat_min",
        "tank_heat_max",
        "zone1_temp",
        "zone1_target_temp",
        "zone1_heat_min",
        "zone1_heat_max",
        "quiet_mode",
        "special_status",
        "special_status_supported",
        "operation_status",
        "tank_operation_status",
        "zone1_operation_status",
        "device_action",
        "mode",
        "force_dhw",
        "native_policy_provenance",
    }
    assert {field.name: getattr(snapshot, field.name) for field in fields(snapshot)} == expected
    assert snapshot.native_policy_provenance["diagnostic_assumptions"]["auxiliary_power_kw"] == 0
    assert_expired(planner_db, DeviceStatusRecord)


@pytest.mark.parametrize("gate", ["valid", "absent", "mismatch"])
async def test_rules_populated_status_and_gate_survive_rollback(planner_db, monkeypatch, gate):
    from packages.optimizer import rules_engine

    planner_db.seed(*planning_rows(gate=gate))
    captured = []
    real = rules_engine.resolve_effective_gate

    def resolve(row, config):
        assert row is None or inspect(row, raiseerr=False) is None
        evidence = real(row, config)
        captured.append(evidence)
        return evidence

    monkeypatch.setattr(rules_engine, "resolve_effective_gate", resolve)
    plan = await RulesOptimizer().generate_plan()
    assert plan is not None
    assert plan["version"] == "rules_v8"
    assert captured[0].state == ("ALLOWED" if gate == "valid" else "UNKNOWN")
    assert (
        captured[0].reason_code
        == {
            "valid": "p17_allowed",
            "absent": "gate_missing",
            "mismatch": "config_fingerprint_mismatch",
        }[gate]
    )
    assert_expired(planner_db, DeviceStatusRecord)
    if gate != "absent":
        assert_expired(planner_db, SpaceHeatingGateRecord)


async def test_milp_populated_plan_persists_without_rules_fallback(planner_db, monkeypatch):
    from packages.optimizer import main

    planner_db.seed(*planning_rows(layer="milp_preferred"))
    fallback = Mock(side_effect=AssertionError("MILP must not fall back"))
    monkeypatch.setattr(main, "_new_rules_optimizer", fallback)
    plan_id = await main.run_optimization()
    assert plan_id is not None
    async with database.get_session(read_only=True) as session:
        record = (
            await session.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        ).scalar_one()
        assert record.optimizer_version in {"milp_v2", "milp_v2+ml"}
        saved = json.loads(record.plan_json)
        assert saved["actions"] == []
        assert saved["diagnostic"] is True
        assert saved["input_provenance"]["execution_policy"] == "observe_only_build"
        policy = await main.get_optimizer_status_snapshot("milp_preferred", reporting_inputs={})
        assert policy["ordinary_writes_allowed"] is False
        assert policy["control_ready"] is False
        assert policy["build_policy"] == "observe_only"
        assert not (await session.execute(select(PlanActionRecord))).scalars().all()
    fallback.assert_not_called()
    assert_expired(planner_db, DeviceStatusRecord)
    assert_expired(planner_db, SpaceHeatingGateRecord)


@pytest.mark.parametrize("timezone", ["Europe/Stockholm", "UTC"])
async def test_baseline_source_day_timezone_survive_rollback(planner_db, timezone):
    now = dt.datetime.now(UTC)
    rows = consumption_rows(now, timezone=timezone)
    prices = [(now + dt.timedelta(hours=h), 0.5) for h in range(2)]
    expected = observed_rate_baseline(rows, prices, device_id=DEVICE, timezone_name="UTC")
    assert expected["unshifted_baseline_cost"] == 2.0
    planner_db.seed(*rows)
    result = await RulesOptimizer()._estimate_unshifted_baseline(prices, device_id=DEVICE, now=now)
    assert result == expected
    assert result["baseline_unavailable_reason"] is None
    assert_expired(planner_db, ConsumptionRecord)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("empty", "no_confirmed_intervals"),
        ("device", "device_unavailable"),
        ("query", "source_unavailable"),
        ("budget", "source_row_budget_exceeded"),
    ],
)
async def test_baseline_genuine_refusals_remain_distinct(planner_db, case, reason):
    now = dt.datetime.now(UTC)
    if case == "query":
        planner_db.injection.error = RuntimeError("owned query failure")
        planner_db.injection.select_number = 1
    if case == "budget":
        planner_db.seed(
            *(
                ConsumptionRecord(device_id=DEVICE, ts=now - dt.timedelta(seconds=i), heat_kwh=1)
                for i in range(20001)
            )
        )
    result = await RulesOptimizer()._estimate_unshifted_baseline(
        [(now, 0.5)],
        device_id=None if case == "device" else DEVICE,
        now=now,
    )
    assert result["baseline_unavailable_reason"] == reason
    if case == "budget":
        assert_expired(planner_db, ConsumptionRecord)

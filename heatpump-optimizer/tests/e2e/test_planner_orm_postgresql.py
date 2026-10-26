"""Owned-PostgreSQL P17 acceptance: populated planning, accounting and safe persistence.

Uses the owned database guard/cleanup fixtures, never an arbitrary local database.
Model evidence alone is isolated; production queries, rollback, gate projection,
represented-strategy validation, MILP solving and plan persistence remain real.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import func, inspect, select, text

from packages.core import database
from packages.core.models import ConsumptionRecord, PlanActionRecord, PlanRecord, SettingRecord
from packages.core.plan_outcome import observed_rate_baseline
from packages.optimizer import main, milp, rules_engine
from packages.optimizer.rules_engine import RulesOptimizer
from tests.e2e.ordinary_cleanup import (
    _house_boundary,
    _settings_rows,
    assert_ordinary_contract_consistent,
)
from tests.planner_orm_support import (
    DEVICE,
    UTC,
    consumption_rows,
    isolate_model_evidence,
    planning_rows,
)


async def _planning_boundary():
    from packages.core.settings_service import get_all_settings

    async with database.get_session(read_only=True) as session:
        await assert_ordinary_contract_consistent(session, await get_all_settings(session=session))
        return await _settings_rows(session), await _house_boundary(session)


async def _seed_planning_rows(db_session, *, layer="rules_only", gate="valid"):
    """Version settings and evidence together; release locks before planning."""
    from packages.core.house_contract import ensure_current_contract
    from packages.core.settings_service import SETTINGS_SCHEMA, set_settings_bulk

    rows = planning_rows(layer=layer, gate=gate)
    values = {row.key: row.value for row in rows if isinstance(row, SettingRecord)}
    assert set(values) <= set(SETTINGS_SCHEMA)
    current = await ensure_current_contract(db_session)
    await set_settings_bulk(
        values,
        session=db_session,
        expected_revision_id=current.id,
        source="p17_planning_fixture",
    )
    db_session.add_all(row for row in rows if not isinstance(row, SettingRecord))
    await db_session.commit()
    return await _planning_boundary()


@pytest.mark.asyncio(loop_scope="session")
class TestP17ReadOnlyPlanning:
    @pytest.mark.parametrize("gate", ["valid", "absent", "mismatch"])
    async def test_populated_rules_status_and_gate(self, db_session, monkeypatch, gate):
        boundary = await _seed_planning_rows(db_session, gate=gate)
        isolate_model_evidence(monkeypatch)
        captured = []
        original = rules_engine.resolve_effective_gate

        def resolve(snapshot, config):
            assert snapshot is None or inspect(snapshot, raiseerr=False) is None
            result = original(snapshot, config)
            captured.append(result)
            return result

        monkeypatch.setattr(rules_engine, "resolve_effective_gate", resolve)
        plan = await RulesOptimizer().generate_plan()
        assert plan is not None
        assert plan["version"] == "rules_v8"
        assert plan["device_id"] == DEVICE
        assert captured[0].state == ("ALLOWED" if gate == "valid" else "UNKNOWN")
        assert (
            captured[0].reason_code
            == {
                "valid": "p17_allowed",
                "absent": "gate_missing",
                "mismatch": "config_fingerprint_mismatch",
            }[gate]
        )
        assert await _planning_boundary() == boundary

    @pytest.mark.parametrize("gate", ["valid", "absent", "mismatch"])
    async def test_milp_diagnostic_persists_without_fallback_or_actions(
        self,
        db_session,
        monkeypatch,
        gate,
    ):
        boundary = await _seed_planning_rows(db_session, layer="milp_preferred", gate=gate)
        isolate_model_evidence(monkeypatch)
        captured = []
        original = milp.resolve_effective_gate

        def resolve(snapshot, config):
            assert snapshot is None or inspect(snapshot, raiseerr=False) is None
            result = original(snapshot, config)
            captured.append(result)
            return result

        monkeypatch.setattr(milp, "resolve_effective_gate", resolve)
        fallback = Mock(side_effect=AssertionError("MILP must not fall back to rules"))
        monkeypatch.setattr(main, "_new_rules_optimizer", fallback)
        before = await db_session.scalar(select(func.count()).select_from(PlanActionRecord))
        plan_id = await main.run_optimization()
        assert plan_id is not None
        async with database.get_session(read_only=True) as session:
            record = (
                await session.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
            ).scalar_one()
            assert record.optimizer_version in {"milp_v2", "milp_v2+ml"}
            assert record.status == "diagnostic"
            assert record.status_reason == "observe_only_build"
            saved = json.loads(record.plan_json)
            assert saved["actions"] == []
            assert saved["diagnostic"] is True
            assert saved["input_provenance"]["execution_policy"] == "observe_only_build"
            assert saved["represented_strategy"]["device_id"] == DEVICE
            assert saved["represented_strategy"]["initial_tank_c"] == 50.0
            after = await session.scalar(select(func.count()).select_from(PlanActionRecord))
            assert after == before
        fallback.assert_not_called()
        assert captured[0].state == ("ALLOWED" if gate == "valid" else "UNKNOWN")
        policy = await main.get_optimizer_status_snapshot("milp_preferred", reporting_inputs={})
        assert policy["ordinary_writes_allowed"] is False
        assert policy["control_ready"] is False
        assert policy["build_policy"] == "observe_only"
        assert await _planning_boundary() == boundary

    @pytest.mark.parametrize("timezone", ["Europe/Stockholm", "UTC"])
    async def test_counter_source_day_and_timezone_reset(self, db_session, timezone):
        now = dt.datetime.now(UTC)
        rows = consumption_rows(now, timezone=timezone)
        prices = [(now + dt.timedelta(hours=h), 0.5) for h in range(2)]
        expected = observed_rate_baseline(rows, prices, device_id=DEVICE, timezone_name="UTC")
        assert expected["unshifted_baseline_cost"] == 2.0
        db_session.add_all(rows)
        await db_session.commit()
        result = await RulesOptimizer()._estimate_unshifted_baseline(
            prices,
            device_id=DEVICE,
            now=now,
        )
        assert result == expected
        assert result["baseline_unavailable_reason"] is None

    @pytest.mark.parametrize(
        "case,reason",
        [
            ("empty", "no_confirmed_intervals"),
            ("device", "device_unavailable"),
            ("query", "source_unavailable"),
            ("budget", "source_row_budget_exceeded"),
        ],
    )
    async def test_genuine_baseline_refusals(self, db_session, monkeypatch, case, reason):
        now = dt.datetime.now(UTC)
        if case == "budget":
            db_session.add_all(
                ConsumptionRecord(
                    device_id=DEVICE,
                    ts=now - dt.timedelta(seconds=i),
                    heat_kwh=1,
                )
                for i in range(20001)
            )
            await db_session.commit()
        if case == "query":

            @asynccontextmanager
            async def query_failure(**kwargs):
                async with database.get_session(**kwargs) as session:
                    await session.execute(text("SELECT 1 / 0"))
                    yield session

            monkeypatch.setattr(rules_engine, "get_session", query_failure)
        result = await RulesOptimizer()._estimate_unshifted_baseline(
            [(now, 0.5)],
            device_id=None if case == "device" else DEVICE,
            now=now,
        )
        assert result["baseline_unavailable_reason"] == reason

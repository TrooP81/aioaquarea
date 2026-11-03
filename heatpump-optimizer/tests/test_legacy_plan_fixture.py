"""Offline regression for the plan INSERT used against the genuine 031 schema."""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause


def test_031_plan_fixture_uses_only_explicit_predecessor_columns():
    source = Path(__file__).parent / "e2e/test_manual_cop_postgresql.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_data_bearing_031_upgrade_and_flag_rollback_preserve_retained_state"
    )
    assignment = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "plan_id" for target in node.targets)
    )
    execute = next(
        node
        for node in ast.walk(assignment.value)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    )
    # Evaluate the real fixture arguments without importing E2E environment/config
    # code. No ORM symbols are provided: any future ORM-default dependency fails.
    stamp = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)
    namespace = {"text": text, "dt": dt, "stamp": stamp, "__builtins__": {}}
    statement, values = (
        eval(compile(ast.Expression(argument), str(source), "eval"), namespace)
        for argument in execute.args
    )
    assert isinstance(statement, TextClause)
    assert values == {
        "created_at": stamp,
        "horizon_start": stamp,
        "horizon_end": stamp + dt.timedelta(days=1),
        "plan_json": '{"fixture":true}',
        "optimizer_version": "rules_v8",
        "input_provenance_json": '{"cop_source":"assumed_planning_curve"}',
        "price_currency": "SEK",
        "price_source": "manual",
        "cost_estimate_eur": 12.5,
        "status": "superseded",
    }
    compiled = statement.compile(dialect=postgresql.dialect())
    assert set(compiled.params) == set(values)
    assert " ".join(statement.text.split()) == (
        "INSERT INTO plans (created_at,horizon_start,horizon_end,plan_json,"
        "optimizer_version,input_provenance_json,price_currency,price_source,"
        "cost_estimate_eur,status) VALUES (:created_at,:horizon_start,"
        ":horizon_end,:plan_json,:optimizer_version,:input_provenance_json,"
        ":price_currency,:price_source,:cost_estimate_eur,:status) RETURNING id"
    )
    assert "contract_state" not in str(compiled)

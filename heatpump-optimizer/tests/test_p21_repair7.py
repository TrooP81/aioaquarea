from __future__ import annotations

from contextlib import asynccontextmanager
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.sql.elements import TextClause

from packages.core import comfort_schedule, house_contract
from packages.core.models import (
    HouseContractRevisionRecord,
    HouseInstallationRecord,
    PlanActionRecord,
    PlanRecord,
    SettingRecord,
)

UTC = dt.timezone.utc


class PersistedHistorySession:
    """Offline record store; settings loading and contract validation stay real."""

    def __init__(self, installation, revision, plan, settings):
        self.installation = installation
        self.revision = revision
        self.plan = plan
        self.settings = [SettingRecord(key=key, value=value) for key, value in settings.items()]
        self.lock_reads = 0
        self.setting_reads = 0

    async def get(self, model, key):
        if model is HouseInstallationRecord and key == 1:
            return self.installation
        if model is HouseContractRevisionRecord and key == self.revision.id:
            return self.revision
        if model is HouseContractRevisionRecord:
            return None
        raise AssertionError((model, key))

    async def execute(self, statement, parameters=None):
        if isinstance(statement, TextClause):
            assert "pg_advisory_xact_lock" in str(statement)
            self.lock_reads += 1
            return None
        model = statement.column_descriptions[0]["entity"]
        if model is SettingRecord:
            self.setting_reads += 1
            rows = self.settings
        elif model is PlanRecord:
            if len(statement.column_descriptions) == 2:
                return SimpleNamespace(all=lambda: [(self.plan, 0)])
            return SimpleNamespace(scalar_one_or_none=lambda: self.plan)
        elif model is PlanActionRecord:
            rows = []
        else:
            raise AssertionError(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.fixture
async def persisted_history(monkeypatch):
    monkeypatch.setattr(house_contract, "timezone_data_version", lambda: "tzdata:version-A")
    monkeypatch.setattr(comfort_schedule, "timezone_data_version", lambda: "tzdata:version-A")
    settings = {
        "timezone": "Europe/Stockholm",
        "comfort_schedule": '{"weekday":[2],"weekend":[2]}',
        "comfort_temp_min": "18",
        "comfort_temp_target": "21",
        "comfort_temp_max": "25",
    }
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    projection = house_contract.HousePlanningContract.from_settings(
        settings, UUID(int=1), UUID(int=2)
    ).to_projection()
    revision = HouseContractRevisionRecord(
        id=4,
        installation_id=UUID(int=1),
        schema_version=house_contract.SCHEMA_VERSION,
        contract_hash=house_contract._contract_hash(projection),
        contract_json=json.loads(json.dumps(projection)),
        effective_at=now,
        source="effective_settings_import",
    )
    installation = HouseInstallationRecord(
        singleton_id=1,
        installation_id=UUID(int=1),
        house_id=UUID(int=2),
        current_revision_id=revision.id,
        demand_dependency_generation=1,
    )
    plan = PlanRecord(
        id=71,
        created_at=now,
        horizon_start=now,
        horizon_end=now + dt.timedelta(hours=3),
        optimizer_version="rules_v8",
        price_source="entsoe",
        price_currency="EUR",
        input_provenance_json="{}",
        status="diagnostic",
        contract_revision_id=revision.id,
        contract_hash=revision.contract_hash,
        contract_state="current",
    )
    session = PersistedHistorySession(installation, revision, plan, settings)
    snapshot = await house_contract.snapshot_from_revision(
        session, revision.id, plan.horizon_start, plan.horizon_end
    )
    plan.plan_json = json.dumps({"house_contract_snapshot": snapshot})
    assert house_contract.validate_plan_contract_snapshot(
        plan, json.loads(plan.plan_json)["house_contract_snapshot"]
    )
    assert await house_contract.current_revision_matches(
        session, revision.id, revision.contract_hash
    )
    return session


@pytest.mark.parametrize("status", ["diagnostic", "active"])
@pytest.mark.parametrize("endpoint", ["history", "detail"])
async def test_history_survives_real_current_contract_tzdata_mismatch(
    monkeypatch, persisted_history, status, endpoint
):
    from packages.api.routers import optimizer as routes

    session = persisted_history
    session.plan.status = status
    saved_json = session.plan.plan_json
    revision_json = json.dumps(session.revision.contract_json, sort_keys=True)
    real_ensure = house_contract.ensure_current_contract
    monkeypatch.setattr(house_contract, "timezone_data_version", lambda: "tzdata:version-B")
    monkeypatch.setattr(comfort_schedule, "timezone_data_version", lambda: "tzdata:version-B")
    with pytest.raises(ValueError, match="planning settings changed without a matching"):
        await real_ensure(session)

    resolver = Mock(side_effect=AssertionError("must not reinterpret saved policy with version B"))
    measurement = Mock(side_effect=AssertionError("unavailable snapshots must not be measured"))
    monkeypatch.setattr(house_contract, "resolve_hourly_targets", resolver)
    monkeypatch.setattr(routes, "plan_measurement", measurement)

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(routes, "get_session", transaction)
    locks_before = session.lock_reads
    settings_before = session.setting_reads
    if endpoint == "history":
        result = await routes.get_plans(limit=10, include_diagnostics=status == "diagnostic")
        assert len(result) == 1
        response = result[0]
    else:
        response = await routes.get_plan_detail(
            session.plan.id, include_diagnostics=status == "diagnostic"
        )
        assert response.outcome["state"] == "unknown"
        assert response.outcome["reason"] == "historical_timezone_data_unavailable"
        for key in ("comfort", "cost", "weather_matched_comparison"):
            assert response.outcome[key]["status"] == "unknown"
        assert response.house_contract_snapshot == json.loads(saved_json)["house_contract_snapshot"]
    assert response.contract_snapshot_state == "timezone_data_unavailable"
    assert response.is_current is False
    assert response.eligible_for_current_presentation is False
    assert response.ordinary_writes_allowed is False
    assert session.lock_reads == locks_before + 2
    assert session.setting_reads == settings_before + 1
    assert house_contract.ensure_current_contract is real_ensure
    resolver.assert_not_called()
    measurement.assert_not_called()
    assert session.plan.plan_json == saved_json
    assert json.dumps(session.revision.contract_json, sort_keys=True) == revision_json
    assert session.installation.current_revision_id == session.revision.id
    with pytest.raises(ValueError, match="planning settings changed without a matching"):
        await house_contract.current_revision_matches(
            session, session.revision.id, session.revision.contract_hash
        )
    with pytest.raises(ValueError, match="planning settings changed without a matching"):
        await house_contract.freeze_contract_snapshot(
            session, session.plan.horizon_start, session.plan.horizon_end
        )


async def test_matching_history_retains_current_diagnostic_classification(
    monkeypatch, persisted_history
):
    from packages.api.routers import optimizer as routes

    @asynccontextmanager
    async def transaction():
        yield persisted_history

    monkeypatch.setattr(routes, "get_session", transaction)
    history = await routes.get_plans(limit=10, include_diagnostics=True)
    detail = await routes.get_plan_detail(persisted_history.plan.id, include_diagnostics=True)
    for response in (history[0], detail):
        assert response.contract_snapshot_state == "valid"
        assert response.is_current is True
        assert response.ordinary_writes_allowed is False
    assert detail.outcome["status"] == "diagnostic"


@pytest.mark.parametrize("endpoint", ["history", "detail"])
async def test_history_does_not_qualify_valid_snapshot_when_current_settings_drift(
    monkeypatch, persisted_history, endpoint
):
    from packages.api.routers import optimizer as routes

    session = persisted_history
    next(row for row in session.settings if row.key == "comfort_temp_min").value = "17"

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(routes, "get_session", transaction)
    if endpoint == "history":
        response = (await routes.get_plans(limit=10, include_diagnostics=True))[0]
    else:
        response = await routes.get_plan_detail(session.plan.id, include_diagnostics=True)
    assert response.contract_snapshot_state == "valid"
    assert response.is_current is False
    assert response.ordinary_writes_allowed is False


@pytest.mark.parametrize("endpoint", ["history", "detail"])
async def test_history_does_not_hide_unrelated_current_pointer_failure(
    monkeypatch, persisted_history, endpoint
):
    from packages.api.routers import optimizer as routes

    session = persisted_history
    session.installation.current_revision_id = 99

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(routes, "get_session", transaction)
    with pytest.raises(ValueError, match="pointer does not reference a revision"):
        if endpoint == "history":
            await routes.get_plans(limit=10, include_diagnostics=True)
        else:
            await routes.get_plan_detail(session.plan.id, include_diagnostics=True)

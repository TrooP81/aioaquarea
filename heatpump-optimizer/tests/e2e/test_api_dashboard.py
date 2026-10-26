"""E2E tests: Health and Dashboard endpoints."""

import json

import pytest
import pytest_asyncio
from httpx import AsyncClient
from packages.core.database import get_session
from packages.core.models import (
    DeviceStatusRecord,
    IndoorTempReading,
    OverrideRecord,
    PlanRecord,
    SpaceHeatingGateRecord,
)
from packages.core.optimizer_control_state import ControlStateOverrideUnavailableError
from packages.core.space_heating_gate import HeatingGateConfig
from tests.e2e.ordinary_cleanup import (
    _house_boundary,
    _settings_rows,
    assert_ordinary_contract_consistent,
)
import datetime as dt


async def _recorded_boundary(session):
    """Materialize values, not ORM identities that a request rollback can expire."""
    from packages.core.settings_service import get_all_settings

    await assert_ordinary_contract_consistent(session, await get_all_settings(session=session))
    return await _settings_rows(session), await _house_boundary(session)


@pytest.mark.asyncio(loop_scope="session")
class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "db": "connected"}


@pytest.mark.asyncio(loop_scope="session")
class TestDashboard:
    async def test_control_state_returns_503_when_active_override_confirmation_fails(
        self, client: AsyncClient, monkeypatch
    ):
        from packages.api.routers import dashboard

        async def unavailable(*_args, **_kwargs):
            raise ControlStateOverrideUnavailableError()

        monkeypatch.setattr(dashboard, "resolve_control_state", unavailable)

        response = await client.get("/api/control-state")

        assert response.status_code == 503
        assert response.json() == {"detail": "Control state unavailable"}

    async def test_overlapping_overrides_select_highest_id_for_dashboard_and_control_state(
        self, client: AsyncClient
    ):
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            session.add_all(
                [
                    OverrideRecord(
                        ts_from=now - dt.timedelta(hours=1),
                        ts_to=now + dt.timedelta(hours=2),
                        action_type="pause_all",
                        reason="newer start, lower id",
                        active=True,
                    ),
                    OverrideRecord(
                        ts_from=now - dt.timedelta(hours=3),
                        ts_to=now + dt.timedelta(hours=1),
                        action_type="pause_all",
                        reason="earlier start, higher id",
                        active=True,
                    ),
                ]
            )
            await session.flush()
            highest_id = (
                await session.execute(
                    __import__("sqlalchemy")
                    .select(OverrideRecord.id)
                    .order_by(OverrideRecord.id.desc())
                    .limit(1)
                )
            ).scalar_one()

        dashboard, control_state = (
            await client.get("/api/dashboard"),
            await client.get("/api/control-state"),
        )

        assert dashboard.status_code == control_state.status_code == 200
        assert dashboard.json()["has_override"] is True
        assert dashboard.json()["override_id"] == control_state.json()["override_id"] == highest_id

    async def test_dashboard_empty_state(self, client: AsyncClient):
        """Dashboard returns valid response even with no data."""
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_status"] is None
        assert data["current_status_fresh"] is False
        assert data["current_status_age_seconds"] is None
        assert data["current_price"] is None
        assert data["today_kwh"] == 0
        # P1.6a design lines 46-51: no evidence is unavailable, not complete zero.
        assert data["today_cost_eur"] is None
        assert data["today_telemetry_status"] == "unavailable"
        assert data["today_cost_incomplete_reason"] == "telemetry_unavailable"
        assert data["today_cost_complete"] is False
        assert data["today_cost_coverage_pct"] is None
        assert data["active_plan"] is None
        assert data["has_override"] is False

    async def test_dashboard_with_device_status(self, client: AsyncClient, seed_device_status):
        """Dashboard shows current device status."""
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_status"] is not None
        assert data["current_status"]["device_id"] == "test-device-001"
        assert data["current_status"]["mode"] == "heat"
        assert data["current_status"]["outdoor_temp"] == 5.0
        assert data["current_status"]["tank_temp"] == 48.5
        assert data["current_status_fresh"] is True
        assert data["current_status_age_seconds"] is not None

    async def test_dashboard_with_prices(
        self, client: AsyncClient, seed_device_status, seed_prices
    ):
        """Dashboard shows current electricity price."""
        resp = await client.get("/api/dashboard")
        data = resp.json()
        # Current price should be one of our seeded values
        if data["current_price"] is not None:
            assert 0.01 <= data["current_price"] <= 0.50

    async def test_dashboard_with_consumption(
        self, client: AsyncClient, seed_device_status, seed_consumption, seed_prices
    ):
        """Dashboard shows today's consumption."""
        resp = await client.get("/api/dashboard")
        data = resp.json()
        assert data["today_kwh"] > 0
        # P1.6a design lines 46-49: flat channels and a partial day cannot
        # certify total energy, even when every confirmed delta has a price.
        assert data["today_cost_eur"] is None
        assert data["today_cost_complete"] is False
        assert data["today_telemetry_status"] == "incomplete"
        assert data["today_cost_incomplete_reason"] == "telemetry_incomplete"
        assert data["today_cost_unpriced_kwh"] == 0
        assert data["today_cost_priced_kwh"] == data["today_kwh"]
        assert data["today_cost_coverage_pct"] == 100
        assert data["today_cost_priced_amount"] > 0

    async def test_dashboard_with_active_plan(
        self, client: AsyncClient, db_session, seed_device_status, seed_plan
    ):
        """Dashboard shows active optimizer plan."""
        from packages.core.house_contract import (
            snapshot_from_revision,
            validate_plan_contract_snapshot,
        )

        persisted = await db_session.get(PlanRecord, seed_plan.id, populate_existing=True)
        snapshot = json.loads(persisted.plan_json)["house_contract_snapshot"]
        assert persisted.contract_state == "current"
        assert validate_plan_contract_snapshot(persisted, snapshot)
        assert snapshot == await snapshot_from_revision(
            db_session,
            persisted.contract_revision_id,
            persisted.horizon_start,
            persisted.horizon_end,
        )
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_plan"] is not None
        assert data["active_plan"]["id"] == seed_plan.id
        assert data["active_plan"]["optimizer_version"] == "rules_v1"
        assert data["active_plan"]["cost_estimate_eur"] == 2.85

        detail = await client.get(f"/api/plans/{seed_plan.id}")
        assert detail.status_code == 200
        assert detail.json()["house_contract_snapshot"] == snapshot
        assert detail.json()["contract_snapshot_state"] == "valid"
        assert detail.json()["contract_revision_id"] == persisted.contract_revision_id
        assert detail.json()["contract_hash"] == persisted.contract_hash

    async def test_dashboard_excludes_new_unbound_legacy_plan(
        self, client: AsyncClient, db_session, seed_plan
    ):
        """Prove visibility first, then exclude the sole active unbound legacy plan."""
        before = await client.get("/api/dashboard")
        assert before.status_code == 200
        assert before.json()["active_plan"]["id"] == seed_plan.id
        now = dt.datetime.now(dt.timezone.utc)
        control_provenance = (
            seed_plan.contract_revision_id,
            seed_plan.contract_hash,
            seed_plan.contract_state,
            seed_plan.plan_json,
        )
        # Synthetic setup must respect ux_plans_one_active: retire the positive
        # control in its own commit before constructing the active legacy row.
        seed_plan.status = "superseded"
        seed_plan.status_reason = "legacy_exclusion_test_control"
        seed_plan.superseded_at = now
        await db_session.commit()
        await db_session.refresh(seed_plan)
        assert seed_plan.status == "superseded"
        assert (
            seed_plan.contract_revision_id,
            seed_plan.contract_hash,
            seed_plan.contract_state,
            seed_plan.plan_json,
        ) == control_provenance
        without_active = await client.get("/api/dashboard")
        assert without_active.status_code == 200
        assert without_active.json()["active_plan"] is None

        legacy = PlanRecord(
            created_at=now,
            horizon_start=now,
            horizon_end=now + dt.timedelta(hours=24),
            plan_json=json.dumps({"version": "rules_v1", "actions": []}),
            optimizer_version="rules_v1",
            cost_estimate_eur=2.85,
        )
        db_session.add(legacy)
        await db_session.commit()
        await db_session.refresh(legacy)
        assert legacy.status == "active"
        assert legacy.contract_state == "legacy_unknown"
        assert legacy.contract_revision_id is None
        assert legacy.contract_hash is None
        legacy_provenance = (
            legacy.contract_revision_id,
            legacy.contract_hash,
            legacy.contract_state,
            legacy.plan_json,
            legacy.input_provenance_json,
        )

        response = await client.get("/api/dashboard")
        assert response.status_code == 200
        assert response.json()["active_plan"] is None
        detail = await client.get(f"/api/plans/{legacy.id}")
        assert detail.status_code == 200
        historical = detail.json()
        assert historical["id"] == legacy.id
        assert historical["contract_state"] == "legacy_unknown"
        assert historical["contract_revision_id"] is None
        assert historical["contract_hash"] is None
        assert historical["house_contract_snapshot"] == {}
        assert historical["contract_snapshot_state"] == "unknown"
        assert historical["outcome"]["state"] == "unknown"
        assert historical["ordinary_writes_allowed"] is False
        await db_session.refresh(legacy)
        assert legacy.status == "active"
        assert (
            legacy.contract_revision_id,
            legacy.contract_hash,
            legacy.contract_state,
            legacy.plan_json,
            legacy.input_provenance_json,
        ) == legacy_provenance
        await db_session.refresh(seed_plan)
        assert seed_plan.status == "superseded"
        assert (
            seed_plan.contract_revision_id,
            seed_plan.contract_hash,
            seed_plan.contract_state,
            seed_plan.plan_json,
        ) == control_provenance

    async def test_dashboard_with_override(
        self, client: AsyncClient, seed_device_status, seed_override
    ):
        """Dashboard detects active override."""
        resp = await client.get("/api/dashboard")
        data = resp.json()
        assert data["has_override"] is True

    async def test_outcome_summary_uses_measured_period_data(
        self, client: AsyncClient, seed_consumption, seed_prices
    ):
        resp = await client.get("/api/outcomes/summary?days=1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["days"] == 1
        assert data["cost"]["measured_kwh"] >= 0
        assert "baseline_method" in data


@pytest.mark.asyncio(loop_scope="session")
class TestG03ReadOnlyRoutes:
    """Committed PostgreSQL rows must survive read-only rollback as API values.

    Keep the production sessions, settings readers and condition evaluators:
    replacing them with non-expiring fakes would conceal the G03 regression.
    The ordinary E2E ownership/cleanup fixtures apply to every case.
    """

    @pytest_asyncio.fixture(loop_scope="session")
    async def recorded_settings(self, db_session):
        from packages.core.house_contract import ensure_current_contract
        from packages.core.settings_service import set_settings_bulk

        values = {
            "aquarea_username": "g03-offline-test",
            "aquarea_password": "g03-offline-test",
            "device_status_max_age_minutes": "15",
            "poll_interval_seconds": "300",
            "smartthings_device_ids": "g03-room",
            "comfort_temp_target": "21.5",
            "outcome_experiments_enabled": "true",
            "outcome_experiment_max_curve_step_c": "0.5",
            "_heat_curve_verification_state": '{"status": "verified"}',
        }
        current = await ensure_current_contract(db_session)
        await set_settings_bulk(
            values,
            session=db_session,
            expected_revision_id=current.id,
            source="g03_read_only_fixture",
        )
        await db_session.commit()

    async def seed_heating_context(self, db_session, *, indoor):
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        config = HeatingGateConfig()
        db_session.add_all(
            [
                DeviceStatusRecord(
                    device_id="g03-device",
                    ts=now,
                    outdoor_temp=5.0,
                    device_action="HEATING",
                    space_heating_active=True,
                    space_heating_evidence="compressor_heating",
                    defrost_active=False,
                ),
                SpaceHeatingGateRecord(
                    device_id="g03-device",
                    state="ALLOWED",
                    config_fingerprint=config.fingerprint,
                    base_c=config.base_c,
                    on_threshold_c=config.on_threshold_c,
                    off_threshold_c=config.off_threshold_c,
                    last_raw_outdoor_c=5.0,
                    reason_code="g03_test_allowed",
                    source_status_ts=now,
                    evaluated_at=now,
                ),
            ]
        )
        if indoor:
            db_session.add(
                IndoorTempReading(
                    timestamp=now, device_id="g03-room", temperature=18.0, is_stale=False
                )
            )
        # The endpoint must load its own persistent objects in a different session.
        await db_session.commit()

    @pytest.mark.parametrize(
        ("case", "expected_status"),
        [
            ("populated", "too_cold"),
            ("missing_indoor", "insufficient_data"),
            ("empty", "not_controllable"),
        ],
    )
    async def test_heat_curve_advice_after_read_only_session(
        self, client: AsyncClient, db_session, recorded_settings, case, expected_status
    ):
        if case != "empty":
            await self.seed_heating_context(db_session, indoor=case == "populated")

        before = await _recorded_boundary(db_session)
        response = await client.get("/api/thermal/heat-curve-advice")
        assert await _recorded_boundary(db_session) == before

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == expected_status
        assert data["manual_only"] is True
        assert data["verification"]["status"] == "verified"
        assert data["verification"]["recommendation_available"] is True
        assert data["readings"] == {
            "indoor_temp_c": 18.0 if case == "populated" else None,
            "comfort_target_c": 21.5,
            "outdoor_temp_c": 5.0 if case != "empty" else None,
            "curve_supply_target_c": 47.0 if case != "empty" else None,
            "controller_heating_enabled": True if case != "empty" else None,
        }
        assert data["gate_state"] == ("UNKNOWN" if case == "empty" else "ALLOWED")
        assert data["reasons"]
        if case == "populated":
            assert data["gate_reason"] == "g03_test_allowed"
            assert data["indoor_error_c"] == -3.5
            assert data["current"]["supply_cold_c"] == 47.0
            assert data["suggested"]["supply_cold_c"] == 49.0
            assert data["suggested"]["supply_warm_c"] == 25.0
        else:
            assert data["suggested"] is None

    @pytest.mark.parametrize("has_data", [True, False], ids=["populated", "empty"])
    async def test_outcome_experiment_after_read_only_session(
        self, client: AsyncClient, db_session, recorded_settings, has_data
    ):
        if has_data:
            await self.seed_heating_context(db_session, indoor=False)

        before = await _recorded_boundary(db_session)
        response = await client.get("/api/outcomes/summary?days=1")
        assert await _recorded_boundary(db_session) == before

        assert response.status_code == 200
        data = response.json()
        assert data["days"] == 1
        assert "baseline_method" in data
        experiment = data["experiment"]
        assert experiment["enabled"] is True
        assert experiment["mode"] == "manual_review_only"
        assert experiment["maximum_curve_step_c"] == 0.5
        assert experiment["status"] == (
            "manual_review_ready" if has_data else "waiting_for_heating_conditions"
        )
        conditions = experiment["conditions"]
        assert conditions["ready"] is has_data
        assert conditions["outdoor_temp_c"] == (5.0 if has_data else None)
        assert conditions["reason"] == (
            "heating_conditions_available"
            if has_data
            else "waiting_for_current_outdoor_temperature"
        )
        if has_data:
            assert conditions["space_heating_confirmed"] is True
        assert len(experiment["guardrails"]) == 4
        assert experiment["guardrails"][0] == "No heat-pump command is created by this feature."

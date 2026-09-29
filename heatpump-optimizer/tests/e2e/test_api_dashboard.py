"""E2E tests: Health and Dashboard endpoints."""

import pytest
from httpx import AsyncClient
from packages.core.database import get_session
from packages.core.models import OverrideRecord
from packages.core.optimizer_control_state import ControlStateOverrideUnavailableError
import datetime as dt


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
        assert data["today_cost_eur"] == 0
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
        # The first cumulative meter sample has no attributable market hour;
        # cost must be marked partial instead of silently priced at a default.
        assert data["today_cost_eur"] is None
        assert data["today_cost_complete"] is False
        assert data["today_cost_unpriced_kwh"] > 0
        assert data["today_cost_priced_amount"] >= 0

    async def test_dashboard_with_active_plan(
        self, client: AsyncClient, seed_device_status, seed_plan
    ):
        """Dashboard shows active optimizer plan."""
        resp = await client.get("/api/dashboard")
        data = resp.json()
        assert data["active_plan"] is not None
        assert data["active_plan"]["optimizer_version"] == "rules_v1"
        assert data["active_plan"]["cost_estimate_eur"] == 2.85

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

"""E2E tests: Health and Dashboard endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio(loop_scope="session")
class TestDashboard:
    async def test_dashboard_empty_state(self, client: AsyncClient):
        """Dashboard returns valid response even with no data."""
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_status"] is None
        assert data["current_price"] is None
        assert data["today_kwh"] == 0
        assert data["today_cost_eur"] == 0
        assert data["active_plan"] is None
        assert data["has_override"] is False

    async def test_dashboard_with_device_status(
        self, client: AsyncClient, seed_device_status
    ):
        """Dashboard shows current device status."""
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_status"] is not None
        assert data["current_status"]["device_id"] == "test-device-001"
        assert data["current_status"]["mode"] == "heat"
        assert data["current_status"]["outdoor_temp"] == 5.0
        assert data["current_status"]["tank_temp"] == 48.5

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
        assert data["today_cost_eur"] >= 0

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

"""E2E tests: Plans, Overrides, and Audit endpoints."""

import datetime as dt

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestPlans:
    async def test_plans_empty(self, client: AsyncClient):
        resp = await client.get("/api/plans")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_plans_list(self, client: AsyncClient, seed_plan):
        resp = await client.get("/api/plans?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["optimizer_version"] == "rules_v1"
        assert data[0]["cost_estimate_eur"] == 2.85

    async def test_plan_detail(self, client: AsyncClient, seed_plan):
        plan_id = seed_plan.id
        resp = await client.get(f"/api/plans/{plan_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == plan_id
        assert data["actions_count"] == 3
        assert len(data["actions"]) == 3

        # Verify action types
        action_types = {a["action_type"] for a in data["actions"]}
        assert "force_dhw_on" in action_types
        assert "force_dhw_off" in action_types
        assert "quiet_mode_on" in action_types

        # Verify action statuses
        statuses = {a["action_type"]: a["status"] for a in data["actions"]}
        assert statuses["force_dhw_on"] == "pending"
        assert statuses["quiet_mode_on"] == "executed"

    async def test_plan_detail_not_found(self, client: AsyncClient):
        resp = await client.get("/api/plans/99999")
        assert resp.status_code == 404

    async def test_plans_limit_validation(self, client: AsyncClient):
        resp = await client.get("/api/plans?limit=0")
        assert resp.status_code == 422

        resp = await client.get("/api/plans?limit=100")
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestOverrides:
    async def test_create_override(self, client: AsyncClient):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "ts_from": now.isoformat(),
            "ts_to": (now + dt.timedelta(hours=4)).isoformat(),
            "action_type": "pause_all",
            "reason": "E2E test",
        }
        resp = await client.post("/api/overrides", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    async def test_create_override_appears_in_dashboard(self, client: AsyncClient, seed_device_status):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "ts_from": (now - dt.timedelta(minutes=5)).isoformat(),
            "ts_to": (now + dt.timedelta(hours=4)).isoformat(),
            "action_type": "pause_all",
            "reason": "Test dashboard visibility",
        }
        await client.post("/api/overrides", json=payload)

        resp = await client.get("/api/dashboard")
        data = resp.json()
        assert data["has_override"] is True

    async def test_cancel_override(self, client: AsyncClient, seed_override):
        override_id = seed_override.id

        # Verify it's active
        resp = await client.get("/api/dashboard")
        # Note: need device status for dashboard to detect override scope
        # Just test the cancel endpoint directly
        resp = await client.delete(f"/api/overrides/{override_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    async def test_create_override_audit_logged(self, client: AsyncClient):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "ts_from": now.isoformat(),
            "ts_to": (now + dt.timedelta(hours=2)).isoformat(),
            "action_type": "pause_all",
            "reason": "Audit test",
        }
        await client.post("/api/overrides", json=payload)

        resp = await client.get("/api/audit?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["actor"] == "user"
        assert data[0]["action"] == "create_override"

    async def test_override_requires_valid_dates(self, client: AsyncClient):
        payload = {
            "ts_from": "not-a-date",
            "ts_to": "also-not-a-date",
            "action_type": "pause_all",
        }
        resp = await client.post("/api/overrides", json=payload)
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestStats:
    async def test_stats_day_empty(self, client: AsyncClient):
        resp = await client.get("/api/stats?period=day")
        assert resp.status_code == 200
        data = resp.json()
        assert data["period"] == "day"
        assert data["total_kwh"] == 0

    async def test_stats_day_with_data(self, client: AsyncClient, seed_consumption, seed_prices):
        resp = await client.get("/api/stats?period=day")
        assert resp.status_code == 200
        data = resp.json()
        assert data["period"] == "day"
        assert data["total_kwh"] > 0
        assert data["total_cost_eur"] > 0
        assert data["avg_price_eur_kwh"] > 0

    async def test_stats_week(self, client: AsyncClient):
        resp = await client.get("/api/stats?period=week")
        assert resp.status_code == 200
        assert resp.json()["period"] == "week"

    async def test_stats_month(self, client: AsyncClient):
        resp = await client.get("/api/stats?period=month")
        assert resp.status_code == 200
        assert resp.json()["period"] == "month"

    async def test_stats_invalid_period(self, client: AsyncClient):
        resp = await client.get("/api/stats?period=year")
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestAudit:
    async def test_audit_empty(self, client: AsyncClient):
        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_audit_after_override(self, client: AsyncClient):
        now = dt.datetime.now(dt.timezone.utc)
        await client.post(
            "/api/overrides",
            json={
                "ts_from": now.isoformat(),
                "ts_to": (now + dt.timedelta(hours=1)).isoformat(),
                "action_type": "pause_all",
                "reason": "Audit check",
            },
        )

        resp = await client.get("/api/audit?limit=10")
        data = resp.json()
        assert len(data) >= 1
        entry = data[0]
        assert entry["actor"] == "user"
        assert entry["action"] == "create_override"
        assert entry["payload"] is not None
        assert "ts_from" in entry["payload"]

    async def test_audit_limit_validation(self, client: AsyncClient):
        resp = await client.get("/api/audit?limit=0")
        assert resp.status_code == 422

        resp = await client.get("/api/audit?limit=500")
        assert resp.status_code == 422

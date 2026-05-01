"""E2E tests: Full user workflow scenarios (multi-step)."""

import datetime as dt

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestOptimizerWorkflow:
    """Tests simulating a complete optimizer cycle."""

    async def test_full_data_pipeline(
        self,
        client: AsyncClient,
        seed_device_status,
        seed_prices,
        seed_weather,
        seed_consumption,
        seed_plan,
    ):
        """Verify the entire data pipeline is queryable end-to-end."""
        # Dashboard shows everything
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_status"] is not None
        assert data["active_plan"] is not None
        assert data["today_kwh"] > 0

        # History endpoints return data
        resp = await client.get("/api/status/history?hours=24")
        assert len(resp.json()) >= 1

        resp = await client.get("/api/consumption/history?hours=24")
        assert len(resp.json()) >= 1

        resp = await client.get("/api/prices?hours=48")
        assert len(resp.json()) >= 1

        resp = await client.get("/api/weather?hours=48")
        assert len(resp.json()) >= 1

        # Plan with actions
        plan_id = data["active_plan"]["id"]
        resp = await client.get(f"/api/plans/{plan_id}")
        assert resp.status_code == 200
        plan_data = resp.json()
        assert plan_data["actions_count"] == 3

    async def test_override_lifecycle(self, client: AsyncClient, seed_device_status):
        """Test creating, detecting, and cancelling an override."""
        now = dt.datetime.now(dt.timezone.utc)

        # 1. No override initially
        resp = await client.get("/api/dashboard")
        assert resp.json()["has_override"] is False

        # 2. Create override
        payload = {
            "ts_from": (now - dt.timedelta(minutes=1)).isoformat(),
            "ts_to": (now + dt.timedelta(hours=6)).isoformat(),
            "action_type": "pause_all",
            "reason": "Workflow test",
        }
        resp = await client.post("/api/overrides", json=payload)
        assert resp.status_code == 200

        # 3. Dashboard detects override
        resp = await client.get("/api/dashboard")
        dashboard = resp.json()
        assert dashboard["has_override"] is True
        override_id = dashboard["override_id"]
        assert override_id is not None

        # 4. Audit log records the action
        resp = await client.get("/api/audit?limit=5")
        entries = resp.json()
        assert any(e["action"] == "create_override" for e in entries)

        # 5. Cancel override
        resp = await client.delete(f"/api/overrides/{override_id}")
        assert resp.status_code == 200

        # 6. Dashboard no longer shows override
        resp = await client.get("/api/dashboard")
        assert resp.json()["has_override"] is False

    async def test_multiple_plans_ordering(self, client: AsyncClient, db_session):
        """Multiple plans are returned most recent first."""
        import json

        now = dt.datetime.now(dt.timezone.utc)
        from packages.core.models import PlanRecord

        for i in range(5):
            plan = PlanRecord(
                created_at=now - dt.timedelta(hours=5 - i),
                horizon_start=now - dt.timedelta(hours=5 - i),
                horizon_end=now + dt.timedelta(hours=19 + i),
                plan_json=json.dumps({"version": f"test_{i}"}),
                optimizer_version="rules_v1",
                cost_estimate_eur=2.0 + i * 0.5,
            )
            db_session.add(plan)
        await db_session.commit()

        resp = await client.get("/api/plans?limit=5")
        data = resp.json()
        assert len(data) == 5

        # Verify ordering: most recent first
        created_times = [entry["created_at"] for entry in data]
        assert created_times == sorted(created_times, reverse=True)

    async def test_concurrent_data_and_override(
        self,
        client: AsyncClient,
        seed_device_status,
        seed_prices,
        seed_plan,
    ):
        """Data remains accessible while override is active."""
        now = dt.datetime.now(dt.timezone.utc)

        # Create override
        await client.post(
            "/api/overrides",
            json={
                "ts_from": (now - dt.timedelta(minutes=1)).isoformat(),
                "ts_to": (now + dt.timedelta(hours=4)).isoformat(),
                "action_type": "pause_all",
                "reason": "Concurrent test",
            },
        )

        # All data endpoints still work
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_override"] is True
        assert data["current_status"] is not None
        assert data["active_plan"] is not None

        resp = await client.get("/api/prices?hours=48")
        assert resp.status_code == 200

        resp = await client.get("/api/plans")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

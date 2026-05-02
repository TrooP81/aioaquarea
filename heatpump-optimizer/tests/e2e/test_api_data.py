"""E2E tests: History and data endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestStatusHistory:
    async def test_status_history_empty(self, client: AsyncClient):
        resp = await client.get("/api/status/history")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_status_history_returns_records(
        self, client: AsyncClient, seed_device_status
    ):
        resp = await client.get("/api/status/history?hours=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["device_id"] == "test-device-001"
        assert data[0]["mode"] == "heat"

    async def test_status_history_respects_hours_param(
        self, client: AsyncClient, seed_device_status
    ):
        resp = await client.get("/api/status/history?hours=720")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_status_history_validates_hours_range(self, client: AsyncClient):
        resp = await client.get("/api/status/history?hours=0")
        assert resp.status_code == 422

        resp = await client.get("/api/status/history?hours=1000")
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestConsumptionHistory:
    async def test_consumption_empty(self, client: AsyncClient):
        resp = await client.get("/api/consumption/history")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_consumption_returns_records(
        self, client: AsyncClient, seed_consumption
    ):
        resp = await client.get("/api/consumption/history?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 5
        # Check total_kwh is computed
        for entry in data:
            assert entry["total_kwh"] == (
                (entry["heat_kwh"] or 0)
                + (entry["cool_kwh"] or 0)
                + (entry["tank_kwh"] or 0)
            )

    async def test_consumption_validates_range(self, client: AsyncClient):
        resp = await client.get("/api/consumption/history?hours=-1")
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestPrices:
    async def test_prices_empty(self, client: AsyncClient):
        resp = await client.get("/api/prices")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_prices_returns_records(self, client: AsyncClient, seed_prices):
        resp = await client.get("/api/prices?hours=48")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        for entry in data:
            assert "ts" in entry
            assert "price_eur_per_kwh" in entry
            assert entry["price_eur_per_kwh"] > 0

    async def test_prices_sorted_by_time(self, client: AsyncClient, seed_prices):
        resp = await client.get("/api/prices?hours=48")
        data = resp.json()
        timestamps = [entry["ts"] for entry in data]
        assert timestamps == sorted(timestamps)

    async def test_prices_validates_hours_range(self, client: AsyncClient):
        resp = await client.get("/api/prices?hours=200")
        assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestWeather:
    async def test_weather_empty(self, client: AsyncClient):
        resp = await client.get("/api/weather")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_weather_returns_records(self, client: AsyncClient, seed_weather):
        resp = await client.get("/api/weather?hours=48")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        for entry in data:
            assert "ts" in entry
            assert "temperature" in entry

    async def test_weather_sorted_by_time(self, client: AsyncClient, seed_weather):
        resp = await client.get("/api/weather?hours=48")
        data = resp.json()
        timestamps = [entry["ts"] for entry in data]
        assert timestamps == sorted(timestamps)

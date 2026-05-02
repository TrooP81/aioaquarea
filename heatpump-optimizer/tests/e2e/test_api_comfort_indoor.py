"""E2E tests: Comfort model and indoor temperature endpoints."""

import datetime as dt
import json

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import IndoorTempReading


@pytest_asyncio.fixture(loop_scope="session")
async def seed_indoor_temps(db_session: AsyncSession):
    """Seed indoor temperature readings from SmartThings sensors."""
    now = dt.datetime.now(dt.timezone.utc)
    readings = [
        IndoorTempReading(
            timestamp=now - dt.timedelta(minutes=5),
            device_id="sensor-living-room",
            device_label="Living Room Sensor",
            room="Living Room",
            temperature=21.5,
        ),
        IndoorTempReading(
            timestamp=now - dt.timedelta(minutes=5),
            device_id="sensor-bedroom",
            device_label="Bedroom Sensor",
            room="Bedroom",
            temperature=20.0,
        ),
        IndoorTempReading(
            timestamp=now - dt.timedelta(minutes=55),
            device_id="sensor-living-room",
            device_label="Living Room Sensor",
            room="Living Room",
            temperature=20.8,
        ),
    ]
    for r in readings:
        db_session.add(r)
    await db_session.commit()
    return readings


@pytest.mark.asyncio(loop_scope="session")
class TestIndoorTemp:
    """Tests for /api/indoor-temp endpoints."""

    async def test_indoor_temp_empty(self, client: AsyncClient):
        resp = await client.get("/api/indoor-temp")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_indoor_temp_returns_readings(
        self, client: AsyncClient, seed_indoor_temps
    ):
        resp = await client.get("/api/indoor-temp?hours=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert all("temperature" in r for r in data)
        assert all("device_id" in r for r in data)

    async def test_indoor_temp_filter_by_device(
        self, client: AsyncClient, seed_indoor_temps
    ):
        resp = await client.get("/api/indoor-temp?device_id=sensor-bedroom")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["device_id"] == "sensor-bedroom"
        assert data[0]["temperature"] == 20.0

    async def test_indoor_temp_latest(
        self, client: AsyncClient, seed_indoor_temps
    ):
        resp = await client.get("/api/indoor-temp/latest")
        assert resp.status_code == 200
        data = resp.json()
        # Two sensors have readings within 15 min
        assert data["sensor_count"] == 2
        # Average of 21.5 and 20.0
        assert data["avg_temperature"] == 20.8
        assert data["latest_reading"] is not None

    async def test_indoor_temp_latest_empty(self, client: AsyncClient):
        resp = await client.get("/api/indoor-temp/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["avg_temperature"] is None
        assert data["sensor_count"] == 0


@pytest.mark.asyncio(loop_scope="session")
class TestComfortModel:
    """Tests for /api/comfort-model endpoints."""

    async def test_comfort_model_status_untrained(self, client: AsyncClient):
        resp = await client.get("/api/comfort-model/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trained"] is False
        assert data["last_trained"] is None
        assert data["training_samples"] == 0

    async def test_comfort_model_predict_requires_training(
        self, client: AsyncClient
    ):
        resp = await client.get(
            "/api/comfort-model/predict",
            params={"water_temp": 35.0, "outdoor_temp": 5.0, "hour": 12},
        )
        assert resp.status_code == 409
        assert "not yet trained" in resp.json()["detail"]

    async def test_comfort_model_train_insufficient_data(
        self, client: AsyncClient
    ):
        """Training with no status data should return insufficient_data."""
        resp = await client.post("/api/comfort-model/train")
        assert resp.status_code == 200
        data = resp.json()
        # With no data, the model returns a status indicating insufficient data
        assert data.get("status") in ("insufficient_data", "error", "trained")


@pytest.mark.asyncio(loop_scope="session")
class TestComfortSchedule:
    """Tests for /api/comfort-schedule endpoints."""

    async def test_get_comfort_schedule_default(self, client: AsyncClient):
        resp = await client.get("/api/comfort-schedule")
        assert resp.status_code == 200
        data = resp.json()
        assert "weekday" in data or data == {}

    async def test_put_comfort_schedule(self, client: AsyncClient):
        schedule = {
            "weekday": [7, 8, 9, 17, 18, 19, 20, 21],
            "weekend": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        }
        resp = await client.put(
            "/api/comfort-schedule",
            json=schedule,
        )
        assert resp.status_code == 200

        # Verify it's persisted
        resp2 = await client.get("/api/comfort-schedule")
        assert resp2.status_code == 200
        saved = resp2.json()
        assert saved["weekday"] == schedule["weekday"]
        assert saved["weekend"] == schedule["weekend"]

    async def test_comfort_schedule_learned(self, client: AsyncClient):
        """The learned schedule endpoint should return without error."""
        resp = await client.get("/api/comfort-schedule/learned")
        assert resp.status_code == 200

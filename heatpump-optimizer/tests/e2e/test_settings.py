"""E2E tests: Settings API endpoints."""

import pytest
from httpx import AsyncClient

from packages.core.models import SettingRecord


@pytest.mark.asyncio(loop_scope="session")
class TestSettingsGet:
    async def test_get_settings_returns_schema(self, client: AsyncClient):
        """GET /api/settings returns all configurable settings with metadata."""
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()

        # Should include key settings from schema
        assert "price_provider" in data
        assert "weather_provider" in data
        assert "entsoe_api_token" in data
        assert "latitude" in data
        assert "manual_price_eur_per_kwh" in data

    async def test_get_settings_includes_type_and_description(self, client: AsyncClient):
        """Each setting has type, description, and value."""
        resp = await client.get("/api/settings")
        data = resp.json()

        price_provider = data["price_provider"]
        assert "type" in price_provider
        assert "description" in price_provider
        assert "value" in price_provider
        assert price_provider["type"] == "str"

    async def test_get_settings_includes_options_for_providers(self, client: AsyncClient):
        """Provider settings include valid options list."""
        resp = await client.get("/api/settings")
        data = resp.json()

        assert data["price_provider"]["options"] == ["entsoe", "tibber", "manual"]
        assert data["weather_provider"]["options"] == ["open-meteo", "manual"]

    async def test_get_settings_masks_secrets(self, client: AsyncClient, db_session):
        """Secret values are masked in GET response."""
        # Seed a secret setting
        db_session.add(SettingRecord(key="entsoe_api_token", value="my-secret-token-123"))
        await db_session.commit()

        resp = await client.get("/api/settings")
        data = resp.json()

        # Secret should be masked
        assert "***" in data["entsoe_api_token"]["value"]
        assert "my-secret-token-123" not in data["entsoe_api_token"]["value"]


@pytest.mark.asyncio(loop_scope="session")
class TestSettingsUpdate:
    async def test_update_settings(self, client: AsyncClient):
        """PUT /api/settings updates values in DB."""
        resp = await client.put(
            "/api/settings",
            json={"settings": {"price_provider": "manual", "manual_price_eur_per_kwh": "0.30"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["count"] == 2

        # Verify the values are persisted
        resp = await client.get("/api/settings")
        data = resp.json()
        assert data["price_provider"]["value"] == "manual"
        assert data["manual_price_eur_per_kwh"]["value"] == "0.30"

    async def test_update_settings_rejects_unknown_keys(self, client: AsyncClient):
        """PUT /api/settings rejects unknown setting keys."""
        resp = await client.put(
            "/api/settings",
            json={"settings": {"nonexistent_key": "value"}},
        )
        assert resp.status_code == 400
        assert "Unknown settings" in resp.json()["detail"]

    async def test_update_settings_creates_audit_log(self, client: AsyncClient):
        """Settings changes are audit-logged."""
        await client.put(
            "/api/settings",
            json={"settings": {"latitude": "51.5"}},
        )

        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        logs = resp.json()

        settings_logs = [l for l in logs if l["action"] == "update_settings"]
        assert len(settings_logs) > 0
        assert settings_logs[0]["actor"] == "user"

    async def test_update_settings_audit_masks_secrets(self, client: AsyncClient):
        """Secret values are masked in audit log."""
        await client.put(
            "/api/settings",
            json={"settings": {"tibber_api_token": "super-secret-token"}},
        )

        resp = await client.get("/api/audit")
        logs = resp.json()

        settings_logs = [l for l in logs if l["action"] == "update_settings"]
        # The payload should have masked the secret
        payload = settings_logs[0]["payload"]
        assert "super-secret-token" not in str(payload)
        assert "***" in str(payload)


@pytest.mark.asyncio(loop_scope="session")
class TestManualMode:
    async def test_set_price_provider_to_manual(self, client: AsyncClient):
        """Can set price provider to manual mode."""
        resp = await client.put(
            "/api/settings",
            json={"settings": {"price_provider": "manual", "manual_price_eur_per_kwh": "0.15"}},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/settings")
        data = resp.json()
        assert data["price_provider"]["value"] == "manual"
        assert data["manual_price_eur_per_kwh"]["value"] == "0.15"

    async def test_set_weather_provider_to_manual(self, client: AsyncClient):
        """Can set weather provider to manual mode."""
        resp = await client.put(
            "/api/settings",
            json={
                "settings": {
                    "weather_provider": "manual",
                    "manual_outdoor_temp": "12.5",
                    "manual_wind_speed": "3.0",
                    "manual_humidity": "75.0",
                    "manual_irradiance": "150.0",
                }
            },
        )
        assert resp.status_code == 200

        resp = await client.get("/api/settings")
        data = resp.json()
        assert data["weather_provider"]["value"] == "manual"
        assert data["manual_outdoor_temp"]["value"] == "12.5"
        assert data["manual_wind_speed"]["value"] == "3.0"

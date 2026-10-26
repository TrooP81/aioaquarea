"""E2E tests: Settings API endpoints."""

import pytest
from contextlib import asynccontextmanager
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch

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
        assert data["weather_provider"]["options"] == ["open-meteo", "smhi", "manual"]

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

        settings_logs = [entry for entry in logs if entry["action"] == "update_settings"]
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

        settings_logs = [entry for entry in logs if entry["action"] == "update_settings"]
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


@pytest.mark.asyncio(loop_scope="session")
class TestTypedSettingsAccessors:
    async def test_typed_getters_parse_defaults(self):
        from packages.core.settings_service import (
            get_bool_setting,
            get_float_setting,
            get_int_setting,
        )

        with patch(
            "packages.core.settings_service.get_setting", new=AsyncMock(side_effect=lambda key: "")
        ):
            assert await get_int_setting("smartthings_poll_interval") == 300
            assert await get_float_setting("comfort_temp_target") == 20.5
            assert await get_bool_setting("smartthings_enabled") is False

    async def test_typed_getters_parse_stored_values(self):
        from packages.core.settings_service import (
            get_bool_setting,
            get_float_setting,
            get_int_setting,
        )

        values = {
            "smartthings_poll_interval": "450",
            "comfort_temp_target": "21.25",
            "smartthings_enabled": "true",
        }
        with patch(
            "packages.core.settings_service.get_setting",
            new=AsyncMock(side_effect=lambda key: values[key]),
        ):
            assert await get_int_setting("smartthings_poll_interval") == 450
            assert await get_float_setting("comfort_temp_target") == 21.25
            assert await get_bool_setting("smartthings_enabled") is True


@asynccontextmanager
async def _ordinary_session():
    from sqlalchemy.ext.asyncio import AsyncSession
    from packages.core.database import engine
    from tests.e2e.test_manual_cop_postgresql import guard_owned_database

    async with engine.begin() as connection:
        await guard_owned_database(connection)
        async with AsyncSession(bind=connection, join_transaction_mode="rollback_only") as session:
            yield session


async def _ordinary_baseline():
    from tests.e2e import ordinary_cleanup

    async with _ordinary_session() as session:
        return await ordinary_cleanup.capture_ordinary_baseline(session)


async def _reset_baseline(baseline):
    from packages.core.database import engine
    from tests.e2e.conftest import truncate_legacy_tables
    from tests.e2e.ordinary_cleanup import reset_ordinary_profile
    from tests.e2e.test_manual_cop_postgresql import guard_owned_database

    await reset_ordinary_profile(
        engine, baseline, guard=guard_owned_database, truncate=truncate_legacy_tables
    )


@pytest.mark.asyncio(loop_scope="session")
class TestOrdinaryCleanup:
    async def test_contract_lock_serializes_cleanup_and_competing_writer(self):
        import asyncio
        from packages.core.house_contract import _lock_contract

        started, acquired = asyncio.Event(), asyncio.Event()

        async def contender():
            async with _ordinary_session() as session:
                started.set()
                await _lock_contract(session)
                acquired.set()

        task = None
        try:
            async with _ordinary_session() as session:
                await _lock_contract(session)
                task = asyncio.create_task(contender())
                await asyncio.wait_for(started.wait(), timeout=2)
                await asyncio.sleep(0.05)
                assert not acquired.is_set(), "contract lock must precede competing state reads"
            await asyncio.wait_for(task, timeout=2)
            assert acquired.is_set()
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)

    async def test_unchanged_reset_keeps_exact_rows_history_and_generations(self):
        baseline = await _ordinary_baseline()
        before = baseline.boundary
        await _reset_baseline(baseline)
        await _reset_baseline(baseline)
        assert baseline.boundary == before

    @pytest.mark.parametrize(
        "keys",
        [
            ("weather_provider",),
            ("smartthings_device_ids",),
            ("comfort_reference_sensor_id",),
            ("smartthings_device_ids", "comfort_reference_sensor_id"),
            ("weather_provider", "smartthings_device_ids", "comfort_reference_sensor_id"),
            ("timezone",),
            ("weather_provider", "timezone"),
            ("comfort_temp_target",),
        ],
    )
    async def test_forward_reset_and_next_unrelated_request(self, keys, client):
        from packages.core.settings_service import set_settings_bulk

        baseline = await _ordinary_baseline()
        before = baseline.boundary
        values = {
            "weather_provider": (
                "smhi" if baseline.effective["weather_provider"] != "smhi" else "open-meteo"
            ),
            "smartthings_device_ids": "cleanup-synthetic-sensor",
            "comfort_reference_sensor_id": "cleanup-synthetic-reference",
            "timezone": "UTC" if baseline.effective["timezone"] != "UTC" else "Europe/Amsterdam",
            "comfort_temp_target": str(
                (
                    float(baseline.effective["comfort_temp_min"])
                    + float(baseline.effective["comfort_temp_max"])
                )
                / 2
            ),
        }
        if values["comfort_temp_target"] == baseline.effective["comfort_temp_target"]:
            values["comfort_temp_target"] = baseline.effective["comfort_temp_min"]
        async with _ordinary_session() as session:
            await set_settings_bulk(
                {key: values[key] for key in keys},
                session=session,
                expected_revision_id=before["installation"]["current_revision_id"],
                source="cleanup_regression",
            )
        await _reset_baseline(baseline)
        after = baseline.boundary
        weather = "weather_provider" in keys
        sensor = any(
            key in keys for key in ("smartthings_device_ids", "comfort_reference_sensor_id")
        )
        timezone = "timezone" in keys
        # Mutation and restoration both count; they never copy old generations.
        assert after["installation"]["demand_dependency_generation"] == (
            before["installation"]["demand_dependency_generation"]
            + 2 * (int(weather) + int(timezone))
        )
        assert after["installation"]["indoor_dependency_generation"] == (
            before["installation"]["indoor_dependency_generation"] + 2 * int(weather or sensor)
        )
        if timezone or "comfort_temp_target" in keys:
            assert (
                after["installation"]["current_revision_id"]
                > before["installation"]["current_revision_id"]
            )
            assert len(after["history"]) == len(before["history"]) + 2
        assert after["history"][: len(before["history"])] == before["history"]
        response = await client.get("/api/settings")
        assert response.status_code == 200
        for key in keys:
            assert response.json()[key]["value"] == baseline.effective[key]

    async def test_completed_dependency_a_b_a_is_not_rewound(self):
        from packages.core.settings_service import set_settings_bulk

        baseline = await _ordinary_baseline()
        before = baseline.boundary
        old = baseline.effective["weather_provider"]
        async with _ordinary_session() as session:
            for value in ("smhi" if old != "smhi" else "open-meteo", old):
                await set_settings_bulk({"weather_provider": value}, session=session)
        await _reset_baseline(baseline)
        assert baseline.boundary["installation"]["demand_dependency_generation"] == (
            before["installation"]["demand_dependency_generation"] + 2
        )
        assert baseline.boundary["installation"]["indoor_dependency_generation"] == (
            before["installation"]["indoor_dependency_generation"] + 2
        )
        assert baseline.boundary["history"] == before["history"]

    @pytest.mark.parametrize("phase", ["dependencies", "rows", "revision", "truncate"])
    async def test_cleanup_failure_rolls_back_and_poisons(self, monkeypatch, phase):
        from sqlalchemy.ext.asyncio import AsyncSession
        from packages.core import settings_service
        from packages.core.database import engine
        from tests.e2e import conftest, ordinary_cleanup
        from tests.e2e.test_manual_cop_postgresql import guard_owned_database

        baseline = await _ordinary_baseline()
        async with _ordinary_session() as session:
            await settings_service.set_settings_bulk(
                {
                    "timezone": "UTC"
                    if baseline.effective["timezone"] != "UTC"
                    else "Europe/Amsterdam",
                    "weather_provider": "smhi"
                    if baseline.effective["weather_provider"] != "smhi"
                    else "open-meteo",
                    "smartthings_device_ids": "cleanup-rollback-sensor",
                },
                session=session,
            )
        async with engine.begin() as connection:
            await guard_owned_database(connection)
            async with AsyncSession(
                bind=connection, join_transaction_mode="rollback_only"
            ) as session:
                before = await ordinary_cleanup._house_boundary(session)
                rows = await ordinary_cleanup._settings_rows(session)

        with monkeypatch.context() as patcher:
            if phase == "dependencies":
                original = settings_service.set_settings_bulk

                async def fail(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError("injected cleanup failure")

                patcher.setattr(settings_service, "set_settings_bulk", fail)
            elif phase == "rows":
                original = AsyncSession.expire_all

                def fail(session):
                    # This boundary follows both DELETE and optional INSERT,
                    # including a genuinely empty persisted-row baseline.
                    original(session)
                    raise RuntimeError("injected cleanup failure")

                patcher.setattr(AsyncSession, "expire_all", fail)
            elif phase == "revision":
                original = ordinary_cleanup.house_contract.revise_contract

                async def fail(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError("injected cleanup failure")

                patcher.setattr(ordinary_cleanup.house_contract, "revise_contract", fail)
            else:
                original = conftest.truncate_legacy_tables

                async def fail(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError("injected cleanup failure")

                patcher.setattr(conftest, "truncate_legacy_tables", fail)
            with pytest.raises(RuntimeError, match="injected cleanup failure"):
                await _reset_baseline(baseline)
        assert baseline.poisoned
        with pytest.raises(AssertionError, match="unusable"):
            await _reset_baseline(baseline)
        async with engine.begin() as connection:
            await guard_owned_database(connection)
            async with AsyncSession(
                bind=connection, join_transaction_mode="rollback_only"
            ) as session:
                after = await ordinary_cleanup._house_boundary(session)
                assert after["installation"] == before["installation"]
                assert after["history"] == before["history"]
                assert after["catalog"] == before["catalog"]
                assert after["sequence"] >= before["sequence"]
                assert await ordinary_cleanup._settings_rows(session) == rows


@pytest.mark.asyncio(loop_scope="session")
async def test_ordinary_first_initialization_retains_fallback_absence_and_immutable_empty_trigger():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession
    from tests.e2e import ordinary_cleanup
    from tests.e2e.test_manual_cop_postgresql import owned_migration_child

    # The primary guard is intentionally not broadened to admit child databases.
    async with owned_migration_child("cleanup") as child:
        await child.alembic("upgrade", "head")
        async with child.engine.begin() as connection:
            empty_probe = text(
                "SELECT NOT EXISTS(SELECT 1 FROM public.house_contract_revisions) "
                "AND NOT EXISTS(SELECT 1 FROM public.house_installations) "
                "AND NOT EXISTS(SELECT 1 FROM public.plans) "
                "AND NOT EXISTS(SELECT 1 FROM public.optimization_requests)"
            )
            assert (await connection.execute(empty_probe)).scalar_one() is True
            with pytest.raises(DBAPIError, match="house contract revisions are immutable"):
                async with connection.begin_nested():
                    # Include every FK referrer so FK rejection cannot mask the
                    # 038 BEFORE TRUNCATE trigger, even with no revision rows.
                    await connection.execute(
                        text(
                            "TRUNCATE public.house_contract_revisions, public.house_installations, "
                            "public.plans, public.optimization_requests"
                        )
                    )
            assert (await connection.execute(empty_probe)).scalar_one() is True
            async with AsyncSession(
                bind=connection, join_transaction_mode="rollback_only"
            ) as session:
                session.add(SettingRecord(key="latitude", value="51.5"))
                await session.flush()
                baseline = await ordinary_cleanup.capture_ordinary_baseline(session)
                again = await ordinary_cleanup.capture_ordinary_baseline(session)
                assert baseline.rows == again.rows
                assert baseline.boundary == again.boundary
                assert "comfort_temp_target" not in {row["key"] for row in baseline.rows}
                for operation in (
                    "UPDATE public.house_contract_revisions SET source='bad'",
                    "DELETE FROM public.house_contract_revisions",
                ):
                    with pytest.raises(DBAPIError, match="house contract revisions are immutable"):
                        async with connection.begin_nested():
                            await connection.execute(text(operation))


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("broken", ["pointer", "settings"])
async def test_ordinary_existing_inconsistent_installation_is_not_silently_repaired(broken):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession
    from tests.e2e import ordinary_cleanup
    from tests.e2e.test_manual_cop_postgresql import owned_migration_child

    async with owned_migration_child("cleanup") as child:
        await child.alembic("upgrade", "head")
        async with child.engine.begin() as connection:
            async with AsyncSession(
                bind=connection, join_transaction_mode="rollback_only"
            ) as session:
                baseline = await ordinary_cleanup.capture_ordinary_baseline(session)
                if broken == "pointer":
                    await session.execute(
                        text("UPDATE house_installations SET current_revision_id=NULL")
                    )
                else:
                    session.add(
                        SettingRecord(
                            key="timezone",
                            value="UTC"
                            if baseline.effective["timezone"] != "UTC"
                            else "Europe/Amsterdam",
                        )
                    )
                    await session.flush()
                session.expire_all()
                with pytest.raises((AssertionError, ValueError)):
                    await ordinary_cleanup.capture_ordinary_baseline(session)

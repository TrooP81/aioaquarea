"""Owned E2E fixtures retain migrated schema and manual controller-COP storage."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Override settings BEFORE importing app modules.
os.environ["API_TOKEN"] = "test-token"
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://heatpump:heatpump_test@127.0.0.1:5433/heatpump_test",
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")
os.environ.setdefault("PRICE_PROVIDER", "entsoe")
os.environ.setdefault("ENTSOE_API_TOKEN", "test-token")
os.environ.setdefault("ENTSOE_AREA", "10Y1001A1001A46L")
os.environ.setdefault("TIBBER_API_TOKEN", "")
os.environ.setdefault("AQUAREA_USERNAME", "test@test.com")
os.environ.setdefault("AQUAREA_PASSWORD", "testpass")
os.environ.setdefault("LATITUDE", "59.3293")
os.environ.setdefault("LONGITUDE", "18.0686")
manual_sql = importlib.import_module("tests.e2e.test_manual_cop_postgresql")
owned_profile = manual_sql.owned_test_environment()

from packages.api.main import app  # noqa: E402
from packages.core.database import engine  # noqa: E402
from packages.core.models import (  # noqa: E402
    Base,
    ConsumptionRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    WeatherRecord,
)


LEGACY_TABLES = tuple(
    table for table in Base.metadata.sorted_tables if table.name != "manual_cop_observations"
)
for legacy_table in LEGACY_TABLES:
    assert all(
        foreign_key.column.table.name != "manual_cop_observations"
        for foreign_key in legacy_table.foreign_keys
    )
    assert all(
        column.server_default is None
        or "manual_cop_revision_seq" not in str(column.server_default.arg)
        for column in legacy_table.columns
    )


async def legacy_schema_state(connection, names):
    queries = {
        "columns": (
            "SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),"
            "a.attnotnull,pg_get_expr(d.adbin,d.adrelid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid "
            "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
            "WHERE n.nspname='public' AND c.relname=ANY(:names) "
            "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
        ),
        "constraints": (
            "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=ANY(:names) ORDER BY c.relname,k.conname"
        ),
        "indexes": (
            "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND tablename=ANY(:names) ORDER BY tablename,indexname"
        ),
    }
    return {
        key: tuple((await connection.execute(text(query), {"names": names})).all())
        for key, query in queries.items()
    }


async def truncate_legacy_tables(connection):
    await manual_sql.guard_owned_database(connection)
    dependencies = (
        await connection.execute(
            text(
                "SELECT k.conname FROM pg_constraint k WHERE k.contype='f' "
                "AND k.confrelid='public.manual_cop_observations'::regclass "
                "UNION ALL SELECT c.relname FROM pg_depend d "
                "JOIN pg_attrdef a ON d.classid='pg_attrdef'::regclass AND d.objid=a.oid "
                "JOIN pg_class c ON c.oid=a.adrelid "
                "WHERE d.refobjid='public.manual_cop_revision_seq'::regclass "
                "AND c.relname=ANY(:names)"
            ),
            {"names": [table.name for table in LEGACY_TABLES]},
        )
    ).all()
    assert not dependencies, "legacy cleanup must not reference retained storage"
    retained = await manual_sql.retained_storage_state(connection)
    identifiers = ", ".join(
        "public." + connection.dialect.identifier_preparer.quote(table.name)
        for table in LEGACY_TABLES
    )
    await connection.execute(text(f"TRUNCATE {identifiers} RESTART IDENTITY"))
    assert await manual_sql.retained_storage_state(connection) == retained


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def setup_database():
    """Create missing legacy tables only; preserve every migrated relation."""
    created = []
    retained = None
    migrated_names = []
    schema = None
    with manual_sql.owned_test_network():
        try:
            async with engine.begin() as connection:
                await manual_sql.guard_owned_database(connection)
                head, _ = manual_sql.migration_targets()
                retained = await manual_sql.retained_storage_state(connection)
                assert retained["marker"] == (head,)
                # Current-head storage keeps local periods only: no stored boundaries,
                # SQL calendar checks or helper (retained_storage_state checks the shape).
                assert head == manual_sql.LOCAL_PERIOD_REVISION
                assert retained["helper"] is None
                existing = await connection.run_sync(
                    lambda current: set(inspect(current).get_table_names(schema="public"))
                )
                migrated_names = [table.name for table in LEGACY_TABLES if table.name in existing]
                schema = await legacy_schema_state(connection, migrated_names)
                for table in LEGACY_TABLES:
                    if table.name not in existing:
                        await connection.run_sync(
                            lambda current, target=table: target.create(current, checkfirst=True)
                        )
                        created.append(table)
                await truncate_legacy_tables(connection)
            yield
        finally:
            try:
                if retained is not None:
                    async with engine.begin() as connection:
                        await manual_sql.guard_owned_database(connection)
                        assert await manual_sql.retained_storage_state(connection) == retained
                        for table in reversed(created):
                            await connection.run_sync(
                                lambda current, target=table: target.drop(current, checkfirst=True)
                            )
                        assert await legacy_schema_state(connection, migrated_names) == schema
                        assert await manual_sql.retained_storage_state(connection) == retained
            finally:
                await engine.dispose()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_tables(setup_database):
    """Clear legacy synthetic rows without cascading into retained storage."""
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await truncate_legacy_tables(connection)


@pytest_asyncio.fixture(loop_scope="session")
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {os.environ['API_TOKEN']}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture(loop_scope="session")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Direct database session for seeding test data. Auto-commits."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="session")
async def seed_device_status(db_session: AsyncSession):
    """Seed a device status record."""
    now = dt.datetime.now(dt.timezone.utc)
    record = DeviceStatusRecord(
        ts=now,
        device_id="test-device-001",
        mode="heat",
        operation_status=1,
        outdoor_temp=5.0,
        tank_temp=48.5,
        tank_target_temp=50,
        zone1_temp=21.0,
        zone1_target_temp=22,
        zone1_heat_min=18,
        zone1_heat_max=45,
        quiet_mode=0,
        powerful_mode=0,
        special_status_supported=True,
    )
    db_session.add(record)
    await db_session.commit()
    return record


@pytest_asyncio.fixture(loop_scope="session")
async def seed_panasonic_mode_parity_statuses(db_session: AsyncSession):
    """Persist the representative Panasonic operation-mode corpus for parity checks."""

    modes = [
        None,
        "0",
        "1",
        "2",
        "3",
        "4",
        "heat",
        "cool",
        "auto_heat",
        "AUTO_COOL",
        "ExtendedOperationMode.HEAT",
        "ExtendedOperationMode.AUTO_COOL",
        "malformed",
    ]
    now = dt.datetime.now(dt.timezone.utc)
    records = [
        DeviceStatusRecord(
            ts=now,
            device_id=f"mode-parity-{mode_index}-{operation_status}",
            mode=mode,
            operation_status=operation_status,
        )
        for mode_index, mode in enumerate(modes)
        for operation_status in (None, 0, 1)
    ]
    db_session.add_all(records)
    await db_session.commit()
    return records


@pytest_asyncio.fixture(loop_scope="session")
async def seed_prices(db_session: AsyncSession):
    """Seed 24 hours of price data."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    prices = []
    hourly = [
        0.05,
        0.04,
        0.03,
        0.03,
        0.04,
        0.06,
        0.08,
        0.12,
        0.15,
        0.18,
        0.20,
        0.22,
        0.20,
        0.18,
        0.15,
        0.14,
        0.16,
        0.25,
        0.30,
        0.28,
        0.20,
        0.12,
        0.08,
        0.06,
    ]
    for h, price in enumerate(hourly):
        ts = now - dt.timedelta(hours=12) + dt.timedelta(hours=h)
        record = PriceRecord(ts=ts, area="10Y1001A1001A46L", price_eur_per_kwh=price)
        db_session.add(record)
        prices.append(record)
    await db_session.commit()
    return prices


@pytest_asyncio.fixture(loop_scope="session")
async def seed_weather(db_session: AsyncSession):
    """Seed 48 hours of weather forecast data."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    records = []
    for h in range(48):
        ts = now + dt.timedelta(hours=h)
        record = WeatherRecord(
            ts=ts,
            source="open-meteo",
            temperature=5.0 + h * 0.2,
            irradiance=max(0, 200 * (1 - abs(h - 12) / 12)),
            wind_speed=3.5,
            humidity=70.0,
        )
        db_session.add(record)
        records.append(record)
    await db_session.commit()
    return records


@pytest_asyncio.fixture(loop_scope="session")
async def seed_consumption(db_session: AsyncSession):
    """Seed consumption records for today (cumulative values — most recent is highest)."""
    now = dt.datetime.now(dt.timezone.utc)
    records = []
    for h in range(6):
        ts = now - dt.timedelta(hours=h)
        record = ConsumptionRecord(
            ts=ts,
            device_id="test-device-001",
            heat_kwh=1.7 - h * 0.1,
            cool_kwh=0.0,
            tank_kwh=0.5,
            outdoor_temp=5.0 + h,
        )
        db_session.add(record)
        records.append(record)
    await db_session.commit()
    return records


@pytest_asyncio.fixture(loop_scope="session")
async def seed_plan(db_session: AsyncSession):
    """Seed an optimizer plan with actions."""
    now = dt.datetime.now(dt.timezone.utc)
    plan = PlanRecord(
        created_at=now - dt.timedelta(hours=1),
        horizon_start=now - dt.timedelta(hours=1),
        horizon_end=now + dt.timedelta(hours=23),
        plan_json=json.dumps({"version": "rules_v1", "actions": []}),
        optimizer_version="rules_v1",
        cost_estimate_eur=2.85,
    )
    db_session.add(plan)
    await db_session.commit()

    actions = [
        PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=now + dt.timedelta(hours=2),
            action_type="force_dhw_on",
            payload_json="{}",
            status="pending",
        ),
        PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=now + dt.timedelta(hours=3),
            action_type="force_dhw_off",
            payload_json="{}",
            status="pending",
        ),
        PlanActionRecord(
            plan_id=plan.id,
            scheduled_ts=now - dt.timedelta(minutes=30),
            action_type="quiet_mode_on",
            payload_json="{}",
            status="executed",
            executed_at=now - dt.timedelta(minutes=30),
        ),
    ]
    for a in actions:
        db_session.add(a)
    await db_session.commit()
    return plan


@pytest_asyncio.fixture(loop_scope="session")
async def seed_override(db_session: AsyncSession):
    """Seed an active override."""
    now = dt.datetime.now(dt.timezone.utc)
    override = OverrideRecord(
        ts_from=now - dt.timedelta(hours=1),
        ts_to=now + dt.timedelta(hours=12),
        action_type="pause_all",
        reason="E2E test override",
        active=True,
    )
    db_session.add(override)
    await db_session.commit()
    return override

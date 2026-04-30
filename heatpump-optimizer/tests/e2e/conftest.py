"""E2E test fixtures: spins up a real database and FastAPI test client."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Override settings BEFORE importing app modules
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://heatpump:heatpump_test@localhost:5433/heatpump_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("PRICE_PROVIDER", "entsoe")
os.environ.setdefault("ENTSOE_API_TOKEN", "test-token")
os.environ.setdefault("ENTSOE_AREA", "10Y1001A1001A46L")
os.environ.setdefault("TIBBER_API_TOKEN", "")
os.environ.setdefault("AQUAREA_USERNAME", "test@test.com")
os.environ.setdefault("AQUAREA_PASSWORD", "testpass")
os.environ.setdefault("LATITUDE", "59.3293")
os.environ.setdefault("LONGITUDE", "18.0686")

from packages.api.main import app  # noqa: E402
from packages.core.database import engine  # noqa: E402
from packages.core.models import (  # noqa: E402
    AuditLogRecord,
    Base,
    ConsumptionRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    SettingRecord,
    WeatherRecord,
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def setup_database():
    """Create all tables at session start, drop at end."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_tables(setup_database):
    """Truncate all tables between tests using TRUNCATE CASCADE."""
    yield
    async with engine.begin() as conn:
        table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        await conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(loop_scope="session")
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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
        quiet_mode=0,
        powerful_mode=0,
    )
    db_session.add(record)
    await db_session.commit()
    return record


@pytest_asyncio.fixture(loop_scope="session")
async def seed_prices(db_session: AsyncSession):
    """Seed 24 hours of price data."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    prices = []
    hourly = [
        0.05, 0.04, 0.03, 0.03, 0.04, 0.06,
        0.08, 0.12, 0.15, 0.18, 0.20, 0.22,
        0.20, 0.18, 0.15, 0.14, 0.16, 0.25,
        0.30, 0.28, 0.20, 0.12, 0.08, 0.06,
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
    """Seed consumption records for today."""
    now = dt.datetime.now(dt.timezone.utc)
    records = []
    for h in range(6):
        ts = now - dt.timedelta(hours=h)
        record = ConsumptionRecord(
            ts=ts,
            device_id="test-device-001",
            heat_kwh=1.2 + h * 0.1,
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

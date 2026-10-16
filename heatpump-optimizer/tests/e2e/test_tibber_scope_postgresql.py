"""Owned PostgreSQL/Timescale qualification; collect locally, execute only in CI.

Run with --noconftest under the existing COP_MANUAL_TEST_* ownership contract.
No live provider requests are made. The child database is guarded by that harness.
"""

from __future__ import annotations


import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import PriceScopeRecord
from packages.core.price_scope import ensure_tibber_scope, tibber_scope
from tests.e2e.test_manual_cop_postgresql import owned_migration_child

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def price_state(connection):
    queries = (
        "SELECT ts,area,price_eur_per_kwh,price_currency,price_source FROM prices ORDER BY ts,area",
        "SELECT column_name,data_type,character_maximum_length FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='prices' ORDER BY ordinal_position",
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='prices' ORDER BY indexname",
        "SELECT constraint_name,constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema='public' AND table_name='prices' ORDER BY constraint_name",
        "SELECT chunk_schema,chunk_name FROM timescaledb_information.chunks "
        "WHERE hypertable_name='prices' ORDER BY chunk_schema,chunk_name",
    )
    return [tuple((await connection.execute(text(query))).all()) for query in queries]


async def test_037_registry_upgrade_downgrade_preserves_populated_price_hypertable(monkeypatch):
    from packages.core import price_scope

    async with owned_migration_child("tibber") as child:
        await child.alembic("upgrade", "036")
        async with child.engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO prices(ts,area,price_eur_per_kwh,price_currency,price_source)
                VALUES ('2026-01-01T00:00:00Z','tibber',0,'SEK','tibber'),
                       ('2026-03-01T00:00:00Z','tibber',-0.2,'SEK','tibber'),
                       ('2026-05-01T00:00:00Z','SE3',0.3,'EUR','entsoe')
            """)
            )
            before = await price_state(connection)
            assert len(before[-1]) >= 2
        await child.alembic("upgrade", "037")
        async with child.engine.begin() as connection:
            assert await price_state(connection) == before
            versions = (
                await connection.execute(
                    text(
                        "SELECT current_setting('server_version'), extversion FROM pg_extension WHERE extname='timescaledb'"
                    )
                )
            ).one()
            print("Qualified PostgreSQL/Timescale versions:", versions)
            async with AsyncSession(bind=connection) as session:
                await ensure_tibber_scope(
                    session, area=tibber_scope("A"), home_id="A", currency="SEK"
                )
                await ensure_tibber_scope(
                    session, area=tibber_scope("A"), home_id="A", currency="SEK"
                )
                original_scope = tibber_scope("A")
                with monkeypatch.context() as patcher:
                    patcher.setattr(price_scope, "tibber_scope", lambda _: original_scope)
                    with pytest.raises(ValueError, match="collision"):
                        await ensure_tibber_scope(
                            session, area=original_scope, home_id="B", currency="SEK"
                        )
                record = (await session.execute(select(PriceScopeRecord))).scalar_one()
                assert record.home_id == "A"
                for sql in (
                    "UPDATE price_scopes SET home_id='B'",
                    "DELETE FROM price_scopes",
                    "TRUNCATE price_scopes",
                ):
                    with pytest.raises(DBAPIError, match="immutable"):
                        async with session.begin_nested():
                            await session.execute(text(sql))
            assert await price_state(connection) == before
        await child.alembic("downgrade", "036")
        async with child.engine.connect() as connection:
            assert await price_state(connection) == before
            assert (
                await connection.execute(text("SELECT to_regclass('price_scopes')"))
            ).scalar_one() is None
        await child.alembic("upgrade", "037")
        async with child.engine.connect() as connection:
            assert await price_state(connection) == before


@pytest.mark.parametrize("outcome", ["same_identity", "collision", "winner_rollback"])
async def test_037_concurrent_registry_inserts_wait_and_verify_transactionally(
    monkeypatch, outcome
):
    import asyncio

    from packages.core import price_scope

    async with owned_migration_child("tibberrace") as child:
        await child.alembic("upgrade", "037")
        area = tibber_scope("race-A")
        # Force a digest collision only for the losing identity, without changing DB constraints.
        original_scope = price_scope.tibber_scope
        monkeypatch.setattr(
            price_scope,
            "tibber_scope",
            lambda identity: area if identity == "race-B" else original_scope(identity),
        )
        contender_home = "race-A" if outcome == "same_identity" else "race-B"
        started = asyncio.Event()

        async def contender():
            async with child.engine.begin() as connection:
                await connection.execute(
                    text("""
                    INSERT INTO prices(ts,area,price_eur_per_kwh,price_currency,price_source)
                    VALUES ('2026-10-16T00:00:00Z','race-sentinel',-0.2,'SEK','tibber')
                """)
                )
                async with AsyncSession(bind=connection) as session:
                    started.set()
                    await ensure_tibber_scope(
                        session, area=area, home_id=contender_home, currency="SEK"
                    )

        async with child.engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(bind=connection) as session:
                await ensure_tibber_scope(session, area=area, home_id="race-A", currency="SEK")
            task = asyncio.create_task(contender())
            try:
                await asyncio.wait_for(started.wait(), timeout=10)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                if outcome == "winner_rollback":
                    await transaction.rollback()
                else:
                    await transaction.commit()
                if outcome == "collision":
                    with pytest.raises(ValueError, match="collision_or_metadata_conflict"):
                        await asyncio.wait_for(task, timeout=10)
                else:
                    await asyncio.wait_for(task, timeout=10)
            finally:
                if transaction.is_active:
                    await transaction.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with child.engine.connect() as connection:
            registered = (
                await connection.execute(text("SELECT home_id FROM price_scopes"))
            ).scalar_one()
            assert registered == ("race-B" if outcome == "winner_rollback" else "race-A")
            persisted = (
                await connection.execute(
                    text("SELECT count(*) FROM prices WHERE area='race-sentinel'")
                )
            ).scalar_one()
            assert persisted == (0 if outcome == "collision" else 1)


async def test_037_unique_full_identity_conflict_is_rejected_without_rebinding():
    async with owned_migration_child("tibberunique") as child:
        await child.alembic("upgrade", "037")
        async with child.engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO price_scopes(area,provider,home_id,currency,amount_basis,
                    interval_schema,tariff_schema_version,application_extra_fee)
                VALUES ('tb:0000000000000000000000000000','tibber','duplicate-A','SEK',
                    'provider_total','hourly_duration_weighted_v1','tibber_provider_total_v1',0)
            """)
            )
        with pytest.raises(ValueError, match="collision_or_metadata_conflict"):
            async with child.engine.begin() as connection:
                async with AsyncSession(bind=connection) as session:
                    await ensure_tibber_scope(
                        session,
                        area=tibber_scope("duplicate-A"),
                        home_id="duplicate-A",
                        currency="SEK",
                    )
        async with child.engine.connect() as connection:
            mappings = (
                await connection.execute(text("SELECT area,home_id FROM price_scopes"))
            ).all()
            assert mappings == [("tb:0000000000000000000000000000", "duplicate-A")]

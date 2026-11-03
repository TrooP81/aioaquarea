"""Explicit-only owned Timescale acceptance for P2.1 migration 038.

This module is not auto-collected by the regular suite. Invoke it only against
the named disposable database provisioned under the independent-test approval.
It never creates or drops containers, databases, networks, or volumes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

PINNED_TIMESCALE_IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)
BASE_REVISION = "d8d53fffd06495fa05569157aba8538367222583"
UTC = dt.timezone.utc


def test_P21_migration_038_remains_in_current_alembic_ancestry() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    project = Path(__file__).resolve().parents[1]
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision("038").down_revision == "037"
    assert scripts.get_revision("039").down_revision == "038"


def _owned_database_environment():
    suffix = os.environ["HPO_P21_DIAG_SUFFIX"]
    assert re.fullmatch(r"[0-9a-f]{16}", suffix)
    container = f"hpo-p21diag-db-{suffix}"
    assert os.environ["HPO_P21_DIAG_CONTAINER"] == container
    assert os.environ["HPO_P21_DIAG_NETWORK"] == f"hpo-p21diag-net-{suffix}"
    assert os.environ["HPO_P21_DIAG_VOLUME"] == f"hpo-p21diag-data-{suffix}"
    assert os.environ["HPO_P21_DIAG_IMAGE"] == PINNED_TIMESCALE_IMAGE
    assert os.environ["HPO_P21_DIAG_LABEL"] == "hpo.diag=p21"
    assert os.environ["HPO_P21_DIAG_NETWORK_INTERNAL"] == "true"

    url = make_url(os.environ["DATABASE_URL"])
    assert os.environ["DATABASE_URL"] == os.environ["HPO_P21_DIAG_DATABASE_URL"]
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == container
    assert url.port == 5432
    assert url.database == "hpo_p21diag"
    assert url.username == "hpo_p21diag"
    assert re.fullmatch(r"[0-9a-f]{64}", url.password or "")
    address = os.environ["HPO_P21_DIAG_DB_ADDRESS"]
    assert re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address)
    assert address not in {"127.0.0.1", "0.0.0.0"}
    assert url.port not in {5433, 5434}
    assert os.environ.get("HPO_TEST_OFFLINE") != "1"
    assert os.environ.get("APP_ENVIRONMENT") == "test"
    return suffix, url, address


def _run_alembic(revision: str, cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "alembic", "upgrade", revision]
    started = time.monotonic()
    print(f"SUBCOMMAND: {command!r}; CWD={cwd}; REVISION={revision}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    print(
        f"SUBCOMMAND_EXIT: {result.returncode}; DURATION_SECONDS: {time.monotonic() - started:.2f}"
    )
    return result


async def _wait_for_lock_waiters(engine, expected: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock' "
                    "AND pid <> pg_backend_pid()"
                )
            )
        if count >= expected:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"timed out waiting for {expected} PostgreSQL lock waiter(s)")


@pytest.mark.asyncio
async def test_P21_owned_timescale_037_to_038_transactions_races_and_rollback(
    monkeypatch, tmp_path
):
    suffix, url, db_address = _owned_database_environment()
    project = Path(__file__).resolve().parents[1]
    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_database(), current_user, "
                        "host(inet_server_addr()), inet_server_port(), "
                        "to_regclass('public.alembic_version')::text"
                    )
                )
            ).one()
            assert identity == ("hpo_p21diag", "hpo_p21diag", db_address, 5432, None)

        migrated = _run_alembic("037", project)
        assert migrated.returncode == 0, migrated.stderr

        legacy_plan_json = json.dumps({"legacy_marker": f"p21-{suffix}"}, sort_keys=True)
        legacy_audit_json = json.dumps({"seed": f"p21-{suffix}"}, sort_keys=True)
        async with engine.begin() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
            ) == "037"
            await connection.execute(
                text(
                    "INSERT INTO settings (key,value) VALUES "
                    "('comfort_schedule', :schedule), "
                    "('comfort_temp_min', '18.0'), "
                    "('comfort_temp_target', '20.0'), "
                    "('comfort_temp_max', '24.0'), "
                    "('timezone', 'Europe/Stockholm'), "
                    "('space_heating_baseline_mode', 'shadow')"
                ),
                {"schedule": '{"weekday":[7,8],"weekend":[9]}'},
            )
            legacy_plan_id = await connection.scalar(
                text(
                    "INSERT INTO plans "
                    "(horizon_start,horizon_end,plan_json,optimizer_version,status,status_reason) "
                    "VALUES (:start,:end,:plan,'rules_v8','diagnostic','p21 legacy seed') "
                    "RETURNING id"
                ),
                {
                    "start": dt.datetime(2026, 10, 19, 0, tzinfo=UTC),
                    "end": dt.datetime(2026, 10, 20, 0, tzinfo=UTC),
                    "plan": legacy_plan_json,
                },
            )
            legacy_audit_id = await connection.scalar(
                text(
                    "INSERT INTO audit_log (actor,action,payload_json,result) "
                    "VALUES ('p21_test','legacy_seed',:payload,'updated') RETURNING id"
                ),
                {"payload": legacy_audit_json},
            )

        migrated = _run_alembic("038", project)
        assert migrated.returncode == 0, migrated.stderr
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migration_config = Config(str(project / "alembic.ini"))
        migration_config.set_main_option("script_location", str(project / "migrations"))
        assert ScriptDirectory.from_config(migration_config).get_heads() == ["039"]

        from packages.core.house_contract import (
            current_revision_matches,
            ensure_current_contract,
            snapshot_from_revision,
            validate_plan_contract_snapshot,
        )
        from packages.core.models import (
            AuditLogRecord,
            HouseContractRevisionRecord,
            HouseInstallationRecord,
            OptimizationRequestRecord,
            PlanRecord,
        )
        from packages.core.settings_service import get_all_settings, set_settings_bulk

        async with factory() as session:
            async with session.begin():
                versions = (
                    await session.scalars(text("SELECT version_num FROM alembic_version"))
                ).all()
                assert versions == ["038"], "expected explicit Alembic 038 upgrade"
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM timescaledb_information.hypertables")
                    )
                    > 0
                )
                settings_after_migration = dict(
                    (
                        await session.execute(
                            text(
                                "SELECT key,value FROM settings WHERE key IN "
                                "('comfort_schedule','comfort_temp_min','comfort_temp_target',"
                                "'comfort_temp_max','timezone','space_heating_baseline_mode')"
                            )
                        )
                    ).all()
                )
                assert settings_after_migration == {
                    "comfort_schedule": '{"weekday":[7,8],"weekend":[9]}',
                    "comfort_temp_min": "18.0",
                    "comfort_temp_target": "20.0",
                    "comfort_temp_max": "24.0",
                    "timezone": "Europe/Stockholm",
                    "space_heating_baseline_mode": "shadow",
                }
                legacy_plan = await session.get(PlanRecord, legacy_plan_id)
                assert legacy_plan is not None
                assert legacy_plan.contract_state == "legacy_unknown"
                assert legacy_plan.contract_revision_id is None
                assert legacy_plan.plan_json == legacy_plan_json
                assert legacy_plan.status == "diagnostic"
                legacy_audit = await session.get(AuditLogRecord, legacy_audit_id)
                assert legacy_audit is not None and legacy_audit.payload_json == legacy_audit_json

        seeded_installation_id = uuid4()
        seeded_house_id = uuid4()
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO house_installations "
                        "(singleton_id,installation_id,house_id,demand_dependency_generation) "
                        "VALUES (1,:installation_id,:house_id,1)"
                    ),
                    {
                        "installation_id": seeded_installation_id,
                        "house_id": seeded_house_id,
                    },
                )

        migrated = _run_alembic("039", project)
        assert migrated.returncode == 0, migrated.stderr
        async with factory() as session:
            async with session.begin():
                versions = (
                    await session.scalars(text("SELECT version_num FROM alembic_version"))
                ).all()
                assert versions == ["039"], "expected explicit Alembic 039 upgrade"
                installation_state = (
                    await session.execute(
                        text(
                            "SELECT installation_id,house_id,demand_dependency_generation,"
                            "indoor_dependency_generation FROM house_installations "
                            "WHERE singleton_id=1"
                        )
                    )
                ).one()
                assert installation_state == (seeded_installation_id, seeded_house_id, 1, 1)

        initial_revision_id: int
        async with factory() as session:
            async with session.begin():
                effective_settings = await get_all_settings(session=session)
                assert effective_settings["comfort_temp_min"] == "18.0"
                assert effective_settings["timezone"] == "Europe/Stockholm"
                initial_revision = await ensure_current_contract(session)
                initial_revision_id = initial_revision.id
                assert initial_revision.contract_json["comfort_temp_min"] == 18.0
                assert initial_revision.contract_json["comfort_schedule"] == {
                    "weekday": [7, 8],
                    "weekend": [9],
                }
                installation = await session.get(HouseInstallationRecord, 1)
                assert installation is not None
                assert installation.indoor_dependency_generation == 1
                assert installation.current_revision_id == initial_revision.id
                assert await session.scalar(text("SELECT count(*) FROM house_installations")) == 1

        # Installation scope is checked by the real authenticated route before
        # any settings, revisions, audit rows, or plans can be changed.
        from packages.api.auth import require_auth
        from packages.api.routers import settings as settings_router

        @asynccontextmanager
        async def real_session():
            async with factory() as session:
                try:
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        monkeypatch.setattr(settings_router, "get_session", lambda: real_session())
        scoped_app = FastAPI(dependencies=[Depends(require_auth)])
        scoped_app.include_router(settings_router.router)
        async with factory() as session:
            installation = await session.get(HouseInstallationRecord, 1)
            assert installation is not None
            installation_id = installation.installation_id
        wrong_id = uuid4()
        before_scope_state: tuple
        async with engine.connect() as connection:
            before_scope_state = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT current_revision_id FROM house_installations WHERE singleton_id=1"
                        )
                    )
                ).one()
            ) + (
                await connection.scalar(text("SELECT count(*) FROM house_contract_revisions")),
                await connection.scalar(text("SELECT count(*) FROM audit_log")),
                await connection.scalar(
                    text("SELECT value FROM settings WHERE key='comfort_temp_target'")
                ),
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=scoped_app, raise_app_exceptions=False),
            base_url="http://p21-owned.invalid",
        ) as client:
            headers = {
                "Authorization": f"Bearer {os.environ['API_TOKEN']}",
                "If-Match": f'"{initial_revision_id}"',
            }
            wrong_get = await client.get(
                f"/api/installations/{wrong_id}/planning-settings", headers=headers
            )
            wrong_put = await client.put(
                f"/api/installations/{wrong_id}/planning-settings",
                headers=headers,
                json={"comfort_temp_target": 21.0},
            )
            wrong_preview = await client.post(
                f"/api/installations/{wrong_id}/planning-settings/preview",
                headers=headers,
                json={
                    "comfort_schedule": {"weekday": [8], "weekend": [9]},
                    "horizon_start": "2026-10-19T00:00:00Z",
                    "horizon_end": "2026-10-19T02:00:00Z",
                },
            )
        assert [wrong_get.status_code, wrong_put.status_code, wrong_preview.status_code] == [
            404,
            404,
            404,
        ]
        async with engine.connect() as connection:
            after_scope_state = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT current_revision_id FROM house_installations WHERE singleton_id=1"
                        )
                    )
                ).one()
            ) + (
                await connection.scalar(text("SELECT count(*) FROM house_contract_revisions")),
                await connection.scalar(text("SELECT count(*) FROM audit_log")),
                await connection.scalar(
                    text("SELECT value FROM settings WHERE key='comfort_temp_target'")
                ),
            )
        assert after_scope_state == before_scope_state

        # The migration's singleton guard rejects any second installation.
        async with engine.connect() as connection:
            try:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO house_installations "
                            "(singleton_id,installation_id,house_id,demand_dependency_generation) "
                            "VALUES (2,:installation,:house,1)"
                        ),
                        {"installation": wrong_id, "house": uuid4()},
                    )
            except DBAPIError:
                pass
            else:
                raise AssertionError("second active installation unexpectedly inserted")

        start = dt.datetime(2026, 10, 19, 0, tzinfo=UTC)
        end = start + dt.timedelta(hours=2)
        async with factory() as session:
            async with session.begin():
                revision = await session.get(HouseContractRevisionRecord, initial_revision_id)
                assert revision is not None
                snapshot = await snapshot_from_revision(session, revision.id, start, end)
                plan = PlanRecord(
                    horizon_start=start,
                    horizon_end=end,
                    plan_json=json.dumps({"house_contract_snapshot": snapshot}),
                    optimizer_version="rules_v8",
                    status="diagnostic",
                    status_reason="observe_only_build",
                    contract_revision_id=revision.id,
                    contract_hash=revision.contract_hash,
                    contract_state="current",
                )
                session.add(plan)
                await session.flush()
                historical_plan_id = plan.id
                assert validate_plan_contract_snapshot(plan, snapshot)

        # Revision and revision-bound snapshot triggers enforce immutability in
        # PostgreSQL, independently of the Python validator.
        async with engine.connect() as connection:
            for statement in (
                "UPDATE house_contract_revisions SET source='tampered' WHERE id=:revision_id",
                "DELETE FROM house_contract_revisions WHERE id=:revision_id",
            ):
                try:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(statement), {"revision_id": initial_revision_id}
                        )
                except DBAPIError as exc:
                    assert "immutable" in str(exc).lower()
                else:
                    raise AssertionError("revision immutability trigger did not reject mutation")
            try:
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE plans SET plan_json='{}' WHERE id=:plan_id"),
                        {"plan_id": historical_plan_id},
                    )
            except DBAPIError as exc:
                assert "immutable" in str(exc).lower()
            else:
                raise AssertionError("snapshot immutability trigger did not reject mutation")

        # Inject a failure at audit insertion: setting, new revision, plan
        # invalidation, and audit must all roll back together.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE FUNCTION p21diag_reject_contract_audit() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "IF NEW.action='house_contract_saved' THEN "
                    "RAISE EXCEPTION 'p21 diagnostic audit failure'; END IF; "
                    "RETURN NEW; END; $$"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER p21diag_reject_contract_audit "
                    "BEFORE INSERT ON audit_log FOR EACH ROW "
                    "EXECUTE FUNCTION p21diag_reject_contract_audit()"
                )
            )
        async with engine.connect() as connection:
            before_rollback = (
                await connection.scalar(
                    text("SELECT value FROM settings WHERE key='comfort_temp_target'")
                ),
                await connection.scalar(text("SELECT count(*) FROM house_contract_revisions")),
                await connection.scalar(text("SELECT count(*) FROM audit_log")),
                await connection.scalar(
                    text("SELECT contract_state FROM plans WHERE id=:id"),
                    {"id": historical_plan_id},
                ),
            )
        try:
            async with factory() as session:
                async with session.begin():
                    await set_settings_bulk(
                        {"comfort_temp_target": "20.5"},
                        session=session,
                        expected_revision_id=initial_revision_id,
                        source="p21_owned_db_rollback",
                    )
        except DBAPIError:
            pass
        else:
            raise AssertionError("injected audit failure did not abort the settings transaction")
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER p21diag_reject_contract_audit ON audit_log")
            )
            await connection.execute(text("DROP FUNCTION p21diag_reject_contract_audit()"))
        async with engine.connect() as connection:
            after_rollback = (
                await connection.scalar(
                    text("SELECT value FROM settings WHERE key='comfort_temp_target'")
                ),
                await connection.scalar(text("SELECT count(*) FROM house_contract_revisions")),
                await connection.scalar(text("SELECT count(*) FROM audit_log")),
                await connection.scalar(
                    text("SELECT contract_state FROM plans WHERE id=:id"),
                    {"id": historical_plan_id},
                ),
            )
        assert after_rollback == before_rollback

        # Hold the first real API write open after it has updated settings and
        # appended the revision. The second HTTP writer waits on the database
        # lock, then receives the route's actual 409 after the first commits.
        original_set_settings_bulk = settings_router.set_settings_bulk
        first_revision_created = asyncio.Event()
        release_first_writer = asyncio.Event()

        async def hold_first_write(
            updates, *, session=None, expected_revision_id=None, source="legacy_unversioned_write"
        ):
            revision_id = await original_set_settings_bulk(
                updates,
                session=session,
                expected_revision_id=expected_revision_id,
                source=source,
            )
            if updates.get("comfort_temp_target") == "20.5":
                first_revision_created.set()
                await release_first_writer.wait()
            return revision_id

        monkeypatch.setattr(settings_router, "set_settings_bulk", hold_first_write)
        api_headers = {
            "Authorization": f"Bearer {os.environ['API_TOKEN']}",
            "If-Match": f'"{initial_revision_id}"',
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=scoped_app, raise_app_exceptions=False),
            base_url="http://p21-owned.invalid",
        ) as client:
            writer_one = asyncio.create_task(
                client.put(
                    f"/api/installations/{installation_id}/planning-settings",
                    headers=api_headers,
                    json={"comfort_temp_target": 20.5},
                )
            )
            writer_two = None
            try:
                await asyncio.wait_for(first_revision_created.wait(), timeout=10)
                writer_two = asyncio.create_task(
                    client.put(
                        f"/api/installations/{installation_id}/planning-settings",
                        headers=api_headers,
                        json={"comfort_temp_target": 21.0},
                    )
                )
                await _wait_for_lock_waiters(engine, 1)
            finally:
                release_first_writer.set()
            tasks = [writer_one]
            if writer_two is not None:
                tasks.append(writer_two)
            writer_responses = await asyncio.gather(*tasks)
        assert sorted(response.status_code for response in writer_responses) == [200, 409]
        winner_response = next(
            response for response in writer_responses if response.status_code == 200
        )
        conflict_response = next(
            response for response in writer_responses if response.status_code == 409
        )
        assert winner_response.headers.get("X-Contract-Revision")
        assert conflict_response.json()["detail"]["reason"] == "contract_revision_changed"
        winning_revision_id = int(winner_response.headers["X-Contract-Revision"])
        async with engine.connect() as connection:
            winner_state = (
                await connection.execute(
                    text("SELECT value FROM settings WHERE key='comfort_temp_target'")
                )
            ).scalar_one()
            invalidation = (
                await connection.execute(
                    text(
                        "SELECT contract_state,invalidated_by_revision_id,"
                        "contract_invalidation_reason,contract_invalidated_at "
                        "FROM plans WHERE id=:id"
                    ),
                    {"id": historical_plan_id},
                )
            ).one()
            assert winner_state in {"20.5", "21.0"}
            assert invalidation.contract_state == "stale"
            assert invalidation.invalidated_by_revision_id == winning_revision_id
            assert invalidation.contract_invalidation_reason == "contract_revision_changed"
            assert invalidation.contract_invalidated_at is not None
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE action='house_contract_saved' AND result='updated'"
                    )
                )
                >= 1
            )

        # Saved historical scope replays exactly after the settings edit, while
        # the migrated pre-038 row remains legacy_unknown without invention.
        async with factory() as session:
            async with session.begin():
                saved_plan = await session.get(PlanRecord, historical_plan_id)
                legacy_plan = await session.get(PlanRecord, legacy_plan_id)
                old_revision = await session.get(HouseContractRevisionRecord, initial_revision_id)
                assert saved_plan is not None and old_revision is not None
                saved_snapshot = json.loads(saved_plan.plan_json)["house_contract_snapshot"]
                replay_snapshot = await snapshot_from_revision(
                    session, initial_revision_id, start, end
                )
                assert saved_plan.contract_state == "stale"
                assert validate_plan_contract_snapshot(saved_plan, saved_snapshot)
                assert replay_snapshot == saved_snapshot
                assert legacy_plan is not None
                assert legacy_plan.contract_state == "legacy_unknown"
                assert legacy_plan.contract_revision_id is None
                assert "house_contract_snapshot" not in json.loads(legacy_plan.plan_json)

        # A solve frozen before a settings commit must fail final admission.
        frozen_revision_id = winning_revision_id
        async with factory() as session:
            frozen_revision = await session.get(HouseContractRevisionRecord, frozen_revision_id)
            assert frozen_revision is not None
            frozen_hash = frozen_revision.contract_hash
        frozen = asyncio.Event()
        settings_committed = asyncio.Event()
        rejected_plan_ids: list[int] = []

        async def solve_then_admit():
            frozen.set()
            await settings_committed.wait()
            async with factory() as session:
                async with session.begin():
                    if not await current_revision_matches(session, frozen_revision_id, frozen_hash):
                        return False
                    candidate = PlanRecord(
                        horizon_start=start,
                        horizon_end=end,
                        plan_json="{}",
                        optimizer_version="rules_v8",
                        status="diagnostic",
                        contract_revision_id=frozen_revision_id,
                        contract_hash=frozen_hash,
                        contract_state="current",
                    )
                    session.add(candidate)
                    await session.flush()
                    rejected_plan_ids.append(candidate.id)
                    return True

        async def commit_settings_before_admission():
            await frozen.wait()
            async with factory() as session:
                async with session.begin():
                    await set_settings_bulk(
                        {"comfort_temp_target": "19.5"},
                        session=session,
                        expected_revision_id=frozen_revision_id,
                        source="p21_owned_db_solve_order",
                    )
            settings_committed.set()

        solve_task = asyncio.create_task(solve_then_admit())
        writer_task = asyncio.create_task(commit_settings_before_admission())
        solve_admitted = await solve_task
        await writer_task
        assert solve_admitted is False
        assert rejected_plan_ids == []

        # If admission commits first, the next revision must atomically stale
        # that plan. Events are set only after the first transaction commits.
        async with factory() as session:
            installation = await session.get(HouseInstallationRecord, 1)
            assert installation is not None and installation.current_revision_id is not None
            latest_revision = await session.get(
                HouseContractRevisionRecord, installation.current_revision_id
            )
            assert latest_revision is not None
            latest_revision_id = latest_revision.id
            latest_hash = latest_revision.contract_hash
        admission_committed = asyncio.Event()
        early_plan_id: list[int] = []

        async def commit_admission_first():
            async with factory() as session:
                async with session.begin():
                    assert await current_revision_matches(session, latest_revision_id, latest_hash)
                    candidate = PlanRecord(
                        horizon_start=start,
                        horizon_end=end,
                        plan_json="{}",
                        optimizer_version="rules_v8",
                        status="diagnostic",
                        contract_revision_id=latest_revision_id,
                        contract_hash=latest_hash,
                        contract_state="current",
                    )
                    session.add(candidate)
                    await session.flush()
                    early_plan_id.append(candidate.id)
            admission_committed.set()

        async def settings_after_admission():
            await admission_committed.wait()
            async with factory() as session:
                async with session.begin():
                    await set_settings_bulk(
                        {"comfort_temp_target": "20.0"},
                        session=session,
                        expected_revision_id=latest_revision_id,
                        source="p21_owned_db_admission_order",
                    )

        admission_task = asyncio.create_task(commit_admission_first())
        settings_task = asyncio.create_task(settings_after_admission())
        await asyncio.gather(admission_task, settings_task)
        assert len(early_plan_id) == 1
        async with engine.connect() as connection:
            early_state = (
                await connection.execute(
                    text(
                        "SELECT contract_state,invalidated_by_revision_id FROM plans WHERE id=:id"
                    ),
                    {"id": early_plan_id[0]},
                )
            ).one()
            assert early_state.contract_state == "stale"
            assert early_state.invalidated_by_revision_id is not None

        # A queued request tied to an old revision is failed at claim and never
        # invokes the optimizer.
        from packages.optimizer import main as optimizer_main

        async with factory() as session:
            async with session.begin():
                stale_request = OptimizationRequestRecord(
                    requested_by="p21_owned_db_test",
                    status="pending",
                    contract_revision_id=latest_revision_id,
                    contract_hash=latest_hash,
                )
                session.add(stale_request)
                await session.flush()
                stale_request_id = stale_request.id

        solver = AsyncMock(side_effect=AssertionError("stale queued request invoked solver"))
        monkeypatch.setattr(
            optimizer_main,
            "run_optimization",
            solver,
        )
        await optimizer_main.process_pending_optimization_requests()
        solver.assert_not_awaited()
        async with factory() as session:
            stale_request = await session.get(OptimizationRequestRecord, stale_request_id)
            assert stale_request is not None
            assert stale_request.status == "failed"
            assert stale_request.error == "refused_contract_changed"

        # Exercise the explicit non-destructive downgrade refusal. This is not
        # considered application rollback; the old application check follows.
        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "037"],
            cwd=project,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        print("SUBCOMMAND: python -m alembic downgrade 037")
        print(downgrade.stdout, end="")
        print(downgrade.stderr, end="", file=sys.stderr)
        print(f"EXPECTED_DESTRUCTIVE_DOWNGRADE_REFUSAL_EXIT: {downgrade.returncode}")
        assert downgrade.returncode != 0
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
            ) == "039"
            assert await connection.scalar(
                text("SELECT to_regclass('public.house_contract_revisions') IS NOT NULL")
            )

        # Run actual core readers from the pre-P2.1 application source against
        # the additive 039 schema. Source comes from the immutable base revision.
        baseline_packages = Path(os.environ["HPO_P21_DIAG_BASE_PACKAGES"])
        assert baseline_packages.is_dir()
        baseline_code = (
            "import asyncio,json\n"
            "from sqlalchemy import select\n"
            "from packages.core.database import engine\n"
            "from packages.core.models import PlanRecord\n"
            "from packages.core.settings_service import get_all_settings\n"
            "async def main():\n"
            " settings=await get_all_settings()\n"
            " async with engine.connect() as connection:\n"
            "  rows=(await connection.execute(select(PlanRecord.id,PlanRecord.plan_json,PlanRecord.status))).all()\n"
            " print(json.dumps({'target':settings['comfort_temp_target'],'timezone':settings['timezone'],'legacy_ids':[r.id for r in rows if 'legacy_marker' in r.plan_json],'plan_count':len(rows)}))\n"
            " await engine.dispose()\n"
            "asyncio.run(main())\n"
        )
        baseline_env = os.environ.copy()
        baseline_env["PYTHONPATH"] = str(baseline_packages)
        baseline_env["PYTHONDONTWRITEBYTECODE"] = "1"
        baseline = subprocess.run(
            [sys.executable, "-c", baseline_code],
            cwd=str(tmp_path),
            env=baseline_env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        print(f"SUBCOMMAND: Python baseline application core read at {BASE_REVISION}")
        print(baseline.stdout, end="")
        print(baseline.stderr, end="", file=sys.stderr)
        print(f"BASELINE_APP_EXIT: {baseline.returncode}")
        assert baseline.returncode == 0, baseline.stderr
        baseline_result = json.loads(baseline.stdout.strip().splitlines()[-1])
        assert baseline_result["target"] == "20.0"
        assert baseline_result["timezone"] == "Europe/Stockholm"
        assert legacy_plan_id in baseline_result["legacy_ids"]

        # Corrupt snapshot responses are unknown, never measured with live
        # settings; covered by direct API unit tests and repeated at DB boundary.
        from packages.api.routers.optimizer import _captured_plan_measurement_scope

        async with factory() as session:
            persisted = await session.get(PlanRecord, historical_plan_id)
            assert persisted is not None
            persisted_snapshot = json.loads(persisted.plan_json)["house_contract_snapshot"]
            mismatch = dict(persisted_snapshot, revision_id=persisted.contract_revision_id + 999)
            assert _captured_plan_measurement_scope(persisted, mismatch) is None

        print(
            "P21_DB_ACCEPTANCE: migration-preservation, triggers, scope, "
            "revisioned-writer-race, rollback, both admission orders, "
            "stale-queue claim, historical replay, and pre-038 application read PASS"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_route", ["canonical", "compatibility"])
async def test_P21_mixed_writers_global_lock_order(monkeypatch, first_route):
    _, url, _ = _owned_database_environment()
    from packages.api.routers import settings as routes
    from packages.api.auth import require_auth
    from packages.core import house_contract, settings_service

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction():
        async with factory() as session, session.begin():
            yield session

    monkeypatch.setattr(routes, "get_session", transaction)
    monkeypatch.setattr(settings_service, "get_session", transaction)
    # Each branch must make a semantic change, independently of prior tests.
    await settings_service.set_settings_bulk({"comfort_temp_target": "20.5"})
    app = FastAPI(dependencies=[Depends(require_auth)])
    app.include_router(routes.router)
    original = house_contract._lock_contract
    held, release = asyncio.Event(), asyncio.Event()
    paused = False

    async def controlled_lock(session):
        nonlocal paused
        await original(session)
        if asyncio.current_task().get_name() == "p21-first" and not paused:
            paused = True
            held.set()
            await release.wait()

    try:
        async with transaction() as session:
            revision = await house_contract.ensure_current_contract(session)
            installation = revision.contract_json["installation_id"]
            revision_id = revision.id
            before_count = await session.scalar(
                text("SELECT count(*) FROM house_contract_revisions")
            )
        monkeypatch.setattr(house_contract, "_lock_contract", controlled_lock)
        paths = {
            "canonical": f"/api/installations/{installation}/planning-settings",
            "compatibility": "/api/settings",
        }
        other = "compatibility" if first_route == "canonical" else "canonical"
        headers = {
            "Authorization": f"Bearer {os.environ['API_TOKEN']}",
            "If-Match": f'"{revision_id}"',
        }

        def payload(route, target):
            return (
                {"comfort_temp_target": target}
                if route == "canonical"
                else {"settings": {"comfort_temp_target": str(target)}}
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://p21-owned.invalid",
        ) as client:
            first = asyncio.create_task(
                client.put(paths[first_route], headers=headers, json=payload(first_route, 20.55)),
                name="p21-first",
            )
            second = None
            try:
                await asyncio.wait_for(held.wait(), 10)
                second = asyncio.create_task(
                    client.put(paths[other], headers=headers, json=payload(other, 21.05))
                )
                await _wait_for_lock_waiters(engine, 1)
            finally:
                release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 15)
        assert [response.status_code for response in responses] == [200, 409], [
            response.text for response in responses
        ]
        async with transaction() as session:
            assert (
                await session.scalar(text("SELECT count(*) FROM house_contract_revisions"))
                == before_count + 1
            )
            current = await house_contract.ensure_current_contract(session)
            assert current.contract_json["comfort_temp_target"] == 20.55
        retry = await client_retry(app, paths[other], payload(other, 21.05), headers, current.id)
        assert retry.status_code == 200, retry.text
    finally:
        release.set()
        await engine.dispose()


async def client_retry(app, path, payload, headers, revision_id):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://p21-owned.invalid",
    ) as client:
        return await client.put(
            path, headers={**headers, "If-Match": f'"{revision_id}"'}, json=payload
        )


async def test_P21_all_raw_schedule_routes_leave_owned_database_unchanged(monkeypatch):
    _, url, _ = _owned_database_environment()
    from packages.api.routers import settings as routes
    from packages.api.auth import require_auth
    from packages.core import house_contract, settings_service

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction():
        async with factory() as session, session.begin():
            yield session

    async def state():
        async with engine.connect() as connection:
            return [
                await connection.scalar(text(query))
                for query in (
                    "SELECT jsonb_agg(to_jsonb(s) ORDER BY key)::text FROM settings s",
                    "SELECT count(*) FROM house_contract_revisions",
                    "SELECT count(*) FROM audit_log",
                    "SELECT jsonb_agg(to_jsonb(p) ORDER BY id)::text FROM plans p",
                    "SELECT jsonb_agg(to_jsonb(i))::text FROM house_installations i",
                )
            ]

    try:
        monkeypatch.setattr(routes, "get_session", transaction)
        monkeypatch.setattr(settings_service, "get_session", transaction)
        app = FastAPI(dependencies=[Depends(require_auth)])
        app.include_router(routes.router)
        async with transaction() as session:
            revision = await house_contract.ensure_current_contract(session)
        before = await state()
        headers = {
            "Authorization": f"Bearer {os.environ['API_TOKEN']}",
            "If-Match": f'"{revision.id}"',
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://p21-owned.invalid",
        ) as client:
            for invalid in (
                {"weekday": [True], "weekend": []},
                {"weekday": [6, 6], "weekend": []},
                {"weekday": [-1], "weekend": []},
                {"weekday": [24], "weekend": []},
                {"weekday": ["6"], "weekend": []},
                {"weekday": "6", "weekend": []},
                {"weekday": [6.5], "weekend": []},
                {"weekday": [], "weekend": [], "unknown": []},
            ):
                for path, payload in (
                    (
                        f"/api/installations/{revision.contract_json['installation_id']}/planning-settings",
                        {"comfort_schedule": invalid},
                    ),
                    ("/api/settings", {"settings": {"comfort_schedule": json.dumps(invalid)}}),
                    ("/api/comfort-schedule", invalid),
                ):
                    response = await client.put(path, headers=headers, json=payload)
                    assert 400 <= response.status_code < 500, response.text
                    assert await state() == before
    finally:
        await engine.dispose()

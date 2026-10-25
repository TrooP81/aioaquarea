"""Calendar, API and migration qualification for proven-owned disposable storage.

Run with --noconftest and a guarded, preconfigured test environment. Each case
rolls back its rows and any metadata-created table/helper changes.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import http.client
import importlib.metadata
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from sqlalchemy import MetaData, event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.cop_monitoring import (
    InvalidStoredPeriod,
    local_instants,
    period_bounds,
    snapshot_from_stored,
)
from packages.core.models import (
    COPRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    ExecutorVerificationReadRecord,
    ManualCOPObservationRecord,
    OptimizationRequestRecord,
    PlanActionRecord,
    PlanRecord,
    SettingRecord,
    SpaceHeatingGateRecord,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


POSTGRES_IMAGE = (
    "timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57"
)
REDIS_IMAGE = (
    "redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952"
)
TEST_DATABASE_URL = "postgresql+asyncpg://heatpump:heatpump_test@127.0.0.1:5433/heatpump_test"
TEST_REDIS_URL = "redis://127.0.0.1:6380/0"
TEST_SECRET_KEY = "deployment-verify-public-test-secret"
TEST_ENDPOINTS = {
    "postgres": {
        "scheme": "postgresql+asyncpg",
        "host": "127.0.0.1",
        "port": 5433,
        "database": "heatpump_test",
        "user": "heatpump",
    },
    "redis": {"scheme": "redis", "host": "127.0.0.1", "port": 6380, "database": 0},
}


def _utc_timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, "ownership timestamp must be timezone-aware"
    return parsed.astimezone(dt.timezone.utc)


def _inspect_owned_resources(service_ids, namespace_id, evidence_dir):
    started = dt.datetime.now(dt.timezone.utc)
    deadline = started + dt.timedelta(seconds=10)
    receipt_path = evidence_dir / ("platform-" + uuid.uuid4().hex + ".json")
    assert os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock") == (
        "unix:///var/run/docker.sock"
    ), "remote Docker platform is forbidden"
    paths = [f"/containers/{service_id}/json" for service_id in service_ids]
    paths.append(f"/networks/{namespace_id}")
    receipt = {
        "pid": os.getpid(),
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "method": "GET",
        "paths": paths,
        "complete": False,
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    results = []
    try:
        for path in paths:
            remaining = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
            assert remaining > 0, "owned platform inspection timed out"
            connection = http.client.HTTPConnection("localhost", timeout=remaining)
            try:
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(remaining)
                connection.sock.connect("/var/run/docker.sock")
                connection.request("GET", path)
                response = connection.getresponse()
                assert response.status == 200, "owned platform object is unavailable"
                results.append(json.loads(response.read()))
            finally:
                connection.close()
        receipt["complete"] = True
        return {"services": results[:-1], "namespace": results[-1]}
    finally:
        receipt["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def migration_targets():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = scripts.get_heads()
    assert len(heads) == 1, "migration verification requires one head"
    head = heads[0]
    assert "032" in {revision.revision for revision in scripts.walk_revisions(head=head)}, (
        "current head must retain migration 032"
    )
    predecessor = scripts.get_revision("032").down_revision
    assert isinstance(predecessor, str), "032 must have one predecessor"
    return head, predecessor


LEGACY_MANUAL_REVISIONS = ("032", "033", "034", "035")
LOCAL_PERIOD_REVISION = "036"
CURRENT_HEAD_REVISION = "039"
HISTORICAL_HEAD = LEGACY_MANUAL_REVISIONS[-1]
LEGACY_BOUNDARY_COLUMNS = ("period_start", "period_end")
LEGACY_CALENDAR_CONSTRAINTS = ("ck_manual_cop_calendar", "ck_manual_cop_period_order")
LOCAL_PERIOD_CONSTRAINTS = {
    "ck_manual_cop_observed_at": "CHECK ((observed_at <= CURRENT_TIMESTAMP))",
    "ck_manual_cop_completeness": (
        "CHECK (((completeness)::text = ANY ((ARRAY['partial'::character varying, "
        "'closed'::character varying, 'unknown'::character varying])::text[])))"
    ),
}
HELPER_SIGNATURE = "public.manual_cop_calendar_midnight(timestamp,text)"


async def retained_storage_state(connection):
    # The marker decides the expected physical shape; rows are read from the actual
    # table columns so historical (032-035) and local-period (036) states compare exactly.
    marker = tuple(
        (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    )
    assert len(marker) == 1, "retained storage requires exactly one Alembic marker"
    revision = marker[0]
    assert revision in (*LEGACY_MANUAL_REVISIONS, LOCAL_PERIOD_REVISION, CURRENT_HEAD_REVISION), (
        "unknown manual ledger revision"
    )
    helper_oid = (
        await connection.execute(text(f"SELECT to_regprocedure('{HELPER_SIGNATURE}')::oid"))
    ).scalar_one()
    if revision in {LOCAL_PERIOD_REVISION, CURRENT_HEAD_REVISION}:
        assert helper_oid is None, "036 must not retain the SQL calendar helper"
        helper = None
    else:
        assert helper_oid is not None, "historical manual ledger requires the calendar helper"
        helper = (
            await connection.execute(
                text(
                    "SELECT pg_get_functiondef(oid),prosrc,proacl,pg_get_userbyid(proowner) "
                    f"FROM pg_proc WHERE oid='{HELPER_SIGNATURE}'::regprocedure"
                )
            )
        ).one()
    state = {
        "rows": tuple(
            (
                await connection.execute(
                    text(
                        "SELECT * FROM public.manual_cop_observations "
                        "ORDER BY device_id, granularity, period_date"
                    )
                )
            ).all()
        ),
        "sequence": (
            await connection.execute(
                text("SELECT last_value,is_called FROM public.manual_cop_revision_seq")
            )
        ).one(),
        "sequence_configuration": (
            await connection.execute(
                text(
                    "SELECT sequenceowner,data_type,start_value,min_value,max_value,"
                    "increment_by,cycle,cache_size FROM pg_sequences "
                    "WHERE schemaname='public' AND sequencename='manual_cop_revision_seq'"
                )
            )
        ).one(),
        "helper": helper,
        "marker": marker,
    }
    assert state["sequence_configuration"].cycle is False
    queries = {
        "columns": (
            "SELECT a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
            "pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid='public.manual_cop_observations'::regclass "
            "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"
        ),
        "constraints": (
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='public.manual_cop_observations'::regclass ORDER BY conname"
        ),
        "indexes": (
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='manual_cop_observations' ORDER BY indexname"
        ),
        "permissions": (
            "SELECT relname,relacl,pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid IN ('public.manual_cop_observations'::regclass,"
            "'public.manual_cop_revision_seq'::regclass) ORDER BY relname"
        ),
        "hypertables": (
            "SELECT hypertable_schema,hypertable_name,num_dimensions "
            "FROM timescaledb_information.hypertables ORDER BY hypertable_schema,hypertable_name"
        ),
    }
    for key, query in queries.items():
        state[key] = tuple((await connection.execute(text(query))).all())
    columns = {row[0] for row in state["columns"]}
    constraints = dict(state["constraints"])
    if revision in {LOCAL_PERIOD_REVISION, CURRENT_HEAD_REVISION}:
        assert columns.isdisjoint(LEGACY_BOUNDARY_COLUMNS), "036 must not store boundaries"
        assert set(constraints).isdisjoint(LEGACY_CALENDAR_CONSTRAINTS), "036 SQL calendar"
        for name, definition in LOCAL_PERIOD_CONSTRAINTS.items():
            assert constraints.get(name) == definition, f"036 replacement {name} mismatch"
    else:
        assert columns.issuperset(LEGACY_BOUNDARY_COLUMNS), "historical boundaries missing"
        assert set(constraints).issuperset(LEGACY_CALENDAR_CONSTRAINTS), "historical checks"
        for name in LOCAL_PERIOD_CONSTRAINTS:
            assert "period_" in constraints.get(name, ""), f"historical {name} mismatch"
    return state


def owned_test_environment(environment=None, platform_reader=None):
    environment = os.environ if environment is None else environment
    try:
        receipt = json.loads(environment["COP_MANUAL_TEST_RECEIPT"])
        context = json.loads(environment["COP_MANUAL_TEST_SERVICE_CONTEXT"])
        url = make_url(environment["DATABASE_URL"])
        redis_url = make_url(environment["REDIS_URL"])
        assert (
            url.drivername,
            url.host,
            url.port,
            url.database,
            url.username,
            url.password,
            dict(url.query),
        ) == (
            "postgresql+asyncpg",
            "127.0.0.1",
            5433,
            "heatpump_test",
            "heatpump",
            "heatpump_test",
            {},
        ), "unowned PostgreSQL endpoint"
        assert (
            redis_url.drivername,
            redis_url.host,
            redis_url.port,
            redis_url.database,
            redis_url.username,
            redis_url.password,
            dict(redis_url.query),
        ) == ("redis", "127.0.0.1", 6380, "0", None, None, {}), "unowned Redis endpoint"
        assert environment["APP_ENVIRONMENT"] == "test", "non-test application environment"
        assert environment["API_TOKEN"] == "test-token", "non-test API token"
        assert environment["SECRET_KEY"] == TEST_SECRET_KEY, "non-test signing key"
        assert (
            environment["PRICE_PROVIDER"],
            environment["ENTSOE_API_TOKEN"],
            environment["AQUAREA_USERNAME"],
            environment["AQUAREA_PASSWORD"],
        ) == ("entsoe", "test-token", "test@test.com", "testpass"), (
            "non-test provider configuration"
        )
        assert receipt["version"] == 1, "unknown ownership receipt version"
        assert receipt["endpoints"] == TEST_ENDPOINTS, "receipt endpoint mismatch"
        assert receipt["token"] == environment["COP_MANUAL_TEST_OWNED"], "ownership token mismatch"
        assert receipt["revision"] == environment["COP_MANUAL_TEST_REVISION"], "source mismatch"
        assert re.fullmatch(r"[0-9a-f]{40}(?:\+[0-9a-f]{64})?", receipt["revision"])
        commit = receipt["commit"]
        digest = receipt["source_manifest_sha256"]
        assert re.fullmatch(r"[0-9a-f]{40}", commit), "malformed tested commit"
        assert re.fullmatch(r"[0-9a-f]{64}", digest), "malformed source manifest digest"
        assert digest == environment[SOURCE_MANIFEST_DIGEST_ENV], "source manifest digest mismatch"
        now = dt.datetime.now(dt.timezone.utc)
        created = _utc_timestamp(receipt["created_at"])
        expires = _utc_timestamp(receipt["expires_at"])
        assert created <= now < expires, "ownership receipt is stale"
        assert 0 < (expires - created).total_seconds() <= 1800, "unbounded ownership lease"
        provider = receipt["provider"]
        assert provider in {"github", "temp"}, "unknown ownership provider"
        # GitHub binds the checked-out merge SHA; TEMP binds the genuine commit+digest.
        assert receipt["revision"] == (commit if provider == "github" else f"{commit}+{digest}"), (
            "source revision mismatch"
        )
        if provider == "github":
            assert environment["GITHUB_ACTIONS"] == "true", "missing GitHub platform context"
            assert receipt["run_id"] == environment["GITHUB_RUN_ID"], "run mismatch"
            assert receipt["run_attempt"] == environment["GITHUB_RUN_ATTEMPT"], "attempt mismatch"
            assert receipt["job"] == environment["GITHUB_JOB"], "job mismatch"
            assert receipt["revision"] == environment["GITHUB_SHA"], "checkout mismatch"
            assert receipt["token"] == (
                f"cop-manual-mon-{receipt['run_id']}-{receipt['run_attempt']}"
            ), "run token mismatch"
            temporary_parent = Path(environment["RUNNER_TEMP"]).resolve(strict=True)
        else:
            assert re.fullmatch(r"[0-9a-f]{32}", receipt["lease_id"]), "invalid TEMP lease"
            assert receipt["lease_id"] == environment["COP_MANUAL_TEST_LEASE_ID"], "lease mismatch"
            assert receipt["token"] == "cop-manual-mon-" + receipt["lease_id"], (
                "lease token mismatch"
            )
            temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
        root = Path(receipt["temp_root"])
        model_root = Path(receipt["model_root"])
        assert root.name == receipt["token"], "unowned TEMP root"
        assert root.is_relative_to(temporary_parent) and root != temporary_parent, (
            "unsafe TEMP root"
        )
        assert provider != "github" or root.parent == temporary_parent, "unexpected runner root"
        assert model_root == root / "models", "unsafe model root"
        assert model_root == Path(environment["MODEL_DIR"]), "model configuration mismatch"
        for path in (root, model_root, root / "evidence"):
            assert path.is_dir() and path.resolve(strict=True) == path, "unsafe ownership directory"
            assert all(not parent.is_symlink() for parent in (path, *path.parents)), "symlink root"
        assert not list(model_root.iterdir()), "model root must contain no payloads"
        assert json.loads((root / "ownership.json").read_text(encoding="utf-8")) == receipt, (
            "TEMP creation receipt mismatch"
        )
        assert receipt["roots"] == {
            "temp_inode": root.stat().st_ino,
            "model_inode": model_root.stat().st_ino,
        }, "TEMP directory identity mismatch"
        assert Path(environment["COP_MANUAL_TEST_EVIDENCE"]) == root / "evidence"
        manifest = root / "evidence" / SOURCE_MANIFEST_NAME
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == digest, (
            "source manifest file mismatch"
        )
        namespace = receipt["namespace"]
        assert re.fullmatch(r"[0-9a-f]{64}", namespace["id"]), "unknown namespace"
        services = receipt["services"]
        assert set(services) == set(context) == {"postgres", "redis"}, "service context mismatch"
        expected_images = {"postgres": POSTGRES_IMAGE, "redis": REDIS_IMAGE}
        service_ids = []
        for role, service in services.items():
            assert re.fullmatch(r"[0-9a-f]{64}", service["id"]), "unknown service"
            assert service["id"] == context[role]["id"], "service ID mismatch"
            assert service["image"] == expected_images[role], "service image mismatch"
            assert context[role]["network"] == namespace["name"], "namespace context mismatch"
            assert (
                created - dt.timedelta(minutes=15)
                <= _utc_timestamp(service["created_at"])
                <= (created)
            ), "service was not created for this lease"
            service_ids.append(service["id"])
        assert len(set(service_ids)) == 2, "services must be distinct"
        reader = _inspect_owned_resources if platform_reader is None else platform_reader
        actual = reader(service_ids, namespace["id"], root / "evidence")
        actual_namespace = actual["namespace"]
        assert (actual_namespace["Id"], actual_namespace["Name"]) == (
            namespace["id"],
            namespace["name"],
        ), "unproven namespace"
        assert (actual_namespace["Driver"], actual_namespace["Scope"]) == ("bridge", "local"), (
            "unknown platform namespace"
        )
        assert actual_namespace["Created"] == namespace["created_at"], "namespace creation mismatch"
        assert (
            created - dt.timedelta(minutes=15) <= _utc_timestamp(namespace["created_at"]) <= created
        )
        if provider == "github":
            assert re.fullmatch(r"github_network_[0-9a-f]{32}", namespace["name"]), (
                "namespace was not created by the GitHub job"
            )
        actual_services = {service["Id"]: service for service in actual["services"]}
        assert set(actual_services) == set(service_ids), "unknown platform service"
        if provider == "temp":
            assert actual_namespace["Internal"] is True, "TEMP network must be private"
            assert actual_namespace["Labels"].get("hpo.verify.owner") == receipt["lease_id"], (
                "unowned TEMP namespace"
            )
        for role, service in services.items():
            current = actual_services[service["id"]]
            assert current["Config"]["Image"] == service["image"], "platform image mismatch"
            assert current["Created"] == service["created_at"], "service creation mismatch"
            assert current["State"]["Running"] is True, "service is not running"
            assert current["HostConfig"]["NetworkMode"] != "host", "host network is forbidden"
            if provider == "github":
                internal_port, published_port = (5432, 5433) if role == "postgres" else (6379, 6380)
                assert str(context[role]["ports"][str(internal_port)]) == str(published_port)
                assert current["NetworkSettings"]["Ports"][f"{internal_port}/tcp"] == [
                    {"HostIp": "127.0.0.1", "HostPort": str(published_port)}
                ], "non-loopback service publication"
                assert (
                    current["NetworkSettings"]["Networks"][namespace["name"]]["NetworkID"]
                    == (namespace["id"])
                ), "service namespace mismatch"
            else:
                labels = current["Config"]["Labels"]
                assert labels.get("hpo.verify.owner") == receipt["lease_id"], "unowned service"
                assert labels.get("hpo.verify.role") == role, "service role mismatch"
                assert not any((current["NetworkSettings"]["Ports"] or {}).values()), (
                    "TEMP service must not publish ports"
                )
                if role == "postgres":
                    assert (
                        current["NetworkSettings"]["Networks"][namespace["name"]]["NetworkID"]
                        == namespace["id"]
                    ), "PostgreSQL namespace mismatch"
                    assert current["Config"]["Cmd"] == ["postgres", "-c", "port=5433"]
                else:
                    assert current["HostConfig"]["NetworkMode"] == (
                        "container:" + services["postgres"]["id"]
                    ), "Redis is not in the owned PostgreSQL namespace"
                    assert current["Config"]["Cmd"] == ["redis-server", "--port", "6380"]
            if role == "postgres":
                declared = dict(
                    value.split("=", 1) for value in current["Config"]["Env"] if "=" in value
                )
                assert (
                    declared.get("POSTGRES_DB"),
                    declared.get("POSTGRES_USER"),
                    declared.get("POSTGRES_PASSWORD"),
                ) == ("heatpump_test", "heatpump", "heatpump_test"), "non-test SQL service"
        return receipt
    except (KeyError, TypeError, ValueError, OSError, AssertionError) as error:
        raise AssertionError(f"owned test resource guard refused: {error}") from error


async def guard_owned_database(
    connection, *, require_empty=False, environment=None, platform_reader=None
):
    receipt = owned_test_environment(environment, platform_reader)
    identity = (
        await connection.execute(
            text("SELECT current_database(), current_user, inet_server_port()")
        )
    ).one()
    expected_port = 5432 if receipt["provider"] == "github" else 5433
    assert identity == ("heatpump_test", "heatpump", expected_port), "SQL identity mismatch"
    if require_empty:
        objects = (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
                    "AND c.relkind IN ('r','p','S','v','m','f') "
                    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass "
                    "AND d.objid=c.oid AND d.deptype='e') "
                    "UNION ALL SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' "
                    "AND n.nspname<>'information_schema' "
                    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass "
                    "AND d.objid=p.oid AND d.deptype='e') "
                    "UNION ALL SELECT n.nspname FROM pg_namespace n "
                    "WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('public','information_schema') "
                    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_namespace'::regclass "
                    "AND d.objid=n.oid AND d.deptype='e')"
                )
            )
        ).all()
        assert not objects, "initial migration database is not empty"
    return receipt


@contextmanager
def owned_test_network():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check_endpoint(current_socket, address):
        if current_socket.family == socket.AF_UNIX:
            assert address == "/var/run/docker.sock", "unowned Unix endpoint"
        else:
            assert address[:2] in {("127.0.0.1", 5433), ("127.0.0.1", 6380)}, (
                "external network is forbidden in disposable E2E"
            )

    def connect(current_socket, address):
        check_endpoint(current_socket, address)
        return original_connect(current_socket, address)

    def connect_ex(current_socket, address):
        check_endpoint(current_socket, address)
        return original_connect_ex(current_socket, address)

    def getaddrinfo(host, port, *arguments, **keywords):
        assert (host, port) in {("127.0.0.1", 5433), ("127.0.0.1", 6380)}, (
            "external DNS/network is forbidden in disposable E2E"
        )
        return original_getaddrinfo(host, port, *arguments, **keywords)

    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        patch.object(socket, "getaddrinfo", getaddrinfo),
    ):
        yield


# Canonical tested-source proof. One producer; preparation writes it once and every
# consumer validates (never regenerates) it against the explicit tested commit.
SOURCE_MANIFEST_SCHEMA = "hpo-verification-source-manifest/1"
SOURCE_MANIFEST_NAME = "source-manifest.json"
SOURCE_MANIFEST_DIGEST_ENV = "COP_MANUAL_TEST_SOURCE_MANIFEST_SHA256"
BACKUP_BINDING_PATHS = (
    "heatpump-optimizer/tests/e2e/test_backup_roundtrip.py",
    "heatpump-optimizer/scripts/backup_db.sh",
    "heatpump-optimizer/scripts/backup_status.sql",
    "heatpump-optimizer/scripts/stage_backup.sh",
    "heatpump-optimizer/scripts/restore_drill.py",
    "heatpump-optimizer/scripts/restore_db.sh",
    "heatpump-optimizer/scripts/restore_archive.sh",
    "heatpump-optimizer/scripts/verify_backup_isolated.sh",
    "heatpump-optimizer/Dockerfile.backup",
    "heatpump-optimizer/Dockerfile.restore-coordinator",
    "heatpump-optimizer/docker-compose.yml",
    "heatpump-optimizer/docker-compose.backup.yml",
    "heatpump-optimizer/docker-compose.test.yml",
)
VERIFICATION_BINDING_PATHS = (
    ".github/workflows/optimizer-ci.yml",
    "heatpump-optimizer/tests/e2e/conftest.py",
    "heatpump-optimizer/tests/e2e/test_manual_cop_postgresql.py",
    "heatpump-optimizer/tests/test_verify_backup.py",
    "heatpump-optimizer/README.md",
    "heatpump-optimizer/migrations/versions/032_manual_cop_observations.py",
    "heatpump-optimizer/tests/conftest.py",
    "heatpump-optimizer/migrations/versions/036_manual_cop_local_periods.py",
    "heatpump-optimizer/migrations/versions/037_tibber_price_scopes.py",
    "heatpump-optimizer/tests/test_migration_head_contract.py",
    "heatpump-optimizer/packages/core/models/__init__.py",
    "heatpump-optimizer/packages/core/cop_monitoring.py",
    "heatpump-optimizer/packages/api/routers/feeds.py",
    "heatpump-optimizer/packages/api/schemas.py",
    "heatpump-optimizer/packages/api/routers/admin.py",
    "heatpump-optimizer/packages/core/version.py",
    "heatpump-optimizer/web/components/ManualCOPMonitoring.tsx",
    "heatpump-optimizer/web/components/ResetDataCard.tsx",
    "heatpump-optimizer/tests/test_ml.py",
    "heatpump-optimizer/tests/test_admin.py",
    "heatpump-optimizer/tests/test_manual_cop_api_contracts.py",
    "heatpump-optimizer/tests/test_app_version.py",
    "heatpump-optimizer/tests/e2e/test_ml_lifecycle_postgresql.py",
    "heatpump-optimizer/tests/e2e/test_phase2_safety_reverts.py",
    "heatpump-optimizer/web/e2e/manual-cop-monitoring.spec.ts",
    "heatpump-optimizer/web/e2e/reset-data.spec.ts",
    "heatpump-optimizer/docs/configuration-reference.md",
    "heatpump-optimizer/docs/manual-cop-schema-reset-runbook.md",
    "heatpump-optimizer/tests/test_optimizer_api_contracts.py",
    "heatpump-optimizer/web/e2e/fixtures.ts",
) + BACKUP_BINDING_PATHS
VERIFICATION_REPAIR_PATHS = (
    "heatpump-optimizer/tests/e2e/test_ml_lifecycle_verification.py",
    "heatpump-optimizer/tests/e2e/test_api_indoor_forecast.py",
    "heatpump-optimizer/tests/e2e/test_api_panasonic.py",
    "heatpump-optimizer/tests/e2e/test_executor_verification_reads.py",
    "heatpump-optimizer/tests/e2e/test_price_providers.py",
)
SOURCE_MANIFEST_PREFIXES = (
    "aioaquarea/",
    "heatpump-optimizer/packages/",
    "heatpump-optimizer/migrations/",
    "heatpump-optimizer/tests/",
)
SOURCE_MANIFEST_DECLARATIONS = (
    "pyproject.toml",
    "heatpump-optimizer/pyproject.toml",
    "heatpump-optimizer/constraints.txt",
)
SOURCE_MANIFEST_MODES = frozenset({"100644", "100755"})
assert len(set(VERIFICATION_BINDING_PATHS)) == len(VERIFICATION_BINDING_PATHS) == 43
assert len(set(VERIFICATION_BINDING_PATHS + VERIFICATION_REPAIR_PATHS)) == 48


def _source_checkout():
    return Path(__file__).resolve().parents[3]


def _source_git(checkout, *arguments, data=None):
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        input=data,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {arguments[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
    )
    return result.stdout


def _safe_manifest_path(path):
    assert isinstance(path, str) and path, "empty source manifest path"
    assert "\\" not in path and ":" not in path and not path.startswith("/"), (
        "unsafe source manifest path"
    )
    assert not any(ord(character) < 32 for character in path), "unsafe source manifest path"
    parts = path.split("/")
    assert all(part not in {"", ".", ".."} for part in parts), "unsafe source manifest path"
    return parts


def canonical_source_manifest(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def validate_source_manifest_document(document):
    """Structural validation; returns the path -> SHA-256 mapping."""
    assert isinstance(document, dict) and set(document) == {"schema", "commit", "entries"}, (
        "malformed source manifest"
    )
    assert document["schema"] == SOURCE_MANIFEST_SCHEMA, "unknown source manifest schema"
    assert isinstance(document["commit"], str) and re.fullmatch(
        r"[0-9a-f]{40}", document["commit"]
    ), "malformed source manifest commit"
    entries = document["entries"]
    assert isinstance(entries, list) and entries, "empty source manifest"
    paths = []
    for entry in entries:
        assert isinstance(entry, dict) and set(entry) == {"path", "sha256"}, (
            "malformed source manifest entry"
        )
        _safe_manifest_path(entry["path"])
        assert isinstance(entry["sha256"], str) and re.fullmatch(
            r"[0-9a-f]{64}", entry["sha256"]
        ), "malformed source manifest hash"
        paths.append(entry["path"])
    assert len(paths) == len(set(paths)), "duplicate source manifest path"
    assert paths == sorted(paths), "unsorted source manifest"
    return {entry["path"]: entry["sha256"] for entry in entries}


def _tested_tree(checkout, commit):
    tree = {}
    for record in _source_git(checkout, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        metadata, _, raw_path = record.partition(b"\t")
        mode, kind, name = metadata.decode("ascii").split(" ")
        path = raw_path.decode("utf-8")
        assert path not in tree, "duplicate tracked source path"
        tree[path] = (mode, kind, name)
    return tree


def _git_blobs(checkout, names):
    output = _source_git(
        checkout, "cat-file", "--batch", data="".join(name + "\n" for name in names).encode()
    )
    blobs = {}
    offset = 0
    for expected in names:
        header_end = output.index(b"\n", offset)
        name, kind, size = output[offset:header_end].decode("ascii").split(" ")
        assert (name, kind) == (expected, "blob"), "unexpected tested Git object"
        start = header_end + 1
        finish = start + int(size)
        assert output[finish : finish + 1] == b"\n", "truncated tested Git object"
        blobs[name] = output[start:finish]
        offset = finish + 1
    assert offset == len(output), "unexpected tested Git output"
    return blobs


def _worktree_source_bytes(checkout, path):
    current = checkout
    for part in _safe_manifest_path(path):
        current = current / part
        status = os.lstat(current)
        assert not stat.S_ISLNK(status.st_mode), f"symlinked source path: {path}"
        assert not getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT, (
            f"reparse-point source path: {path}"
        )
    assert stat.S_ISREG(status.st_mode), f"non-regular source path: {path}"
    assert current.resolve(strict=True).is_relative_to(checkout), f"source escapes checkout: {path}"
    return current.read_bytes()


def build_verification_source_manifest(
    checkout=None,
    commit=None,
    *,
    literal_paths=VERIFICATION_BINDING_PATHS + VERIFICATION_REPAIR_PATHS,
    prefixes=SOURCE_MANIFEST_PREFIXES,
    declarations=SOURCE_MANIFEST_DECLARATIONS,
):
    """The single canonical source manifest of the explicit tested commit."""
    checkout = Path(checkout or _source_checkout()).resolve(strict=True)
    assert isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), (
        "malformed tested commit"
    )
    head = _source_git(checkout, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    assert head == commit, "checkout is not the explicit tested commit"
    tree = _tested_tree(checkout, commit)
    required = set(literal_paths) | set(declarations)
    assert len(required) == len(literal_paths) + len(declarations), "duplicate required source"
    missing = sorted(required - set(tree))
    assert not missing, f"required source is not tracked: {missing[0]}"
    selected = sorted(
        required
        | {path for path in tree if path.endswith(".py") and path.startswith(tuple(prefixes))}
    )
    for path in selected:
        _safe_manifest_path(path)
        mode, kind, _ = tree[path]
        assert kind == "blob" and mode in SOURCE_MANIFEST_MODES, (
            f"non-regular tracked source: {path}"
        )
    blobs = _git_blobs(checkout, sorted({tree[path][2] for path in selected}))
    entries = []
    for path in selected:
        committed = blobs[tree[path][2]]
        assert b"\0" not in committed and b"\r" not in committed, (
            f"unsupported source representation: {path}"
        )
        try:
            committed.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AssertionError(f"unsupported source encoding: {path}") from error
        # Only worktree CRLF -> LF is normalized; no BOM, whitespace or content exclusions.
        current = _worktree_source_bytes(checkout, path).replace(b"\r\n", b"\n")
        assert current == committed, f"source drift from tested commit: {path}"
        entries.append({"path": path, "sha256": hashlib.sha256(committed).hexdigest()})
    document = {"schema": SOURCE_MANIFEST_SCHEMA, "commit": commit, "entries": entries}
    validate_source_manifest_document(document)
    return document


def write_verification_source_manifest(evidence, commit, checkout=None, selection=None):
    """Preparation writes the canonical manifest exactly once; returns its digest."""
    raw = canonical_source_manifest(
        build_verification_source_manifest(checkout, commit, **(selection or {}))
    )
    with (Path(evidence) / SOURCE_MANIFEST_NAME).open("xb") as manifest:
        manifest.write(raw)
    return hashlib.sha256(raw).hexdigest()


def current_verification_source_proof(environment=None, checkout=None, selection=None):
    """Validate the prepared manifest against the tested commit and current source bytes."""
    environment = os.environ if environment is None else environment
    path = Path(environment["COP_MANUAL_TEST_EVIDENCE"]) / SOURCE_MANIFEST_NAME
    digest = environment[SOURCE_MANIFEST_DIGEST_ENV]
    assert re.fullmatch(r"[0-9a-f]{64}", digest), "malformed source manifest digest"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest, "source manifest digest mismatch"
    document = json.loads(raw.decode("utf-8"))
    binding = validate_source_manifest_document(document)
    assert canonical_source_manifest(document) == raw, "non-canonical source manifest"
    commit = document["commit"]
    assert environment["COP_MANUAL_TEST_REVISION"] in {commit, f"{commit}+{digest}"}, (
        "source revision mismatch"
    )
    expected = validate_source_manifest_document(
        build_verification_source_manifest(checkout, commit, **(selection or {}))
    )
    missing = sorted(set(expected) - set(binding))
    assert not missing, f"source manifest omits required path: {missing[0]}"
    extra = sorted(set(binding) - set(expected))
    assert not extra, f"source manifest has unexpected path: {extra[0]}"
    changed = sorted(path for path in expected if expected[path] != binding[path])
    assert not changed, f"source manifest hash mismatch: {changed[0]}"
    return {"commit": commit, "digest": digest, "path": path, "binding": binding}


def verification_source_binding():
    """Compatibility view: the validated canonical manifest's path -> SHA-256 mapping."""
    return current_verification_source_proof()["binding"]


def prepare_owned_test_profile():
    provider = "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "temp"
    context = json.loads(os.environ["COP_MANUAL_TEST_SERVICE_CONTEXT"])
    assert set(context) == {"postgres", "redis"}, "unknown job service"
    assert context["postgres"]["network"] == context["redis"]["network"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", service["id"]) for service in context.values())
    if provider == "github":
        token = f"cop-manual-mon-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        temporary_parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        commit = os.environ["GITHUB_SHA"]
    else:
        token = "cop-manual-mon-" + os.environ["COP_MANUAL_TEST_LEASE_ID"]
        temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
        commit = (
            _source_git(_source_checkout(), "rev-parse", "--verify", "HEAD^{commit}")
            .decode("ascii")
            .strip()
        )
        requested = os.environ.get("COP_MANUAL_TEST_REVISION")
        assert requested is None or requested.split("+", 1)[0] == commit, (
            "TEMP revision is not the tested checkout"
        )
    # Built before any owned root exists, so source refusal leaves nothing behind.
    manifest = canonical_source_manifest(build_verification_source_manifest(None, commit))
    digest = hashlib.sha256(manifest).hexdigest()
    revision = commit if provider == "github" else f"{commit}+{digest}"
    root = temporary_parent / token
    assert os.environ["COP_MANUAL_TEST_OWNED"] == token
    assert Path(os.environ["MODEL_DIR"]) == root / "models", "model root must be set before imports"
    assert make_url(os.environ["DATABASE_URL"]) == make_url(TEST_DATABASE_URL)
    assert make_url(os.environ["REDIS_URL"]) == make_url(TEST_REDIS_URL)
    root.mkdir(mode=0o700)
    (root / "models").mkdir(mode=0o700)
    (root / "evidence").mkdir(mode=0o700)
    with (root / "evidence" / SOURCE_MANIFEST_NAME).open("xb") as manifest_file:
        manifest_file.write(manifest)
    now = dt.datetime.now(dt.timezone.utc)
    actual = _inspect_owned_resources(
        [context[role]["id"] for role in ("postgres", "redis")],
        context["postgres"]["network"],
        root / "evidence",
    )
    current = {service["Id"]: service for service in actual["services"]}
    receipt = {
        "version": 1,
        "provider": provider,
        "token": token,
        "revision": revision,
        "commit": commit,
        "source_manifest_sha256": digest,
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=30)).isoformat(),
        "temp_root": str(root),
        "model_root": str(root / "models"),
        "roots": {"temp_inode": root.stat().st_ino, "model_inode": (root / "models").stat().st_ino},
        "namespace": {
            "id": actual["namespace"]["Id"],
            "name": actual["namespace"]["Name"],
            "created_at": actual["namespace"]["Created"],
        },
        "endpoints": copy.deepcopy(TEST_ENDPOINTS),
        "services": {
            role: {
                "id": context[role]["id"],
                "image": current[context[role]["id"]]["Config"]["Image"],
                "created_at": current[context[role]["id"]]["Created"],
            }
            for role in ("postgres", "redis")
        },
    }
    if provider == "github":
        receipt.update(
            run_id=os.environ["GITHUB_RUN_ID"],
            run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
            job=os.environ["GITHUB_JOB"],
        )
    else:
        receipt["lease_id"] = os.environ["COP_MANUAL_TEST_LEASE_ID"]
    (root / "ownership.json").write_text(json.dumps(receipt), encoding="utf-8")
    updates = {
        "COP_MANUAL_TEST_RECEIPT": json.dumps(receipt, separators=(",", ":")),
        "COP_MANUAL_TEST_REVISION": revision,
        SOURCE_MANIFEST_DIGEST_ENV: digest,
        "COP_MANUAL_TEST_SERVICE_CONTEXT": json.dumps(context, separators=(",", ":")),
        "COP_MANUAL_TEST_EVIDENCE": str(root / "evidence"),
    }
    os.environ.update(updates)
    owned_test_environment()
    if provider == "github":
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as environment_file:
            for key, value in updates.items():
                assert "\n" not in value and "\r" not in value
                environment_file.write(f"{key}={value}\n")
    return root


async def verify_owned_sql_identity():
    owned_test_environment()
    engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=NullPool, connect_args={"command_timeout": 20, "timeout": 20}
    )
    try:
        with owned_test_network():
            async with engine.connect() as connection:
                await guard_owned_database(connection)
    finally:
        await engine.dispose()


# Owned schema fixture whose teardown writes one cleanup receipt per using node.
LIFECYCLE_RESOURCE_FIXTURE = "lifecycle_database"


def lifecycle_receipt_obligations(items):
    """Nodes whose pytest fixture closure (direct or transitive) owns a lifecycle schema."""
    return sorted(
        item.nodeid
        for item in items
        if LIFECYCLE_RESOURCE_FIXTURE in tuple(getattr(item, "fixturenames", ()))
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    if os.environ.get("COP_MANUAL_TEST_PHASE"):
        config._cop_verification_nodes = [item.nodeid for item in items]
        config._cop_receipt_required = lifecycle_receipt_obligations(items)


def pytest_collection_finish(session):
    if os.environ.get("COP_MANUAL_TEST_PHASE"):
        session.config._cop_verification_selected = [item.nodeid for item in session.items]


def pytest_runtest_logreport(report):
    if os.environ.get("COP_MANUAL_TEST_PHASE"):
        path = Path(os.environ["COP_MANUAL_TEST_REPORTS"])
        with path.open("a", encoding="utf-8") as reports:
            reports.write(
                json.dumps(
                    {
                        "nodeid": report.nodeid,
                        "when": report.when,
                        "duration": report.duration,
                        "outcome": report.outcome,
                        "wasxfail": bool(getattr(report, "wasxfail", None)),
                    }
                )
                + "\n"
            )


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("COP_MANUAL_TEST_PHASE"):
        path = Path(os.environ["COP_MANUAL_TEST_NODES"])
        path.write_text(
            json.dumps(
                {
                    "phase": os.environ["COP_MANUAL_TEST_PHASE"],
                    "collected": getattr(session.config, "_cop_verification_nodes", []),
                    "selected": getattr(session.config, "_cop_verification_selected", []),
                    "receipt_fixture": LIFECYCLE_RESOURCE_FIXTURE,
                    "receipt_required": getattr(session.config, "_cop_receipt_required", []),
                    "source_manifest_sha256": os.environ.get(SOURCE_MANIFEST_DIGEST_ENV),
                    "exit": int(exitstatus),
                }
            ),
            encoding="utf-8",
        )


def run_verification_phase(name, arguments, *, limit=600, owned_sql=True):
    assert re.fullmatch(r"[a-z0-9-]+", name)
    assert 0 < limit <= 600
    if owned_sql:
        asyncio.run(verify_owned_sql_identity())
    # Source drift refuses before any phase directory or child process exists.
    proof = current_verification_source_proof()
    evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"])
    phase = evidence / name
    phase.mkdir()
    environment = dict(os.environ)
    if arguments[1:3] == ["-m", "pytest"]:
        assert environment.get("PYTEST_PLUGINS") in {
            None,
            "",
            "tests.e2e.test_manual_cop_postgresql",
        }, "unexpected test plugins"
        environment["PYTEST_PLUGINS"] = "tests.e2e.test_manual_cop_postgresql"
        environment["COP_MANUAL_TEST_PHASE"] = name
        environment["COP_MANUAL_TEST_REPORTS"] = str(phase / "reports.jsonl")
        environment["COP_MANUAL_TEST_NODES"] = str(phase / "nodes.json")
        arguments = arguments + [
            "--basetemp=" + str(evidence.parent / ("pytest-" + name)),
            "-o",
            "cache_dir=" + str(evidence.parent / ("pytest-cache-" + name)),
        ]
    started = dt.datetime.now(dt.timezone.utc)
    deadline = started + dt.timedelta(seconds=limit)
    parent_deadline = started + dt.timedelta(seconds=660)
    receipt = {
        "parent_pid": os.getpid(),
        "external_parent_pid": os.getppid(),
        "parent_deadline": parent_deadline.isoformat(),
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "command": arguments,
        "cwd": str(Path.cwd()),
        "source": proof["binding"],
        "commit": proof["commit"],
        "source_manifest_sha256": proof["digest"],
        "revision": os.environ.get("COP_MANUAL_TEST_REVISION"),
        "timed_out": False,
    }
    process = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def parent_terminated(signum, frame):
        receipt["timed_out"] = True
        receipt["parent_signal"] = signum
        raise TimeoutError("verification parent terminated")

    signal.signal(signal.SIGTERM, parent_terminated)
    try:
        with (
            (phase / "stdout.log").open("w", encoding="utf-8") as stdout,
            (phase / "stderr.log").open("w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(
                arguments, stdout=stdout, stderr=stderr, env=environment, start_new_session=True
            )
            receipt.update(pid=process.pid, pgid=process.pid)
            (phase / "process.json").write_text(json.dumps(receipt), encoding="utf-8")
            try:
                process.wait(
                    timeout=max(0, (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds())
                )
            except subprocess.TimeoutExpired:
                receipt["timed_out"] = True
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    assert os.getpgid(process.pid) == process.pid, "unknown child process group"
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
                receipt.update(exit=process.returncode, alive=process.poll() is None)
            receipt["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            (phase / "process.json").write_text(json.dumps(receipt), encoding="utf-8")
        finally:
            signal.signal(signal.SIGTERM, previous_term)
    print((phase / "stdout.log").read_text(encoding="utf-8"), end="")
    print((phase / "stderr.log").read_text(encoding="utf-8"), end="", file=sys.stderr)
    assert not receipt["timed_out"] and receipt["exit"] == 0, f"verification phase {name} failed"


def verification_preflight(root, *, owned_sql=True):
    import aioaquarea
    import fastapi
    import packages.core.models

    checkout = Path(__file__).resolve().parents[3]
    assert Path(aioaquarea.__file__).resolve().is_relative_to(checkout / "aioaquarea")
    assert (
        Path(packages.core.models.__file__)
        .resolve()
        .is_relative_to(checkout / "heatpump-optimizer/packages/core/models")
    )
    assert sys.version_info[:2] == (3, 12)
    assert fastapi.__version__ == "0.141.1"
    for timezone in ("UTC", "US/Eastern", "Asia/Calcutta", "Pacific/Apia"):
        assert ZoneInfo(timezone).key == timezone
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = {}
    for line in Path("constraints.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate():
                pins[canonicalize_name(requirement.name)] = requirement
    requirements = list(project["dependencies"])
    for extra in ("api", "optimizer", "ml", "dev"):
        requirements.extend(project["optional-dependencies"][extra])
    versions = {}
    for value in requirements:
        requirement = Requirement(value)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        installed = importlib.metadata.version(requirement.name)
        assert installed in requirement.specifier, (
            f"unsatisfied active dependency {requirement.name}"
        )
        pin = pins.get(canonicalize_name(requirement.name))
        if requirement.url is not None:
            assert re.fullmatch(
                r"git\+https://github\.com/TrooP81/aioaquarea\.git@[0-9a-f]{40}",
                requirement.url,
            ), "unrecognized unpinned VCS dependency"
        else:
            assert pin is not None and installed in pin.specifier, (
                f"constraint mismatch {requirement.name}"
            )
        versions[requirement.name] = installed
    probe = root / "tmp-executable-probe"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    probe.chmod(0o700)
    run_verification_phase("tmp-probe", [str(probe)], limit=2, owned_sql=owned_sql)
    probe.unlink()
    proof = current_verification_source_proof()
    (root / "evidence" / "preflight.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "versions": versions,
                "source": proof["binding"],
                "commit": proof["commit"],
                "source_manifest_sha256": proof["digest"],
                "tzdata": importlib.metadata.version("tzdata"),
                "executable_tmp": True,
            }
        ),
        encoding="utf-8",
    )


MANUAL_MODULE = "tests/e2e/test_manual_cop_postgresql.py"
LIFECYCLE_MODULE = "tests/e2e/test_ml_lifecycle_postgresql.py"
LIFECYCLE_VERIFICATION_MODULE = "tests/e2e/test_ml_lifecycle_verification.py"
BACKUP_MODULE = "tests/e2e/test_backup_roundtrip.py"
# Ordinary E2E keeps conftest and ignores exactly these coordinator-owned modules.
COORDINATOR_MODULES = (
    MANUAL_MODULE,
    LIFECYCLE_MODULE,
    LIFECYCLE_VERIFICATION_MODULE,
    BACKUP_MODULE,
)
REAL_MIGRATION_CLASS = LIFECYCLE_VERIFICATION_MODULE + "::TestOBS5RealMigration"
SEALED_TEST_PHASES = (
    "migration",
    "manual",
    "exhaustion",
    "obs5-real-migration",
    "obs5-pg-original",
    "obs5-pg-supplement",
    "ordinary",
    "backup-roundtrip",
)
OBS5_TEST_PHASES = ("obs5-real-migration", "obs5-pg-original", "obs5-pg-supplement")
REQUIRED_SCHEMA_PHASES = (
    "heads",
    "upgrade-head",
    "check-migration",
    "check-manual",
    "check-exhaustion",
    "obs5-primary-upgrade",
    "obs5-primary-check",
    "check-obs5",
    "check-ordinary",
    "check-backup",
)


def _verification_phase_owns(name, nodeid):
    module = nodeid.split("::", 1)[0]
    real_migration = nodeid.startswith(REAL_MIGRATION_CLASS + "::")
    owners = {
        "migration": module == MANUAL_MODULE,
        "manual": module == MANUAL_MODULE,
        "exhaustion": module == MANUAL_MODULE,
        "obs5-real-migration": real_migration,
        "obs5-pg-original": module == LIFECYCLE_MODULE,
        "obs5-pg-supplement": module == LIFECYCLE_VERIFICATION_MODULE and not real_migration,
        "ordinary": module not in COORDINATOR_MODULES,
        "backup-roundtrip": module == BACKUP_MODULE,
    }
    assert name in owners, f"unknown verification phase {name}"
    return owners[name]


def _successful_process(evidence, name):
    path = evidence / name / "process.json"
    assert path.is_file(), f"verification phase {name} did not succeed"
    process = json.loads(path.read_text(encoding="utf-8"))
    assert process["exit"] == 0 and not process["alive"] and not process["timed_out"], (
        f"verification phase {name} did not succeed"
    )
    return process


def _reconcile_verification_phase(evidence, name):
    """Exactly one passed setup/call/teardown per selected, phase-owned node."""
    process = _successful_process(evidence, name)
    nodes = json.loads((evidence / name / "nodes.json").read_text(encoding="utf-8"))
    assert nodes["phase"] == name and nodes["exit"] == process["exit"] == 0, (
        f"verification phase {name} did not succeed"
    )
    selected = nodes["selected"]
    assert selected and len(selected) == len(set(selected)), f"phase {name} selection is invalid"
    foreign = [node for node in selected if not _verification_phase_owns(name, node)]
    assert not foreign, f"phase {name} selected a node it does not own"
    reports = [
        json.loads(line)
        for line in (evidence / name / "reports.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(report["outcome"] == "passed" and not report["wasxfail"] for report in reports), (
        f"phase {name} skip/xfail/failure is forbidden"
    )
    assert {report["nodeid"] for report in reports} == set(selected), (
        f"phase {name} reports differ from its selection"
    )
    counts = Counter((report["nodeid"], report["when"]) for report in reports)
    assert len(reports) == 3 * len(selected) and all(
        counts[(node, when)] == 1 for node in selected for when in ("setup", "call", "teardown")
    ), f"phase {name} lacks exactly one setup/call/teardown per node"
    return set(selected)


def reconcile_verification_partitions(evidence, inventory, names):
    """Disjoint, complete execution ownership of an actual collected inventory."""
    evidence = Path(evidence)
    _successful_process(evidence, inventory)
    collected = json.loads((evidence / inventory / "nodes.json").read_text(encoding="utf-8"))
    expected = collected["collected"]
    assert collected["exit"] == 0 and expected and len(expected) == len(set(expected)), (
        "verification inventory is invalid"
    )
    partitions = {}
    for name in names:
        selected = _reconcile_verification_phase(evidence, name)
        for other, previous in partitions.items():
            assert not selected & previous, f"verification phases {other} and {name} overlap"
        partitions[name] = selected
    assert set().union(*partitions.values()) == set(expected), (
        "verification inventory is not completely partitioned"
    )
    return set(expected), partitions


async def _primary_marker():
    owned_test_environment()
    engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=NullPool, connect_args={"command_timeout": 20, "timeout": 20}
    )
    try:
        with owned_test_network():
            async with engine.connect() as connection:
                await guard_owned_database(connection)
                if (
                    await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
                ).scalar_one() is None:
                    return None
                markers = (
                    (
                        await connection.execute(
                            text("SELECT version_num FROM public.alembic_version")
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(markers) <= 1, "primary has multiple Alembic markers"
                return markers[0] if markers else None
    finally:
        await engine.dispose()


def run_primary_head_upgrade(label, *, evidence=None, read_marker=None, run_phase=None):
    """Explicit guarded primary upgrade/check; never inferred from a child migration."""
    assert re.fullmatch(r"[a-z0-9-]+", label)
    evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"] if evidence is None else evidence)
    read_marker = read_marker or (lambda: asyncio.run(_primary_marker()))
    run_phase = run_phase or run_verification_phase
    head, _ = migration_targets()
    assert head == CURRENT_HEAD_REVISION, "reviewed primary head changed"
    before = read_marker()
    assert before in {None, head}, "unexpected primary migration state"
    run_phase(label + "-upgrade", [sys.executable, "-m", "alembic", "upgrade", "head"])
    run_phase(label + "-check", [sys.executable, "-m", "alembic", "check"])
    after = read_marker()
    assert after == head, "primary is not at the reviewed head"
    (evidence / (label + "-head.json")).write_text(
        json.dumps({"before": before, "after": after, "head": head}), encoding="utf-8"
    )
    return after


OBS5_SCHEMA_PHASES = ("obs5-primary-upgrade", "obs5-primary-check", "check-obs5")
LIFECYCLE_RECEIPT_PHASES = ("obs5-pg-original", "obs5-pg-supplement")
_SEAL_SNAPSHOT = ContextVar("lifecycle_seal_snapshot", default=None)
_SEAL_PUBLICATIONS = ContextVar("lifecycle_seal_publications", default=None)


def _revoke_lifecycle_seals():
    """Revoke only this invocation's published inode, never an earlier/replaced seal."""
    for path, identity in _SEAL_PUBLICATIONS.get() or ():
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        assert (current.st_dev, current.st_ino) == identity, (
            "lifecycle receipt seal replacement prevents revocation"
        )
        path.unlink()
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


@contextmanager
def lifecycle_evidence_lock(evidence):
    """Nonblocking cross-process lock; crash releases it, contention fails closed.

    The owned, private evidence directory is a cooperative test-process boundary,
    not protection against an attacker running as the same OS user. Never unlink
    the lock inode: all receipt writers and sealers must use this protocol.
    """
    evidence = Path(evidence)
    info = evidence.lstat()
    assert stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ), "lifecycle receipt evidence root is unsafe"
    if os.name != "nt":
        assert info.st_mode & 0o077 == 0, "lifecycle receipt evidence root is not private"
    path = evidence / ".lifecycle-evidence.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise AssertionError("lifecycle receipt lock unavailable") from error
    acquired = False
    try:
        opened, named = os.fstat(descriptor), path.lstat()
        assert (
            stat.S_ISREG(opened.st_mode)
            and opened.st_nlink == 1
            and opened.st_size == 0
            and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
            and not (
                getattr(named, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        ), "lifecycle receipt lock is unsafe"
        if os.name != "nt":
            assert opened.st_mode & 0o077 == 0 and opened.st_uid == info.st_uid, (
                "lifecycle receipt lock is not private"
            )
        if os.name == "nt":
            import msvcrt

            # Byte-range locking works beyond EOF; the stable lock file stays empty.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        current_root, current_lock = evidence.lstat(), path.lstat()
        assert (current_root.st_dev, current_root.st_ino) == (info.st_dev, info.st_ino) and (
            current_lock.st_dev,
            current_lock.st_ino,
        ) == (named.st_dev, named.st_ino), "lifecycle receipt lock identity changed"
        yield
    except OSError as error:
        raise AssertionError("lifecycle receipt lock unavailable") from error
    finally:
        original = sys.exception()
        errors = []
        try:
            if acquired:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            errors.append(error)
            try:
                _revoke_lifecycle_seals()
            except BaseException as revoke_error:
                errors.append(revoke_error)
        finally:
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        if errors:
            if original is not None:
                errors.insert(0, original)
            raise AssertionError(
                "lifecycle receipt lock settlement failed"
            ) from BaseExceptionGroup("lifecycle lock body/cleanup failures", errors)


def _lifecycle_evidence_snapshot(evidence):
    records = {}
    for name, path in (
        ("root", Path(evidence)),
        ("lock", Path(evidence) / ".lifecycle-evidence.lock"),
    ):
        info = path.lstat()
        records[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
    paths = (
        set(Path(evidence).glob("obs5-receipt*"))
        | set(Path(evidence).glob("obs5-*.json"))
        | set(Path(evidence).glob("backup-receipt*"))
    )
    for path in paths:
        if path.name == "obs5-scope-seal.json":
            continue
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ), "lifecycle receipt snapshot contains unsafe file"
        records[path.name] = (
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return records


def _publish_lifecycle_json(path, record):
    """Exclusive atomic publication under the lifecycle lock, never overwrite."""
    path = Path(path)
    snapshot = _SEAL_SNAPSHOT.get()
    if path.name in {"seal.json", "obs5-scope-seal.json"} and record.get("passed") is not False:
        assert snapshot is not None and snapshot == _lifecycle_evidence_snapshot(path.parent), (
            "lifecycle receipt store changed before seal publication"
        )
    pending = path.parent / (".lifecycle-pending-" + uuid.uuid4().hex)
    try:
        with pending.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(record))
            stream.flush()
            os.fsync(stream.fileno())
        publications = _SEAL_PUBLICATIONS.get()
        if publications is not None and path.name in {"seal.json", "obs5-scope-seal.json"}:
            identity = pending.stat()
            publications.append((path, (identity.st_dev, identity.st_ino)))
        os.link(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def write_lifecycle_receipt(evidence, receipt):
    evidence = Path(evidence)
    with lifecycle_evidence_lock(evidence):
        assert not any(
            (evidence / name).exists() for name in ("seal.json", "obs5-scope-seal.json")
        ), "lifecycle receipt store is sealed"
        node = receipt["test"]
        assert isinstance(node, str) and node, "lifecycle receipt node is missing"
        name = "obs5-receipt-" + hashlib.sha256(node.encode()).hexdigest()[:32] + ".json"
        _publish_lifecycle_json(evidence / name, receipt)


def write_backup_receipt(evidence, receipt):
    evidence = Path(evidence)
    with lifecycle_evidence_lock(evidence):
        assert not (evidence / "seal.json").exists(), "backup receipt store is sealed"
        node = receipt["test"]
        assert node.split("::", 1)[0] == BACKUP_MODULE, "foreign backup receipt owner"
        name = "backup-receipt-" + hashlib.sha256(node.encode()).hexdigest()[:32] + ".json"
        _publish_lifecycle_json(evidence / name, receipt)


def validate_backup_receipts(evidence, nodes, proof, ownership):
    expected = {
        "backup-receipt-" + hashlib.sha256(node.encode()).hexdigest()[:32] + ".json": node
        for node in nodes
    }
    paths = list(Path(evidence).glob("backup-receipt*"))
    assert nodes and {path.name for path in paths} == set(expected), (
        "backup receipt coverage mismatch"
    )
    for path in paths:
        assert path.is_file() and not path.is_symlink(), "unsafe backup receipt"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert (
            record["test"] == expected[path.name]
            and record["ownership_token"] == ownership["token"]
            and record["commit"] == proof["commit"]
            and record["source_manifest_sha256"] == proof["digest"]
            and record["source_preserved"] is True
            and record["cleanup_verified"] is True
            and record["coordinator_verified"] is True
            and record["created_resources"]
            and len(record["created_resources"]) == len(set(record["created_resources"]))
            and record["worker_script_sha256"]
            == proof["binding"]["heatpump-optimizer/scripts/backup_db.sh"]
        ), "backup receipt source/creation/cleanup mismatch"


def atomic_lifecycle_seal(function):
    """Hold the writer lock from validation through atomic seal publication.

    Snapshot checks additionally reject observed non-protocol changes during
    validation/publication. No retry or stale-lock deletion is permitted.
    """

    @wraps(function)
    def locked(evidence=None, *args, **kwargs):
        evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"] if evidence is None else evidence)
        publications_token = _SEAL_PUBLICATIONS.set([])
        try:
            with lifecycle_evidence_lock(evidence):
                assert not any(
                    (evidence / name).exists() for name in ("seal.json", "obs5-scope-seal.json")
                ), "lifecycle receipt store is already sealed"
                before = _lifecycle_evidence_snapshot(evidence)
                token = _SEAL_SNAPSHOT.set(before)
                try:
                    result = function(evidence, *args, **kwargs)
                    assert before == _lifecycle_evidence_snapshot(evidence), (
                        "lifecycle receipt store changed during sealing"
                    )
                except BaseException:
                    _revoke_lifecycle_seals()
                    raise
                finally:
                    _SEAL_SNAPSHOT.reset(token)
            return result
        except BaseException as error:
            try:
                _revoke_lifecycle_seals()
            except BaseException as revoke_error:
                raise AssertionError(
                    "lifecycle receipt seal revocation failed"
                ) from BaseExceptionGroup("lifecycle seal/rollback failures", [error, revoke_error])
            raise
        finally:
            _SEAL_PUBLICATIONS.reset(publications_token)

    return locked


def lifecycle_receipt_requirements(evidence, inventory, partitions, digest):
    """Exact lifecycle-receipt obligations from the source-bound collection inventory.

    The inventory's pytest fixture closure decides which nodes own a lifecycle schema;
    each executing lifecycle phase must have collected the identical obligation subset.
    """
    collected = json.loads((evidence / inventory / "nodes.json").read_text(encoding="utf-8"))
    required = collected.get("receipt_required")
    assert (
        collected.get("receipt_fixture") == LIFECYCLE_RESOURCE_FIXTURE
        and collected.get("source_manifest_sha256") == digest
        and isinstance(required, list)
        and required
        and len(required) == len(set(required))
        and set(required) <= set(collected["collected"])
    ), "lifecycle receipt inventory is missing or unbound"
    required = set(required)
    executed = set().union(*(partitions.get(name, set()) for name in LIFECYCLE_RECEIPT_PHASES))
    assert required <= executed, "lifecycle receipt obligation ran outside a lifecycle phase"
    for name in LIFECYCLE_RECEIPT_PHASES:
        nodes = json.loads((evidence / name / "nodes.json").read_text(encoding="utf-8"))
        assert (
            nodes.get("receipt_fixture") == LIFECYCLE_RESOURCE_FIXTURE
            and nodes.get("source_manifest_sha256") == digest
            and set(nodes.get("receipt_required", ())) & partitions[name]
            == required & partitions[name]
        ), f"phase {name} lifecycle receipt inventory drifted"
    return required


def validate_bound_verification_evidence(evidence, proof, partitions, schema_phases, inventory):
    """Shared full/OBS-only seal validation: every bound receipt, before any seal is written.

    Requires successful, source-identical process receipts; preflight/ownership source;
    the explicit primary-head receipt; and exactly one schema-ownership/cleanup receipt,
    bound to the owned profile token, per executed lifecycle-resource node.
    """
    evidence = Path(evidence)
    digest, commit, binding = proof["digest"], proof["commit"], proof["binding"]
    for name in schema_phases:
        _successful_process(evidence, name)
    head = json.loads((evidence / "obs5-primary-head.json").read_text(encoding="utf-8"))
    assert (
        head["before"] in {None, CURRENT_HEAD_REVISION}
        and head["after"] == head["head"] == CURRENT_HEAD_REVISION
    ), "primary head was not proven"
    processes = [
        json.loads(path.read_text(encoding="utf-8")) for path in evidence.glob("*/process.json")
    ]
    assert all(
        process["exit"] == 0 and not process["alive"] and not process["timed_out"]
        for process in processes
    ), "verification process failed or survived"
    assert all(
        (process["commit"], process["source_manifest_sha256"], process["source"])
        == (commit, digest, binding)
        for process in processes
    ), "verification source drift"
    ownership = json.loads((evidence.parent / "ownership.json").read_text(encoding="utf-8"))
    for receipt in (
        json.loads((evidence / "preflight.json").read_text(encoding="utf-8")),
        ownership,
    ):
        assert (receipt["commit"], receipt["source_manifest_sha256"]) == (commit, digest), (
            "receipt source digest mismatch"
        )
    required = lifecycle_receipt_requirements(evidence, inventory, partitions, digest)
    expected_receipts = {
        "obs5-receipt-" + hashlib.sha256(node.encode()).hexdigest()[:32] + ".json": node
        for node in required
    }
    # These are coordinator records, not lifecycle receipts. Everything else in the
    # receipt namespace must match an inventory obligation, even without an owner field.
    coordinator_records = {
        "obs5-primary-head.json",
        "obs5-readiness-postgres.json",
        "obs5-readiness-redis.json",
        "obs5-scope-seal.json",
    }
    receipt_paths = set(evidence.glob("obs5-receipt*")) | {
        path for path in evidence.glob("obs5-*.json") if path.name not in coordinator_records
    }
    assert len(expected_receipts) == len(required) and {path.name for path in receipt_paths} == set(
        expected_receipts
    ), "lifecycle receipt coverage mismatch"
    lifecycle = []
    for path in sorted(receipt_paths):
        metadata = path.lstat()
        assert stat.S_ISREG(metadata.st_mode) and not (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ), "lifecycle receipt is not a regular file"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AssertionError("lifecycle receipt is unreadable or malformed") from error
        assert isinstance(receipt, dict) and receipt.get("test") == expected_receipts[path.name], (
            "lifecycle receipt coverage mismatch"
        )
        lifecycle.append(receipt)
    assert {receipt["test"] for receipt in lifecycle} == required, (
        "lifecycle receipt coverage mismatch"
    )
    assert all(
        receipt.get("ownership_token") == ownership["token"]
        and (receipt.get("commit"), receipt.get("source_manifest_sha256")) == (commit, digest)
        and receipt.get("schema_owned") is True
        and receipt.get("schema_removed") is True
        and receipt.get("public_manual_storage_unchanged") is True
        for receipt in lifecycle
    ), "lifecycle receipt source/cleanup mismatch"
    return head, processes


@atomic_lifecycle_seal
def seal_verification_evidence(evidence=None, proof=None):
    evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"] if evidence is None else evidence)
    proof = current_verification_source_proof() if proof is None else proof
    digest, commit, binding = proof["digest"], proof["commit"], proof["binding"]
    expected, partitions = reconcile_verification_partitions(
        evidence, "e2e-inventory", SEALED_TEST_PHASES
    )
    outside = sorted(
        {node.split("::", 1)[0] for node in expected}
        - {path.removeprefix("heatpump-optimizer/") for path in binding}
    )
    assert not outside, f"inventoried test source is outside the source manifest: {outside[0]}"
    manual = json.loads((evidence / "manual-inventory/nodes.json").read_text(encoding="utf-8"))
    _successful_process(evidence, "manual-inventory")
    manual_nodes = partitions["migration"] | partitions["manual"] | partitions["exhaustion"]
    assert manual_nodes == set(manual["collected"]), "manual phase inventory is incomplete"
    head, processes = validate_bound_verification_evidence(
        evidence, proof, partitions, REQUIRED_SCHEMA_PHASES, "e2e-inventory"
    )
    ownership = json.loads((evidence.parent / "ownership.json").read_text(encoding="utf-8"))
    validate_backup_receipts(evidence, partitions["backup-roundtrip"], proof, ownership)
    _publish_lifecycle_json(
        evidence / "seal.json",
        {
            "commit": commit,
            "source_manifest_sha256": digest,
            "source": binding,
            "partitions": {name: sorted(nodes) for name, nodes in partitions.items()},
            "primary_head": head,
            "phases": processes,
            "sealed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )


class TestOwnedResourceGuard:
    @pytest.fixture(autouse=True)
    def owned_temp_parents(self):
        self.temp_parents = []
        yield
        for parent in self.temp_parents:
            shutil.rmtree(parent)

    def environment(self, tmp_path, provider):
        now = dt.datetime.now(dt.timezone.utc)
        lease = uuid.uuid4().hex
        token = "cop-manual-mon-123-2" if provider == "github" else "cop-manual-mon-" + lease
        if provider == "temp":
            # The TEMP provider is only valid below the real temporary directory, never
            # below pytest's basetemp (which CI places under RUNNER_TEMP).
            system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
            parent = Path(tempfile.mkdtemp(prefix="cop-manual-guard-", dir=system_temp))
            self.temp_parents.append(parent)
        else:
            parent = tmp_path
        root = parent / token
        root.mkdir()
        (root / "models").mkdir()
        (root / "evidence").mkdir()
        commit = "4" * 40
        # Synthetic canonical manifest; guard tests prove binding, not repository content.
        manifest = canonical_source_manifest(
            {
                "schema": SOURCE_MANIFEST_SCHEMA,
                "commit": commit,
                "entries": [{"path": "pkg/module.py", "sha256": "5" * 64}],
            }
        )
        (root / "evidence" / SOURCE_MANIFEST_NAME).write_bytes(manifest)
        digest = hashlib.sha256(manifest).hexdigest()
        namespace = {
            "id": "3" * 64,
            "name": "github_network_" + "3" * 32 if provider == "github" else lease,
            "created_at": (now - dt.timedelta(seconds=3)).isoformat(),
        }
        service_created = (now - dt.timedelta(seconds=2)).isoformat()
        receipt = {
            "version": 1,
            "provider": provider,
            "token": token,
            "revision": commit if provider == "github" else f"{commit}+{digest}",
            "commit": commit,
            "source_manifest_sha256": digest,
            "created_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(minutes=30)).isoformat(),
            "run_id": "123",
            "run_attempt": "2",
            "job": "backend-e2e",
            "lease_id": lease,
            "temp_root": str(root),
            "model_root": str(root / "models"),
            "roots": {
                "temp_inode": root.stat().st_ino,
                "model_inode": (root / "models").stat().st_ino,
            },
            "namespace": namespace,
            "endpoints": copy.deepcopy(TEST_ENDPOINTS),
            "services": {
                "postgres": {
                    "id": "1" * 64,
                    "image": POSTGRES_IMAGE,
                    "created_at": service_created,
                },
                "redis": {"id": "2" * 64, "image": REDIS_IMAGE, "created_at": service_created},
            },
        }
        context = {
            role: {
                "id": service["id"],
                "network": namespace["name"],
                "ports": {str(port): port + 1},
            }
            for (role, service), port in zip(receipt["services"].items(), (5432, 6379), strict=True)
        }
        environment = {
            "DATABASE_URL": TEST_DATABASE_URL,
            "REDIS_URL": TEST_REDIS_URL,
            "APP_ENVIRONMENT": "test",
            "API_TOKEN": "test-token",
            "SECRET_KEY": TEST_SECRET_KEY,
            "PRICE_PROVIDER": "entsoe",
            "ENTSOE_API_TOKEN": "test-token",
            "AQUAREA_USERNAME": "test@test.com",
            "AQUAREA_PASSWORD": "testpass",
            "MODEL_DIR": receipt["model_root"],
            "COP_MANUAL_TEST_OWNED": token,
            "COP_MANUAL_TEST_REVISION": receipt["revision"],
            SOURCE_MANIFEST_DIGEST_ENV: digest,
            "COP_MANUAL_TEST_LEASE_ID": lease,
            "COP_MANUAL_TEST_EVIDENCE": str(root / "evidence"),
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_JOB": "backend-e2e",
            "GITHUB_SHA": commit,
            "RUNNER_TEMP": str(tmp_path),
            "COP_MANUAL_TEST_SERVICE_CONTEXT": json.dumps(context),
        }
        actual = {
            "namespace": {
                "Id": namespace["id"],
                "Name": namespace["name"],
                "Created": namespace["created_at"],
                "Driver": "bridge",
                "Scope": "local",
                "Internal": True,
                "Labels": {"hpo.verify.owner": lease},
            },
            "services": [],
        }
        for role, service in receipt["services"].items():
            internal_port, published_port = (5432, 5433) if role == "postgres" else (6379, 6380)
            actual["services"].append(
                {
                    "Id": service["id"],
                    "Created": service_created,
                    "State": {"Running": True},
                    "Config": {
                        "Image": service["image"],
                        "Env": [
                            "POSTGRES_DB=heatpump_test",
                            "POSTGRES_USER=heatpump",
                            "POSTGRES_PASSWORD=heatpump_test",
                        ],
                        "Labels": {"hpo.verify.owner": lease, "hpo.verify.role": role},
                        "Cmd": ["postgres", "-c", "port=5433"]
                        if role == "postgres"
                        else ["redis-server", "--port", "6380"],
                    },
                    "HostConfig": {
                        "NetworkMode": namespace["name"]
                        if provider == "github" or role == "postgres"
                        else "container:" + receipt["services"]["postgres"]["id"]
                    },
                    "NetworkSettings": {
                        "Ports": {
                            f"{internal_port}/tcp": [
                                {"HostIp": "127.0.0.1", "HostPort": str(published_port)}
                            ]
                        }
                        if provider == "github"
                        else {f"{internal_port}/tcp": None},
                        "Networks": {namespace["name"]: {"NetworkID": namespace["id"]}},
                    },
                }
            )
        return environment, receipt, actual

    def bind(self, environment, receipt):
        environment["COP_MANUAL_TEST_RECEIPT"] = json.dumps(receipt)
        (Path(receipt["temp_root"]) / "ownership.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )

    @pytest.mark.parametrize("provider", ["github", "temp"])
    @pytest.mark.parametrize(
        "replace",
        [
            ("services", "postgres", "id"),
            ("services", "redis", "id"),
            ("namespace", "id"),
            ("namespace", "name"),
            ("namespace", "created_at"),
            ("lease_id",),
            ("revision",),
            ("expires_at",),
        ],
    )
    async def test_migration_child_binding_covers_original_parent_identity(
        self, tmp_path, provider, replace
    ):
        _, receipt, _ = self.environment(tmp_path, provider)
        original = _migration_child_parent_identity(receipt)
        assert original == _migration_child_parent_identity(copy.deepcopy(receipt))
        changed = copy.deepcopy(receipt)
        target = changed
        for key in replace[:-1]:
            target = target[key]
        target[replace[-1]] = "f" * 64 if replace[-1] == "id" else "replaced-" + replace[-1]
        assert _migration_child_parent_identity(changed) != original

    def child_engine_fake(self, identity):
        calls = []

        class Transaction:
            async def __aenter__(self):
                calls.append("begin")

            async def __aexit__(self, kind, error, traceback):
                calls.append("rollback" if kind else "commit")
                return False

        class Connection:
            async def execute(self, statement, parameters=None):
                calls.append(str(statement))
                return SimpleNamespace(one=lambda: identity)

            async def rollback(self):
                calls.append("identity-rollback")

            def begin(self):
                return Transaction()

        @asynccontextmanager
        async def connect():
            calls.append("connect")
            try:
                yield Connection()
            finally:
                calls.append("close")

        return SimpleNamespace(connect=connect), calls

    @pytest.mark.parametrize("provider", ["github", "temp"])
    @pytest.mark.parametrize(
        "change", ["none", "parent_service", "parent_namespace", "parent_receipt", "child_oid"]
    )
    async def test_guarded_child_engine_refuses_mutation_after_identity_change(
        self, tmp_path, provider, change, monkeypatch
    ):
        _, receipt, _ = self.environment(tmp_path, provider)
        record = {
            "database": "cop_manual_migration_forward_child",
            "oid": 41,
            "binding": "{}",
            "parent": _migration_child_parent_identity(receipt),
        }
        expected = (record["database"], "heatpump", 5432, 41, "{}")
        current = copy.deepcopy(receipt)
        identity = expected
        if change == "parent_service":
            current["services"]["postgres"]["id"] = "f" * 64
        elif change == "parent_namespace":
            current["namespace"]["id"] = "e" * 64
        elif change == "parent_receipt":
            current["expires_at"] = "2099-01-01T00:00:00+00:00"
        elif change == "child_oid":
            identity = (*expected[:3], 42, expected[4])
        monkeypatch.setitem(globals(), "owned_test_environment", lambda: current)
        engine, calls = self.child_engine_fake(identity)
        guarded = GuardedChildEngine(engine, record, expected)
        mutation = "UPDATE settings SET value = 'mutated'"
        if change == "none":
            async with guarded.begin() as connection:
                await connection.execute(text(mutation))
            assert calls == [
                "connect",
                MIGRATION_CHILD_IDENTITY_SQL,
                "identity-rollback",
                "begin",
                mutation,
                "commit",
                "close",
            ]
            return
        message = "child identity mismatch" if change == "child_oid" else "parent identity changed"
        for surface in (guarded.begin, guarded.connect):
            with pytest.raises(AssertionError, match=message):
                async with surface() as connection:
                    await connection.execute(text(mutation))
        assert mutation not in calls and "begin" not in calls and "commit" not in calls
        if change == "child_oid":
            assert (
                calls
                == [
                    "connect",
                    MIGRATION_CHILD_IDENTITY_SQL,
                    "identity-rollback",
                    "close",
                ]
                * 2
            )
        else:
            assert calls == []

    @pytest.mark.parametrize("provider", ["github", "temp"])
    async def test_owned_resource_receipt_is_accepted(self, tmp_path, provider):
        environment, receipt, actual = self.environment(tmp_path, provider)
        self.bind(environment, receipt)
        connection = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    one=lambda: (
                        "heatpump_test",
                        "heatpump",
                        5432 if provider == "github" else 5433,
                    ),
                    all=lambda: [],
                )
            )
        )
        reader = Mock(return_value=copy.deepcopy(actual))
        accepted = await guard_owned_database(
            connection, require_empty=True, environment=environment, platform_reader=reader
        )
        assert accepted == receipt
        assert connection.execute.await_count == 2
        reader.assert_called_once()

    @pytest.mark.parametrize(
        "case",
        [
            "missing_receipt",
            "malformed_receipt",
            "wrong_token",
            "wrong_attempt",
            "unknown_service",
            "expired",
            "unproven_namespace",
            "wrong_database_host",
            "wrong_database_port",
            "wrong_database_name",
            "wrong_database_user",
            "wrong_database_password",
            "wrong_redis_endpoint",
            "mismatched_sql_identity",
            "non_test_environment",
            "unsafe_model_root",
            "nonempty_initial_database",
            "wrong_platform_image",
            "stopped_service",
            "published_temp_port",
            "foreign_namespace",
            "wrong_receipt_endpoints",
            "wrong_source_digest",
            "drifted_source_manifest",
            "temp_revision_without_digest",
        ],
    )
    async def test_guard_refuses_before_mutation(self, tmp_path, case):
        temp_cases = {"published_temp_port", "temp_revision_without_digest"}
        provider = "temp" if case in temp_cases else "github"
        environment, receipt, actual = self.environment(tmp_path, provider)
        identity = ("heatpump_test", "heatpump", 5432 if provider == "github" else 5433)
        objects = []
        if case == "wrong_token":
            environment["COP_MANUAL_TEST_OWNED"] += "-foreign"
        elif case == "wrong_attempt":
            environment["GITHUB_RUN_ATTEMPT"] = "3"
        elif case == "unknown_service":
            actual["services"][0]["Id"] = "9" * 64
        elif case == "expired":
            receipt["created_at"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
            ).isoformat()
            receipt["expires_at"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
            ).isoformat()
        elif case == "unproven_namespace":
            actual["namespace"]["Id"] = "9" * 64
        elif case.startswith("wrong_database_"):
            url = make_url(TEST_DATABASE_URL)
            replacement = {
                "wrong_database_host": {"host": "db"},
                "wrong_database_port": {"port": 5432},
                "wrong_database_name": {"database": "heatpump"},
                "wrong_database_user": {"username": "postgres"},
                "wrong_database_password": {"password": "foreign"},
            }[case]
            environment["DATABASE_URL"] = url.set(**replacement).render_as_string(
                hide_password=False
            )
        elif case == "wrong_redis_endpoint":
            environment["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        elif case == "mismatched_sql_identity":
            identity = ("heatpump", "heatpump", 5432)
        elif case == "non_test_environment":
            environment["APP_ENVIRONMENT"] = "production"
        elif case == "unsafe_model_root":
            receipt["model_root"] = str(tmp_path)
            environment["MODEL_DIR"] = str(tmp_path)
        elif case == "nonempty_initial_database":
            objects = [("foreign_application_table",)]
        elif case == "wrong_platform_image":
            actual["services"][0]["Config"]["Image"] = "unapproved-image"
        elif case == "stopped_service":
            actual["services"][0]["State"]["Running"] = False
        elif case == "published_temp_port":
            actual["services"][0]["NetworkSettings"]["Ports"] = {
                "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5433"}]
            }
        elif case == "foreign_namespace":
            receipt["namespace"]["name"] = "existing-hpo-test-stack"
            actual["namespace"]["Name"] = receipt["namespace"]["name"]
            context = json.loads(environment["COP_MANUAL_TEST_SERVICE_CONTEXT"])
            for service in context.values():
                service["network"] = receipt["namespace"]["name"]
            environment["COP_MANUAL_TEST_SERVICE_CONTEXT"] = json.dumps(context)
        elif case == "wrong_receipt_endpoints":
            receipt["endpoints"]["postgres"]["port"] = 5432
        elif case == "wrong_source_digest":
            environment[SOURCE_MANIFEST_DIGEST_ENV] = "0" * 64
        elif case == "drifted_source_manifest":
            (Path(environment["COP_MANUAL_TEST_EVIDENCE"]) / SOURCE_MANIFEST_NAME).write_bytes(
                b"{}"
            )
        elif case == "temp_revision_without_digest":
            receipt["revision"] = receipt["commit"]
            environment["COP_MANUAL_TEST_REVISION"] = receipt["commit"]
        self.bind(environment, receipt)
        if case == "missing_receipt":
            environment.pop("COP_MANUAL_TEST_RECEIPT")
        elif case == "malformed_receipt":
            environment["COP_MANUAL_TEST_RECEIPT"] = "{"
        connection = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(one=lambda: identity, all=lambda: objects)
            )
        )
        mutation = AsyncMock()
        with pytest.raises(AssertionError) as refused:
            await guard_owned_database(
                connection,
                require_empty=True,
                environment=environment,
                platform_reader=Mock(return_value=actual),
            )
            await mutation()
        mutation.assert_not_awaited()
        if case == "published_temp_port":
            assert "TEMP service must not publish ports" in str(refused.value)
        expected_refusal = {
            "wrong_source_digest": "source manifest digest mismatch",
            "drifted_source_manifest": "source manifest file mismatch",
            "temp_revision_without_digest": "source revision mismatch",
        }.get(case)
        if expected_refusal:
            assert expected_refusal in str(refused.value)
            assert connection.execute.await_count == 0
        for call in connection.execute.await_args_list:
            statement = str(call.args[0]).upper()
            assert statement.startswith("SELECT ")
            assert not re.search(
                r"\b(CREATE|DROP|ALTER|TRUNCATE|DELETE|INSERT|UPDATE|NEXTVAL|SETVAL)\b", statement
            )

    @pytest.mark.parametrize("needs_kill", [False, True], ids=["term", "kill"])
    async def test_phase_timeout_closes_only_owned_child(self, tmp_path, monkeypatch, needs_kill):
        environment, receipt, _ = self.environment(tmp_path, "github")
        self.bind(environment, receipt)
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        proof = {"commit": receipt["commit"], "digest": "5" * 64, "binding": {"a.py": "6" * 64}}
        monkeypatch.setitem(globals(), "current_verification_source_proof", lambda: proof)
        child = SimpleNamespace(pid=4242, returncode=None, waits=[])

        def wait(timeout):
            child.waits.append(timeout)
            if len(child.waits) == 1 or (needs_kill and len(child.waits) == 2):
                raise subprocess.TimeoutExpired("fake-owned-child", timeout)
            child.returncode = -signal.SIGKILL if needs_kill else -signal.SIGTERM
            return child.returncode

        child.wait = wait
        child.poll = lambda: child.returncode
        launch = Mock(return_value=child)
        kill_group = Mock()
        monkeypatch.setattr(subprocess, "Popen", launch)
        monkeypatch.setattr(os, "getpgid", lambda process_id: child.pid)
        monkeypatch.setattr(os, "killpg", kill_group)
        with pytest.raises(AssertionError, match="verification phase fake-timeout failed"):
            run_verification_phase(
                "fake-timeout", [sys.executable, "-m", "pytest"], owned_sql=False
            )
        result = json.loads(
            (Path(environment["COP_MANUAL_TEST_EVIDENCE"]) / "fake-timeout/process.json").read_text(
                encoding="utf-8"
            )
        )
        assert result["pid"] == result["pgid"] == child.pid
        assert result["timed_out"] is True and result["alive"] is False
        assert 0 < child.waits[0] <= 600
        assert child.waits[1:] == ([10, 10] if needs_kill else [10])
        assert (
            _utc_timestamp(result["deadline"]) - _utc_timestamp(result["started_at"])
        ).total_seconds() == 600
        assert (
            _utc_timestamp(result["parent_deadline"]) - _utc_timestamp(result["started_at"])
        ).total_seconds() == 660
        assert kill_group.call_args_list == [
            ((child.pid, signal.SIGTERM),),
            *((((child.pid, signal.SIGKILL),),) if needs_kill else ()),
        ]
        assert launch.call_args.kwargs["start_new_session"] is True


class TestVerificationSourceManifest:
    """Canonical tested-source proof on a synthetic Git checkout (never the tested one)."""

    SELECTION = {
        "literal_paths": ("pkg/required.py", "docs/readme.md"),
        "prefixes": ("pkg/",),
        "declarations": ("pyproject.toml",),
    }
    FILES = {
        "pkg/required.py": b"A = 1\n",
        "pkg/extra.py": b"B = 2\n",
        "pkg/data.txt": b"not python\n",
        "docs/readme.md": b"readme\n",
        "pyproject.toml": b"[project]\n",
        "other/outside.py": b"C = 3\n",
    }

    @staticmethod
    def git(root, *arguments):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=verification",
                "-c",
                "user.email=verification@example.invalid",
                "-c",
                "core.autocrlf=false",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.decode("utf-8").strip()

    def repository(self, tmp_path, changes=None):
        root = tmp_path / "checkout"
        root.mkdir()
        self.git(root, "init", "-q")
        for path, content in {**self.FILES, **(changes or {})}.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(content)
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", "fixture")
        return root, self.git(root, "rev-parse", "HEAD")

    def prepare(self, tmp_path, root, commit, document=None):
        evidence = tmp_path / "evidence"
        evidence.mkdir(exist_ok=True)
        document = document or build_verification_source_manifest(root, commit, **self.SELECTION)
        raw = canonical_source_manifest(document)
        (evidence / SOURCE_MANIFEST_NAME).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        environment = {
            "COP_MANUAL_TEST_EVIDENCE": str(evidence),
            SOURCE_MANIFEST_DIGEST_ENV: digest,
            "COP_MANUAL_TEST_REVISION": commit,
        }
        return environment, raw

    def proof(self, environment, root):
        return current_verification_source_proof(environment, root, self.SELECTION)

    async def test_manifest_is_canonical_exact_and_lf_equivalent(self, tmp_path):
        root, commit = self.repository(tmp_path)
        document = build_verification_source_manifest(root, commit, **self.SELECTION)
        assert document["schema"] == SOURCE_MANIFEST_SCHEMA and document["commit"] == commit
        assert [entry["path"] for entry in document["entries"]] == [
            "docs/readme.md",
            "pkg/extra.py",
            "pkg/required.py",
            "pyproject.toml",
        ]
        assert all(
            entry["sha256"] == hashlib.sha256(self.FILES[entry["path"]]).hexdigest()
            for entry in document["entries"]
        )
        environment, raw = self.prepare(tmp_path, root, commit, document)
        assert not raw.endswith(b"\n") and b" " not in raw
        # A CRLF worktree checkout of an LF blob is the same tested source.
        (root / "pkg/required.py").write_bytes(b"A = 1\r\n")
        proof = self.proof(environment, root)
        assert proof["commit"] == commit
        assert proof["binding"] == validate_source_manifest_document(document)
        environment["COP_MANUAL_TEST_REVISION"] = f"{commit}+{proof['digest']}"
        assert self.proof(environment, root)["digest"] == proof["digest"]

    @pytest.mark.parametrize(
        "case",
        [
            "extra_field",
            "wrong_schema",
            "short_commit",
            "empty_entries",
            "entry_field",
            "uppercase_hash",
            "duplicate",
            "unsorted",
            "traversal",
            "absolute",
            "backslash",
            "empty_component",
            "dot_component",
            "drive",
            "control",
            "non_string_path",
        ],
    )
    async def test_document_validation_refuses_malformed_manifests(self, case):
        entries = [{"path": "a.py", "sha256": "1" * 64}, {"path": "b.py", "sha256": "2" * 64}]
        document = {"schema": SOURCE_MANIFEST_SCHEMA, "commit": "3" * 40, "entries": entries}
        unsafe = {
            "traversal": "../a.py",
            "absolute": "/a.py",
            "backslash": "pkg\\a.py",
            "empty_component": "pkg//a.py",
            "dot_component": "./a.py",
            "drive": "c:a.py",
            "control": "a\n.py",
            "non_string_path": 7,
        }
        if case == "extra_field":
            document["extra"] = True
        elif case == "wrong_schema":
            document["schema"] = "other/1"
        elif case == "short_commit":
            document["commit"] = "3" * 39
        elif case == "empty_entries":
            document["entries"] = []
        elif case == "entry_field":
            entries[0]["mode"] = "100644"
        elif case == "uppercase_hash":
            entries[0]["sha256"] = "A" * 64
        elif case == "duplicate":
            entries[1]["path"] = "a.py"
        elif case == "unsorted":
            entries.reverse()
        else:
            entries[0]["path"] = unsafe[case]
        with pytest.raises(AssertionError):
            validate_source_manifest_document(document)

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("omitted", "source manifest omits required path"),
            ("unexpected", "source manifest has unexpected path"),
            ("hash", "source manifest hash mismatch"),
            ("digest", "source manifest digest mismatch"),
            ("non_canonical", "non-canonical source manifest"),
            ("worktree_drift", "source drift from tested commit"),
            ("byte_order_mark", "source drift from tested commit"),
            ("commit_moved", "checkout is not the explicit tested commit"),
            ("foreign_revision", "source revision mismatch"),
        ],
    )
    async def test_proof_refuses_drift_and_tampering(self, tmp_path, case, message):
        root, commit = self.repository(tmp_path)
        document = build_verification_source_manifest(root, commit, **self.SELECTION)
        if case == "omitted":
            document["entries"].pop(1)
        elif case == "unexpected":
            document["entries"].append({"path": "pkg/zz.py", "sha256": "4" * 64})
            document["entries"].sort(key=lambda entry: entry["path"])
        elif case == "hash":
            document["entries"][1]["sha256"] = "4" * 64
        environment, raw = self.prepare(tmp_path, root, commit, document)
        if case == "digest":
            environment[SOURCE_MANIFEST_DIGEST_ENV] = "4" * 64
        elif case == "non_canonical":
            pretty = json.dumps(document, indent=1).encode("utf-8")
            (Path(environment["COP_MANUAL_TEST_EVIDENCE"]) / SOURCE_MANIFEST_NAME).write_bytes(
                pretty
            )
            environment[SOURCE_MANIFEST_DIGEST_ENV] = hashlib.sha256(pretty).hexdigest()
        elif case == "worktree_drift":
            (root / "pkg/extra.py").write_bytes(b"B = 3\n")
        elif case == "byte_order_mark":
            (root / "pkg/extra.py").write_bytes(b"\xef\xbb\xbf" + self.FILES["pkg/extra.py"])
        elif case == "commit_moved":
            (root / "pkg/extra.py").write_bytes(b"B = 3\n")
            self.git(root, "commit", "-q", "-am", "moved")
        elif case == "foreign_revision":
            environment["COP_MANUAL_TEST_REVISION"] = "5" * 40
        with pytest.raises(AssertionError, match=message):
            self.proof(environment, root)

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("untracked_literal", "required source is not tracked"),
            ("binary", "unsupported source representation"),
            ("committed_cr", "unsupported source representation"),
            ("invalid_utf8", "unsupported source encoding"),
            ("non_regular_entry", "non-regular tracked source"),
            ("worktree_symlink", "symlinked source path"),
            ("foreign_commit", "checkout is not the explicit tested commit"),
            ("malformed_commit", "malformed tested commit"),
        ],
    )
    async def test_build_refuses_unsupported_sources(self, tmp_path, case, message):
        changes = {
            "binary": {"pkg/blob.py": b"A\0B\n"},
            "committed_cr": {"pkg/crlf.py": b"A = 1\r\n"},
            "invalid_utf8": {"pkg/latin.py": b"A = '\xff'\n"},
        }.get(case)
        root, commit = self.repository(tmp_path, changes)
        selection = dict(self.SELECTION)
        if case == "untracked_literal":
            selection["literal_paths"] += ("pkg/missing.py",)
        elif case == "non_regular_entry":
            blob = self.git(root, "hash-object", "-w", "pkg/required.py")
            self.git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},pkg/link.py")
            self.git(root, "commit", "-q", "-m", "link")
            commit = self.git(root, "rev-parse", "HEAD")
        elif case == "worktree_symlink":
            (root / "pkg/extra.py").unlink()
            os.symlink(root / "pkg/required.py", root / "pkg/extra.py")
        elif case == "foreign_commit":
            commit = "6" * 40
        elif case == "malformed_commit":
            commit = commit.upper()
        with pytest.raises(AssertionError, match=message):
            build_verification_source_manifest(root, commit, **selection)

    async def test_preparation_writes_the_manifest_exactly_once(self, tmp_path):
        root, commit = self.repository(tmp_path)
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        digest = write_verification_source_manifest(evidence, commit, root, self.SELECTION)
        raw = (evidence / SOURCE_MANIFEST_NAME).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        with pytest.raises(FileExistsError):
            write_verification_source_manifest(evidence, commit, root, self.SELECTION)
        assert (evidence / SOURCE_MANIFEST_NAME).read_bytes() == raw

    async def test_child_alembic_invocation_contract(self):
        function, arguments = _migration_child_alembic_invocation("check")
        assert function is command.check and len(arguments) == 1
        assert isinstance(arguments[0], Config)
        for operation, target in (("upgrade", "033"), ("downgrade", "032")):
            function, arguments = _migration_child_alembic_invocation(operation, target)
            assert function is getattr(command, operation) and arguments[1] == target
        for operation, target in (
            ("check", "036"),
            ("upgrade", None),
            ("downgrade", ""),
            ("stamp", "036"),
            ("revision", None),
        ):
            with pytest.raises(AssertionError, match="unsupported migration child Alembic"):
                _migration_child_alembic_invocation(operation, target)

    @pytest.mark.parametrize("before", [None, CURRENT_HEAD_REVISION])
    async def test_primary_head_upgrade_runs_upgrade_then_check(self, tmp_path, before):
        markers = iter([before, CURRENT_HEAD_REVISION])
        calls = []
        assert (
            run_primary_head_upgrade(
                "obs5-primary",
                evidence=tmp_path,
                read_marker=lambda: next(markers),
                run_phase=lambda name, arguments: calls.append((name, arguments[1:])),
            )
            == CURRENT_HEAD_REVISION
        )
        assert calls == [
            ("obs5-primary-upgrade", ["-m", "alembic", "upgrade", "head"]),
            ("obs5-primary-check", ["-m", "alembic", "check"]),
        ]
        assert json.loads((tmp_path / "obs5-primary-head.json").read_text(encoding="utf-8")) == {
            "before": before,
            "after": CURRENT_HEAD_REVISION,
            "head": CURRENT_HEAD_REVISION,
        }

    @pytest.mark.parametrize(
        ("markers", "message", "phases"),
        [
            (["035"], "unexpected primary migration state", 0),
            ([None, "035"], "primary is not at the reviewed head", 2),
        ],
    )
    async def test_primary_head_upgrade_refuses_unproven_state(
        self, tmp_path, markers, message, phases
    ):
        values = iter(markers)
        calls = []
        with pytest.raises(AssertionError, match=message):
            run_primary_head_upgrade(
                "obs5-primary",
                evidence=tmp_path,
                read_marker=lambda: next(values),
                run_phase=lambda name, arguments: calls.append(name),
            )
        assert len(calls) == phases
        assert not (tmp_path / "obs5-primary-head.json").exists()


class SyntheticVerificationEvidence:
    """Unit fixture only: a synthetic evidence tree, never tested-checkout authority."""

    COMMIT = "a" * 40
    DIGEST = "b" * 64
    TOKEN = "cop-manual-mon-1-1"
    NODES = {
        "migration": [MANUAL_MODULE + "::TestManualCOPPostgreSQLMigration::test_migrate"],
        "manual": [MANUAL_MODULE + "::TestOwnedResourceGuard::test_guard"],
        "exhaustion": [
            MANUAL_MODULE
            + "::TestManualCOPPostgreSQLAPI::test_sequence_exhaustion_is_503_and_never_wraps"
        ],
        "obs5-real-migration": [REAL_MIGRATION_CLASS + "::test_round_trip"],
        "obs5-pg-original": [
            LIFECYCLE_MODULE + "::test_lifecycle",
            LIFECYCLE_MODULE + "::test_lifecycle_second[a]",
        ],
        "obs5-pg-supplement": [
            LIFECYCLE_VERIFICATION_MODULE + "::test_supplement",
            LIFECYCLE_VERIFICATION_MODULE + "::TestOBS5ScopeSeal::test_resource_free",
        ],
        "ordinary": [
            "tests/e2e/test_price_providers.py::test_entsoe",
            "tests/e2e/test_api_panasonic.py::test_capabilities[a]",
        ],
        "backup-roundtrip": [BACKUP_MODULE + "::test_owned_worker_and_coordinator_image_roundtrip"],
    }

    # Inventory fixture-closure obligations; the resource-free supplement node has none.
    RECEIPT_REQUIRED = (
        LIFECYCLE_MODULE + "::test_lifecycle",
        LIFECYCLE_MODULE + "::test_lifecycle_second[a]",
        LIFECYCLE_VERIFICATION_MODULE + "::test_supplement",
    )

    def binding(self):
        modules = {node.split("::", 1)[0] for nodes in self.NODES.values() for node in nodes}
        return {"heatpump-optimizer/" + module: "c" * 64 for module in sorted(modules)} | {
            "heatpump-optimizer/scripts/backup_db.sh": "c" * 64
        }

    def process(self, evidence, name, **changes):
        (evidence / name).mkdir(exist_ok=True)
        process = {
            "exit": 0,
            "alive": False,
            "timed_out": False,
            "commit": self.COMMIT,
            "source_manifest_sha256": self.DIGEST,
            "source": self.binding(),
        } | changes
        (evidence / name / "process.json").write_text(json.dumps(process), encoding="utf-8")

    def phase(self, evidence, name, selected, collected=None):
        self.process(evidence, name)
        collected = selected if collected is None else collected
        (evidence / name / "nodes.json").write_text(
            json.dumps(
                {
                    "phase": name,
                    "collected": collected,
                    "selected": selected,
                    "receipt_fixture": LIFECYCLE_RESOURCE_FIXTURE,
                    "receipt_required": [
                        node for node in collected if node in self.RECEIPT_REQUIRED
                    ],
                    "source_manifest_sha256": self.DIGEST,
                    "exit": 0,
                }
            ),
            encoding="utf-8",
        )
        (evidence / name / "reports.jsonl").write_text(
            "".join(
                json.dumps({"nodeid": node, "when": when, "outcome": "passed", "wasxfail": False})
                + "\n"
                for node in selected
                for when in ("setup", "call", "teardown")
            ),
            encoding="utf-8",
        )

    def evidence(self, tmp_path):
        assert set(self.NODES) == set(SEALED_TEST_PHASES), "synthetic phase fixture drifted"
        evidence = tmp_path / "owned" / "evidence"
        evidence.mkdir(parents=True, mode=0o700)
        source = {"commit": self.COMMIT, "source_manifest_sha256": self.DIGEST}
        (evidence.parent / "ownership.json").write_text(
            json.dumps(source | {"token": self.TOKEN}), encoding="utf-8"
        )
        (evidence / "preflight.json").write_text(json.dumps(source), encoding="utf-8")
        every = [node for nodes in self.NODES.values() for node in nodes]
        manual = self.NODES["migration"] + self.NODES["manual"] + self.NODES["exhaustion"]
        self.phase(evidence, "e2e-inventory", [], every)
        self.phase(evidence, "manual-inventory", [], manual)
        obs5 = [node for name in OBS5_TEST_PHASES for node in self.NODES[name]]
        self.phase(evidence, "obs5-inventory", [], obs5)
        for name, nodes in self.NODES.items():
            self.phase(evidence, name, nodes)
        for name in REQUIRED_SCHEMA_PHASES:
            self.process(evidence, name)
        (evidence / "obs5-primary-head.json").write_text(
            json.dumps(
                {"before": None, "after": CURRENT_HEAD_REVISION, "head": CURRENT_HEAD_REVISION}
            ),
            encoding="utf-8",
        )
        for node in self.RECEIPT_REQUIRED:
            self.write_receipt(evidence, node)
        for node in self.NODES["backup-roundtrip"]:
            # Fixture construction, like write_receipt below, must not invoke
            # the publication/lock operations these tests deliberately fault.
            name = "backup-receipt-" + hashlib.sha256(node.encode()).hexdigest()[:32] + ".json"
            (evidence / name).write_text(
                json.dumps(
                    {
                        "test": node,
                        "commit": self.COMMIT,
                        "source_manifest_sha256": self.DIGEST,
                        "ownership_token": self.TOKEN,
                        "created_resources": ["d" * 64],
                        "source_preserved": True,
                        "cleanup_verified": True,
                        "coordinator_verified": True,
                        "worker_script_sha256": self.binding()[
                            "heatpump-optimizer/scripts/backup_db.sh"
                        ],
                    }
                ),
                encoding="utf-8",
            )
        proof = {"commit": self.COMMIT, "digest": self.DIGEST, "binding": self.binding()}
        return evidence, proof

    def reports(self, evidence, name):
        path = evidence / name / "reports.jsonl"
        return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def receipt_path(self, evidence, node, suffix=""):
        name = hashlib.sha256(node.encode()).hexdigest()[:32]
        return evidence / ("obs5-receipt-" + name + suffix + ".json")

    def write_receipt(self, evidence, node, suffix=""):
        self.receipt_path(evidence, node, suffix).write_text(
            json.dumps(
                {
                    "test": node,
                    "commit": self.COMMIT,
                    "source_manifest_sha256": self.DIGEST,
                    "ownership_token": self.TOKEN,
                    "schema_owned": True,
                    "schema_removed": True,
                    "public_manual_storage_unchanged": True,
                }
            ),
            encoding="utf-8",
        )

    def lifecycle_receipt(self, evidence, name):
        return self.receipt_path(evidence, self.NODES[name][0])

    def edit_nodes(self, evidence, name, **changes):
        path = evidence / name / "nodes.json"
        nodes = json.loads(path.read_text(encoding="utf-8")) | changes
        path.write_text(json.dumps(nodes), encoding="utf-8")
        return nodes

    def mutate(self, evidence, case):
        """Shared bound-receipt mutations for the full and OBS-only seals."""

        def edit(path, **changes):
            path.write_text(
                json.dumps(json.loads(path.read_text(encoding="utf-8")) | changes),
                encoding="utf-8",
            )

        original = self.lifecycle_receipt(evidence, "obs5-pg-original")
        supplement = self.lifecycle_receipt(evidence, "obs5-pg-supplement")
        head = evidence / "obs5-primary-head.json"
        if case == "process_digest":
            self.process(evidence, "obs5-pg-supplement", source_manifest_sha256="d" * 64)
        elif case == "process_source":
            self.process(evidence, "check-obs5", source={"other.py": "e" * 64})
        elif case == "schema_check_failed":
            self.process(evidence, "check-obs5", exit=1)
        elif case == "primary_check_missing":
            (evidence / "obs5-primary-check" / "process.json").unlink()
        elif case == "primary_not_head":
            edit(head, after="035")
        elif case == "primary_unexpected_before":
            edit(head, before="032")
        elif case == "preflight_digest":
            edit(evidence / "preflight.json", source_manifest_sha256="f" * 64)
        elif case == "ownership_commit":
            edit(evidence.parent / "ownership.json", commit="f" * 40)
        elif case == "lifecycle_cleanup":
            edit(original, schema_removed=False)
        elif case == "lifecycle_public_drift":
            edit(supplement, public_manual_storage_unchanged=False)
        elif case == "lifecycle_digest":
            edit(original, source_manifest_sha256="f" * 64)
        elif case == "no_lifecycle_receipt":
            original.unlink()
            supplement.unlink()
        elif case == "missing_supplement_receipt":
            supplement.unlink()
        elif case == "lifecycle_foreign_test":
            edit(supplement, test=self.NODES["ordinary"][0])
        elif case == "lifecycle_duplicate_test":
            edit(supplement, test=self.NODES["obs5-pg-original"][0])
        elif case == "lifecycle_foreign_token":
            edit(original, ownership_token="cop-manual-mon-9-9")
        elif case == "lifecycle_truthy_flag":
            edit(original, schema_owned="yes")
        elif case == "missing_original_second_receipt":
            self.receipt_path(evidence, self.NODES["obs5-pg-original"][1]).unlink()
        elif case == "duplicate_receipt":
            self.write_receipt(evidence, self.NODES["obs5-pg-original"][1], "-again")
        elif case == "extra_resource_free_receipt":
            self.write_receipt(evidence, self.NODES["obs5-pg-supplement"][1])
        elif case in {"inventory_obligations_missing", "inventory_unbound_digest"}:
            for name in ("e2e-inventory", "obs5-inventory"):
                path = evidence / name / "nodes.json"
                nodes = json.loads(path.read_text(encoding="utf-8"))
                if case == "inventory_obligations_missing":
                    nodes.pop("receipt_required")
                else:
                    nodes["source_manifest_sha256"] = "d" * 64
                path.write_text(json.dumps(nodes), encoding="utf-8")
        elif case == "inventory_drops_obligation":
            for name in ("e2e-inventory", "obs5-inventory"):
                self.edit_nodes(evidence, name, receipt_required=list(self.RECEIPT_REQUIRED[1:]))
        elif case == "obligation_outside_lifecycle":
            for name in ("e2e-inventory", "obs5-inventory"):
                self.edit_nodes(
                    evidence,
                    name,
                    receipt_required=[*self.RECEIPT_REQUIRED, self.NODES["obs5-real-migration"][0]],
                )
        elif case == "phase_inventory_drift":
            self.edit_nodes(
                evidence, "obs5-pg-original", receipt_required=[self.NODES["obs5-pg-original"][0]]
            )
        else:
            raise AssertionError("unknown synthetic mutation " + case)


class TestLifecycleReceiptSeal(SyntheticVerificationEvidence):
    """Actual seals/runner, genuine unit-fixture Git proof, synthetic execution evidence."""

    def binding(self):
        return self._proof["binding"]

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    @pytest.mark.parametrize(
        "failure", ("unlock_before", "unlock_after", "close_after", "close_before")
    )
    async def test_lock_settlement_failure_revokes_own_seal(
        self, tmp_path, monkeypatch, path, failure
    ):
        injected = OSError("injected " + failure)
        descriptor = None
        close_called = False
        close_injected = False
        real_close = os.close
        if os.name == "nt":
            import msvcrt

            primitive, release = msvcrt.locking, msvcrt.LK_UNLCK
            target, attribute = msvcrt, "locking"
        else:
            import fcntl

            primitive, release = fcntl.flock, fcntl.LOCK_UN
            target, attribute = fcntl, "flock"

        def locking(fd, operation, *args):
            nonlocal descriptor
            if operation == release:
                descriptor = fd
                if failure == "unlock_before":
                    raise injected
                result = primitive(fd, operation, *args)
                if failure == "unlock_after":
                    raise injected
                return result
            return primitive(fd, operation, *args)

        def closing(fd):
            nonlocal close_called, close_injected
            if fd == descriptor:
                close_called = True
                if failure == "close_before" and not close_injected:
                    close_injected = True
                    raise injected
                result = real_close(fd)
                if failure == "close_after" and not close_injected:
                    close_injected = True
                    raise injected
                return result
            return real_close(fd)

        monkeypatch.setattr(target, attribute, locking)
        monkeypatch.setattr(os, "close", closing)
        try:
            await self.test_receipt_namespace_is_closed(tmp_path, monkeypatch, path, "racing_valid")
            assert descriptor is not None and close_called
            if failure == "close_before":
                # A failing close syscall has uncertain settlement; no retry in the
                # implementation. Test owns this descriptor and releases it explicitly.
                os.fstat(descriptor)
                real_close(descriptor)
            else:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
        finally:
            monkeypatch.setattr(target, attribute, primitive)
            monkeypatch.setattr(os, "close", real_close)
        evidence = tmp_path / "owned" / "evidence"
        with lifecycle_evidence_lock(evidence):
            pass

    async def test_preexisting_seal_is_never_revoked(self, tmp_path):
        evidence, current = self.bound_evidence(tmp_path)
        seal = evidence / "seal.json"
        seal.write_text("earlier invocation", encoding="utf-8")
        identity = seal.stat().st_ino
        with pytest.raises(AssertionError, match="already sealed"):
            seal_verification_evidence(evidence, current())
        assert seal.read_text(encoding="utf-8") == "earlier invocation"
        assert seal.stat().st_ino == identity

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    async def test_publish_then_error_still_revokes_marker(self, tmp_path, monkeypatch, path):
        real_link = os.link

        def failing(source, destination, *args, **kwargs):
            result = real_link(source, destination, *args, **kwargs)
            if Path(destination).name in {"seal.json", "obs5-scope-seal.json"}:
                raise OSError("publication settlement failed")
            return result

        monkeypatch.setattr(os, "link", failing)
        await self.test_receipt_namespace_is_closed(tmp_path, monkeypatch, path, "racing_valid")

    async def test_replaced_marker_is_not_deleted_on_failure(self, tmp_path, monkeypatch):
        evidence, current = self.bound_evidence(tmp_path)
        real_link = os.link
        replaced = evidence / "earlier-marker"
        replaced.write_text("other invocation", encoding="utf-8")

        def replacing(source, destination, *args, **kwargs):
            result = real_link(source, destination, *args, **kwargs)
            if Path(destination).name == "seal.json":
                os.replace(replaced, destination)
                raise OSError("publication marker replaced")
            return result

        monkeypatch.setattr(os, "link", replacing)
        with pytest.raises(AssertionError, match="revocation failed"):
            seal_verification_evidence(evidence, current())
        assert (evidence / "seal.json").read_text(encoding="utf-8") == "other invocation"

    async def test_body_and_unlock_errors_are_both_preserved(self, tmp_path, monkeypatch):
        evidence = tmp_path / "evidence"
        evidence.mkdir(mode=0o700)
        body, cleanup = RuntimeError("body failure"), OSError("unlock failure")
        if os.name == "nt":
            import msvcrt

            target, name, release = msvcrt, "locking", msvcrt.LK_UNLCK
        else:
            import fcntl

            target, name, release = fcntl, "flock", fcntl.LOCK_UN
        primitive = getattr(target, name)
        descriptors = []

        def failing(fd, operation, *args):
            if operation == release:
                descriptors.append(fd)
                raise cleanup
            return primitive(fd, operation, *args)

        monkeypatch.setattr(target, name, failing)
        with pytest.raises(AssertionError, match="settlement failed") as caught:
            with lifecycle_evidence_lock(evidence):
                raise body
        assert caught.value.__cause__.exceptions == (body, cleanup)
        with pytest.raises(OSError):
            os.fstat(descriptors[0])

    def bound_evidence(self, tmp_path):
        fixture = TestVerificationSourceManifest()
        paths = SyntheticVerificationEvidence.binding(self)
        root = Path(__file__).resolve().parents[3]
        fixture.FILES = {path: (root / path).read_bytes().replace(b"\r\n", b"\n") for path in paths}
        fixture.SELECTION = {"literal_paths": tuple(paths), "prefixes": (), "declarations": ()}
        checkout, commit = fixture.repository(tmp_path)
        environment, _ = fixture.prepare(tmp_path, checkout, commit)
        self._proof = fixture.proof(environment, checkout)
        self.COMMIT, self.DIGEST = commit, self._proof["digest"]
        evidence, _ = self.evidence(tmp_path)
        return evidence, lambda: fixture.proof(environment, checkout)

    def change_receipt(self, evidence, case):
        free = self.NODES["obs5-pg-supplement"][1]
        extra = self.receipt_path(evidence, free)
        original = self.lifecycle_receipt(evidence, "obs5-pg-original")
        if case == "valid":
            return
        if case == "extra_missing_owner":
            extra.write_text(json.dumps({"test": free, "schema_removed": True}), encoding="utf-8")
        elif case == "extra_malformed":
            extra.write_text("{", encoding="utf-8")
        elif case == "extra_missing_fields":
            extra.write_text(json.dumps({"ownership_token": self.TOKEN}), encoding="utf-8")
        elif case == "extra_wrong_owner":
            self.write_receipt(evidence, free)
            receipt = json.loads(extra.read_text(encoding="utf-8"))
            receipt["ownership_token"] = "foreign"
            extra.write_text(json.dumps(receipt), encoding="utf-8")
        elif case == "extra_resource_free":
            self.write_receipt(evidence, free)
        elif case == "required_missing_owner":
            receipt = json.loads(original.read_text(encoding="utf-8"))
            receipt.pop("ownership_token")
            original.write_text(json.dumps(receipt), encoding="utf-8")
        elif case == "required_missing_fields":
            original.write_text(json.dumps({"test": self.RECEIPT_REQUIRED[0]}), encoding="utf-8")
        elif case == "required_malformed":
            original.write_text("null", encoding="utf-8")
        elif case == "required_wrong_filename":
            original.rename(evidence / "obs5-receipt-unknown.json")
        elif case == "duplicate_filename":
            self.write_receipt(evidence, self.RECEIPT_REQUIRED[0], "-duplicate")
        elif case == "unsafe_filename":
            (evidence / "obs5-receipt-unknown.txt").write_text("{}", encoding="utf-8")
        elif case == "receipt_directory":
            original.unlink()
            original.mkdir()
        elif case == "required_wrong_node":
            receipt = json.loads(original.read_text(encoding="utf-8"))
            receipt["test"] = free
            original.write_text(json.dumps(receipt), encoding="utf-8")
        elif case == "required_wrong_owner":
            receipt = json.loads(original.read_text(encoding="utf-8"))
            receipt["ownership_token"] = "foreign"
            original.write_text(json.dumps(receipt), encoding="utf-8")
        else:
            raise AssertionError("unknown receipt case " + case)

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    async def test_mutation_after_enumeration_cannot_persist_seal(
        self, tmp_path, monkeypatch, path
    ):
        original_read = Path.read_text
        mutated = False

        def racing_read(file, *args, **kwargs):
            nonlocal mutated
            if file.name.startswith("obs5-receipt-") and not mutated:
                mutated = True
                free = self.NODES["obs5-pg-supplement"][1]
                self.receipt_path(file.parent, free).write_text(
                    json.dumps({"test": free, "schema_removed": True}), encoding="utf-8"
                )
            return original_read(file, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", racing_read)
        await self.test_receipt_namespace_is_closed(tmp_path, monkeypatch, path, "racing_valid")
        assert mutated

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    @pytest.mark.parametrize("moment", ("read", "publish"))
    async def test_concurrent_protocol_writer_is_fenced(self, tmp_path, monkeypatch, path, moment):
        original_read, original_link = Path.read_text, os.link
        attempted = False
        refusals = []

        def attempt(evidence):
            nonlocal attempted
            if attempted:
                return
            attempted = True

            def writer():
                try:
                    write_lifecycle_receipt(evidence, {"test": "unowned"})
                except AssertionError as error:
                    refusals.append(str(error))

            worker = threading.Thread(target=writer)
            worker.start()
            worker.join(5)
            assert not worker.is_alive(), "receipt contender did not finish"

        def read(file, *args, **kwargs):
            if moment == "read" and file.name.startswith("obs5-receipt-"):
                attempt(file.parent)
            return original_read(file, *args, **kwargs)

        def link(source, destination, *args, **kwargs):
            destination = Path(destination)
            if moment == "publish" and destination.name in {"seal.json", "obs5-scope-seal.json"}:
                attempt(destination.parent)
            return original_link(source, destination, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
        monkeypatch.setattr(os, "link", link)
        await self.test_receipt_namespace_is_closed(tmp_path, monkeypatch, path, "valid")
        assert attempted and refusals == ["lifecycle receipt lock unavailable"]
        with pytest.raises(AssertionError, match="sealed"):
            write_lifecycle_receipt(tmp_path / "owned" / "evidence", {"test": "late"})

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    async def test_nonprotocol_publish_mutation_removes_seal(self, tmp_path, monkeypatch, path):
        original_link = os.link

        def link(source, destination, *args, **kwargs):
            result = original_link(source, destination, *args, **kwargs)
            destination = Path(destination)
            if destination.name in {"seal.json", "obs5-scope-seal.json"}:
                free = self.NODES["obs5-pg-supplement"][1]
                self.receipt_path(destination.parent, free).write_text("{}", encoding="utf-8")
            return result

        monkeypatch.setattr(os, "link", link)
        await self.test_receipt_namespace_is_closed(tmp_path, monkeypatch, path, "racing_valid")

    async def test_process_lock_contention_and_crash_release(self, tmp_path):
        evidence = tmp_path / "evidence"
        evidence.mkdir(mode=0o700)
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from tests.e2e.test_manual_cop_postgresql import lifecycle_evidence_lock\n"
            "with lifecycle_evidence_lock(Path(sys.argv[1])):\n"
            " print('LOCKED', flush=True)\n"
            " sys.stdin.read()\n"
        )
        environment = dict(os.environ)
        root = Path(__file__).resolve().parents[3]
        python_paths = (str(root), str(root.parent), environment.get("PYTHONPATH", ""))
        environment["PYTHONPATH"] = os.pathsep.join(path for path in python_paths if path)
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(evidence)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            ready = []
            reader = threading.Thread(target=lambda: ready.append(child.stdout.readline().strip()))
            reader.start()
            reader.join(10)
            if ready != ["LOCKED"] and child.poll() is not None:
                _, child_stderr = child.communicate(timeout=10)
                pytest.fail(
                    "receipt lock child did not become ready "
                    f"(exit {child.returncode}): {child_stderr}"
                )
            assert ready == ["LOCKED"], "receipt lock child did not become ready within 10s"
            with pytest.raises(AssertionError, match="lock unavailable"):
                write_lifecycle_receipt(evidence, {"test": "contender"})
        finally:
            child.kill()
            child.communicate(timeout=10)
            reader.join(5)
        write_lifecycle_receipt(evidence, {"test": "after-crash"})
        assert (evidence / ".lifecycle-evidence.lock").exists()
        with pytest.raises(AssertionError, match="lock unavailable"):
            write_lifecycle_receipt(evidence, {"test": "after-crash"})

    @pytest.mark.parametrize(
        "case",
        (
            "malformed_lock",
            "directory_lock",
            "hardlinked_lock",
            "permission_denied",
            "publication_failure",
        ),
    )
    async def test_lock_or_publication_failure_never_seals(self, tmp_path, monkeypatch, case):
        evidence, current = self.bound_evidence(tmp_path)
        lock = evidence / ".lifecycle-evidence.lock"
        if case == "malformed_lock":
            lock.write_text("not a lock", encoding="utf-8")
        elif case == "directory_lock":
            lock.mkdir()
        elif case == "hardlinked_lock":
            lock.touch()
            os.link(lock, evidence / "lock-alias")
        elif case == "permission_denied":

            def denied_open(*args, **kwargs):
                raise PermissionError("test lock denied")

            monkeypatch.setattr(os, "open", denied_open)
        else:

            def denied(*args, **kwargs):
                raise PermissionError("test publication denied")

            monkeypatch.setattr(os, "link", denied)
        with pytest.raises(AssertionError, match="lifecycle receipt"):
            seal_verification_evidence(evidence, current())
        assert not (evidence / "seal.json").exists()
        assert not list(evidence.glob(".lifecycle-pending-*"))

    @pytest.mark.parametrize("path", ("full", "obs", "prepared"))
    @pytest.mark.parametrize(
        "case",
        (
            "valid",
            "extra_missing_owner",
            "extra_malformed",
            "extra_missing_fields",
            "extra_wrong_owner",
            "extra_resource_free",
            "required_missing_owner",
            "required_missing_fields",
            "required_malformed",
            "required_wrong_filename",
            "duplicate_filename",
            "unsafe_filename",
            "receipt_directory",
            "required_wrong_node",
            "required_wrong_owner",
        ),
    )
    async def test_receipt_namespace_is_closed(self, tmp_path, monkeypatch, path, case):
        from tests.e2e import test_ml_lifecycle_verification as verification

        evidence, current = self.bound_evidence(tmp_path)
        if case != "racing_valid":
            self.change_receipt(evidence, case)
        seal_path = evidence / ("seal.json" if path == "full" else "obs5-scope-seal.json")

        def invoke():
            proof = current()
            if path == "full":
                seal_verification_evidence(evidence, proof)
            elif path == "obs":
                verification.seal_obs5_scope(
                    evidence,
                    proof,
                    current(),
                    verification.OBS5_RUNNER_PHASES,
                    lambda: CURRENT_HEAD_REVISION,
                )
            else:
                assert verification._run_owned_verification(prepared=True) == 0

        if path == "prepared":
            # Only external execution is faked; proof creation and all seal validators are real.
            root = evidence.parent
            (root / "models").mkdir()
            monkeypatch.setenv("MODEL_DIR", str(root / "models"))
            monkeypatch.delenv("HPO_OBS5_EXPORT", raising=False)
            monkeypatch.setattr(
                verification,
                "owned_test_environment",
                lambda: {
                    "temp_root": str(root),
                    "token": self.TOKEN,
                    "commit": self.COMMIT,
                    "source_manifest_sha256": self.DIGEST,
                },
            )
            monkeypatch.setattr(verification, "current_verification_source_proof", current)
            monkeypatch.setattr(
                verification,
                "_source_receipt",
                lambda receipt: {
                    "commit": self.COMMIT,
                    "source_manifest_sha256": self.DIGEST,
                },
            )
            monkeypatch.setattr(verification, "_wait_owned_health", lambda *args: None)
            monkeypatch.setattr(verification, "run_verification_phase", lambda *args: None)
            monkeypatch.setattr(verification, "run_primary_head_upgrade", lambda *args: None)
            version = verification.importlib.metadata.version
            monkeypatch.setattr(
                verification.importlib.metadata,
                "version",
                lambda name: "2026.4" if name == "tzdata" else version(name),
            )

            async def marker():
                return CURRENT_HEAD_REVISION

            def immediate(coroutine):
                try:
                    coroutine.send(None)
                except StopIteration as stop:
                    return stop.value
                coroutine.close()
                raise AssertionError("fake marker unexpectedly suspended")

            monkeypatch.setattr(verification, "_primary_marker", marker)
            monkeypatch.setattr(verification, "asyncio", SimpleNamespace(run=immediate))
        if case == "valid":
            invoke()
            assert seal_path.exists()
            assert not self.receipt_path(evidence, self.NODES["obs5-pg-supplement"][1]).exists()
        else:
            with pytest.raises(AssertionError, match="lifecycle receipt"):
                invoke()
            assert not seal_path.exists()


class TestVerificationSeal(SyntheticVerificationEvidence):
    """Seal reconciliation over a synthetic evidence tree; no process or database."""

    @pytest.mark.parametrize(
        "missing",
        ["phase", "receipt", "source_preserved", "cleanup_verified", "coordinator_verified"],
    )
    async def test_backup_phase_and_bound_cleanup_receipt_remain_required(self, tmp_path, missing):
        evidence, proof = self.evidence(tmp_path)
        if missing == "phase":
            (evidence / "backup-roundtrip" / "process.json").unlink()
        else:
            path = next(evidence.glob("backup-receipt-*.json"))
            if missing == "receipt":
                path.unlink()
            else:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                receipt[missing] = False
                path.write_text(json.dumps(receipt), encoding="utf-8")
        with pytest.raises(AssertionError, match="backup"):
            seal_verification_evidence(evidence, proof)
        assert not (evidence / "seal.json").exists()

    async def test_complete_disjoint_partition_seals(self, tmp_path):
        evidence, proof = self.evidence(tmp_path)
        seal_verification_evidence(evidence, proof)
        seal = json.loads((evidence / "seal.json").read_text(encoding="utf-8"))
        assert (seal["commit"], seal["source_manifest_sha256"]) == (self.COMMIT, self.DIGEST)
        assert seal["partitions"] == {name: sorted(nodes) for name, nodes in self.NODES.items()}
        assert seal["primary_head"]["after"] == CURRENT_HEAD_REVISION
        # The resource-free supplement node needs (and has) no lifecycle receipt.
        free = self.NODES["obs5-pg-supplement"][1]
        assert free not in self.RECEIPT_REQUIRED
        assert not self.receipt_path(evidence, free).exists()

    async def test_receipt_obligations_follow_the_actual_fixture_closure(self, tmp_path):
        """Real pytest collection: direct, transitive and usefixtures closures only."""
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (tmp_path / "test_closure_probe.py").write_text(
            "import pytest\n\n\n"
            "@pytest.fixture\ndef lifecycle_database():\n    yield\n\n\n"
            "@pytest.fixture\ndef wrapped(lifecycle_database):\n    yield\n\n\n"
            "def test_direct(lifecycle_database):\n    pass\n\n\n"
            "def test_transitive(wrapped):\n    pass\n\n\n"
            "@pytest.mark.usefixtures('lifecycle_database')\ndef test_usefixtures():\n    pass\n\n\n"
            "@pytest.mark.parametrize('value', [1, 2])\ndef test_param(wrapped, value):\n    pass\n\n\n"
            "def test_free(tmp_path):\n    pass\n",
            encoding="utf-8",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("COP_MANUAL_TEST_") or key == "COP_MANUAL_TEST_PROVIDER"
        }
        environment |= {
            "PYTEST_PLUGINS": "tests.e2e.test_manual_cop_postgresql",
            "COP_MANUAL_TEST_PHASE": "closure-probe",
            "COP_MANUAL_TEST_NODES": str(tmp_path / "nodes.json"),
            "COP_MANUAL_TEST_REPORTS": str(tmp_path / "reports.jsonl"),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--noconftest",
                "-p",
                "no:cacheprovider",
                "-c",
                str(tmp_path / "pytest.ini"),
                "--rootdir",
                str(tmp_path),
                "--collect-only",
                "-q",
                str(tmp_path / "test_closure_probe.py"),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        nodes = json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))
        module = "test_closure_probe.py::"
        assert nodes["receipt_fixture"] == LIFECYCLE_RESOURCE_FIXTURE
        assert nodes["receipt_required"] == sorted(
            module + name
            for name in (
                "test_direct",
                "test_transitive",
                "test_usefixtures",
                "test_param[1]",
                "test_param[2]",
            )
        )
        assert module + "test_free" in nodes["collected"]
        assert module + "test_free" not in nodes["receipt_required"]

    async def test_receipt_obligations_use_each_items_fixture_closure(self):
        items = [
            SimpleNamespace(nodeid="b", fixturenames=("tmp_path", LIFECYCLE_RESOURCE_FIXTURE)),
            SimpleNamespace(nodeid="a", fixturenames=["wrapped", LIFECYCLE_RESOURCE_FIXTURE]),
            SimpleNamespace(nodeid="c", fixturenames=("lifecycle_database_like",)),
            SimpleNamespace(nodeid="d"),
        ]
        assert lifecycle_receipt_obligations(items) == ["a", "b"]

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("unrun_node", "not completely partitioned"),
            ("overlap", "overlap"),
            ("foreign_ordinary", "does not own"),
            ("duplicate_call", "exactly one setup/call/teardown"),
            ("missing_teardown", "exactly one setup/call/teardown"),
            ("failed_setup", "skip/xfail/failure"),
            ("failed_teardown", "skip/xfail/failure"),
            ("skipped", "skip/xfail/failure"),
            ("xfail", "skip/xfail/failure"),
            ("nonzero_exit", "did not succeed"),
            ("alive", "did not succeed"),
            ("timed_out", "did not succeed"),
            ("process_digest", "verification source drift"),
            ("process_source", "verification source drift"),
            ("outside_manifest", "outside the source manifest"),
            ("manual_inventory", "manual phase inventory is incomplete"),
            ("schema_check_failed", "did not succeed"),
            ("primary_not_head", "primary head was not proven"),
            ("preflight_digest", "receipt source digest mismatch"),
            ("ownership_commit", "receipt source digest mismatch"),
            ("lifecycle_cleanup", "lifecycle receipt"),
            ("lifecycle_digest", "lifecycle receipt"),
            ("no_lifecycle_receipt", "lifecycle receipt"),
            ("missing_supplement_receipt", "lifecycle receipt"),
            ("lifecycle_foreign_test", "lifecycle receipt"),
            ("lifecycle_duplicate_test", "lifecycle receipt"),
            ("lifecycle_foreign_token", "lifecycle receipt"),
            ("lifecycle_truthy_flag", "lifecycle receipt"),
            ("lifecycle_public_drift", "lifecycle receipt"),
            ("primary_unexpected_before", "primary head was not proven"),
            ("primary_check_missing", "did not succeed"),
            ("missing_original_second_receipt", "lifecycle receipt coverage mismatch"),
            ("duplicate_receipt", "lifecycle receipt coverage mismatch"),
            ("extra_resource_free_receipt", "lifecycle receipt coverage mismatch"),
            ("inventory_obligations_missing", "lifecycle receipt inventory is missing or unbound"),
            ("inventory_unbound_digest", "lifecycle receipt inventory is missing or unbound"),
            ("inventory_drops_obligation", "lifecycle receipt inventory drifted"),
            ("obligation_outside_lifecycle", "ran outside a lifecycle phase"),
            ("phase_inventory_drift", "lifecycle receipt inventory drifted"),
        ],
    )
    async def test_incomplete_or_unbound_evidence_refuses_seal(self, tmp_path, case, message):
        evidence, proof = self.evidence(tmp_path)
        every = [node for nodes in self.NODES.values() for node in nodes]
        if case == "unrun_node":
            self.phase(
                evidence, "e2e-inventory", [], every + ["tests/e2e/test_api_panasonic.py::test_x"]
            )
        elif case == "overlap":
            self.phase(evidence, "manual", self.NODES["manual"] + self.NODES["migration"])
        elif case == "foreign_ordinary":
            self.phase(
                evidence, "ordinary", self.NODES["ordinary"] + self.NODES["obs5-pg-original"]
            )
        elif case in {
            "duplicate_call",
            "missing_teardown",
            "failed_setup",
            "failed_teardown",
            "skipped",
            "xfail",
        }:
            path, reports = self.reports(evidence, "ordinary")
            if case == "duplicate_call":
                reports.append(reports[1])
            elif case == "missing_teardown":
                reports.pop(2)
            elif case == "failed_setup":
                reports[0]["outcome"] = "failed"
            elif case == "failed_teardown":
                reports[2]["outcome"] = "failed"
            elif case == "skipped":
                reports[0]["outcome"] = "skipped"
                reports.pop(1)
            else:
                reports[1]["wasxfail"] = True
            path.write_text("".join(json.dumps(report) + "\n" for report in reports))
        elif case == "nonzero_exit":
            self.process(evidence, "ordinary", exit=1)
        elif case == "alive":
            self.process(evidence, "obs5-pg-original", alive=True)
        elif case == "timed_out":
            self.process(evidence, "manual", timed_out=True)
        elif case == "process_digest":
            self.process(evidence, "heads", source_manifest_sha256="d" * 64)
        elif case == "outside_manifest":
            proof["binding"].pop("heatpump-optimizer/tests/e2e/test_api_panasonic.py")
        elif case == "manual_inventory":
            self.phase(
                evidence,
                "manual-inventory",
                [],
                self.NODES["migration"] + self.NODES["manual"],
            )
        else:
            self.mutate(evidence, case)
        with pytest.raises(AssertionError, match=message):
            seal_verification_evidence(evidence, proof)
        assert not (evidence / "seal.json").exists()

    async def test_phase_ownership_is_exact_and_unknown_phases_refuse(self):
        assert _verification_phase_owns("obs5-real-migration", self.NODES["obs5-real-migration"][0])
        assert not _verification_phase_owns(
            "obs5-pg-supplement", self.NODES["obs5-real-migration"][0]
        )
        assert all(
            not _verification_phase_owns("ordinary", module + "::test_x")
            for module in COORDINATOR_MODULES
        )
        with pytest.raises(AssertionError, match="unknown verification phase"):
            _verification_phase_owns("ordinary-extra", self.NODES["ordinary"][0])


@pytest.fixture(params=("migration", "metadata"))
async def calendar_database(request):
    owned_test_environment()
    url = make_url(os.environ["DATABASE_URL"])
    head, _ = migration_targets()
    assert head == CURRENT_HEAD_REVISION
    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await guard_owned_database(connection)
                assert (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == head
                await connection.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
                table_name = "manual_cop_observations"
                if request.param == "metadata":
                    table_name = "manual_cop_orm_" + uuid.uuid4().hex
                    table = ManualCOPObservationRecord.__table__.to_metadata(
                        MetaData(), name=table_name
                    )
                    statements = []

                    def capture_statement(
                        sql_connection, cursor, statement, parameters, context, executemany
                    ):
                        statements.append(statement)

                    event.listen(
                        connection.sync_connection, "before_cursor_execute", capture_statement
                    )
                    try:
                        await connection.run_sync(
                            lambda sync_connection: table.create(sync_connection, checkfirst=True)
                        )
                    finally:
                        event.remove(
                            connection.sync_connection, "before_cursor_execute", capture_statement
                        )
                    assert not any("FUNCTION" in statement.upper() for statement in statements)
                assert (
                    await connection.execute(
                        text(f"SELECT to_regprocedure('{HELPER_SIGNATURE}')::oid")
                    )
                ).scalar_one() is None
                yield connection, table_name
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def observation_values(granularity, period_date, timezone):
    _, end = period_bounds(granularity, period_date, timezone)
    return {
        "device_id": "calendar-" + uuid.uuid4().hex,
        "granularity": granularity,
        "period_date": period_date,
        "timezone": timezone,
        "value": 9.7,
        "observed_at": end + dt.timedelta(days=2),
        "completeness": "closed",
    }


async def insert_observation(connection, table_name, values):
    identifier = connection.dialect.identifier_preparer.quote(table_name)
    await connection.execute(
        text(
            f"INSERT INTO {identifier} "
            "(device_id, granularity, period_date, timezone, value, observed_at, completeness) "
            "VALUES (:device_id, :granularity, :period_date, :timezone, :value, "
            ":observed_at, :completeness)"
        ),
        values,
    )


async def insert_legacy_observation(connection, values):
    # Genuine 032-035 schemas still persist the derived boundaries.
    start, end = period_bounds(values["granularity"], values["period_date"], values["timezone"])
    await connection.execute(
        text(
            "INSERT INTO public.manual_cop_observations (device_id, granularity, period_date, "
            "timezone, period_start, period_end, value, observed_at, completeness) VALUES "
            "(:device_id, :granularity, :period_date, :timezone, :period_start, :period_end, "
            ":value, :observed_at, :completeness)"
        ),
        values | {"period_start": start, "period_end": end},
    )


async def stored_observation(connection, table_name, device_id):
    identifier = connection.dialect.identifier_preparer.quote(table_name)
    return (
        await connection.execute(
            text(f"SELECT * FROM {identifier} WHERE device_id=:device_id"),
            {"device_id": device_id},
        )
    ).one()


CURRENT_COLUMNS_SQL = (
    "SELECT a.attname FROM pg_attribute a WHERE a.attrelid=CAST(:table AS regclass) "
    "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"
)
CHECK_CONSTRAINTS_SQL = (
    "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conrelid=CAST(:table AS regclass) AND contype='c'"
)


class TestManualCOPPostgreSQLCalendar:
    async def test_catalog_queries_bind_table_through_regclass_cast(self):
        from sqlalchemy.dialects import postgresql

        for statement in (CURRENT_COLUMNS_SQL, CHECK_CONSTRAINTS_SQL):
            assert "::regclass" not in statement
            clause = text(statement)
            assert set(clause._bindparams) == {"table"}
            compiled = clause.bindparams(table="public.synthetic").compile(
                dialect=postgresql.asyncpg.dialect()
            )
            assert compiled.params == {"table": "public.synthetic"}
            rendered = str(compiled)
            assert "CAST($1" in rendered and "AS regclass)" in rendered
            assert ":table" not in rendered

    async def test_current_schema_stores_local_identity_without_sql_calendar(
        self, calendar_database
    ):
        connection, table_name = calendar_database
        columns = (
            (
                await connection.execute(
                    text(CURRENT_COLUMNS_SQL),
                    {"table": "public." + table_name},
                )
            )
            .scalars()
            .all()
        )
        assert columns == [
            "device_id",
            "granularity",
            "period_date",
            "timezone",
            "value",
            "observed_at",
            "completeness",
            "created_at",
            "updated_at",
            "revision",
        ]
        constraints = dict(
            (
                await connection.execute(
                    text(CHECK_CONSTRAINTS_SQL),
                    {"table": "public." + table_name},
                )
            ).all()
        )
        assert set(constraints) == {
            "ck_manual_cop_granularity",
            "ck_manual_cop_month_start",
            "ck_manual_cop_timezone",
            "ck_manual_cop_value",
            "ck_manual_cop_revision",
            "ck_manual_cop_observed_at",
            "ck_manual_cop_completeness",
            "ck_manual_cop_server_times",
        }
        for name, definition in LOCAL_PERIOD_CONSTRAINTS.items():
            assert constraints[name] == definition
        assert "manual_cop_calendar_midnight" not in " ".join(constraints.values())

    @pytest.mark.parametrize(
        ("granularity", "period_date", "timezone"),
        [
            ("day", dt.date(2025, 11, 2), "America/Havana"),
            ("day", dt.date(2025, 11, 1), "America/Havana"),
            ("month", dt.date(2015, 11, 1), "America/Havana"),
            ("month", dt.date(2015, 10, 1), "America/Havana"),
            ("day", dt.date(1996, 10, 26), "Asia/Colombo"),
            ("day", dt.date(1996, 10, 25), "Asia/Colombo"),
            ("day", dt.date(2010, 3, 5), "Antarctica/Casey"),
            ("day", dt.date(2010, 3, 4), "Antarctica/Casey"),
            ("month", dt.date(1970, 1, 1), "Africa/Monrovia"),
            ("month", dt.date(2020, 12, 1), "UTC"),
            ("day", dt.date(1969, 9, 30), "Pacific/Kwajalein"),
            ("month", dt.date(1969, 9, 1), "Pacific/Kwajalein"),
            ("day", dt.date(1892, 7, 4), "Pacific/Apia"),
            ("day", dt.date(1892, 7, 3), "Pacific/Apia"),
            ("day", dt.date(2020, 10, 3), "UTC"),
            ("day", dt.date(2020, 10, 3), "Europe/Stockholm"),
            ("day", dt.date(2020, 10, 3), "US/Eastern"),
        ],
    )
    async def test_application_derives_earliest_local_midnights_from_stored_identity(
        self, calendar_database, granularity, period_date, timezone
    ):
        connection, table_name = calendar_database
        values = observation_values(granularity, period_date, timezone)
        start, end = period_bounds(granularity, period_date, timezone)
        zone = ZoneInfo(timezone)
        expected_hours = {
            ("day", dt.date(1969, 9, 30), "Pacific/Kwajalein"): 47,
            ("day", dt.date(1892, 7, 4), "Pacific/Apia"): 48,
        }.get((granularity, period_date, timezone))
        if expected_hours is not None:
            assert (end - start).total_seconds() == expected_hours * 3600
        for boundary in (start, end):
            local_midnight = boundary.astimezone(zone).replace(tzinfo=None)
            assert local_midnight.time() == dt.time()
            assert local_instants(local_midnight, zone)[0] == boundary
        async with connection.begin_nested():
            await insert_observation(connection, table_name, values)
        stored = await stored_observation(connection, table_name, values["device_id"])
        assert stored.observed_at == values["observed_at"]
        snapshot = snapshot_from_stored(stored)
        assert (snapshot.period_start, snapshot.period_end) == (start, end)
        assert snapshot.observed_at == values["observed_at"]
        assert snapshot.period_date == period_date and snapshot.timezone == timezone

    @pytest.mark.parametrize(
        ("granularity", "period_date", "timezone"),
        [
            ("day", dt.date(2018, 11, 4), "America/Sao_Paulo"),
            ("day", dt.date(2018, 11, 3), "America/Sao_Paulo"),
            ("month", dt.date(2017, 10, 1), "America/Asuncion"),
            ("month", dt.date(2017, 9, 1), "America/Asuncion"),
            ("day", dt.date(2011, 12, 30), "Pacific/Apia"),
            ("day", dt.date(1993, 8, 21), "Pacific/Kwajalein"),
            ("day", dt.date(1993, 8, 20), "Pacific/Kwajalein"),
        ],
    )
    async def test_nonexistent_midnight_rows_are_refused_by_the_application(
        self, calendar_database, granularity, period_date, timezone
    ):
        connection, table_name = calendar_database
        with pytest.raises(ValueError, match="nonexistent local midnight"):
            period_bounds(granularity, period_date, timezone)
        values = {
            "device_id": "nonexistent-" + uuid.uuid4().hex,
            "granularity": granularity,
            "period_date": period_date,
            "timezone": timezone,
            "value": 9.7,
            "observed_at": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            "completeness": "closed",
        }
        async with connection.begin_nested():
            await insert_observation(connection, table_name, values)
        stored = await stored_observation(connection, table_name, values["device_id"])
        with pytest.raises(InvalidStoredPeriod, match="^invalid_stored_period$"):
            snapshot_from_stored(stored)

    @pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
    async def test_sql_rejects_nonpositive_and_nonfinite_values(self, calendar_database, value):
        connection, table_name = calendar_database
        values = observation_values("day", dt.date(2020, 10, 3), "UTC") | {"value": value}
        with pytest.raises(IntegrityError, match="ck_manual_cop_value") as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

    @pytest.mark.parametrize(
        ("changes", "constraint"),
        [
            ({"timezone": ""}, "ck_manual_cop_timezone"),
            ({"completeness": "total"}, "ck_manual_cop_completeness"),
            (
                {"observed_at": dt.datetime(2999, 1, 1, tzinfo=dt.timezone.utc)},
                "ck_manual_cop_observed_at",
            ),
            ({"granularity": "month", "period_date": dt.date(2020, 10, 3)}, "month_start"),
        ],
    )
    async def test_sql_retains_calendar_independent_checks(
        self, calendar_database, changes, constraint
    ):
        connection, table_name = calendar_database
        values = observation_values("day", dt.date(2020, 10, 3), "UTC") | changes
        with pytest.raises(IntegrityError, match=constraint) as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

    @pytest.mark.parametrize(
        "timezone",
        [
            "Invalid/Manual",
            "UTC+03",
            "europe/stockholm",
            "europe/Stockholm",
            "us/Eastern",
            "utc",
            "US/eastern",
        ],
    )
    async def test_unsupported_timezone_strings_are_refused_by_application_authority(
        self, calendar_database, timezone
    ):
        connection, table_name = calendar_database
        with pytest.raises(ValueError, match="Invalid timezone"):
            period_bounds("day", dt.date(2020, 10, 3), timezone)
        values = {
            "device_id": "non-iana-" + uuid.uuid4().hex,
            "granularity": "day",
            "period_date": dt.date(2020, 10, 3),
            "timezone": timezone,
            "value": 9.7,
            "observed_at": dt.datetime(2020, 10, 5, tzinfo=dt.timezone.utc),
            "completeness": "closed",
        }
        async with connection.begin_nested():
            await insert_observation(connection, table_name, values)
        stored = await stored_observation(connection, table_name, values["device_id"])
        with pytest.raises(InvalidStoredPeriod, match="^invalid_stored_period$"):
            snapshot_from_stored(stored)

    async def test_supported_catalog_keys_load_exactly_in_zoneinfo(self, calendar_database):
        connection, _ = calendar_database
        names = (
            (
                await connection.execute(
                    text(
                        "SELECT name FROM pg_timezone_names "
                        "WHERE name !~ '^(posix|right)/' "
                        "AND name NOT IN ('localtime', 'posixrules') "
                        'ORDER BY name COLLATE "C"'
                    )
                )
            )
            .scalars()
            .all()
        )
        assert names and {"UTC", "Europe/Stockholm", "US/Eastern"}.issubset(names)
        for timezone in names:
            assert ZoneInfo(timezone).key == timezone

    async def test_catalog_pseudozones_are_refused_by_application_authority(
        self, calendar_database
    ):
        connection, _ = calendar_database
        names = (
            (
                await connection.execute(
                    text(
                        "SELECT name FROM pg_timezone_names "
                        "WHERE name ~ '^(posix|right)/' "
                        "OR name IN ('localtime', 'posixrules')"
                    )
                )
            )
            .scalars()
            .all()
        )
        for timezone in names:
            with pytest.raises(ValueError, match="Invalid timezone"):
                period_bounds("day", dt.date(2020, 10, 3), timezone)


MIGRATION_CHILD_PREFIX = "cop_manual_child_"
# Exactly the nullable columns genuine 034 and 035 add on the forward route.
FORWARD_ADDED_COLUMNS = {
    "034": {"plan_actions": (("safety_claim_token", "uuid"),)},
    "035": {
        "consumption": (("source_timezone", "character varying(64)"),),
        "prices": (("interval_schema", "character varying(64)"),),
        "weather": (("units_schema", "character varying(64)"),),
    },
}


MIGRATION_CHILD_IDENTITY_SQL = (
    "SELECT current_database(), current_user, inet_server_port(), "
    "d.oid::bigint, shobj_description(d.oid, 'pg_database') "
    "FROM pg_database d WHERE d.datname = current_database()"
)


async def _migration_child_identity(connection):
    return tuple((await connection.execute(text(MIGRATION_CHILD_IDENTITY_SQL))).one())


async def _parent_relations(connection):
    return tuple(
        (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relkind::text FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' ORDER BY 1, 2"
                )
            )
        ).all()
    )


def _migration_child_parent_identity(receipt):
    """Parent ownership facts a child is bound to; any later difference refuses it."""
    return {
        "provider": receipt["provider"],
        "lease": receipt.get("lease_id") or f"{receipt['run_id']}-{receipt['run_attempt']}",
        "revision": receipt["revision"],
        "token": receipt["token"],
        "endpoints": receipt["endpoints"],
        "namespace": receipt["namespace"],
        "services": receipt["services"],
        "receipt_sha256": hashlib.sha256(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _require_original_parent(child):
    current = _migration_child_parent_identity(owned_test_environment())
    assert current == child["parent"], "migration child parent identity changed"


class GuardedChildEngine:
    """The only migration-child database surface handed to tests and fixtures.

    Every connection first re-proves the originally approved parent receipt and
    services, then proves on that same connection the child's name, user, port, OID
    and binding before any caller SQL can run.
    """

    def __init__(self, engine, record, expected_identity):
        self._engine = engine
        self._record = record
        self._expected = expected_identity

    @property
    def expected_identity(self):
        return self._expected

    def expecting(self, identity):
        """Same child connection source bound to a different expected identity."""
        return GuardedChildEngine(self._engine, self._record, identity)

    @asynccontextmanager
    async def connect(self):
        _require_original_parent(self._record)
        async with self._engine.connect() as connection:
            identity = await _migration_child_identity(connection)
            # End the identity read so callers can begin their own transaction.
            await connection.rollback()
            assert identity == self._expected, "migration child identity mismatch"
            _require_original_parent(self._record)
            yield connection

    @asynccontextmanager
    async def begin(self):
        async with self.connect() as connection:
            async with connection.begin():
                yield connection


def _migration_child_alembic_invocation(operation, target=None):
    """Validated child Alembic call, refused before any worker or settings change."""
    if operation == "check":
        assert target is None, "unsupported migration child Alembic check target"
        return command.check, (Config("alembic.ini"),)
    assert operation in {"upgrade", "downgrade"}, "unsupported migration child Alembic operation"
    assert isinstance(target, str) and target, "unsupported migration child Alembic target"
    return getattr(command, operation), (Config("alembic.ini"), target)


async def _release_retained_blockers(workers, retained):
    """Close resources retained for a child user only once every child user has settled."""
    if all(worker.done() for worker in workers):
        while retained:
            await retained.pop().aclose()


MIGRATION_CHILD_POLLS = 100
MIGRATION_CHILD_POLL_SECONDS = 0.1
MIGRATION_CHILD_DIAGNOSTICS_VERSION = 1
MIGRATION_CHILD_DIAGNOSTIC_ROWS = 64
MIGRATION_CHILD_DIAGNOSTIC_IDENTITY_SQL = (
    "SELECT current_database(), current_user, inet_server_port(), d.oid::bigint, "
    "d.datdba::bigint, pg_get_userbyid(d.datdba)::text, "
    "shobj_description(d.oid, 'pg_database'), current_setting('server_version'), "
    "current_setting('server_version_num')::integer, "
    "(SELECT COALESCE(json_agg(json_build_array(e.extname, e.extversion) "
    "ORDER BY e.extname), '[]'::json)::text FROM pg_extension e) "
    "FROM pg_database d WHERE d.datname = :name"
)
# Allowlisted columns of the child's sessions only: never query text, credentials or
# sessions of any other database.
MIGRATION_CHILD_ACTIVITY_SQL = (
    "SELECT count(*) OVER (), a.pid, a.datid::bigint, a.datname::text, a.usesysid::bigint, "
    "a.usename::text, a.backend_type, a.application_name, host(a.client_addr), "
    "a.client_port, a.backend_start, a.xact_start, a.query_start, a.state_change, "
    "a.state, a.wait_event_type, a.wait_event FROM pg_stat_activity a "
    "WHERE a.datid = CAST(:child_oid AS oid) ORDER BY a.pid "
    f"LIMIT {MIGRATION_CHILD_DIAGNOSTIC_ROWS}"
)
MIGRATION_CHILD_ACTIVITY_FIELDS = (
    ("pid", "int"),
    ("datid", "int"),
    ("datname", "label"),
    ("usesysid", "int"),
    ("usename", "label"),
    ("backend_type", "label"),
    ("application_name", "label"),
    ("client_addr", "address"),
    ("client_port", "int"),
    ("backend_start", "time"),
    ("xact_start", "time"),
    ("query_start", "time"),
    ("state_change", "time"),
    ("state", "label"),
    ("wait_event_type", "event_type"),
    ("wait_event", "event"),
)
MIGRATION_CHILD_KNOWN_LABELS = frozenset(
    {
        "",
        "heatpump",
        "postgres",
        "client backend",
        "autovacuum launcher",
        "autovacuum worker",
        "background worker",
        "logical replication launcher",
        "logical replication worker",
        "parallel worker",
        "walsender",
        "TimescaleDB Background Worker Launcher",
        "TimescaleDB Background Worker Scheduler",
        "active",
        "idle",
        "idle in transaction",
        "idle in transaction (aborted)",
        "fastpath function call",
        "disabled",
    }
)
# Structured SQLSTATEs that mean the statement or session was cancelled or timed out:
# connection exceptions, operator intervention (57014 query_canceled, shutdowns) and timeouts.
MIGRATION_CHILD_TERMINAL_SQLSTATE_CLASSES = frozenset({"08", "57"})
MIGRATION_CHILD_TERMINAL_SQLSTATES = frozenset({"25P03", "55P03"})
# Exact recognized names only; anything else, including a known name with extra text, is hashed.
MIGRATION_CHILD_KNOWN_EXTENSIONS = frozenset({"plpgsql", "timescaledb"})
MIGRATION_CHILD_WAIT_EVENT_TYPES = frozenset(
    {"Activity", "BufferPin", "Client", "Extension", "IO", "IPC", "LWLock", "Lock", "Timeout"}
)
MIGRATION_CHILD_WAIT_EVENTS = frozenset(
    {
        # Client
        "ClientRead",
        "ClientWrite",
        "SSLOpenServer",
        # Activity (including the main loops of background workers)
        "AutoVacuumMain",
        "BgWriterHibernate",
        "BgWriterMain",
        "CheckpointerMain",
        "LogicalLauncherMain",
        "WalWriterMain",
        # Lock
        "advisory",
        "extend",
        "object",
        "page",
        "relation",
        "transactionid",
        "tuple",
        "userlock",
        "virtualxid",
        # LWLock / IO / IPC / Timeout commonly seen around DROP DATABASE
        "BufferContent",
        "BufferMapping",
        "LockManager",
        "ProcArray",
        "DataFileRead",
        "DataFileWrite",
        "WALSync",
        "WALWrite",
        "BgWorkerShutdown",
        "BgWorkerStartup",
        "CheckpointDone",
        "CheckpointStart",
        "ProcSignalBarrier",
        "PgSleep",
        "SpinDelay",
        "VacuumDelay",
        # Extensions such as TimescaleDB background workers report this generic event.
        "Extension",
    }
)


def _diagnostic_digest(value):
    return "redacted:sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _diagnostic_value(kind, value, known=MIGRATION_CHILD_KNOWN_LABELS):
    """Allowlisted serialization: unknown free-form text becomes a stable digest."""
    if value is None:
        return "unknown"
    if kind == "int":
        return value if type(value) is int else _diagnostic_digest(value)
    if kind == "time":
        return value.isoformat() if isinstance(value, dt.datetime) else _diagnostic_digest(value)
    if kind == "address":
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            return _diagnostic_digest(value)
        return (
            str(address) if address.is_loopback or address.is_private else _diagnostic_digest(value)
        )
    if kind == "release":
        # Only the leading numeric release is parsed out; the remaining text is never reported.
        match = isinstance(value, str) and re.match(
            r"[0-9]+(?:\.[0-9]+){0,2}(?![0-9A-Za-z.])", value
        )
        return match.group(0) if match else "unknown"
    if kind == "version":
        allowed = isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value)
    else:
        allowlist = {
            "extension": MIGRATION_CHILD_KNOWN_EXTENSIONS,
            "event_type": MIGRATION_CHILD_WAIT_EVENT_TYPES,
            "event": MIGRATION_CHILD_WAIT_EVENTS,
        }.get(kind, known)
        allowed = isinstance(value, str) and value in allowlist
    return value if allowed else _diagnostic_digest(value)


def _migration_child_parent_sql(child):
    """The parent database, role and port every cleanup connection must report."""
    return (
        "heatpump_test",
        "heatpump",
        5432 if child["parent"]["provider"] == "github" else 5433,
    )


def _diagnostic_identity(row, child):
    """Parent-scoped identity facts; the binding comment is reported only as match and hash."""
    if row is None:
        return {"owned": False, "child": "missing"}
    parent_sql = _migration_child_parent_sql(child)
    try:
        pairs = json.loads(row[9])
    except (TypeError, ValueError):
        pairs = None
    extensions = "unknown"
    if isinstance(pairs, list) and all(isinstance(p, list) and len(p) == 2 for p in pairs):
        extensions = [
            [_diagnostic_value("extension", name), _diagnostic_value("version", version)]
            for name, version in pairs
        ]
    return {
        "owned": tuple(row[:3]) == parent_sql
        and row[3] == child["oid"]
        and row[6] == child["binding"],
        "parent_sql_matches": tuple(row[:3]) == parent_sql,
        "child_oid": _diagnostic_value("int", row[3]),
        "owner_oid": _diagnostic_value("int", row[4]),
        "owner": _diagnostic_value("label", row[5]),
        "comment_matches": row[6] == child["binding"],
        "comment_sha256": (
            "unknown" if row[6] is None else hashlib.sha256(row[6].encode("utf-8")).hexdigest()
        ),
        "parent_server_version": _diagnostic_value("release", row[7]),
        "parent_server_version_text": _diagnostic_value("version", row[7]),
        "parent_server_version_num": _diagnostic_value("int", row[8]),
        "parent_extensions": extensions,
        "child_extensions": "unknown",
    }


def _diagnostic_activity(row, child):
    known = MIGRATION_CHILD_KNOWN_LABELS | {child["database"]}
    return {
        name: _diagnostic_value(kind, value, known)
        for (name, kind), value in zip(MIGRATION_CHILD_ACTIVITY_FIELDS, row, strict=True)
    }


def _diagnostic_connection_probe(connection):
    """Capture the healthy raw references now; never revalidate (reconnect) after an error."""
    fairy = connection.sync_connection._dbapi_connection
    driver = fairy.driver_connection

    def usable():
        return (
            not connection.invalidated
            and not connection.closed
            and connection.sync_connection._dbapi_connection is fairy
            and fairy.driver_connection is driver
            and not driver.is_closed()
            and not driver.is_in_transaction()
        )

    return usable


async def _capture_migration_child_diagnostics(
    connection, child, diagnostics, stage, poll, count, usable
):
    """At most three child-scoped SELECTs of evidence; never authorizes DROP or termination.

    A server-returned SELECT error on a still usable AUTOCOMMIT connection is recorded and
    polling continues; any other failure, including cancellation, is terminal. Returns the
    raw captured rows (None when not captured); they only gate whether the independent
    scheduler admission recheck runs at all.
    """
    snapshot = {
        "stage": stage,
        "poll": poll,
        "count": count,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "queries": 0,
        "status": "incomplete",
    }
    diagnostics["snapshots"].append(snapshot)
    parameters = {"name": child["database"]}
    try:
        _require_original_parent(child)
        snapshot["queries"] += 1
        before = (
            await connection.execute(text(MIGRATION_CHILD_DIAGNOSTIC_IDENTITY_SQL), parameters)
        ).one_or_none()
        snapshot["identity"] = _diagnostic_identity(before, child)
        if not snapshot["identity"]["owned"]:
            snapshot["status"] = "identity_drift"
            raise AssertionError("migration child ownership uncertain")
        snapshot["queries"] += 1
        rows = (
            await connection.execute(
                text(MIGRATION_CHILD_ACTIVITY_SQL), {"child_oid": child["oid"]}
            )
        ).all()
        snapshot["queries"] += 1
        after = (
            await connection.execute(text(MIGRATION_CHILD_DIAGNOSTIC_IDENTITY_SQL), parameters)
        ).one_or_none()
        _require_original_parent(child)
        if after != before:
            snapshot["status"] = "identity_drift"
            raise AssertionError("migration child ownership uncertain")
        rows = rows[:MIGRATION_CHILD_DIAGNOSTIC_ROWS]
        snapshot["total"] = _diagnostic_value("int", rows[0][0]) if rows else 0
        snapshot["truncated"] = not isinstance(snapshot["total"], int) or snapshot["total"] > len(
            rows
        )
        snapshot["activity"] = [_diagnostic_activity(row[1:], child) for row in rows]
        snapshot["status"] = "captured"
        return rows
    except BaseException as error:
        snapshot["error"] = type(error).__name__
        if snapshot["status"] == "incomplete":
            snapshot["status"] = "failed"
        sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
        if not (isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)):
            sqlstate = None
        snapshot["sqlstate"] = sqlstate or "unknown"
        # Only a structured, ordinary server error on a still usable connection is recoverable;
        # unstructured, cancellation and timeout outcomes are terminal.
        if (
            isinstance(error, DBAPIError)
            and not error.connection_invalidated
            and sqlstate is not None
            and sqlstate[:2] not in MIGRATION_CHILD_TERMINAL_SQLSTATE_CLASSES
            and sqlstate not in MIGRATION_CHILD_TERMINAL_SQLSTATES
            and snapshot["status"] == "failed"
            and usable()
        ):
            snapshot["terminal"] = False
            return
        snapshot["terminal"] = True
        raise


async def _exit_migration_child_connection(context, primary, diagnostics):
    """Run the connection's own context exit exactly once and drain it to completion.

    No new deadline: the existing process phase deadline stays authoritative. Caller
    cancellations are remembered, never forwarded to the exit, and never end the drain.
    """
    arguments = (None, None, None)
    if primary is not None:
        arguments = (type(primary), primary, primary.__traceback__)
    exiting = asyncio.ensure_future(context.__aexit__(*arguments))
    cancelled = None
    while not exiting.done():
        try:
            # asyncio.wait shields the exit: cancelling this wait never cancels the exit task.
            await asyncio.wait({exiting})
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    state = {"cancelled": cancelled is not None}
    if exiting.cancelled() or exiting.exception() is not None:
        failure = asyncio.CancelledError() if exiting.cancelled() else exiting.exception()
        state["status"] = "failed"
        state["error"] = type(failure).__name__
    else:
        state["status"] = "closed"
        failure = cancelled
    diagnostics["connection_exit"] = state
    if failure is None:
        return
    if primary is not None:
        # The primary cause stays primary; the exit outcome is recorded alongside it.
        primary.add_note(f"migration child connection exit: {state}")
        return
    if cancelled is not None and failure is not cancelled:
        # The caller's cancellation arrived first and stays primary; the later exit failure
        # keeps its exact identity as the explicit cause.
        cancelled.add_note(f"migration child connection exit then failed: {state}")
        raise cancelled from failure
    raise failure


# TimescaleDB 2.26.4's loader intercepts an ordinary DROP DATABASE before it executes and stops
# that database's scheduler (src/loader/loader.c 416-432, 523-533; bgw_launcher.c 686-715).
# PostgreSQL 16.13 then still counts every remaining backend under the exclusive database lock
# and refuses after about five seconds (dbcommands.c 1583-1705, procarray.c 3699-3763): the
# server, not this classification, makes the final decision. backend_type comes from the
# server's worker registration (pgstatfuncs.c 551-564); application_name is never authority.
MIGRATION_CHILD_SCHEDULER = "TimescaleDB Background Worker Scheduler"
MIGRATION_CHILD_SCHEDULER_SERVER_VERSION_NUM = 160013
MIGRATION_CHILD_SCHEDULER_TIMESCALEDB = "2.26.4"
MIGRATION_CHILD_SCHEDULER_PROFILE_SQL = (
    "SELECT current_database(), current_user, inet_server_port(), d.oid::bigint, "
    "shobj_description(d.oid, 'pg_database'), d.datdba::bigint, "
    "pg_get_userbyid(d.datdba)::text, current_setting('server_version_num')::integer, "
    "(SELECT e.extversion FROM pg_extension e WHERE e.extname = 'timescaledb'), "
    "current_setting('shared_preload_libraries') "
    "FROM pg_database d WHERE d.datname = :name"
)
# Raw, complete, child-scoped facts: two rows suffice to prove the total is exactly one.
MIGRATION_CHILD_SCHEDULER_ACTIVITY_SQL = (
    "SELECT count(*) OVER (), a.datid::bigint, a.datname::text, a.usesysid::bigint, "
    "a.usename::text, a.backend_type, a.state, host(a.client_addr), a.client_port, "
    "a.xact_start FROM pg_stat_activity a "
    "WHERE a.datid = CAST(:child_oid AS oid) ORDER BY a.pid LIMIT 2"
)
MIGRATION_CHILD_SCHEDULER_RESIDUE = (
    "the DROP hook may have stopped and disabled the owned child's Timescale scheduler; "
    "the child is retained and is not retried, re-enabled or adopted"
)


def _is_scheduler_candidate(count, rows):
    """Gate only: whether raw capture rows justify running the admission recheck at all."""
    return (
        count == 1
        and rows is not None
        and len(rows) == 1
        and rows[0][0] == 1
        and rows[0][6] == MIGRATION_CHILD_SCHEDULER
    )


def _preloads_timescaledb(value):
    """Exact `timescaledb` token in a plain comma list; any other representation refuses."""
    if not isinstance(value, str):
        return False
    tokens = [token.strip() for token in value.split(",")]
    return all(re.fullmatch(r"[A-Za-z0-9_]+", token) for token in tokens) and (
        "timescaledb" in tokens
    )


def _owned_scheduler_profile(row, child):
    return (
        row is not None
        and tuple(row[:3]) == _migration_child_parent_sql(child)
        and row[3] == child["oid"]
        and row[4] == child["binding"]
    )


def _scheduler_admission_facts(profile, sessions, child, diagnostics):
    """Every fact must be True; raw SQL NULL is None, never the diagnostics' "unknown"."""
    total = sessions[0][0] if sessions else 0
    single = type(total) is int and total == 1 and len(sessions) == 1
    facts = {
        "complete": (total == 0 and not sessions) or single,
        "server_version_num": type(profile[7]) is int
        and profile[7] == MIGRATION_CHILD_SCHEDULER_SERVER_VERSION_NUM,
        "timescaledb_version": profile[8] == MIGRATION_CHILD_SCHEDULER_TIMESCALEDB,
        "preload_timescaledb": _preloads_timescaledb(profile[9]),
        "workers_settled": all(worker["done"] for worker in diagnostics["workers"]),
        "no_retained_resources": diagnostics["retained_resources"] == 0,
    }
    if single:
        _total, datid, datname, usesysid, usename, backend_type, state, *client, xact = sessions[0]
        facts |= {
            "child_scoped": datid == child["oid"] and datname == child["database"],
            "scheduler": backend_type == MIGRATION_CHILD_SCHEDULER,
            "owner": usename == "heatpump"
            and profile[6] == "heatpump"
            and type(usesysid) is int
            and usesysid == profile[5],
            "idle": state == "idle",
            "no_client": client == [None, None],
            "no_transaction": xact is None,
        }
    return facts


async def _admit_migration_child_scheduler(connection, child, diagnostics, stage, count, rows):
    """Recheck, immediately before DROP, that at most the child's own scheduler remains.

    Uses fresh raw server rows only, never the redacted diagnostics. Unqualified sessions
    return False and keep the existing bounded refusal; ownership drift or any error is
    terminal. Never terminates a session, uses FORCE, reconnects or changes configuration.
    """
    if not _is_scheduler_candidate(count, rows):
        return False
    attempt = {"stage": stage, "queries": 0, "eligible": False}
    diagnostics.setdefault("scheduler_admissions", []).append(attempt)
    parameters = {"name": child["database"]}
    try:
        _require_original_parent(child)
        attempt["queries"] += 1
        before = (
            await connection.execute(text(MIGRATION_CHILD_SCHEDULER_PROFILE_SQL), parameters)
        ).one_or_none()
        if not _owned_scheduler_profile(before, child):
            raise AssertionError("migration child ownership uncertain")
        attempt["queries"] += 1
        sessions = (
            await connection.execute(
                text(MIGRATION_CHILD_SCHEDULER_ACTIVITY_SQL), {"child_oid": child["oid"]}
            )
        ).all()
        attempt["queries"] += 1
        after = (
            await connection.execute(text(MIGRATION_CHILD_SCHEDULER_PROFILE_SQL), parameters)
        ).one_or_none()
        _require_original_parent(child)
        if after != before:
            raise AssertionError("migration child ownership uncertain")
    except BaseException as error:
        attempt["error"] = type(error).__name__
        attempt["terminal"] = True
        raise
    facts = _scheduler_admission_facts(before, sessions, child, diagnostics)
    attempt["sessions"] = len(sessions) if facts["complete"] else "unknown"
    attempt["facts"] = facts
    attempt["eligible"] = all(facts.values())
    return attempt["eligible"]


async def _drain_and_drop_migration_child(connection, child, parent_before, diagnostics):
    """Ordinary DROP once no session, or only the qualified owned scheduler, remains."""
    usable = None
    admitted = False
    for attempt in range(MIGRATION_CHILD_POLLS):
        current = (
            await connection.execute(
                text(
                    "SELECT d.oid::bigint, shobj_description(d.oid, 'pg_database'), "
                    "(SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid) "
                    "FROM pg_database d WHERE d.datname = :name"
                ),
                {"name": child["database"]},
            )
        ).one_or_none()
        assert current is not None, "migration child disappeared"
        assert (current[0], current[1]) == (child["oid"], child["binding"]), (
            "migration child ownership uncertain"
        )
        if current[2] == 0:
            break
        if usable is None:
            usable = _diagnostic_connection_probe(connection)
            rows = await _capture_migration_child_diagnostics(
                connection, child, diagnostics, "first_nonzero", attempt, current[2], usable
            )
            admitted = await _admit_migration_child_scheduler(
                connection, child, diagnostics, "first_nonzero", current[2], rows
            )
            if admitted:
                break
        await asyncio.sleep(MIGRATION_CHILD_POLL_SECONDS)
    else:
        # Created before collection: no collector, exit or receipt failure replaces it.
        refusal = AssertionError("migration child still has sessions")
        try:
            rows = await _capture_migration_child_diagnostics(
                connection, child, diagnostics, "final_refusal", attempt, current[2], usable
            )
            admitted = await _admit_migration_child_scheduler(
                connection, child, diagnostics, "final_refusal", current[2], rows
            )
        except BaseException as failure:
            raise refusal from failure
        if not admitted:
            raise refusal
    try:
        await connection.execute(text(f'DROP DATABASE "{child["database"]}"'))
    except BaseException as error:
        if admitted:
            # Exactly one DROP: its failure or uncertainty is final and reported, never retried.
            admission = diagnostics["scheduler_admissions"][-1]
            admission["drop"] = "not_confirmed"
            admission["drop_error"] = type(error).__name__
            admission["residue"] = MIGRATION_CHILD_SCHEDULER_RESIDUE
            error.add_note(f"scheduler-admitted DROP not confirmed: {admission['residue']}")
        raise
    if admitted:
        diagnostics["scheduler_admissions"][-1]["drop"] = "completed"
    assert await _parent_relations(connection) == parent_before, "parent state changed"


async def _drop_migration_child(parent, child, workers, parent_before, retained_count):
    diagnostics = {
        "version": MIGRATION_CHILD_DIAGNOSTICS_VERSION,
        "workers": [],
        "retained_resources": retained_count,
        "snapshots": [],
    }
    try:
        diagnostics["workers"] = [
            {
                "index": index,
                "done": worker.done(),
                "cancelled": worker.done() and worker.cancelled(),
            }
            for index, worker in enumerate(workers)
        ]
        assert child["oid"] is not None, "migration child OID was not recorded"
        assert all(worker.done() for worker in workers), "migration worker did not settle"
        _require_original_parent(child)
        context = parent.connect()
        connection = await context.__aenter__()
        primary = None
        try:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await guard_owned_database(connection)
            _require_original_parent(child)
            await _drain_and_drop_migration_child(connection, child, parent_before, diagnostics)
        except BaseException as error:
            primary = error
            raise
        finally:
            await _exit_migration_child_connection(context, primary, diagnostics)
    except BaseException as error:
        receipt = "migration-child-cleanup-" + child["database"] + ".json"
        try:
            (Path(os.environ["COP_MANUAL_TEST_EVIDENCE"]) / receipt).write_text(
                json.dumps(child | {"error": type(error).__name__, "diagnostics": diagnostics}),
                encoding="utf-8",
            )
        except Exception as failure:
            # The receipt is evidence only; its failure never replaces the refusal's cause.
            error.add_note(f"migration child cleanup receipt not written: {type(failure).__name__}")
            receipt += " (unwritten)"
        raise AssertionError(f"migration child cleanup refused; receipt {receipt}") from error
    admissions = diagnostics.get("scheduler_admissions", ())
    if any(admission.get("drop") == "completed" for admission in admissions):
        receipt = "migration-child-cleanup-" + child["database"] + ".json"
        try:
            (Path(os.environ["COP_MANUAL_TEST_EVIDENCE"]) / receipt).write_text(
                json.dumps(
                    child | {"outcome": "scheduler_admitted_drop", "diagnostics": diagnostics}
                ),
                encoding="utf-8",
            )
        except Exception as failure:
            # The DROP already completed: qualification fails, nothing is dropped again.
            raise AssertionError(
                f"migration child dropped; admission receipt {receipt} not written"
            ) from failure


@asynccontextmanager
async def owned_migration_child(purpose):
    """Own one new disposable migration database bound to the parent receipt; drop only it."""
    from packages.core.config import settings

    receipt = owned_test_environment()
    assert re.fullmatch(r"[a-z]{1,16}", purpose), "invalid migration child purpose"
    parent_url = make_url(os.environ["DATABASE_URL"])
    assert parent_url == make_url(TEST_DATABASE_URL), "migration child parent is not owned"
    name = f"{MIGRATION_CHILD_PREFIX}{purpose}_{uuid.uuid4().hex}"
    # The child is bound to the parent's service IDs, namespace and exact receipt as
    # first approved, not merely to whatever receipt is current later.
    parent_identity = _migration_child_parent_identity(receipt)
    binding = json.dumps({"database": name, "parent": parent_identity}, sort_keys=True)
    assert "'" not in binding, "unsafe migration child binding"
    child_url = parent_url.set(database=name)
    # Child connections differ from the approved parent URL only by the recorded name.
    assert child_url.set(database=parent_url.database) == parent_url
    expected_port = 5432 if receipt["provider"] == "github" else 5433
    record = {"database": name, "oid": None, "binding": binding, "parent": parent_identity}
    previous_url = settings.database_url
    parent = create_async_engine(
        parent_url, poolclass=NullPool, connect_args={"command_timeout": 20}
    )
    child_engine = None
    created = False
    workers = []
    retained = []
    parent_before = None
    try:
        async with parent.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await guard_owned_database(connection)
            _require_original_parent(record)
            parent_before = await _parent_relations(connection)
            assert (
                await connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                )
            ).first() is None, "migration child name is already allocated"
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
            record["oid"] = (
                await connection.execute(
                    text("SELECT oid::bigint FROM pg_database WHERE datname = :name"),
                    {"name": name},
                )
            ).scalar_one()
            await connection.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{binding}'"))
        expected_identity = (name, "heatpump", expected_port, record["oid"], binding)
        child_engine = create_async_engine(
            child_url, poolclass=NullPool, connect_args={"command_timeout": 20}
        )
        guarded = GuardedChildEngine(child_engine, record, expected_identity)

        async def verify():
            async with guarded.connect():
                pass

        async def alembic(operation, target=None):
            function, arguments = _migration_child_alembic_invocation(operation, target)
            await verify()
            settings.database_url = child_url.render_as_string(hide_password=False)
            worker = asyncio.ensure_future(asyncio.to_thread(function, *arguments))
            workers.append(worker)
            try:
                await asyncio.shield(worker)
            finally:
                # Cancelling the await does not stop the thread: drain before restoring.
                if not worker.done():
                    await asyncio.wait({worker}, timeout=60)
                if worker.done():
                    settings.database_url = previous_url

        @asynccontextmanager
        async def session_context():
            async with (
                guarded.connect() as connection,
                AsyncSession(bind=connection, expire_on_commit=False) as session,
            ):
                try:
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        def retain(task, resources):
            # An unsettled reset keeps its blocker: teardown neither releases nor drops.
            workers.append(task)
            retained.append(resources)

        yield SimpleNamespace(
            engine=guarded,
            name=name,
            oid=record["oid"],
            alembic=alembic,
            verify=verify,
            session_context=session_context,
            retain=retain,
        )
    finally:
        try:
            unsettled = [worker for worker in workers if not worker.done()]
            if unsettled:
                await asyncio.wait(unsettled, timeout=60)
            settings.database_url = previous_url
            await _release_retained_blockers(workers, retained)
            if child_engine is not None:
                await child_engine.dispose()
            if created:
                await _drop_migration_child(parent, record, workers, parent_before, len(retained))
        finally:
            await parent.dispose()


@pytest.fixture
async def forward_manual_database():
    async with owned_migration_child("forward") as child:
        yield child


def canonical_rows(rows):
    return tuple(sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)))


async def migration_child_snapshot(connection):
    """Capture every public table's columns and rows plus every sequence state."""
    tables = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
                    "AND c.relkind IN ('r', 'p') AND c.relname <> 'alembic_version' "
                    "ORDER BY c.relname"
                )
            )
        )
        .scalars()
        .all()
    )
    snapshot = {
        "marker": (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one(),
        "columns": {},
        "rows": {},
        "sequences": {},
    }
    for table in tables:
        identifier = connection.dialect.identifier_preparer.quote(table)
        snapshot["columns"][table] = tuple(
            tuple(column)
            for column in (
                await connection.execute(
                    text(
                        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) "
                        "FROM pg_attribute a WHERE a.attrelid = to_regclass(:name) "
                        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
                    ),
                    {"name": "public." + identifier},
                )
            ).all()
        )
        snapshot["rows"][table] = canonical_rows(
            json.loads(value)
            for value in (
                await connection.execute(
                    text(f"SELECT to_jsonb(record)::text FROM public.{identifier} record")
                )
            )
            .scalars()
            .all()
        )
    names = (
        (
            await connection.execute(
                text(
                    "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' "
                    "ORDER BY sequencename"
                )
            )
        )
        .scalars()
        .all()
    )
    for name in names:
        identifier = connection.dialect.identifier_preparer.quote(name)
        snapshot["sequences"][name] = tuple(
            (
                await connection.execute(
                    text(f"SELECT last_value, is_called FROM public.{identifier}")
                )
            ).one()
        )
    snapshot["sequence_configuration"] = tuple(
        tuple(row)
        for row in (
            await connection.execute(
                text(
                    "SELECT sequencename, sequenceowner, data_type::text, start_value, "
                    "min_value, max_value, increment_by, cycle, cache_size FROM pg_sequences "
                    "WHERE schemaname = 'public' ORDER BY sequencename"
                )
            )
        ).all()
    )
    return snapshot


def expected_forward_additions(snapshot, revision):
    expected = copy.deepcopy(snapshot) | {"marker": revision}
    for table, added in FORWARD_ADDED_COLUMNS[revision].items():
        expected["columns"][table] = (*expected["columns"][table], *added)
        expected["rows"][table] = canonical_rows(
            row | {name: None for name, _sql_type in added} for row in expected["rows"][table]
        )
    return expected


def expected_local_periods(snapshot):
    expected = copy.deepcopy(snapshot) | {"marker": LOCAL_PERIOD_REVISION}
    table = "manual_cop_observations"
    expected["columns"][table] = tuple(
        column for column in expected["columns"][table] if column[0] not in LEGACY_BOUNDARY_COLUMNS
    )
    expected["rows"][table] = canonical_rows(
        {key: value for key, value in row.items() if key not in LEGACY_BOUNDARY_COLUMNS}
        for row in expected["rows"][table]
    )
    return expected


async def seed_forward_records(connection):
    """Seed genuine pre-036 authority, restore links and manual entries via explicit columns."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    devices = tuple(f"forward-{side}-{uuid.uuid4().hex}" for side in ("a", "b"))

    async def returning(statement, values):
        return (await connection.execute(text(statement), values)).scalar_one()

    plan_id = await returning(
        "INSERT INTO plans (horizon_start,horizon_end,plan_json,optimizer_version,status) "
        "VALUES (:start,:end,'{}','forward-fixture','superseded') RETURNING id",
        {"start": now - dt.timedelta(hours=3), "end": now + dt.timedelta(hours=3)},
    )
    action = (
        "INSERT INTO plan_actions (plan_id,scheduled_ts,action_type,payload_json,device_id,"
        "status,executed_at,reverts_action_id,safety_attempt_count,safety_next_retry_at,"
        "safety_claimed_at) VALUES (:plan,:scheduled,:type,:payload,:device,:status,"
        ":executed,:source,:attempts,:retry,:claimed) RETURNING id"
    )
    pairs = (
        ("force_dhw_on", "force_dhw_off", "{}", "{}", "pending", None),
        (
            "zone_temp_boost",
            "zone_temp_restore",
            '{"zone_id":1,"baseline_temperature":35,"temperature":37}',
            '{"zone_id":1,"temperature":35}',
            "executing",
            now - dt.timedelta(minutes=1),
        ),
        (
            "zone_temp_boost",
            "zone_temp_restore",
            '{"zone_id":2,"baseline_temperature":30,"temperature":32}',
            '{"zone_id":2,"temperature":30}',
            "dispatched",
            None,
        ),
    )
    links = {}
    for index, (source_type, restore_type, source_payload, payload, status, claimed) in enumerate(
        pairs
    ):
        scheduled = now - dt.timedelta(hours=2, minutes=index)
        source = await returning(
            action,
            {
                "plan": plan_id,
                "scheduled": scheduled,
                "type": source_type,
                "payload": source_payload,
                "device": devices[0],
                "status": "executed",
                "executed": scheduled,
                "source": None,
                "attempts": 0,
                "retry": None,
                "claimed": None,
            },
        )
        restore = await returning(
            action,
            {
                "plan": plan_id,
                "scheduled": scheduled + dt.timedelta(hours=1),
                "type": restore_type,
                "payload": payload,
                "device": devices[0],
                "status": status,
                "executed": None,
                "source": source,
                "attempts": index + 1,
                "retry": now + dt.timedelta(minutes=5 + index),
                "claimed": claimed,
            },
        )
        links[restore] = source
    restores = tuple(links)
    await connection.execute(
        text(
            "INSERT INTO shower_events (started_at,pre_shower_temp,status,peak_price_skipped,"
            "device_id,expires_at,activation_action_id) VALUES (:started,42.5,'active',false,"
            ":device,:expires,:source)"
        ),
        {
            "started": now - dt.timedelta(hours=2),
            "device": devices[0],
            "expires": now + dt.timedelta(hours=1),
            "source": links[restores[0]],
        },
    )
    await connection.execute(
        text(
            "INSERT INTO executor_verification_reads (reserved_at,device_id,lane,action_id,"
            "phase,checkpoint_seconds) VALUES (:reserved,:device,'safety',:action,'safety',60)"
        ),
        {"reserved": now - dt.timedelta(minutes=2), "device": devices[0], "action": restores[1]},
    )
    for device in devices:
        await connection.execute(
            text(
                "INSERT INTO device_status (ts,device_id,zone1_target_temp,"
                "native_policy_provenance) VALUES (:ts,:device,35,CAST(:provenance AS jsonb))"
            ),
            {
                "ts": now - dt.timedelta(minutes=30),
                "device": device,
                "provenance": json.dumps({"fixture": "forward", "policy": "native"}),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO consumption (ts,device_id,source_date,heat_kwh,cool_kwh,tank_kwh) "
                "VALUES (:ts,:device,:day,12.5,0,2.25)"
            ),
            {"ts": now - dt.timedelta(days=1), "device": device, "day": (now.date())},
        )
    await connection.execute(
        text(
            "INSERT INTO settings (key,value) VALUES ('manual_cop_monitoring_enabled','true'),"
            "('optimizer_layer','rules_only') ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO audit_log (actor,action,target_device,payload_json,result) "
            "VALUES ('fixture','forward_seed',:device,'{}','ok')"
        ),
        {"device": devices[0]},
    )
    month = (now - dt.timedelta(days=62)).date().replace(day=1)
    manual = (
        (devices[0], "day", (now - dt.timedelta(days=3)).date(), "Europe/Stockholm"),
        (devices[0], "month", month, "America/Havana"),
        (devices[1], "day", (now - dt.timedelta(days=4)).date(), "Europe/Stockholm"),
    )
    for device, granularity, period_date, timezone in manual:
        values = observation_values(granularity, period_date, timezone)
        values.update(
            device_id=device, observed_at=period_bounds(granularity, period_date, timezone)[1]
        )
        await insert_legacy_observation(connection, values)
    return SimpleNamespace(devices=devices, links=links, manual=len(manual))


class _DiagnosticDriver:
    def __init__(self):
        self.closed = False
        self.in_transaction = False

    def is_closed(self):
        return self.closed

    def is_in_transaction(self):
        return self.in_transaction


def _diagnostic_statement_kind(sql):
    if sql == MIGRATION_CHILD_SCHEDULER_PROFILE_SQL:
        return "profile"
    if sql == MIGRATION_CHILD_SCHEDULER_ACTIVITY_SQL:
        return "eligibility"
    if sql.startswith("DROP DATABASE"):
        return "drop"
    if "FROM pg_class" in sql:
        return "relations"
    if "server_version_num" in sql:
        return "identity"
    if "count(*) OVER ()" in sql:
        return "activity"
    if "count(*) FROM pg_stat_activity" in sql:
        return "poll"
    raise AssertionError(f"unexpected cleanup SQL: {sql}")


class _DiagnosticParent:
    """Scripted parent engine and connection for cleanup diagnostics; no network at all."""

    def __init__(
        self,
        child,
        counts,
        *,
        rows=(),
        total=None,
        failures=None,
        blocks=None,
        exit_gate=None,
        exit_error=None,
        eligibility=None,
    ):
        self.child = child
        self.counts = list(counts)
        self.rows = list(rows)
        self.total = len(self.rows) if total is None else total
        self.identities = []
        self.profiles = []
        # Raw scheduler-admission result sets; the last one repeats.
        self.eligibility = list(eligibility or [[_scheduler_session(child)]])
        self.failures = dict(failures or {})
        self.blocks = dict(blocks or {})
        self.exit_gate = exit_gate
        self.exit_error = exit_error
        self.statements = []
        self.connects = 0
        self.exits = []
        self.driver = _DiagnosticDriver()
        self.fairy = SimpleNamespace(driver_connection=self.driver)
        self.sync_connection = SimpleNamespace(_dbapi_connection=self.fairy)
        self.invalidated = False
        self.closed = False

    def connect(self):
        self.connects += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.exits.append(exc_info[0])
        if self.exit_gate is not None:
            await self.exit_gate.wait()
        self.closed = True
        if self.exit_error is not None:
            raise self.exit_error

    async def execution_options(self, **options):
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def kinds(self):
        return [kind for kind, _sql, _parameters in self.statements]

    def owned_identity(self):
        return (
            "heatpump_test",
            "heatpump",
            5432,
            self.child["oid"],
            10,
            "heatpump",
            self.child["binding"],
            "16.4 (Debian 16.4-1.pgdg120+2)",
            160004,
            '[["plpgsql", "1.0"], ["timescaledb", "2.17.2"]]',
        )

    def scheduler_profile(self):
        return (
            "heatpump_test",
            "heatpump",
            5432,
            self.child["oid"],
            self.child["binding"],
            SCHEDULER_OWNER_OID,
            "heatpump",
            160013,
            "2.26.4",
            "timescaledb",
        )

    async def execute(self, statement, parameters=None):
        assert not self.closed and not self.invalidated, "SQL on a closed or invalidated connection"
        sql = str(statement)
        kind = _diagnostic_statement_kind(sql)
        self.statements.append((kind, sql, parameters))
        key = (kind, self.kinds().count(kind))
        if key in self.blocks:
            await self.blocks.pop(key).wait()
        if key in self.failures:
            raise self.failures.pop(key)(self)
        if kind == "poll":
            count = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
            rows = [(self.child["oid"], self.child["binding"], count)]
        elif kind == "identity":
            rows = [self.identities.pop(0) if self.identities else self.owned_identity()]
        elif kind == "activity":
            rows = [(self.total, *row) for row in self.rows]
        elif kind == "profile":
            rows = [self.profiles.pop(0) if self.profiles else self.scheduler_profile()]
        elif kind == "eligibility":
            rows = self.eligibility.pop(0) if len(self.eligibility) > 1 else self.eligibility[0]
        else:
            rows = []
        return SimpleNamespace(one_or_none=lambda: rows[0] if rows else None, all=lambda: rows)


def _diagnostic_child():
    name = MIGRATION_CHILD_PREFIX + "diag_" + "0" * 32
    return {
        "database": name,
        "oid": 4242,
        "binding": json.dumps({"database": name, "marker": "binding-text-never-reported"}),
        "parent": {"provider": "github"},
    }


def _diagnostic_rows(child, count):
    start = dt.datetime(2026, 10, 12, 21, 0, tzinfo=dt.timezone.utc)
    return [
        (1000 + index, child["oid"], child["database"], 10, "heatpump", "client backend", "")
        + ("127.0.0.1", 50000 + index, start, None, start, start, "idle", "Client", "ClientRead")
        for index in range(count)
    ]


def _diagnostic_guards(monkeypatch, tmp_path, *, polls, parent_failure_at=None):
    """Replace the receipt/SQL guards with counted fakes; bound the poll loop for speed."""
    monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path))
    monkeypatch.setitem(globals(), "MIGRATION_CHILD_POLLS", polls)
    monkeypatch.setitem(globals(), "MIGRATION_CHILD_POLL_SECONDS", 0)
    calls = []

    async def guard(connection):
        calls.append("guard")

    def parent_check(child):
        calls.append("parent")
        if calls.count("parent") == parent_failure_at:
            raise AssertionError("migration child parent identity changed")

    monkeypatch.setitem(globals(), "guard_owned_database", guard)
    monkeypatch.setitem(globals(), "_require_original_parent", parent_check)
    return calls


def _diagnostic_receipt(tmp_path, child):
    receipt = tmp_path / f"migration-child-cleanup-{child['database']}.json"
    return json.loads(receipt.read_text(encoding="utf-8"))["diagnostics"]


DIAGNOSTIC_CAPTURE = ["identity", "activity", "identity"]
SCHEDULER_ADMISSION = ["profile", "eligibility", "profile"]
# Deliberately not 10: the owner OID is read from the server, never assumed.
SCHEDULER_OWNER_OID = 16385
SCHEDULER_FACTS = (
    "complete",
    "server_version_num",
    "timescaledb_version",
    "preload_timescaledb",
    "workers_settled",
    "no_retained_resources",
    "child_scoped",
    "scheduler",
    "owner",
    "idle",
    "no_client",
    "no_transaction",
)


def _scheduler_session(child, **changes):
    """One raw admission row for the owned child's scheduler; SQL NULL stays None."""
    row = {
        "total": 1,
        "datid": child["oid"],
        "datname": child["database"],
        "usesysid": SCHEDULER_OWNER_OID,
        "usename": "heatpump",
        "backend_type": MIGRATION_CHILD_SCHEDULER,
        "state": "idle",
        "client_addr": None,
        "client_port": None,
        "xact_start": None,
    }
    return tuple((row | changes).values())


def _scheduler_capture_rows(child, backend_type=MIGRATION_CHILD_SCHEDULER):
    """Diagnostic activity rows as observed in CI run 38318192441 (application name spoofable)."""
    start = dt.datetime(2026, 10, 12, 22, 43, tzinfo=dt.timezone.utc)
    return [
        (232, child["oid"], child["database"], SCHEDULER_OWNER_OID, "heatpump", backend_type)
        + (MIGRATION_CHILD_SCHEDULER, None, None, start, None, None, start, "idle")
        + ("Extension", "Extension")
    ]


def _adapted_asyncpg_error(name, *, invalidated=False):
    """The DBAPIError SQLAlchemy's asyncpg dialect actually builds for an asyncpg error class."""
    import asyncpg
    from sqlalchemy.dialects.postgresql.asyncpg import (
        AsyncAdapt_asyncpg_connection,
        AsyncAdapt_asyncpg_dbapi,
    )

    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    adapter = AsyncAdapt_asyncpg_connection.__new__(AsyncAdapt_asyncpg_connection)
    adapter.dbapi = dbapi
    adapter._connection = SimpleNamespace(is_closed=lambda: False)
    try:
        adapter._handle_exception(getattr(asyncpg.exceptions, name)("synthetic"))
    except dbapi.Error as adapted:
        return DBAPIError.instance(
            "SELECT", {}, adapted, dbapi.Error, connection_invalidated=invalidated
        )
    raise AssertionError(f"{name} was not adapted")


class _DiagnosticSuspend:
    def __await__(self):
        yield


class _DiagnosticPendingExit:
    """Scripted connection exit task: pending until settled, every interaction counted."""

    def __init__(self):
        self.settled = False
        self.error = None
        self.cancels = 0
        self.calls = []
        self.waits = 0

    def exit(self, *exc_info):
        self.calls.append(exc_info)
        return "exit-coroutine"

    def ensure_future(self, awaitable):
        assert awaitable == "exit-coroutine"
        return self

    def done(self):
        return self.settled

    def cancelled(self):
        return False

    def exception(self):
        assert self.settled
        return self.error

    def cancel(self):
        self.cancels += 1

    async def wait(self, tasks, timeout=None):
        assert tasks == {self} and timeout is None, "a new exit deadline was introduced"
        self.waits += 1
        await _DiagnosticSuspend()
        return set(), set(tasks)


class TestManualCOPPostgreSQLMigration:
    async def test_data_bearing_031_upgrade_and_flag_rollback_preserve_retained_state(self):
        owned_test_environment()
        url = make_url(os.environ["DATABASE_URL"])
        head, predecessor = migration_targets()
        config = Config("alembic.ini")
        engine = create_async_engine(url, poolclass=NullPool, echo=False)
        models = (
            DeviceStatusRecord,
            ConsumptionRecord,
            COPRecord,
            SpaceHeatingGateRecord,
            PlanRecord,
            PlanActionRecord,
            ExecutorVerificationReadRecord,
            OptimizationRequestRecord,
        )
        try:
            async with engine.connect() as connection:
                await guard_owned_database(connection, require_empty=True)
            for operation, target in (
                (command.upgrade, predecessor),
                (command.downgrade, "020"),
                (command.upgrade, predecessor),
            ):
                await asyncio.to_thread(operation, config, target)
            async with engine.begin() as connection:
                await guard_owned_database(connection)
                assert (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == predecessor
                stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(
                    days=2
                )
                device = "preservation-" + uuid.uuid4().hex
                await connection.execute(
                    insert(DeviceStatusRecord).values(
                        ts=stamp,
                        device_id=device,
                        mode="HEATING",
                        operation_status=1,
                        operation_status_present=True,
                        operation_status_valid=True,
                        outdoor_temp=5.5,
                        heat_pump_outdoor_temp=4.5,
                        outdoor_temp_source="manual",
                        tank_temp=48.5,
                        zone1_temp=21.5,
                        zone1_target_temp=22,
                        zone1_heat_min=20,
                        zone1_heat_max=55,
                        zone2_temp=20.5,
                        direction="PUMP",
                        space_heating_active=True,
                        space_heating_evidence="component_activity",
                        special_status=0,
                        special_status_supported=True,
                    )
                )
                for index, heat_kwh in enumerate((12.5, 14.75)):
                    await connection.execute(
                        insert(ConsumptionRecord).values(
                            ts=stamp + dt.timedelta(hours=index),
                            device_id=device,
                            source_date=stamp.date(),
                            heat_kwh=heat_kwh,
                            cool_kwh=0,
                            tank_kwh=2.25,
                            outdoor_temp=5.5,
                            heat_pump_outdoor_temp=4.5,
                            outdoor_temp_source="manual",
                        )
                    )
                await connection.execute(
                    insert(COPRecord).values(
                        ts=stamp,
                        device_id=device,
                        cop_value=3.7,
                        mode="HEATING",
                        outdoor_temp=5.5,
                        electrical_kwh=1,
                        thermal_kwh=3.7,
                    )
                )
                await connection.execute(
                    insert(SpaceHeatingGateRecord).values(
                        device_id=device,
                        state="eligible",
                        config_fingerprint="disposable-fixture",
                        base_c=15,
                        on_threshold_c=14,
                        off_threshold_c=16,
                        last_raw_outdoor_c=5.5,
                        reason_code="fixture",
                        source_status_ts=stamp,
                        evaluated_at=stamp,
                        consecutive_evaluation_failures=2,
                        failure_since=stamp,
                        last_failure_at=stamp,
                    )
                )
                plan_id = (
                    await connection.execute(
                        insert(PlanRecord)
                        .values(
                            created_at=stamp,
                            horizon_start=stamp,
                            horizon_end=stamp + dt.timedelta(days=1),
                            plan_json='{"fixture":true}',
                            optimizer_version="rules_v8",
                            input_provenance_json='{"cop_source":"assumed_planning_curve"}',
                            price_currency="SEK",
                            price_source="manual",
                            cost_estimate_eur=12.5,
                            status="superseded",
                        )
                        .returning(PlanRecord.id)
                    )
                ).scalar_one()
                action_id = (
                    await connection.execute(
                        insert(PlanActionRecord)
                        .values(
                            plan_id=plan_id,
                            scheduled_ts=stamp,
                            action_type="fixture_only",
                            payload_json="{}",
                            device_id=device,
                            status="verified",
                            verify_attempts=2,
                            safety_attempt_count=3,
                            result_json='{"fixture":true}',
                        )
                        .returning(PlanActionRecord.id)
                    )
                ).scalar_one()
                await connection.execute(
                    insert(ExecutorVerificationReadRecord).values(
                        reserved_at=stamp,
                        device_id=device,
                        lane="ordinary",
                        action_id=action_id,
                        phase="initial",
                        checkpoint_seconds=15,
                    )
                )
                await connection.execute(
                    insert(OptimizationRequestRecord).values(
                        requested_at=stamp,
                        requested_by="disposable-fixture",
                        status="completed",
                        plan_id=plan_id,
                        completed_at=stamp,
                    )
                )
                await connection.execute(
                    insert(SettingRecord).values(key="manual_cop_monitoring_enabled", value="true")
                )
                relations = tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT relname, relkind::text FROM pg_class JOIN pg_namespace "
                                "ON pg_namespace.oid=relnamespace WHERE nspname='public' "
                                "AND relkind IN ('r','p','S') ORDER BY relname"
                            )
                        )
                    ).all()
                )
                table_names = [name for name, kind in relations if kind in ("r", "p")]
                sequence_names = [name for name, kind in relations if kind == "S"]
                assert {model.__tablename__ for model in models} <= set(table_names)
                assert sequence_names
                legacy_metadata = MetaData()
                await connection.run_sync(
                    lambda current: legacy_metadata.reflect(
                        bind=current, only=[model.__tablename__ for model in models]
                    )
                )

                async def legacy_state(current):
                    state = {
                        model.__tablename__: tuple(
                            (
                                await current.execute(
                                    select(legacy_metadata.tables[model.__tablename__]).order_by(
                                        *legacy_metadata.tables[
                                            model.__tablename__
                                        ].primary_key.columns
                                    )
                                )
                            ).all()
                        )
                        for model in models
                    }
                    queries = {
                        "columns": (
                            "SELECT c.relname, a.attname, a.attnum, "
                            "format_type(a.atttypid,a.atttypmod), a.attnotnull, "
                            "pg_get_expr(d.adbin,d.adrelid) FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "JOIN pg_attribute a ON a.attrelid=c.oid "
                            "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
                            "WHERE n.nspname='public' AND c.relname=ANY(:names) "
                            "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
                        ),
                        "constraints": (
                            "SELECT c.relname, conname, pg_get_constraintdef(k.oid) "
                            "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname='public' AND c.relname=ANY(:names) "
                            "ORDER BY c.relname,conname"
                        ),
                        "indexes": (
                            "SELECT tablename,indexname,indexdef FROM pg_indexes "
                            "WHERE schemaname='public' AND tablename=ANY(:names) "
                            "ORDER BY tablename,indexname"
                        ),
                    }
                    for name, query in queries.items():
                        state[name] = tuple(
                            (await current.execute(text(query), {"names": table_names})).all()
                        )
                    state["sequences"] = {}
                    for name in sequence_names:
                        identifier = current.dialect.identifier_preparer.quote(name)
                        state["sequences"][name] = (
                            await current.execute(
                                text(f"SELECT last_value,is_called FROM public.{identifier}")
                            )
                        ).one()
                    state["hypertables"] = tuple(
                        (
                            await current.execute(
                                text(
                                    "SELECT hypertable_schema,hypertable_name,num_dimensions "
                                    "FROM timescaledb_information.hypertables "
                                    "ORDER BY hypertable_schema,hypertable_name"
                                )
                            )
                        ).all()
                    )
                    state["raw_rows"] = {}
                    for name in table_names:
                        if name == "alembic_version":
                            continue
                        identifier = current.dialect.identifier_preparer.quote(name)
                        state["raw_rows"][name] = tuple(
                            json.loads(value)
                            for value in (
                                await current.execute(
                                    text(
                                        f"SELECT to_jsonb(record)::text FROM public.{identifier} "
                                        "record ORDER BY to_jsonb(record)::text"
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    return state

                before = await legacy_state(connection)
                assert len(before["device_status"]) == len(before["cop_history"]) == 1
                assert len(before["consumption"]) == 2
                assert before["sequences"]
                assert all(before[key] for key in ("columns", "constraints", "indexes"))
            await asyncio.to_thread(command.upgrade, config, "032")
            async with engine.begin() as connection:
                assert await legacy_state(connection) == before
                after_relations = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT relname,relkind::text FROM pg_class JOIN pg_namespace "
                                "ON pg_namespace.oid=relnamespace WHERE nspname='public' "
                                "AND relkind IN ('r','p','S')"
                            )
                        )
                    ).all()
                )
                assert after_relations - set(relations) == {
                    ("manual_cop_observations", "r"),
                    ("manual_cop_revision_seq", "S"),
                }
                assert set(relations) <= after_relations
                assert (
                    await connection.execute(text("SELECT count(*) FROM manual_cop_observations"))
                ).scalar_one() == 0
                values = observation_values("day", stamp.date(), "Europe/Stockholm")
                values["device_id"] = device
                values["observed_at"] = period_bounds("day", stamp.date(), "Europe/Stockholm")[1]
                await insert_legacy_observation(connection, values)
                await connection.execute(
                    text(
                        "UPDATE settings SET value='false' WHERE key='manual_cop_monitoring_enabled'"
                    )
                )
                retained_legacy = await legacy_state(connection)
            tokens = []
            for target in (predecessor, "020"):
                async with engine.connect() as connection:
                    retained = await retained_storage_state(connection)
                    assert retained["marker"] == ("032",)
                    assert retained["rows"] and retained["sequence"].is_called is True
                with pytest.raises(
                    RuntimeError,
                    match="^manual_cop_retained: disable forecasting; retain ledger and sequence$",
                ):
                    await asyncio.to_thread(command.downgrade, config, target)
                async with engine.begin() as connection:
                    assert await legacy_state(connection) == retained_legacy
                    assert await retained_storage_state(connection) == retained
                    assert (
                        await connection.execute(
                            text(
                                "SELECT value FROM settings WHERE key='manual_cop_monitoring_enabled'"
                            )
                        )
                    ).scalar_one() == "false"
                    token = (
                        await connection.execute(text("SELECT nextval('manual_cop_revision_seq')"))
                    ).scalar_one()
                    assert token > retained["sequence"].last_value
                    assert token not in tokens
                    tokens.append(token)
            async with engine.connect() as connection:
                retained = await retained_storage_state(connection)
            # Historical qualification stops at genuine 035: 036 is forward-only, and the
            # workflow's upgrade-head phase then upgrades this populated database to 039.
            assert head == CURRENT_HEAD_REVISION
            assert (
                ScriptDirectory.from_config(config)
                .get_revision(LOCAL_PERIOD_REVISION)
                .down_revision
                == HISTORICAL_HEAD
            )
            # Exactly the nullable, default-free columns 033-035 add to retained legacy tables.
            additions = {
                "device_status": (("native_policy_provenance", "jsonb"),),
                "plan_actions": (("safety_claim_token", "uuid"),),
                "consumption": (("source_timezone", "character varying(64)"),),
                "prices": (("interval_schema", "character varying(64)"),),
                "weather": (("units_schema", "character varying(64)"),),
            }
            assert set(additions) <= set(retained_legacy["raw_rows"])
            # PostgreSQL appends after dropped attributes, so each re-upgrade leaves a gap.
            next_positions = {
                table: 1
                + max(column[2] for column in retained_legacy["columns"] if column[0] == table)
                for table in additions
            }
            for operation, target in (
                (command.upgrade, HISTORICAL_HEAD),
                (command.downgrade, "032"),
                (command.upgrade, HISTORICAL_HEAD),
            ):
                await asyncio.to_thread(operation, config, target)
                async with engine.connect() as connection:
                    assert await retained_storage_state(connection) == retained | {
                        "marker": (target,)
                    }
                    expected_legacy = copy.deepcopy(retained_legacy)
                    if target == HISTORICAL_HEAD:
                        added = tuple(
                            (table, name, next_positions[table] + offset, sql_type, False, None)
                            for table, columns in additions.items()
                            for offset, (name, sql_type) in enumerate(columns)
                        )
                        expected_legacy["columns"] = tuple(
                            sorted(
                                (*expected_legacy["columns"], *added),
                                key=lambda column: (column[0], column[2]),
                            )
                        )
                        for table, columns in additions.items():
                            expected_legacy["raw_rows"][table] = tuple(
                                row | {name: None for name, _sql_type in columns}
                                for row in expected_legacy["raw_rows"][table]
                            )
                    assert await legacy_state(connection) == expected_legacy
                    if target == HISTORICAL_HEAD:
                        for table, columns in additions.items():
                            next_positions[table] += len(columns)
            # No historical `alembic check`: 035 differs from the current 039 metadata by
            # design. The explicit catalog, row and sequence comparisons above replace it.
        finally:
            await engine.dispose()

    async def test_033_to_036_forward_only_preserves_restore_authority_and_scoped_reset(
        self, forward_manual_database, monkeypatch
    ):
        from packages.api.routers import admin

        child = forward_manual_database
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        assert scripts.get_heads() == [CURRENT_HEAD_REVISION]
        assert {
            revision: scripts.get_revision(revision).down_revision
            for revision in ("034", "035", "036")
        } == {"034": "033", "035": "034", "036": "035"}
        await child.alembic("upgrade", "033")
        async with child.engine.begin() as connection:
            seeded = await seed_forward_records(connection)
        async with child.engine.connect() as connection:
            stage = await migration_child_snapshot(connection)
            retained = await retained_storage_state(connection)
        assert stage["marker"] == "033"
        assert len(stage["rows"]["manual_cop_observations"]) == seeded.manual
        for revision in ("034", HISTORICAL_HEAD):
            await child.alembic("upgrade", revision)
            async with child.engine.connect() as connection:
                current = await migration_child_snapshot(connection)
                assert await retained_storage_state(connection) == retained | {
                    "marker": (revision,)
                }
            assert current == expected_forward_additions(stage, revision)
            stage = current
        # Representative 035 provenance is captured before the forward-only step.
        async with child.engine.begin() as connection:
            await connection.execute(
                text("UPDATE consumption SET source_timezone = 'Europe/Stockholm'")
            )
        async with child.engine.connect() as connection:
            provenance = await migration_child_snapshot(connection)
            legacy = await retained_storage_state(connection)
        assert {row["source_timezone"] for row in provenance["rows"]["consumption"]} == {
            "Europe/Stockholm"
        }
        await child.alembic("upgrade", LOCAL_PERIOD_REVISION)
        async with child.engine.connect() as connection:
            current = await migration_child_snapshot(connection)
            local = await retained_storage_state(connection)
        assert current == expected_local_periods(provenance)
        for key in ("sequence", "sequence_configuration", "indexes", "permissions", "hypertables"):
            assert local[key] == legacy[key], key
        assert legacy["helper"] is not None and local["helper"] is None
        assert local["columns"] == tuple(
            column for column in legacy["columns"] if column[0] not in LEGACY_BOUNDARY_COLUMNS
        )
        assert (
            dict(local["constraints"])
            == {
                name: definition
                for name, definition in dict(legacy["constraints"]).items()
                if name not in LEGACY_CALENDAR_CONSTRAINTS
            }
            | LOCAL_PERIOD_CONSTRAINTS
        )
        for before, after in zip(legacy["rows"], local["rows"], strict=True):
            snapshot = snapshot_from_stored(after)
            assert (snapshot.period_start, snapshot.period_end) == (
                before.period_start,
                before.period_end,
            )
            assert snapshot.revision == str(before.revision)
        actions = {row["id"]: row for row in current["rows"]["plan_actions"]}
        assert {restore: actions[restore]["reverts_action_id"] for restore in seeded.links} == (
            seeded.links
        )
        assert all(row["safety_claim_token"] is None for row in actions.values())

        monkeypatch.setattr(admin, "get_session", child.session_context)
        result = await admin.reset_manual_cop_ledger()
        assert result == {
            "status": "ok",
            "scopes": ["manual_cop"],
            "deleted_rows": {"manual_cop_observations": seeded.manual},
            "total_rows_deleted": seeded.manual,
            "manual_cop_monitoring_enabled": False,
            "models_reset": False,
            "deleted_models": [],
            "model_lifecycle": None,
        }
        async with child.engine.connect() as connection:
            after_reset = await migration_child_snapshot(connection)
            retained_reset = await retained_storage_state(connection)
        added = [
            row
            for row in after_reset["rows"]["audit_log"]
            if row not in current["rows"]["audit_log"]
        ]
        assert len(added) == 1
        audit = added[0]
        assert (audit["actor"], audit["action"], audit["target_device"], audit["result"]) == (
            "user",
            "reset_data",
            None,
            "ok",
        )
        assert json.loads(audit["payload_json"]) == {
            "scopes": ["manual_cop"],
            "deleted_rows": {"manual_cop_observations": seeded.manual},
            "settings": {"manual_cop_monitoring_enabled": "false"},
            "models_reset": False,
            "deleted_models": [],
            "model_lifecycle": None,
        }
        assert audit["id"] > max(row["id"] for row in current["rows"]["audit_log"])
        expected = copy.deepcopy(current)
        expected["rows"]["manual_cop_observations"] = ()
        expected["rows"]["settings"] = canonical_rows(
            row | {"value": "false"} if row["key"] == "manual_cop_monitoring_enabled" else row
            for row in expected["rows"]["settings"]
        )
        expected["rows"]["audit_log"] = canonical_rows((*expected["rows"]["audit_log"], audit))
        # Normal audit_log.id allocation is the only permitted sequence-state change.
        assert "audit_log_id_seq" in expected["sequences"]
        expected["sequences"]["audit_log_id_seq"] = (audit["id"], True)
        assert after_reset == expected
        assert retained_reset == local | {"rows": ()}

        with pytest.raises(RuntimeError, match="^manual_cop_local_periods_forward_only"):
            await child.alembic("downgrade", HISTORICAL_HEAD)
        async with child.engine.connect() as connection:
            assert await migration_child_snapshot(connection) == after_reset
            assert await retained_storage_state(connection) == retained_reset

    async def test_blocked_036_upgrade_keeps_genuine_035_and_retry_completes(
        self, forward_manual_database
    ):
        child = forward_manual_database
        await child.alembic("upgrade", HISTORICAL_HEAD)
        async with child.engine.begin() as connection:
            seeded = await seed_forward_records(connection)
        async with child.engine.connect() as connection:
            before = await migration_child_snapshot(connection)
            retained = await retained_storage_state(connection)
        assert before["marker"] == HISTORICAL_HEAD
        assert len(before["rows"]["manual_cop_observations"]) == seeded.manual
        async with child.engine.connect() as blocker:
            transaction = await blocker.begin()
            try:
                # ACCESS SHARE blocks the ACCESS EXCLUSIVE DDL until 036's lock timeout.
                await blocker.execute(text("SELECT count(*) FROM manual_cop_observations"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    await child.alembic("upgrade", LOCAL_PERIOD_REVISION)
            finally:
                await transaction.rollback()
        async with child.engine.connect() as connection:
            assert await migration_child_snapshot(connection) == before
            assert await retained_storage_state(connection) == retained
        await child.alembic("upgrade", LOCAL_PERIOD_REVISION)
        async with child.engine.connect() as connection:
            assert await migration_child_snapshot(connection) == expected_local_periods(before)
            assert (await retained_storage_state(connection))["helper"] is None

    async def test_guarded_child_blocks_mutation_on_changed_parent_or_child(
        self, forward_manual_database, monkeypatch
    ):
        child = forward_manual_database
        assert isinstance(child.engine, GuardedChildEngine)
        async with child.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE guard_probe (id integer)"))
        mutation = text("INSERT INTO guard_probe (id) VALUES (1)")
        changed = copy.deepcopy(owned_test_environment())
        changed["services"]["postgres"]["id"] = "f" * 64
        with monkeypatch.context() as patched:
            patched.setitem(globals(), "owned_test_environment", lambda: changed)
            for surface in (child.engine.begin, child.session_context):
                with pytest.raises(AssertionError, match="parent identity changed"):
                    async with surface() as handle:
                        await handle.execute(mutation)
            with pytest.raises(AssertionError, match="parent identity changed"):
                await child.verify()
        name, user, port, oid, binding = child.engine.expected_identity
        for identity in ((name, user, port, oid + 1, binding), (name, user, port, oid, "{}")):
            with pytest.raises(AssertionError, match="child identity mismatch"):
                async with child.engine.expecting(identity).begin() as connection:
                    await connection.execute(mutation)
        async with child.engine.connect() as connection:
            count = await connection.execute(text("SELECT count(*) FROM guard_probe"))
            assert count.scalar_one() == 0

    async def reset_ready_child(self, child):
        await child.alembic("upgrade", HISTORICAL_HEAD)
        async with child.engine.begin() as connection:
            seeded = await seed_forward_records(connection)
        await child.alembic("upgrade", LOCAL_PERIOD_REVISION)
        async with child.engine.connect() as connection:
            before = await migration_child_snapshot(connection)
            retained = await retained_storage_state(connection)
        assert before["marker"] == LOCAL_PERIOD_REVISION
        assert len(before["rows"]["manual_cop_observations"]) == seeded.manual
        settings_rows = {row["key"]: row["value"] for row in before["rows"]["settings"]}
        assert settings_rows["manual_cop_monitoring_enabled"] == "true"
        assert "audit_log_id_seq" in before["sequences"]
        return seeded, before, retained

    async def assert_reset_rolled_back(self, child, before, retained):
        async with child.engine.connect() as connection:
            after = await migration_child_snapshot(connection)
            assert await retained_storage_state(connection) == retained
        expected = copy.deepcopy(before)
        # A rolled-back audit INSERT may only consume normal audit_log.id values.
        allocated = after["sequences"]["audit_log_id_seq"]
        assert allocated[0] >= before["sequences"]["audit_log_id_seq"][0]
        expected["sequences"]["audit_log_id_seq"] = allocated
        assert after == expected

    async def test_manual_reset_audit_failure_rolls_back_ledger_preference_and_audit(
        self, forward_manual_database, monkeypatch
    ):
        from fastapi import HTTPException

        from packages.api.routers import admin

        child = forward_manual_database
        _seeded, before, retained = await self.reset_ready_child(child)
        monkeypatch.setattr(admin, "get_session", child.session_context)
        async with child.engine.connect() as blocker:
            transaction = await blocker.begin()
            try:
                # SHARE conflicts only with the audit INSERT, after the delete and upsert ran.
                await blocker.execute(text("LOCK TABLE public.audit_log IN SHARE MODE"))
                with pytest.raises(HTTPException) as refused:
                    await admin.reset_data(admin.ResetRequest(scopes=["manual_cop"]))
            finally:
                await transaction.rollback()
        assert refused.value.status_code == 503
        assert refused.value.detail == {"code": "manual_cop_reset_unavailable"}
        assert isinstance(refused.value.__cause__, DBAPIError)
        assert "lock timeout" in str(refused.value.__cause__)
        await self.assert_reset_rolled_back(child, before, retained)

    async def test_manual_reset_cancellation_rolls_back_ledger_preference_and_audit(
        self, forward_manual_database, monkeypatch
    ):
        from packages.api.routers import admin

        child = forward_manual_database
        _seeded, before, retained = await self.reset_ready_child(child)
        monkeypatch.setattr(admin, "get_session", child.session_context)
        waiting_sql = text(
            "SELECT count(*) FROM pg_locks WHERE relation = 'public.audit_log'::regclass "
            "AND NOT granted"
        )
        async with AsyncExitStack() as stack:
            blocker = await stack.enter_async_context(child.engine.connect())
            observer = await stack.enter_async_context(child.engine.connect())

            async def waiting():
                blocked = (await observer.execute(waiting_sql)).scalar_one() == 1
                await observer.rollback()
                return blocked

            transaction = await blocker.begin()
            await blocker.execute(text("LOCK TABLE public.audit_log IN SHARE MODE"))
            # Releases the blocker only after the reset settled as cancelled; an unsettled
            # reset keeps both connections held and is handed to the child fixture, whose
            # teardown then refuses to release them or drop the child.
            await cancel_blocked_reset(
                admin.reset_manual_cop_ledger,
                waiting,
                transaction.rollback,
                lambda task: child.retain(task, stack.pop_all()),
            )
        await self.assert_reset_rolled_back(child, before, retained)

    @pytest.mark.parametrize(
        "exit_path",
        [
            "cancelled_as_expected",
            "polling_error",
            "polling_exhausted",
            "test_cancelled",
            "cancel_during_drain",
            "release_error",
            "cancel_during_drain_and_release_error",
            "drain_timeout",
        ],
    )
    async def test_cancel_blocked_reset_settles_reset_before_release(self, exit_path):
        lock = asyncio.Event()
        events = []
        cancel_during_drain = exit_path.startswith("cancel_during_drain")
        resist = None if exit_path == "drain_timeout" else int(cancel_during_drain)
        control = {"resist": resist}
        retained = []

        @asynccontextmanager
        async def session_context():
            try:
                yield
                # Committing flushes the audit INSERT, which waits on the blocker's lock.
                await flush()
                events.append("reset_committed")
            except BaseException:
                events.append("reset_rolled_back")
                raise

        async def flush():
            events.append("flush_blocked")
            ignored = 0
            while True:
                try:
                    await lock.wait()
                    break
                except asyncio.CancelledError:
                    resist = control["resist"]
                    if resist is not None and ignored >= resist:
                        raise
                    ignored += 1
                    events.append("cancel_ignored")
            events.append("flush_done")

        async def reset():
            async with session_context():
                events.append("reset_body")

        async def waiting():
            events.append("poll")
            if exit_path == "polling_error":
                raise RuntimeError("synthetic polling failure")
            return exit_path != "polling_exhausted" and "flush_blocked" in events

        async def release():
            events.append("blocker_released")
            lock.set()
            if exit_path.endswith("release_error"):
                raise OSError("synthetic release failure")

        def retain(task):
            events.append("retained")
            retained.append(task)

        async def scenario():
            await cancel_blocked_reset(
                reset, waiting, release, retain, attempts=3, interval=0.01, limit=0.05
            )

        expected = {
            "cancelled_as_expected": None,
            "polling_error": RuntimeError,
            "polling_exhausted": AssertionError,
            "test_cancelled": asyncio.CancelledError,
            "cancel_during_drain": asyncio.CancelledError,
            "release_error": OSError,
            "cancel_during_drain_and_release_error": AssertionError,
            "drain_timeout": AssertionError,
        }[exit_path]
        before = asyncio.all_tasks()
        runner = asyncio.ensure_future(scenario())
        if exit_path == "test_cancelled":
            while "poll" not in events:
                await asyncio.sleep(0)
            runner.cancel()
        elif cancel_during_drain:
            while "cancel_ignored" not in events:
                await asyncio.sleep(0)
            runner.cancel()
        if expected is None:
            await runner
        else:
            with pytest.raises(expected) as raised:
                await runner
        if exit_path == "cancel_during_drain_and_release_error":
            # Neither the drain's cancellation nor the release failure is lost.
            assert isinstance(raised.value.__cause__, asyncio.CancelledError)
            assert isinstance(raised.value.__context__, OSError)
        # Give any wrongly surviving reset the chance to run after the exit.
        for _ in range(5):
            await asyncio.sleep(0)
        assert events[:2] == ["reset_body", "flush_blocked"]
        assert "reset_committed" not in events and "flush_done" not in events
        if exit_path == "drain_timeout":
            # Fail closed: the unsettled reset is retained, and the blocker is never released.
            assert "did not settle" in str(raised.value)
            assert [task.done() for task in retained] == [False]
            assert "blocker_released" not in events and events[-1] == "retained"
            assert asyncio.all_tasks() - before == set(retained)
            control["resist"] = 0
            retained[0].cancel()
            await asyncio.wait(retained, timeout=1)
            assert retained[0].cancelled() and events[-1] == "reset_rolled_back"
            assert "reset_committed" not in events
            return
        assert not asyncio.all_tasks() - before, "reset task survived the exit path"
        assert retained == [] and events.count("blocker_released") == 1
        # Settled before release: nothing of the reset runs once the blocker is released.
        assert events[-2:] == ["reset_rolled_back", "blocker_released"]

    async def test_child_teardown_refuses_while_a_retained_reset_is_unsettled(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path))
        pending = asyncio.get_running_loop().create_future()
        closed = []

        class Resources:
            async def aclose(self):
                closed.append(True)

        class Parent:
            def connect(self):
                raise AssertionError("cleanup contacted the parent despite an unsettled reset")

        retained = [Resources()]
        await _release_retained_blockers([pending], retained)
        assert closed == [] and len(retained) == 1
        child = {"database": "manual_cop_child_x", "oid": 1, "binding": "{}", "parent": {}}
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(Parent(), child, [pending], None, 1)
        assert "did not settle" in str(refused.value.__cause__)
        receipt = json.loads(
            (tmp_path / "migration-child-cleanup-manual_cop_child_x.json").read_text()
        )
        assert receipt["error"] == "AssertionError"
        pending.cancel()
        await _release_retained_blockers([pending], retained)
        assert closed == [True] and retained == []

    async def test_diag_capture_is_child_scoped_and_bounded(self, tmp_path, monkeypatch):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        # More rows than the bound: the client-side bound holds even if the server's does not.
        parent = _DiagnosticParent(child, [2], rows=_diagnostic_rows(child, 70), total=70)
        loop = asyncio.get_running_loop()
        finished, cancelled = loop.create_future(), loop.create_future()
        finished.set_result(None)
        cancelled.cancel()
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [finished, cancelled], (), 2)
        assert str(refused.value.__cause__) == "migration child still has sessions"
        assert parent.kinds() == ["poll", *DIAGNOSTIC_CAPTURE, "poll", "poll", *DIAGNOSTIC_CAPTURE]
        assert parent.connects == 1 and parent.exits == [AssertionError]
        for kind, sql, parameters in parent.statements:
            assert not re.search(r"\b(query|password|passwd|client_hostname)\b", sql)
            if kind == "activity":
                assert parameters == {"child_oid": child["oid"]}
                assert "WHERE a.datid = CAST(:child_oid AS oid) ORDER BY a.pid LIMIT 64" in sql
        diagnostics = _diagnostic_receipt(tmp_path, child)
        assert diagnostics["version"] == 1 and diagnostics["retained_resources"] == 2
        assert diagnostics["workers"] == [
            {"index": 0, "done": True, "cancelled": False},
            {"index": 1, "done": True, "cancelled": True},
        ]
        snapshots = diagnostics["snapshots"]
        assert [(s["stage"], s["poll"], s["count"]) for s in snapshots] == [
            ("first_nonzero", 0, 2),
            ("final_refusal", 2, 2),
        ]
        for snapshot in snapshots:
            assert snapshot["status"] == "captured" and snapshot["queries"] == 3
            assert snapshot["total"] == 70 and snapshot["truncated"] is True
            assert len(snapshot["activity"]) == MIGRATION_CHILD_DIAGNOSTIC_ROWS
            assert {row["datid"] for row in snapshot["activity"]} == {child["oid"]}
            identity = snapshot["identity"]
            assert identity["owned"] and identity["comment_matches"]
            assert identity["child_extensions"] == "unknown"
            assert identity["parent_extensions"] == [["plpgsql", "1.0"], ["timescaledb", "2.17.2"]]
        assert diagnostics["connection_exit"] == {"cancelled": False, "status": "closed"}
        assert "binding-text-never-reported" not in json.dumps(diagnostics)

    @pytest.mark.parametrize(
        "drift", ["oid_swap", "comment", "parent_sql", "parent_receipt", "after_activity"]
    )
    async def test_diag_identity_drift_refuses_before_activity(self, tmp_path, monkeypatch, drift):
        _diagnostic_guards(
            monkeypatch,
            tmp_path,
            polls=3,
            parent_failure_at=3 if drift == "parent_receipt" else None,
        )
        child = _diagnostic_child()
        parent = _DiagnosticParent(child, [1], rows=_diagnostic_rows(child, 1))
        owned = parent.owned_identity()
        swapped = {
            "oid_swap": owned[:3] + (9999,) + owned[4:],
            "comment": owned[:6] + ("another binding",) + owned[7:],
            "parent_sql": ("postgres",) + owned[1:],
            "after_activity": owned[:3] + (9999,) + owned[4:],
        }.get(drift)
        if drift == "after_activity":
            parent.identities = [owned, swapped]
        elif swapped is not None:
            parent.identities = [swapped]
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        # The first capture's actual terminal error is primary; no final refusal is invented.
        expected = (
            "migration child parent identity changed"
            if drift == "parent_receipt"
            else "migration child ownership uncertain"
        )
        assert str(refused.value.__cause__) == expected
        assert parent.kinds() == {
            "parent_receipt": ["poll"],
            "after_activity": ["poll", *DIAGNOSTIC_CAPTURE],
        }.get(drift, ["poll", "identity"])
        assert parent.connects == 1 and len(parent.exits) == 1
        [snapshot] = _diagnostic_receipt(tmp_path, child)["snapshots"]
        assert snapshot["terminal"] is True and "activity" not in snapshot
        assert snapshot["status"] == ("failed" if drift == "parent_receipt" else "identity_drift")

    @pytest.mark.parametrize("stage", ["first", "final"])
    async def test_diag_usable_autocommit_select_failure_preserves_polling(
        self, tmp_path, monkeypatch, stage
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()

        def server_error(fake):
            return _adapted_asyncpg_error("InsufficientPrivilegeError")

        rows = _diagnostic_rows(child, 1)
        if stage == "first":
            parent = _DiagnosticParent(
                child, [1, 1, 0], rows=rows, failures={("activity", 1): server_error}
            )
            await _drop_migration_child(parent, child, [], (), 0)
            assert parent.kinds() == ["poll", "identity", "activity", "poll", "poll", "drop"] + [
                "relations"
            ]
            assert list(tmp_path.iterdir()) == []
        else:
            parent = _DiagnosticParent(
                child, [1], rows=rows, failures={("activity", 2): server_error}
            )
            with pytest.raises(AssertionError, match="cleanup refused") as refused:
                await _drop_migration_child(parent, child, [], (), 0)
            refusal = refused.value.__cause__
            assert str(refusal) == "migration child still has sessions"
            assert refusal.__cause__ is None
            assert parent.kinds() == ["poll", *DIAGNOSTIC_CAPTURE, "poll", "poll"] + [
                "identity",
                "activity",
            ]
            snapshots = _diagnostic_receipt(tmp_path, child)["snapshots"]
            assert [s["status"] for s in snapshots] == ["captured", "failed"]
            assert snapshots[1]["terminal"] is False and snapshots[1]["sqlstate"] == "42501"
            assert snapshots[1]["error"] == "ProgrammingError"
        assert parent.connects == 1 and len(parent.exits) == 1

    @pytest.mark.parametrize("stage", ["first", "final"])
    @pytest.mark.parametrize(
        "mode",
        [
            "timeout",
            "error_invalidated",
            "sqlalchemy_invalidated",
            "driver_closed",
            "in_transaction",
            "replaced_connection",
            "unstructured",
        ],
    )
    async def test_diag_timeout_or_invalidation_is_terminal(
        self, tmp_path, monkeypatch, mode, stage
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        raised = []

        def fail(fake):
            if mode == "timeout":
                error = TimeoutError("synthetic command timeout")
            elif mode == "unstructured":
                # No structured SQLSTATE: the outcome is uncertain, hence terminal.
                error = DBAPIError("SELECT", {}, Exception("synthetic"))
            else:
                # An otherwise recoverable SQLSTATE; only the connection state makes it terminal.
                error = _adapted_asyncpg_error(
                    "InsufficientPrivilegeError", invalidated=mode == "error_invalidated"
                )
            if mode == "sqlalchemy_invalidated":
                fake.invalidated = True
            elif mode == "driver_closed":
                fake.driver.closed = True
            elif mode == "in_transaction":
                fake.driver.in_transaction = True
            elif mode == "replaced_connection":
                fake.sync_connection._dbapi_connection = SimpleNamespace()
            raised.append(error)
            return error

        nth = 1 if stage == "first" else 2
        parent = _DiagnosticParent(
            child, [1], rows=_diagnostic_rows(child, 1), failures={("activity", nth): fail}
        )
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        # Terminal: nothing more is sent, nothing reconnects, nothing is dropped.
        prefix = [] if stage == "first" else ["poll", *DIAGNOSTIC_CAPTURE, "poll"]
        assert parent.kinds() == prefix + ["poll", "identity", "activity"]
        assert parent.connects == 1 and len(parent.exits) == 1
        cause = refused.value.__cause__
        if stage == "final":
            assert str(cause) == "migration child still has sessions"
            cause = cause.__cause__
        assert cause is raised[0]
        snapshot = _diagnostic_receipt(tmp_path, child)["snapshots"][-1]
        assert snapshot["terminal"] is True and snapshot["status"] == "failed"

    @pytest.mark.parametrize("moment", ["capture", "exit"])
    async def test_diag_external_cancellation_drains_exit_without_reconnect(
        self, tmp_path, monkeypatch, moment
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        blocked, gate = asyncio.Event(), asyncio.Event()
        if moment == "capture":
            expected = ["poll", "identity", "activity"]
            parent = _DiagnosticParent(
                child,
                [1],
                rows=_diagnostic_rows(child, 1),
                blocks={("activity", 1): blocked},
                exit_gate=gate,
            )
        else:
            expected = ["poll", "drop", "relations"]
            parent = _DiagnosticParent(child, [0], exit_gate=gate)
        before = asyncio.all_tasks()
        runner = asyncio.ensure_future(_drop_migration_child(parent, child, [], (), 0))
        while parent.kinds() != expected:
            await asyncio.sleep(0)
        if moment == "capture":
            for _ in range(5):
                await asyncio.sleep(0)
            runner.cancel()
        while not parent.exits:
            await asyncio.sleep(0)
        if moment == "exit":
            runner.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
        # A further cancellation while the exit drains neither abandons nor repeats it.
        runner.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not runner.done() and len(parent.exits) == 1
        gate.set()
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await runner
        assert isinstance(refused.value.__cause__, asyncio.CancelledError)
        assert parent.kinds() == expected and not blocked.is_set()
        assert parent.connects == 1 and len(parent.exits) == 1
        assert asyncio.all_tasks() - before == set(), "a cleanup task was abandoned"
        diagnostics = _diagnostic_receipt(tmp_path, child)
        assert diagnostics["connection_exit"] == {"cancelled": True, "status": "closed"}
        if moment == "capture":
            [snapshot] = diagnostics["snapshots"]
            assert snapshot["terminal"] is True and snapshot["error"] == "CancelledError"

    @pytest.mark.parametrize(
        "failing", [("capture",), ("exit",), ("receipt",), ("capture", "exit", "receipt")]
    )
    async def test_diag_final_refusal_survives_capture_exit_and_receipt_failure(
        self, tmp_path, monkeypatch, failing
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=2)
        if "receipt" in failing:
            monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path / "missing"))
        child = _diagnostic_child()
        timeout = TimeoutError("synthetic capture timeout")
        parent = _DiagnosticParent(
            child,
            [1],
            rows=_diagnostic_rows(child, 1),
            failures={("activity", 2): lambda fake: timeout} if "capture" in failing else None,
            exit_error=OSError("synthetic exit failure") if "exit" in failing else None,
        )
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        refusal = refused.value.__cause__
        assert str(refusal) == "migration child still has sessions"
        assert refusal.__cause__ is (timeout if "capture" in failing else None)
        notes = getattr(refusal, "__notes__", [])
        assert any("connection exit" in note for note in notes) == ("exit" in failing)
        assert any("receipt not written" in note for note in notes) == ("receipt" in failing)
        assert str(refused.value).endswith("(unwritten)") == ("receipt" in failing)
        assert parent.exits == [AssertionError] and "drop" not in parent.kinds()
        if "receipt" not in failing:
            diagnostics = _diagnostic_receipt(tmp_path, child)
            exit_status = "failed" if "exit" in failing else "closed"
            assert diagnostics["connection_exit"]["status"] == exit_status
            final_status = "failed" if "capture" in failing else "captured"
            assert diagnostics["snapshots"][-1]["status"] == final_status

    async def test_diag_zero_session_path_and_redaction_contract(self, tmp_path, monkeypatch):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        parent = _DiagnosticParent(child, [0])
        # The zero-session path never probes the raw connection nor issues a new query.
        parent.sync_connection = None
        await _drop_migration_child(parent, child, [], (), 0)
        assert parent.kinds() == ["poll", "drop", "relations"]
        assert parent.connects == 1 and parent.exits == [None]
        assert list(tmp_path.iterdir()) == []
        secret = "postgresql://heatpump:hunter2@db.example/heatpump"
        start = dt.datetime(2026, 10, 12, 21, 0, tzinfo=dt.timezone.utc)
        serialized = _diagnostic_activity(
            (77, 4242, child["database"], 10, "heatpump", "TimescaleDB Background Worker Scheduler")
            + (secret, "8.8.8.8", None, start, None, None, start, "idle in transaction")
            + ("Lock", "transactionid"),
            child,
        )
        assert serialized["datname"] == child["database"]
        assert serialized["backend_type"] == "TimescaleDB Background Worker Scheduler"
        assert serialized["application_name"] == _diagnostic_digest(secret)
        assert serialized["client_addr"] == _diagnostic_digest("8.8.8.8")
        assert serialized["client_port"] == "unknown" and serialized["xact_start"] == "unknown"
        assert serialized["backend_start"] == start.isoformat()
        assert serialized["wait_event"] == "transactionid"
        assert "hunter2" not in json.dumps(serialized)
        for address in ("127.0.0.1", "::1", "10.1.2.3", "192.168.0.4"):
            assert _diagnostic_value("address", address) == address
        assert _diagnostic_value("label", "someone else").startswith("redacted:sha256:")
        assert _diagnostic_value("event", "two words").startswith("redacted:sha256:")
        assert _diagnostic_value("int", "12") == _diagnostic_digest("12")
        identity = _diagnostic_identity(parent.owned_identity(), child)
        assert identity["owned"] and identity["comment_matches"]
        assert identity["comment_sha256"] == hashlib.sha256(child["binding"].encode()).hexdigest()
        assert "binding-text-never-reported" not in json.dumps(identity)
        assert _diagnostic_identity(None, child) == {"owned": False, "child": "missing"}

    @pytest.mark.parametrize("primary", [False, True])
    @pytest.mark.parametrize("outcome", ["closed", "failed"])
    async def test_diag_pending_exit_drains_without_deadline_or_reconnect(
        self, monkeypatch, primary, outcome
    ):
        real = asyncio
        exiting = _DiagnosticPendingExit()
        cancellations = []

        async def wait(tasks, timeout=None):
            if exiting.waits in (1, 3, 250) and len(cancellations) < 3:
                cancellations.append(real.CancelledError())
                exiting.waits += 1
                raise cancellations[-1]
            return await exiting.wait(tasks, timeout)

        # Scripted, loop-free asyncio: no clock exists, so no budget can end the drain early.
        fake = SimpleNamespace(CancelledError=real.CancelledError, wait=wait)
        fake.ensure_future = exiting.ensure_future
        monkeypatch.setitem(globals(), "asyncio", fake)
        refusal = AssertionError("migration child still has sessions") if primary else None
        diagnostics = {}
        draining = _exit_migration_child_connection(
            SimpleNamespace(__aexit__=exiting.exit), refusal, diagnostics
        )
        for _ in range(1000):
            assert draining.send(None) is None, "the drain ended while the exit was pending"
        assert len(cancellations) == 3 and exiting.waits >= 1000
        assert exiting.calls == [(type(refusal), refusal, None) if primary else (None, None, None)]
        assert exiting.cancels == 0 and "connection_exit" not in diagnostics
        exiting.settled = True
        if outcome == "failed":
            exiting.error = OSError("synthetic exit failure")
        if primary:
            with pytest.raises(StopIteration):
                draining.send(None)
            assert refusal.__notes__ == [
                f"migration child connection exit: {diagnostics['connection_exit']}"
            ]
        else:
            with pytest.raises(BaseException) as raised:
                draining.send(None)
            if outcome == "closed":
                assert raised.value is cancellations[0]
            else:
                # The first terminal event, the caller's cancellation, stays primary; the
                # later exit failure keeps its exact identity as the explicit cause.
                assert raised.value is cancellations[0]
                assert raised.value.__cause__ is exiting.error
                assert raised.value.__notes__ == [
                    f"migration child connection exit then failed: {diagnostics['connection_exit']}"
                ]
            assert exiting.error is None or not hasattr(exiting.error, "__notes__")
            assert all(c.__cause__ is None for c in cancellations[1:])
        expected = {"cancelled": True, "status": outcome}
        if outcome == "failed":
            expected["error"] = "OSError"
        assert diagnostics["connection_exit"] == expected
        assert len(exiting.calls) == 1 and exiting.cancels == 0

    @pytest.mark.parametrize("stage", ["first", "final"])
    @pytest.mark.parametrize(
        ("name", "sqlstate"),
        [
            ("QueryCanceledError", "57014"),
            ("AdminShutdownError", "57P01"),
            ("LockNotAvailableError", "55P03"),
            ("IdleInTransactionSessionTimeoutError", "25P03"),
        ],
    )
    async def test_diag_cancel_or_timeout_sqlstate_is_terminal(
        self, tmp_path, monkeypatch, name, sqlstate, stage
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        error = _adapted_asyncpg_error(name)
        # The SQLAlchemy-adapted error is not an invalidation; its SQLSTATE alone decides.
        assert error.orig.sqlstate == sqlstate and not error.connection_invalidated
        nth = 1 if stage == "first" else 2
        # A first-stage session count that would otherwise reach zero and DROP.
        counts = [1, 0] if stage == "first" else [1]
        parent = _DiagnosticParent(
            child,
            counts,
            rows=_diagnostic_rows(child, 1),
            failures={("activity", nth): lambda fake: error},
        )
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        # Terminal: no further poll, diagnostic SQL or DROP; nothing reconnects.
        prefix = [] if stage == "first" else ["poll", *DIAGNOSTIC_CAPTURE, "poll"]
        assert parent.kinds() == prefix + ["poll", "identity", "activity"]
        assert parent.connects == 1
        cause = refused.value.__cause__
        if stage == "final":
            # The existing count refusal stays primary, the cancellation is its attached cause.
            assert str(cause) == "migration child still has sessions"
            assert parent.exits == [AssertionError]
            cause = cause.__cause__
        else:
            # The first actual error is primary and is what the connection exit receives.
            assert parent.exits == [type(error)]
        assert cause is error
        snapshot = _diagnostic_receipt(tmp_path, child)["snapshots"][-1]
        assert snapshot["terminal"] is True and snapshot["status"] == "failed"
        assert snapshot["sqlstate"] == sqlstate

    @pytest.mark.parametrize("placement", ["exact", "embedded"])
    async def test_diag_free_form_canaries_are_never_serialized(self, placement):
        child = _diagnostic_child()

        def canary(legitimate, name):
            token = f"CANARY_{name}_SECRET_TOKEN"
            return token if placement == "exact" else f"{legitimate} {token}"

        parent = _DiagnosticParent(child, [1])
        owned = parent.owned_identity()
        extensions = [
            [canary("timescaledb", "EXTENSION"), canary("2.17.2", "EXTENSION_VERSION")],
            ["timescaledb", canary("2.17.2", "PASSWORD_VALUE")],
            [canary("plpgsql", "PRIVATE_EXTENSION"), "1.0"],
        ]
        identities = [
            owned[:5] + (canary("heatpump", "OWNER"),) + owned[6:7],
            owned[:7] + (canary("16.4", "SERVER_VERSION"),),
        ]
        identities = [
            identities[0] + owned[7:],
            identities[1] + (owned[8], json.dumps(extensions)),
            owned[:9] + (json.dumps({"CANARY_MAPPING_SECRET_TOKEN": "1.0"}),),
            owned[:9] + (json.dumps(["CANARY_FLAT_SECRET_TOKEN"]),),
        ]
        start = dt.datetime(2026, 10, 12, 21, 0, tzinfo=dt.timezone.utc)
        activity = (77, 4242, canary(child["database"], "DATNAME"), 10)
        activity += (canary("heatpump", "USER"), canary("client backend", "BACKEND_TYPE"))
        activity += (canary("", "APPLICATION"), canary("127.0.0.1", "ADDRESS"), 5000)
        activity += (start, None, start, start, canary("idle", "STATE"))
        activity += (canary("Client", "WAIT_EVENT_TYPE"), canary("ClientRead", "WAIT_EVENT"))
        serialized = [_diagnostic_identity(row, child) for row in identities]
        serialized.append(_diagnostic_activity(activity, child))
        serialized += [
            _diagnostic_value(kind, canary(legitimate, "VALUE"))
            for kind, legitimate in [
                ("event", "ClientRead"),
                ("event_type", "Lock"),
                ("extension", "timescaledb"),
                ("version", "2.17.2"),
                ("release", "16.4"),
                ("label", "idle"),
            ]
        ]
        assert "CANARY" not in json.dumps(serialized)
        assert serialized[1]["parent_server_version"] == (
            "unknown" if placement == "exact" else "16.4"
        )
        assert serialized[1]["parent_server_version_text"].startswith("redacted:sha256:")
        assert serialized[1]["parent_extensions"][1][0] == "timescaledb"
        assert serialized[2]["parent_extensions"] == "unknown"
        assert serialized[3]["parent_extensions"] == "unknown"
        # Legitimate evidence stays exact.
        legitimate = _diagnostic_identity(owned, child)
        assert legitimate["parent_server_version"] == "16.4"
        assert legitimate["parent_server_version_num"] == 160004
        assert legitimate["parent_extensions"] == [["plpgsql", "1.0"], ["timescaledb", "2.17.2"]]
        assert _diagnostic_value("version", "16.4") == "16.4"
        assert _diagnostic_value("release", "16.4.CANARY") == "unknown"
        assert _diagnostic_value("event_type", "Lock") == "Lock"
        assert _diagnostic_value("event", "Extension") == "Extension"
        assert _diagnostic_value("event", "transactionid") == "transactionid"

    @pytest.mark.parametrize("case", ["zero_sessions", "qualified", "receipt_unwritten"])
    async def test_clean_zero_or_qualified_scheduler_drops_once(self, tmp_path, monkeypatch, case):
        calls = _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        if case == "receipt_unwritten":
            monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path / "missing"))
        child = _diagnostic_child()
        zero = case == "zero_sessions"
        parent = _DiagnosticParent(child, [0] if zero else [1], rows=_scheduler_capture_rows(child))
        if zero:
            # The zero-session path never probes the raw connection nor issues a new query.
            parent.sync_connection = None
        if case == "receipt_unwritten":
            with pytest.raises(AssertionError, match="dropped; admission receipt") as raised:
                await _drop_migration_child(parent, child, [], (), 0)
            assert isinstance(raised.value.__cause__, FileNotFoundError)
        else:
            await _drop_migration_child(parent, child, [], (), 0)
        admission = [] if zero else [*DIAGNOSTIC_CAPTURE, *SCHEDULER_ADMISSION]
        # Exactly one ordinary DROP; no retry, FORCE, termination or reconnect.
        assert parent.kinds() == ["poll", *admission, "drop", "relations"]
        assert parent.connects == 1 and parent.exits == [None]
        assert calls.count("guard") == 1
        drop = 'DROP DATABASE "' + child["database"] + '"'
        for kind, sql, parameters in parent.statements:
            assert not re.search(r"(?i)\(\s*force|pg_terminate|pg_cancel|\bSET\b|\bALTER\b", sql)
            if kind == "drop":
                assert sql == drop
            if kind == "eligibility":
                assert parameters == {"child_oid": child["oid"]}
        if zero:
            assert list(tmp_path.iterdir()) == []
            return
        if case == "receipt_unwritten":
            return
        receipt = json.loads(
            (tmp_path / f"migration-child-cleanup-{child['database']}.json").read_text()
        )
        assert receipt["outcome"] == "scheduler_admitted_drop" and "error" not in receipt
        diagnostics = receipt["diagnostics"]
        assert diagnostics["scheduler_admissions"] == [
            {
                "stage": "first_nonzero",
                "queries": 3,
                "eligible": True,
                "sessions": 1,
                "facts": dict.fromkeys(SCHEDULER_FACTS, True),
                "drop": "completed",
            }
        ]
        assert diagnostics["connection_exit"] == {"cancelled": False, "status": "closed"}
        assert [s["stage"] for s in diagnostics["snapshots"]] == ["first_nonzero"]

    @pytest.mark.parametrize(
        "case",
        [
            "client_backend",
            "mixed",
            "application_spoof",
            "unknown_type",
            "raw_unknown_string",
            "wrong_role",
            "owner_oid_mismatch",
            "client_present",
            "transaction_present",
            "not_idle",
            "server_version",
            "timescaledb_version",
            "preload_missing",
            "profile_incomplete",
            "retained_resource",
        ],
    )
    async def test_clean_unqualified_sessions_keep_bounded_refusal(
        self, tmp_path, monkeypatch, case
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        start = dt.datetime(2026, 10, 12, 21, tzinfo=dt.timezone.utc)
        session = {
            "client_backend": {"backend_type": "client backend"},
            "unknown_type": {"backend_type": None},
            "raw_unknown_string": {"client_addr": "unknown", "xact_start": "unknown"},
            "wrong_role": {"usename": "postgres"},
            "owner_oid_mismatch": {"usesysid": 10},
            "client_present": {"client_addr": "127.0.0.1", "client_port": 50000},
            "transaction_present": {"xact_start": start},
            "not_idle": {"state": "active"},
        }.get(case, {})
        eligibility = [[_scheduler_session(child, **session)]]
        if case == "mixed":
            eligibility = [
                [
                    _scheduler_session(child, total=2),
                    _scheduler_session(child, total=2, backend_type="client backend"),
                ]
            ]
        # A client may set any application name; only the server's backend_type can qualify.
        spoof = case == "application_spoof"
        parent = _DiagnosticParent(
            child,
            [1],
            rows=_scheduler_capture_rows(child, "client backend")
            if spoof
            else _scheduler_capture_rows(child),
            eligibility=eligibility,
        )
        profile = {
            "server_version": {7: 160004},
            "timescaledb_version": {8: "2.17.2"},
            "preload_missing": {9: "pg_stat_statements"},
            "profile_incomplete": {8: None, 9: None},
        }.get(case)
        if profile:
            row = list(parent.scheduler_profile())
            for index, value in profile.items():
                row[index] = value
            parent.profiles = [tuple(row)] * 4
        retained = 1 if case == "retained_resource" else 0
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), retained)
        refusal = refused.value.__cause__
        assert str(refusal) == "migration child still has sessions" and refusal.__cause__ is None
        admission = [] if spoof else SCHEDULER_ADMISSION
        # The existing bounded poll is unchanged and nothing is ever dropped.
        assert parent.kinds() == [
            "poll",
            *DIAGNOSTIC_CAPTURE,
            *admission,
            "poll",
            "poll",
            *DIAGNOSTIC_CAPTURE,
            *admission,
        ]
        assert parent.connects == 1 and parent.exits == [AssertionError]
        attempts = _diagnostic_receipt(tmp_path, child).get("scheduler_admissions", [])
        if spoof:
            assert attempts == []
            return
        assert [a["stage"] for a in attempts] == ["first_nonzero", "final_refusal"]
        failing = {
            "client_backend": "scheduler",
            "mixed": "complete",
            "unknown_type": "scheduler",
            "raw_unknown_string": "no_client",
            "wrong_role": "owner",
            "owner_oid_mismatch": "owner",
            "client_present": "no_client",
            "transaction_present": "no_transaction",
            "not_idle": "idle",
            "server_version": "server_version_num",
            "timescaledb_version": "timescaledb_version",
            "preload_missing": "preload_timescaledb",
            "profile_incomplete": "preload_timescaledb",
            "retained_resource": "no_retained_resources",
        }[case]
        for attempt in attempts:
            assert attempt["eligible"] is False and attempt["facts"][failing] is False
            assert "drop" not in attempt and "terminal" not in attempt
        if case == "mixed":
            assert attempts[0]["sessions"] == "unknown"
        if case == "preload_missing":
            assert _preloads_timescaledb("pg_stat_statements, timescaledb")
            for value in ('"timescaledb"', "$libdir/timescaledb", "timescaledb-2.26.4", "", None):
                assert not _preloads_timescaledb(value)

    @pytest.mark.parametrize(
        "drift",
        [
            "oid_swap",
            "comment",
            "parent_sql",
            "final_swap",
            "parent_receipt_start",
            "parent_receipt_final",
        ],
    )
    async def test_clean_ownership_drift_before_drop_is_terminal(
        self, tmp_path, monkeypatch, drift
    ):
        # Parent receipt checks: two in cleanup, two per capture, then admission start/end.
        failure_at = {"parent_receipt_start": 5, "parent_receipt_final": 6}.get(drift)
        _diagnostic_guards(monkeypatch, tmp_path, polls=3, parent_failure_at=failure_at)
        child = _diagnostic_child()
        parent = _DiagnosticParent(child, [1], rows=_scheduler_capture_rows(child))
        owned = parent.scheduler_profile()
        swapped = {
            "oid_swap": owned[:3] + (9999,) + owned[4:],
            "comment": owned[:4] + ("another binding",) + owned[5:],
            "parent_sql": ("postgres",) + owned[1:],
            "final_swap": owned[:3] + (9999,) + owned[4:],
        }.get(drift)
        if drift == "final_swap":
            parent.profiles = [owned, swapped]
        elif swapped is not None:
            parent.profiles = [swapped]
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        receipt_drift = drift.startswith("parent_receipt")
        expected = (
            "migration child parent identity changed"
            if receipt_drift
            else "migration child ownership uncertain"
        )
        # The first actual terminal cause is primary; no later count refusal is invented.
        assert str(refused.value.__cause__) == expected
        assert parent.kinds() == ["poll", *DIAGNOSTIC_CAPTURE] + {
            "parent_receipt_start": [],
            "final_swap": SCHEDULER_ADMISSION,
            "parent_receipt_final": SCHEDULER_ADMISSION,
        }.get(drift, ["profile"])
        assert parent.connects == 1 and len(parent.exits) == 1
        [attempt] = _diagnostic_receipt(tmp_path, child)["scheduler_admissions"]
        assert attempt["terminal"] is True and attempt["eligible"] is False
        assert attempt["error"] == "AssertionError" and "drop" not in attempt

    async def test_clean_unsettled_worker_never_reaches_admission(self, tmp_path, monkeypatch):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        parent = _DiagnosticParent(child, [1], rows=_scheduler_capture_rows(child))
        pending = asyncio.get_running_loop().create_future()
        try:
            with pytest.raises(AssertionError, match="cleanup refused") as refused:
                await _drop_migration_child(parent, child, [pending], (), 0)
        finally:
            pending.cancel()
        assert "did not settle" in str(refused.value.__cause__)
        assert parent.connects == 0 and parent.statements == []
        assert "scheduler_admissions" not in _diagnostic_receipt(tmp_path, child)

    @pytest.mark.parametrize(
        "failure", ["server_refusal", "timeout", "invalidation", "cancellation"]
    )
    async def test_clean_admitted_drop_failure_is_final_and_reports_residue(
        self, tmp_path, monkeypatch, failure
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()
        raised = []

        def fail(fake):
            if failure == "server_refusal":
                # A client raced in: PostgreSQL's own count refuses (55006); nothing is killed.
                raised.append(_adapted_asyncpg_error("ObjectInUseError"))
            elif failure == "timeout":
                raised.append(TimeoutError("synthetic DROP timeout"))
            else:
                raised.append(
                    _adapted_asyncpg_error("ConnectionDoesNotExistError", invalidated=True)
                )
            return raised[-1]

        key = ("eligibility", 1) if failure == "invalidation" else ("drop", 1)
        blocked = asyncio.Event()
        parent = _DiagnosticParent(
            child,
            [1],
            rows=_scheduler_capture_rows(child),
            failures=None if failure == "cancellation" else {key: fail},
            blocks={key: blocked} if failure == "cancellation" else None,
        )
        if failure == "cancellation":
            runner = asyncio.ensure_future(_drop_migration_child(parent, child, [], (), 0))
            while "drop" not in parent.kinds():
                await asyncio.sleep(0)
            runner.cancel()
            with pytest.raises(AssertionError, match="cleanup refused") as refused:
                await runner
            cause = refused.value.__cause__
            assert isinstance(cause, asyncio.CancelledError) and not blocked.is_set()
        else:
            with pytest.raises(AssertionError, match="cleanup refused") as refused:
                await _drop_migration_child(parent, child, [], (), 0)
            cause = refused.value.__cause__
            assert cause is raised[0]
        admission = ["profile", "eligibility"]
        if failure != "invalidation":
            admission = [*SCHEDULER_ADMISSION, "drop"]
        # One DROP at most, never retried; nothing reconnects.
        assert parent.kinds() == ["poll", *DIAGNOSTIC_CAPTURE, *admission]
        assert parent.connects == 1 and parent.exits == [type(cause)]
        [attempt] = _diagnostic_receipt(tmp_path, child)["scheduler_admissions"]
        notes = getattr(cause, "__notes__", [])
        if failure == "invalidation":
            assert attempt["terminal"] is True and "drop" not in attempt and notes == []
            return
        assert attempt["eligible"] is True and attempt["drop"] == "not_confirmed"
        assert attempt["drop_error"] == type(cause).__name__
        assert attempt["residue"] == MIGRATION_CHILD_SCHEDULER_RESIDUE
        assert notes == [
            f"scheduler-admitted DROP not confirmed: {MIGRATION_CHILD_SCHEDULER_RESIDUE}"
        ]

    @pytest.mark.parametrize("evidence", ["redacted_values", "incomplete_capture"])
    async def test_clean_redacted_or_incomplete_evidence_cannot_authorize(
        self, tmp_path, monkeypatch, evidence
    ):
        _diagnostic_guards(monkeypatch, tmp_path, polls=3)
        child = _diagnostic_child()

        def server_error(fake):
            return _adapted_asyncpg_error("InsufficientPrivilegeError")

        redacted = _scheduler_session(
            child,
            backend_type=_diagnostic_digest(MIGRATION_CHILD_SCHEDULER),
            state=_diagnostic_value("label", None),
        )
        parent = _DiagnosticParent(
            child,
            [1],
            rows=_scheduler_capture_rows(child),
            eligibility=[[redacted]],
            failures=(
                {("activity", 1): server_error, ("activity", 2): server_error}
                if evidence == "incomplete_capture"
                else None
            ),
        )
        with pytest.raises(AssertionError, match="cleanup refused") as refused:
            await _drop_migration_child(parent, child, [], (), 0)
        assert str(refused.value.__cause__) == "migration child still has sessions"
        diagnostics = _diagnostic_receipt(tmp_path, child)
        if evidence == "incomplete_capture":
            # Without complete raw capture rows the admission recheck never even runs.
            assert parent.kinds() == ["poll", "identity", "activity", "poll", "poll"] + [
                "identity",
                "activity",
            ]
            assert "scheduler_admissions" not in diagnostics
            return
        assert parent.kinds() == [
            "poll",
            *DIAGNOSTIC_CAPTURE,
            *SCHEDULER_ADMISSION,
            "poll",
            "poll",
            *DIAGNOSTIC_CAPTURE,
            *SCHEDULER_ADMISSION,
        ]
        for attempt in diagnostics["scheduler_admissions"]:
            assert attempt["eligible"] is False
            assert attempt["facts"]["scheduler"] is False and attempt["facts"]["idle"] is False

    async def test_manual_reset_recreate_rejects_stale_pre_reset_revision(
        self, forward_manual_database, monkeypatch
    ):
        from fastapi import HTTPException, Response

        from packages.api.routers import admin, feeds
        from packages.api.schemas import ManualCOPPut

        child = forward_manual_database
        seeded, before, _retained = await self.reset_ready_child(child)
        manual = before["rows"]["manual_cop_observations"]
        stale = next(
            row
            for row in manual
            if row["device_id"] == seeded.devices[0] and row["granularity"] == "day"
        )
        period_date = dt.date.fromisoformat(stale["period_date"])
        observed_at = period_bounds("day", period_date, stale["timezone"])[1]
        monkeypatch.setattr(admin, "get_session", child.session_context)
        monkeypatch.setattr(feeds, "get_session", child.session_context)
        result = await admin.reset_manual_cop_ledger()
        assert result["deleted_rows"] == {"manual_cop_observations": seeded.manual}

        async def put(revision):
            response = Response()
            body = ManualCOPPut(
                expected_revision=str(revision),
                value=4.2,
                observed_at=observed_at,
                completeness="closed",
                timezone=stale["timezone"],
            )
            row = await feeds.put_manual_cop(stale["device_id"], "day", period_date, body, response)
            return response.status_code, row

        with pytest.raises(HTTPException) as conflict:
            await put(stale["revision"])
        assert conflict.value.status_code == 409
        status, created = await put(0)
        assert status == 201
        # The recreated row's opaque revision continues the sequence; never rewound.
        assert int(created.revision) > max(row["revision"] for row in manual)
        with pytest.raises(HTTPException) as conflict:
            await put(stale["revision"])
        assert conflict.value.status_code == 409
        status, updated = await put(created.revision)
        assert status == 200 and int(updated.revision) > int(created.revision)
        async with child.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT device_id, granularity, period_date, revision, value "
                        "FROM manual_cop_observations"
                    )
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            (stale["device_id"], "day", period_date, int(updated.revision), 4.2)
        ]


RESET_SETTLE_LIMIT_SECONDS = 30


async def drain_reset_task(task, limit):
    """Cancel and wait, bounded, for the reset task; outlast further cancellation.

    Returns the cancellation that interrupted the wait, if any, for re-raising later.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    interrupted = None
    if not task.done():
        task.cancel()
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as error:
            interrupted = interrupted or error
            task.cancel()
    return interrupted


def _raise_unless_reset_cancelled(task):
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        raise AssertionError("reset task completed instead of being cancelled")
    raise AssertionError("reset task failed instead of being cancelled") from error


async def cancel_blocked_reset(
    start, waiting, release, retain, *, attempts=60, interval=0.05, limit=RESET_SETTLE_LIMIT_SECONDS
):
    """Start a reset and prove it is blocked; on every exit cancel and drain it while the
    blocker still holds, and release the blocker only once the reset has settled.

    A reset still unsettled after the bounded drain is never released: it is handed to
    ``retain`` with the blocker, so the child fixture's teardown refuses cleanup.
    """
    task = asyncio.ensure_future(start())
    try:
        blocked = False
        # Poll well inside the reset's 5 s lock timeout.
        for _attempt in range(attempts):
            await asyncio.sleep(interval)
            blocked = await waiting()
            if blocked or task.done():
                break
        assert blocked and not task.done(), "reset did not reach the audit insert"
    finally:
        interrupted = await drain_reset_task(task, limit)
        if not task.done():
            retain(task)
            failure = AssertionError("reset task did not settle; its blocker stays held")
            failure.__cause__ = interrupted
            raise failure
        try:
            try:
                await release()
            except BaseException:
                # The release error stays attached as the context of any outcome failure.
                _raise_unless_reset_cancelled(task)
                raise
            _raise_unless_reset_cancelled(task)
        except BaseException as error:
            if interrupted is None:
                raise
            # A release error may already carry its own cause (e.g. the driver error), so
            # the drain's cancellation is kept as the cause of a new failure instead.
            raise AssertionError(
                f"{type(error).__name__} after the reset drain was cancelled"
            ) from interrupted
        if interrupted is not None:
            raise interrupted


SINGLE_INSERT_LIMIT_SECONDS = 20


def single_insert_rows(device, now):
    """91 day plus 91 month rows for one device, inserted by one unchanged statement."""
    values = []
    for index in range(91):
        period_date = dt.date(2026, 10, 3) - dt.timedelta(days=index)
        day = observation_values("day", period_date, "UTC")
        day.update(device_id=device, observed_at=period_bounds("day", period_date, "UTC")[1])
        month_index = 2026 * 12 + 9 - index
        year, zero_month = divmod(month_index, 12)
        month_date = dt.date(year, zero_month + 1, 1)
        month_start, month_end = period_bounds("month", month_date, "UTC")
        month = observation_values("month", month_date, "UTC")
        month.update(device_id=device, value=3.9, observed_at=month_end)
        if month_end > now:
            month.update(completeness="partial", observed_at=month_start)
        values.extend((day, month))
    return values


async def timed_single_insert(engine, values):
    """Run the single multi-row INSERT; return execution and commit durations."""
    async with engine.connect() as connection:
        started = time.perf_counter()
        await connection.execute(insert(ManualCOPObservationRecord), values)
        executed = time.perf_counter()
        await connection.commit()
        committed = time.perf_counter()
    return executed - started, committed - executed


@pytest.fixture
async def manual_api_database(monkeypatch):
    receipt = owned_test_environment()
    from packages.api.main import app
    from packages.api.routers import feeds
    from packages.core.config import settings

    url = make_url(os.environ["DATABASE_URL"])
    assert settings.api_token == "test-token"
    assert settings.app_environment == "test"
    assert settings.database_url == TEST_DATABASE_URL
    assert settings.redis_url == TEST_REDIS_URL
    assert settings.model_dir == receipt["model_root"]
    head, _ = migration_targets()
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"command_timeout": 20})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    device = "manual-api-" + uuid.uuid4().hex
    state = SimpleNamespace(opened=0, statements=[])

    def capture_statement(connection, cursor, statement, parameters, context, executemany):
        state.statements.append(statement)

    @asynccontextmanager
    async def session_context():
        state.opened += 1
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    monkeypatch.setattr(feeds, "get_session", session_context)
    monkeypatch.setattr(feeds, "get_bool_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(feeds, "get_user_tz", AsyncMock(return_value="UTC"))
    try:
        with owned_test_network():
            async with engine.begin() as connection:
                await guard_owned_database(connection)
                assert (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == head
                await connection.execute(
                    insert(DeviceStatusRecord).values(
                        ts=dt.datetime.now(dt.timezone.utc), device_id=device
                    )
                )
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                yield SimpleNamespace(
                    engine=engine,
                    client=client,
                    device=device,
                    state=state,
                    factory=factory,
                    session_context=session_context,
                    model_root=Path(receipt["model_root"]),
                )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)
        await engine.dispose()


class TestManualCOPPostgreSQLAPI:
    def body(self, granularity, period_date, timezone="UTC", revision="0"):
        values = observation_values(granularity, period_date, timezone)
        return {
            "value": values["value"],
            "observed_at": values["observed_at"].isoformat(),
            "completeness": values["completeness"],
            "timezone": timezone,
            "expected_revision": revision,
        }

    @pytest.mark.parametrize("timezone", ["UTC", "US/Eastern", "Etc/UTC", "Asia/Calcutta"])
    @pytest.mark.parametrize("granularity", ["day", "month"])
    async def test_exact_keys_commit_and_invalid_case_never_opens_sql(
        self, manual_api_database, timezone, granularity
    ):
        database = manual_api_database
        period_date = dt.date(2020, 10, 1)
        path = f"/api/cop/manual/{database.device}/{granularity}/{period_date}"
        body = self.body(granularity, period_date, timezone)
        for invalid in ("utc", "US/eastern"):
            database.state.opened = 0
            database.state.statements.clear()
            response = await database.client.put(path, json=body | {"timezone": invalid})
            assert response.status_code == 422
            assert database.state.opened == 0
            assert database.state.statements == []
        created = await database.client.put(path, json=body)
        assert created.status_code == 201
        assert created.json()["value"] == 9.7
        assert created.json()["timezone"] == timezone
        assert created.json()["source"] == "controller_estimate"
        assert created.json()["measured"] is False
        revision = created.json()["revision"]
        assert isinstance(revision, str)
        updated = await database.client.put(path, json=body | {"expected_revision": revision})
        assert updated.status_code == 200
        assert int(updated.json()["revision"]) > int(revision)
        async with database.engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(ManualCOPObservationRecord.__table__).where(
                        ManualCOPObservationRecord.device_id == database.device
                    )
                )
            ).one()
        assert stored.timezone == timezone
        assert not {"period_start", "period_end"} & set(stored._fields)
        snapshot = snapshot_from_stored(stored)
        assert (snapshot.period_start, snapshot.period_end) == period_bounds(
            granularity, period_date, timezone
        )
        assert str(stored.revision) == updated.json()["revision"]

    async def test_concurrent_tokens_rollback_and_recreation_never_allow_aba(
        self, manual_api_database
    ):
        database = manual_api_database
        period_date = dt.date(2020, 10, 3)
        path = f"/api/cop/manual/{database.device}/day/{period_date}"
        body = self.body("day", period_date)
        async with database.engine.begin() as connection:
            previous = (
                await connection.execute(text("SELECT last_value FROM manual_cop_revision_seq"))
            ).scalar_one()
            assert previous < 9007199254740993
            await connection.execute(
                text("SELECT setval('manual_cop_revision_seq',9007199254740993,false)")
            )
        created = await database.client.put(path, json=body)
        assert created.status_code == 201
        original = created.json()["revision"]
        assert original == "9007199254740993"
        assert (await database.client.put(path, json=body)).status_code == 409
        responses = await asyncio.gather(
            *(
                database.client.put(
                    path, json=body | {"value": value, "expected_revision": original}
                )
                for value in (9.8, 9.9)
            )
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        current = next(
            response.json()["revision"] for response in responses if response.status_code == 200
        )
        stale = await database.client.request("DELETE", path, json={"expected_revision": original})
        assert stale.status_code == 409
        deleted = await database.client.request("DELETE", path, json={"expected_revision": current})
        assert deleted.status_code == 204
        recreated = await database.client.put(path, json=body)
        assert recreated.status_code == 201
        token = recreated.json()["revision"]
        assert int(token) > int(current) > int(original)
        for revision in (original, current):
            assert (
                await database.client.put(path, json=body | {"expected_revision": revision})
            ).status_code == 409
            assert (
                await database.client.request("DELETE", path, json={"expected_revision": revision})
            ).status_code == 409
        corrected_body = self.body("day", period_date, "US/Eastern", token)
        corrected = await database.client.put(path, json=corrected_body)
        assert corrected.status_code == 200
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            burned = (
                await connection.execute(text("SELECT nextval('manual_cop_revision_seq')"))
            ).scalar_one()
            await transaction.rollback()
        committed = await database.client.put(
            path, json=corrected_body | {"expected_revision": corrected.json()["revision"]}
        )
        assert committed.status_code == 200
        assert int(committed.json()["revision"]) > burned
        async with database.engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(ManualCOPObservationRecord.__table__).where(
                        ManualCOPObservationRecord.device_id == database.device
                    )
                )
            ).one()
            assert stored.timezone == "US/Eastern"
            snapshot = snapshot_from_stored(stored)
            assert (snapshot.period_start, snapshot.period_end) == period_bounds(
                "day", period_date, "US/Eastern"
            )
        unknown = await database.client.put(
            f"/api/cop/manual/unknown-{uuid.uuid4().hex}/day/{period_date}", json=body
        )
        assert unknown.status_code == 404

    async def test_committed_day_and_month_pages_are_independent_and_bounded(
        self, manual_api_database, monkeypatch
    ):
        from packages.api.routers import feeds

        database = manual_api_database
        now = dt.datetime.now(dt.timezone.utc)
        # DBNEW3: one cold then three fresh-device trials of the same single 182-row
        # INSERT, with execution and commit timed separately. The first trial's rows
        # are the ones paginated below.
        trials = []
        evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"]) / (
            f"dbnew3-single-insert-{database.device}.json"
        )
        try:
            for trial in range(4):
                device = database.device if trial == 0 else f"{database.device}-trial{trial}"
                values = single_insert_rows(device, now)
                assert len(values) == 182
                execute_seconds, commit_seconds = await timed_single_insert(database.engine, values)
                trials.append(
                    {
                        "trial": trial,
                        "kind": "cold" if trial == 0 else "fresh-device",
                        "device": device,
                        "rows": len(values),
                        "execute_seconds": execute_seconds,
                        "commit_seconds": commit_seconds,
                    }
                )
        finally:
            evidence.write_text(
                json.dumps(
                    {"limit_seconds": SINGLE_INSERT_LIMIT_SECONDS, "trials": trials}, indent=2
                ),
                encoding="utf-8",
            )
        assert [record["trial"] for record in trials] == [0, 1, 2, 3]
        for record in trials:
            total = record["execute_seconds"] + record["commit_seconds"]
            assert total < SINGLE_INSERT_LIMIT_SECONDS, record
        read_clock = Mock(wraps=dt.datetime)
        read_clock.now.return_value = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
        monkeypatch.setattr(feeds, "dt", SimpleNamespace(**(vars(dt) | {"datetime": read_clock})))
        monitoring_counts = []
        for granularity in ("day", "month"):
            query = {"device_id": database.device}
            if granularity == "month":
                query["granularity"] = granularity
            first = await database.client.get("/api/cop/manual", params=query)
            assert first.status_code == 200
            page = first.json()
            assert page["granularity"] == granularity
            assert len(page["observations"]) == 90
            assert page["next_before"] == page["observations"][-1]["period_date"]
            assert {row["granularity"] for row in page["observations"]} == {granularity}
            assert "2026-09-01" in {row["period_date"] for row in page["observations"]}
            second = await database.client.get(
                "/api/cop/manual", params=query | {"before": page["next_before"]}
            )
            assert second.status_code == 200
            tail = second.json()
            assert len(tail["observations"]) == 1
            assert tail["next_before"] is None
            dates = [row["period_date"] for row in page["observations"] + tail["observations"]]
            assert len(set(dates)) == 91
            assert dates == sorted(dates, reverse=True)
            for snapshot in (page, tail):
                assert snapshot["monitoring"]["status"] == "disabled"
                assert snapshot["monitoring"]["forecast"] is None
                monitoring_counts.append(snapshot["monitoring"]["counts"]["daily_rows"])
        assert monitoring_counts == [27] * 4

    @pytest.mark.parametrize(
        "reset_models", [False, True], ids=["energy-only", "energy-and-models"]
    )
    async def test_committed_energy_and_model_reset_preserve_retained_storage(
        self, manual_api_database, monkeypatch, reset_models
    ):
        from packages.api.routers import admin
        from packages.ml.models_common import MODEL_DIR

        database = manual_api_database
        assert MODEL_DIR == database.model_root
        assert not list(database.model_root.iterdir())
        monkeypatch.setattr(admin, "get_session", database.session_context)
        period_date = dt.date(2020, 10, 3)
        path = f"/api/cop/manual/{database.device}/day/{period_date}"
        body = self.body("day", period_date)
        created = await database.client.put(path, json=body)
        assert created.status_code == 201
        revision = created.json()["revision"]
        async with database.engine.begin() as connection:
            await connection.execute(
                insert(ConsumptionRecord).values(
                    ts=dt.datetime.now(dt.timezone.utc),
                    device_id=database.device,
                    heat_kwh=1.7,
                    cool_kwh=0,
                    tank_kwh=0.5,
                )
            )
        async with database.engine.connect() as connection:
            retained = await retained_storage_state(connection)
            consumption_count = (
                await connection.execute(text("SELECT count(*) FROM consumption"))
            ).scalar_one()
            assert consumption_count > 0
        response = await database.client.post(
            "/api/admin/reset", json={"scopes": ["energy"], "reset_models": reset_models}
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "scopes": ["energy"],
            "deleted_rows": {"consumption": consumption_count},
            "total_rows_deleted": consumption_count,
            "models_reset": reset_models,
            "deleted_models": [],
            "model_lifecycle": None,
        }
        async with database.engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT count(*) FROM consumption"))
            ).scalar_one() == 0
            assert await retained_storage_state(connection) == retained
        assert not list(database.model_root.iterdir())
        updated = await database.client.put(path, json=body | {"expected_revision": revision})
        assert updated.status_code == 200
        assert int(updated.json()["revision"]) > int(revision)
        assert int(updated.json()["revision"]) > retained["sequence"].last_value

    async def test_sequence_exhaustion_is_503_and_never_wraps(self, manual_api_database):
        database = manual_api_database
        maximum = 9223372036854775807
        async with database.engine.begin() as connection:
            assert (
                await connection.execute(text("SELECT last_value FROM manual_cop_revision_seq"))
            ).scalar_one() < maximum
            await connection.execute(
                text("SELECT setval('manual_cop_revision_seq',9223372036854775807,false)")
            )
        period_date = dt.date(2020, 10, 3)
        body = self.body("day", period_date)
        response = await database.client.put(
            f"/api/cop/manual/{database.device}/day/{period_date}", json=body
        )
        assert response.status_code == 201
        assert response.json()["revision"] == str(maximum)
        next_date = period_date + dt.timedelta(days=1)
        failed = await database.client.put(
            f"/api/cop/manual/{database.device}/day/{next_date}",
            json=self.body("day", next_date),
        )
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Manual COP storage unavailable"}
        async with database.engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT cycle,max_value,last_value FROM pg_sequences WHERE sequencename='manual_cop_revision_seq'"
                    )
                )
            ).one()
        assert state == (False, maximum, maximum)


if __name__ == "__main__":
    operation, *arguments = sys.argv[1:]
    if operation == "prepare":
        verification_preflight(prepare_owned_test_profile())
    elif operation == "prepare-unit":
        root = Path(os.environ["MODEL_DIR"]).parent
        assert root.parent == Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        assert (
            root.name
            == f"hpo-unit-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        )
        root.mkdir(mode=0o700)
        (root / "models").mkdir(mode=0o700)
        (root / "evidence").mkdir(mode=0o700)
        digest = write_verification_source_manifest(root / "evidence", os.environ["GITHUB_SHA"])
        os.environ["COP_MANUAL_TEST_EVIDENCE"] = str(root / "evidence")
        os.environ["COP_MANUAL_TEST_REVISION"] = os.environ["GITHUB_SHA"]
        os.environ[SOURCE_MANIFEST_DIGEST_ENV] = digest
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as environment_file:
            environment_file.write(f"COP_MANUAL_TEST_EVIDENCE={root / 'evidence'}\n")
            environment_file.write(f"COP_MANUAL_TEST_REVISION={os.environ['GITHUB_SHA']}\n")
            environment_file.write(f"{SOURCE_MANIFEST_DIGEST_ENV}={digest}\n")
        verification_preflight(root, owned_sql=False)
    elif operation == "phase":
        name, *command_arguments = arguments
        run_verification_phase(name, [sys.executable, *command_arguments])
    elif operation == "unit-phase":
        name, *command_arguments = arguments
        run_verification_phase(name, [sys.executable, *command_arguments], owned_sql=False)
    elif operation == "primary-head":
        (label,) = arguments
        run_primary_head_upgrade(label)
    elif operation == "seal":
        seal_verification_evidence()
    else:
        raise AssertionError("unknown disposable verification operation")

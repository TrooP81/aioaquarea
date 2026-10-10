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
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import tomllib
import uuid
from contextlib import asynccontextmanager, contextmanager
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.cop_monitoring import local_instants, period_bounds
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


async def retained_storage_state(connection):
    state = {
        "rows": tuple(
            (
                await connection.execute(
                    select(ManualCOPObservationRecord.__table__).order_by(
                        *ManualCOPObservationRecord.__table__.primary_key.columns
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
        "helper": (
            await connection.execute(
                text(
                    "SELECT pg_get_functiondef(oid),prosrc,proacl,pg_get_userbyid(proowner) "
                    "FROM pg_proc WHERE oid="
                    "'public.manual_cop_calendar_midnight(timestamp,text)'::regprocedure"
                )
            )
        ).one(),
        "marker": tuple(
            (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
            )
            .scalars()
            .all()
        ),
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
        now = dt.datetime.now(dt.timezone.utc)
        created = _utc_timestamp(receipt["created_at"])
        expires = _utc_timestamp(receipt["expires_at"])
        assert created <= now < expires, "ownership receipt is stale"
        assert 0 < (expires - created).total_seconds() <= 1800, "unbounded ownership lease"
        provider = receipt["provider"]
        assert provider in {"github", "temp"}, "unknown ownership provider"
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


def verification_source_binding():
    checkout = Path(__file__).resolve().parents[3]
    paths = (
        ".github/workflows/optimizer-ci.yml",
        "heatpump-optimizer/tests/e2e/conftest.py",
        "heatpump-optimizer/tests/e2e/test_manual_cop_postgresql.py",
        "heatpump-optimizer/tests/test_verify_backup.py",
        "heatpump-optimizer/README.md",
        "heatpump-optimizer/migrations/versions/032_manual_cop_observations.py",
        "heatpump-optimizer/tests/conftest.py",
    )
    return {path: hashlib.sha256((checkout / path).read_bytes()).hexdigest() for path in paths}


def prepare_owned_test_profile():
    provider = "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "temp"
    context = json.loads(os.environ["COP_MANUAL_TEST_SERVICE_CONTEXT"])
    assert set(context) == {"postgres", "redis"}, "unknown job service"
    assert context["postgres"]["network"] == context["redis"]["network"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", service["id"]) for service in context.values())
    if provider == "github":
        token = f"cop-manual-mon-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        temporary_parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        revision = os.environ["GITHUB_SHA"]
    else:
        token = "cop-manual-mon-" + os.environ["COP_MANUAL_TEST_LEASE_ID"]
        temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
        revision = os.environ["COP_MANUAL_TEST_REVISION"]
    root = temporary_parent / token
    assert os.environ["COP_MANUAL_TEST_OWNED"] == token
    assert Path(os.environ["MODEL_DIR"]) == root / "models", "model root must be set before imports"
    assert make_url(os.environ["DATABASE_URL"]) == make_url(TEST_DATABASE_URL)
    assert make_url(os.environ["REDIS_URL"]) == make_url(TEST_REDIS_URL)
    root.mkdir(mode=0o700)
    (root / "models").mkdir(mode=0o700)
    (root / "evidence").mkdir(mode=0o700)
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


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    if os.environ.get("COP_MANUAL_TEST_PHASE"):
        config._cop_verification_nodes = [item.nodeid for item in items]


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
        "source": verification_source_binding(),
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
    (root / "evidence" / "preflight.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "versions": versions,
                "source": verification_source_binding(),
                "tzdata": importlib.metadata.version("tzdata"),
                "executable_tmp": True,
            }
        ),
        encoding="utf-8",
    )


def seal_verification_evidence():
    evidence = Path(os.environ["COP_MANUAL_TEST_EVIDENCE"])
    inventory = json.loads((evidence / "manual-inventory/nodes.json").read_text(encoding="utf-8"))
    expected = set(inventory["collected"])
    phases = []
    for name in ("migration", "manual", "exhaustion"):
        nodes = json.loads((evidence / name / "nodes.json").read_text(encoding="utf-8"))
        process = json.loads((evidence / name / "process.json").read_text(encoding="utf-8"))
        assert nodes["exit"] == process["exit"] == 0 and not process["alive"]
        selected = set(nodes["selected"])
        assert selected and not any(selected & previous for previous in phases)
        reports = [
            json.loads(line)
            for line in (evidence / name / "reports.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert all(
            report["outcome"] == "passed" and not report["wasxfail"] for report in reports
        ), "manual skip/failure is forbidden"
        assert {report["nodeid"] for report in reports if report["when"] == "call"} == selected
        phases.append(selected)
    assert set.union(*phases) == expected, "manual phase inventory is incomplete"
    ordinary = json.loads((evidence / "ordinary/nodes.json").read_text(encoding="utf-8"))
    ordinary_process = json.loads((evidence / "ordinary/process.json").read_text(encoding="utf-8"))
    assert set(ordinary["selected"]) == set(ordinary["collected"])
    assert not expected & set(ordinary["selected"])
    assert ordinary["exit"] == ordinary_process["exit"] == 0 and not ordinary_process["alive"]
    reports = [
        json.loads(line)
        for line in (evidence / "ordinary/reports.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(report["outcome"] == "passed" and not report["wasxfail"] for report in reports), (
        "ordinary E2E skip/failure is forbidden"
    )
    assert {report["nodeid"] for report in reports if report["when"] == "call"} == set(
        ordinary["selected"]
    )
    processes = [
        json.loads(path.read_text(encoding="utf-8")) for path in evidence.glob("*/process.json")
    ]
    assert all(process["exit"] == 0 and not process["alive"] for process in processes)
    binding = verification_source_binding()
    assert all(process["source"] == binding for process in processes), "verification source drift"
    (evidence / "seal.json").write_text(
        json.dumps(
            {
                "manual_nodes": sorted(expected),
                "ordinary_nodes": ordinary["selected"],
                "source": binding,
                "phases": processes,
                "sealed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


class TestOwnedResourceGuard:
    def environment(self, tmp_path, provider):
        now = dt.datetime.now(dt.timezone.utc)
        lease = uuid.uuid4().hex
        token = "cop-manual-mon-123-2" if provider == "github" else "cop-manual-mon-" + lease
        root = tmp_path / token
        root.mkdir()
        (root / "models").mkdir()
        (root / "evidence").mkdir()
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
            "revision": "4" * 40,
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
            "COP_MANUAL_TEST_LEASE_ID": lease,
            "COP_MANUAL_TEST_EVIDENCE": str(root / "evidence"),
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_JOB": "backend-e2e",
            "GITHUB_SHA": receipt["revision"],
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
        ],
    )
    async def test_guard_refuses_before_mutation(self, tmp_path, case):
        provider = "temp" if case == "published_temp_port" else "github"
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
        with pytest.raises(AssertionError):
            await guard_owned_database(
                connection,
                require_empty=True,
                environment=environment,
                platform_reader=Mock(return_value=actual),
            )
            await mutation()
        mutation.assert_not_awaited()
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


@pytest.fixture(params=("migration", "metadata"))
async def calendar_database(request):
    owned_test_environment()
    url = make_url(os.environ["DATABASE_URL"])
    head, _ = migration_targets()
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
                    helper_query = text(
                        "SELECT prosrc FROM pg_proc WHERE oid = "
                        "'public.manual_cop_calendar_midnight(timestamp,text)'::regprocedure"
                    )
                    migration_helper = (await connection.execute(helper_query)).scalar_one()
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
                    assert any(
                        "CREATE OR REPLACE FUNCTION public.manual_cop_calendar_midnight"
                        in statement
                        for statement in statements
                    )
                    metadata_helper = (await connection.execute(helper_query)).scalar_one()
                    assert migration_helper.split() == metadata_helper.split()
                yield connection, table_name
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def observation_values(granularity, period_date, timezone):
    start, end = period_bounds(granularity, period_date, timezone)
    return {
        "device_id": "calendar-" + uuid.uuid4().hex,
        "granularity": granularity,
        "period_date": period_date,
        "timezone": timezone,
        "period_start": start,
        "period_end": end,
        "value": 9.7,
        "observed_at": end + dt.timedelta(days=2),
        "completeness": "closed",
    }


async def insert_observation(connection, table_name, values):
    identifier = connection.dialect.identifier_preparer.quote(table_name)
    await connection.execute(
        text(
            f"INSERT INTO {identifier} "
            "(device_id, granularity, period_date, timezone, period_start, "
            "period_end, value, observed_at, completeness) VALUES "
            "(:device_id, :granularity, :period_date, :timezone, :period_start, "
            ":period_end, :value, :observed_at, :completeness)"
        ),
        values,
    )


async def canonical_midnight(connection, local_midnight, timezone):
    return (
        await connection.execute(
            text("SELECT public.manual_cop_calendar_midnight(:local_midnight, :timezone)"),
            {"local_midnight": local_midnight, "timezone": timezone},
        )
    ).scalar_one()


class TestManualCOPPostgreSQLCalendar:
    @pytest.mark.parametrize(
        ("period_date", "boundary"),
        [
            (dt.date(2025, 11, 2), "period_start"),
            (dt.date(2025, 11, 1), "period_end"),
        ],
    )
    async def test_sql_rejects_later_ambiguous_midnight(
        self, calendar_database, period_date, boundary
    ):
        connection, table_name = calendar_database
        timezone = "America/Havana"
        start, end = period_bounds("day", period_date, timezone)
        values = {
            "device_id": "ambiguous-" + uuid.uuid4().hex,
            "granularity": "day",
            "period_date": period_date,
            "timezone": timezone,
            "period_start": start,
            "period_end": end,
            "value": 9.7,
            "observed_at": end + dt.timedelta(hours=1),
            "completeness": "closed",
        }
        assert values[boundary].astimezone(ZoneInfo(timezone)).fold == 0
        values[boundary] += dt.timedelta(hours=1)
        later_midnight = values[boundary].astimezone(ZoneInfo(timezone))
        assert later_midnight.fold == 1
        assert later_midnight.time() == dt.time()
        with pytest.raises(IntegrityError, match="ck_manual_cop_calendar") as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

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
    async def test_sql_accepts_only_canonical_start_and_end(
        self, calendar_database, granularity, period_date, timezone
    ):
        connection, table_name = calendar_database
        values = observation_values(granularity, period_date, timezone)
        zone = ZoneInfo(timezone)
        expected_hours = {
            ("day", dt.date(1969, 9, 30), "Pacific/Kwajalein"): 47,
            ("day", dt.date(1892, 7, 4), "Pacific/Apia"): 48,
        }.get((granularity, period_date, timezone))
        if expected_hours is not None:
            assert (values["period_end"] - values["period_start"]).total_seconds() == (
                expected_hours * 3600
            )
        for boundary in ("period_start", "period_end"):
            local_midnight = values[boundary].astimezone(zone).replace(tzinfo=None)
            assert (
                await canonical_midnight(connection, local_midnight, timezone) == values[boundary]
            )
            instants = local_instants(local_midnight, zone)
            assert instants[0] == values[boundary]
            if len(instants) == 2:
                invalid = values | {
                    "device_id": "later-" + uuid.uuid4().hex,
                    boundary: instants[1],
                }
                with pytest.raises(IntegrityError, match="ck_manual_cop_calendar") as rejected:
                    async with connection.begin_nested():
                        await insert_observation(connection, table_name, invalid)
                assert rejected.value.orig.sqlstate == "23514"
        async with connection.begin_nested():
            await insert_observation(connection, table_name, values)
        identifier = connection.dialect.identifier_preparer.quote(table_name)
        stored = (
            await connection.execute(
                text(
                    f"SELECT period_start, period_end FROM {identifier} WHERE device_id=:device_id"
                ),
                {"device_id": values["device_id"]},
            )
        ).one()
        assert stored == (values["period_start"], values["period_end"])

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
    async def test_sql_rejects_nonexistent_start_or_end(
        self, calendar_database, granularity, period_date, timezone
    ):
        connection, table_name = calendar_database
        zone = ZoneInfo(timezone)
        end_date = (
            period_date + dt.timedelta(days=1)
            if granularity == "day"
            else dt.date(period_date.year, period_date.month + 1, 1)
        )
        local_boundaries = [
            dt.datetime.combine(boundary, dt.time()) for boundary in (period_date, end_date)
        ]
        assert any(not local_instants(boundary, zone) for boundary in local_boundaries)
        for local_midnight in local_boundaries:
            instants = local_instants(local_midnight, zone)
            assert await canonical_midnight(connection, local_midnight, timezone) == (
                instants[0] if instants else None
            )
        with pytest.raises(ValueError, match="nonexistent local midnight"):
            period_bounds(granularity, period_date, timezone)
        start, end = [
            boundary.replace(tzinfo=zone, fold=0).astimezone(dt.timezone.utc)
            for boundary in local_boundaries
        ]
        if start >= end:
            start = end - dt.timedelta(days=1)
        values = {
            "device_id": "nonexistent-" + uuid.uuid4().hex,
            "granularity": granularity,
            "period_date": period_date,
            "timezone": timezone,
            "period_start": start,
            "period_end": end,
            "value": 9.7,
            "observed_at": end + dt.timedelta(days=2),
            "completeness": "closed",
        }
        with pytest.raises(IntegrityError, match="ck_manual_cop_calendar") as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

    @pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
    async def test_sql_rejects_nonpositive_and_nonfinite_values(self, calendar_database, value):
        connection, table_name = calendar_database
        values = observation_values("day", dt.date(2020, 10, 3), "UTC") | {"value": value}
        with pytest.raises(IntegrityError, match="ck_manual_cop_value") as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

    async def test_sql_rejects_invalid_timezone(self, calendar_database):
        connection, table_name = calendar_database
        values = observation_values("day", dt.date(2020, 10, 3), "UTC") | {
            "timezone": "Invalid/Manual"
        }
        with pytest.raises(DBAPIError, match='time zone "Invalid/Manual" not recognized'):
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)

    @pytest.mark.parametrize(
        "timezone", ["UTC+03", "europe/stockholm", "europe/Stockholm", "us/Eastern"]
    )
    async def test_sql_rejects_postgresql_timezone_strings_unloadable_by_zoneinfo(
        self, calendar_database, timezone
    ):
        from zoneinfo import ZoneInfoNotFoundError

        connection, table_name = calendar_database
        period = dt.date(2020, 10, 3)
        with pytest.raises(ZoneInfoNotFoundError):
            ZoneInfo(timezone)
        with pytest.raises(ValueError, match="Invalid timezone"):
            period_bounds("day", period, timezone)
        local_start = dt.datetime.combine(period, dt.time())
        local_end = local_start + dt.timedelta(days=1)
        start, end = (
            await connection.execute(
                text(
                    "SELECT :local_start AT TIME ZONE :timezone, :local_end AT TIME ZONE :timezone"
                ),
                {"local_start": local_start, "local_end": local_end, "timezone": timezone},
            )
        ).one()
        assert start < end
        values = {
            "device_id": "non-iana-" + uuid.uuid4().hex,
            "granularity": "day",
            "period_date": period,
            "timezone": timezone,
            "period_start": start,
            "period_end": end,
            "value": 9.7,
            "observed_at": end + dt.timedelta(days=1),
            "completeness": "closed",
        }
        with pytest.raises(IntegrityError) as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

    @pytest.mark.parametrize("timezone", ["utc", "US/eastern"])
    async def test_sql_rejects_non_catalog_case_spellings(self, calendar_database, timezone):
        connection, table_name = calendar_database
        assert (
            await connection.execute(
                text(
                    'SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name COLLATE "C" = :timezone COLLATE "C")'
                ),
                {"timezone": timezone},
            )
        ).scalar_one() is False
        period = dt.date(2020, 10, 3)
        local_start = dt.datetime.combine(period, dt.time())
        local_end = local_start + dt.timedelta(days=1)
        start, end = (
            await connection.execute(
                text(
                    "SELECT :local_start AT TIME ZONE :timezone, :local_end AT TIME ZONE :timezone"
                ),
                {"local_start": local_start, "local_end": local_end, "timezone": timezone},
            )
        ).one()
        assert start < end
        assert await canonical_midnight(connection, local_start, timezone) is None
        values = {
            "device_id": "non-catalog-" + uuid.uuid4().hex,
            "granularity": "day",
            "period_date": period,
            "timezone": timezone,
            "period_start": start,
            "period_end": end,
            "value": 9.7,
            "observed_at": end + dt.timedelta(days=1),
            "completeness": "closed",
        }
        with pytest.raises(IntegrityError, match="ck_manual_cop_calendar") as rejected:
            async with connection.begin_nested():
                await insert_observation(connection, table_name, values)
        assert rejected.value.orig.sqlstate == "23514"

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

    async def test_sql_rejects_catalog_pseudozones(self, calendar_database):
        connection, table_name = calendar_database
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
        period = dt.date(2020, 10, 3)
        local_start = dt.datetime.combine(period, dt.time())
        local_end = local_start + dt.timedelta(days=1)
        for timezone in names:
            start, end = (
                await connection.execute(
                    text(
                        "SELECT :local_start AT TIME ZONE :timezone, "
                        ":local_end AT TIME ZONE :timezone"
                    ),
                    {"local_start": local_start, "local_end": local_end, "timezone": timezone},
                )
            ).one()
            assert start < end
            assert await canonical_midnight(connection, local_start, timezone) is None
            values = {
                "device_id": "pseudozone-" + uuid.uuid4().hex,
                "granularity": "day",
                "period_date": period,
                "timezone": timezone,
                "period_start": start,
                "period_end": end,
                "value": 9.7,
                "observed_at": end + dt.timedelta(days=1),
                "completeness": "closed",
            }
            with pytest.raises(IntegrityError, match="ck_manual_cop_calendar") as rejected:
                async with connection.begin_nested():
                    await insert_observation(connection, table_name, values)
            assert rejected.value.orig.sqlstate == "23514"


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
                            optimizer_version="rules_v7",
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
                values["observed_at"] = values["period_end"]
                await insert_observation(connection, "manual_cop_observations", values)
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
            next_native_position = 1 + max(
                column[2] for column in retained_legacy["columns"] if column[0] == "device_status"
            )
            for operation, target in (
                (command.upgrade, head),
                (command.downgrade, "032"),
                (command.upgrade, head),
            ):
                await asyncio.to_thread(operation, config, target)
                async with engine.connect() as connection:
                    assert await retained_storage_state(connection) == retained | {
                        "marker": (target,)
                    }
                    expected_legacy = copy.deepcopy(retained_legacy)
                    if target == head and head != "032":
                        expected_legacy["columns"] = tuple(
                            sorted(
                                (
                                    *expected_legacy["columns"],
                                    (
                                        "device_status",
                                        "native_policy_provenance",
                                        next_native_position,
                                        "jsonb",
                                        False,
                                        None,
                                    ),
                                ),
                                key=lambda column: (column[0], column[2]),
                            )
                        )
                        expected_legacy["raw_rows"]["device_status"] = tuple(
                            row | {"native_policy_provenance": None}
                            for row in expected_legacy["raw_rows"]["device_status"]
                        )
                    assert await legacy_state(connection) == expected_legacy
                    if target == head and head != "032":
                        next_native_position += 1
                if target == head:
                    await asyncio.to_thread(command.check, config)
        finally:
            await engine.dispose()


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
        assert (stored.period_start, stored.period_end) == period_bounds(
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
            assert (stored.period_start, stored.period_end) == period_bounds(
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
        values = []
        now = dt.datetime.now(dt.timezone.utc)
        for index in range(91):
            period_date = dt.date(2026, 10, 3) - dt.timedelta(days=index)
            day = observation_values("day", period_date, "UTC")
            day.update(device_id=database.device, observed_at=day["period_end"])
            month_index = 2026 * 12 + 9 - index
            year, zero_month = divmod(month_index, 12)
            month = observation_values("month", dt.date(year, zero_month + 1, 1), "UTC")
            month.update(device_id=database.device, value=3.9, observed_at=month["period_end"])
            if month["period_end"] > now:
                month.update(completeness="partial", observed_at=month["period_start"])
            values.extend((day, month))
        async with database.engine.begin() as connection:
            await connection.execute(insert(ManualCOPObservationRecord), values)
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
        os.environ["COP_MANUAL_TEST_EVIDENCE"] = str(root / "evidence")
        os.environ["COP_MANUAL_TEST_REVISION"] = os.environ["GITHUB_SHA"]
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as environment_file:
            environment_file.write(f"COP_MANUAL_TEST_EVIDENCE={root / 'evidence'}\n")
            environment_file.write(f"COP_MANUAL_TEST_REVISION={os.environ['GITHUB_SHA']}\n")
        verification_preflight(root, owned_sql=False)
    elif operation == "phase":
        name, *command_arguments = arguments
        run_verification_phase(name, [sys.executable, *command_arguments])
    elif operation == "unit-phase":
        name, *command_arguments = arguments
        run_verification_phase(name, [sys.executable, *command_arguments], owned_sql=False)
    elif operation == "seal":
        seal_verification_evidence()
    else:
        raise AssertionError("unknown disposable verification operation")

"""Offline regressions for the legacy synthetic workflow's real receipt producer."""

from __future__ import annotations

import copy
import datetime as dt
import json
import time

import pytest

from tests.test_restore_db_integration import (
    PINNED_IMAGE,
    SOURCE_LABEL,
    _inspect_backup_source,
    _seed,
    _seed_backup_source,
)

WORKER = "a" * 64
NETWORK = "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
TOKEN = "d" * 32
SOURCE_ID = "e" * 32


class InspectedSyntheticSource:
    database = "restore_production_test_unit"

    def __init__(self):
        self.started = time.time() - 10
        self.container = {
            "Id": WORKER,
            "Image": IMAGE_ID,
            "Created": dt.datetime.fromtimestamp(self.started + 1, dt.timezone.utc).isoformat(),
            "Config": {"Image": PINNED_IMAGE, "Labels": {SOURCE_LABEL: TOKEN}},
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"owned": {"NetworkID": NETWORK}}},
        }
        self.network = {"Id": NETWORK, "Internal": True, "Containers": {WORKER: {}}}
        self.worker_source = SOURCE_ID
        self.calls = []

    def docker(self, *args):
        self.calls.append(args)
        if args == ("container", "inspect", WORKER):
            return json.dumps([self.container])
        if args == ("image", "inspect", PINNED_IMAGE):
            return json.dumps([{"Id": IMAGE_ID}])
        if args == ("network", "inspect", NETWORK):
            return json.dumps([self.network])
        assert args[:2] == ("exec", WORKER) and args[2] == "psql"
        assert "pg_control_system()" in args[-1]
        return self.worker_source

    def sql(self, database, query):
        assert database == self.database and "pg_control_system()" in query
        return SOURCE_ID


def test_synthetic_source_receipt_is_bound_to_inspection_and_independent_sql():
    stack = InspectedSyntheticSource()
    receipt = _inspect_backup_source(stack, WORKER, TOKEN, NETWORK, stack.started)
    assert receipt == {
        "container_id": WORKER,
        "image_id": IMAGE_ID,
        "created": stack.container["Created"],
        "network_id": NETWORK,
        "source_id": SOURCE_ID,
        "prepared_at": receipt["prepared_at"],
        "expires_at": receipt["prepared_at"] + 5400,
    }
    assert stack.started < receipt["prepared_at"] <= time.time()
    assert len(stack.calls) == 4


@pytest.mark.parametrize(
    "case",
    [
        "id",
        "label",
        "image",
        "reference",
        "stopped",
        "old",
        "future",
        "network",
        "public_network",
        "membership",
        "sql_identity",
    ],
)
def test_synthetic_source_receipt_refuses_unproven_or_changed_source(case):
    stack = InspectedSyntheticSource()
    if case == "id":
        stack.container["Id"] = "f" * 64
    elif case == "label":
        stack.container["Config"]["Labels"][SOURCE_LABEL] = "foreign"
    elif case == "image":
        stack.container["Image"] = "sha256:" + "f" * 64
    elif case == "reference":
        stack.container["Config"]["Image"] = "untrusted:latest"
    elif case == "stopped":
        stack.container["State"]["Running"] = False
    elif case in {"old", "future"}:
        epoch = stack.started - 1 if case == "old" else time.time() + 100
        stack.container["Created"] = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()
    elif case == "network":
        stack.container["NetworkSettings"]["Networks"]["owned"]["NetworkID"] = "f" * 64
    elif case == "public_network":
        stack.network["Internal"] = False
    elif case == "membership":
        stack.network["Containers"] = {}
    else:
        stack.worker_source = "f" * 32
    before = copy.deepcopy((stack.container, stack.network))
    with pytest.raises(AssertionError):
        _inspect_backup_source(stack, WORKER, TOKEN, NETWORK, stack.started)
    # Producer is read-only: it never repairs/adopts/removes a mismatched source.
    assert all(args[0] in {"container", "image", "network", "exec"} for args in stack.calls)
    assert all("rm" not in args and "start" not in args for args in stack.calls)
    assert (stack.container, stack.network) == before


class SeedRoleModel:
    """Only models fixture role order; real Timescale privilege checks remain in CI."""

    database = "restore_production_test_source"

    def __init__(self):
        self.login = False
        self.statements = []

    def sql(self, database, statement):
        assert "SUPERUSER" not in statement
        self.statements.append((database, statement))
        if statement == "CREATE ROLE heatpump LOGIN":
            assert database == "postgres"
            self.login = True
        if "SELECT add_retention_policy(" in statement:
            assert statement.startswith("SET ROLE heatpump;")
            if not self.login:
                raise PermissionError(
                    'permission denied to start background process as role "heatpump"'
                )
            assert "RESET ROLE; SELECT add_job(" in statement
        return ""


def test_source_seed_uses_login_job_owner_without_superuser_grant():
    stack = SeedRoleModel()
    _seed_backup_source(stack)
    assert stack.login
    assert any("add_retention_policy(" in sql for _, sql in stack.statements)
    assert all("NOLOGIN" not in sql for _, sql in stack.statements)


def test_old_nologin_source_seed_is_rejected_at_policy_registration():
    stack = SeedRoleModel()
    with pytest.raises(PermissionError, match="background process"):
        _seed(stack, stack.database, "archive")

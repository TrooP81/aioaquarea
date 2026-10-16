"""Read-only resource preflight tests. Docker and filesystem targets are fakes."""

import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from packages.core import reset_operator as operator
from packages.core.data_reset import ReceiptStore, ResetRefused


@pytest.fixture
def resources(monkeypatch, tmp_path):
    model = tmp_path / "fresh-models"
    model.mkdir()
    project = "isolated-project"
    maintenance = "a" * 64
    database = "b" * 64
    api = "c" * 64
    scope = {
        "context": "desktop-linux",
        "project": project,
        "compose_files": {
            database: [r"D:\isolated\compose.yml", r"D:\isolated\infra.override.yml"],
            api: ["/isolated/generated/deployment-compose.json"],
            maintenance: ["/isolated/maintenance.yml", "/isolated/maintenance.override.yml"],
        },
        "database": "isolated_reset",
        "database_oid": 123,
        "source_revision": "source-hash",
        "old_model_volume": "old",
        "new_model_volume": "new",
        "owners": [database, api],
        "maintenance_container": maintenance,
        "server_address": "172.31.1.2",
        "server_port": 5432,
    }
    containers = []
    for identity, service in ((maintenance, "reset-maintenance"), (database, "db"), (api, "api")):
        containers.append(
            {
                "Id": identity,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": project,
                        "com.docker.compose.service": service,
                        "com.docker.compose.project.config_files": ",".join(
                            scope["compose_files"][identity]
                        ),
                    }
                },
                "State": {
                    "Running": service != "api",
                    "Restarting": False,
                    "Paused": False,
                    "Status": "exited" if service == "api" else "running",
                },
                "Mounts": [{"Type": "volume", "Name": "new", "Destination": str(model.resolve())}],
                "NetworkSettings": {"Networks": {"isolated": {"IPAddress": "172.31.1.2"}}},
            }
        )
    volumes = {
        name: {"Name": name, "Labels": {"com.docker.compose.project": project}}
        for name in ("old", "new")
    }
    users = {"old": "", "new": maintenance[:12]}
    monkeypatch.setenv("HOSTNAME", maintenance[:12])
    monkeypatch.setattr(operator, "source_fingerprint", lambda: "source-hash")

    def docker_json(context, *args):
        assert context == "desktop-linux"
        if args[0] == "context":
            return [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        if args[0] == "inspect":
            assert set(args[1:]) == {container["Id"] for container in containers}
            return copy.deepcopy(containers)
        assert args[:2] == ("volume", "inspect")
        return [volumes[args[2]]]

    def run(args, **kwargs):
        assert args[:3] == ["docker", "--context", "desktop-linux"]
        assert kwargs["timeout"] == 15
        assert "capture_output" in kwargs
        assert args[3] == "ps"
        predicate = args[-1]
        if predicate.startswith("label="):
            # Real `docker ps -aq` truncates IDs unless --no-trunc is requested.
            ids = [container["Id"] for container in containers]
            stdout = "\n".join(ids if "--no-trunc" in args else [identity[:12] for identity in ids])
        else:
            stdout = users[predicate.split("=")[1]]
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(operator, "docker_json", docker_json)
    monkeypatch.setattr(operator.subprocess, "run", run)
    return scope, model, containers, volumes, users


def test_RESET1_RESET5_exact_isolated_resources_pass(resources):
    scope, model, *_ = resources
    operator.inspect_resources(scope, model)


def test_PF_T2_exact_mixed_provenance_passes_only_quiesced(resources):
    scope, model, containers, *_ = resources
    original = copy.deepcopy(scope)
    operator.inspect_resources(scope, model)
    assert scope == original  # Literal path strings and list order are approval material.
    containers[2]["State"].update(Running=True, Status="running")
    with pytest.raises(ResetRefused, match="not quiesced"):
        operator.inspect_resources(scope, model)


def test_PF_T3_cross_container_substitution_is_not_a_union(resources):
    scope, model, *_ = resources
    database, api = scope["owners"]
    scope["compose_files"][database], scope["compose_files"][api] = (
        scope["compose_files"][api],
        scope["compose_files"][database],
    )
    with pytest.raises(ResetRefused, match="Compose identity drift"):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize(
    "drift",
    ["order", "basename", "case", "slash", "subset", "label_missing", "maintenance", "new_path"],
)
def test_PF_T4_literal_ordered_provenance_drift_refuses(resources, drift):
    scope, model, containers, *_ = resources
    labels = containers[1]["Config"]["Labels"]
    key = "com.docker.compose.project.config_files"
    files = labels[key].split(",")
    if drift == "order":
        labels[key] = ",".join(reversed(files))
    elif drift == "basename":
        labels[key] = ",".join(path.rsplit("\\", 1)[-1] for path in files)
    elif drift == "case":
        labels[key] = labels[key].lower()
    elif drift == "slash":
        labels[key] = labels[key].replace("\\", "/")
    elif drift == "subset":
        labels[key] = files[0]
    elif drift == "label_missing":
        del labels[key]
    elif drift == "maintenance":
        containers[0]["Config"]["Labels"][key] = labels[key]
    else:
        labels[key] += ",/isolated/new.yml"
    with pytest.raises(ResetRefused, match="Compose identity drift"):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize(
    "drift",
    [
        "missing_key",
        "extra_key",
        "maintenance_key",
        "short_id",
        "global_list",
        "empty",
        "string_value",
        "empty_path",
        "non_string_path",
    ],
)
def test_PF_T4_scope_mapping_must_cover_exact_full_inventory(resources, drift):
    scope, model, *_ = resources
    maintenance = scope["maintenance_container"]
    if drift == "missing_key":
        del scope["compose_files"][scope["owners"][0]]
    elif drift == "extra_key":
        scope["compose_files"]["d" * 64] = ["/unapproved.yml"]
    elif drift == "maintenance_key":
        scope["compose_files"][maintenance[:12]] = scope["compose_files"].pop(maintenance)
    elif drift == "short_id":
        scope["owners"][0] = scope["owners"][0][:12]
    elif drift == "global_list":
        scope["compose_files"] = ["/isolated/compose.yml"]
    elif drift == "empty":
        scope["compose_files"][maintenance] = []
    elif drift == "string_value":
        scope["compose_files"][maintenance] = "/isolated/maintenance.yml"
    elif drift == "empty_path":
        scope["compose_files"][maintenance] = [""]
    else:
        scope["compose_files"][maintenance] = [None]
    with pytest.raises(ResetRefused):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize("state", ["running", "paused", "restarting"])
def test_PF_T5_mixed_provenance_does_not_relax_owner_quiescence(resources, state):
    scope, model, containers, *_ = resources
    containers[2]["State"].update(
        Running=True, Status="running", Paused=state == "paused", Restarting=state == "restarting"
    )
    with pytest.raises(ResetRefused):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize("command", ["plan", "apply"])
async def test_PF_T1_passive_artifact_is_not_executable(tmp_path, monkeypatch, command):
    artifact = tmp_path / "passive.json"
    artifact.write_text(
        json.dumps(
            {
                "kind": "preliminary_read_only_target_preflight_NOT_EXECUTABLE",
                "context": "desktop-linux",
                "containers": [],
            }
        )
    )
    inspection, engine, receipts = (MagicMock() for _ in range(3))
    monkeypatch.setattr(operator, "inspect_resources", inspection)
    monkeypatch.setattr(operator, "create_async_engine", engine)
    monkeypatch.setattr(operator, "ReceiptStore", receipts)
    args = SimpleNamespace(command=command, input=artifact, dsn_env="UNSET_PASSIVE_TEST_DSN")
    monkeypatch.delenv(args.dsn_env, raising=False)
    with pytest.raises(ResetRefused):
        await operator.run(args)
    inspection.assert_not_called()
    engine.assert_not_called()
    receipts.assert_not_called()


@pytest.mark.parametrize(
    "approved,observed,ipv4,ipv6",
    [
        ("172.31.1.2", "172.31.1.2", "172.31.1.2", ""),
        ("172.31.1.2/32", "172.31.1.2", "172.31.1.2", ""),
        ("2001:db8::2", "2001:0DB8:0000:0000:0000:0000:0000:0002", "", "2001:DB8:0:0::2"),
        ("2001:0db8:0:0:0:0:0:2", "2001:db8::2", "172.31.1.2", "2001:db8::2"),
        ("2001:db8::2/128", "2001:db8::2", "", "2001:db8::2"),
    ],
)
async def test_RESET1_RESET5_both_guards_accept_same_canonical_host(
    resources, approved, observed, ipv4, ipv6
):
    from packages.core import data_reset
    from tests.test_data_reset import GuardSession

    scope, model, containers, *_ = resources
    scope.update(database="isolated_reset", database_oid=123, server_address=approved)
    containers[1]["NetworkSettings"]["Networks"]["isolated"] = {
        "IPAddress": ipv4,
        "GlobalIPv6Address": ipv6,
    }
    operator.inspect_resources(scope, model)
    session = GuardSession(address=observed)
    await data_reset._guard(session, scope, "test-device")
    assert any("host(inet_server_addr())" in sql for sql in session.sql)
    assert scope["server_address"] == approved  # Never rewrite approved manifest material.


@pytest.mark.parametrize(
    "address,port",
    [
        ("172.31.1.20", 5432),  # Same textual prefix is not the same host.
        ("172.31.1.2", 5433),
        ("2001:db8::20", 5432),
        ("2001:db8::2", 5433),
    ],
)
def test_RESET1_RESET5_docker_guard_refuses_different_exact_endpoint(resources, address, port):
    scope, model, containers, *_ = resources
    containers[1]["NetworkSettings"]["Networks"]["isolated"]["GlobalIPv6Address"] = "2001:db8::2"
    scope.update(server_address=address, server_port=port)
    with pytest.raises(ResetRefused, match="database endpoint"):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize("drift", ["extra", "missing", "prefix_collision"])
def test_RESET1_RESET5_full_ID_inventory_still_refuses_new_or_missing_owner(resources, drift):
    scope, model, containers, *_ = resources
    if drift != "missing":
        extra = copy.deepcopy(containers[-1])
        extra["Id"] = (
            scope["maintenance_container"][:12] + "d" * 52
            if drift == "prefix_collision"
            else "d" * 64
        )
        containers.append(extra)
    else:
        containers.pop()
    with pytest.raises(ResetRefused, match="new/missing/unapproved project container"):
        operator.inspect_resources(scope, model)


@pytest.mark.parametrize(
    "drift",
    [
        "context",
        "build",
        "owner_active",
        "cleanup",
        "files",
        "project",
        "volume_project",
        "old_attached",
        "new_shared",
        "nonempty_models",
        "mount",
        "database_address",
        "caller",
    ],
)
def test_RESET1_RESET5_resource_drift_refuses_before_DB(resources, drift, monkeypatch):
    scope, model, containers, volumes, users = resources
    if drift == "context":
        scope["context"] = "default"
    elif drift == "build":
        scope["source_revision"] = "other-build"
    elif drift == "owner_active":
        containers[2]["State"]["Running"] = True
    elif drift == "cleanup":
        containers[2]["State"]["Status"] = "dead"
    elif drift == "files":
        scope["compose_files"][scope["owners"][0]] = ["unapproved"]
    elif drift == "project":
        containers[1]["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
    elif drift == "volume_project":
        volumes["new"]["Labels"]["com.docker.compose.project"] = "unrelated"
    elif drift == "old_attached":
        users["old"] = "d" * 12
    elif drift == "new_shared":
        users["new"] += "\n" + "d" * 12
    elif drift == "nonempty_models":
        (model / "stale.pickle").write_bytes(b"fake-not-a-model")
    elif drift == "mount":
        containers[0]["Mounts"][0]["Name"] = "old"
    elif drift == "database_address":
        scope["server_address"] = "192.0.2.1"
    elif drift == "caller":
        monkeypatch.setenv("HOSTNAME", "unapproved-owner")
    with pytest.raises(ResetRefused):
        operator.inspect_resources(scope, model)


def test_CG_T7_receipt_custody_and_exclusive_replay(tmp_path):
    if os.name != "posix":
        with pytest.raises(ResetRefused, match="private Linux"):
            ReceiptStore(tmp_path)
        return
    from uuid import uuid4

    tmp_path.chmod(0o700)
    store = ReceiptStore(tmp_path)
    operation = str(uuid4())
    store.persist(operation, "prepared", {"stage": "intention"})
    store.persist(operation, "prepared", {"stage": "intention"})
    with pytest.raises(ResetRefused, match="conflict"):
        store.persist(operation, "prepared", {"stage": "altered"})
    assert (tmp_path / f"{operation}.prepared.json").stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="requires real Linux receipt filesystem")
def test_REPAIR2_linux_receipt_fsync_modes_and_failure(tmp_path, monkeypatch):
    import stat
    from uuid import uuid4

    tmp_path.chmod(0o755)
    with pytest.raises(ResetRefused, match="mode 0700"):
        ReceiptStore(tmp_path)
    tmp_path.chmod(0o700)
    store = ReceiptStore(tmp_path)
    original = os.fsync
    synced = []

    def track_fsync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        original(fd)  # Real kernel fsync, not a mocked success.

    monkeypatch.setattr(os, "fsync", track_fsync)
    operation = str(uuid4())
    store.persist(operation, "prepared", {"intention": True})
    assert synced == [False, False, True]  # write, readback, directory
    receipt = tmp_path / f"{operation}.prepared.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert receipt.stat().st_uid == os.getuid()
    synced.clear()
    store.persist(operation, "prepared", {"intention": True})
    assert synced == [False, True]

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory fsync"):
        store.persist(str(uuid4()), "prepared", {"intention": True})

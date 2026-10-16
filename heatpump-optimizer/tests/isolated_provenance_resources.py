"""Test-only real Docker provenance checks, called by the explicit operator test."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core import data_reset as reset
from packages.core.reset_operator import inspect_resources, run


async def verify_real_provenance(scope, proof, tmp_path, docker):
    original = copy.deepcopy(scope)
    db = proof["database_container"]
    maintenance = proof["maintenance_container"]
    owner = proof["state_owner"]
    restarting = proof["restart_owner"]
    assert {db, maintenance, owner, restarting} <= set(scope["compose_files"])
    assert scope["compose_files"][db] == [
        r"D:\synthetic\compose.yml",
        r"D:\synthetic\infra.override.yml",
    ]
    assert scope["compose_files"][owner] == [
        "/synthetic/generated/deployment-compose.json",
        "/synthetic/generated/app.override.json",
    ]
    assert scope["compose_files"][maintenance] == [
        "/synthetic/maintenance.yml",
        "/synthetic/maintenance.override.yml",
    ]
    inspect_resources(scope, Path("/fresh-models"))
    print("PF-T2_REAL_MIXED_INFRA_APPLICATION_MAINTENANCE_PASS", flush=True)

    for drift in (
        "cross_substitution",
        "order",
        "basename",
        "case",
        "slash",
        "subset",
        "extra_path",
        "maintenance",
        "missing_key",
        "extra_key",
    ):
        changed = copy.deepcopy(scope)
        files = changed["compose_files"]
        if drift == "cross_substitution":
            files[db], files[owner] = files[owner], files[db]
        elif drift == "order":
            files[db].reverse()
        elif drift == "basename":
            files[db] = [path.rsplit("\\", 1)[-1] for path in files[db]]
        elif drift == "case":
            files[db] = [path.lower() for path in files[db]]
        elif drift == "slash":
            files[db] = [path.replace("\\", "/") for path in files[db]]
        elif drift == "subset":
            files[db] = files[db][:1]
        elif drift == "extra_path":
            files[db].append("/synthetic/unapproved.yml")
        elif drift == "maintenance":
            files[maintenance] = list(files[owner])
        elif drift == "missing_key":
            del files[owner]
        else:
            files["f" * 64] = ["/synthetic/unapproved.yml"]
        with pytest.raises(reset.ResetRefused):
            inspect_resources(changed, Path("/fresh-models"))
        print("PF-T3_T4_REAL_REFUSAL_PASS", drift, flush=True)

    # Extra-container creation is exclusively owned; record and remove its exact ID.
    image = json.loads(docker("inspect", maintenance))[0]["Config"]["Image"]
    extra = docker(
        "create",
        "--name",
        proof["project"] + "-unapproved",
        "--label",
        "com.docker.compose.project=" + proof["project"],
        "--network",
        "none",
        "--entrypoint",
        "python",
        image,
        "-c",
        "pass",
    ).strip()
    assert len(extra) == 64
    print("EXACT_OWNED_TRANSIENT_CONTAINER", extra, flush=True)
    try:
        with pytest.raises(reset.ResetRefused, match="container"):
            inspect_resources(scope, Path("/fresh-models"))
    finally:
        docker("rm", extra)
        print("EXACT_OWNED_TRANSIENT_REMOVED", extra, flush=True)
    print("PF-T4_REAL_NEW_CONTAINER_REFUSAL_PASS", flush=True)

    # These are inert synthetic probe owners, never application/runtime services.
    for identity in (owner, restarting):
        assert identity in proof["owners"]
        inspected = json.loads(docker("inspect", identity))[0]
        assert inspected["Config"]["Labels"]["com.docker.compose.project"] == proof["project"]
    docker("start", owner)
    try:
        with pytest.raises(reset.ResetRefused, match="not quiesced"):
            inspect_resources(scope, Path("/fresh-models"))
        docker("pause", owner)
        try:
            assert json.loads(docker("inspect", owner))[0]["State"]["Paused"]
            with pytest.raises(reset.ResetRefused):
                inspect_resources(scope, Path("/fresh-models"))
        finally:
            docker("unpause", owner)
    finally:
        docker("stop", "-t", "1", owner)
    docker("start", restarting)
    try:
        for _ in range(30):
            if json.loads(docker("inspect", restarting))[0]["State"]["Restarting"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("owned restart probe did not enter real Restarting state")
        with pytest.raises(reset.ResetRefused):
            inspect_resources(scope, Path("/fresh-models"))
    finally:
        docker("stop", "-t", "1", restarting)
    inspect_resources(scope, Path("/fresh-models"))
    assert scope == original
    print("PF-T5_REAL_RUNNING_PAUSED_RESTARTING_REFUSAL_AND_QUIESCED_PASS", flush=True)

    before_receipts = sorted(Path("/receipts").iterdir())
    artifact = tmp_path / "informational-not-executable.json"
    artifact.write_text(json.dumps({"status": "NOT_EXECUTABLE", "scope": scope}))
    for command in ("plan", "apply"):
        # A non-existent DSN variable would raise KeyError if validation happened late.
        # No fake replaces a production guard or receipt/engine/resource operation.
        with pytest.raises(reset.ResetRefused):
            await run(
                SimpleNamespace(
                    command=command,
                    input=artifact,
                    dsn_env="HPO_UNSET_PASSIVE_DSN",
                    model_dir=Path("/does-not-exist"),
                    receipts=Path("/does-not-exist"),
                )
            )
    assert sorted(Path("/receipts").iterdir()) == before_receipts
    print("PF-T1_REAL_PASSIVE_REJECTION_BEFORE_DSN_AND_RECEIPTS_PASS", flush=True)


def verify_manifest_refusals(manifest, tmp_path, cli):
    assert manifest["version"] == 2
    before = sorted(Path("/receipts").iterdir())
    for drift in ("v1", "global_list", "stale_digest"):
        changed = copy.deepcopy(manifest)
        if drift == "v1":
            changed["version"] = 1
        elif drift == "global_list":
            changed["scope"]["compose_files"] = ["/synthetic/global.yml"]
        else:
            changed["statement"] = "changed without new exact approval"
        if drift != "stale_digest":
            changed["digest"] = reset.manifest_digest(changed)
        path = tmp_path / f"rejected-{drift}.json"
        path.write_text(json.dumps(changed))
        assert cli("apply", path, "--approved-digest", changed["digest"]).returncode == 1
        assert sorted(Path("/receipts").iterdir()) == before
        print("PF-T6_REAL_CLI_REFUSAL_PASS", drift, flush=True)

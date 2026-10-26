"""Synthetic unit negatives for the supplementary phase's evidence boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from tests.e2e.test_manual_cop_postgresql import (
    BACKUP_MODULE,
    _lifecycle_evidence_snapshot,
    _verification_phase_owns,
    validate_backup_receipts,
    write_backup_receipt,
)


def test_coordinator_base_and_ci_default_are_the_same_immutable_official_image():
    project = Path(__file__).resolve().parents[1]
    dockerfile = (project / "Dockerfile.restore-coordinator").read_text(encoding="utf-8")
    base = re.search(r"^ARG RESTORE_COORDINATOR_BASE_IMAGE=(.+)$", dockerfile, re.M)[1]
    assert re.fullmatch(r"docker:\d+\.\d+\.\d+-cli@sha256:[0-9a-f]{64}", base)
    workflow = yaml.safe_load(
        (project.parent / ".github/workflows/optimizer-ci.yml").read_text(encoding="utf-8")
    )
    assert workflow["jobs"]["backend-e2e"]["env"]["RESTORE_COORDINATOR_BASE_IMAGE"] == base
    assert "FROM ${RESTORE_COORDINATOR_BASE_IMAGE}" in dockerfile
    assert re.search(r"^RUN apk add --no-cache python3=\d+\.\d+\.\d+-r\d+$", dockerfile, re.M)
    assert 'ENTRYPOINT ["python3", "/drill/restore_drill.py"]' in dockerfile


def record():
    return {
        "test": BACKUP_MODULE + "::test_owned_worker_and_coordinator_image_roundtrip",
        "ownership_token": "unit-only",
        "commit": "a" * 40,
        "source_manifest_sha256": "b" * 64,
        "created_resources": ["c" * 64],
        "source_preserved": True,
        "cleanup_verified": True,
        "coordinator_verified": True,
        "worker_script_sha256": "d" * 64,
    }


def proof():
    return {
        "commit": "a" * 40,
        "digest": "b" * 64,
        "binding": {"heatpump-optimizer/scripts/backup_db.sh": "d" * 64},
    }


def test_backup_nodes_are_never_owned_by_ordinary_phase():
    node = record()["test"]
    assert _verification_phase_owns("backup-roundtrip", node)
    assert not _verification_phase_owns("ordinary", node)
    assert not _verification_phase_owns(
        "backup-roundtrip", "tests/e2e/test_api_dashboard.py::test_x"
    )


def test_required_real_sql_matrix_is_wired_through_owned_runner():
    from tests.e2e.test_manual_cop_postgresql import (
        BACKUP_BINDING_PATHS,
        BACKUP_STATUS_MODULE,
    )

    project = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (project.parent / ".github/workflows/optimizer-ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["backend-e2e"]["steps"]
    prefix = (
        "timeout --signal=TERM --kill-after=10s 660s "
        "python tests/e2e/test_manual_cop_postgresql.py phase "
    )
    commands = [
        prefix
        + "backup-status-inventory -m pytest --noconftest "
        + BACKUP_STATUS_MODULE
        + " --collect-only -q",
        prefix + "backup-status -m pytest --noconftest " + BACKUP_STATUS_MODULE + " -q --tb=short",
    ]
    indexes = []
    for command in commands:
        matching = [(index, step) for index, step in enumerate(steps) if step.get("run") == command]
        assert len(matching) == 1, "required real-SQL inventory/execution is absent"
        index, step = matching[0]
        assert not step.get("continue-on-error") and "if" not in step
        indexes.append(index)
    roundtrip = next(
        index
        for index, step in enumerate(steps)
        if "phase backup-roundtrip " in step.get("run", "")
    )
    assert indexes[0] < indexes[1] < roundtrip
    assert "heatpump-optimizer/" + BACKUP_STATUS_MODULE in BACKUP_BINDING_PATHS
    node = BACKUP_STATUS_MODULE + "::test_transition_exact_original_and_terminal_replay"
    assert _verification_phase_owns("backup-status", node)
    assert not _verification_phase_owns("ordinary", node)
    assert not _verification_phase_owns("backup-roundtrip", node)


def test_real_sql_phase_excludes_configured_database_without_mutating_owned_parent():
    from tests.e2e.test_manual_cop_postgresql import verification_phase_environment

    parent = {
        "DATABASE_URL": "never-forward",
        "COP_MANUAL_TEST_RECEIPT": "parent-proof",
        "COP_MANUAL_TEST_SOURCE_MANIFEST_SHA256": "a" * 64,
    }
    for phase in ("backup-status-inventory", "backup-status"):
        child = verification_phase_environment(phase, parent)
        assert child == {key: value for key, value in parent.items() if key != "DATABASE_URL"}
        assert parent["DATABASE_URL"] == "never-forward"
    assert verification_phase_environment("ordinary", parent) == parent


@pytest.mark.parametrize("phase", ["backup-status-inventory", "backup-status"])
def test_required_sql_phase_keeps_parent_guards_process_binding_and_deadlines(
    tmp_path, monkeypatch, phase
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.e2e import test_manual_cop_postgresql as runner

    calls = []

    async def owned_sql():
        calls.append("owned-sql")

    proof = {
        "commit": "a" * 40,
        "digest": "b" * 64,
        "binding": {"heatpump-optimizer/" + runner.BACKUP_STATUS_MODULE: "c" * 64},
    }

    def source():
        calls.append("source")
        return proof

    child = SimpleNamespace(pid=4242, returncode=0, wait=lambda timeout: 0, poll=lambda: 0)

    def launch(*args, **kwargs):
        calls.append("launch")
        assert calls == ["owned-sql", "source", "launch"]
        return child

    launch_mock = Mock(side_effect=launch)
    monkeypatch.setattr(runner, "verify_owned_sql_identity", owned_sql)
    monkeypatch.setattr(runner, "current_verification_source_proof", source)
    monkeypatch.setattr(runner.subprocess, "Popen", launch_mock)
    monkeypatch.setenv("DATABASE_URL", "never-forward")
    monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path))
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    command = [sys.executable, "-m", "pytest", "--noconftest", runner.BACKUP_STATUS_MODULE]
    runner.run_verification_phase(phase, command)
    environment = launch_mock.call_args.kwargs["env"]
    assert "DATABASE_URL" not in environment
    assert os.environ["DATABASE_URL"] == "never-forward"
    assert environment["PYTEST_PLUGINS"] == "tests.e2e.test_manual_cop_postgresql"
    assert environment["COP_MANUAL_TEST_PHASE"] == phase
    assert launch_mock.call_args.kwargs["start_new_session"] is True
    record = json.loads((tmp_path / phase / "process.json").read_text(encoding="utf-8"))
    assert record["source"] == proof["binding"]
    assert (record["commit"], record["source_manifest_sha256"]) == (
        proof["commit"],
        proof["digest"],
    )
    assert record["exit"] == 0 and not record["alive"] and not record["timed_out"]
    assert (
        runner._utc_timestamp(record["deadline"]) - runner._utc_timestamp(record["started_at"])
    ).total_seconds() == 600
    assert (
        runner._utc_timestamp(record["parent_deadline"])
        - runner._utc_timestamp(record["started_at"])
    ).total_seconds() == 660


@pytest.mark.parametrize("guard", ["owned-sql", "source"])
def test_required_sql_phase_cannot_launch_after_parent_guard_refusal(tmp_path, monkeypatch, guard):
    from unittest.mock import Mock

    from tests.e2e import test_manual_cop_postgresql as runner

    async def owned_sql():
        if guard == "owned-sql":
            raise AssertionError("parent guard refused")

    def source():
        raise AssertionError("parent guard refused")

    launch = Mock()
    monkeypatch.setattr(runner, "verify_owned_sql_identity", owned_sql)
    monkeypatch.setattr(runner, "current_verification_source_proof", source)
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setenv("COP_MANUAL_TEST_EVIDENCE", str(tmp_path))
    with pytest.raises(AssertionError, match="parent guard refused"):
        runner.run_verification_phase(
            "backup-status",
            [sys.executable, "-m", "pytest", "--noconftest", runner.BACKUP_STATUS_MODULE],
        )
    launch.assert_not_called()
    assert not (tmp_path / "backup-status").exists()


@pytest.mark.parametrize(
    ("phase", "damage", "message"),
    [
        ("backup-status-inventory", "source", "verification source drift"),
        ("backup-status", "source", "verification source drift"),
        ("backup-status", "duplicate_call", "exactly one setup/call/teardown"),
        ("backup-status", "failed_teardown", "skip/xfail/failure"),
    ],
)
def test_required_sql_seal_rejects_process_drift_and_bad_reports(tmp_path, phase, damage, message):
    """Synthetic negative evidence only; never a canonical ownership receipt."""
    from tests.e2e.test_manual_cop_postgresql import (
        SyntheticVerificationEvidence,
        seal_verification_evidence,
    )

    fixture = SyntheticVerificationEvidence()
    evidence, source = fixture.evidence(tmp_path)
    if damage == "source":
        fixture.process(evidence, phase, source_manifest_sha256="d" * 64)
    else:
        path, reports = fixture.reports(evidence, phase)
        if damage == "duplicate_call":
            reports.append(next(report for report in reports if report["when"] == "call"))
        else:
            next(report for report in reports if report["when"] == "teardown")["outcome"] = "failed"
        path.write_text("".join(json.dumps(report) + "\n" for report in reports), encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        seal_verification_evidence(evidence, source)
    assert not (evidence / "seal.json").exists()


def test_unit_gate_executes_conftest_free_seal_regressions(tmp_path):
    """Keep the normal unit gate from missing coordinator-only synthetic fixtures."""
    root = Path(__file__).resolve().parents[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COP_MANUAL_TEST_") and key != "PYTEST_PLUGINS"
    }
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_PREFIX",
    ):
        environment.pop(key, None)
    environment["HPO_TEST_OFFLINE"] = "1"
    python_path = [
        str(root),
        str(root.parent),
        environment.get("PYTHONPATH", ""),
    ]
    environment["PYTHONPATH"] = os.pathsep.join(path for path in python_path if path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "tests/e2e/test_manual_cop_postgresql.py::TestLifecycleReceiptSeal",
            "tests/e2e/test_manual_cop_postgresql.py::TestVerificationSeal",
            "tests/e2e/test_ml_lifecycle_verification.py::TestOBS5ScopeSeal",
            "-q",
            "--tb=short",
            "--basetemp=" + str(tmp_path / "seal-protocol"),
            "-o",
            "cache_dir=" + str(tmp_path / "seal-cache"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert " skipped" not in result.stdout


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_preserved", False),
        ("cleanup_verified", False),
        ("coordinator_verified", False),
        ("ownership_token", "foreign"),
        ("commit", "e" * 40),
        ("source_manifest_sha256", "f" * 64),
        ("worker_script_sha256", "e" * 64),
        ("created_resources", ["c" * 64, "c" * 64]),
    ],
)
def test_seal_rejects_unbound_or_incomplete_backup_receipt(tmp_path, field, value):
    tmp_path.chmod(0o700)
    receipt = record()
    receipt[field] = value
    write_backup_receipt(tmp_path, receipt)
    with pytest.raises(AssertionError, match="source/creation/cleanup"):
        validate_backup_receipts(tmp_path, {receipt["test"]}, proof(), {"token": "unit-only"})


def test_missing_backup_receipt_cannot_seal(tmp_path):
    with pytest.raises(AssertionError, match="coverage"):
        validate_backup_receipts(tmp_path, {record()["test"]}, proof(), {"token": "unit-only"})


def test_receipts_are_snapshot_bound_and_cannot_be_overwritten(tmp_path):
    tmp_path.chmod(0o700)
    receipt = record()
    write_backup_receipt(tmp_path, receipt)
    before = _lifecycle_evidence_snapshot(tmp_path)
    validate_backup_receipts(tmp_path, {receipt["test"]}, proof(), {"token": "unit-only"})
    with pytest.raises(AssertionError, match="lifecycle receipt lock unavailable") as duplicate:
        write_backup_receipt(tmp_path, receipt)
    assert isinstance(duplicate.value.__cause__, FileExistsError)
    path = next(tmp_path.glob("backup-receipt*"))
    assert json.loads(path.read_text(encoding="utf-8")) == receipt
    changed = dict(receipt, cleanup_verified=False)
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert _lifecycle_evidence_snapshot(tmp_path) != before


def test_unit_phase_receipts_retain_per_test_duration_even_without_a_summary(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.e2e.test_manual_cop_postgresql import pytest_runtest_logreport

    path = tmp_path / "reports.jsonl"
    with monkeypatch.context() as environment:
        environment.setenv("COP_MANUAL_TEST_PHASE", "unit")
        environment.setenv("COP_MANUAL_TEST_REPORTS", str(path))
        pytest_runtest_logreport(
            SimpleNamespace(nodeid="slow::case", when="call", duration=15.0, outcome="passed")
        )
    report = json.loads(path.read_text())
    assert report["duration"] == 15.0
    assert report["nodeid"] == "slow::case"

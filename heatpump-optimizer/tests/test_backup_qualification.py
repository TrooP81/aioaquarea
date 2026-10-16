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


def test_unit_gate_executes_conftest_free_seal_regressions(tmp_path):
    """Keep the normal unit gate from missing coordinator-only synthetic fixtures."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COP_MANUAL_TEST_") and key != "PYTEST_PLUGINS"
    }
    environment["HPO_TEST_OFFLINE"] = "1"
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

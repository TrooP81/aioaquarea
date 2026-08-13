"""Security and integrity tests for persisted ML artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.config import settings
from packages.ml.safe_persistence import _validate_path, safe_dump, safe_load


@pytest.fixture
def model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "models"
    directory.mkdir()
    monkeypatch.setattr(settings, "model_dir", str(directory))
    monkeypatch.setattr(settings, "secret_key", "test-only-model-signing-key")
    return directory


def test_signed_model_round_trip(model_dir: Path) -> None:
    path = model_dir / "model.pkl"

    safe_dump({"version": 1, "weights": [1.0, 2.0]}, path)

    assert safe_load(path) == {"version": 1, "weights": [1.0, 2.0]}


def test_rejects_parent_traversal(model_dir: Path) -> None:
    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(model_dir / ".." / "escaped.pkl")


def test_rejects_sibling_with_matching_string_prefix(model_dir: Path) -> None:
    sibling = model_dir.parent / f"{model_dir.name}-backup" / "model.pkl"

    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(sibling)


def test_rejects_symlink_that_escapes_model_directory(model_dir: Path) -> None:
    outside = model_dir.parent / "outside"
    outside.mkdir()
    link = model_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="outside model directory"):
        _validate_path(link / "model.pkl")


def test_rejects_tampered_model(model_dir: Path) -> None:
    path = model_dir / "model.pkl"
    safe_dump({"trusted": True}, path)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="HMAC mismatch"):
        safe_load(path)

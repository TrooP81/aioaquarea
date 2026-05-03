"""Safe model serialization with HMAC integrity verification.

Wraps pickle with HMAC-SHA256 to detect tampering of model files.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import pickle
from pathlib import Path
from typing import Any

from packages.core.config import settings


def _get_signing_key() -> bytes:
    """Derive signing key from the app secret_key."""
    return hashlib.sha256(settings.secret_key.encode()).digest()


def _validate_path(path: Path) -> Path:
    """Resolve path and ensure it stays within the configured model directory."""
    model_dir = Path(settings.model_dir).resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(model_dir)):
        raise ValueError(f"Path {path} resolves outside model directory {model_dir}")
    return resolved


def safe_dump(obj: Any, path: Path) -> None:
    """Serialize object to file with HMAC integrity tag."""
    resolved = _validate_path(path)
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    mac = hmac.new(_get_signing_key(), data, hashlib.sha256).digest()

    # Write 32-byte HMAC prefix then pickled data
    resolved.write_bytes(mac + data)


def safe_load(path: Path) -> Any:
    """
    Deserialize object from file, verifying HMAC integrity first.

    Raises ValueError if the file has been tampered with or was not
    created by safe_dump.
    """
    resolved = _validate_path(path)
    raw = resolved.read_bytes()

    if len(raw) < 32:
        raise ValueError(f"Model file too small to contain HMAC: {path}")

    stored_mac = raw[:32]
    data = raw[32:]

    expected_mac = hmac.new(_get_signing_key(), data, hashlib.sha256).digest()

    if not hmac.compare_digest(stored_mac, expected_mac):
        raise ValueError(
            f"Model file integrity check failed (HMAC mismatch): {path}. "
            "File may have been tampered with or was saved with a different secret_key."
        )

    return pickle.loads(data)  # noqa: S301 — verified via HMAC

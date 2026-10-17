"""Safe model serialization with HMAC integrity verification.

Wraps pickle with HMAC-SHA256 to detect tampering of model files.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import errno
import hashlib
import hmac
import json
import os
import pickle
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from packages.core.config import settings

_FITTING_WORKER: ContextVar[bool] = ContextVar("ml_fitting_worker", default=False)


@contextmanager
def fitting_worker_scope() -> Iterator[None]:
    token = _FITTING_WORKER.set(True)
    try:
        yield
    finally:
        _FITTING_WORKER.reset(token)


def _require_publication_owner() -> None:
    if _FITTING_WORKER.get():
        raise PermissionError("Fitting workers cannot publish artifacts or lifecycle authority")


def _get_signing_key() -> bytes:
    """Derive signing key from the app secret_key."""
    return hashlib.sha256(settings.secret_key.encode()).digest()


def _validate_path(path: Path) -> Path:
    """Resolve path and ensure it stays within the configured model directory."""
    model_dir = Path(settings.model_dir).resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(model_dir)
    except ValueError:
        raise ValueError(f"Path {path} resolves outside model directory {model_dir}")
    return resolved


def safe_write_temp(obj: Any, path: Path) -> Path:
    """Serialize a signed artifact to a temporary file beside *path*."""
    _require_publication_owner()
    resolved = _validate_path(path)
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    mac = hmac.new(_get_signing_key(), data, hashlib.sha256).digest()
    with NamedTemporaryFile(
        mode="wb", dir=resolved.parent, prefix=f".{resolved.name}.", delete=False
    ) as temp:
        temp.write(mac + data)
        temp.flush()
        return Path(temp.name)


def safe_publish_temp(temp_path: Path, path: Path) -> None:
    """Atomically publish a previously signed temporary artifact."""
    _require_publication_owner()
    resolved = _validate_path(path)
    resolved_temp = _validate_path(temp_path)
    resolved_temp.replace(resolved)


def safe_dump(obj: Any, path: Path) -> None:
    """Serialize object to file with HMAC integrity tag."""
    temp_path = safe_write_temp(obj, path)
    try:
        safe_publish_temp(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


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


_ARTIFACT_MAGIC = b"HPO-ML-ARTIFACT\x01"
_MAX_HEADER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SignedArtifact:
    artifact_hash: str
    component: str
    schema_version: str
    manifest: dict[str, Any]
    path: Path


def artifact_path(artifact_hash: str, *, resolve_entry: bool = True) -> Path:
    if len(artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in artifact_hash):
        raise ValueError("Invalid artifact hash")
    path = Path(settings.model_dir) / "lifecycle" / f"{artifact_hash}.pkl"
    return _validate_path(path) if resolve_entry else _validate_path(path.parent) / path.name


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    for key in ("fit_id", "data_cutoff", "input_schema", "output_schema"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError(f"Invalid artifact manifest: {key}")
    cutoff = dt.datetime.fromisoformat(manifest["data_cutoff"])
    if cutoff.tzinfo is None:
        raise ValueError("Artifact cutoff must include a timezone")
    versions = manifest.get("runtime_versions")
    if (
        not isinstance(versions, dict)
        or not versions
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in versions.items()
        )
    ):
        raise ValueError("Invalid artifact runtime versions")
    hashes = manifest.get("evidence_hashes")
    if not isinstance(hashes, list) or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in hashes
    ):
        raise ValueError("Invalid artifact evidence hashes")


def _fsync_directory(directory: Path) -> None:
    """Fail closed on filesystems that cannot durably publish a directory entry."""
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_immutable(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-replace rename is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def verify_signed_artifact(
    artifact_hash: str, *, component: str, schema_version: str
) -> tuple[SignedArtifact, bytes]:
    """Authenticate one byte snapshot and validate its envelope before unpickling."""
    path = artifact_path(artifact_hash)
    raw = path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), artifact_hash):
        raise ValueError("Artifact hash mismatch")
    if len(raw) < 32 or not hmac.compare_digest(
        raw[:32], hmac.new(_get_signing_key(), raw[32:], hashlib.sha256).digest()
    ):
        raise ValueError("Artifact HMAC mismatch")
    signed = raw[32:]
    offset = len(_ARTIFACT_MAGIC)
    if not signed.startswith(_ARTIFACT_MAGIC) or len(signed) < offset + 4:
        raise ValueError("Invalid artifact envelope")
    header_size = int.from_bytes(signed[offset : offset + 4], "big")
    start = offset + 4
    if not 0 < header_size <= _MAX_HEADER_BYTES or start + header_size >= len(signed):
        raise ValueError("Invalid artifact header size")
    header = json.loads(signed[start : start + header_size])
    if not isinstance(header, dict) or (
        header.get("format") != "ml_artifact_v1"
        or header.get("component") != component
        or header.get("schema_version") != schema_version
    ):
        raise ValueError("Artifact schema mismatch")
    manifest = header.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("Invalid artifact manifest")
    _validate_manifest(manifest)
    return SignedArtifact(artifact_hash, component, schema_version, manifest, path), signed[
        start + header_size :
    ]


def certify_durable_artifact(
    artifact_hash: str, *, component: str, schema_version: str
) -> tuple[SignedArtifact, bytes]:
    """Re-confirm storage durability before a reference can be CAS-committed."""
    path = artifact_path(artifact_hash)
    with path.open("r+b" if os.name == "nt" else "rb") as artifact_file:
        os.fsync(artifact_file.fileno())
    _fsync_directory(path.parent)
    _fsync_directory(path.parent.parent)
    return verify_signed_artifact(artifact_hash, component=component, schema_version=schema_version)


def load_signed_artifact(artifact_hash: str, *, component: str, schema_version: str) -> Any:
    """Load an exact hash selected by committed lifecycle state, never directory order."""
    _, payload = verify_signed_artifact(
        artifact_hash, component=component, schema_version=schema_version
    )
    return pickle.loads(payload)


def publish_signed_artifact(
    obj: Any,
    *,
    component: str,
    schema_version: str,
    manifest: Mapping[str, Any],
    round_trip_check: Callable[[Any], None] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> SignedArtifact:
    """Durably publish immutable bytes; this does not commit a lifecycle reference."""
    _require_publication_owner()
    _validate_manifest(manifest)
    if not component or not schema_version:
        raise ValueError("Artifact component and schema are required")
    header = json.dumps(
        {
            "format": "ml_artifact_v1",
            "component": component,
            "schema_version": schema_version,
            "manifest": dict(manifest),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(header) > _MAX_HEADER_BYTES:
        raise ValueError("Artifact header is too large")
    signed = (
        _ARTIFACT_MAGIC
        + len(header).to_bytes(4, "big")
        + header
        + pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    )
    raw = hmac.new(_get_signing_key(), signed, hashlib.sha256).digest() + signed
    artifact_hash = hashlib.sha256(raw).hexdigest()
    path = artifact_path(artifact_hash)
    path.parent.mkdir(exist_ok=True)
    _fsync_directory(path.parent.parent)
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".artifact-", delete=False
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(raw)
            temp.flush()
            os.fsync(temp.fileno())
        if checkpoint:
            checkpoint("file_synced")
        try:
            _rename_immutable(temp_path, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("Immutable artifact already exists with different bytes") from None
        if checkpoint:
            checkpoint("renamed")
        _fsync_directory(path.parent)
        if checkpoint:
            checkpoint("directory_synced")
        artifact, payload = verify_signed_artifact(
            artifact_hash, component=component, schema_version=schema_version
        )
        if round_trip_check:
            round_trip_check(pickle.loads(payload))
        if checkpoint:
            checkpoint("verified")
        return artifact
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

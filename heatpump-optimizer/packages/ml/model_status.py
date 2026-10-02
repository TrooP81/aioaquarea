"""Read deployed model artifact status without sharing estimators with request handlers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from packages.ml.cop_model_core import COP_MODEL_ARTIFACT_GLOB, COP_MODEL_ARTIFACT_PREFIX
from packages.ml.demand_model_core import (
    DEMAND_MODEL_ARTIFACT_GLOB,
    DEMAND_MODEL_ARTIFACT_PREFIX,
)

_ARTIFACT_CONFIG = {
    "cop": (COP_MODEL_ARTIFACT_GLOB, COP_MODEL_ARTIFACT_PREFIX),
    "demand": (DEMAND_MODEL_ARTIFACT_GLOB, DEMAND_MODEL_ARTIFACT_PREFIX),
}
_artifact_status_cache: dict[
    str, tuple[tuple[tuple[str, int, int], ...] | None, dict[str, object]]
] = {}


def _empty_status(reason: str) -> dict[str, object]:
    return {
        "trained": False,
        "last_trained": None,
        "samples": 0,
        "metrics": None,
        "unavailable_reason": reason,
    }


def _version_to_iso(path: Path, prefix: str) -> str | None:
    version = path.stem.removeprefix(prefix)
    try:
        return (
            dt.datetime.strptime(version, "%Y%m%d_%H%M").replace(tzinfo=dt.timezone.utc).isoformat()
        )
    except ValueError:
        return None


def _artifacts_with_fingerprint(
    model_dir: Path, glob: str
) -> tuple[list[Path], tuple[tuple[str, int, int], ...] | None]:
    artifacts = sorted(model_dir.glob(glob))
    if not artifacts:
        return artifacts, None
    return artifacts, tuple(
        (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in artifacts
    )


def _inspect_model_artifact(model_kind: str, model_dir: Path) -> dict[str, object]:
    """Inspect the newest compatible signed artifact through the model's loader."""
    glob, prefix = _ARTIFACT_CONFIG[model_kind]
    artifacts, cache_key = _artifacts_with_fingerprint(model_dir, glob)

    cached = _artifact_status_cache.get(model_kind)
    if cached is not None and cached[0] == cache_key:
        return dict(cached[1])

    if not artifacts:
        status = _empty_status("not_found")
    else:
        from packages.ml.models import COPModel, DemandModel

        model = COPModel() if model_kind == "cop" else DemandModel()
        try:
            loaded = model.load_latest()
        except ValueError:
            status = _empty_status("integrity_check_failed")
        except Exception:  # noqa: BLE001 - model artifacts must not fail the status endpoint
            status = _empty_status("load_failed")
        else:
            if loaded:
                raw_metrics = model.metrics
                metrics = raw_metrics if raw_metrics else None
                samples = raw_metrics.get("samples", 0) if raw_metrics else 0
                version_path = Path(f"{prefix}{model.version}.pkl")
                status = {
                    "trained": True,
                    "last_trained": _version_to_iso(version_path, prefix),
                    "samples": int(samples) if isinstance(samples, (int, float)) else 0,
                    "metrics": metrics,
                    "unavailable_reason": None,
                }
            else:
                from packages.ml.safe_persistence import safe_load

                try:
                    safe_load(artifacts[-1])
                except ValueError:
                    status = _empty_status("integrity_check_failed")
                except Exception:  # noqa: BLE001 - artifact inspection is best effort
                    status = _empty_status("load_failed")
                else:
                    status = _empty_status("incompatible_artifact")

    _artifact_status_cache[model_kind] = (cache_key, status)
    return dict(status)


def clear_artifact_status_cache() -> None:
    """Clear cached snapshots for tests and explicit process-local reset points."""
    _artifact_status_cache.clear()

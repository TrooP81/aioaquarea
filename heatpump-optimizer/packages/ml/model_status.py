"""Reporting status from committed lifecycle authority, never directory discovery."""

from __future__ import annotations

import datetime as dt
import asyncio
import copy
import math
import time
from pathlib import Path

from packages.ml.lifecycle import (
    LIFECYCLE_SCHEMAS,
    ArtifactReference,
    ArtifactUnavailableError,
    DEMAND_CADENCE,
    Lifecycle,
    LifecycleSnapshot,
    LifecycleUnavailable,
    MAINTENANCE_CADENCE,
    verify_reference,
)

_CACHE_MAX_AGE = 60.0
_artifact_status_cache: dict[str, tuple[tuple[object, ...], float, dict[str, object]]] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _cache_time() -> float:
    return time.monotonic()


def _empty_status(reason: str) -> dict[str, object]:
    return {
        "trained": False,
        "last_trained": None,
        "samples": 0,
        "metrics": None,
        "unavailable_reason": reason,
        "status": "unavailable",
        "control_ready": False,
        "ordinary_writes_allowed": False,
        "lifecycle_epoch": None,
        "eligibility_revision": None,
        "committed_hash": None,
        "candidate_hash": None,
        "candidate_status": None,
        "qualification_expires_at": None,
        "last_valid_outcome_at": None,
        "qualification_scope": {},
        "incumbent_status": "unavailable",
        "candidate_progress": None,
        "maintenance_enabled": None,
        "next_cadence_at": None,
        "cadence_hours": None,
        "cadence_reason": "lifecycle_unavailable",
        "evidence_for": None,
        "evidence_as_of": None,
        "evidence_reason": reason,
        "counts": None,
        "missing_counts": None,
        "missing_diversity": None,
        "exclusion_counts": {},
        "pending_outcomes": None,
        "next_decision_checkpoint": None,
        "last_decision": None,
        "control_readiness": {"ready": False, "reason": "observe_only_build"},
    }


def _artifacts_with_fingerprint(model_dir: Path, glob: str) -> None:
    raise LifecycleUnavailable("directory_discovery_disabled")


def _base_status(reference: ArtifactReference | None) -> dict[str, object]:
    if reference is None:
        return _empty_status("no_committed_artifact")
    metrics = reference.manifest.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise ValueError("Invalid artifact metrics schema")
    samples = reference.manifest.get("samples", (metrics or {}).get("samples", 0))
    if (
        isinstance(samples, bool)
        or not isinstance(samples, (float, int))
        or not math.isfinite(samples)
        or samples < 0
    ):
        raise ValueError("Invalid artifact sample count")
    trained_at = reference.manifest.get("trained_at", reference.manifest["data_cutoff"])
    parsed = dt.datetime.fromisoformat(trained_at)
    if parsed.tzinfo is None:
        raise ValueError("Invalid artifact fit timestamp")
    return {
        **_empty_status("no_committed_artifact"),
        "trained": True,
        "last_trained": parsed.isoformat(),
        "samples": int(samples),
        "metrics": copy.deepcopy(metrics),
        "unavailable_reason": None,
    }


def _reporting_progress(
    model_kind: str,
    state: LifecycleSnapshot,
    reference: ArtifactReference | None,
    evidence: dict[str, object] | None,
    now: dt.datetime,
) -> dict[str, object]:
    from packages.ml import forecast_quality as quality

    cadence = DEMAND_CADENCE if model_kind == "demand" else MAINTENANCE_CADENCE
    next_cadence = state.next_maintenance_at
    if model_kind == "demand" and state.last_demand_attempt_at is not None:
        demand_due = state.last_demand_attempt_at + DEMAND_CADENCE
        next_cadence = max(next_cadence, demand_due) if next_cadence else demand_due
    checkpoints = state.checkpoints or {}
    frozen_at = None
    next_checkpoint = None
    last_decision = None
    if reference is not None and reference.manifest.get("frozen_at"):
        frozen_at = dt.datetime.fromisoformat(reference.manifest["frozen_at"])
        if frozen_at.tzinfo is None or frozen_at > now:
            raise ValueError("Invalid frozen-fit timestamp")
        for index, hours in enumerate(quality.DECISION_OFFSETS_HOURS):
            decision = checkpoints.get(f"decision:{reference.artifact_hash}:{index}")
            if decision is not None:
                last_decision = {
                    "index": index,
                    "status": decision.get("status"),
                    "reason": decision.get("result", {}).get("reason"),
                }
            elif next_checkpoint is None:
                due = frozen_at + dt.timedelta(hours=hours)
                next_checkpoint = {
                    "index": index,
                    "at": due.isoformat(),
                    "due": now >= due,
                    "policy": quality.QUALIFICATION_POLICY_SCHEMA,
                }
    raw_counts = evidence.get("counts") if evidence is not None else None
    if evidence is not None and not isinstance(raw_counts, dict):
        raise ValueError("Invalid reporting evidence counts")
    counts = copy.deepcopy(raw_counts) if isinstance(raw_counts, dict) else None
    if reference is None:
        counts = {
            "pairs": 0,
            "device_day_clusters": 0,
            "issue_cohorts": 0,
            "horizons": {},
            "outdoor_bins": {},
        }
    missing_counts = None
    missing_diversity = None
    if counts is not None:
        horizons = reference.manifest.get("horizons", []) if reference else []
        bin_counts = counts.get("outdoor_bins", {})
        populated_bins = sum(
            count >= quality.MIN_OUTDOOR_BIN_PAIRS
            for key, count in bin_counts.items()
            if key != "unknown"
        )
        missing_counts = {
            "paired_outcomes": max(0, quality.MIN_PROSPECTIVE_PAIRS - counts["pairs"]),
            "device_day_clusters": max(
                0, quality.MIN_DEVICE_DAY_CLUSTERS - counts["device_day_clusters"]
            ),
            "issue_cohorts": max(0, quality.MIN_ISSUE_COHORTS - counts["issue_cohorts"]),
            "elapsed_days": max(
                0.0, quality.MIN_PROSPECTIVE_DAYS - (now - frozen_at).total_seconds() / 86400
            )
            if frozen_at
            else None,
        }
        missing_diversity = {
            "populated_outdoor_bins": max(0, quality.MIN_POPULATED_OUTDOOR_BINS - populated_bins),
            "pairs_per_horizon": {
                str(horizon): max(0, 12 - counts.get("horizons", {}).get(str(horizon), 0))
                for horizon in horizons
            },
            "pairs_per_outdoor_bin": {
                key: max(0, quality.MIN_OUTDOOR_BIN_PAIRS - count)
                for key, count in bin_counts.items()
                if key != "unknown"
            },
        }
    return {
        "maintenance_enabled": state.maintenance_enabled,
        "cadence_hours": cadence.total_seconds() / 3600,
        "next_cadence_at": next_cadence.isoformat()
        if next_cadence and state.maintenance_enabled
        else None,
        "cadence_reason": "maintenance_disabled"
        if not state.maintenance_enabled
        else (None if next_cadence else "schedule_unavailable"),
        "evidence_for": reference.role if reference else None,
        "evidence_as_of": evidence.get("as_of") if evidence else None,
        "evidence_reason": None
        if evidence or reference is None
        else "prospective_evidence_unavailable",
        "counts": counts,
        "missing_counts": missing_counts,
        "missing_diversity": missing_diversity,
        "exclusion_counts": copy.deepcopy(evidence.get("exclusions", {})) if evidence else {},
        "pending_outcomes": evidence.get("pending_outcomes") if evidence else None,
        "last_valid_outcome_at": evidence.get("last_valid_outcome_at") if evidence else None,
        "next_decision_checkpoint": next_checkpoint,
        "last_decision": last_decision,
    }


def _inspect_model_artifact(
    model_kind: str,
    model_dir: Path,
    state: LifecycleSnapshot | None = None,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    """Inspect exact committed hashes; verify even when parsed metadata is cached."""
    if model_kind == "cop":
        return {
            "trained": False,
            "last_trained": None,
            "samples": 0,
            "source_records": 0,
            "metrics": None,
            "status": "retired",
            "supported": False,
            "unavailable_reason": "retired",
            "reason": "unverified_thermal_output",
        }

    if state is None:
        _artifact_status_cache.pop(model_kind, None)
        return _empty_status("lifecycle_unavailable")
    try:
        incumbent = state.reference(model_kind)
        candidate = state.reference(model_kind, "candidate")
        for reference in (incumbent, candidate):
            if reference is not None:
                if reference.epoch != state.epoch:
                    raise ValueError("Artifact epoch mismatch")
                verify_reference(reference)
        now = _now()
        key = (
            state.epoch,
            state.revision,
            LIFECYCLE_SCHEMAS[model_kind],
            incumbent.artifact_hash if incumbent else None,
            candidate.artifact_hash if candidate else None,
        )
        cached = _artifact_status_cache.get(model_kind)
        cache_time = _cache_time()
        if cached is not None and cached[0] == key and cache_time < cached[1]:
            status = copy.deepcopy(cached[2])
        else:
            status = _base_status(incumbent)
            _artifact_status_cache[model_kind] = (
                key,
                cache_time + _CACHE_MAX_AGE,
                copy.deepcopy(status),
            )
        reporting_state = "unqualified"
        expiry = incumbent.qualification_expires_at if incumbent else None
        last_outcome = incumbent.last_valid_outcome_at if incumbent else None
        if (
            incumbent is not None
            and len({incumbent.qualified_at is None, expiry is None, last_outcome is None}) != 1
        ):
            raise ValueError("Invalid qualification schema")
        if incumbent is None and candidate is not None:
            reporting_state = "candidate"
        elif incumbent is not None and incumbent.qualified_at is not None:
            if (
                expiry is None
                or last_outcome is None
                or not incumbent.qualification_scope
                or not last_outcome <= incumbent.qualified_at < expiry
                or expiry > last_outcome + dt.timedelta(hours=72)
            ):
                raise ValueError("Invalid qualification schema")
            reporting_state = "expired" if now >= expiry else "qualified-for-reporting"
        progress = _reporting_progress(model_kind, state, candidate or incumbent, evidence, now)
        status.update(
            {
                **progress,
                "status": reporting_state,
                "incumbent_status": reporting_state if incumbent else "unqualified",
                "lifecycle_epoch": state.epoch,
                "eligibility_revision": state.revision,
                "committed_hash": incumbent.artifact_hash if incumbent else None,
                "candidate_hash": candidate.artifact_hash if candidate else None,
                "candidate_status": "candidate" if candidate else None,
                "qualification_expires_at": expiry.isoformat() if expiry else None,
                "last_valid_outcome_at": last_outcome.isoformat() if last_outcome else None,
                "qualification_scope": copy.deepcopy(incumbent.qualification_scope or {})
                if incumbent
                else {},
                "candidate_progress": {
                    "status": "candidate",
                    "artifact_hash": candidate.artifact_hash,
                    "control_ready": False,
                    **copy.deepcopy(progress),
                }
                if candidate
                else None,
            }
        )
        if last_outcome is None and evidence is not None:
            status["last_valid_outcome_at"] = evidence.get("last_valid_outcome_at")
        return status
    except ArtifactUnavailableError:
        _artifact_status_cache.pop(model_kind, None)
        return _empty_status("artifact_unavailable")
    except Exception:
        _artifact_status_cache.pop(model_kind, None)
        return _empty_status("integrity_or_schema_failed")


async def get_model_status(
    model_kind: str, model_dir: Path, *, lifecycle: Lifecycle | None = None
) -> dict[str, object]:
    if model_kind == "cop":
        return _inspect_model_artifact(model_kind, model_dir)
    lifecycle = lifecycle or Lifecycle()
    try:
        state = await lifecycle.read()
    except Exception:
        _artifact_status_cache.pop(model_kind, None)
        return _empty_status("lifecycle_unavailable")
    return await _component_reporting_status(model_kind, model_dir, lifecycle, state)


async def _component_reporting_status(
    model_kind: str, model_dir: Path, lifecycle: Lifecycle, state: LifecycleSnapshot
) -> dict[str, object]:
    try:
        reference = state.reference(model_kind, "candidate") or state.reference(model_kind)
        evidence = (
            await lifecycle.reporting_evidence(reference, now=_now())
            if reference is not None and reference.manifest.get("frozen_at")
            else None
        )
        return await asyncio.to_thread(
            _inspect_model_artifact, model_kind, model_dir, state, evidence
        )
    except Exception:
        _artifact_status_cache.pop(model_kind, None)
        return _empty_status("reporting_evidence_unavailable")


async def get_model_reporting_statuses(
    model_dir: Path | None = None, *, lifecycle: Lifecycle | None = None
) -> dict[str, dict[str, object]]:
    from packages.core.config import settings

    lifecycle = lifecycle or Lifecycle()
    model_dir = model_dir or Path(settings.model_dir)
    try:
        state = await lifecycle.read()
    except Exception:
        return {
            component: _empty_status("lifecycle_unavailable") for component in LIFECYCLE_SCHEMAS
        }
    statuses = await asyncio.gather(
        *(
            _component_reporting_status(component, model_dir, lifecycle, state)
            for component in LIFECYCLE_SCHEMAS
        )
    )
    return dict(zip(LIFECYCLE_SCHEMAS, statuses, strict=True))


def clear_artifact_status_cache() -> None:
    """Clear cached snapshots for tests and explicit process-local reset points."""
    _artifact_status_cache.clear()

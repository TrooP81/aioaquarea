"""Reporting status from committed lifecycle authority, never directory discovery."""

from __future__ import annotations

import datetime as dt
import asyncio
import copy
import hashlib
import json
import math
import pickle
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from sqlalchemy import func, select

from packages.core.database import get_session

from packages.ml.lifecycle import (
    LIFECYCLE_SCHEMAS,
    ArtifactReference,
    ArtifactUnavailableError,
    DEMAND_CADENCE,
    Lifecycle,
    LifecycleSnapshot,
    LifecycleUnavailable,
    MAINTENANCE_CADENCE,
    reference_is_qualified,
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
        counts = None
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
        "evidence_reason": "no_selected_reference"
        if reference is None
        else None
        if evidence
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
    *,
    as_of: dt.datetime | None = None,
    context: dict | None = None,
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

    use_cache = as_of is None and context is None
    if state is None:
        if use_cache:
            _artifact_status_cache.pop(model_kind, None)
        return _empty_status("lifecycle_unavailable")
    progress = {}
    identity = {}
    try:
        incumbent = state.reference(model_kind)
        candidate = state.reference(model_kind, "candidate")
        now = as_of if as_of is not None else _now()
        progress = _reporting_progress(model_kind, state, candidate or incumbent, evidence, now)
        identity = {
            "lifecycle_epoch": state.epoch,
            "eligibility_revision": state.revision,
            "committed_hash": incumbent.artifact_hash if incumbent else None,
            "candidate_hash": candidate.artifact_hash if candidate else None,
        }
        for reference in (incumbent, candidate):
            if reference is not None:
                if reference.epoch != state.epoch:
                    raise ValueError("Artifact epoch mismatch")
                verify_reference(reference)
        key = (
            state.epoch,
            state.revision,
            LIFECYCLE_SCHEMAS[model_kind],
            incumbent.artifact_hash if incumbent else None,
            candidate.artifact_hash if candidate else None,
        )
        cached = _artifact_status_cache.get(model_kind) if use_cache else None
        cache_time = _cache_time()
        if cached is not None and cached[0] == key and cache_time < cached[1]:
            status = copy.deepcopy(cached[2])
        else:
            status = _base_status(incumbent)
            if use_cache:
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
        if use_cache:
            _artifact_status_cache.pop(model_kind, None)
        return {**_empty_status("artifact_unavailable"), **progress, **identity}
    except Exception:
        if use_cache:
            _artifact_status_cache.pop(model_kind, None)
        return {**_empty_status("integrity_or_schema_failed"), **progress, **identity}


async def get_model_status(
    model_kind: str,
    model_dir: Path,
    *,
    lifecycle: Lifecycle | None = None,
    snapshot: LifecycleSnapshot | None = None,
    evidence_by_component: dict | None = None,
    as_of: dt.datetime | None = None,
    context: dict | None = None,
) -> dict[str, object]:
    if model_kind == "cop":
        return _inspect_model_artifact(model_kind, model_dir)
    if snapshot is not None or context is not None or evidence_by_component is not None:
        return await asyncio.to_thread(
            _inspect_model_artifact,
            model_kind,
            model_dir,
            snapshot,
            (evidence_by_component or {}).get(model_kind),
            as_of=as_of,
            context=context,
        )
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
    model_dir: Path | None = None,
    *,
    lifecycle: Lifecycle | None = None,
    snapshot: LifecycleSnapshot | None = None,
    evidence_by_component: dict | None = None,
    as_of: dt.datetime | None = None,
    context: dict | None = None,
) -> dict[str, dict[str, object]]:
    from packages.core.config import settings

    model_dir = model_dir or Path(settings.model_dir)
    if snapshot is not None or context is not None or evidence_by_component is not None:
        return await asyncio.to_thread(
            _inspect_snapshot_statuses,
            model_dir,
            snapshot,
            evidence_by_component or {},
            as_of,
            context,
        )
    lifecycle = lifecycle or Lifecycle()
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


def _inspect_snapshot_statuses(model_dir, snapshot, evidence_by_component, as_of, context):
    return {
        component: _inspect_model_artifact(
            component,
            model_dir,
            snapshot,
            evidence_by_component.get(component),
            as_of=as_of,
            context=context,
        )
        for component in LIFECYCLE_SCHEMAS
    }


_READINESS_MAX_WAITERS = 32
_READINESS_MAX_BYTES = 64 * 1024 * 1024
_READINESS_MATERIALIZATION_SECONDS = 5.0
_READINESS_RESPONSE_SECONDS = 15.0
_READINESS_FLIGHTS: WeakKeyDictionary = WeakKeyDictionary()


@dataclass
class _ReadinessFlight:
    generation: str
    as_of: dt.datetime
    deadline: float
    task: asyncio.Task | None = None
    waiters: int = 0
    worker_started: bool = False
    joinable: bool = True


def _unavailable_collection(reason, *, demand=False):
    return {
        "availability": "unavailable",
        "total": None,
        "train": None,
        "holdout": None,
        "minimum_train": 168 if demand else 20,
        "minimum_holdout": 24 if demand else 3,
        "remaining_train": None,
        "remaining_holdout": None,
        "reason": reason,
    }


def _maintenance_settings(values):
    from packages.core.settings_service import SETTING_SPECS

    enabled, preference = None, "unknown"
    try:
        enabled = bool(
            SETTING_SPECS["ml_maintenance_enabled"].parse(values["ml_maintenance_enabled"])
        )
    except (ValueError, KeyError):
        pass
    try:
        preference = (
            "automatic"
            if SETTING_SPECS["seasonal_calibration_auto_train"].parse(
                values["seasonal_calibration_auto_train"]
            )
            else "manual"
        )
    except (ValueError, KeyError):
        pass
    return enabled, preference


def _unavailable_readiness(
    reason, *, as_of=None, generation=None, context=None, seasonal_inputs=None
):
    enabled, preference = (
        _maintenance_settings(context["settings"]) if context else (None, "unknown")
    )
    components = {
        name: {
            "collection": _unavailable_collection(reason, demand=name == "demand"),
            "fit": {"availability": "unavailable", "status": None, "reason": reason},
            "reporting": {
                "status": "unavailable",
                "reason": reason,
                "counts": None,
                "missing_counts": None,
                "missing_diversity": None,
                "exclusion_counts": {},
                "pending_outcomes": None,
                "next_decision_checkpoint": None,
                "last_decision": None,
                "qualification_expires_at": None,
                "last_valid_outcome_at": None,
                "evidence_as_of": None,
                "evidence_reason": reason,
                "artifact_hash": None,
                "scope": None,
                "current_scope_match": None,
            },
            "next_step": "readiness_unavailable",
        }
        for name in ("demand", "indoor_heating", "indoor_cooling")
    }
    return {
        "as_of": as_of,
        "context": context,
        "seasonal_inputs": seasonal_inputs,
        "device_quality": None,
        "model_lifecycle": {component: _empty_status(reason) for component in LIFECYCLE_SCHEMAS},
        "demand_quality": {
            "usable_samples": None,
            "minimum_samples": 168,
            "remaining_samples": None,
            "ready_to_train": False,
            "training_blocker": reason,
        },
        "thermal_model": _thermal_diagnostic_fields(None, None, as_of),
        "readiness": {
            "as_of": as_of.isoformat() if as_of else None,
            "generation": generation,
            "context_digest": None,
            "source_watermarks": {},
            "availability": "unavailable",
            "reason": reason,
            "components": components,
            "maintenance": {
                "enabled": enabled,
                "preference": preference,
                "queue_count": None,
                "running": None,
                "phase": None,
                "last_attempt_at": None,
                "next_due_at": None,
                "reason": reason,
            },
        },
    }


def _copied_input_size(value):
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if total > _READINESS_MAX_BYTES:
            raise LifecycleUnavailable("readiness_input_budget_exceeded")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif isinstance(item, SimpleNamespace):
            pending.append(vars(item))
        elif is_dataclass(item):
            pending.extend(getattr(item, field.name) for field in fields(item))
    return total


async def _capture_readiness_inputs(*, as_of, generation, components, supplied_context=None):
    from packages.core.config import settings
    from packages.core.models import (
        ConsumptionRecord,
        DeviceStatusRecord,
        MLMaintenanceRequestRecord,
    )
    from packages.core.settings_service import get_all_settings
    from packages.ml.demand_model_core import DemandModel
    from packages.ml.forecast_quality import materialize_native_inputs
    from packages.ml.seasonal_learning import materialize_seasonal_inputs
    from packages.ml.thermal import materialize_thermal_inputs
    from packages.poller.smartthings import get_selected_device_ids

    captured = {
        "as_of": as_of,
        "generation": generation,
        "context": None,
        "state": None,
        "thermal": None,
        "demand": None,
        "native": None,
        "seasonal_inputs": None,
        "latest_status": None,
        "evidence": {},
        "requests": {},
        "running_requests": {},
        "queue_counts": None,
        "source_records": None,
        "model_dir": Path(settings.model_dir),
    }
    authority = Lifecycle()
    copied_bytes = 0
    copied_ids = set()

    def claim_copy(item):
        nonlocal copied_bytes
        pending = [item]
        copied_bytes += 8
        while pending:
            value = pending.pop()
            if id(value) in copied_ids:
                continue
            copied_ids.add(id(value))
            copied_bytes += sys.getsizeof(value)
            if copied_bytes > _READINESS_MAX_BYTES:
                raise LifecycleUnavailable("readiness_input_budget_exceeded")
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
            elif isinstance(value, SimpleNamespace):
                pending.append(vars(value))

    try:
        async with get_session(read_only=True, isolation_level="REPEATABLE READ") as session:
            values = await get_all_settings(session=session)
            selected = await get_selected_device_ids(values=values)
            context = {
                "settings": values,
                "as_of": as_of,
                "selected_ids": selected,
                "reference_sensor_id": str(values.get("comfort_reference_sensor_id") or "").strip(),
                "weather_provider": str(values.get("weather_provider") or "open-meteo"),
                "demand_max_rate_kw": DemandModel._max_plausible_rate_kw(),
                "schemas": dict(LIFECYCLE_SCHEMAS),
                "_readiness_copy_claim": claim_copy,
            }
            captured["context"] = context
            device_id = (supplied_context or {}).get("device_id")
            status_query = select(DeviceStatusRecord).where(DeviceStatusRecord.ts <= as_of)
            if device_id:
                status_query = status_query.where(DeviceStatusRecord.device_id == device_id)
            latest = (
                await session.execute(
                    status_query.order_by(
                        DeviceStatusRecord.ts.desc(), DeviceStatusRecord.device_id
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None:
                captured["latest_status"] = SimpleNamespace(
                    **{
                        column.name: copy.deepcopy(getattr(latest, column.name))
                        for column in DeviceStatusRecord.__table__.columns
                    }
                )
            context["device_id"] = device_id or (latest.device_id if latest is not None else None)
            try:
                captured["state"] = await authority.read(session=session)
            except LifecycleUnavailable as error:
                if str(error) != "lifecycle_not_initialized":
                    raise
            captured["seasonal_inputs"] = await materialize_seasonal_inputs(
                now=as_of,
                window_days=int(values["seasonal_calibration_window_days"]),
                session=session,
            )
            device_id = context["device_id"]
            if device_id:
                captured["source_records"] = (
                    await session.execute(
                        select(func.count())
                        .select_from(ConsumptionRecord)
                        .where(
                            ConsumptionRecord.device_id == device_id, ConsumptionRecord.ts <= as_of
                        )
                    )
                ).scalar_one()
                if "thermal" in components:
                    captured["thermal"] = await materialize_thermal_inputs(
                        device_id,
                        context["reference_sensor_id"],
                        context["weather_provider"],
                        as_of,
                        selected_ids=selected,
                        session=session,
                        context=context,
                        raw=True,
                    )
                    _copied_input_size(captured)
                if "demand" in components:
                    captured["demand"] = await DemandModel()._materialize_training_rows(
                        cutoff=as_of,
                        device_id=device_id,
                        weather_provider=context["weather_provider"],
                        lifecycle_evidence=True,
                        session=session,
                        context=context,
                    )
                    _copied_input_size(captured)
                captured["native"] = await materialize_native_inputs(
                    "thermal",
                    now=as_of,
                    device_id=device_id,
                    timer_reader=None,
                    session=session,
                    context=context,
                    raw=True,
                )
            state = captured["state"]
            if state is not None:
                request = MLMaintenanceRequestRecord
                grouped = (
                    await session.execute(
                        select(request.status, func.count())
                        .where(request.epoch == state.epoch, request.requested_at <= as_of)
                        .group_by(request.status)
                    )
                ).all()
                captured["queue_counts"] = {status: int(count) for status, count in grouped}
                for component in components:
                    latest_request = (
                        await session.execute(
                            select(request)
                            .where(
                                request.epoch == state.epoch,
                                request.component == component,
                                request.attempted_at <= as_of,
                            )
                            .order_by(request.attempted_at.desc(), request.requested_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    running = (
                        await session.execute(
                            select(request)
                            .where(
                                request.epoch == state.epoch,
                                request.component == component,
                                request.status == "running",
                                request.requested_at <= as_of,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    for key, row in (("requests", latest_request), ("running_requests", running)):
                        if row is not None:
                            captured[key][component] = {
                                column.name: copy.deepcopy(getattr(row, column.name))
                                for column in request.__table__.columns
                            }
                    reference = state.reference(component, "candidate") or state.reference(
                        component
                    )
                    if reference is not None and reference.manifest.get("frozen_at"):
                        captured["evidence"][component] = await authority.reporting_evidence(
                            reference, now=as_of, session=session
                        )
            _copied_input_size(captured)
    except Exception as error:
        captured["unavailable_reason"] = (
            str(error)
            if isinstance(error, LifecycleUnavailable)
            and str(error) == "readiness_input_budget_exceeded"
            else "readiness_row_limit_exceeded"
            if isinstance(error, ValueError) and "overflow" in str(error)
            else "snapshot_materialization_unavailable"
        )
    finally:
        if captured["context"] is not None:
            captured["context"].pop("_readiness_copy_claim", None)
    return captured


def _watermark(rows, clock):
    times = [
        getattr(row, clock) for row in rows if isinstance(getattr(row, clock, None), dt.datetime)
    ]
    return {
        "copied_rows": len(rows),
        "first_at": min(times).isoformat() if times else None,
        "last_at": max(times).isoformat() if times else None,
        "content_digest": hashlib.sha256(
            pickle.dumps([vars(row) for row in rows], protocol=5)
        ).hexdigest(),
    }


def _maintenance_readiness(captured):
    values = captured["context"]["settings"]
    enabled, preference = _maintenance_settings(values)
    state, now = captured["state"], captured["as_of"]
    running = captured["running_requests"]
    proven = [
        row
        for row in running.values()
        if state is not None
        and enabled
        and row["epoch"] == state.epoch
        and row["fence"] == state.fence
        and state.lease_owner
        and state.lease_expires_at
        and state.lease_expires_at > now
        and row["status"] == "running"
        and row["attempted_at"]
        and row["attempted_at"] <= now
        and row["finished_at"] is None
        and isinstance(row["details"].get("phase"), str)
        and row["details"]["phase"]
        in {"materializing", "preprocessing", "fitting", "publishing", "reviewing"}
    ]
    proof = proven[0] if len(proven) == 1 else None
    counts = captured["queue_counts"]
    return {
        "enabled": enabled,
        "preference": preference,
        "queue_count": counts.get("queued", 0) if proof and counts is not None else None,
        "running": True if proof else None,
        "phase": proof["details"]["phase"] if proof else None,
        "request_id": proof["id"] if proof else None,
        "last_attempt_at": proof["attempted_at"].isoformat() if proof else None,
        "next_due_at": state.next_maintenance_at.isoformat()
        if proof and state.next_maintenance_at
        else None,
        "reason": None
        if proof
        else "running_request_proof_unavailable"
        if running
        else "lifecycle_not_initialized"
        if state is None
        else "no_running_request",
    }


def _qualified_reference_covers(component, reference, now):
    if reference is None or not isinstance(reference.qualification_scope, dict):
        return False
    declared = reference.manifest.get("capabilities")
    qualified = reference.qualification_scope.get("capabilities")
    return bool(
        isinstance(declared, (list, tuple))
        and all(isinstance(capability, str) and capability for capability in declared)
        and component in declared
        and isinstance(qualified, (list, tuple))
        and all(isinstance(capability, str) and capability for capability in qualified)
        and component in qualified
        and reference_is_qualified(reference, now=now)
    )


def _fit_readiness(component, captured, statuses):
    family = "demand" if component == "demand" else "thermal"
    state, status = captured["state"], statuses[family]
    reference = (state.reference(family, "candidate") or state.reference(family)) if state else None
    request = captured["requests"].get(family)
    diagnostics = (request or {}).get("details", {}).get("readiness_rejection", {})
    measured = diagnostics.get("components", {}).get(component)
    if measured and request["status"] in {"queued", "failed"}:
        return {
            "availability": "persisted",
            "status": measured.get("fit_status"),
            "data_cutoff": diagnostics.get("data_cutoff"),
            "artifact_hash": None,
            "role": None,
            "request_id": request["id"],
            "requested_family": diagnostics.get("requested_family"),
            "validation": copy.deepcopy(measured.get("validation")),
            "reason": diagnostics.get("reason"),
        }
    if reference is None:
        return {
            "availability": "not_assessed",
            "status": "awaiting_fit",
            "data_cutoff": None,
            "artifact_hash": None,
            "request_id": None,
            "reason": "no_persisted_fit",
        }
    metrics = reference.manifest.get("metrics", {})
    validation = (
        metrics
        if component == "demand"
        else metrics.get(component)
        if isinstance(metrics, dict)
        else None
    )
    invalid_metrics = not isinstance(metrics, dict) or (
        component != "demand" and component in metrics and not isinstance(validation, dict)
    )
    unavailable = status["status"] == "unavailable" or invalid_metrics
    declared = component == "demand" or component in reference.manifest.get("capabilities", ())
    request_id = (
        request["id"]
        if request
        and request["status"] == "succeeded"
        and request["details"].get("revision") == reference.committed_revision
        else None
    )
    return {
        "availability": "unavailable" if unavailable else "persisted",
        "status": "validated"
        if declared and validation and not unavailable
        else "not_declared"
        if not declared
        else None,
        "data_cutoff": reference.manifest.get("data_cutoff"),
        "artifact_hash": reference.artifact_hash,
        "role": reference.role,
        "request_id": request_id,
        "validation": None if invalid_metrics else copy.deepcopy(validation),
        "reason": "integrity_or_schema_failed"
        if invalid_metrics
        else status.get("unavailable_reason"),
    }


def _thermal_diagnostic_fields(state, status, as_of):
    result = {
        "calibrated": False,
        "tank_heating_rate": None,
        "confidence": "default",
        "indoor_heating_confidence": "default",
        "indoor_heating_samples": None,
        "calibration_status": {},
        "last_calibrated": None,
        "sample_basis": "artifact_diagnostics",
    }
    if state is None or status is None or status["status"] == "unavailable":
        return result
    reference = state.reference("thermal", "candidate") or state.reference("thermal")
    if reference is None:
        return result
    try:
        from packages.ml import safe_persistence
        from packages.ml.lifecycle import reference_is_qualified
        from packages.ml.thermal import ThermalModel

        payload = safe_persistence.load_signed_artifact(
            reference.artifact_hash,
            component="thermal",
            schema_version=LIFECYCLE_SCHEMAS["thermal"],
        )
        params = ThermalModel._decode_params(payload["params"])
        learned = reference_is_qualified(reference, now=as_of)
        result.update(
            {
                "calibrated": params.last_calibrated is not None,
                "tank_heating_rate": round(params.tank_heating_rate, 2),
                "confidence": "learned"
                if learned and "tank_heating" in payload["declared_components"]
                else "default",
                "indoor_heating_confidence": "learned"
                if learned and "indoor_heating" in payload["declared_components"]
                else "default",
                "indoor_heating_samples": params.indoor_heating_samples,
                "calibration_status": dict(params.calibration_status),
                "last_calibrated": params.last_calibrated.isoformat()
                if params.last_calibrated
                else None,
            }
        )
    except Exception:
        pass
    return result


def _compose_readiness(captured):
    from packages.core.device_data_quality import device_data_quality_from_inputs
    from packages.ml.comfort_model import ComfortModel
    from packages.ml.demand_model_core import DemandModel, _demand_sample_counts
    from packages.ml.forecast_quality import _prepare_native_inputs, native_scope
    from packages.ml.thermal import _rate_sample_counts, _thermal_rate_samples

    context, now = captured["context"], captured["as_of"]
    if context is None or captured.get("unavailable_reason") in {
        "readiness_input_budget_exceeded",
        "readiness_row_limit_exceeded",
    }:
        return _unavailable_readiness(
            captured.get("unavailable_reason", "snapshot_unavailable"),
            as_of=now,
            generation=captured["generation"],
            context=context,
            seasonal_inputs=captured["seasonal_inputs"],
        )
    watermarks = {}
    collections = {
        name: _unavailable_collection("source_not_assessed", demand=name == "demand")
        for name in ("demand", "indoor_heating", "indoor_cooling")
    }
    quality = {
        "usable_samples": None,
        "minimum_samples": 168,
        "remaining_samples": None,
        "ready_to_train": False,
        "training_blocker": "source_not_assessed",
    }
    thermal = captured["thermal"]
    if thermal is not None:
        for name, rows, clock in (
            ("thermal_status", thermal["records"], "ts"),
            ("thermal_weather", thermal["weather"], "ts"),
            ("indoor", thermal["readings"], "timestamp"),
        ):
            watermarks[name] = _watermark(rows, clock)
        if thermal.get("indoor_blocker"):
            for name in ("indoor_heating", "indoor_cooling"):
                collections[name] = _unavailable_collection(thermal["indoor_blocker"])
                collections[name]["availability"] = "not_assessed"
        else:
            readings, _, _ = ComfortModel._select_indoor_observations(
                thermal["readings"],
                context["reference_sensor_id"],
                selected_ids=context["selected_ids"],
                as_of=now,
            )
            samples = _thermal_rate_samples({**thermal, "readings": readings})
            collections.update(
                {
                    name: _rate_sample_counts(samples[name])
                    for name in ("indoor_heating", "indoor_cooling")
                }
            )
    if captured["demand"] is not None:
        demand = captured["demand"]
        for name, rows, clock in (
            ("consumption", demand["consumption_rows"], "ts"),
            ("demand_weather", demand["weather_rows"], "ts"),
            ("demand_status", demand["statuses"], "ts"),
        ):
            watermarks[name] = _watermark(rows, clock)
        prepared = DemandModel._preprocess_materialized(demand)
        collections["demand"] = _demand_sample_counts(prepared)
        quality = prepared["quality"]
    statuses = _inspect_snapshot_statuses(
        captured["model_dir"], captured["state"], captured["evidence"], now, context
    )
    statuses["demand"]["source_records"] = captured["source_records"]
    maintenance = _maintenance_readiness(captured)
    for status in statuses.values():
        status["maintenance_enabled"] = maintenance["enabled"]
        status["sample_basis"] = "artifact_diagnostics"
    components = {}
    for name, collection in collections.items():
        family = "demand" if name == "demand" else "thermal"
        status = statuses[family]
        fit = _fit_readiness(name, captured, statuses)
        reporting = {
            key: copy.deepcopy(status.get(key))
            for key in (
                "counts",
                "missing_counts",
                "missing_diversity",
                "exclusion_counts",
                "pending_outcomes",
                "next_decision_checkpoint",
                "last_decision",
                "qualification_expires_at",
                "last_valid_outcome_at",
                "evidence_as_of",
                "evidence_reason",
            )
        }
        state = captured["state"]
        reference = (
            (state.reference(family, "candidate") or state.reference(family)) if state else None
        )
        reporting.update(
            {
                "status": status["status"],
                "artifact_hash": reference.artifact_hash if reference else None,
                "scope": copy.deepcopy(reference.manifest.get("scope")) if reference else None,
                "current_scope_match": None,
            }
        )
        if fit["availability"] == "unavailable":
            reporting["status"] = "unavailable"
            reporting["reason"] = fit["reason"]
            if fit["reason"] == "integrity_or_schema_failed" and isinstance(
                status.get("metrics"), dict
            ):
                status["metrics"][name] = None
        elif reporting["status"] == "qualified-for-reporting" and not _qualified_reference_covers(
            name, reference, now
        ):
            reporting["status"] = (
                "candidate" if reference and reference.role == "candidate" else "unqualified"
            )
        if reference is not None and captured["native"] is not None:
            inputs = _prepare_native_inputs(
                captured["native"],
                component=family,
                capabilities=tuple(reference.manifest.get("capabilities", ())),
            )
            if inputs is not None:
                reporting["current_scope_match"] = native_scope(inputs) == reference.manifest.get(
                    "scope"
                )
        next_step = (
            "readiness_unavailable"
            if collection["availability"] != "assessed" or fit["availability"] == "unavailable"
            else (
                "collect_space_heating_evidence"
                if collection["remaining_train"] or collection["remaining_holdout"]
                else "ready_for_reporting"
                if reporting["status"] == "qualified-for-reporting"
                else "await_prospective_reporting_qualification"
                if fit["status"] == "validated" and reference
                else "await_fit_or_validation"
            )
        )
        components[name] = {
            "collection": collection,
            "fit": fit,
            "reporting": reporting,
            "next_step": next_step,
        }
    digest_context = {
        "as_of": now.isoformat(),
        "device_id": context["device_id"],
        "reference_sensor_id": context["reference_sensor_id"],
        "selected_ids": context["selected_ids"],
        "weather_provider": context["weather_provider"],
        "timezone": context["settings"]["timezone"],
        "schemas": context["schemas"],
        "settings": {
            key: context["settings"].get(key)
            for key in (
                "learning_mode_enabled",
                "seasonal_calibration_enabled",
                "seasonal_calibration_max_outdoor_c",
                "seasonal_calibration_window_days",
                "seasonal_calibration_auto_train",
                "ml_maintenance_enabled",
                "_seasonal_calibration_campaign_since",
                "_seasonal_calibration_safety_deferred_since",
                "optimizer_layer",
                "device_status_max_age_minutes",
                "poll_interval_seconds",
            )
        },
        "epoch": captured["state"].epoch if captured["state"] else None,
        "revision": captured["state"].revision if captured["state"] else None,
        "selected_hashes": {
            key: status["candidate_hash"] or status["committed_hash"]
            for key, status in statuses.items()
        },
        "source_watermarks": watermarks,
    }
    return {
        "as_of": now,
        "context": context,
        "seasonal_inputs": captured["seasonal_inputs"],
        "device_quality": device_data_quality_from_inputs(
            now=now, values=context["settings"], status=captured["latest_status"]
        ),
        "model_lifecycle": statuses,
        "demand_quality": quality,
        "thermal_model": _thermal_diagnostic_fields(captured["state"], statuses["thermal"], now),
        "readiness": {
            "as_of": now.isoformat(),
            "generation": captured["generation"],
            "context_digest": hashlib.sha256(
                json.dumps(digest_context, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "source_watermarks": watermarks,
            "availability": "unavailable" if captured.get("unavailable_reason") else "assessed",
            "reason": captured.get("unavailable_reason"),
            "components": components,
            "maintenance": maintenance,
        },
    }


async def _run_readiness_generation(flight, *, components, supplied_context):
    executor, future, captured = None, None, None
    try:
        async with asyncio.timeout(_READINESS_MATERIALIZATION_SECONDS):
            captured = await _capture_readiness_inputs(
                as_of=flight.as_of,
                generation=flight.generation,
                components=components,
                supplied_context=supplied_context,
            )
        _copied_input_size(captured)
        if not flight.joinable:
            raise asyncio.CancelledError
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readiness")
        flight.worker_started = True
        future = asyncio.get_running_loop().run_in_executor(
            executor, partial(_compose_readiness, captured)
        )
        result = await asyncio.shield(future)
        if not flight.joinable or time.monotonic() >= flight.deadline:
            return _unavailable_readiness(
                "readiness_response_deadline", as_of=flight.as_of, generation=flight.generation
            )
        return result
    except asyncio.CancelledError:
        if future is not None:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        raise
    except Exception as error:
        reason = (
            "readiness_materialization_deadline"
            if isinstance(error, TimeoutError)
            else (
                "readiness_input_budget_exceeded"
                if isinstance(error, LifecycleUnavailable)
                and str(error) == "readiness_input_budget_exceeded"
                else "readiness_unavailable"
            )
        )
        return _unavailable_readiness(
            reason,
            as_of=flight.as_of,
            generation=flight.generation,
            context=captured.get("context") if captured else None,
            seasonal_inputs=captured.get("seasonal_inputs") if captured else None,
        )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


async def get_readiness_snapshot(
    *,
    route: str,
    arguments=(),
    components=tuple(LIFECYCLE_SCHEMAS),
    authorization_scope="api-token",
    database_binding=None,
    as_of: dt.datetime | None = None,
    context: dict | None = None,
):
    from packages.core.config import settings

    loop = asyncio.get_running_loop()
    key = (
        route,
        json.dumps(arguments, sort_keys=True, default=str),
        tuple(sorted(components)),
        authorization_scope,
        database_binding or settings.database_url,
        as_of.isoformat() if as_of else None,
        json.dumps(context, sort_keys=True, default=str) if context else None,
    )
    current = _READINESS_FLIGHTS.get(loop)
    if current is not None and current[1].task.done():
        _READINESS_FLIGHTS.pop(loop)
        current = None
    if current is not None and (
        current[0] != key or not current[1].joinable or current[1].waiters >= _READINESS_MAX_WAITERS
    ):
        return _unavailable_readiness("readiness_busy")
    if current is None:
        cutoff = as_of if as_of is not None else _now()
        cutoff = (
            cutoff.replace(tzinfo=dt.timezone.utc)
            if cutoff.tzinfo is None
            else cutoff.astimezone(dt.timezone.utc)
        )
        flight = _ReadinessFlight(
            str(uuid.uuid4()), cutoff, time.monotonic() + _READINESS_RESPONSE_SECONDS
        )
        flight.task = loop.create_task(
            _run_readiness_generation(flight, components=components, supplied_context=context)
        )
        _READINESS_FLIGHTS[loop] = (key, flight)

        def finished(task):
            active = _READINESS_FLIGHTS.get(loop)
            if active is not None and active[1] is flight:
                _READINESS_FLIGHTS.pop(loop)
            if not task.cancelled():
                task.exception()

        flight.task.add_done_callback(finished)
    else:
        flight = current[1]
    flight.waiters += 1
    try:
        result = await asyncio.wait_for(
            asyncio.shield(flight.task), max(0.001, flight.deadline - time.monotonic())
        )
        return copy.deepcopy(result)
    except TimeoutError:
        return _unavailable_readiness(
            "readiness_response_deadline", as_of=flight.as_of, generation=flight.generation
        )
    finally:
        flight.waiters -= 1
        if flight.waiters == 0 and not flight.task.done():
            flight.joinable = False
            if not flight.worker_started:
                flight.task.cancel()
                with suppress(asyncio.CancelledError):
                    await flight.task


def clear_artifact_status_cache() -> None:
    """Clear cached snapshots for tests and explicit process-local reset points."""
    _artifact_status_cache.clear()

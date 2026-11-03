"""Installation-scoped P2.1 settings revisions and immutable plan projections."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.comfort_schedule import (
    TIMEZONE_POLICY,
    normalize_schedule,
    normalize_timezone,
    resolve_hourly_targets,
    timezone_data_version,
)
from packages.core.models import (
    AuditLogRecord,
    HouseContractRevisionRecord,
    HouseInstallationRecord,
    PlanRecord,
    SettingRecord,
)
from packages.core.settings_service import SETTING_SPECS

SCHEMA_VERSION = 2
CONTRACT_SETTING_KEYS = frozenset(
    {
        "comfort_schedule",
        "comfort_temp_min",
        "comfort_temp_target",
        "comfort_temp_max",
        "timezone",
    }
)
CONTRACT_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"house-contract:single-installation").digest()[:8],
    "big",
    signed=True,
)
RESOLVER_SCHEMA = "comfort_schedule_utc_half_open_v1"


@dataclass(frozen=True)
class HousePlanningContract:
    """Validated immutable planning settings for one installation revision."""

    schema_version: int
    installation_id: UUID
    house_id: UUID
    timezone: str
    weekday_hours: tuple[int, ...]
    weekend_hours: tuple[int, ...]
    comfort_temp_min: float
    comfort_temp_target: float
    comfort_temp_max: float
    timezone_policy: str
    tzdata_version: str

    @classmethod
    def from_settings(
        cls, settings: dict[str, str], installation_id: UUID, house_id: UUID
    ) -> HousePlanningContract:
        try:
            timezone_name = normalize_timezone(settings.get("timezone") or "Europe/Amsterdam")
            schedule = normalize_schedule(json.loads(settings["comfort_schedule"]))
            temperatures = {
                key: float(settings[key])
                for key in ("comfort_temp_min", "comfort_temp_target", "comfort_temp_max")
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"planning settings do not form a valid contract: {exc}") from exc

        lower = temperatures["comfort_temp_min"]
        target = temperatures["comfort_temp_target"]
        upper = temperatures["comfort_temp_max"]
        if not all(10 <= value <= 30 for value in temperatures.values()):
            raise ValueError("comfort temperatures must be between 10 and 30 °C")
        if lower > target or target > upper:
            raise ValueError("comfort temperatures must satisfy min ≤ target ≤ max")

        return cls(
            schema_version=SCHEMA_VERSION,
            installation_id=installation_id,
            house_id=house_id,
            timezone=timezone_name,
            weekday_hours=tuple(schedule["weekday"]),
            weekend_hours=tuple(schedule["weekend"]),
            comfort_temp_min=lower,
            comfort_temp_target=target,
            comfort_temp_max=upper,
            timezone_policy=TIMEZONE_POLICY,
            tzdata_version=timezone_data_version(),
        )

    def to_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "installation_id": str(self.installation_id),
            "house_id": str(self.house_id),
            "timezone": self.timezone,
            "comfort_schedule": {
                "weekday": list(self.weekday_hours),
                "weekend": list(self.weekend_hours),
            },
            "comfort_temp_min": self.comfort_temp_min,
            "comfort_temp_target": self.comfort_temp_target,
            "comfort_temp_max": self.comfort_temp_max,
            "timezone_policy": self.timezone_policy,
            "tzdata_version": self.tzdata_version,
        }


class ContractRevisionConflict(ValueError):
    """An optimistic settings update was based on an old contract revision."""

    def __init__(self, current_revision_id: int):
        super().__init__("planning settings changed; reload before saving")
        self.current_revision_id = current_revision_id


class ContractSettingsMismatch(ValueError):
    """Installed planning inputs no longer qualify the saved current revision."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _settings_projection(
    settings: dict[str, str], installation_id: UUID, house_id: UUID
) -> dict[str, Any]:
    return HousePlanningContract.from_settings(settings, installation_id, house_id).to_projection()


def _contract_hash(projection: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(projection).encode("utf-8")).hexdigest()


def policy_dependency_fingerprint(contract: dict[str, Any]) -> str:
    """Hash only the settings that affect schedule/target policy projections."""
    dependencies = {
        "installation_id": contract["installation_id"],
        "resolver_schema": RESOLVER_SCHEMA,
        "timezone_policy": contract["timezone_policy"],
        "tzdata_version": contract["tzdata_version"],
        "timezone": contract["timezone"],
        "comfort_schedule": contract["comfort_schedule"],
        "comfort_temp_min": contract["comfort_temp_min"],
        "comfort_temp_target": contract["comfort_temp_target"],
        "comfort_temp_max": contract["comfort_temp_max"],
    }
    return hashlib.sha256(_canonical_json(dependencies).encode("utf-8")).hexdigest()


def validate_plan_contract_snapshot(plan: PlanRecord, snapshot: object) -> bool:
    """Verify a saved plan snapshot is canonical and bound to its owning row."""
    if not isinstance(snapshot, dict):
        return False
    plan_revision_id = getattr(plan, "contract_revision_id", None)
    plan_contract_hash = getattr(plan, "contract_hash", None)
    if (
        type(plan_revision_id) is not int
        or plan_revision_id <= 0
        or not isinstance(plan_contract_hash, str)
        or len(plan_contract_hash) != 64
        or any(character not in "0123456789abcdef" for character in plan_contract_hash)
    ):
        return False
    revision_id = snapshot.get("revision_id")
    contract_hash = snapshot.get("contract_hash")
    if (
        isinstance(revision_id, bool)
        or not isinstance(revision_id, int)
        or revision_id <= 0
        or revision_id != plan_revision_id
        or not isinstance(contract_hash, str)
        or len(contract_hash) != 64
        or any(character not in "0123456789abcdef" for character in contract_hash)
        or contract_hash != plan_contract_hash
    ):
        return False

    try:
        projection = {
            key: snapshot[key]
            for key in (
                "schema_version",
                "installation_id",
                "house_id",
                "timezone",
                "comfort_schedule",
                "comfort_temp_min",
                "comfort_temp_target",
                "comfort_temp_max",
                "timezone_policy",
                "tzdata_version",
            )
        }
        if (
            type(projection["schema_version"]) is not int
            or projection["schema_version"] != SCHEMA_VERSION
            or str(UUID(projection["installation_id"])) != projection["installation_id"]
            or str(UUID(projection["house_id"])) != projection["house_id"]
            or normalize_timezone(projection["timezone"]) != projection["timezone"]
            or projection["timezone_policy"] != TIMEZONE_POLICY
            or not isinstance(projection["tzdata_version"], str)
            or not projection["tzdata_version"]
        ):
            return False

        schedule = normalize_schedule(projection["comfort_schedule"])
        if schedule != projection["comfort_schedule"]:
            return False
        temperatures = tuple(
            projection[key]
            for key in ("comfort_temp_min", "comfort_temp_target", "comfort_temp_max")
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in temperatures
        ):
            return False
        minimum, target, maximum = temperatures
        if not 10 <= minimum <= target <= maximum <= 30:
            return False
        if _contract_hash(projection) != contract_hash:
            return False
        if snapshot.get("policy_dependency_fingerprint") != policy_dependency_fingerprint(
            projection
        ):
            return False
        if snapshot.get("resolver_schema") != RESOLVER_SCHEMA:
            return False
        if snapshot.get("ordinary_writes_allowed") is not False:
            return False
        if (
            not isinstance(snapshot.get("source"), str)
            or not snapshot["source"]
            or not isinstance(snapshot.get("field_sources"), dict)
            or not isinstance(snapshot.get("effective_at"), str)
        ):
            return False
        effective_at = dt.datetime.fromisoformat(snapshot["effective_at"])
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            return False
        if snapshot.get("scope") != {
            "protected_rooms": "not_configured_in_p2_1",
            "dhw_custom_schedule": "not_configured_in_p2_1",
            "tariff_contract": "unchanged_existing_price_scope",
            "controller_curve_attestation": "not_configured_in_p2_1",
        }:
            return False
        if snapshot.get("p2_defaults") != {
            "protected_rooms": [],
            "dhw_schedule_mode": "inherit_comfort",
            "dhw_custom_schedule": None,
            "tariff_settings": None,
            "curve_attestation": "unknown_not_recorded_by_p2_1",
        }:
            return False

        intervals = snapshot.get("schedule_projection")
        if not isinstance(intervals, list) or not intervals:
            return False
        expected_target = float(target)
        expected_minimum = float(minimum)
        expected_maximum = float(maximum)
        previous_end: dt.datetime | None = None
        for interval in intervals:
            if not isinstance(interval, dict):
                return False
            start = dt.datetime.fromisoformat(interval["start_utc"])
            end = dt.datetime.fromisoformat(interval["end_utc"])
            local_start = dt.datetime.fromisoformat(interval["local_start"])
            local_end = dt.datetime.fromisoformat(interval["local_end"])
            if any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (
                    start,
                    end,
                    local_start,
                    local_end,
                )
            ):
                return False
            start_utc, end_utc = start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)
            if (
                end_utc <= start_utc
                or end_utc - start_utc > dt.timedelta(hours=1)
                or (previous_end is not None and start_utc != previous_end)
                or local_start.astimezone(dt.timezone.utc) != start_utc
                or local_end.astimezone(dt.timezone.utc) != end_utc
            ):
                return False
            previous_end = end_utc

            scheduled = interval.get("scheduled")
            if type(scheduled) is not bool or interval.get("preferred_comfort") is not scheduled:
                return False
            if (
                type(interval.get("local_hour")) is not int
                or interval["local_hour"] != local_start.hour
                or interval.get("local_date") != local_start.date().isoformat()
                or interval.get("day_type")
                != ("weekday" if local_start.weekday() < 5 else "weekend")
                or type(interval.get("fold")) is not int
                or interval["fold"] not in (0, 1)
                or type(interval.get("utc_offset_minutes")) is not int
                or interval["utc_offset_minutes"]
                != int((local_start.utcoffset() or dt.timedelta()).total_seconds() // 60)
                or scheduled != (interval["local_hour"] in schedule[interval["day_type"]])
                or type(interval.get("contract_revision_id")) is not int
                or interval["contract_revision_id"] != revision_id
                or interval.get("contract_hash") != contract_hash
                or interval.get("reason") != f"base_{interval['day_type']}"
            ):
                return False
            expected_values = {
                "target_c": expected_target if scheduled else expected_minimum,
                "outer_min_c": expected_minimum,
                "outer_max_c": expected_maximum,
                "contract_revision_id": revision_id,
                "contract_hash": contract_hash,
                "timezone_policy": TIMEZONE_POLICY,
                "tzdata_version": projection["tzdata_version"],
                "preferred_band": None,
                "room_policy": "not_configured",
                "dhw_preference": "inherit_comfort",
            }
            if any(interval.get(key) != value for key, value in expected_values.items()):
                return False
            if (
                interval.get("timezone_policy") != TIMEZONE_POLICY
                or interval.get("local_hour") not in range(24)
                or interval.get("fold") not in (0, 1)
                or interval.get("day_type") not in {"weekday", "weekend"}
                or interval.get("utc_offset_minutes") is None
                or not isinstance(interval.get("reason"), str)
            ):
                return False
        plan_start = getattr(plan, "horizon_start", None)
        plan_end = getattr(plan, "horizon_end", None)
        if (
            not isinstance(plan_start, dt.datetime)
            or not isinstance(plan_end, dt.datetime)
            or plan_start.tzinfo is None
            or plan_end.tzinfo is None
            or intervals[0]["start_utc"] != plan_start.astimezone(dt.timezone.utc).isoformat()
            or intervals[-1]["end_utc"] != plan_end.astimezone(dt.timezone.utc).isoformat()
        ):
            return False
        # Only replay against the exact authoritative database version. Unknown
        # historical versions are unavailable, never reinterpreted with today's
        # rules or accepted on the strength of self-consistent local strings.
        if projection["tzdata_version"] != timezone_data_version():
            return False
        expected_intervals = resolve_hourly_targets(
            schedule,
            projection["timezone"],
            plan_start,
            plan_end,
            scheduled_target_c=expected_target,
            unscheduled_target_c=expected_minimum,
            outer_max_c=expected_maximum,
        )
        if len(expected_intervals) != len(intervals) or any(
            any(actual.get(key) != value for key, value in expected.items())
            for actual, expected in zip(intervals, expected_intervals)
        ):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def canonical_target_schedule(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Materialize the authoritative interval projection for API and plan consumers."""
    return [
        {
            "hour": index + 1,
            "ts": interval["end_utc"],
            "target": interval["target_c"],
            "comfort_hour": interval["scheduled"],
            "interval_start_utc": interval["start_utc"],
            "interval_end_utc": interval["end_utc"],
            "local_start": interval["local_start"],
            "local_end": interval["local_end"],
            "local_date": interval["local_date"],
            "local_hour": interval["local_hour"],
            "utc_offset_minutes": interval["utc_offset_minutes"],
            "fold": interval["fold"],
            "preferred_comfort": interval["preferred_comfort"],
            "outer_min_c": interval["outer_min_c"],
            "outer_max_c": interval["outer_max_c"],
            "reason": interval["reason"],
            "contract_revision_id": interval["contract_revision_id"],
            "contract_hash": interval["contract_hash"],
            "policy_dependency_fingerprint": snapshot["policy_dependency_fingerprint"],
            "resolver_schema": snapshot["resolver_schema"],
            "timezone_policy": snapshot["timezone_policy"],
            "tzdata_version": snapshot["tzdata_version"],
        }
        for index, interval in enumerate(snapshot["schedule_projection"])
    ]


def demand_dependency_changes(previous: dict[str, Any], current: dict[str, Any]) -> tuple[str, ...]:
    """Return only contract fields that change demand-label interpretation."""
    return tuple(
        key for key in ("timezone", "tzdata_version") if previous.get(key) != current.get(key)
    )


async def _lock_contract(session: AsyncSession) -> None:
    """Global order: contract, then settings/heat-gate, before reading either.

    All settings writers and contract initialization use this order. Locks are
    transaction scoped and reentrant, including canonical PUT's nested write.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CONTRACT_LOCK_KEY})
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {
            "key": int.from_bytes(
                hashlib.sha256(b"settings:heat-gate").digest()[:8], "big", signed=True
            )
        },
    )


async def _setting_sources(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(
        select(SettingRecord.key).where(SettingRecord.key.in_(CONTRACT_SETTING_KEYS))
    )
    persisted = set(rows.scalars().all())
    sources: dict[str, str] = {}
    for key in CONTRACT_SETTING_KEYS:
        spec = SETTING_SPECS[key]
        if key in persisted:
            sources[key] = "database"
        elif spec.default_env and spec.default_env in os.environ:
            sources[key] = "environment"
        else:
            sources[key] = "default"
    return sources


async def _insert_revision(
    session: AsyncSession,
    installation: HouseInstallationRecord,
    settings: dict[str, str],
    *,
    source: str,
    changed_keys: list[str],
) -> HouseContractRevisionRecord:
    projection = _settings_projection(settings, installation.installation_id, installation.house_id)
    digest = _contract_hash(projection)
    current_id = installation.current_revision_id
    previous_revision = (
        await session.get(HouseContractRevisionRecord, current_id)
        if current_id is not None
        else None
    )
    if current_id is not None:
        if previous_revision is not None and previous_revision.contract_hash == digest:
            return previous_revision

    demand_dependency_invalidations = (
        demand_dependency_changes(previous_revision.contract_json, projection)
        if previous_revision is not None
        else ()
    )

    source_map = await _setting_sources(session)
    revision = HouseContractRevisionRecord(
        installation_id=installation.installation_id,
        schema_version=SCHEMA_VERSION,
        contract_hash=digest,
        contract_json={**projection, "field_sources": source_map},
        effective_at=dt.datetime.now(dt.timezone.utc),
        source=source,
    )
    session.add(revision)
    await session.flush()
    previous_revision_id = installation.current_revision_id
    installation.current_revision_id = revision.id
    if demand_dependency_invalidations:
        installation.demand_dependency_generation += 1
    await session.flush()

    from packages.core.plan_lifecycle import supersede_active_plan

    await supersede_active_plan(session, reason="contract_revision_changed")
    if previous_revision_id is not None:
        invalidated_at = dt.datetime.now(dt.timezone.utc)
        await session.execute(
            update(PlanRecord)
            .where(
                PlanRecord.contract_state == "current",
                PlanRecord.contract_revision_id == previous_revision_id,
            )
            .values(
                contract_state="stale",
                contract_invalidated_at=invalidated_at,
                invalidated_by_revision_id=revision.id,
                contract_invalidation_reason="contract_revision_changed",
            )
            .execution_options(synchronize_session=False)
        )

    session.add(
        AuditLogRecord(
            actor="settings_service",
            action="house_contract_saved",
            payload_json=_canonical_json(
                {
                    "revision_id": revision.id,
                    "contract_hash": digest,
                    "changed_keys": sorted(changed_keys),
                    "source": source,
                    "model_dependency_invalidations": [
                        f"demand_{key}" for key in demand_dependency_invalidations
                    ],
                }
            ),
            result="updated",
        )
    )
    return revision


async def ensure_current_contract(
    session: AsyncSession,
    settings: dict[str, str] | None = None,
    *,
    lock: bool = True,
    verify_current_settings: bool = True,
) -> HouseContractRevisionRecord:
    """Load or atomically initialize this deployment's single contract revision."""
    if lock:
        await _lock_contract(session)
    if settings is None:
        from packages.core.settings_service import get_all_settings

        settings = await get_all_settings(session=session)
    installation = await session.get(HouseInstallationRecord, 1)
    if installation is None:
        installation = HouseInstallationRecord(
            singleton_id=1,
            installation_id=uuid4(),
            house_id=uuid4(),
        )
        session.add(installation)
        await session.flush()
        return await _insert_revision(
            session,
            installation,
            settings,
            source="effective_settings_import",
            changed_keys=sorted(CONTRACT_SETTING_KEYS),
        )

    if installation.current_revision_id is None:
        return await _insert_revision(
            session,
            installation,
            settings,
            source="effective_settings_import",
            changed_keys=sorted(CONTRACT_SETTING_KEYS),
        )
    revision = await session.get(HouseContractRevisionRecord, installation.current_revision_id)
    if revision is None:
        raise ValueError("current house-contract pointer does not reference a revision")
    if verify_current_settings:
        projection = _settings_projection(
            settings, installation.installation_id, installation.house_id
        )
        if _contract_hash(projection) != revision.contract_hash:
            raise ContractSettingsMismatch(
                "planning settings changed without a matching contract revision"
            )
    return revision


async def revise_contract(
    session: AsyncSession,
    settings: dict[str, str],
    *,
    source: str,
    changed_keys: list[str],
    expected_revision_id: int | None = None,
) -> HouseContractRevisionRecord:
    """Append a semantic settings revision inside the settings transaction."""
    await _lock_contract(session)
    current = await ensure_current_contract(
        session, settings, lock=False, verify_current_settings=False
    )
    if expected_revision_id is not None and current.id != expected_revision_id:
        raise ContractRevisionConflict(current.id)
    installation = await session.get(HouseInstallationRecord, 1)
    if installation is None:
        raise ValueError("house installation was not initialized")
    return await _insert_revision(
        session,
        installation,
        settings,
        source=source,
        changed_keys=changed_keys,
    )


async def freeze_contract_snapshot(
    session: AsyncSession,
    start: dt.datetime,
    end: dt.datetime,
    *,
    settings: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the revision plus its resolved schedule/target projection."""
    revision = await ensure_current_contract(session, settings)
    return await snapshot_from_revision(session, revision.id, start, end)


async def snapshot_from_revision(
    session: AsyncSession,
    revision_id: int,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    """Project a historical immutable revision over a plan horizon."""
    revision = await session.get(HouseContractRevisionRecord, revision_id)
    if revision is None:
        raise ValueError("house contract revision is unavailable")
    projection = {
        key: value for key, value in revision.contract_json.items() if key != "field_sources"
    }
    if projection.get("tzdata_version") != timezone_data_version():
        raise ValueError(
            "historical contract timezone data differs; replay its persisted plan snapshot"
        )
    intervals = resolve_hourly_targets(
        projection["comfort_schedule"],
        projection["timezone"],
        start,
        end,
        scheduled_target_c=float(projection["comfort_temp_target"]),
        unscheduled_target_c=float(projection["comfort_temp_min"]),
        outer_max_c=float(projection["comfort_temp_max"]),
    )
    for interval in intervals:
        interval["contract_revision_id"] = revision.id
        interval["contract_hash"] = revision.contract_hash
        interval["preferred_band"] = None
        interval["room_policy"] = "not_configured"
        interval["dhw_preference"] = "inherit_comfort"
    return {
        **projection,
        "revision_id": revision.id,
        "contract_hash": revision.contract_hash,
        "effective_at": revision.effective_at.isoformat(),
        "source": revision.source,
        "field_sources": revision.contract_json.get("field_sources", {}),
        "policy_dependency_fingerprint": policy_dependency_fingerprint(projection),
        "resolver_schema": RESOLVER_SCHEMA,
        "schedule_projection": intervals,
        "ordinary_writes_allowed": False,
        "scope": {
            "protected_rooms": "not_configured_in_p2_1",
            "dhw_custom_schedule": "not_configured_in_p2_1",
            "tariff_contract": "unchanged_existing_price_scope",
            "controller_curve_attestation": "not_configured_in_p2_1",
        },
        "p2_defaults": {
            "protected_rooms": [],
            "dhw_schedule_mode": "inherit_comfort",
            "dhw_custom_schedule": None,
            "tariff_settings": None,
            "curve_attestation": "unknown_not_recorded_by_p2_1",
        },
    }


async def current_revision_matches(
    session: AsyncSession, revision_id: int, contract_hash: str
) -> bool:
    revision = await ensure_current_contract(session)
    return revision.id == revision_id and revision.contract_hash == contract_hash


def plan_matches_current_revision(plan: PlanRecord, revision: HouseContractRevisionRecord) -> bool:
    return (
        plan.contract_state == "current"
        and plan.contract_revision_id == revision.id
        and plan.contract_hash == revision.contract_hash
    )

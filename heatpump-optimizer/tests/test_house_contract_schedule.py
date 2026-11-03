from __future__ import annotations

import datetime as dt
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from packages.core.comfort_schedule import (
    TIMEZONE_POLICY,
    normalize_schedule,
    resolve_comfort_intervals,
    resolve_hourly_targets,
    timezone_data_version,
)
from packages.core.house_contract import (
    HousePlanningContract,
    _contract_hash,
    _insert_revision,
    _settings_projection,
    demand_dependency_changes,
    policy_dependency_fingerprint,
    snapshot_from_revision,
    validate_plan_contract_snapshot,
)
from packages.optimizer.main import (
    _canonical_contract_target_schedule,
    _target_schedule_matches_contract,
)

UTC = dt.timezone.utc


@pytest.mark.parametrize(("zone", "minute"), [("Asia/Kolkata", 30), ("Asia/Kathmandu", 15)])
def test_fractional_offsets_keep_exact_local_comfort_boundaries(zone, minute):
    from packages.core.settings_service import is_comfort_hour

    start = dt.datetime(2026, 10, 19, tzinfo=UTC)
    schedule = {"weekday": [6], "weekend": []}
    slots = resolve_hourly_targets(
        schedule,
        zone,
        start,
        start + dt.timedelta(hours=2),
        scheduled_target_c=20.55,
        unscheduled_target_c=20.05,
        outer_max_c=22,
    )
    scheduled = [slot for slot in slots if slot["scheduled"]]
    assert scheduled[0]["start_utc"] == (start + dt.timedelta(minutes=minute)).isoformat()
    assert scheduled[-1]["end_utc"] == (start + dt.timedelta(hours=1, minutes=minute)).isoformat()
    for slot in slots:
        instant = dt.datetime.fromisoformat(slot["start_utc"])
        assert is_comfort_hour(schedule, instant, zone) is slot["scheduled"]
        assert slot["target_c"] == (20.55 if slot["scheduled"] else 20.05)
    assert is_comfort_hour(schedule, start + dt.timedelta(minutes=minute), zone)
    assert not is_comfort_hour(schedule, start + dt.timedelta(hours=1, minutes=minute), zone)


def test_lord_howe_half_hour_dst_fold_retains_exact_offset_boundary():
    start = dt.datetime(2026, 4, 4, 14, tzinfo=UTC)
    end = start + dt.timedelta(hours=3)
    slots = resolve_hourly_targets(
        {"weekday": [], "weekend": [1]},
        "Australia/Lord_Howe",
        start,
        end,
        scheduled_target_c=20.55,
        unscheduled_target_c=20.05,
        outer_max_c=22,
    )
    assert [(slot["start_utc"], slot["end_utc"]) for slot in slots if slot["scheduled"]] == [
        (start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()),
        (
            (start + dt.timedelta(hours=1)).isoformat(),
            (start + dt.timedelta(hours=1, minutes=30)).isoformat(),
        ),
    ]
    assert [slot["utc_offset_minutes"] for slot in slots[:2]] == [660, 630]
    assert [slot["fold"] for slot in slots[:2]] == [0, 1]


@pytest.mark.parametrize(
    "schedule",
    [
        {"weekday": [True], "weekend": []},
        {"weekday": [1, 1], "weekend": []},
        {"weekday": [24], "weekend": []},
        {"weekday": [], "weekend": [], "holiday": []},
    ],
)
def test_schedule_contract_rejects_noncanonical_or_ambiguous_masks(schedule):
    with pytest.raises(ValueError):
        normalize_schedule(schedule)


def test_schedule_contract_canonicalizes_unsorted_valid_hours():
    assert normalize_schedule({"weekday": [9, 7], "weekend": [10, 8]}) == {
        "weekday": [7, 9],
        "weekend": [8, 10],
    }


def test_stockholm_spring_transition_skips_nonexistent_local_hour():
    slots = resolve_hourly_targets(
        {"weekday": [], "weekend": [2]},
        "Europe/Stockholm",
        dt.datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        dt.datetime(2026, 3, 29, 3, 0, tzinfo=UTC),
        scheduled_target_c=20.5,
        unscheduled_target_c=18,
        outer_max_c=25,
    )
    assert len(slots) == 3
    assert [slot["local_start"][11:16] for slot in slots] == ["01:00", "03:00", "04:00"]
    assert not any(slot["scheduled"] for slot in slots)
    assert (
        len(
            resolve_comfort_intervals(
                {"weekday": [], "weekend": [2]},
                "Europe/Stockholm",
                dt.datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
                dt.datetime(2026, 3, 29, 22, 0, tzinfo=UTC),
            )
        )
        == 0
    )


def test_stockholm_fall_transition_resolves_both_fold_hours():
    slots = resolve_hourly_targets(
        {"weekday": [], "weekend": [2]},
        "Europe/Stockholm",
        dt.datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
        dt.datetime(2026, 10, 25, 23, 0, tzinfo=UTC),
        scheduled_target_c=20.5,
        unscheduled_target_c=18,
        outer_max_c=25,
    )
    repeated = [slot for slot in slots if slot["local_start"][11:13] == "02"]
    assert len(slots) == 25
    assert [slot["fold"] for slot in repeated] == [0, 1]
    assert [slot["utc_offset_minutes"] for slot in repeated] == [120, 60]
    assert all(slot["scheduled"] for slot in repeated)
    assert {slot["timezone_policy"] for slot in slots} == {TIMEZONE_POLICY}
    assert {slot["tzdata_version"] for slot in slots} == {timezone_data_version()}


def test_intervals_are_utc_half_open_and_clip_to_requested_bounds():
    schedule = {"weekday": [8], "weekend": []}
    start = dt.datetime(2026, 4, 29, 8, 30, tzinfo=UTC)
    end = dt.datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    intervals = resolve_comfort_intervals(schedule, "UTC", start, end)
    assert intervals == [(start, end)]
    assert (
        resolve_comfort_intervals(
            schedule,
            "UTC",
            end,
            dt.datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        )
        == []
    )


def test_policy_fingerprint_changes_with_schedule_targets_and_installation():
    base = {
        "installation_id": "installation-a",
        "timezone": "Europe/Stockholm",
        "comfort_schedule": {"weekday": [7], "weekend": [9]},
        "comfort_temp_min": 18.0,
        "comfort_temp_target": 20.5,
        "comfort_temp_max": 25.0,
        "timezone_policy": TIMEZONE_POLICY,
        "tzdata_version": timezone_data_version(),
    }
    assert policy_dependency_fingerprint(base) == policy_dependency_fingerprint(dict(base))
    changed = {**base, "comfort_temp_target": 21.0}
    assert policy_dependency_fingerprint(base) != policy_dependency_fingerprint(changed)
    other_installation = {**base, "installation_id": "installation-b"}
    assert policy_dependency_fingerprint(base) != policy_dependency_fingerprint(other_installation)
    assert policy_dependency_fingerprint({**base, "fixed_monthly_fee": "300"}) == (
        policy_dependency_fingerprint(base)
    )


def test_demand_dependency_changes_exclude_schedule_and_target_edits():
    base = {
        "timezone": "Europe/Stockholm",
        "tzdata_version": "2026a",
        "comfort_schedule": {"weekday": [7], "weekend": [9]},
        "comfort_temp_target": 20.5,
    }
    assert (
        demand_dependency_changes(
            base, {**base, "comfort_schedule": {"weekday": [], "weekend": []}}
        )
        == ()
    )
    assert demand_dependency_changes(base, {**base, "comfort_temp_target": 21.0}) == ()
    assert demand_dependency_changes(base, {**base, "timezone": "UTC"}) == ("timezone",)
    assert demand_dependency_changes(base, {**base, "tzdata_version": "2026b"}) == (
        "tzdata_version",
    )


@pytest.mark.parametrize(
    ("setting_key", "setting_value", "expected_generation"),
    [
        ("comfort_schedule", '{"weekday":[9],"weekend":[]}', 4),
        ("comfort_temp_target", "21.0", 4),
        ("timezone", "UTC", 5),
    ],
)
@pytest.mark.asyncio
async def test_contract_revision_advances_only_demand_dependency_generation(
    monkeypatch, setting_key, setting_value, expected_generation
):
    from packages.core import plan_lifecycle
    from packages.core.models import HouseContractRevisionRecord

    installation_id, house_id = uuid4(), uuid4()
    original = {
        "comfort_schedule": '{"weekday":[8],"weekend":[]}',
        "comfort_temp_min": "18",
        "comfort_temp_target": "20.5",
        "comfort_temp_max": "22",
        "timezone": "Europe/Stockholm",
    }
    previous_projection = _settings_projection(original, installation_id, house_id)
    previous = SimpleNamespace(
        id=7,
        contract_hash=_contract_hash(previous_projection),
        contract_json={**previous_projection, "field_sources": {}},
    )
    installation = SimpleNamespace(
        installation_id=installation_id,
        house_id=house_id,
        current_revision_id=7,
        demand_dependency_generation=4,
    )
    added = []

    async def flush():
        for record in added:
            if isinstance(record, HouseContractRevisionRecord) and record.id is None:
                record.id = 8

    session = SimpleNamespace(
        get=AsyncMock(return_value=previous),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        add=Mock(side_effect=added.append),
        flush=AsyncMock(side_effect=flush),
    )
    monkeypatch.setattr(plan_lifecycle, "supersede_active_plan", AsyncMock(return_value=None))

    updated = {**original, setting_key: setting_value}
    revision = await _insert_revision(
        session,
        installation,
        updated,
        source="test",
        changed_keys=[setting_key],
    )

    assert revision.id == 8
    assert installation.demand_dependency_generation == expected_generation


def test_house_planning_contract_is_typed_immutable_and_canonical():
    contract = HousePlanningContract.from_settings(
        {
            "comfort_schedule": '{"weekday":[9,7],"weekend":[10,8]}',
            "comfort_temp_min": "18",
            "comfort_temp_target": "20.5",
            "comfort_temp_max": "22",
            "timezone": "Europe/Stockholm",
        },
        uuid4(),
        uuid4(),
    )

    assert contract.weekday_hours == (7, 9)
    assert contract.weekend_hours == (8, 10)
    assert contract.timezone_policy == TIMEZONE_POLICY
    assert contract.tzdata_version == timezone_data_version()
    assert contract.to_projection()["comfort_schedule"] == {
        "weekday": [7, 9],
        "weekend": [8, 10],
    }
    with pytest.raises(FrozenInstanceError):
        contract.timezone = "UTC"


def test_P2_AC05_initial_contract_uses_existing_settings_schedule_and_targets(monkeypatch):
    from packages.core.settings_service import SETTING_SPECS, _default_setting_value
    from packages.core.config import settings as env_settings

    monkeypatch.setattr(env_settings, "comfort_temp_min", 18.0)
    monkeypatch.setattr(env_settings, "comfort_temp_max", 25.0)

    settings = {
        key: _default_setting_value(SETTING_SPECS[key])
        for key in (
            "comfort_schedule",
            "comfort_temp_min",
            "comfort_temp_target",
            "comfort_temp_max",
            "timezone",
        )
    }
    contract = HousePlanningContract.from_settings(settings, uuid4(), uuid4())

    assert contract.weekday_hours == (7, 8, 9, 17, 18, 19, 20, 21)
    assert contract.weekend_hours == tuple(range(8, 22))
    assert contract.comfort_temp_min == 18.0
    assert contract.comfort_temp_target == 20.5
    assert contract.comfort_temp_max == 25.0
    assert contract.timezone == settings["timezone"]


@pytest.mark.asyncio
async def test_plan_snapshot_carries_shared_projection_and_explicit_later_phase_defaults():
    installation_id = uuid4()
    house_id = uuid4()
    projection = _settings_projection(
        {
            "comfort_schedule": '{"weekday":[8],"weekend":[]}',
            "comfort_temp_min": "18",
            "comfort_temp_target": "20.5",
            "comfort_temp_max": "22",
            "timezone": "UTC",
        },
        installation_id,
        house_id,
    )
    revision = SimpleNamespace(
        id=7,
        contract_json={**projection, "field_sources": {"timezone": "environment"}},
        contract_hash=_contract_hash(projection),
        effective_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
        source="effective_settings_import",
    )
    session = AsyncMock()
    session.get.return_value = revision

    snapshot = await snapshot_from_revision(
        session,
        revision.id,
        dt.datetime(2026, 4, 29, 8, 0, tzinfo=UTC),
        dt.datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
    )

    plan = SimpleNamespace(
        contract_revision_id=revision.id,
        contract_hash=revision.contract_hash,
        horizon_start=dt.datetime(2026, 4, 29, 8, 0, tzinfo=UTC),
        horizon_end=dt.datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
    )
    assert validate_plan_contract_snapshot(plan, snapshot)
    tampered_target = {
        **snapshot,
        "schedule_projection": [dict(slot) for slot in snapshot["schedule_projection"]],
    }
    tampered_target["schedule_projection"][0]["target_c"] = 21.0
    assert not validate_plan_contract_snapshot(plan, tampered_target)
    tampered_schedule = {**snapshot, "comfort_schedule": {"weekday": [9], "weekend": []}}
    assert not validate_plan_contract_snapshot(plan, tampered_schedule)

    assert snapshot["revision_id"] == revision.id
    assert snapshot["contract_hash"] == revision.contract_hash
    assert snapshot["timezone_policy"] == TIMEZONE_POLICY
    assert snapshot["tzdata_version"] == timezone_data_version()
    assert [slot["target_c"] for slot in snapshot["schedule_projection"]] == [20.5, 18.0]
    assert all(
        slot["contract_revision_id"] == revision.id for slot in snapshot["schedule_projection"]
    )
    assert snapshot["p2_defaults"] == {
        "protected_rooms": [],
        "dhw_schedule_mode": "inherit_comfort",
        "dhw_custom_schedule": None,
        "tariff_settings": None,
        "curve_attestation": "unknown_not_recorded_by_p2_1",
    }
    canonical = _canonical_contract_target_schedule(snapshot)
    assert canonical[0]["ts"] == snapshot["schedule_projection"][0]["end_utc"]
    assert canonical[0]["contract_hash"] == revision.contract_hash
    assert _target_schedule_matches_contract(
        {
            "target_schedule": [{"ts": canonical[0]["ts"], "target": 20.5}]
            + [{"ts": canonical[1]["ts"], "target": 18.0}]
        },
        canonical,
    )
    assert not _target_schedule_matches_contract(
        {
            "target_schedule": [{"ts": canonical[0]["ts"], "target": 19.0}]
            + [{"ts": canonical[1]["ts"], "target": 18.0}]
        },
        canonical,
    )


@pytest.mark.asyncio
async def test_historical_snapshot_refuses_reinterpretation_with_other_timezone_data(monkeypatch):
    from packages.core import house_contract

    projection = {
        "schema_version": 2,
        "installation_id": str(uuid4()),
        "house_id": str(uuid4()),
        "timezone": "UTC",
        "comfort_schedule": {"weekday": [], "weekend": []},
        "comfort_temp_min": 18.0,
        "comfort_temp_target": 20.5,
        "comfort_temp_max": 22.0,
        "timezone_policy": TIMEZONE_POLICY,
        "tzdata_version": "tzdata:old",
    }
    revision = SimpleNamespace(id=9, contract_json=projection)
    session = AsyncMock()
    session.get.return_value = revision
    monkeypatch.setattr(house_contract, "timezone_data_version", lambda: "tzdata:new")

    with pytest.raises(ValueError, match="persisted plan snapshot"):
        await snapshot_from_revision(
            session,
            revision.id,
            dt.datetime(2026, 1, 1, tzinfo=UTC),
            dt.datetime(2026, 1, 1, 1, tzinfo=UTC),
        )

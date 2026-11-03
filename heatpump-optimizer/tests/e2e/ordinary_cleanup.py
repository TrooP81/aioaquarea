"""Ordinary-test baseline restoration, never immutable-history restoration.

Callers own one guarded outer transaction. These helpers never commit, acquire
another connection, or change the authority/identity of a historical revision.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import house_contract, settings_service
from packages.core.heat_curve import HeatCurveConfig
from packages.core.models import (
    HouseContractRevisionRecord,
    HouseInstallationRecord,
    SettingRecord,
)
from packages.core.space_heating_gate import HeatingGateConfig

HOUSE_TABLES = frozenset({"house_installations", "house_contract_revisions"})
PRESERVED_TABLES = HOUSE_TABLES | {"settings"}
DEPENDENCIES = ("weather_provider", "smartthings_device_ids", "comfort_reference_sensor_id")
SOURCE = "ordinary_fixture_reset"


@dataclass
class OrdinaryBaseline:
    rows: tuple
    effective: dict
    boundary: dict
    poisoned: bool = False

    def require_usable(self):
        assert not self.poisoned, "ordinary cleanup failed; fixture profile is unusable"


async def _rows(session, table, order):
    return tuple(
        deepcopy(dict(row))
        for row in (await session.execute(select(table).order_by(order))).mappings().all()
    )


async def _settings_rows(session):
    return await _rows(session, SettingRecord.__table__, SettingRecord.key)


async def _house_boundary(session):
    installation = (
        (await session.execute(select(HouseInstallationRecord.__table__))).mappings().one()
    )
    history = await _rows(
        session, HouseContractRevisionRecord.__table__, HouseContractRevisionRecord.id
    )
    # Include triggers, indexes, defaults, sequence ownership and configuration.
    catalog = tuple(
        (
            await session.execute(
                text(
                    "SELECT 'column',c.relname,a.attname,concat_ws('|',a.attnum,"
                    "format_type(a.atttypid,a.atttypmod),a.attnotnull,"
                    "pg_get_expr(d.adbin,d.adrelid)) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid=c.oid "
                    "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
                    "WHERE n.nspname='public' AND c.relname=ANY(:names) "
                    "AND a.attnum>0 AND NOT a.attisdropped "
                    "UNION ALL SELECT 'constraint',c.relname,k.conname,"
                    "pg_get_constraintdef(k.oid) FROM pg_constraint k "
                    "JOIN pg_class c ON c.oid=k.conrelid "
                    "WHERE c.oid IN (SELECT unnest(CAST(:names AS text[]))::regclass) "
                    "UNION ALL SELECT 'trigger',c.relname,t.tgname,"
                    "concat_ws('|',t.tgenabled,pg_get_triggerdef(t.oid),pg_get_functiondef(t.tgfoid)) FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid=t.tgrelid "
                    "WHERE c.oid IN (SELECT unnest(CAST(:names AS text[]))::regclass) "
                    "UNION ALL SELECT 'index',tablename,indexname,indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND tablename=ANY(:names) "
                    "UNION ALL SELECT 'sequence',c.relname,s.relname,"
                    "concat_ws('|',a.attname,d.deptype,q.seqtypid,q.seqstart,q.seqincrement,"
                    "q.seqmax,q.seqmin,q.seqcache,q.seqcycle) FROM pg_depend d "
                    "JOIN pg_class s ON s.oid=d.objid AND s.relkind='S' "
                    "JOIN pg_sequence q ON q.seqrelid=s.oid "
                    "JOIN pg_class c ON c.oid=d.refobjid "
                    "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=d.refobjsubid "
                    "WHERE d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass "
                    "AND c.oid IN (SELECT unnest(CAST(:names AS text[]))::regclass) "
                    "ORDER BY 1,2,3,4"
                ),
                {"names": sorted(PRESERVED_TABLES)},
            )
        ).all()
    )
    sequence = (
        await session.execute(text("SELECT last_value FROM public.house_contract_revisions_id_seq"))
    ).scalar_one()
    return {
        "installation": deepcopy(dict(installation)),
        "history": history,
        "catalog": catalog,
        "sequence": sequence,
    }


def _validate_effective(effective, installation):
    house_contract.HousePlanningContract.from_settings(
        effective,
        installation.installation_id if installation else UUID(int=0),
        installation.house_id if installation else UUID(int=0),
    )
    HeatCurveConfig.from_settings(effective)
    HeatingGateConfig(
        profile_id=effective["space_heating_behavior_profile"],
        base_c=float(effective["heat_curve_heating_off_outdoor_c"]),
        on_offset_c=float(effective["space_heating_gate_on_offset_c"]),
        off_offset_c=float(effective["space_heating_gate_off_offset_c"]),
    )


async def assert_ordinary_contract_consistent(session, effective):
    installation = await session.get(HouseInstallationRecord, 1)
    assert installation is not None and installation.current_revision_id is not None, (
        "existing installation must have a valid current pointer"
    )
    _validate_effective(effective, installation)
    revision = await house_contract.ensure_current_contract(
        session, effective, lock=False, verify_current_settings=True
    )
    assert revision.installation_id == installation.installation_id
    projection = house_contract._settings_projection(
        effective, installation.installation_id, installation.house_id
    )
    assert revision.schema_version == house_contract.SCHEMA_VERSION
    assert {key: revision.contract_json.get(key) for key in projection} == projection
    return installation, revision


async def capture_ordinary_baseline(session):
    await house_contract._lock_contract(session)
    rows = await _settings_rows(session)
    effective = await settings_service.get_all_settings(session=session)
    installation = await session.get(HouseInstallationRecord, 1)
    _validate_effective(effective, installation)
    if installation is None:
        assert not (
            await session.execute(select(HouseContractRevisionRecord.id).limit(1))
        ).first(), "history without installation"
        await house_contract.ensure_current_contract(session, effective, lock=False)
    await assert_ordinary_contract_consistent(session, effective)
    assert await _settings_rows(session) == rows, (
        "initialization must not persist fallback settings"
    )
    return OrdinaryBaseline(rows, effective, await _house_boundary(session))


def _assert_conservation(before, after):
    for key in ("singleton_id", "installation_id", "house_id"):
        assert after["installation"][key] == before["installation"][key], key
    for key in ("demand_dependency_generation", "indoor_dependency_generation"):
        assert after["installation"][key] >= before["installation"][key], key
    retained = {row["id"]: row for row in after["history"]}
    assert all(retained.get(row["id"]) == row for row in before["history"]), (
        "immutable revisions must remain unchanged"
    )
    assert after["catalog"] == before["catalog"], "retained house/settings schema changed"
    assert after["sequence"] >= before["sequence"], "house sequence must never rewind"


async def restore_ordinary_baseline(session, baseline, truncate):
    """Return a tentative boundary; caller publishes it only after outer commit."""
    baseline.require_usable()
    await house_contract._lock_contract(session)
    pre_effective = await settings_service.get_all_settings(session=session)
    installation, previous = await assert_ordinary_contract_consistent(session, pre_effective)
    before = await _house_boundary(session)
    _assert_conservation(baseline.boundary, before)
    pre_current_id = previous.id
    previous_hash = previous.contract_hash
    previous_projection = deepcopy(previous.contract_json)
    demand_before = installation.demand_dependency_generation
    indoor_before = installation.indoor_dependency_generation
    weather = pre_effective["weather_provider"] != baseline.effective["weather_provider"]
    sensors = any(pre_effective[key] != baseline.effective[key] for key in DEPENDENCIES[1:])
    # This service MUST see the actual old rows, including dependency fallbacks.
    result = await settings_service.set_settings_bulk(
        {key: baseline.effective[key] for key in DEPENDENCIES},
        session=session,
        expected_revision_id=pre_current_id,
        source=SOURCE,
    )
    assert result is None, "dependency-only write must not revise the contract"
    await session.flush()
    await session.execute(delete(SettingRecord.__table__))
    if baseline.rows:
        await session.execute(insert(SettingRecord.__table__), list(baseline.rows))
    session.expire_all()
    restored_effective = await settings_service.get_all_settings(session=session)
    assert restored_effective == baseline.effective
    revision = await house_contract.revise_contract(
        session,
        restored_effective,
        source=SOURCE,
        changed_keys=sorted(
            key
            for key in house_contract.CONTRACT_SETTING_KEYS
            if pre_effective[key] != restored_effective[key]
        ),
        expected_revision_id=pre_current_id,
    )
    authoritative_id = revision.id
    timezone = bool(
        house_contract.demand_dependency_changes(previous_projection, revision.contract_json)
    )
    await session.flush()
    await truncate(await session.connection())
    session.expire_all()
    current_installation, current = await assert_ordinary_contract_consistent(
        session, await settings_service.get_all_settings(session=session)
    )
    assert current.id == current_installation.current_revision_id == authoritative_id
    assert current_installation.demand_dependency_generation == demand_before + int(weather) + int(
        timezone
    )
    assert current_installation.indoor_dependency_generation == indoor_before + int(
        weather or sensors
    )
    assert await _settings_rows(session) == baseline.rows
    after = await _house_boundary(session)
    _assert_conservation(before, after)
    expected_append = int(previous_hash != current.contract_hash)
    assert len(after["history"]) == len(before["history"]) + expected_append
    return after


async def reset_ordinary_profile(engine, baseline, *, guard, truncate):
    """A failed/rolled-back cleanup permanently poisons this fixture instance."""
    baseline.require_usable()
    try:
        async with engine.begin() as connection:
            await guard(connection)
            async with AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only"
            ) as session:
                boundary = await restore_ordinary_baseline(session, baseline, truncate)
        baseline.boundary = boundary
    except BaseException:
        baseline.poisoned = True
        raise

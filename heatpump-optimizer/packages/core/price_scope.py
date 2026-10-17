"""Installation-qualified Tibber scopes and transactional binding provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from packages.core.database import get_session
from packages.core.models import PriceScopeRecord, SettingRecord

UNAVAILABLE_AREA = "tb:unavailable"
TARIFF_SCHEMA_VERSION = "tibber_provider_total_v1"
AUTO_BINDING_SUSPENDED = "auto_binding_requires_explicit_selection"


def tibber_scope(home_id: str) -> str:
    """Hash UTF-8 compact JSON [\"tibber\", exact provider ID], without normalization."""
    if not isinstance(home_id, str) or not home_id or home_id != home_id.strip():
        raise ValueError("invalid_tibber_home_id")
    identity = json.dumps(["tibber", home_id], ensure_ascii=False, separators=(",", ":"))
    return "tb:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def price_area_from_settings(values: Mapping[str, str]) -> str:
    provider = values["price_provider"]
    if provider != "tibber":
        return values["entsoe_area"] if provider == "entsoe" else provider
    home_id = values.get("tibber_home_id", "")
    try:
        diagnostic = json.loads(values.get("tibber_feed_diagnostic", "") or "{}")
    except (ValueError, TypeError):
        return UNAVAILABLE_AREA
    if not isinstance(diagnostic, dict):
        return UNAVAILABLE_AREA
    if diagnostic.get("home_id") == home_id and diagnostic.get("reason"):
        return UNAVAILABLE_AREA
    try:
        return tibber_scope(home_id)
    except ValueError:
        return UNAVAILABLE_AREA


async def resolve_price_area(values: Mapping[str, str], *, session=None) -> str:
    """A digest alone is not identity proof: readers must verify the registry too."""
    from contextlib import nullcontext

    area = price_area_from_settings(values)
    if values["price_provider"] != "tibber" or area == UNAVAILABLE_AREA:
        return area
    async with get_session() if session is None else nullcontext(session) as current:
        record = (
            await current.execute(select(PriceScopeRecord).where(PriceScopeRecord.area == area))
        ).scalar_one_or_none()
    if record is None or record.provider != "tibber" or record.home_id != values["tibber_home_id"]:
        return UNAVAILABLE_AREA
    return area


def tariff_metadata(currency: str) -> dict:
    return {
        "currency": currency,
        "amount_basis": "provider_total",
        "interval_schema": "hourly_duration_weighted_v1",
        "tariff_schema_version": TARIFF_SCHEMA_VERSION,
        "application_extra_fee": 0.0,
    }


async def ensure_tibber_scope(session, *, area: str, home_id: str, currency: str) -> None:
    """Insert-or-verify in the price-write transaction; NEVER update conflicts.

    PostgreSQL waits for a conflicting uncommitted insert. The subsequent read
    verifies both unique keys; a mismatch aborts the caller's entire transaction.
    """
    if area != tibber_scope(home_id):
        raise ValueError("tibber_scope_identity_mismatch")
    expected = dict(area=area, provider="tibber", home_id=home_id, **tariff_metadata(currency))
    await session.execute(insert(PriceScopeRecord).values(**expected).on_conflict_do_nothing())
    record = (
        await session.execute(select(PriceScopeRecord).where(PriceScopeRecord.area == area))
    ).scalar_one_or_none()
    if record is None or any(getattr(record, key) != value for key, value in expected.items()):
        raise ValueError("tibber_scope_collision_or_metadata_conflict")


async def record_explicit_tibber_binding(session, home_id: str) -> None:
    """Called after the owner setting write, in the same transaction/row lock."""
    diagnostic = json.dumps(
        {
            "home_id": home_id,
            "reason": None,
            "binding_method": "explicit",
        },
        sort_keys=True,
    )
    stmt = insert(SettingRecord).values(key="tibber_feed_diagnostic", value=diagnostic)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": diagnostic},
        )
    )


async def record_tibber_binding(
    captured_home: str, selected_home: str, reason: str | None, *, auto_bind: bool = False
) -> bool:
    """CAS an empty binding; lock before recording a home-qualified diagnostic.

    A concurrent settings edit is never overwritten. A stale poll can still
    persist its captured prices, but cannot change the new home's diagnostic.
    """
    async with get_session() as session:
        if auto_bind:
            stmt = insert(SettingRecord).values(key="tibber_home_id", value=selected_home)
            result = await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": selected_home},
                    where=SettingRecord.value == "",
                ).returning(SettingRecord.value)
            )
            if result.scalar_one_or_none() != selected_home:
                return False
        else:
            await session.execute(
                insert(SettingRecord)
                .values(key="tibber_home_id", value=captured_home)
                .on_conflict_do_nothing()
            )
            current = (
                await session.execute(
                    select(SettingRecord.value)
                    .where(SettingRecord.key == "tibber_home_id")
                    .with_for_update()
                )
            ).scalar_one()
            if current != captured_home:
                return False
        previous = (
            await session.execute(
                select(SettingRecord.value).where(SettingRecord.key == "tibber_feed_diagnostic")
            )
        ).scalar_one_or_none()
        try:
            previous = json.loads(previous or "{}")
        except (ValueError, TypeError):
            previous = {}
        method = "single_active_subscription" if auto_bind else "explicit"
        if isinstance(previous, dict) and previous.get("home_id") == selected_home:
            method = previous.get("binding_method", method)
            # Only an owner setting write can release this latch. An older poll
            # cannot clear it by succeeding, or overwrite a new explicit decision.
            if method == "explicit" and reason == AUTO_BINDING_SUSPENDED:
                return False
            if (
                method == "single_active_subscription"
                and previous.get("reason") == AUTO_BINDING_SUSPENDED
            ):
                reason = AUTO_BINDING_SUSPENDED
        diagnostic = json.dumps(
            {
                "home_id": selected_home,
                "reason": reason,
                "binding_method": method,
            },
            sort_keys=True,
        )
        stmt = insert(SettingRecord).values(key="tibber_feed_diagnostic", value=diagnostic)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["key"], set_={"value": diagnostic})
        )
    return True

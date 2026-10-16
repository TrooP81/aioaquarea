"""Repair regressions: automatic ambiguity is latched until explicit selection."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from packages.core import price_scope, settings_service
from packages.poller import feeds


def home(identity):
    return {
        "id": identity,
        "currentSubscription": {
            "priceInfo": {
                "today": [{"currency": "SEK", "total": -0.2, "startsAt": "2026-10-16T00:00:00Z"}],
                "tomorrow": [],
            }
        },
    }


@pytest.fixture
def binding_store(monkeypatch):
    """Exercise real persistence helpers against a statement-aware transaction fake."""
    values = {"price_provider": "tibber", "tibber_api_token": "offline-token", "tibber_home_id": ""}

    async def execute(statement, parameters=None):
        params = statement.compile(dialect=postgresql.dialect()).params
        if getattr(statement, "is_insert", False):
            key, value = params["key"], params["value"]
            values[key] = value
            return SimpleNamespace(scalar_one_or_none=lambda: value)
        if "key_1" in params:
            value = values.get(params["key_1"])
            return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)
        return SimpleNamespace(scalars=lambda: [])

    session = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(price_scope, "get_session", MagicMock(return_value=context))
    monkeypatch.setattr(settings_service, "get_session", MagicMock(return_value=context))
    monkeypatch.setattr(feeds, "get_all_settings", AsyncMock(side_effect=lambda: dict(values)))
    response = MagicMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(feeds.httpx, "AsyncClient", MagicMock(return_value=client))
    return values, session, response, client


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_auto_ambiguity_stays_suspended_until_explicit_same_id_write(
    binding_store, bulk, reverse
):
    values, _, response, client = binding_store
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    assert await feeds._fetch_prices_tibber()
    assert (
        json.loads(values["tibber_feed_diagnostic"])["binding_method"]
        == "single_active_subscription"
    )
    homes = [home("A"), home("B")]
    response.json.return_value = {"data": {"viewer": {"homes": homes[::-1] if reverse else homes}}}
    rejected = await feeds._fetch_prices_tibber()
    assert rejected == []
    assert rejected.rejection_reason == price_scope.AUTO_BINDING_SUSPENDED
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA
    assert values["tibber_home_id"] == "A"

    # Even if the extra home disappears, automatic planning remains suspended.
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    calls = client.post.await_count
    assert await feeds._fetch_prices_tibber() == []
    assert client.post.await_count == calls
    assert (
        json.loads(values["tibber_feed_diagnostic"])["reason"] == price_scope.AUTO_BINDING_SUSPENDED
    )

    if bulk:
        await settings_service.set_settings_bulk({"tibber_home_id": "A"})
    else:
        await settings_service.set_setting("tibber_home_id", "A")
    assert json.loads(values["tibber_feed_diagnostic"]) == {
        "home_id": "A",
        "binding_method": "explicit",
        "reason": None,
    }
    response.json.return_value = {"data": {"viewer": {"homes": homes}}}
    accepted = await feeds._fetch_prices_tibber()
    assert [value for _, value in accepted] == [-0.2]
    assert accepted.context.area == price_scope.tibber_scope("A")
    assert json.loads(values["tibber_feed_diagnostic"])["binding_method"] == "explicit"


@pytest.mark.parametrize("reason", [None, "selected_home_has_no_prices"])
async def test_stale_poll_cannot_clear_auto_binding_suspension(binding_store, reason):
    values, _, _, _ = binding_store
    values["tibber_home_id"] = "A"
    values["tibber_feed_diagnostic"] = json.dumps(
        {
            "home_id": "A",
            "binding_method": "single_active_subscription",
            "reason": price_scope.AUTO_BINDING_SUSPENDED,
        }
    )
    await price_scope.record_tibber_binding("A", "A", reason)
    assert (
        json.loads(values["tibber_feed_diagnostic"])["reason"] == price_scope.AUTO_BINDING_SUSPENDED
    )
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA


async def test_stale_automatic_poll_cannot_suspend_new_explicit_same_home(binding_store):
    values, _, _, _ = binding_store
    await settings_service.set_setting("tibber_home_id", "A")
    assert not await price_scope.record_tibber_binding("A", "A", price_scope.AUTO_BINDING_SUSPENDED)
    assert json.loads(values["tibber_feed_diagnostic"]) == {
        "home_id": "A",
        "binding_method": "explicit",
        "reason": None,
    }


@pytest.mark.parametrize("bulk", [False, True])
async def test_owner_cannot_clear_provenance_without_explicit_home_write(binding_store, bulk):
    values, _, _, _ = binding_store
    values["tibber_home_id"] = "A"
    values["tibber_feed_diagnostic"] = json.dumps(
        {
            "home_id": "A",
            "binding_method": "single_active_subscription",
            "reason": price_scope.AUTO_BINDING_SUSPENDED,
        }
    )
    before = dict(values)
    with pytest.raises(ValueError, match="read-only"):
        if bulk:
            await settings_service.set_settings_bulk({"tibber_feed_diagnostic": ""})
        else:
            await settings_service.set_setting("tibber_feed_diagnostic", "")
    assert values == before

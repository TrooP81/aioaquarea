"""Independent delta acceptance for persistent automatic-binding suspension."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from packages.core import price_scope, settings_service
from packages.poller import feeds
from tests import test_tibber_binding_repair as support

binding_store = support.binding_store
home = support.home


@pytest.mark.parametrize("bulk", [False, True])
async def test_explicit_binding_disappears_and_returns_without_fallback(binding_store, bulk):
    values, _, response, _ = binding_store
    if bulk:
        await settings_service.set_settings_bulk({"tibber_home_id": "A"})
    else:
        await settings_service.set_setting("tibber_home_id", "A")
    response.json.return_value = {"data": {"viewer": {"homes": [home("B"), home("C")]}}}
    missing = await feeds._fetch_prices_tibber()
    assert missing == []
    assert missing.rejection_reason == "selected_home_not_found"
    assert values["tibber_home_id"] == "A"
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA
    assert json.loads(values["tibber_feed_diagnostic"])["binding_method"] == "explicit"
    response.json.return_value = {"data": {"viewer": {"homes": [home("C"), home("A"), home("B")]}}}
    returned = await feeds._fetch_prices_tibber()
    assert [amount for _, amount in returned] == [-0.2]
    assert returned.context.area == price_scope.tibber_scope("A")
    assert json.loads(values["tibber_feed_diagnostic"])["reason"] is None


async def test_automatic_binding_latch_survives_fresh_python_process(binding_store):
    values, _, response, _ = binding_store
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    assert await feeds._fetch_prices_tibber()
    response.json.return_value = {"data": {"viewer": {"homes": [home("B"), home("A")]}}}
    rejected = await feeds._fetch_prices_tibber()
    assert rejected == []
    assert rejected.rejection_reason == price_scope.AUTO_BINDING_SUSPENDED
    # Only settings values cross this process boundary; no poller module state does.
    script = r"""
import asyncio, json, sys
from unittest.mock import AsyncMock, MagicMock
from packages.core import price_scope
from packages.poller import feeds
values = json.loads(sys.stdin.read())
feeds.get_all_settings = AsyncMock(return_value=values)
feeds.record_tibber_binding = AsyncMock(return_value=True)
feeds.httpx.AsyncClient = MagicMock(side_effect=AssertionError('latched restart performed network I/O'))
async def run():
    feed = await feeds._fetch_prices_tibber()
    assert feed == []
    assert feed.rejection_reason == price_scope.AUTO_BINDING_SUSPENDED
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA
    feeds.httpx.AsyncClient.assert_not_called()
    print('persisted-settings latch restored in fresh Python process')
asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "latch restored" in result.stdout


async def test_second_home_removal_and_unrelated_write_cannot_unlatch(binding_store):
    values, _, response, client = binding_store
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    assert await feeds._fetch_prices_tibber()
    response.json.return_value = {"data": {"viewer": {"homes": [home("A"), home("B")]}}}
    assert await feeds._fetch_prices_tibber() == []
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    calls = client.post.await_count
    await settings_service.set_setting("tibber_api_token", "replacement-offline-token")
    assert await feeds._fetch_prices_tibber() == []
    assert client.post.await_count == calls
    assert (
        json.loads(values["tibber_feed_diagnostic"])["reason"] == price_scope.AUTO_BINDING_SUSPENDED
    )
    await settings_service.set_setting("tibber_home_id", "A")
    assert await feeds._fetch_prices_tibber()
    assert client.post.await_count == calls + 1


async def test_automatic_missing_selected_home_never_binds_replacement(binding_store):
    values, _, response, _ = binding_store
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    assert await feeds._fetch_prices_tibber()
    response.json.return_value = {"data": {"viewer": {"homes": [home("B")]}}}
    missing = await feeds._fetch_prices_tibber()
    assert missing == []
    assert missing.rejection_reason == "selected_home_not_found"
    assert values["tibber_home_id"] == "A"
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA

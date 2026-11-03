"""Independent AC2 cache identity, waiting-client and cancellation regressions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.api.routers import dashboard


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(dashboard, "_comfort_assessment_cache", None)
    monkeypatch.setattr(dashboard, "_comfort_assessment_lock", asyncio.Lock())


@pytest.mark.parametrize("changed", ["revision", "contract_hash", "demand", "indoor", "settings"])
async def test_AC2_real_identity_invalidates_cached_missing_warning(monkeypatch, changed):
    revision = SimpleNamespace(id=7, contract_hash="contract-a")
    installation = SimpleNamespace(demand_dependency_generation=1, indoor_dependency_generation=1)
    settings = {"comfort_temp_target": "21"}
    session = SimpleNamespace(get=AsyncMock(return_value=installation))

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(dashboard, "get_session", sessions)
    monkeypatch.setattr(
        "packages.core.house_contract.ensure_current_contract", AsyncMock(return_value=revision)
    )
    monkeypatch.setattr(
        "packages.core.settings_service.get_all_settings",
        AsyncMock(side_effect=lambda **_: dict(settings)),
    )
    warning = {"state": "at_risk", "reason": "current inputs"}
    forecast = AsyncMock(
        side_effect=[{"comfort_assessment": None}, {"comfort_assessment": warning}]
    )
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", forecast)
    assert await dashboard._get_comfort_assessment() is None
    before = dashboard._comfort_assessment_cache[1]
    if changed == "revision":
        revision.id += 1
    elif changed == "contract_hash":
        revision.contract_hash = "contract-b"
    elif changed == "demand":
        installation.demand_dependency_generation += 1
    elif changed == "indoor":
        installation.indoor_dependency_generation += 1
    else:
        settings["comfort_temp_target"] = "23"
    assert await dashboard._get_comfort_assessment() == warning
    assert dashboard._comfort_assessment_cache[1] != before
    assert await dashboard._get_comfort_assessment() == warning
    assert forecast.await_count == 2


async def test_AC2_waiting_client_cannot_reuse_prechange_missing_warning(monkeypatch):
    context = [7, 1, 1]
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    warning = {"state": "at_risk"}

    async def forecast(*, hours):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return {"comfort_assessment": None}
        return {"comfort_assessment": warning}

    async def second_client():
        waiting.set()
        return await dashboard._get_comfort_assessment()

    monkeypatch.setattr(
        dashboard, "_comfort_assessment_context", AsyncMock(side_effect=lambda: tuple(context))
    )
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", forecast)
    first = asyncio.create_task(dashboard._get_comfort_assessment())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(second_client())
        await asyncio.wait_for(waiting.wait(), 1)
        context[2] += 1
        release.set()
        assert await asyncio.wait_for(first, 1) == warning
        assert await asyncio.wait_for(second, 1) == warning
        assert calls == 2
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None), return_exceptions=True
        )


async def test_AC2_cancelled_refresh_releases_lock_without_caching(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*, hours):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(dashboard, "_comfort_assessment_context", AsyncMock(return_value=(7, 1, 1)))
    monkeypatch.setattr("packages.api.routers.models_router.get_indoor_forecast", blocked)
    task = asyncio.create_task(dashboard._get_comfort_assessment())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert not dashboard._comfort_assessment_lock.locked()
        assert dashboard._comfort_assessment_cache is None
        warning = {"state": "at_risk"}
        monkeypatch.setattr(
            "packages.api.routers.models_router.get_indoor_forecast",
            AsyncMock(return_value={"comfort_assessment": warning}),
        )
        assert await asyncio.wait_for(dashboard._get_comfort_assessment(), 1) == warning
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

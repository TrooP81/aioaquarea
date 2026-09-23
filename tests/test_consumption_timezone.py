"""Tests for the timezone offset sent to the consumption API."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from aioaquarea.consumption_manager import AquareaConsumptionManager
from aioaquarea.errors import ApiError, AuthenticationError, AuthenticationErrorCodes
from aioaquarea.statistics import DateType


class _Response:
    async def json(self):
        return {"historyDataList": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (dt.timedelta(hours=5, minutes=30), "+05:30"),
        (dt.timedelta(hours=-3, minutes=-30), "-03:30"),
        (dt.timedelta(minutes=-30), "-00:30"),
    ],
)
async def test_consumption_payload_formats_signed_offset(offset, expected) -> None:
    api_client = AsyncMock()
    api_client.request.return_value = _Response()
    manager = AquareaConsumptionManager(
        api_client,
        "https://example.test/",
        dt.timezone(offset),
    )

    await manager.get_device_consumption("device", DateType.DAY, "2026-08-13")

    payload = api_client.request.await_args.kwargs["json"]
    assert payload["bodyParam"]["osTimezone"] == expected


@pytest.mark.asyncio
async def test_consumption_manager_propagates_authentication_error() -> None:
    api_client = AsyncMock()
    api_client.request.side_effect = AuthenticationError(
        AuthenticationErrorCodes.TOKEN_EXPIRED, "Token expired"
    )
    manager = AquareaConsumptionManager(
        api_client, "https://example.test/", dt.timezone.utc
    )

    with pytest.raises(AuthenticationError):
        await manager.get_device_consumption("device", DateType.DAY, "2026-09-23")


@pytest.mark.asyncio
async def test_consumption_manager_returns_none_for_api_error() -> None:
    api_client = AsyncMock()
    api_client.request.side_effect = ApiError("5000-0001", "Unavailable")
    manager = AquareaConsumptionManager(
        api_client, "https://example.test/", dt.timezone.utc
    )

    result = await manager.get_device_consumption("device", DateType.DAY, "2026-09-23")

    assert result is None

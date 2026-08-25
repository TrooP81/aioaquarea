import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aioaquarea.weekly_timer import DayOfWeek
from aioaquarea.weekly_timer_manager import WeeklyTimerManager


@pytest.mark.asyncio
async def test_weekly_timer_read_uses_transfer_api_without_write_method() -> None:
    response = SimpleNamespace(
        content_type="application/json",
        json=AsyncMock(return_value={"enabled": False, "schedule": []}),
    )
    api_client = SimpleNamespace(request=AsyncMock(return_value=response))
    manager = WeeklyTimerManager(api_client)

    settings = await manager.get_weekly_timer("device-1")

    assert settings is not None
    assert settings.enabled is False
    api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/weeklytimer",
            "requestMethod": "GET",
            "bodyParam": {"gwid": "device-1"},
        },
        throw_on_error=True,
    )
    assert not hasattr(manager, "set_weekly_timer")


def test_weekly_timer_parser_skips_invalid_slots() -> None:
    settings = WeeklyTimerManager.parse(
        {
            "enabled": True,
            "schedule": [
                {
                    "dayOfWeek": 1,
                    "zoneId": 1,
                    "startTime": "22:00",
                    "endTime": "06:00",
                    "heatSet": "32.5",
                },
                {"dayOfWeek": 9, "startTime": "bad", "endTime": "06:00"},
            ],
        }
    )

    assert settings is not None
    assert settings.enabled is True
    assert len(settings.slots) == 1
    assert settings.slots[0].heat_set == 32.5


def test_weekly_timer_evaluates_midnight_span_in_configured_timezone() -> None:
    settings = WeeklyTimerManager.parse(
        {
            "enabled": True,
            "schedule": [
                {
                    "dayOfWeek": DayOfWeek.MONDAY,
                    "startTime": "22:00",
                    "endTime": "06:00",
                }
            ],
        }
    )
    assert settings is not None

    monday = dt.datetime(2026, 8, 24, 21, 30, tzinfo=dt.timezone.utc)
    tuesday = dt.datetime(2026, 8, 25, 3, 30, tzinfo=dt.timezone.utc)

    assert settings.active_slots(monday, "Europe/Stockholm")
    assert settings.active_slots(tuesday, "Europe/Stockholm")


def test_weekly_timer_requires_aware_evaluation_time() -> None:
    settings = WeeklyTimerManager.parse({"enabled": True, "schedule": []})
    assert settings is not None

    with pytest.raises(ValueError, match="aware datetime"):
        settings.active_slots(dt.datetime(2026, 8, 24, 22), "Europe/Stockholm")

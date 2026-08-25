from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from .weekly_timer import DayOfWeek, WeeklyTimerSettings, WeeklyTimerSlot

if TYPE_CHECKING:
    from .api_client import AquareaAPIClient

_LOGGER = logging.getLogger(__name__)


class WeeklyTimerManager:
    """Read Panasonic's weekly timer without exposing write operations."""

    def __init__(self, api_client: AquareaAPIClient):
        self._api_client = api_client

    async def get_weekly_timer(self, device_id: str) -> WeeklyTimerSettings | None:
        response = await self._api_client.request(
            "POST",
            "remote/v1/app/common/transfer",
            json={
                "apiName": "/remote/v1/api/weeklytimer",
                "requestMethod": "GET",
                "bodyParam": {"gwid": device_id},
            },
            throw_on_error=True,
        )
        if response.content_type != "application/json":
            _LOGGER.warning("Panasonic weekly timer returned a non-JSON response")
            return None
        payload = await response.json()
        return self.parse(payload)

    @classmethod
    def parse(cls, payload: object) -> WeeklyTimerSettings | None:
        data = payload
        for key in ("data", "result", "body"):
            if isinstance(data, dict) and isinstance(data.get(key), dict):
                data = data[key]
        if not isinstance(data, dict) or not isinstance(data.get("schedule"), list):
            return None

        slots: list[WeeklyTimerSlot] = []
        for entry in data["schedule"]:
            slot = cls._parse_slot(entry)
            if slot is not None:
                slots.append(slot)
        return WeeklyTimerSettings(enabled=data.get("enabled") is True, slots=tuple(slots))

    @staticmethod
    def _parse_slot(entry: object) -> WeeklyTimerSlot | None:
        if not isinstance(entry, dict):
            return None
        try:
            day = DayOfWeek(int(entry["dayOfWeek"]))
            zone_id = int(entry.get("zoneId", 1))
            start = dt.time.fromisoformat(str(entry["startTime"]))
            end = dt.time.fromisoformat(str(entry["endTime"]))
        except (KeyError, TypeError, ValueError):
            return None
        if zone_id < 1:
            return None

        def optional_float(value: object) -> float | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return WeeklyTimerSlot(
            day=day,
            zone_id=zone_id,
            start=start,
            end=end,
            heat_set=optional_float(entry.get("heatSet")),
            cool_set=optional_float(entry.get("coolSet")),
            enabled=entry.get("enabled", True) is True,
        )

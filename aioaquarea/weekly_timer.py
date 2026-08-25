from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import IntEnum
from zoneinfo import ZoneInfo


class DayOfWeek(IntEnum):
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7


@dataclass(frozen=True, slots=True)
class WeeklyTimerSlot:
    day: DayOfWeek
    zone_id: int
    start: dt.time
    end: dt.time
    heat_set: float | None = None
    cool_set: float | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class WeeklyTimerSettings:
    enabled: bool
    slots: tuple[WeeklyTimerSlot, ...]

    def active_slots(
        self, at: dt.datetime, timezone: str | ZoneInfo
    ) -> tuple[WeeklyTimerSlot, ...]:
        """Return slots active at an aware instant, including midnight spans."""
        if not self.enabled:
            return ()
        if at.tzinfo is None:
            raise ValueError("Weekly timer evaluation requires an aware datetime")
        zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        local = at.astimezone(zone)
        minute = local.hour * 60 + local.minute
        weekday = local.isoweekday()
        active: list[WeeklyTimerSlot] = []
        for slot in self.slots:
            if not slot.enabled:
                continue
            start = slot.start.hour * 60 + slot.start.minute
            end = slot.end.hour * 60 + slot.end.minute
            if start < end:
                matches = slot.day.value == weekday and start <= minute < end
            elif start > end:
                previous_day = 7 if weekday == 1 else weekday - 1
                matches = (
                    slot.day.value == weekday and minute >= start
                ) or (slot.day.value == previous_day and minute < end)
            else:
                matches = False
            if matches:
                active.append(slot)
        return tuple(active)

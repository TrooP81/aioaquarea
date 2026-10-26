"""Canonical comfort schedule validation and UTC interval resolution."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError


UTC = dt.timezone.utc
DAY_TYPES = ("weekday", "weekend")
TIMEZONE_POLICY = "local-wall-hour-by-utc-half-open-v1"
CONTROL_SAMPLING_POLICY = "utc-hour-slot-start-v1"


def sample_control_target(targets: list[dict], slot_start: dt.datetime) -> dict:
    """Sample exact policy at the start of [slot_start, slot_start + 1h)."""
    instant = _aware_utc(slot_start)
    matches = [
        row
        for row in targets
        if dt.datetime.fromisoformat(row["interval_start_utc"])
        <= instant
        < dt.datetime.fromisoformat(row["interval_end_utc"])
    ]
    if len(matches) != 1:
        raise ValueError("control slot has no unique authoritative target")
    return matches[0]


@lru_cache(maxsize=1)
def timezone_data_version() -> str:
    """Identify the timezone database used by ZoneInfo for historical replay."""
    for root in TZPATH:
        version_file = Path(root) / "tzdata.zi"
        try:
            with version_file.open(encoding="utf-8") as stream:
                first_line = stream.readline()
        except OSError:
            continue
        match = re.match(r"# version ([A-Za-z0-9._-]+)", first_line)
        if match:
            return f"system:{match.group(1)}"
    try:
        return f"tzdata:{version('tzdata')}"
    except PackageNotFoundError as exc:
        raise ValueError("timezone database version cannot be determined") from exc


def normalize_schedule(value: object) -> dict[str, list[int]]:
    """Validate and canonicalize a weekday/weekend hour mask."""
    if not isinstance(value, dict) or set(value) - set(DAY_TYPES):
        raise ValueError("comfort_schedule may contain only weekday and weekend")

    result: dict[str, list[int]] = {}
    for day_type in DAY_TYPES:
        hours = value.get(day_type, [])
        if not isinstance(hours, list):
            raise ValueError(f"comfort_schedule.{day_type} must be a list")
        if any(isinstance(hour, bool) or not isinstance(hour, int) for hour in hours):
            raise ValueError(f"comfort_schedule.{day_type} values must be integer hours")
        if any(hour < 0 or hour > 23 for hour in hours):
            raise ValueError(f"comfort_schedule.{day_type} values must be between 0 and 23")
        if len(set(hours)) != len(hours):
            raise ValueError(f"comfort_schedule.{day_type} must not contain duplicate hours")
        result[day_type] = sorted(hours)
    return result


def normalize_timezone(timezone_name: str) -> str:
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {timezone_name!r}") from exc
    return timezone_name


def _aware_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule resolution requires timezone-aware instants")
    return value.astimezone(UTC)


def resolve_hourly_targets(
    schedule: object,
    timezone_name: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    scheduled_target_c: float,
    unscheduled_target_c: float,
    outer_max_c: float,
) -> list[dict[str, object]]:
    """Project local comfort hours onto exact clipped, half-open UTC intervals.

    Split at UTC planner hours, valid local wall hours, and offset transitions.
    Planner controls remain hourly and sample policy at the slot start; the
    persisted policy projection retains every boundary inside a control slot.
    """
    normalized = normalize_schedule(schedule)
    zone = ZoneInfo(normalize_timezone(timezone_name))
    tzdata_version = timezone_data_version()
    utc_start = _aware_utc(start)
    utc_end = _aware_utc(end)
    if utc_end <= utc_start:
        raise ValueError("schedule resolution end must be after start")

    boundaries = {utc_start, utc_end}
    local_day = (utc_start.astimezone(zone) - dt.timedelta(days=2)).date()
    last_day = (utc_end.astimezone(zone) + dt.timedelta(days=2)).date()
    while local_day <= last_day:
        for hour in range(24):
            wall = dt.datetime.combine(local_day, dt.time(hour))
            for fold in (0, 1):
                candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                # Reject nonexistent wall times, retaining both genuine folds.
                if candidate.astimezone(zone).replace(tzinfo=None) == wall:
                    if utc_start < candidate < utc_end:
                        boundaries.add(candidate)
        local_day += dt.timedelta(days=1)

    cursor = utc_start.replace(minute=0, second=0, microsecond=0)
    while cursor < utc_end:
        hour_end = cursor + dt.timedelta(hours=1)
        if utc_start < hour_end < utc_end:
            boundaries.add(hour_end)
        if cursor.astimezone(zone).utcoffset() != hour_end.astimezone(zone).utcoffset():
            left, right = cursor, hour_end
            offset = left.astimezone(zone).utcoffset()
            while right - left > dt.timedelta(microseconds=1):
                middle = left + (right - left) // 2
                if middle.astimezone(zone).utcoffset() == offset:
                    left = middle
                else:
                    right = middle
            if utc_start < right < utc_end:
                boundaries.add(right)
        cursor = hour_end

    result: list[dict[str, object]] = []
    ordered = sorted(boundaries)
    for interval_start, interval_end in zip(ordered, ordered[1:]):
        local = interval_start.astimezone(zone)
        day_type = "weekday" if local.weekday() < 5 else "weekend"
        scheduled = local.hour in normalized[day_type]
        result.append(
            {
                "start_utc": interval_start.isoformat(),
                "end_utc": interval_end.isoformat(),
                "local_start": interval_start.astimezone(zone).isoformat(),
                "local_end": interval_end.astimezone(zone).isoformat(),
                "local_date": interval_start.astimezone(zone).date().isoformat(),
                "local_hour": interval_start.astimezone(zone).hour,
                "day_type": day_type,
                "utc_offset_minutes": int(
                    (interval_start.astimezone(zone).utcoffset() or dt.timedelta()).total_seconds()
                    // 60
                ),
                "fold": interval_start.astimezone(zone).fold,
                "scheduled": scheduled,
                "preferred_comfort": scheduled,
                "target_c": scheduled_target_c if scheduled else unscheduled_target_c,
                "outer_min_c": unscheduled_target_c,
                "outer_max_c": outer_max_c,
                "reason": "base_weekday" if day_type == "weekday" else "base_weekend",
                "timezone_policy": TIMEZONE_POLICY,
                "tzdata_version": tzdata_version,
            }
        )
    return result


def resolve_comfort_intervals(
    schedule: object, timezone_name: str, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Return scheduled half-open UTC intervals for an aware time range."""
    slots = resolve_hourly_targets(
        schedule,
        timezone_name,
        start,
        end,
        scheduled_target_c=0.0,
        unscheduled_target_c=0.0,
        outer_max_c=0.0,
    )
    return [
        (
            dt.datetime.fromisoformat(str(slot["start_utc"])).astimezone(UTC),
            dt.datetime.fromisoformat(str(slot["end_utc"])).astimezone(UTC),
        )
        for slot in slots
        if slot["scheduled"]
    ]

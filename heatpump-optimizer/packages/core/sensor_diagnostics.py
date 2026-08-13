"""Observation-only quality checks for indoor-temperature sensors."""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any, Iterable


STALE_AFTER = dt.timedelta(minutes=15)
MIN_SAMPLES_FOR_SHADOW = 24


def summarize_sensor_diagnostics(
    rows: Iterable[Any], *, reference_sensor_id: str = "", now: dt.datetime | None = None
) -> dict[str, Any]:
    """Summarise sensor health without changing which sensor controls comfort.

    Rooms can legitimately have different temperatures, so this deliberately
    does *not* reject a sensor merely because it differs from another room.
    It only surfaces cadence, staleness and broad cross-room spread as a
    shadow diagnostic for the user to review.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[str(row.device_id)].append(row)

    sensors: list[dict[str, Any]] = []
    medians: list[float] = []
    for device_id, readings in grouped.items():
        readings.sort(key=lambda row: row.timestamp)
        fresh = [row for row in readings if not bool(row.is_stale)]
        latest = readings[-1]
        latest_ts = latest.timestamp
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=dt.timezone.utc)
        age_seconds = max(0, round((now - latest_ts).total_seconds()))
        temperatures = [float(row.temperature) for row in fresh]
        median = round(float(statistics.median(temperatures)), 2) if temperatures else None
        if median is not None:
            medians.append(median)
        stale_fraction = round((len(readings) - len(fresh)) / len(readings), 3) if readings else 1.0
        state = "healthy"
        reasons: list[str] = []
        if not fresh:
            state = "no_fresh_data"
            reasons.append("no_fresh_readings")
        elif age_seconds > STALE_AFTER.total_seconds():
            state = "stale"
            reasons.append("latest_reading_stale")
        elif len(fresh) < MIN_SAMPLES_FOR_SHADOW:
            state = "collecting"
            reasons.append("collecting_shadow_evidence")
        elif stale_fraction > 0.2:
            state = "intermittent"
            reasons.append("frequent_stale_readings")
        sensors.append(
            {
                "device_id": device_id,
                "label": latest.device_label,
                "room": latest.room,
                "state": state,
                "reasons": reasons,
                "samples": len(readings),
                "fresh_samples": len(fresh),
                "stale_fraction": stale_fraction,
                "latest_reading": latest_ts.isoformat(),
                "age_seconds": age_seconds,
                "median_temperature_c": median,
                "is_reference": device_id == reference_sensor_id,
            }
        )

    fleet_median = round(float(statistics.median(medians)), 2) if medians else None
    for sensor in sensors:
        median = sensor["median_temperature_c"]
        sensor["median_offset_from_rooms_c"] = (
            round(float(median) - fleet_median, 2)
            if median is not None and fleet_median is not None
            else None
        )

    candidate = next(
        (
            item
            for item in sorted(
                sensors, key=lambda item: (-item["fresh_samples"], item["age_seconds"])
            )
            if item["state"] == "healthy"
        ),
        None,
    )
    return {
        "mode": "shadow",
        "controls_unchanged": True,
        "reference_sensor_id": reference_sensor_id or None,
        "sensor_count": len(sensors),
        "room_median_temperature_c": fleet_median,
        "room_spread_c": round(max(medians) - min(medians), 2) if len(medians) >= 2 else None,
        "sensors": sensors,
        "suggested_reference_sensor_id": (
            candidate["device_id"] if candidate and not reference_sensor_id else None
        ),
        "summary": (
            "Sensor diagnostics are observation-only; they never change the comfort reference automatically."
        ),
    }

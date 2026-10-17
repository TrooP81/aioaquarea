import datetime as dt
from types import SimpleNamespace

from packages.core.sensor_diagnostics import summarize_sensor_diagnostics


def test_sensor_diagnostics_is_shadow_only_and_flags_stale_reference():
    now = dt.datetime(2026, 7, 17, 12, tzinfo=dt.timezone.utc)
    rows = [
        SimpleNamespace(
            device_id="reference",
            device_label="Hall",
            room="Hall",
            temperature=21.0,
            timestamp=now - dt.timedelta(minutes=30),
            is_stale=False,
        ),
    ] + [
        SimpleNamespace(
            device_id="fresh",
            device_label="Living room",
            room="Living",
            temperature=22.0,
            timestamp=now - dt.timedelta(minutes=index),
            is_stale=False,
        )
        for index in range(24)
    ]

    result = summarize_sensor_diagnostics(rows, reference_sensor_id="reference", now=now)

    assert result["mode"] == "shadow"
    assert result["controls_unchanged"] is True
    assert result["suggested_reference_sensor_id"] is None
    reference = next(sensor for sensor in result["sensors"] if sensor["device_id"] == "reference")
    assert reference["state"] == "stale"


def test_get_sensor_diagnostics_selection_future_input_excluded():
    now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
    rows = [
        SimpleNamespace(
            device_id="future",
            device_label="Future clock",
            room="Living",
            temperature=28.0,
            timestamp=now + dt.timedelta(hours=1, seconds=index),
            is_stale=False,
        )
        for index in range(24)
    ]

    result = summarize_sensor_diagnostics(rows, now=now)

    assert result["sensors"][0]["fresh_samples"] == 0
    assert result["sensors"][0]["state"] != "healthy"
    assert result["room_median_temperature_c"] is None
    assert result["suggested_reference_sensor_id"] is None

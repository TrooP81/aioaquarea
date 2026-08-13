from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from packages.core import outdoor_temperature as subject


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.executed = False

    async def execute(self, _statement):
        self.executed = True
        return _Result(self.rows)


def _weather(
    at: dt.datetime,
    temperature: float,
    *,
    issued_at: dt.datetime | None = None,
):
    return SimpleNamespace(
        ts=at,
        temperature=temperature,
        forecast_issued_at=issued_at or at,
    )


def test_nearest_weather_temperature_rejects_distant_rows():
    now = dt.datetime(2026, 7, 28, 15, tzinfo=dt.timezone.utc)
    rows = [_weather(now - dt.timedelta(hours=3), 20.0)]

    assert subject.nearest_weather_temperature(rows, now) is None


@pytest.mark.asyncio
async def test_weather_report_replaces_sun_heated_pump_sensor(monkeypatch):
    now = dt.datetime(2026, 7, 28, 15, 15, tzinfo=dt.timezone.utc)
    session = _Session([_weather(now.replace(minute=0), 23.1)])

    async def string_setting(key):
        return {
            "outdoor_temperature_source": "weather",
            "weather_provider": "smhi",
        }[key]

    async def float_setting(_key):
        return 0.0

    async def int_setting(_key):
        return 180

    monkeypatch.setattr(subject, "get_string_setting", string_setting)
    monkeypatch.setattr(subject, "get_float_setting", float_setting)
    monkeypatch.setattr(subject, "get_int_setting", int_setting)

    reading = await subject.resolve_outdoor_temperature(
        session,
        heat_pump_c=29.0,
        at=now,
    )

    assert reading.effective_c == pytest.approx(23.1)
    assert reading.heat_pump_c == pytest.approx(29.0)
    assert reading.weather_c == pytest.approx(23.1)
    assert reading.compensation_c == pytest.approx(-5.9)
    assert reading.source == "weather"
    assert reading.weather_provider == "smhi"


@pytest.mark.asyncio
async def test_stale_weather_falls_back_to_pump_sensor(monkeypatch):
    now = dt.datetime(2026, 7, 28, 15, 15, tzinfo=dt.timezone.utc)
    session = _Session(
        [
            _weather(
                now.replace(minute=0),
                23.1,
                issued_at=now - dt.timedelta(hours=4),
            )
        ]
    )

    async def string_setting(key):
        return {
            "outdoor_temperature_source": "weather",
            "weather_provider": "smhi",
        }[key]

    async def float_setting(_key):
        return 0.0

    async def int_setting(_key):
        return 180

    monkeypatch.setattr(subject, "get_string_setting", string_setting)
    monkeypatch.setattr(subject, "get_float_setting", float_setting)
    monkeypatch.setattr(subject, "get_int_setting", int_setting)

    reading = await subject.resolve_outdoor_temperature(
        session,
        heat_pump_c=29.0,
        at=now,
    )

    assert reading.effective_c == pytest.approx(29.0)
    assert reading.source == "heat_pump_fallback"
    assert reading.fallback_reason == "weather_report_stale"


@pytest.mark.asyncio
async def test_manual_weather_offset_is_explicit(monkeypatch):
    now = dt.datetime(2026, 7, 28, 15, 15, tzinfo=dt.timezone.utc)
    session = _Session([_weather(now.replace(minute=0), 23.1)])

    async def string_setting(key):
        return {
            "outdoor_temperature_source": "weather",
            "weather_provider": "smhi",
        }[key]

    async def float_setting(_key):
        return -0.5

    async def int_setting(_key):
        return 180

    monkeypatch.setattr(subject, "get_string_setting", string_setting)
    monkeypatch.setattr(subject, "get_float_setting", float_setting)
    monkeypatch.setattr(subject, "get_int_setting", int_setting)

    reading = await subject.resolve_outdoor_temperature(
        session,
        heat_pump_c=29.0,
        at=now,
    )

    assert reading.effective_c == pytest.approx(22.6)
    assert reading.compensation_c == pytest.approx(-6.4)

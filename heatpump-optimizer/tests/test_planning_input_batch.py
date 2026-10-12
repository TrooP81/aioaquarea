"""Pure frozen-provider readiness regressions; no services or event loop."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from packages.core.planning_data_quality import (
    PlanningInputBatch,
    PriceInput,
    WeatherInput,
    planning_batch_quality,
)


def _batch():
    now = dt.datetime(2026, 10, 12, 10, tzinfo=dt.timezone.utc)
    start = now + dt.timedelta(hours=1)
    prices = tuple(
        PriceInput(
            start + dt.timedelta(hours=h), -0.1, "SEK", "tibber", now, "hourly_duration_weighted_v1"
        )
        for h in range(24)
    )
    weather = tuple(
        WeatherInput(
            start + dt.timedelta(hours=h),
            0.0,
            10.0,
            100.0,
            0.0,
            50.0,
            0.5,
            "open-meteo",
            now,
            "weather_c_mps_wm2_mm_v1",
        )
        for h in range(24)
    )
    return PlanningInputBatch(
        now,
        start,
        start + dt.timedelta(hours=24),
        "tibber",
        "tibber",
        "open-meteo",
        prices,
        weather,
    )


def test_R18_selected_complete_provider_batch_is_ready():
    batch = _batch()
    quality = planning_batch_quality(batch)
    assert quality["control_allowed"] is True
    assert quality["effective_horizon_hours"] == 24
    assert quality["price"]["currency"] == "SEK"
    assert quality["price"]["source"] == "tibber"
    assert quality["weather"]["source"] == "open-meteo"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "entsoe"),
        ("currency", "EUR"),
        ("interval_schema", None),
        ("value", float("nan")),
        ("fetched_at", None),
    ],
)
def test_R18_foreign_or_unqualified_price_cannot_certify_batch(field, value):
    batch = _batch()
    prices = (replace(batch.prices[0], **{field: value}),) + batch.prices[1:]
    assert planning_batch_quality(replace(batch, prices=prices))["control_allowed"] is False


@pytest.mark.parametrize("future", [True, False])
def test_R18_recent_global_clock_cannot_hide_bad_horizon_clock(future):
    batch = _batch()
    clock = (
        batch.captured_at + dt.timedelta(seconds=1)
        if future
        else batch.captured_at - dt.timedelta(hours=5)
    )
    weather = (replace(batch.weather[0], forecast_issued_at=clock),) + batch.weather[1:]
    quality = planning_batch_quality(replace(batch, weather=weather))
    assert quality["control_allowed"] is False
    assert quality["weather"]["fresh"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "smhi"),
        ("temperature", float("inf")),
        ("wind_speed", None),
        ("irradiance", float("nan")),
        ("units_schema", None),
    ],
)
def test_R18_wrong_weather_or_missing_required_dimension_is_unavailable(field, value):
    batch = _batch()
    weather = (replace(batch.weather[0], **{field: value}),) + batch.weather[1:]
    assert planning_batch_quality(replace(batch, weather=weather))["control_allowed"] is False


def test_R18_missing_or_duplicate_hour_never_completes_horizon():
    batch = _batch()
    assert (
        planning_batch_quality(replace(batch, prices=batch.prices[1:]))["control_allowed"] is False
    )
    assert (
        planning_batch_quality(replace(batch, weather=batch.weather + batch.weather[:1]))[
            "control_allowed"
        ]
        is False
    )

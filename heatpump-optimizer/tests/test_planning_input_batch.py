"""Pure frozen-provider readiness regressions; no services or event loop."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize(
    "feed,clock",
    [
        ("prices", "fetched_at"),
        ("weather", "forecast_issued_at"),
    ],
)
def test_R18_naive_horizon_clock_degrades_without_comparing_mixed_datetimes(feed, clock):
    batch = _batch()
    rows = getattr(batch, feed)
    rows = (replace(rows[0], **{clock: batch.captured_at.replace(tzinfo=None)}),) + rows[1:]
    quality = planning_batch_quality(replace(batch, **{feed: rows}))
    assert quality["control_allowed"] is False
    assert quality["status"] == "degraded"
    assert quality["price" if feed == "prices" else "weather"]["fresh"] is False


@pytest.mark.asyncio
async def test_R18_planning_accessors_and_readiness_reuse_one_frozen_batch(monkeypatch):
    from packages.core import planning_data_quality as quality_module
    from packages.optimizer import data_access

    batch = _batch()
    session = SimpleNamespace(info={})
    loader = AsyncMock(return_value=batch)
    monkeypatch.setattr(data_access, "load_planning_input_batch", loader)
    prices = await data_access.get_prices(session, batch.start, batch.end)
    # A hypothetical newer retrieval/settings context must not replace this batch.
    loader.return_value = replace(batch, price_provider="entsoe", prices=())
    weather = await data_access.get_weather(session, batch.start, batch.end)
    full = await data_access.get_weather_full(session, batch.start, batch.end)
    monkeypatch.setattr(
        quality_module,
        "load_planning_input_batch",
        AsyncMock(side_effect=AssertionError("must not reload")),
    )
    quality = await quality_module.get_planning_data_quality(
        batch=session.info["planning_input_batch"],
        now=batch.captured_at + dt.timedelta(days=1),
    )
    loader.assert_awaited_once_with(session, batch.start, batch.end)
    assert session.info["planning_input_batch"] is batch
    assert prices == [(row.ts, row.value) for row in batch.prices]
    assert weather == [(row.ts, row.temperature) for row in batch.weather]
    assert [row["source"] for row in full] == ["open-meteo"] * 24
    assert quality == planning_batch_quality(batch)
    assert quality["control_allowed"] is True
    assert quality["captured_at"] == batch.captured_at.isoformat()
    # A different planning window must load a different frozen batch.
    await data_access.get_prices(session, batch.start, batch.end + dt.timedelta(hours=1))
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_R18_loader_selects_context_and_freezes_orm_values(monkeypatch):
    from packages.core import planning_data_quality as quality_module

    batch = _batch()
    prices = [
        SimpleNamespace(
            ts=row.ts,
            price_eur_per_kwh=row.value,
            price_currency=row.currency,
            price_source=row.source,
            fetched_at=row.fetched_at,
            interval_schema=row.interval_schema,
        )
        for row in batch.prices
    ]
    weather = [SimpleNamespace(**vars(row)) for row in batch.weather]

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    session = SimpleNamespace(execute=AsyncMock(side_effect=[Result(prices), Result(weather)]))
    settings = AsyncMock(
        return_value={
            "price_provider": "tibber",
            "entsoe_area": "foreign-area",
            "weather_provider": "open-meteo",
        }
    )
    monkeypatch.setattr(quality_module, "get_all_settings", settings)
    frozen = await quality_module.load_planning_input_batch(
        session,
        batch.start,
        batch.end,
        now=batch.captured_at,
    )
    settings.assert_awaited_once_with(session=session)
    assert frozen == batch
    price_query, weather_query = [call.args[0] for call in session.execute.await_args_list]
    assert price_query.compile().params == {
        "area_1": "tibber",
        "ts_1": batch.start,
        "ts_2": batch.end,
    }
    assert weather_query.compile().params == {
        "source_1": "open-meteo",
        "ts_1": batch.start,
        "ts_2": batch.end,
    }
    assert "ORDER BY prices.ts" in str(price_query)
    assert "ORDER BY weather.ts" in str(weather_query)
    prices[0].price_eur_per_kwh = float("nan")
    weather[0].temperature = None
    assert frozen.prices[0].value == -0.1
    assert frozen.weather[0].temperature == 0.0
    assert planning_batch_quality(frozen)["control_allowed"] is True


@pytest.mark.parametrize("hours,allowed", [(5, False), (6, True), (24, True)])
def test_R18_effective_horizon_matches_available_frozen_prefix(hours, allowed):
    batch = _batch()
    quality = planning_batch_quality(replace(batch, prices=batch.prices[:hours]))
    assert quality["control_allowed"] is allowed
    assert quality["effective_horizon_hours"] == hours
    assert quality["price_horizon_limited"] is (hours < 24)

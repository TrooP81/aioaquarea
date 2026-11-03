"""All current-feed reads and compatibility writes respect installation identity."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core import price_scope, settings_service
from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    PriceRecord,
    PriceScopeRecord,
)
from packages.ml import forecast_quality

NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)


class Context:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


def values(reason=None):
    return {
        "price_provider": "tibber",
        "tibber_home_id": "B",
        "weather_provider": "open-meteo",
        "tibber_feed_diagnostic": json.dumps(
            {"home_id": "B", "binding_method": "single_active_subscription", "reason": reason}
        ),
    }


@pytest.mark.parametrize(
    "reason", [None, "selected_home_has_no_prices", price_scope.AUTO_BINDING_SUSPENDED]
)
async def test_native_materialization_filters_captured_scope_before_limit_and_honors_latch(
    monkeypatch, reason
):
    from packages.core import house_contract

    captured = values(reason)
    contract_revision = SimpleNamespace(
        id=7,
        contract_hash="a" * 64,
        contract_json={
            "installation_id": "00000000-0000-0000-0000-000000000001",
            "timezone": "Europe/Amsterdam",
        },
    )
    monkeypatch.setattr(
        house_contract,
        "ensure_current_contract",
        AsyncMock(return_value=contract_revision),
    )
    status = DeviceStatusRecord(ts=NOW, device_id="heatpump", tank_temp=50)
    prices = [
        PriceRecord(
            ts=NOW,
            area=area,
            price_eur_per_kwh=amount,
            price_currency="SEK",
            price_source="tibber",
            fetched_at=NOW,
        )
        for area, amount in [
            ("tibber", 99),
            (price_scope.tibber_scope("A"), 88),
            (price_scope.tibber_scope("B"), -0.2),
        ]
    ]
    statements = []

    async def execute(statement):
        statements.append(statement)
        model = statement.column_descriptions[0]["entity"]
        if model is PriceScopeRecord:
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(provider="tibber", home_id="B")
            )
        if model is PriceRecord:
            params = statement.compile().params
            assert params["area_1"] == price_scope.tibber_scope("B")
            sql = str(statement)
            assert sql.index("prices.area =") < sql.index("ORDER BY") < sql.index("LIMIT")
            assert statement._limit_clause.value == 101
            rows = [row for row in prices if row.area == params["area_1"]]
        else:
            rows = [status] if model is DeviceStatusRecord else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        get=AsyncMock(
            return_value=SimpleNamespace(
                installation_id="00000000-0000-0000-0000-000000000001",
                demand_dependency_generation=1,
            )
        ),
    )
    monkeypatch.setattr(
        forecast_quality,
        "get_session",
        MagicMock(side_effect=AssertionError("must reuse caller session")),
    )
    monkeypatch.setattr(
        settings_service,
        "get_all_settings",
        AsyncMock(side_effect=AssertionError("must use captured settings")),
    )
    result = await forecast_quality.materialize_native_inputs(
        "demand", now=NOW, session=session, context={"settings": captured}
    )
    assert result is not None
    assert result["pricing"]["area"] == (
        price_scope.UNAVAILABLE_AREA if reason else price_scope.tibber_scope("B")
    )
    assert result["pricing"]["available"] is (reason is None)
    assert [point["price_eur_per_kwh"] for point in result["pricing"]["points"]] == (
        [] if reason else [-0.2]
    )
    assert sum(
        statement.column_descriptions[0]["entity"] is PriceRecord for statement in statements
    ) == (0 if reason else 1)


@pytest.mark.parametrize("route", ["stats", "dashboard"])
@pytest.mark.parametrize("latched", [False, True])
async def test_reporting_costs_never_use_another_homes_prices(monkeypatch, route, latched):
    from packages.api.routers import dashboard
    from packages.core.pricing import PriceContext
    from packages.core.space_heating_gate import HeatingGateConfig
    from tests.test_p16a_reporting import Result, START, rows, session_context

    now = START + dt.timedelta(hours=2)
    records = rows()
    scope_b = price_scope.UNAVAILABLE_AREA if latched else price_scope.tibber_scope("B")
    context = PriceContext(scope_b, "SEK", "unavailable" if latched else "tibber")
    other_home = price_scope.tibber_scope("A")
    all_prices = [
        (START, 99.0, "SEK", "tibber", other_home),
        (START, 0.0, "SEK", "tibber", scope_b),
        (START + dt.timedelta(hours=1), -0.25, "SEK", "tibber", scope_b),
    ]

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(
        dashboard,
        "dt",
        SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
    )
    monkeypatch.setattr(dashboard, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(dashboard, "get_active_price_context", AsyncMock(return_value=context))
    if route == "dashboard":
        monkeypatch.setattr(
            dashboard, "get_device_data_quality", AsyncMock(return_value={"threshold_seconds": 900})
        )
        monkeypatch.setattr(
            dashboard, "get_space_heating_gate_config", AsyncMock(return_value=HeatingGateConfig())
        )
        monkeypatch.setattr(dashboard, "get_controlling_override", AsyncMock(return_value=None))

    statements = []

    async def execute(statement):
        statements.append(statement)
        descriptions = statement.column_descriptions
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is PriceRecord:
            params = statement.compile().params
            rows_for_scope = [
                (ts, amount, currency, source, area)
                for ts, amount, currency, source, area in all_prices
                if area == params.get("area_1")
                and currency == params.get("price_currency_1")
                and source == params.get("price_source_1")
                and area != price_scope.UNAVAILABLE_AREA
            ]
            for key in ("area_1", "price_currency_1", "price_source_1"):
                assert key in params
            if route == "stats" or len(statement.selected_columns) == 2:
                return Result([(ts, amount) for ts, amount, *_ in rows_for_scope])
            if len(statement.selected_columns) == 3:
                current = [
                    (amount, currency, source) for _, amount, currency, source, _ in rows_for_scope
                ]
                return SimpleNamespace(one_or_none=lambda: current[-1] if current else None)
            return Result(
                [
                    (ts, amount, currency, source)
                    for ts, amount, currency, source, _ in rows_for_scope
                ]
            )
        if entity is ConsumptionRecord:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: records))
        if entity is DeviceStatusRecord:
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        if route == "stats":
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: records))
        if len(statement.selected_columns) == 3:
            return SimpleNamespace(all=lambda: ())
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    session = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    monkeypatch.setattr(dashboard, "get_session", lambda: session_context(session))
    result = await (dashboard.get_stats("day") if route == "stats" else dashboard.get_dashboard())

    price_queries = [
        statement
        for statement in statements
        if statement.column_descriptions
        and statement.column_descriptions[0].get("entity") is PriceRecord
    ]
    assert len(price_queries) == (1 if route == "stats" else 2)
    for statement in price_queries:
        sql = str(statement)
        assert "prices.area =" in sql
        assert "prices.area !=" in sql
        assert "prices.price_currency =" in sql
        assert "prices.price_source =" in sql
        assert statement.compile().params["area_1"] == scope_b

    if route == "stats":
        assert result.partial_priced_amount == (None if latched else pytest.approx(-1.5))
        assert result.total_cost_eur is None
    else:
        assert result.today_cost_priced_amount == (None if latched else pytest.approx(-1.5))
        assert result.today_cost_eur is None


@pytest.mark.parametrize("entry", ["legacy", "poll_now"])
@pytest.mark.parametrize("latched", [False, True])
async def test_poll_now_compatibility_writes_captured_scope_or_nothing_when_latched(
    monkeypatch, entry, latched
):
    from packages.api.routers import polling
    from packages.poller import feeds

    session = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(polling, "get_session", lambda: Context(session))
    monkeypatch.setattr(
        polling,
        "get_price_area",
        AsyncMock(side_effect=AssertionError("must not reconstruct scope after fetch")),
    )
    monkeypatch.setattr(settings_service, "get_setting", AsyncMock(return_value=""))
    context = feeds.FetchContext(
        "tibber", NOW, area=price_scope.tibber_scope("A"), tibber_home_id="A"
    )
    feed = feeds.PriceFeed(
        [] if latched else [(NOW, -0.2)],
        "SEK",
        "tibber",
        context,
        feeds.PRICE_INTERVAL_SCHEMA,
        price_scope.AUTO_BINDING_SUSPENDED if latched else None,
    )
    monkeypatch.setattr(feeds, "fetch_price_feed", AsyncMock(return_value=feed))
    monkeypatch.setattr(feeds, "fetch_weather", AsyncMock(return_value=[]))
    registry = AsyncMock()
    monkeypatch.setattr(price_scope, "ensure_tibber_scope", registry)
    if entry == "legacy":
        result = {}
        await polling._poll_prices_and_weather_legacy(result)
    else:
        result = (await polling.poll_now(None))["results"]
    assert result["prices"]["success"] is (not latched)
    if latched:
        session.execute.assert_not_awaited()
        registry.assert_not_awaited()
    else:
        registry.assert_awaited_once_with(
            session, area=price_scope.tibber_scope("A"), home_id="A", currency="SEK"
        )
        params = session.execute.await_args.args[0].compile().params
        assert params["area"] == price_scope.tibber_scope("A")
        assert params["fetched_at"] == NOW
        assert params["interval_schema"] == feeds.PRICE_INTERVAL_SCHEMA

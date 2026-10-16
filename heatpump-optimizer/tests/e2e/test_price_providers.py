"""E2E tests: Price provider integration (Tibber and ENTSO-E feeds)."""

import datetime as dt
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from packages.poller.feeds import (
    fetch_price_feed,
    fetch_prices,
    _fetch_prices_entsoe,
    _fetch_prices_tibber,
)

ENTSOE_UNITS = (
    "<currency_Unit.name>EUR</currency_Unit.name>"
    "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
)


SAMPLE_ENTSOE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
    <TimeSeries>
        <currency_Unit.name>EUR</currency_Unit.name>
        <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
        <Period>
            <timeInterval>
                <start>2026-04-30T00:00Z</start>
                <end>2026-05-01T00:00Z</end>
            </timeInterval>
            <resolution>PT60M</resolution>
            <Point><position>1</position><price.amount>85.00</price.amount></Point>
            <Point><position>2</position><price.amount>72.50</price.amount></Point>
            <Point><position>3</position><price.amount>45.00</price.amount></Point>
            <Point><position>4</position><price.amount>110.00</price.amount></Point>
        </Period>
    </TimeSeries>
</Publication_MarketDocument>"""
ENTSOE_DECLARED_UNITS = (
    "<currency_Unit.name>EUR</currency_Unit.name>\n"
    "        <price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
)
assert SAMPLE_ENTSOE_XML.count(ENTSOE_DECLARED_UNITS) == 1
ENTSOE_EXPECTED_HOURS = [
    dt.datetime(2026, 4, 30, hour, tzinfo=dt.timezone.utc) for hour in range(4)
]


SAMPLE_TIBBER_RESPONSE = {
    "data": {
        "viewer": {
            "homes": [
                {
                    "id": "test-home",
                    "currentSubscription": {
                        "priceInfo": {
                            "today": [
                                {
                                    "total": 0.2850,
                                    "currency": "SEK",
                                    "startsAt": "2026-04-30T00:00:00+02:00",
                                },
                                {
                                    "total": 0.2510,
                                    "currency": "SEK",
                                    "startsAt": "2026-04-30T01:00:00+02:00",
                                },
                                {
                                    "total": 0.2340,
                                    "currency": "SEK",
                                    "startsAt": "2026-04-30T02:00:00+02:00",
                                },
                                {
                                    "total": 0.2200,
                                    "currency": "SEK",
                                    "startsAt": "2026-04-30T03:00:00+02:00",
                                },
                            ],
                            "tomorrow": [
                                {
                                    "total": 0.3100,
                                    "currency": "SEK",
                                    "startsAt": "2026-05-01T00:00:00+02:00",
                                },
                                {
                                    "total": 0.2750,
                                    "currency": "SEK",
                                    "startsAt": "2026-05-01T01:00:00+02:00",
                                },
                            ],
                        }
                    },
                }
            ]
        }
    }
}


@pytest.mark.asyncio(loop_scope="session")
class TestEntsoeIntegration:
    @respx.mock
    async def test_fetch_entsoe_prices(self):
        """ENTSO-E fetch parses XML and returns EUR/kWh."""
        respx.get("https://web-api.tp.entsoe.eu/api").mock(
            return_value=Response(200, text=SAMPLE_ENTSOE_XML)
        )

        async def mock_get_setting(key):
            return {
                "entsoe_api_token": "test-token",
                "entsoe_area": "10Y1001A1001A46L",
                "price_provider": "entsoe",
            }.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_entsoe()

        assert prices.currency == "EUR" and prices.source == "entsoe"
        # Declared EUR/MWH: each MWh price divided by 1000 gives EUR/kWh at its UTC hour.
        assert [hour for hour, _price in prices] == ENTSOE_EXPECTED_HOURS
        assert [price for _hour, price in prices] == pytest.approx([0.085, 0.0725, 0.045, 0.11])

    @pytest.mark.parametrize(
        "units",
        [
            "",
            "<currency_Unit.name>EUR</currency_Unit.name>",
            "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>",
            ENTSOE_UNITS.replace(">MWH<", ">KWH<"),
            ENTSOE_UNITS.replace(">EUR<", ">euro<"),
        ],
        ids=["no_units", "no_measure", "no_currency", "wrong_measure", "bad_currency"],
    )
    @respx.mock
    async def test_fetch_entsoe_rejects_missing_or_wrong_metadata(self, units):
        """Undeclared or wrong currency/unit metadata never yields guessed prices."""
        xml = SAMPLE_ENTSOE_XML.replace(ENTSOE_DECLARED_UNITS, units)
        assert xml != SAMPLE_ENTSOE_XML
        respx.get("https://web-api.tp.entsoe.eu/api").mock(return_value=Response(200, text=xml))

        async def mock_get_setting(key):
            return {
                "entsoe_api_token": "test-token",
                "entsoe_area": "10Y1001A1001A46L",
                "price_provider": "entsoe",
            }.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_entsoe()

        assert list(prices) == [] and prices.source == "entsoe"

    @respx.mock
    async def test_fetch_entsoe_empty_token(self):
        """No fetch when token is empty."""

        async def mock_get_setting(key):
            return {
                "entsoe_api_token": "",
                "entsoe_area": "10Y1001A1001A46L",
                "price_provider": "entsoe",
            }.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_entsoe()

        assert prices == []


@pytest.mark.asyncio(loop_scope="session")
class TestTibberIntegration:
    @respx.mock
    async def test_fetch_tibber_prices(self):
        """Tibber fetch parses GraphQL response."""
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=SAMPLE_TIBBER_RESPONSE)
        )

        async def mock_get_setting(key):
            return {"tibber_api_token": "test-tibber-token", "price_provider": "tibber"}.get(
                key, ""
            )

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await _fetch_prices_tibber()

        # 4 today + 2 tomorrow = 6
        assert len(prices) == 6
        assert prices[0][1] == 0.2850
        assert prices[1][1] == 0.2510
        # Tomorrow prices
        assert prices[4][1] == 0.3100

    @respx.mock
    async def test_fetch_tibber_empty_token(self):
        """No fetch when token is empty."""

        async def mock_get_setting(key):
            return {"tibber_api_token": "", "price_provider": "tibber"}.get(key, "")

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await _fetch_prices_tibber()

        assert prices == []

    @respx.mock
    async def test_fetch_tibber_no_homes(self):
        """Gracefully handles account with no homes."""
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json={"data": {"viewer": {"homes": []}}})
        )

        async def mock_get_setting(key):
            return {"tibber_api_token": "test-token", "price_provider": "tibber"}.get(key, "")

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await _fetch_prices_tibber()

        assert prices == []

    @respx.mock
    async def test_fetch_tibber_no_tomorrow(self):
        """Handles case where tomorrow's prices aren't available yet."""
        response = {
            "data": {
                "viewer": {
                    "homes": [
                        {
                            "id": "test-home",
                            "currentSubscription": {
                                "priceInfo": {
                                    "today": [
                                        {
                                            "currency": "SEK",
                                            "total": 0.25,
                                            "startsAt": "2026-04-30T00:00:00+02:00",
                                        },
                                        {
                                            "currency": "SEK",
                                            "total": 0.22,
                                            "startsAt": "2026-04-30T01:00:00+02:00",
                                        },
                                    ],
                                    "tomorrow": [],
                                }
                            },
                        }
                    ]
                }
            }
        }
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=response)
        )

        async def mock_get_setting(key):
            return {"tibber_api_token": "test-token", "price_provider": "tibber"}.get(key, "")

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await _fetch_prices_tibber()

        assert len(prices) == 2

    @respx.mock
    async def test_fetch_tibber_autobinds_single_active_subscription(self):
        """Auto-bind only the single active subscription, recording its identity."""
        response = {
            "data": {
                "viewer": {
                    "homes": [
                        {"id": "inactive-home", "currentSubscription": None},
                        {
                            "id": "test-home",
                            "currentSubscription": {
                                "priceInfo": {
                                    "today": [
                                        {
                                            "currency": "SEK",
                                            "total": 0.31,
                                            "startsAt": "2026-05-04T00:00:00+02:00",
                                        },
                                        {
                                            "currency": "SEK",
                                            "total": 0.28,
                                            "startsAt": "2026-05-04T01:00:00+02:00",
                                        },
                                    ],
                                    "tomorrow": [
                                        {
                                            "currency": "SEK",
                                            "total": 0.22,
                                            "startsAt": "2026-05-05T00:00:00+02:00",
                                        },
                                    ],
                                }
                            },
                        },
                    ]
                }
            }
        }
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=response)
        )

        async def mock_get_setting(key):
            return {"tibber_api_token": "test-token", "price_provider": "tibber"}.get(key, "")

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await _fetch_prices_tibber()

        from packages.core.settings_service import get_setting
        from packages.core.price_scope import tibber_scope
        import json

        assert await get_setting("tibber_home_id") == "test-home"
        diagnostic = json.loads(await get_setting("tibber_feed_diagnostic"))
        assert diagnostic["binding_method"] == "single_active_subscription"
        assert prices.context.area == tibber_scope("test-home")
        assert len(prices) == 3
        assert prices[0][1] == 0.31
        assert prices[2][1] == 0.22


@pytest.mark.asyncio(loop_scope="session")
class TestProviderRouting:
    @respx.mock
    async def test_fetch_prices_routes_to_entsoe(self):
        """fetch_prices() dispatches to ENTSO-E when configured."""
        respx.get("https://web-api.tp.entsoe.eu/api").mock(
            return_value=Response(200, text=SAMPLE_ENTSOE_XML)
        )

        async def mock_get_setting(key):
            return {
                "price_provider": "entsoe",
                "entsoe_api_token": "test-token",
                "entsoe_area": "10Y1001A1001A46L",
            }.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await fetch_prices()

        assert [hour for hour, _price in prices] == ENTSOE_EXPECTED_HOURS
        assert [price for _hour, price in prices] == pytest.approx([0.085, 0.0725, 0.045, 0.11])

    @respx.mock
    async def test_fetch_prices_routes_to_tibber(self):
        """fetch_prices() dispatches to Tibber when configured."""
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=SAMPLE_TIBBER_RESPONSE)
        )

        async def mock_get_setting(key):
            return {"price_provider": "tibber", "tibber_api_token": "test-token"}.get(key, "")

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            prices = await fetch_prices()

        assert len(prices) == 6

    @respx.mock
    async def test_fetch_price_feed_retains_tibber_currency(self):
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=SAMPLE_TIBBER_RESPONSE)
        )

        async def mock_get_setting(key):
            return {"price_provider": "tibber", "tibber_api_token": "test-tibber-token"}.get(
                key, ""
            )

        snapshot = {
            key: await mock_get_setting(key)
            for key in (
                "price_provider",
                "tibber_api_token",
                "tibber_home_id",
            )
        }
        with (
            patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting),
            patch("packages.poller.feeds.get_all_settings", return_value=snapshot),
        ):
            feed = await fetch_price_feed()

        assert feed.currency == "SEK"
        assert feed.source == "tibber"
        assert len(feed.prices) == 6

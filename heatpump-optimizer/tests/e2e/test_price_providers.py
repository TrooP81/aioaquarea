"""E2E tests: Price provider integration (Tibber and ENTSO-E feeds)."""

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import AsyncClient, Response

from packages.poller.feeds import fetch_prices, _fetch_prices_entsoe, _fetch_prices_tibber


SAMPLE_ENTSOE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
    <TimeSeries>
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


SAMPLE_TIBBER_RESPONSE = {
    "data": {
        "viewer": {
            "homes": [
                {
                    "currentSubscription": {
                        "priceInfo": {
                            "today": [
                                {"total": 0.2850, "startsAt": "2026-04-30T00:00:00+02:00"},
                                {"total": 0.2510, "startsAt": "2026-04-30T01:00:00+02:00"},
                                {"total": 0.2340, "startsAt": "2026-04-30T02:00:00+02:00"},
                                {"total": 0.2200, "startsAt": "2026-04-30T03:00:00+02:00"},
                            ],
                            "tomorrow": [
                                {"total": 0.3100, "startsAt": "2026-05-01T00:00:00+02:00"},
                                {"total": 0.2750, "startsAt": "2026-05-01T01:00:00+02:00"},
                            ],
                        }
                    }
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
            return {"entsoe_api_token": "test-token", "entsoe_area": "10Y1001A1001A46L", "price_provider": "entsoe"}.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_entsoe()

        assert len(prices) == 4
        # 85.00 EUR/MWh = 0.085 EUR/kWh
        assert abs(prices[0][1] - 0.085) < 0.0001
        # 45.00 EUR/MWh = 0.045 EUR/kWh
        assert abs(prices[2][1] - 0.045) < 0.0001

    @respx.mock
    async def test_fetch_entsoe_empty_token(self):
        """No fetch when token is empty."""
        async def mock_get_setting(key):
            return {"entsoe_api_token": "", "entsoe_area": "10Y1001A1001A46L", "price_provider": "entsoe"}.get(key, "")

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
            return {"tibber_api_token": "test-tibber-token", "price_provider": "tibber"}.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
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

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
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

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
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
                            "currentSubscription": {
                                "priceInfo": {
                                    "today": [
                                        {"total": 0.25, "startsAt": "2026-04-30T00:00:00+02:00"},
                                        {"total": 0.22, "startsAt": "2026-04-30T01:00:00+02:00"},
                                    ],
                                    "tomorrow": [],
                                }
                            }
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

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_tibber()

        assert len(prices) == 2

    @respx.mock
    async def test_fetch_tibber_skips_unsubscribed_home(self):
        """Uses the first home that actually has subscription price data."""
        response = {
            "data": {
                "viewer": {
                    "homes": [
                        {"currentSubscription": None},
                        {
                            "currentSubscription": {
                                "priceInfo": {
                                    "today": [
                                        {"total": 0.31, "startsAt": "2026-05-04T00:00:00+02:00"},
                                        {"total": 0.28, "startsAt": "2026-05-04T01:00:00+02:00"},
                                    ],
                                    "tomorrow": [
                                        {"total": 0.22, "startsAt": "2026-05-05T00:00:00+02:00"},
                                    ],
                                }
                            }
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

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await _fetch_prices_tibber()

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
            return {"price_provider": "entsoe", "entsoe_api_token": "test-token", "entsoe_area": "10Y1001A1001A46L"}.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await fetch_prices()

        assert len(prices) == 4

    @respx.mock
    async def test_fetch_prices_routes_to_tibber(self):
        """fetch_prices() dispatches to Tibber when configured."""
        respx.post("https://api.tibber.com/v1-beta/gql").mock(
            return_value=Response(200, json=SAMPLE_TIBBER_RESPONSE)
        )

        async def mock_get_setting(key):
            return {"price_provider": "tibber", "tibber_api_token": "test-token"}.get(key, "")

        with patch("packages.poller.feeds.get_setting", side_effect=mock_get_setting):
            prices = await fetch_prices()

        assert len(prices) == 6

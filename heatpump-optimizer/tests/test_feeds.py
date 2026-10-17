"""Tests for the ENTSO-E price parser."""

import asyncio
import datetime as dt
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.poller.feeds import _parse_entsoe_xml, _parse_smhi_forecast


@pytest.mark.parametrize("wind_unit,wind", [("km/h", 36.0), ("m/s", 10.0)])
def test_R17_open_meteo_normalizes_declared_wind_units(wind_unit, wind):
    from packages.poller.feeds import _parse_open_meteo_forecast

    payload = {
        "hourly_units": {
            "temperature_2m": "°C",
            "wind_speed_10m": wind_unit,
            "direct_radiation": "W/m²",
            "precipitation": "mm",
        },
        "hourly": {
            "time": ["2026-10-12T10:00"],
            "temperature_2m": [0.0],
            "wind_speed_10m": [wind],
            "direct_radiation": [100.0],
            "precipitation": [0.0],
            "cloud_cover": [50.0],
        },
    }
    result = _parse_open_meteo_forecast(payload)
    assert result[0]["temperature"] == 0.0
    assert result[0]["wind_speed"] == pytest.approx(10.0)
    assert result[0]["cloud_cover"] == 0.5
    payload["hourly"]["temperature_2m"] = [float("nan")]
    assert _parse_open_meteo_forecast(payload)[0]["temperature"] is None
    payload.pop("hourly_units")
    assert _parse_open_meteo_forecast(payload) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-0.2", -0.2),
        ("invalid", 0.25),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("NaN", None),
    ],
)
async def test_R04_SC056_manual_price_negative_fallback_and_nonfinite(monkeypatch, raw, expected):
    from packages.poller import feeds

    monkeypatch.setattr(
        feeds,
        "get_setting",
        AsyncMock(side_effect=lambda key: "manual" if key == "price_provider" else raw),
    )
    monkeypatch.setattr(feeds, "get_string_setting", AsyncMock(return_value="SEK"))

    result = await feeds.fetch_price_feed()

    assert result.currency == "SEK"
    assert result.source == "manual"
    if expected is None:
        assert result.prices == []
    else:
        assert len(result.prices) == 48
        assert all(price == expected for _, price in result.prices)


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
            <Point>
                <position>1</position>
                <price.amount>45.50</price.amount>
            </Point>
            <Point>
                <position>2</position>
                <price.amount>42.30</price.amount>
            </Point>
            <Point>
                <position>3</position>
                <price.amount>38.10</price.amount>
            </Point>
        </Period>
    </TimeSeries>
</Publication_MarketDocument>"""


class TestEntsoeParser:
    @pytest.mark.parametrize("missing", [False, True])
    def test_R16_quarter_hours_and_later_periods(self, missing):
        points = "".join(
            f"<Point><position>{index}</position><price.amount>{price}</price.amount></Point>"
            for index, price in enumerate((-40, 0, 40, 80), 1)
            if not (missing and index == 2)
        )
        second = SAMPLE_ENTSOE_XML.split("<Period>")[1].split("</Period>")[0]
        first = f"""<Period><timeInterval><start>2026-04-29T23:00Z</start>
        <end>2026-04-30T00:00Z</end></timeInterval><resolution>PT15M</resolution>
        {points}</Period>"""
        xml = SAMPLE_ENTSOE_XML.replace(
            f"<Period>{second}</Period>", first + f"<Period>{second}</Period>"
        )
        result = _parse_entsoe_xml(xml, dt.datetime(2026, 4, 29, tzinfo=dt.timezone.utc))
        assert result.currency == "EUR"
        assert len(result) == (3 if missing else 4)
        if not missing:
            assert result[0] == (dt.datetime(2026, 4, 29, 23, tzinfo=dt.timezone.utc), 0.02)
        assert result[-1][0] == dt.datetime(2026, 4, 30, 2, tzinfo=dt.timezone.utc)

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_R16_nonfinite_hour_not_certified(self, value):
        xml = SAMPLE_ENTSOE_XML.replace("45.50", value)
        result = _parse_entsoe_xml(xml, dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc))
        assert len(result) == 2
        assert result[0][0].hour == 1

    def test_R16_preserves_currency_and_rejects_unit_conflicts(self):
        start = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        assert _parse_entsoe_xml(SAMPLE_ENTSOE_XML.replace("EUR", "SEK"), start).currency == "SEK"
        assert _parse_entsoe_xml(SAMPLE_ENTSOE_XML.replace("MWH", "KWH"), start) == []

    def test_parse_prices(self):
        start = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        result = _parse_entsoe_xml(SAMPLE_ENTSOE_XML, start)

        assert len(result) == 3

        # First hour: 45.50 EUR/MWh = 0.04550 EUR/kWh
        ts, price = result[0]
        assert ts == dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        assert abs(price - 0.04550) < 0.0001

        # Second hour
        ts, price = result[1]
        assert ts == dt.datetime(2026, 4, 30, 1, 0, tzinfo=dt.timezone.utc)
        assert abs(price - 0.04230) < 0.0001

    def test_parse_empty_xml(self):
        result = _parse_entsoe_xml("<root></root>", dt.datetime.now(dt.timezone.utc))
        assert result == []

    def test_parse_invalid_xml(self):
        result = _parse_entsoe_xml("not xml at all", dt.datetime.now(dt.timezone.utc))
        assert result == []


SAMPLE_SMHI_JSON = {
    "createdTime": "2026-07-06T21:19:32Z",
    "referenceTime": "2026-07-06T21:00:00Z",
    "geometry": {"type": "Point", "coordinates": [16.158549, 58.577821]},
    "timeSeries": [
        {
            "time": "2026-07-06T22:00:00Z",
            "data": {
                "air_temperature": 11.2,
                "wind_speed": 2.1,
                "relative_humidity": 54,
                "cloud_area_fraction": 8,  # fully overcast (octas)
                "precipitation": 1.7,
            },
        },
        {
            "time": "2026-07-06T23:00:00Z",
            "data": {
                "air_temperature": 10.5,
                "wind_speed": 2.4,
                "relative_humidity": 58,
                "cloud_area_fraction": 4,  # half cloud
            },
        },
        {
            # Coarser 3h step later in the forecast — hours in between are filled.
            "time": "2026-07-07T02:00:00Z",
            "data": {
                "air_temperature": 9.0,
                "wind_speed": 3.0,
                "relative_humidity": 70,
                "cloud_area_fraction": 0,  # clear sky
            },
        },
    ],
}

# Norrköping-ish coordinates for the sample.
_SMHI_LAT = 58.5812
_SMHI_LON = 16.158


class TestSmhiParser:
    def test_parses_contiguous_hourly_grid(self):
        result = _parse_smhi_forecast(SAMPLE_SMHI_JSON, _SMHI_LAT, _SMHI_LON, hours=6)
        assert len(result) == 6
        assert result[0]["ts"] == dt.datetime(2026, 7, 6, 22, tzinfo=dt.timezone.utc)
        assert result[1]["ts"] == dt.datetime(2026, 7, 6, 23, tzinfo=dt.timezone.utc)
        assert result[2]["ts"] == dt.datetime(2026, 7, 7, 0, tzinfo=dt.timezone.utc)

    def test_maps_parameters(self):
        result = _parse_smhi_forecast(SAMPLE_SMHI_JSON, _SMHI_LAT, _SMHI_LON, hours=3)
        assert result[0]["temperature"] == 11.2
        assert result[0]["wind_speed"] == 2.1
        assert result[0]["humidity"] == 54
        # Cloud cover in octas (0–8) mapped to a fraction (0–1).
        assert result[0]["cloud_cover"] == 1.0
        assert result[1]["cloud_cover"] == 0.5
        assert result[0]["precipitation"] == 1.7

    def test_derives_irradiance_from_cloud_and_geometry(self):
        result = _parse_smhi_forecast(SAMPLE_SMHI_JSON, _SMHI_LAT, _SMHI_LON, hours=3)
        # SMHI has no radiation parameter; irradiance is reconstructed and must
        # be a number (0 at night — these sample hours are around local midnight).
        for row in result:
            assert row["irradiance"] is not None
            assert row["irradiance"] >= 0.0

    def test_forward_fills_coarse_steps(self):
        result = _parse_smhi_forecast(SAMPLE_SMHI_JSON, _SMHI_LAT, _SMHI_LON, hours=6)
        # 00:00 and 01:00 have no explicit sample → forward-fill 23:00's value.
        assert result[2]["temperature"] == 10.5
        assert result[3]["temperature"] == 10.5
        # 02:00 has an explicit sample.
        assert result[4]["temperature"] == 9.0

    def test_empty_series(self):
        assert _parse_smhi_forecast({"timeSeries": []}, _SMHI_LAT, _SMHI_LON) == []
        assert _parse_smhi_forecast({}, _SMHI_LAT, _SMHI_LON) == []

    def test_daytime_clear_sky_has_positive_irradiance(self):
        data = {
            "timeSeries": [
                {
                    "time": "2026-06-21T11:00:00Z",  # ~solar noon, high summer sun
                    "data": {
                        "air_temperature": 20.0,
                        "wind_speed": 2.0,
                        "relative_humidity": 40,
                        "cloud_area_fraction": 0,
                    },
                }
            ]
        }
        result = _parse_smhi_forecast(data, _SMHI_LAT, _SMHI_LON, hours=1)
        assert result[0]["irradiance"] > 0.0


@pytest.fixture
def feed_http(monkeypatch):
    """An in-memory HTTP boundary; no network or provider credentials."""
    from packages.poller import feeds

    client = AsyncMock()
    client.__aenter__.return_value = client
    response = MagicMock()
    client.get.return_value = response
    client.post.return_value = response
    monkeypatch.setattr(feeds.httpx, "AsyncClient", MagicMock(return_value=client))
    return client, response


def _tibber_payload(entries):
    return {
        "data": {
            "viewer": {
                "homes": [
                    {"currentSubscription": {"priceInfo": {"today": entries, "tomorrow": []}}}
                ]
            }
        }
    }


async def test_S3_R16_entsoe_context_survives_settings_change(monkeypatch, feed_http):
    from packages.poller import feeds

    config = {
        "price_provider": "entsoe",
        "entsoe_api_token": "offline-token",
        "entsoe_area": "original-area",
    }
    getter = AsyncMock(side_effect=lambda key: config.get(key, ""))
    monkeypatch.setattr(feeds, "get_setting", getter)
    client, response = feed_http
    response.text = SAMPLE_ENTSOE_XML.replace("EUR", "SEK")
    request_clock = None

    async def request(*args, **kwargs):
        nonlocal request_clock
        request_clock = dt.datetime.now(dt.timezone.utc)
        config.update(price_provider="manual", entsoe_area="replacement-area")
        await asyncio.sleep(0)
        return response

    client.get.side_effect = request
    result = await feeds.fetch_price_feed()

    assert result.source == "entsoe"
    assert result.currency == "SEK"
    assert result.context.provider == "entsoe"
    assert result.context.area == "original-area"
    assert result.context.started_at <= request_clock
    assert result.interval_schema == feeds.PRICE_INTERVAL_SCHEMA
    assert client.get.await_args.kwargs["params"]["in_Domain"] == "original-area"
    assert getter.await_count == 3  # No post-response settings reads.
    with pytest.raises(FrozenInstanceError):
        result.context.area = "replacement-area"


async def test_S3_R16_plain_price_list_is_not_certified(monkeypatch):
    from packages.poller import feeds

    values = [(dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), 0.2)]
    monkeypatch.setattr(feeds, "get_setting", AsyncMock(return_value="entsoe"))
    monkeypatch.setattr(feeds, "_fetch_prices_entsoe", AsyncMock(return_value=values))

    result = await feeds.fetch_price_feed()

    assert result.prices == values
    assert result.currency == ""
    assert result.source == ""
    assert result.context is None
    assert result.interval_schema is None
    assert await feeds.fetch_prices() == values  # Legacy price-only API.


@pytest.mark.parametrize("unit", ["KWH", "", "MW"])
def test_S3_R16_invalid_entsoe_units_have_no_schema(unit):
    result = _parse_entsoe_xml(
        SAMPLE_ENTSOE_XML.replace("MWH", unit),
        dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc),
    )
    assert result == []
    assert result.interval_schema is None


@pytest.mark.parametrize(
    ("change", "count", "certified"),
    [
        ({}, 1, True),
        ({"currency": None}, 1, False),
        ({"currency": "invalid"}, 1, False),
        ({"total": float("nan")}, 0, False),
        ({"total": True}, 0, False),
        ({"startsAt": "2026-10-15T00:15:00Z"}, 0, False),
        ({"startsAt": "2026-10-15T00:00:00"}, 0, False),
    ],
)
async def test_S3_R16_tibber_schema_requires_verifiable_hour(
    monkeypatch, feed_http, change, count, certified
):
    from packages.poller import feeds

    config = {"price_provider": "tibber", "tibber_api_token": "offline-token"}
    monkeypatch.setattr(
        feeds, "get_setting", AsyncMock(side_effect=lambda key: config.get(key, ""))
    )
    entry = {"currency": "SEK", "total": -0.2, "startsAt": "2026-10-15T00:00:00Z"}
    entry.update(change)
    client, response = feed_http
    response.json.return_value = _tibber_payload([entry])

    async def request(*args, **kwargs):
        config["price_provider"] = "manual"
        await asyncio.sleep(0)
        return response

    client.post.side_effect = request
    result = await feeds.fetch_price_feed()

    assert len(result.prices) == count
    assert result.source == "tibber"
    assert result.context.area == "tibber"
    assert (result.interval_schema == feeds.PRICE_INTERVAL_SCHEMA) is certified
    assert (result.rejection_reason is not None) is (count == 0)
    if certified:
        assert result.currency == "SEK"
        assert result.prices[0][1] == -0.2


@pytest.mark.parametrize("invalid", ["duplicate", "mixed_currency", "mixed_currency_reversed"])
async def test_S3_R16_tibber_conflicting_batch_is_not_certified(monkeypatch, feed_http, invalid):
    from packages.poller import feeds

    monkeypatch.setattr(feeds, "get_setting", AsyncMock(return_value="offline-token"))
    first = {"currency": "SEK", "total": -0.2, "startsAt": "2026-10-15T00:00:00Z"}
    second = dict(first)
    if invalid.startswith("mixed_currency"):
        second.update(currency="EUR", startsAt="2026-10-15T01:00:00Z")
    entries = [second, first] if invalid == "mixed_currency_reversed" else [first, second]
    _, response = feed_http
    response.json.return_value = _tibber_payload(entries)

    result = await feeds._fetch_prices_tibber()

    assert result == []
    assert result.interval_schema is None
    assert result.context.provider == "tibber"
    assert result.rejection_reason == (
        "duplicate_timestamp" if invalid == "duplicate" else "currency_conflict"
    )


@pytest.mark.parametrize(
    ("currencies", "expected_currency", "certified"),
    [
        ([None, "SEK"], "SEK", False),
        (["SEK", None], "SEK", False),
        (["invalid", "SEK"], "SEK", False),
        (["SEK", "invalid"], "SEK", False),
        ([None, "invalid"], "", False),
        (["invalid", None], "", False),
        (["sek", "SEK"], "SEK", True),
        (["SEK", "sek"], "SEK", True),
    ],
)
async def test_S3_R16_tibber_partial_currency_metadata_is_order_independent(
    monkeypatch, feed_http, currencies, expected_currency, certified
):
    from packages.poller import feeds

    monkeypatch.setattr(feeds, "get_setting", AsyncMock(return_value="offline-token"))
    entries = [
        {"currency": currency, "total": -0.2, "startsAt": f"2026-10-15T0{hour}:00:00Z"}
        for hour, currency in enumerate(currencies)
    ]
    _, response = feed_http
    response.json.return_value = _tibber_payload(entries)

    result = await feeds._fetch_prices_tibber()

    assert len(result) == 2
    assert [price for _, price in result] == [-0.2, -0.2]
    assert result.currency == expected_currency
    assert result.rejection_reason is None
    assert (result.interval_schema == feeds.PRICE_INTERVAL_SCHEMA) is certified


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "malformed_response"),
        ({"errors": [{"message": "untrusted provider text"}]}, "graphql_error"),
        (_tibber_payload("not-an-entry-list"), "malformed_price_entries"),
        (_tibber_payload(["not-an-entry"]), "malformed_price_entry"),
        (_tibber_payload([]), None),
        ({"data": {"viewer": {"homes": []}}}, None),
    ],
)
async def test_S3_R16_tibber_rejected_and_empty_are_distinct(
    monkeypatch, feed_http, payload, reason
):
    from packages.poller import feeds

    monkeypatch.setattr(feeds, "get_setting", AsyncMock(return_value="offline-token"))
    _, response = feed_http
    response.json.return_value = payload

    result = await feeds._fetch_prices_tibber()

    assert result == []
    assert result.interval_schema is None
    assert result.rejection_reason == reason


@pytest.mark.parametrize("field", ["today", "tomorrow", "currentSubscription", "priceInfo"])
@pytest.mark.parametrize(
    "value",
    [{}, False, 0, "", [], None, "absent"],
    ids=["object", "false", "zero", "empty-string", "array", "null", "absent"],
)
async def test_S3_R16_tibber_original_structure_types(monkeypatch, feed_http, field, value):
    from packages.poller import feeds

    payload = _tibber_payload([])
    home = payload["data"]["viewer"]["homes"][0]
    parent = (
        home
        if field == "currentSubscription"
        else home["currentSubscription"]
        if field == "priceInfo"
        else home["currentSubscription"]["priceInfo"]
    )
    if value == "absent":
        parent.pop(field, None)
    else:
        parent[field] = value
    monkeypatch.setattr(feeds, "get_setting", AsyncMock(return_value="offline-token"))
    _, response = feed_http
    response.json.return_value = payload

    result = await feeds._fetch_prices_tibber()

    list_field = field in ("today", "tomorrow")
    wrong_type = (
        value is not None
        and value != "absent"
        and not isinstance(value, list if list_field else dict)
    )
    expected_reason = (
        ("malformed_price_entries" if list_field else "malformed_response") if wrong_type else None
    )
    assert result == []
    assert result.rejection_reason == expected_reason
    assert result.interval_schema is None


@pytest.mark.parametrize("provider", ["open-meteo", "smhi"])
async def test_S3_R17_weather_context_survives_settings_change(monkeypatch, feed_http, provider):
    from packages.poller import feeds

    config = {"weather_provider": provider, "latitude": "58.5812", "longitude": "16.158"}
    getter = AsyncMock(side_effect=lambda key: config.get(key, ""))
    monkeypatch.setattr(feeds, "get_setting", getter)
    client, response = feed_http
    response.json.return_value = (
        deepcopy(SAMPLE_SMHI_JSON)
        if provider == "smhi"
        else {
            "hourly_units": {
                "temperature_2m": "°C",
                "wind_speed_10m": "m/s",
                "direct_radiation": "W/m²",
                "precipitation": "mm",
            },
            "hourly": {
                "time": ["2026-10-15T00:00"],
                "temperature_2m": [0.0],
                "wind_speed_10m": [2.0],
                "direct_radiation": [0.0],
                "precipitation": [0.0],
            },
        }
    )
    request_clock = None

    async def request(*args, **kwargs):
        nonlocal request_clock
        request_clock = dt.datetime.now(dt.timezone.utc)
        config.update(weather_provider="manual", latitude="-10", longitude="-20")
        await asyncio.sleep(0)
        return response

    client.get.side_effect = request
    result = await feeds.fetch_weather()

    assert isinstance(result, list)
    assert result.context.provider == provider
    assert result.context.latitude == _SMHI_LAT
    assert result.context.longitude == _SMHI_LON
    assert result.context.started_at <= request_clock
    assert all(row["source"] == provider for row in result)
    assert all(row["units_schema"] == feeds.WEATHER_UNITS_SCHEMA for row in result)
    assert getter.await_count == 3
    if provider == "smhi":
        assert "/lon/16.158/lat/58.5812/" in client.get.await_args.args[0]
        assert result[0]["forecast_issued_at"] == dt.datetime(
            2026, 7, 6, 21, tzinfo=dt.timezone.utc
        )
        expected = _parse_smhi_forecast(SAMPLE_SMHI_JSON, _SMHI_LAT, _SMHI_LON)
        assert [row["irradiance"] for row in result] == [row["irradiance"] for row in expected]
    else:
        assert client.get.await_args.kwargs["params"]["latitude"] == _SMHI_LAT
        assert result[0]["forecast_issued_at"] == result.context.started_at


@pytest.mark.parametrize("clock", [None, "not-a-clock", "2026-07-06T21:00:00"])
async def test_S3_R17_smhi_invalid_issue_clock_is_not_upgraded(monkeypatch, feed_http, clock):
    from packages.poller import feeds

    config = {"weather_provider": "smhi", "latitude": "58.5812", "longitude": "16.158"}
    monkeypatch.setattr(
        feeds, "get_setting", AsyncMock(side_effect=lambda key: config.get(key, ""))
    )
    _, response = feed_http
    payload = deepcopy(SAMPLE_SMHI_JSON)
    payload["referenceTime"] = clock
    response.json.return_value = payload

    result = await feeds.fetch_weather()

    assert result
    assert all(row["forecast_issued_at"] is None for row in result)
    assert all(row["units_schema"] is None for row in result)


async def test_S3_R17_plain_weather_list_is_not_certified(monkeypatch, feed_http):
    from packages.poller import feeds

    config = {"weather_provider": "open-meteo", "latitude": "58", "longitude": "16"}
    monkeypatch.setattr(
        feeds, "get_setting", AsyncMock(side_effect=lambda key: config.get(key, ""))
    )
    monkeypatch.setattr(
        feeds,
        "_parse_open_meteo_forecast",
        MagicMock(
            return_value=[
                {
                    "ts": dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                    "temperature": 0.0,
                    "wind_speed": 0.0,
                    "irradiance": 0.0,
                }
            ]
        ),
    )

    result = await feeds.fetch_weather()

    assert result[0]["temperature"] == 0.0
    assert result[0]["units_schema"] is None


@pytest.mark.parametrize("malformed", ["units", "temperature", "wind"])
async def test_S3_R17_malformed_weather_is_not_certified(monkeypatch, feed_http, malformed):
    from packages.poller import feeds

    config = {"weather_provider": "open-meteo", "latitude": "58", "longitude": "16"}
    monkeypatch.setattr(
        feeds, "get_setting", AsyncMock(side_effect=lambda key: config.get(key, ""))
    )
    _, response = feed_http
    payload = {
        "hourly_units": {
            "temperature_2m": "°C",
            "wind_speed_10m": "m/s",
            "direct_radiation": "W/m²",
            "precipitation": "mm",
        },
        "hourly": {
            "time": ["2026-10-15T00:00"],
            "temperature_2m": [0.0],
            "wind_speed_10m": [2.0],
            "direct_radiation": [0.0],
            "precipitation": [0.0],
        },
    }
    if malformed == "units":
        payload["hourly_units"]["wind_speed_10m"] = "knots"
    elif malformed == "temperature":
        payload["hourly"]["temperature_2m"] = [float("nan")]
    else:
        payload["hourly"]["wind_speed_10m"] = ["unknown"]
    response.json.return_value = payload

    result = await feeds.fetch_weather()

    if malformed == "units":
        assert result == []
    else:
        assert len(result) == 1
        assert result[0]["units_schema"] is None

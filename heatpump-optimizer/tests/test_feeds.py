"""Tests for the ENTSO-E price parser."""

import datetime as dt

from packages.poller.feeds import _parse_entsoe_xml, _parse_smhi_forecast


SAMPLE_ENTSOE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
    <TimeSeries>
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

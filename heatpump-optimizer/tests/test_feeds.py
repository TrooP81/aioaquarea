"""Tests for the ENTSO-E price parser."""

import datetime as dt

from packages.poller.feeds import _parse_entsoe_xml


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

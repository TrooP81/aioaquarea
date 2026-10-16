"""Independent P1.6b owner-decision and first-deployment acceptance checks."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core import planning_data_quality as quality
from packages.core.price_scope import tibber_scope
from packages.poller import feeds


def _home(identity, *, active=True, today=None, tomorrow=None):
    return {
        "id": identity,
        "currentSubscription": {
            "priceInfo": {
                "today": today
                if today is not None
                else [{"startsAt": "2026-10-16T00:00:00Z", "currency": "SEK", "total": -0.2}],
                "tomorrow": tomorrow or [],
            }
        }
        if active
        else None,
    }


@pytest.fixture
def provider_boundary(monkeypatch):
    values = {"price_provider": "tibber", "tibber_api_token": "offline-token", "tibber_home_id": ""}
    monkeypatch.setattr(feeds, "get_all_settings", AsyncMock(side_effect=lambda: dict(values)))
    response = MagicMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(feeds.httpx, "AsyncClient", MagicMock(return_value=client))

    async def persist(captured, selected, reason, *, auto_bind=False):
        if values["tibber_home_id"] != captured:
            return False
        if auto_bind:
            values["tibber_home_id"] = selected
        previous = json.loads(values.get("tibber_feed_diagnostic", "{}"))
        values["tibber_feed_diagnostic"] = json.dumps(
            {
                "home_id": selected,
                "reason": reason,
                "binding_method": previous.get("binding_method")
                or ("single_active_subscription" if auto_bind else "explicit"),
            }
        )
        return True

    monkeypatch.setattr(feeds, "record_tibber_binding", AsyncMock(side_effect=persist))
    return values, response, client


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("second_active", [False, True])
async def test_autobound_home_rechecks_eligibility_on_later_poll(
    provider_boundary, reverse, second_active
):
    values, response, _ = provider_boundary
    response.json.return_value = {"data": {"viewer": {"homes": [_home("A")]}}}
    first = await feeds._fetch_prices_tibber()
    assert first.context.area == tibber_scope("A")
    assert values["tibber_home_id"] == "A"
    assert (
        json.loads(values["tibber_feed_diagnostic"])["binding_method"]
        == "single_active_subscription"
    )

    homes = [_home("A"), _home("B", active=second_active)]
    response.json.return_value = {"data": {"viewer": {"homes": homes[::-1] if reverse else homes}}}
    later = await feeds._fetch_prices_tibber()
    assert values["tibber_home_id"] == "A", "An automatic binding must never silently move"
    if second_active:
        assert later == [], "Owner decision (a) requires ambiguous auto-bindings to fail closed"
        assert later.rejection_reason
        assert json.loads(values["tibber_feed_diagnostic"])["reason"]
    else:
        assert later.context.area == tibber_scope("A")
        assert [value for _, value in later] == [-0.2]


@pytest.mark.parametrize("identity", ['a"b\\c', "å雪", "e\u0301", "é", "A" * 1000])
def test_canonical_identity_is_exact_utf8_compact_json(identity):
    encoded = json.dumps(["tibber", identity], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert tibber_scope(identity) == "tb:" + hashlib.sha256(encoded).hexdigest()[:28]
    assert re.fullmatch(r"tb:[0-9a-f]{28}", tibber_scope(identity))
    assert len(tibber_scope(identity)) <= 32
    assert tibber_scope("e\u0301") != tibber_scope("é")


@pytest.mark.parametrize(
    "hour,tomorrow_available,expected_hours,admitted",
    [(12, False, 11, True), (19, False, 4, False), (19, True, 24, True)],
)
async def test_first_deployment_poll_fetches_whole_days_not_one_new_hour_at_a_time(
    provider_boundary, monkeypatch, hour, tomorrow_available, expected_hours, admitted
):
    values, response, client = provider_boundary
    now = dt.datetime(2026, 10, 16, hour, 30, tzinfo=dt.timezone.utc)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(feeds, "dt", SimpleNamespace(datetime=Clock, timezone=dt.timezone))

    def day(offset):
        midnight = now.replace(hour=0, minute=0) + dt.timedelta(days=offset)
        return [
            {
                "startsAt": (midnight + dt.timedelta(hours=h)).isoformat(),
                "currency": "SEK",
                "total": [0.0, -0.2, 0.37][h % 3],
            }
            for h in range(24)
        ]

    response.json.return_value = {
        "data": {
            "viewer": {
                "homes": [_home("A", today=day(0), tomorrow=day(1) if tomorrow_available else [])]
            }
        }
    }
    feed = await feeds._fetch_prices_tibber()
    assert len(feed) == (48 if tomorrow_available else 24)
    assert values["tibber_home_id"] == "A"
    assert client.post.await_args.kwargs["headers"]["Authorization"] == "Bearer offline-token"
    query = client.post.await_args.kwargs["json"]["query"]
    assert "today" in query and "tomorrow" in query
    assert [value for _, value in feed] == [
        entry["total"] for entry in day(0) + (day(1) if tomorrow_available else [])
    ]
    start = now.replace(minute=0) + dt.timedelta(hours=1)
    end = start + dt.timedelta(hours=24)
    batch = quality.PlanningInputBatch(
        now,
        start,
        end,
        feed.context.area,
        "tibber",
        "open-meteo",
        tuple(
            quality.PriceInput(
                ts, value, feed.currency, feed.source, feed.context.started_at, feed.interval_schema
            )
            for ts, value in feed
            if start <= ts < end
        ),
        tuple(
            quality.WeatherInput(
                start + dt.timedelta(hours=h),
                5.0,
                1.0,
                0.0,
                0.0,
                50.0,
                0.0,
                "open-meteo",
                now,
                "weather_c_mps_wm2_mm_v1",
            )
            for h in range(24)
        ),
    )
    result = quality.planning_batch_quality(batch)
    assert result["price"]["contiguous_hours"] == expected_hours
    assert result["control_allowed"] is admitted

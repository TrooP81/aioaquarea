"""Tests for the rules optimizer."""

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from packages.optimizer.rules import RulesOptimizer


@pytest.fixture
def sample_prices():
    """24 hours of sample prices."""
    base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
    # Prices in EUR/kWh: cheap at night, expensive during day
    hourly_prices = [
        0.05, 0.04, 0.03, 0.03, 0.04, 0.06,  # 00-05
        0.08, 0.12, 0.15, 0.18, 0.20, 0.22,  # 06-11
        0.20, 0.18, 0.15, 0.14, 0.16, 0.25,  # 12-17
        0.30, 0.28, 0.20, 0.12, 0.08, 0.06,  # 18-23
    ]
    return [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly_prices)]


@pytest.fixture
def sample_weather():
    """24 hours of sample weather."""
    base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
    temps = [
        3, 2, 1, 0, -1, -1,  # Cold night
        0, 2, 5, 8, 10, 12,  # Warming up
        13, 14, 14, 13, 11, 9,  # Afternoon
        7, 5, 4, 3, 3, 2,  # Evening cool
    ]
    return [(base + dt.timedelta(hours=h), t) for h, t in enumerate(temps)]


class TestRulesOptimizer:
    def test_plan_dhw_picks_cheapest_hours(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        comfort_schedule = {"weekday": [7, 8, 9, 17, 18, 19, 20, 21], "weekend": [8, 9, 10, 11, 17, 18, 19, 20, 21]}
        actions = optimizer._plan_dhw(
            sample_prices, sample_weather, sample_prices[0][0],
            current_tank_temp=42.0, tank_target=50,
            current_outdoor_temp=5.0, comfort_schedule=comfort_schedule,
        )

        # Should have DHW actions (on/off pairs)
        dhw_on_actions = [a for a in actions if a["type"] == "force_dhw_on"]
        assert len(dhw_on_actions) > 0

        # Should pick cheap hours
        for action in dhw_on_actions:
            ts = dt.datetime.fromisoformat(action["ts"])
            # Find corresponding price
            price = next((p for t, p in sample_prices if t == ts), None)
            if price is not None:
                assert price <= 0.15  # Should be a relatively cheap hour

    def test_plan_peak_avoidance(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_peak_avoidance(sample_prices, sample_weather, sample_prices[0][0])

        quiet_on = [a for a in actions if a["type"] == "quiet_mode_on"]
        # Should activate quiet mode during expensive hours
        assert len(quiet_on) > 0

    def test_plan_preheat_before_cold(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_preheat(
            sample_prices, sample_weather, sample_prices[0][0],
            current_indoor_temp=20.0, current_outdoor_temp=5.0, current_water_temp=35.0,
        )

        boost_actions = [a for a in actions if a["type"] == "zone_temp_boost"]
        # Should have pre-heat actions since we have sub-zero temps
        assert len(boost_actions) > 0

    def test_no_plan_without_prices(self, sample_weather):
        optimizer = RulesOptimizer()
        comfort_schedule = {"weekday": [7, 8, 9, 17, 18, 19, 20, 21], "weekend": [8, 9, 10, 11, 17, 18, 19, 20, 21]}
        actions = optimizer._plan_dhw(
            [], sample_weather, dt.datetime.now(dt.timezone.utc),
            current_tank_temp=42.0, tank_target=50,
            current_outdoor_temp=5.0, comfort_schedule=comfort_schedule,
        )
        assert actions == []

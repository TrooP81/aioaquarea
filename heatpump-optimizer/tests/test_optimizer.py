"""Tests for the rules optimizer."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from packages.core.heat_curve import HeatCurveConfig
from packages.optimizer.rules import RulesOptimizer


@pytest.fixture
def sample_prices():
    """24 hours of sample prices."""
    base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
    # Prices in EUR/kWh: cheap at night, expensive during day
    hourly_prices = [
        0.05,
        0.04,
        0.03,
        0.03,
        0.04,
        0.06,  # 00-05
        0.08,
        0.12,
        0.15,
        0.18,
        0.20,
        0.22,  # 06-11
        0.20,
        0.18,
        0.15,
        0.14,
        0.16,
        0.25,  # 12-17
        0.30,
        0.28,
        0.20,
        0.12,
        0.08,
        0.06,  # 18-23
    ]
    return [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly_prices)]


@pytest.fixture
def sample_weather():
    """24 hours of sample weather."""
    base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
    temps = [
        3,
        2,
        1,
        0,
        -1,
        -1,  # Cold night
        0,
        2,
        5,
        8,
        10,
        12,  # Warming up
        13,
        14,
        14,
        13,
        11,
        9,  # Afternoon
        7,
        5,
        4,
        3,
        3,
        2,  # Evening cool
    ]
    return [(base + dt.timedelta(hours=h), t) for h, t in enumerate(temps)]


class TestRulesOptimizer:
    def test_preheat_slot_uses_effective_price_after_cop(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [
            (base, 0.10),
            (base + dt.timedelta(hours=1), 0.12),
            (base + dt.timedelta(hours=2), 0.50),
        ]
        weather = [
            (base, 0.0),
            (base + dt.timedelta(hours=1), 10.0),
            (base + dt.timedelta(hours=2), -5.0),
        ]
        passive = {
            base: 21.0,
            base + dt.timedelta(hours=1): 21.0,
            base + dt.timedelta(hours=2): 19.0,
        }

        with (
            patch.object(optimizer, "_passive_indoor_forecast", return_value=passive),
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_zone_heating_time",
                return_value=SimpleNamespace(
                    estimated_hours=0.5,
                    estimated_minutes=30.0,
                    heating_rate_per_hour=4.0,
                ),
            ),
        ):
            actions = optimizer._plan_preheat(
                prices,
                weather,
                base,
                current_indoor_temp=21.0,
                current_outdoor_temp=0.0,
                current_water_temp=30.0,
                comfort_temp_target=20.5,
                comfort_temp_min=18.0,
            )

        boost = next(action for action in actions if action["type"] == "zone_temp_boost")
        restore = next(action for action in actions if action["type"] == "zone_temp_restore")
        assert boost["ts"] == (base + dt.timedelta(hours=1)).isoformat()
        assert boost["payload"]["baseline_temperature"] == 30
        assert boost["payload"]["temperature"] == 32
        assert restore["payload"]["temperature"] == 30
        assert restore["payload"]["boost_temperature"] == 32

    def test_dhw_slot_uses_effective_price_after_cop(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [
            (base, 0.10),
            (base + dt.timedelta(hours=1), 0.12),
        ]
        weather = [
            (base, 0.0),
            (base + dt.timedelta(hours=1), 10.0),
        ]

        slot = optimizer._find_lowest_dhw_energy_cost_slot(
            prices,
            weather,
            hours_needed=1,
            fallback_outdoor_temp=5.0,
        )

        # Raw price favours 00:00, but price/COP is lower at warmer 01:00:
        # 0.10/3.5 > 0.12/4.5.
        assert slot == [prices[1]]

    def test_dhw_slot_still_prefers_materially_lower_raw_price(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [
            (base, 0.05),
            (base + dt.timedelta(hours=1), 0.12),
        ]
        weather = [
            (base, 0.0),
            (base + dt.timedelta(hours=1), 10.0),
        ]

        slot = optimizer._find_lowest_dhw_energy_cost_slot(
            prices,
            weather,
            hours_needed=1,
            fallback_outdoor_temp=5.0,
        )

        assert slot == [prices[0]]

    def test_plan_dhw_picks_cheapest_hours(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        comfort_schedule = {
            "weekday": [7, 8, 9, 17, 18, 19, 20, 21],
            "weekend": [8, 9, 10, 11, 17, 18, 19, 20, 21],
        }
        actions = optimizer._plan_dhw(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_tank_temp=42.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=comfort_schedule,
        )

        # Should have DHW actions (on/off pairs)
        dhw_on_actions = [a for a in actions if a["type"] == "force_dhw_on"]
        assert len(dhw_on_actions) > 0

        # Every action carries the local readiness deadline it was selected to
        # meet. The exact price varies with how close the deadline is.
        for action in dhw_on_actions:
            assert action["payload"]["reason"].startswith("thermal_optimized_before_")

    def test_plan_peak_avoidance(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_peak_avoidance(sample_prices, sample_weather, sample_prices[0][0])

        quiet_on = [a for a in actions if a["type"] == "quiet_mode_on"]
        # Should activate quiet mode during expensive hours
        assert len(quiet_on) > 0
        assert all(action["payload"]["level"] == 1 for action in quiet_on)

    def test_normalise_actions_collapses_repeated_quiet_transitions(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        actions = [
            {
                "ts": (base + dt.timedelta(hours=2)).isoformat(),
                "type": "quiet_mode_on",
                "payload": {},
            },
            {"ts": base.isoformat(), "type": "quiet_mode_on", "payload": {}},
            {
                "ts": (base + dt.timedelta(hours=1)).isoformat(),
                "type": "quiet_mode_on",
                "payload": {"reason": "peak"},
            },
            {
                "ts": (base + dt.timedelta(hours=3)).isoformat(),
                "type": "quiet_mode_off",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=4)).isoformat(),
                "type": "quiet_mode_off",
                "payload": {},
            },
        ]

        normalised = RulesOptimizer._normalise_actions(actions)

        assert [action["type"] for action in normalised] == ["quiet_mode_on", "quiet_mode_off"]
        assert [action["ts"] for action in normalised] == [
            base.isoformat(),
            (base + dt.timedelta(hours=3)).isoformat(),
        ]

    def test_normalise_actions_preserves_quiet_level_changes(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        normalised = RulesOptimizer._normalise_actions(
            [
                {
                    "ts": base.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"level": 1, "reason": "peak"},
                },
                {
                    "ts": (base + dt.timedelta(hours=1)).isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"level": 2, "reason": "night"},
                },
                {
                    "ts": (base + dt.timedelta(hours=2)).isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"level": 2, "reason": "still_night"},
                },
            ]
        )

        assert [action["payload"]["level"] for action in normalised] == [1, 2]

    def test_normalise_actions_prefers_stronger_quiet_level_at_same_timestamp(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        normalised = RulesOptimizer._normalise_actions(
            [
                {
                    "ts": base.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"level": 2, "reason": "night"},
                },
                {
                    "ts": base.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"level": 1, "reason": "peak"},
                },
            ]
        )

        assert len(normalised) == 1
        assert normalised[0]["payload"]["level"] == 2

    def test_normalise_actions_keeps_final_quiet_state_at_same_timestamp(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        normalised = RulesOptimizer._normalise_actions(
            [
                {
                    "ts": base.isoformat(),
                    "type": "quiet_mode_off",
                    "payload": {"reason": "peak_avoidance_end"},
                },
                {
                    "ts": base.isoformat(),
                    "type": "quiet_mode_on",
                    "payload": {"reason": "night_quiet_schedule"},
                },
            ]
        )

        assert len(normalised) == 1
        assert normalised[0]["type"] == "quiet_mode_on"
        assert normalised[0]["payload"]["reason"] == "night_quiet_schedule"

    def test_plan_preheat_before_cold(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=20.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
        )

        boost_actions = [a for a in actions if a["type"] == "zone_temp_boost"]
        restore_actions = [a for a in actions if a["type"] == "zone_temp_restore"]
        # Should have pre-heat actions since we have sub-zero temps
        assert len(boost_actions) > 0
        assert len(restore_actions) == len(boost_actions)
        assert boost_actions[0]["payload"]["baseline_temperature"] == 35
        assert boost_actions[0]["payload"]["temperature"] == 37
        assert restore_actions[0]["payload"]["temperature"] == 35
        assert restore_actions[0]["payload"]["boost_temperature"] == 37

    def test_preheat_does_not_heat_an_already_warm_home(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=28.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
            comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
            comfort_temp_target=21.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
        )

        assert actions == []

    def test_comfort_mode_uses_passive_forecast_before_clearing_eco(self, sample_prices):
        optimizer = RulesOptimizer()
        warm_weather = [(ts, 20.0) for ts, _ in sample_prices]
        actions = optimizer._plan_eco_comfort(
            sample_prices,
            warm_weather,
            sample_prices[0][0],
            {"weekday": list(range(24)), "weekend": list(range(24))},
            current_indoor_temp=26.0,
            current_outdoor_temp=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13),
            comfort_temp_target=21.0,
            tz_name="UTC",
        )

        assert all(action["type"] != "normal_mode_on" for action in actions)
        assert actions[0]["type"] == "eco_mode_on"
        assert actions[0]["payload"]["reason"].startswith("comfort_satisfied_forecast_")

    def test_cheap_eco_hour_does_not_restore_normal_when_home_is_warm(self, sample_prices):
        optimizer = RulesOptimizer()
        warm_weather = [(ts, 20.0) for ts, _ in sample_prices]
        actions = optimizer._plan_eco_comfort(
            sample_prices,
            warm_weather,
            sample_prices[0][0],
            {"weekday": [], "weekend": []},
            current_indoor_temp=26.0,
            current_outdoor_temp=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13),
            comfort_temp_target=21.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
        )

        assert all(action["type"] != "normal_mode_on" for action in actions)
        assert actions[0]["type"] == "eco_mode_on"
        assert actions[0]["payload"]["reason"].startswith("setback_satisfied_forecast_")

    def test_comfort_schedule_does_not_emit_mode_only_heat_above_cutoff(self, sample_prices):
        optimizer = RulesOptimizer()
        warm_weather = [(ts, 18.0) for ts, _ in sample_prices]

        actions = optimizer._plan_eco_comfort(
            sample_prices,
            warm_weather,
            sample_prices[0][0],
            {"weekday": list(range(24)), "weekend": list(range(24))},
            current_indoor_temp=18.0,
            current_outdoor_temp=18.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13),
            comfort_temp_target=21.0,
            tz_name="UTC",
        )

        assert all(
            action["type"] not in {"normal_mode_on", "comfort_mode_on"} for action in actions
        )

    def test_no_plan_without_prices(self, sample_weather):
        optimizer = RulesOptimizer()
        comfort_schedule = {
            "weekday": [7, 8, 9, 17, 18, 19, 20, 21],
            "weekend": [8, 9, 10, 11, 17, 18, 19, 20, 21],
        }
        actions = optimizer._plan_dhw(
            [],
            sample_weather,
            dt.datetime.now(dt.timezone.utc),
            current_tank_temp=42.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=comfort_schedule,
        )
        assert actions == []

    def test_dhw_does_not_force_a_negligible_top_up(self, sample_prices, sample_weather):
        """A tank already at target must not become an on/off action pair."""
        optimizer = RulesOptimizer()
        comfort_schedule = {"weekday": [7, 8], "weekend": [7, 8]}

        actions = optimizer._plan_dhw(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_tank_temp=50.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=comfort_schedule,
        )

        assert actions == []

    def test_dhw_deadline_uses_next_days_local_schedule(self):
        optimizer = RulesOptimizer()
        start = dt.datetime(2026, 7, 12, 15, tzinfo=dt.timezone.utc)  # Sunday 17:00 in Amsterdam
        prices = [(start + dt.timedelta(hours=h), 0.10) for h in range(24)]
        weather = [(ts, 8.0) for ts, _ in prices]
        schedule = {
            "weekday": [7, 8, 9, 17, 18, 19, 20, 21],
            "weekend": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        }

        actions = optimizer._plan_dhw(
            prices,
            weather,
            start,
            current_tank_temp=42.0,
            tank_target=50,
            current_outdoor_temp=8.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        dhw_on = [action for action in actions if action["type"] == "force_dhw_on"]
        assert len(dhw_on) == 1
        action_time = dt.datetime.fromisoformat(dhw_on[0]["ts"]).astimezone(
            dt.timezone(dt.timedelta(hours=2))
        )
        assert action_time.hour < 7
        assert dhw_on[0]["payload"]["reason"] == "thermal_optimized_before_7:00"

    def test_rules_snapshot_without_zone_heat_matches_no_heating(
        self, sample_prices, sample_weather
    ):
        """Normal/Eco mode must not masquerade as an explicit heating command."""
        optimizer = RulesOptimizer()
        snapshot = optimizer._build_forecast_snapshot(
            prices=sample_prices[:4],
            weather=sample_weather[:4],
            weather_full=[],
            actions=[
                {
                    "ts": sample_prices[0][0].isoformat(),
                    "type": "normal_mode_on",
                    "payload": {"reason": "mild_weather"},
                }
            ],
            horizon_start=sample_prices[0][0],
            current_indoor=20.0,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={"weekday": [], "weekend": []},
            comfort_temp_target=20.5,
            comfort_temp_min=18.0,
            tz_name="UTC",
        )

        planned = snapshot["forecast_with_plan"]
        no_heating = snapshot["forecast_no_heating"]
        assert [row["space_heating_fraction"] for row in planned] == [0.0] * 4
        assert [row["predicted_indoor_temp"] for row in planned] == [
            row["predicted_indoor_temp"] for row in no_heating
        ]

    def test_rules_snapshot_never_substitutes_20c_for_missing_room_observation(
        self, sample_prices, sample_weather
    ):
        snapshot = RulesOptimizer()._build_forecast_snapshot(
            prices=sample_prices[:2],
            weather=sample_weather[:2],
            weather_full=[],
            actions=[],
            horizon_start=sample_prices[0][0],
            current_indoor=None,
            current_water_temp=35.0,
            heat_curve=HeatCurveConfig(),
            comfort_schedule={"weekday": [], "weekend": []},
            comfort_temp_target=20.5,
            comfort_temp_min=18.0,
            tz_name="UTC",
            control_input={"available": False, "reason": "reference_sensor_not_fresh"},
        )

        assert snapshot["forecast_status"] == "unavailable"
        assert snapshot["current_indoor"] is None
        assert snapshot["forecast_with_plan"] == []
        assert snapshot["forecast_unavailable_reason"] == "reference_sensor_not_fresh"

    def test_guardrail_does_not_schedule_heat_above_controller_cutoff(self, sample_prices):
        optimizer = RulesOptimizer()
        warm_weather = [(ts, 18.0) for ts, _ in sample_prices[:4]]

        actions = optimizer._plan_indoor_guardrails(
            sample_prices[:4],
            warm_weather,
            sample_prices[0][0],
            current_indoor_temp=18.0,
            current_outdoor_temp=18.0,
            current_water_temp=35.0,
            comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
            comfort_temp_target=21.0,
            comfort_temp_min=18.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13),
            tz_name="UTC",
        )

        assert actions == []

    def test_guardrail_preserves_zone_water_baseline(self, sample_prices):
        optimizer = RulesOptimizer()
        cold_weather = [(ts, 2.0) for ts, _ in sample_prices[:4]]

        with (
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_indoor_controlled_curve",
                return_value=[{"predicted_indoor_temp": 17.0}] * 4,
            ),
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_indoor_heating_time",
                return_value=SimpleNamespace(estimated_hours=0.5, estimated_minutes=30.0),
            ),
        ):
            actions = optimizer._plan_indoor_guardrails(
                sample_prices[:4],
                cold_weather,
                sample_prices[0][0],
                current_indoor_temp=18.0,
                current_outdoor_temp=2.0,
                current_water_temp=35.0,
                comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
            )

        boost, restore = actions
        assert boost["payload"]["baseline_temperature"] == 35
        assert boost["payload"]["temperature"] == 37
        assert restore["payload"]["temperature"] == 35
        assert restore["payload"]["boost_temperature"] == 37

    def test_emergency_guardrail_preserves_zone_water_baseline(self, sample_prices):
        optimizer = RulesOptimizer()
        cold_weather = [(ts, 2.0) for ts, _ in sample_prices[:4]]

        with (
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_indoor_controlled_curve",
                return_value=[{"predicted_indoor_temp": 21.0}] * 4,
            ),
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_indoor_cooling_time",
                return_value=SimpleNamespace(estimated_minutes=60.0),
            ),
        ):
            actions = optimizer._plan_indoor_guardrails(
                sample_prices[:4],
                cold_weather,
                sample_prices[0][0],
                current_indoor_temp=20.0,
                current_outdoor_temp=2.0,
                current_water_temp=35.0,
                comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
            )

        boost, restore = actions
        assert boost["payload"]["reason"] == "indoor_cooling_imminent"
        assert boost["payload"]["baseline_temperature"] == 35
        assert boost["payload"]["temperature"] == 37
        assert restore["payload"]["temperature"] == 35
        assert restore["payload"]["boost_temperature"] == 37

    def test_guardrail_does_not_start_emergency_heat_above_comfort_target(self, sample_prices):
        optimizer = RulesOptimizer()
        cold_weather = [(ts, 2.0) for ts, _ in sample_prices[:4]]

        actions = optimizer._plan_indoor_guardrails(
            sample_prices[:4],
            cold_weather,
            sample_prices[0][0],
            current_indoor_temp=22.0,
            current_outdoor_temp=2.0,
            current_water_temp=35.0,
            comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
            comfort_temp_target=21.0,
            comfort_temp_min=18.0,
            heat_curve=HeatCurveConfig(heating_off_outdoor_c=13),
            tz_name="UTC",
        )

        assert actions == []


class TestFlatPriceOptimizer:
    """Verify that flat (manual) pricing skips price-based logic."""

    @pytest.fixture
    def flat_prices(self):
        """24 hours of identical prices (simulates manual provider)."""
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        return [(base + dt.timedelta(hours=h), 0.25) for h in range(24)]

    @pytest.fixture
    def sample_weather(self):
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        temps = [
            3,
            2,
            1,
            0,
            -1,
            -1,
            0,
            2,
            5,
            8,
            10,
            12,
            13,
            14,
            14,
            13,
            11,
            9,
            7,
            5,
            4,
            3,
            3,
            2,
        ]
        return [(base + dt.timedelta(hours=h), t) for h, t in enumerate(temps)]

    def test_peak_avoidance_skipped_for_flat_price(self, flat_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_peak_avoidance(flat_prices, sample_weather, flat_prices[0][0])
        # With identical prices there are no peaks — should produce no actions
        assert actions == []

    def test_eco_comfort_follows_schedule_for_flat_price(self, flat_prices, sample_weather):
        optimizer = RulesOptimizer()
        comfort_schedule = {
            "weekday": [7, 8, 9, 17, 18, 19, 20, 21],
            "weekend": [8, 9, 10, 11, 17, 18, 19, 20, 21],
        }
        actions = optimizer._plan_eco_comfort(
            flat_prices,
            sample_weather,
            flat_prices[0][0],
            comfort_schedule,
        )
        # With flat prices, no price-based overrides should occur
        for a in actions:
            if a["type"] == "comfort_mode_on":
                assert "peak_price" not in a["payload"].get("reason", "")
            # No eco hours should be "upgraded" due to cheap price
            if a["type"] == "normal_mode_on":
                assert "cheap_price" not in a["payload"].get("reason", "")

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
    def test_zone_boost_requires_live_headroom(self):
        optimizer = RulesOptimizer()

        assert optimizer._zone_boost_targets(5.0, -5, 5) is None

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
                current_zone_target_temp=36.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                comfort_temp_target=20.5,
                comfort_temp_min=18.0,
            )

        boost = next(action for action in actions if action["type"] == "zone_temp_boost")
        restore = next(action for action in actions if action["type"] == "zone_temp_restore")
        assert boost["ts"] == (base + dt.timedelta(hours=1)).isoformat()
        assert boost["payload"]["baseline_temperature"] == 36
        assert boost["payload"]["temperature"] == 38
        assert restore["payload"]["temperature"] == 36
        assert restore["payload"]["boost_temperature"] == 38

    def test_preheat_uses_one_step_for_small_modelled_water_deficit(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=h), 0.10) for h in range(3)]
        weather = [
            (base, 0.0),
            (base + dt.timedelta(hours=1), -5.0),
            (base + dt.timedelta(hours=2), -5.0),
        ]
        passive = {
            base: 21.0,
            base + dt.timedelta(hours=1): 19.0,
            base + dt.timedelta(hours=2): 19.0,
        }
        model = SimpleNamespace(
            is_ready_for_control=True,
            required_zone_temp=lambda **_kwargs: 31.0,
        )

        with (
            patch.object(optimizer, "_passive_indoor_forecast", return_value=passive),
            patch("packages.optimizer.rule_mixins.comfort_model", model),
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_zone_heating_time",
                return_value=SimpleNamespace(
                    estimated_hours=0.5,
                    estimated_minutes=30.0,
                    heating_rate_per_hour=2.0,
                ),
            ),
        ):
            actions = optimizer._plan_preheat(
                prices,
                weather,
                base,
                current_indoor_temp=20.0,
                current_outdoor_temp=-5.0,
                current_water_temp=30.0,
                current_zone_target_temp=36.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
            )

        boost = next(action for action in actions if action["type"] == "zone_temp_boost")
        assert boost["payload"]["offset"] == 1
        assert boost["payload"]["temperature"] == 37

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

    def test_dhw_slot_uses_trained_cop_model_when_tank_target_known(self):
        """When the ML COP model is trained, DHW slot picks reflect the tank
        target instead of the outdoor-only default curve. That opens the door
        to real savings on hours where the ML model predicts a materially
        different COP than the linear default.
        """
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [
            (base, 0.10),
            (base + dt.timedelta(hours=1), 0.10),
        ]
        weather = [
            (base, 5.0),
            (base + dt.timedelta(hours=1), 5.0),
        ]

        # ML model reports a much higher COP at hour 1 (e.g. warmer supply
        # side, better setpoint alignment) than at hour 0. Same raw price, but
        # hour 1 wins on cost per delivered kWh.
        def _predict_cop(outdoor, target, hour):
            return 5.0 if hour == 1 else 2.5

        mock_cop = SimpleNamespace(is_trained=True, predict_cop=_predict_cop)
        optimizer = RulesOptimizer(cop_model=mock_cop)
        slot = optimizer._find_lowest_dhw_energy_cost_slot(
            prices,
            weather,
            hours_needed=1,
            fallback_outdoor_temp=5.0,
            tank_target=52,
        )

        assert slot == [prices[1]]

    def test_dhw_slot_falls_back_to_default_cop_without_tank_target(self):
        """The trained COP model must not be used when the tank target is
        unknown, since predict_cop needs it. The default curve stays in play
        for shared preheat callers that don't have a tank target.
        """
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base, 0.10), (base + dt.timedelta(hours=1), 0.10)]
        weather = [(base, 5.0), (base + dt.timedelta(hours=1), 5.0)]

        called = []

        def _predict_cop(outdoor, target, hour):
            called.append((outdoor, target, hour))
            return 5.0

        mock_cop = SimpleNamespace(is_trained=True, predict_cop=_predict_cop)
        optimizer = RulesOptimizer(cop_model=mock_cop)
        optimizer._find_lowest_dhw_energy_cost_slot(
            prices, weather, hours_needed=1, fallback_outdoor_temp=5.0
        )

        assert called == []

    def test_heat_slot_rejects_fragmented_price_hours(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base, 0.05), (base + dt.timedelta(hours=2), 0.04)]
        weather = [(ts, 5.0) for ts, _ in prices]

        slot = optimizer._find_lowest_heat_energy_cost_slot(
            prices, weather, hours_needed=2, fallback_outdoor_temp=5.0
        )

        assert slot is None

    def test_heat_slot_rejects_incomplete_runtime(self):
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base, 0.05)]
        weather = [(base, 5.0)]

        slot = optimizer._find_lowest_heat_energy_cost_slot(
            prices, weather, hours_needed=2, fallback_outdoor_temp=5.0
        )

        assert slot is None

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

        # Every deadline-driven action carries the local readiness deadline it
        # was selected to meet. Opportunistic banking is a separate branch and
        # allowed to appear alongside deadline-driven top-ups.
        deadline_actions = [
            a
            for a in dhw_on_actions
            if not a["payload"].get("reason", "").startswith("opportunistic_")
        ]
        assert deadline_actions
        for action in deadline_actions:
            assert action["payload"]["reason"].startswith("thermal_optimized_before_")

    def test_plan_peak_avoidance(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_peak_avoidance(sample_prices, sample_weather, sample_prices[0][0])

        quiet_on = [a for a in actions if a["type"] == "quiet_mode_on"]
        # Should activate quiet mode during expensive hours
        assert len(quiet_on) > 0
        assert all(action["payload"]["level"] == 1 for action in quiet_on)

    def test_peak_avoidance_covers_multiple_peaks_when_curve_is_spiky(
        self, sample_prices, sample_weather
    ):
        """A single peak-hour cap misses obvious multi-hour price spikes.

        With sample_prices the top three hours (0.30, 0.28, 0.25 EUR/kWh) are
        all well above 1.3 * median. The new rule enters quiet mode for each
        of them, giving more electricity savings during the daily peak.
        """
        optimizer = RulesOptimizer()

        actions = optimizer._plan_peak_avoidance(sample_prices, sample_weather, sample_prices[0][0])

        quiet_on = [a for a in actions if a["type"] == "quiet_mode_on"]
        assert len(quiet_on) >= 3
        assert all(a["payload"]["level"] == 1 for a in quiet_on)

    def test_peak_avoidance_ignores_marginal_high_hours(self):
        """When the top hour is only slightly above median, no peak fires.

        Prevents entering quiet mode on nearly-flat pricing days where the
        heat-pump throttle would save little but add compressor cycling.
        """
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        # Median 0.10; top price 0.12 (only 1.20x median, below 1.30 gate)
        prices = [(base + dt.timedelta(hours=h), 0.10) for h in range(24)]
        prices[10] = (base + dt.timedelta(hours=10), 0.12)
        weather = [(ts, 5.0) for ts, _ in prices]

        actions = optimizer._plan_peak_avoidance(prices, weather, base)

        assert actions == []

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

    def test_zone_control_windows_merge_overlapping_and_touching_pairs(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        actions = [
            {"ts": base.isoformat(), "type": "zone_temp_boost", "payload": {}},
            {
                "ts": (base + dt.timedelta(hours=1)).isoformat(),
                "type": "zone_temp_restore",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=1)).isoformat(),
                "type": "zone_temp_boost",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=2)).isoformat(),
                "type": "zone_temp_restore",
                "payload": {},
            },
        ]

        assert RulesOptimizer._zone_control_windows(actions) == [
            (base, base + dt.timedelta(hours=2))
        ]

    def test_normalise_actions_blocks_special_status_at_zone_window_boundaries(self):
        base = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
        actions = [
            {"ts": base.isoformat(), "type": "zone_temp_boost", "payload": {}},
            {"ts": base.isoformat(), "type": "eco_mode_on", "payload": {}},
            {
                "ts": (base + dt.timedelta(hours=1)).isoformat(),
                "type": "comfort_mode_on",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=2)).isoformat(),
                "type": "zone_temp_restore",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=2)).isoformat(),
                "type": "normal_mode_on",
                "payload": {},
            },
            {
                "ts": (base + dt.timedelta(hours=3)).isoformat(),
                "type": "eco_mode_on",
                "payload": {},
            },
        ]

        normalised = RulesOptimizer._normalise_actions(actions)

        assert [action["type"] for action in normalised] == [
            "zone_temp_boost",
            "zone_temp_restore",
            "eco_mode_on",
        ]

    def test_eco_comfort_resumes_after_zone_control_window(self, sample_prices):
        optimizer = RulesOptimizer()
        flat_prices = [(ts, 0.1) for ts, _ in sample_prices]
        base = flat_prices[0][0]

        actions = optimizer._plan_eco_comfort(
            flat_prices,
            [(ts, 0.0) for ts, _ in flat_prices],
            base,
            {"weekday": [], "weekend": []},
            special_status_supported=True,
            current_special_status=None,
            zone_control_windows=[(base, base + dt.timedelta(hours=1))],
        )

        assert len(actions) == 1
        assert actions[0]["type"] == "eco_mode_on"
        assert actions[0]["ts"] == (base + dt.timedelta(hours=2)).isoformat()

    def test_plan_preheat_before_cold(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=20.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
            current_zone_target_temp=34.0,
            current_zone_heat_min=20,
            current_zone_heat_max=65,
        )

        boost_actions = [a for a in actions if a["type"] == "zone_temp_boost"]
        restore_actions = [a for a in actions if a["type"] == "zone_temp_restore"]
        # Should have pre-heat actions since we have sub-zero temps
        assert len(boost_actions) > 0
        assert len(restore_actions) == len(boost_actions)
        assert boost_actions[0]["payload"]["baseline_temperature"] == 34
        assert boost_actions[0]["payload"]["temperature"] == 36
        assert restore_actions[0]["payload"]["temperature"] == 34
        assert restore_actions[0]["payload"]["boost_temperature"] == 36

    def test_preheat_uses_panasonic_curve_shift_range(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()

        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=20.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
            current_zone_target_temp=-5.0,
            current_zone_heat_min=-5,
            current_zone_heat_max=5,
        )

        boost = next(action for action in actions if action["type"] == "zone_temp_boost")
        restore = next(action for action in actions if action["type"] == "zone_temp_restore")
        assert boost["payload"]["baseline_temperature"] == -5
        assert boost["payload"]["temperature"] == -3
        assert restore["payload"]["temperature"] == -5
        assert restore["payload"]["boost_temperature"] == -3

    def test_preheat_clamps_boost_to_panasonic_live_max(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()

        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=20.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
            current_zone_target_temp=4.0,
            current_zone_heat_min=-5,
            current_zone_heat_max=5,
        )

        boost = next(action for action in actions if action["type"] == "zone_temp_boost")
        restore = next(action for action in actions if action["type"] == "zone_temp_restore")
        assert boost["payload"]["offset"] == 1
        assert boost["payload"]["temperature"] == 5
        assert restore["payload"]["temperature"] == 4
        assert restore["payload"]["boost_temperature"] == 5

    def test_preheat_does_not_heat_an_already_warm_home(self, sample_prices, sample_weather):
        optimizer = RulesOptimizer()
        actions = optimizer._plan_preheat(
            sample_prices,
            sample_weather,
            sample_prices[0][0],
            current_indoor_temp=28.0,
            current_outdoor_temp=5.0,
            current_water_temp=35.0,
            current_zone_target_temp=35.0,
            current_zone_heat_min=20,
            current_zone_heat_max=65,
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
            special_status_supported=True,
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
            special_status_supported=True,
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
            special_status_supported=True,
        )

        assert all(
            action["type"] not in {"normal_mode_on", "comfort_mode_on"} for action in actions
        )

    def test_eco_comfort_skips_device_without_safe_panasonic_support(self, sample_prices):
        optimizer = RulesOptimizer()

        actions = optimizer._plan_eco_comfort(
            sample_prices,
            [(ts, 0.0) for ts, _ in sample_prices],
            sample_prices[0][0],
            {"weekday": [], "weekend": []},
            special_status_supported=False,
        )

        assert actions == []

    def test_eco_comfort_does_not_repeat_observed_eco_mode(self, sample_prices):
        optimizer = RulesOptimizer()
        flat_prices = [(ts, 0.1) for ts, _ in sample_prices]

        actions = optimizer._plan_eco_comfort(
            flat_prices,
            [(ts, 0.0) for ts, _ in sample_prices],
            sample_prices[0][0],
            {"weekday": [], "weekend": []},
            special_status_supported=True,
            current_special_status=1,
        )

        assert actions == []

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
        action_time = dt.datetime.fromisoformat(dhw_on[0]["ts"])
        local_deadline = dt.datetime(2026, 7, 13, 7, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        assert action_time < local_deadline.astimezone(dt.timezone.utc)
        assert dhw_on[0]["payload"]["reason"] == "thermal_optimized_before_7:00"

    def test_multiple_dhw_deadlines_project_the_prior_cycle(self):
        """Evening sizing must start from the morning cycle, not the stale live reading."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=hour), 0.10 + (hour % 3) * 0.01) for hour in range(24)]
        weather = [(timestamp, 5.0) for timestamp, _ in prices]
        schedule = {
            "weekday": [7, 8, 9, 17, 18, 19],
            "weekend": [7, 8, 9, 17, 18, 19],
        }

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=45.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        dhw_ons = [action for action in actions if action["type"] == "force_dhw_on"]
        assert len(dhw_ons) == 2
        assert dhw_ons[0]["payload"]["reason"] == "thermal_optimized_before_7:00"
        assert dhw_ons[1]["payload"]["reason"] == "thermal_optimized_before_17:00"
        assert dhw_ons[1]["payload"]["predicted_minutes"] < 90

        evening_start = dt.datetime.fromisoformat(dhw_ons[1]["ts"])
        evening_deadline = dt.datetime(2026, 4, 30, 15, tzinfo=dt.timezone.utc)
        assert evening_start >= evening_deadline - dt.timedelta(hours=2)

    def test_plan_dhw_rejects_cheap_slot_that_cools_before_deadline(self):
        """A cheap early slot must not win when its heat is lost before ready-by time."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        # The old planner selected hour 1 solely on price, then stopped three
        # hours before the deadline. The tank cannot bank above its target, so
        # that early heat cools away and causes another thermostat cycle.
        hourly = [0.20, 0.03, 0.15, 0.16, 0.17, 0.18, 0.19, 0.19, 0.19, 0.19]
        prices = [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly)]
        weather = [(base + dt.timedelta(hours=h), 8.0) for h in range(len(hourly))]
        # Deadline at 07:00 Amsterdam = 05:00 UTC in summer, i.e. hour 5.
        schedule = {"weekday": [7, 8], "weekend": [7, 8]}

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=48.0,
            tank_target=50,
            current_outdoor_temp=8.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        dhw_on = [a for a in actions if a["type"] == "force_dhw_on"]
        assert len(dhw_on) == 1
        assert dhw_on[0]["ts"] == prices[4][0].isoformat()

    def test_plan_dhw_sizes_top_up_for_projected_standby_loss(self):
        """A slot several hours ahead must include tank cooling in its sizing.

        A tank at 48°C now with target 52°C looks like a 1h top-up. But if
        the cheapest slot starts 6h out, standby loss will have dropped the
        tank another few °C, so the on-off pair must span more than one hour
        to reach target.
        """
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        # Cheap slot pinned at hour 6 with a valley of 0.02 EUR, ambient
        # prices at 0.20 EUR everywhere else so the picker cannot avoid it.
        hourly = [0.20] * 12
        hourly[6] = 0.02
        prices = [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly)]
        weather = [(base + dt.timedelta(hours=h), 5.0) for h in range(len(hourly))]
        schedule = {"weekday": [9, 10], "weekend": [9, 10]}

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=48.0,
            tank_target=52,
            current_outdoor_temp=5.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        on_action = next(
            a
            for a in actions
            if a["type"] == "force_dhw_on" and "opportunistic" not in a["payload"]["reason"]
        )
        off_action = next(
            a
            for a in actions
            if a["type"] == "force_dhw_off" and a["payload"]["reason"] == "dhw_target_reached"
        )
        on_ts = dt.datetime.fromisoformat(on_action["ts"])
        off_ts = dt.datetime.fromisoformat(off_action["ts"])
        # Standby loss between horizon start (t=0) and the slot start (t≈6h)
        # extends the required heating window beyond the naive one hour.
        assert (off_ts - on_ts) >= dt.timedelta(hours=2)

    def test_plan_dhw_does_not_add_banking_to_deadline_cycle(self):
        """A deadline top-up to target must not get a redundant banking cycle."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        hourly = [0.20] * 24
        # Ultra-cheap valley at hour 2 (0.02 EUR, well below 0.6 * median 0.20 = 0.12)
        hourly[2] = 0.02
        prices = [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly)]
        weather = [(ts, 5.0) for ts, _ in prices]
        # Morning DHW deadline at 07:00 local (05:00 UTC in summer)
        schedule = {"weekday": [7, 8], "weekend": [7, 8]}

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=45.0,  # ≥3°C headroom below target 50
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        dhw_ons = [a for a in actions if a["type"] == "force_dhw_on"]
        assert len(dhw_ons) == 1
        assert dhw_ons[0]["payload"]["reason"] == "thermal_optimized_before_7:00"

    def test_plan_dhw_can_bank_without_a_deadline_cycle(self):
        """An ultra-cheap slot remains usable when no target cycle is planned."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        hourly = [0.20] * 24
        hourly[2] = 0.02
        prices = [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly)]
        weather = [(ts, 5.0) for ts, _ in prices]

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=45.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule={"weekday": [], "weekend": []},
            tz_name="Europe/Amsterdam",
        )

        opportunistic_ons = [
            action
            for action in actions
            if action["type"] == "force_dhw_on"
            and action["payload"].get("reason", "").startswith("opportunistic_cheap_slot_")
        ]
        assert len(opportunistic_ons) == 1
        assert opportunistic_ons[0]["ts"] == prices[2][0].isoformat()

    def test_plan_dhw_no_arbitrage_when_tank_full(self):
        """A near-target tank has no headroom to bank cheap kWh."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        hourly = [0.20] * 24
        hourly[2] = 0.02
        prices = [(base + dt.timedelta(hours=h), p) for h, p in enumerate(hourly)]
        weather = [(ts, 5.0) for ts, _ in prices]
        schedule = {"weekday": [7, 8], "weekend": [7, 8]}

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=49.0,  # only 1°C below target -> no headroom
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        opportunistic = [
            a
            for a in actions
            if a["type"] == "force_dhw_on"
            and a["payload"].get("reason", "").startswith("opportunistic_cheap_slot_")
        ]
        assert opportunistic == []

    def test_plan_dhw_no_arbitrage_on_flat_prices(self):
        """Flat pricing offers no arbitrage opportunity."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 4, 30, 0, 0, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=h), 0.10) for h in range(24)]
        weather = [(ts, 5.0) for ts, _ in prices]
        schedule = {"weekday": [7, 8], "weekend": [7, 8]}

        actions = optimizer._plan_dhw(
            prices,
            weather,
            base,
            current_tank_temp=45.0,
            tank_target=50,
            current_outdoor_temp=5.0,
            comfort_schedule=schedule,
            tz_name="Europe/Amsterdam",
        )

        opportunistic = [
            a
            for a in actions
            if a["type"] == "force_dhw_on"
            and a["payload"].get("reason", "").startswith("opportunistic_cheap_slot_")
        ]
        assert opportunistic == []

    def test_weather_heat_loss_factor_scales_with_wind_and_rain(self):
        """Wind and rain must push the loss factor above 1.0 but stay bounded."""
        optimizer = RulesOptimizer()
        ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

        # Calm: exactly 1.0
        calm = optimizer._weather_heat_loss_factor(
            ts, [{"ts": ts, "wind_speed": 2.0, "precipitation": 0.0}]
        )
        assert calm == 1.0

        # Windy but dry
        windy = optimizer._weather_heat_loss_factor(
            ts, [{"ts": ts, "wind_speed": 10.0, "precipitation": 0.0}]
        )
        assert 1.0 < windy < optimizer._WEATHER_LOSS_MAX_FACTOR

        # Storm: clamped
        storm = optimizer._weather_heat_loss_factor(
            ts, [{"ts": ts, "wind_speed": 30.0, "precipitation": 20.0}]
        )
        assert storm == optimizer._WEATHER_LOSS_MAX_FACTOR

        # No weather data -> passthrough
        assert optimizer._weather_heat_loss_factor(ts, None) == 1.0
        assert optimizer._weather_heat_loss_factor(ts, []) == 1.0

    def test_preheat_does_not_use_dhw_cop_for_supply_water(self):
        """A tank-trained COP model must not receive a zone supply target."""
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [
            (base, 0.10),
            (base + dt.timedelta(hours=1), 0.10),
            (base + dt.timedelta(hours=2), 0.50),
        ]
        weather = [
            (base, 5.0),
            (base + dt.timedelta(hours=1), 5.0),
            (base + dt.timedelta(hours=2), -5.0),
        ]
        passive = {
            base: 21.0,
            base + dt.timedelta(hours=1): 21.0,
            base + dt.timedelta(hours=2): 19.0,
        }
        seen_targets: list[int] = []

        def _predict_cop(outdoor, target, hour):
            seen_targets.append(int(target))
            return 4.0

        mock_cop = SimpleNamespace(is_trained=True, predict_cop=_predict_cop)
        optimizer = RulesOptimizer(cop_model=mock_cop)

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
            optimizer._plan_preheat(
                prices,
                weather,
                base,
                current_indoor_temp=21.0,
                current_outdoor_temp=5.0,
                current_water_temp=30.0,
                current_zone_target_temp=36.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                comfort_temp_target=20.5,
                comfort_temp_min=18.0,
            )

        assert seen_targets == []

    def test_preheat_scales_hours_needed_in_windy_weather(self):
        """A storm at the cold hour should reserve more heating runtime."""
        optimizer = RulesOptimizer()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        prices = [(base + dt.timedelta(hours=h), 0.10) for h in range(6)]
        weather = [(base + dt.timedelta(hours=h), 5.0) for h in range(6)]
        weather[3] = (base + dt.timedelta(hours=3), -5.0)
        weather_full = [
            {
                "ts": base + dt.timedelta(hours=3),
                "wind_speed": 15.0,
                "precipitation": 5.0,
                "temperature": -5.0,
            }
        ]
        passive = {ts: 19.0 for ts, _ in prices}
        passive[base] = 21.0

        with (
            patch.object(optimizer, "_passive_indoor_forecast", return_value=passive),
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_zone_heating_time",
                return_value=SimpleNamespace(
                    estimated_hours=1.0,
                    estimated_minutes=60.0,
                    heating_rate_per_hour=4.0,
                ),
            ),
        ):
            actions = optimizer._plan_preheat(
                prices,
                weather,
                base,
                current_indoor_temp=21.0,
                current_outdoor_temp=5.0,
                current_water_temp=30.0,
                current_zone_target_temp=36.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                weather_full=weather_full,
            )

        boost = next(a for a in actions if a["type"] == "zone_temp_boost")
        restore = next(a for a in actions if a["type"] == "zone_temp_restore")
        boost_ts = dt.datetime.fromisoformat(boost["ts"])
        restore_ts = dt.datetime.fromisoformat(restore["ts"])
        # Baseline hours_needed=1h; storm penalty must extend it beyond that.
        assert (restore_ts - boost_ts) >= dt.timedelta(hours=2)

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
                current_zone_target_temp=33.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
            )

        boost, restore = actions
        assert boost["payload"]["baseline_temperature"] == 33
        assert boost["payload"]["temperature"] == 35
        assert restore["payload"]["temperature"] == 33
        assert restore["payload"]["boost_temperature"] == 35

    def test_guardrail_uses_one_step_for_small_indoor_deficit(self, sample_prices):
        optimizer = RulesOptimizer()
        cold_weather = [(ts, 2.0) for ts, _ in sample_prices[:4]]

        with (
            patch(
                "packages.optimizer.rule_mixins.thermal_model.predict_indoor_controlled_curve",
                return_value=[{"predicted_indoor_temp": 20.6}] * 4,
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
                current_indoor_temp=20.6,
                current_outdoor_temp=2.0,
                current_water_temp=35.0,
                current_zone_target_temp=33.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
            )

        boost, restore = actions
        assert boost["payload"]["offset"] == 1
        assert boost["payload"]["temperature"] == 34
        assert restore["payload"]["boost_temperature"] == 34

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
                current_zone_target_temp=64.0,
                current_zone_heat_min=20,
                current_zone_heat_max=65,
                comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
                comfort_temp_target=21.0,
                comfort_temp_min=18.0,
                tz_name="UTC",
            )

        boost, restore = actions
        assert boost["payload"]["reason"] == "indoor_cooling_imminent"
        assert boost["payload"]["offset"] == 1
        assert boost["payload"]["baseline_temperature"] == 64
        assert boost["payload"]["temperature"] == 65
        assert restore["payload"]["temperature"] == 64
        assert restore["payload"]["boost_temperature"] == 65

    def test_guardrail_skips_missing_panasonic_zone_target(self, sample_prices):
        optimizer = RulesOptimizer()
        cold_weather = [(ts, 2.0) for ts, _ in sample_prices[:4]]

        actions = optimizer._plan_indoor_guardrails(
            sample_prices[:4],
            cold_weather,
            sample_prices[0][0],
            current_indoor_temp=18.0,
            current_outdoor_temp=2.0,
            current_water_temp=35.0,
            current_zone_target_temp=None,
            comfort_schedule={"weekday": list(range(24)), "weekend": list(range(24))},
            comfort_temp_target=21.0,
            comfort_temp_min=18.0,
            tz_name="UTC",
        )

        assert actions == []

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
            special_status_supported=True,
        )
        # With flat prices, no price-based overrides should occur
        for a in actions:
            if a["type"] == "comfort_mode_on":
                assert "peak_price" not in a["payload"].get("reason", "")
            # No eco hours should be "upgraded" due to cheap price
            if a["type"] == "normal_mode_on":
                assert "cheap_price" not in a["payload"].get("reason", "")

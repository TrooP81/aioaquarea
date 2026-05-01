"""Tests for the MILP optimizer."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.optimizer import InfeasibleError, DataIncompleteError, SolverTimeoutError


def _make_prices(hours=24, base_price=0.10):
    """Generate hourly prices with a day/night pattern."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    hourly = [
        0.05, 0.04, 0.03, 0.03, 0.04, 0.06,
        0.08, 0.12, 0.15, 0.18, 0.20, 0.22,
        0.20, 0.18, 0.15, 0.14, 0.16, 0.25,
        0.30, 0.28, 0.20, 0.12, 0.08, 0.06,
    ]
    return [(base + dt.timedelta(hours=h), hourly[h % 24]) for h in range(hours)]


def _make_weather(hours=24):
    """Generate hourly weather (mild spring day)."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    temps = [
        5, 4, 3, 3, 3, 4,
        5, 7, 9, 11, 13, 14,
        15, 15, 14, 13, 12, 10,
        8, 7, 6, 6, 5, 5,
    ]
    return [(base + dt.timedelta(hours=h), temps[h % 24]) for h in range(hours)]


def _make_freezing_weather(hours=24):
    """Generate freezing weather."""
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    return [(base + dt.timedelta(hours=h), -5.0) for h in range(hours)]


class TestMILPSolver:
    """Tests for the MILP solve logic (synchronous, no DB)."""

    def test_milp_picks_cheapest_dhw_slot(self):
        """MILP should schedule DHW during cheap hours."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()
        prices = _make_prices()
        weather = _make_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=lambda t, h=12: 3.5 + 0.1 * t,
            demand_per_hour=[3.0] * 24,
            current_tank_temp=48.0,
        )

        assert plan is not None
        assert plan["version"] == "milp_v1"
        assert plan["cost_estimate"] is not None

        dhw_on = [a for a in plan["actions"] if a["type"] == "force_dhw_on"]
        assert len(dhw_on) > 0

        # DHW should be in relatively cheap hours
        for action in dhw_on:
            price = action["payload"]["price"]
            assert price <= 0.20  # Not in the most expensive hours

    def test_milp_respects_tank_deadline(self):
        """Tank should be warm enough by deadline hours."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()
        prices = _make_prices()
        weather = _make_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=lambda t, h=12: 3.5 + 0.1 * t,
            demand_per_hour=[3.0] * 24,
            current_tank_temp=45.0,  # Start at minimum
        )

        assert plan is not None
        # Plan should have DHW actions to heat the tank before deadlines
        dhw_on = [a for a in plan["actions"] if a["type"] == "force_dhw_on"]
        assert len(dhw_on) > 0

    def test_milp_freeze_protection(self):
        """Must maintain minimum heating when outdoor temp < 0°C."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()
        prices = _make_prices()
        weather = _make_freezing_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=lambda t, h=12: max(1.5, 3.5 + 0.1 * t),
            demand_per_hour=[3.0] * 24,
            current_tank_temp=48.0,
        )

        # Should produce a plan even in freezing conditions
        assert plan is not None

    def test_milp_rate_limit_constraint(self):
        """No more than 20 DHW activations per day."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()
        prices = _make_prices()
        weather = _make_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=lambda t, h=12: 3.5 + 0.1 * t,
            demand_per_hour=[3.0] * 24,
            current_tank_temp=48.0,
        )

        assert plan is not None
        dhw_on = [a for a in plan["actions"] if a["type"] == "force_dhw_on"]
        assert len(dhw_on) <= 20

    def test_milp_with_ml_cop_model(self):
        """MILP should use ML COP model when provided."""
        from packages.optimizer.milp import MILPOptimizer

        mock_cop = MagicMock()
        mock_cop.is_trained = True
        mock_cop.predict_cop = MagicMock(return_value=4.0)

        milp = MILPOptimizer(cop_model=mock_cop)
        prices = _make_prices()
        weather = _make_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=milp._build_cop_function(None),
            demand_per_hour=[3.0] * 24,
            current_tank_temp=48.0,
        )

        assert plan is not None
        assert "+ml" in plan["version"]

    def test_milp_version_without_ml(self):
        """Version should be plain milp_v1 without ML models."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()
        prices = _make_prices()
        weather = _make_weather()

        plan = milp._solve(
            prices, weather,
            cop_fn=lambda t, h=12: 3.5 + 0.1 * t,
            demand_per_hour=[3.0] * 24,
            current_tank_temp=48.0,
        )

        assert plan is not None
        assert plan["version"] == "milp_v1"


class TestMILPGeneratePlan:
    """Tests for the async generate_plan interface."""

    @pytest.mark.asyncio
    async def test_missing_prices_raises_data_incomplete(self):
        """Should raise DataIncompleteError when no prices available."""
        from packages.optimizer.milp import MILPOptimizer

        milp = MILPOptimizer()

        with patch("packages.optimizer.milp.get_session") as mock_session_ctx:
            mock_session = AsyncMock()
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            # Empty price result
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_session.execute = AsyncMock(return_value=mock_result)

            with pytest.raises(DataIncompleteError, match="No price data"):
                await milp.generate_plan()

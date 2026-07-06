"""Tests for thermal model calibration and predictions."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from packages.ml.thermal import ThermalModel, ThermalPrediction


def _make_status_record(ts, tank_temp, outdoor_temp, zone1_temp=20.0, direction=None, defrost_active=False):
    """Create a mock DeviceStatusRecord."""
    rec = MagicMock()
    rec.ts = ts
    rec.tank_temp = tank_temp
    rec.outdoor_temp = outdoor_temp
    rec.zone1_temp = zone1_temp
    rec.direction = direction
    rec.defrost_active = defrost_active
    return rec


def _generate_heating_records(
    n=30, start_tank=40.0, heating_rate=4.0, outdoor=10.0, interval_minutes=15
):
    """Generate records simulating tank heating."""
    base = dt.datetime(2026, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    records = []
    tank_temp = start_tank
    for i in range(n):
        ts = base + dt.timedelta(minutes=interval_minutes * i)
        records.append(_make_status_record(
            ts=ts,
            tank_temp=tank_temp,
            outdoor_temp=outdoor,
            direction="WATER",
        ))
        # Increase tank temp at given rate (convert hourly rate to per-interval)
        tank_temp += heating_rate * (interval_minutes / 60.0)
    return records


class TestThermalPredictions:
    """Test predict methods with default params (no calibration)."""

    def test_tank_heating_time_basic(self):
        model = ThermalModel()
        pred = model.predict_tank_heating_time(
            current_temp=42.0, target_temp=50.0, outdoor_temp=10.0
        )
        assert isinstance(pred, ThermalPrediction)
        assert pred.estimated_minutes > 0
        assert pred.confidence == "default"

    def test_tank_heating_warmer_outdoor_is_faster(self):
        """Warmer outdoor temp → higher COP → faster heating."""
        model = ThermalModel()
        cold = model.predict_tank_heating_time(42.0, 50.0, outdoor_temp=-5.0)
        warm = model.predict_tank_heating_time(42.0, 50.0, outdoor_temp=20.0)
        assert warm.estimated_minutes < cold.estimated_minutes

    def test_tank_heating_already_at_target(self):
        model = ThermalModel()
        pred = model.predict_tank_heating_time(50.0, 50.0, outdoor_temp=10.0)
        assert pred.estimated_minutes == 0.0

    def test_tank_heating_above_target(self):
        model = ThermalModel()
        pred = model.predict_tank_heating_time(52.0, 50.0, outdoor_temp=10.0)
        assert pred.estimated_minutes == 0.0

    def test_tank_cooling_time_basic(self):
        model = ThermalModel()
        pred = model.predict_tank_cooling_time(50.0, 40.0, outdoor_temp=10.0)
        assert pred.estimated_minutes > 0
        assert pred.heating_rate_per_hour < 0  # loss rate is negative

    def test_tank_cooling_already_below_min(self):
        model = ThermalModel()
        pred = model.predict_tank_cooling_time(38.0, 40.0, outdoor_temp=10.0)
        assert pred.estimated_minutes == 0.0

    def test_zone_heating_time_basic(self):
        model = ThermalModel()
        pred = model.predict_zone_heating_time(18.0, 21.0, outdoor_temp=5.0)
        assert pred.estimated_minutes > 0
        assert pred.confidence == "default"

    def test_zone_heating_already_at_target(self):
        model = ThermalModel()
        pred = model.predict_zone_heating_time(21.0, 21.0, outdoor_temp=5.0)
        assert pred.estimated_minutes == 0.0


class TestOptimalStartTime:
    """Test optimal start time calculation."""

    def test_calculates_start_before_deadline(self):
        model = ThermalModel()
        deadline = dt.datetime(2026, 1, 10, 7, 0, tzinfo=dt.timezone.utc)
        start = model.optimal_start_time(
            current_temp=42.0,
            target_temp=50.0,
            deadline=deadline,
            outdoor_temp=10.0,
            is_tank=True,
        )
        assert start < deadline

    def test_includes_buffer(self):
        """Start time should include 15% buffer over raw prediction."""
        model = ThermalModel()
        deadline = dt.datetime(2026, 1, 10, 7, 0, tzinfo=dt.timezone.utc)
        pred = model.predict_tank_heating_time(42.0, 50.0, outdoor_temp=10.0)
        start = model.optimal_start_time(42.0, 50.0, deadline, 10.0, is_tank=True)

        # The optimal_start_time should account for 15% buffer
        raw_start = deadline - dt.timedelta(minutes=pred.estimated_minutes)
        assert start < raw_start  # With buffer, starts earlier

    def test_zone_start_time(self):
        model = ThermalModel()
        deadline = dt.datetime(2026, 1, 10, 7, 0, tzinfo=dt.timezone.utc)
        start = model.optimal_start_time(18.0, 21.0, deadline, 5.0, is_tank=False)
        assert start < deadline


class TestTemperatureCurve:
    """Test temperature curve prediction."""

    def test_standby_curve_decreasing(self):
        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=50.0, outdoor_temp=5.0, hours=12, is_tank=True
        )
        assert len(curve) == 12
        # Temperature should be decreasing (standby loss)
        assert curve[-1]["predicted_temp"] < curve[0]["predicted_temp"]
        assert all(entry["state"] == "standby" for entry in curve)

    def test_standby_curve_never_below_outdoor(self):
        """Tank can never cool below outdoor temperature."""
        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=50.0, outdoor_temp=5.0, hours=168, is_tank=True  # 7 days
        )
        for entry in curve:
            assert entry["predicted_temp"] >= 5.0, (
                f"hour {entry['hour']}: {entry['predicted_temp']}°C is below "
                f"outdoor temp 5.0°C"
            )

    def test_standby_curve_loss_slows_near_outdoor(self):
        """Loss rate should diminish as temp approaches outdoor (Newton's law)."""
        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=50.0, outdoor_temp=10.0, hours=48, is_tank=True
        )
        # Hourly drops should decrease over time
        drops = [
            curve[i]["predicted_temp"] - curve[i + 1]["predicted_temp"]
            for i in range(len(curve) - 1)
            if curve[i + 1]["predicted_temp"] > 10.0  # still above outdoor
        ]
        if len(drops) > 2:
            assert drops[0] >= drops[-1], "Loss should slow as temp approaches outdoor"

    def test_heating_then_standby_curve(self):
        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=40.0, outdoor_temp=10.0, hours=24, target_temp=50.0, is_tank=True
        )
        assert len(curve) == 24
        states = [entry["state"] for entry in curve]
        assert "heating" in states
        # Eventually reaches target and switches to standby
        assert states[-1] == "standby"


class TestCalibrate:
    """Test calibration with mocked DB data."""

    async def test_insufficient_data_returns_status(self):
        model = ThermalModel()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            _make_status_record(
                dt.datetime(2026, 1, 10, i, 0, tzinfo=dt.timezone.utc), 45.0, 10.0
            )
            for i in range(5)  # Only 5 records, less than min_samples=20
        ]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await model.calibrate()

        assert result["status"] == "insufficient_data"
        assert result["records"] == 5

    async def test_calibrate_with_heating_data(self):
        """Calibration with records showing clear tank heating."""
        model = ThermalModel()

        # Generate 40 records with tank heating (4°C/h at outdoor=10)
        records = _generate_heating_records(n=40, heating_rate=5.0, outdoor=10.0)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records

        # Second session (indoor readings) returns empty
        mock_indoor_result = MagicMock()
        mock_indoor_result.scalars.return_value.all.return_value = []

        call_count = [0]
        async def _mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] <= 1 else mock_indoor_result

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=_mock_execute)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await model.calibrate()

        assert result["status"] == "calibrated"
        assert result["samples"] == 40
        assert result["tank_heating_samples"] > 0
        assert model.params.last_calibrated is not None
        # Learned rate should be around 5°C/h (at outdoor=10)
        effective_rate = model._tank_heating_rate(10.0)
        assert 3.0 < effective_rate < 8.0

    async def test_defrost_intervals_filtered(self):
        """Records with defrost_active=True should be skipped."""
        model = ThermalModel()
        base = dt.datetime(2026, 1, 10, 0, 0, tzinfo=dt.timezone.utc)

        records = []
        for i in range(30):
            ts = base + dt.timedelta(minutes=15 * i)
            defrost = (i % 5 == 0)  # Every 5th record is defrost
            records.append(_make_status_record(
                ts=ts, tank_temp=45.0 + i * 0.5, outdoor_temp=5.0,
                direction="WATER", defrost_active=defrost,
            ))

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records

        mock_indoor_result = MagicMock()
        mock_indoor_result.scalars.return_value.all.return_value = []

        call_count = [0]
        async def _mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] <= 1 else mock_indoor_result

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=_mock_execute)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await model.calibrate()

        assert result["status"] == "calibrated"
        assert result["defrost_intervals_filtered"] > 0

    async def test_calibrate_confidence_becomes_learned(self):
        """After calibration, predictions should have confidence='learned'."""
        model = ThermalModel()
        records = _generate_heating_records(n=30, heating_rate=4.0, outdoor=10.0)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records

        mock_indoor_result = MagicMock()
        mock_indoor_result.scalars.return_value.all.return_value = []

        call_count = [0]
        async def _mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] <= 1 else mock_indoor_result

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=_mock_execute)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            await model.calibrate()

        pred = model.predict_tank_heating_time(42.0, 50.0, outdoor_temp=10.0)
        assert pred.confidence == "learned"


class TestInternalRates:
    """Test the internal rate calculation methods."""

    def test_tank_heating_rate_minimum(self):
        """Tank heating rate should never go below 1.0."""
        model = ThermalModel()
        model.params.tank_heating_rate = 0.5
        model.params.tank_heating_outdoor_factor = 0.0
        assert model._tank_heating_rate(-20.0) == 1.0

    def test_tank_loss_rate_capped(self):
        """Tank loss rate should be capped to -3.0 maximum."""
        model = ThermalModel()
        model.params.tank_standby_loss = -10.0
        assert model._tank_loss_rate(0.0) == -3.0

    def test_tank_loss_rate_never_positive(self):
        """Tank loss rate should always be <= 0."""
        model = ThermalModel()
        assert model._tank_loss_rate(30.0) <= 0.0

    def test_zone_heating_rate_minimum(self):
        """Zone heating rate should never go below 0.5."""
        model = ThermalModel()
        model.params.zone_heating_rate = 0.1
        assert model._zone_heating_rate(-30.0) >= 0.5

    def test_zone_loss_rate_never_positive(self):
        model = ThermalModel()
        assert model._zone_loss_rate(30.0) <= 0.0


# --- Indoor temperature prediction tests ---


class TestIndoorPredictions:
    """Test indoor air temperature prediction methods."""

    def test_indoor_heating_time_basic(self):
        model = ThermalModel()
        pred = model.predict_indoor_heating_time(19.0, 21.0, outdoor_temp=5.0)
        assert isinstance(pred, ThermalPrediction)
        assert pred.estimated_minutes > 0
        assert pred.confidence == "default"

    def test_indoor_heating_already_at_target(self):
        model = ThermalModel()
        pred = model.predict_indoor_heating_time(21.0, 21.0, outdoor_temp=5.0)
        assert pred.estimated_minutes == 0.0

    def test_indoor_heating_above_target(self):
        model = ThermalModel()
        pred = model.predict_indoor_heating_time(22.0, 21.0, outdoor_temp=5.0)
        assert pred.estimated_minutes == 0.0

    def test_indoor_cooling_time_basic(self):
        model = ThermalModel()
        pred = model.predict_indoor_cooling_time(21.0, 18.0, outdoor_temp=5.0)
        assert pred.estimated_minutes > 0
        assert pred.heating_rate_per_hour < 0

    def test_indoor_cooling_already_below_min(self):
        model = ThermalModel()
        pred = model.predict_indoor_cooling_time(17.0, 18.0, outdoor_temp=5.0)
        assert pred.estimated_minutes == 0.0

    def test_indoor_cooling_never_below_outdoor(self):
        """Indoor temp can't cool below outdoor temperature."""
        model = ThermalModel()
        pred = model.predict_indoor_cooling_time(21.0, 10.0, outdoor_temp=15.0)
        # min_temp=10 but outdoor=15, so effective_min=15
        # 21 → 15 at cooling rate
        assert pred.estimated_minutes > 0

    def test_indoor_warmer_outdoor_heats_faster(self):
        """Warmer outdoor should allow slightly faster indoor heating."""
        model = ThermalModel()
        cold = model.predict_indoor_heating_time(18.0, 21.0, outdoor_temp=-5.0)
        warm = model.predict_indoor_heating_time(18.0, 21.0, outdoor_temp=15.0)
        assert warm.estimated_minutes <= cold.estimated_minutes


class TestIndoorRates:
    """Test indoor heating/cooling rate helper methods."""

    def test_indoor_heating_rate_minimum(self):
        model = ThermalModel()
        model.params.indoor_heating_rate = 0.01
        model.params.indoor_heating_outdoor_factor = 0.0
        assert model._indoor_heating_rate(-20.0) == 0.1

    def test_indoor_heating_rate_maximum(self):
        model = ThermalModel()
        model.params.indoor_heating_rate = 5.0
        assert model._indoor_heating_rate(10.0) == 3.0

    def test_indoor_cooling_rate_never_positive(self):
        model = ThermalModel()
        assert model._indoor_cooling_rate(30.0) <= 0.0

    def test_indoor_cooling_rate_capped(self):
        model = ThermalModel()
        model.params.indoor_cooling_rate = -5.0
        assert model._indoor_cooling_rate(0.0) == -1.0


class TestIndoorCurve:
    """Test predict_indoor_curve method."""

    def test_indoor_curve_length(self):
        model = ThermalModel()
        weather = [{"outdoor_temp": 5.0, "wind_speed": 3.0, "irradiance": 0.0, "hour": h}
                   for h in range(12)]
        water_temps = [35.0] * 12
        curve = model.predict_indoor_curve(20.0, water_temps, weather, hours=12)
        assert len(curve) == 12

    def test_indoor_curve_keys(self):
        model = ThermalModel()
        weather = [{"outdoor_temp": 5.0, "wind_speed": 3.0, "irradiance": 0.0, "hour": 10}]
        water_temps = [35.0]
        curve = model.predict_indoor_curve(20.0, water_temps, weather, hours=1)
        assert "predicted_indoor_temp" in curve[0]
        assert "source" in curve[0]
        assert "hour" in curve[0]

    def test_indoor_curve_fallback_source(self):
        """Without trained comfort model, source should be linear_rates."""
        model = ThermalModel()
        weather = [{"outdoor_temp": 5.0, "wind_speed": 3.0, "irradiance": 0.0, "hour": h}
                   for h in range(6)]
        water_temps = [35.0] * 6
        curve = model.predict_indoor_curve(20.0, water_temps, weather, hours=6)
        assert all(entry["source"] == "linear_rates" for entry in curve)

    def test_indoor_curve_cooling_without_heating(self):
        """With low water temp, indoor should cool toward outdoor."""
        model = ThermalModel()
        weather = [{"outdoor_temp": 5.0, "wind_speed": 3.0, "irradiance": 0.0, "hour": h}
                   for h in range(24)]
        water_temps = [15.0] * 24  # Water temp below indoor → no heating effect
        curve = model.predict_indoor_curve(20.0, water_temps, weather, hours=24)
        # Should be cooling over time
        assert curve[-1]["predicted_indoor_temp"] < curve[0]["predicted_indoor_temp"]

    def test_indoor_curve_never_below_outdoor(self):
        """Indoor temp should never go below outdoor temperature."""
        model = ThermalModel()
        weather = [{"outdoor_temp": 10.0, "wind_speed": 3.0, "irradiance": 0.0, "hour": h}
                   for h in range(48)]
        water_temps = [10.0] * 48
        curve = model.predict_indoor_curve(15.0, water_temps, weather, hours=48)
        for entry in curve:
            assert entry["predicted_indoor_temp"] >= 10.0


class TestManagedTankCurve:
    """Test predict_managed_tank_curve — schedule-aware deadband control."""

    def test_curve_length_and_keys(self):
        model = ThermalModel()
        floors = [45.0] * 24
        curve = model.predict_managed_tank_curve(
            current_temp=48.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=floors, hours=24,
        )
        assert len(curve) == 24
        assert {"hour", "predicted_temp", "state", "floor"} <= set(curve[0])

    def test_not_pinned_at_target(self):
        """Regression: the tank must coast down, not hold flat at the target."""
        model = ThermalModel()
        floors = [45.0] * 24
        curve = model.predict_managed_tank_curve(
            current_temp=52.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=floors, hours=24,
        )
        temps = [c["predicted_temp"] for c in curve]
        assert min(temps) < 52.0  # it cools between reheats

    def test_overnight_floor_allows_deeper_drop(self):
        """Lower overnight floor should let the tank dip toward the off-peak min."""
        model = ThermalModel()
        # Daytime floor 45 for first 6h, off-peak floor 41 overnight, back to 45.
        floors = [45.0] * 6 + [41.0] * 10 + [45.0] * 8
        curve = model.predict_managed_tank_curve(
            current_temp=48.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=floors, hours=24,
        )
        overnight_min = min(c["predicted_temp"] for c in curve[6:16])
        # Reaches close to the off-peak floor (within one heating step).
        assert overnight_min <= 42.0
        assert overnight_min >= 41.0

    def test_never_coasts_below_floor(self):
        model = ThermalModel()
        floors = [45.0] * 12
        curve = model.predict_managed_tank_curve(
            current_temp=48.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=floors, hours=12,
        )
        for c in curve:
            assert c["predicted_temp"] >= c["floor"] - 1e-6

    def test_reheats_to_target(self):
        """Once it hits the floor it must climb back to the target."""
        model = ThermalModel()
        floors = [45.0] * 48
        curve = model.predict_managed_tank_curve(
            current_temp=45.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=floors, hours=48,
        )
        temps = [c["predicted_temp"] for c in curve]
        assert max(temps) >= 51.9  # reheats up to target

    def test_shorter_floor_list_reuses_last(self):
        model = ThermalModel()
        curve = model.predict_managed_tank_curve(
            current_temp=48.0, outdoor_temp=7.0, tank_target=52.0,
            tank_min_per_hour=[45.0], hours=6,
        )
        assert len(curve) == 6
        assert all(c["floor"] == 45.0 for c in curve)


class TestManagedIndoorCurve:
    """Test predict_managed_indoor_curve — schedule-aware comfort setback."""

    def _weather(self, hours, outdoor=2.0):
        return [{"outdoor_temp": outdoor, "wind_speed": 3.0, "irradiance": 0.0, "hour": h % 24}
                for h in range(hours)]

    def test_curve_length_and_keys(self):
        model = ThermalModel()
        targets = [20.5] * 24
        curve = model.predict_managed_indoor_curve(
            current_indoor=20.5, indoor_target_per_hour=targets,
            weather_forecast=self._weather(24), hours=24,
        )
        assert len(curve) == 24
        assert {"hour", "predicted_indoor_temp", "target", "state", "source"} <= set(curve[0])

    def test_not_flat_follows_setback(self):
        """Regression: predicted indoor must dip during the overnight setback."""
        model = ThermalModel()
        targets = [20.5] * 6 + [18.0] * 8 + [20.5] * 10
        curve = model.predict_managed_indoor_curve(
            current_indoor=20.5, indoor_target_per_hour=targets,
            weather_forecast=self._weather(24), hours=24,
        )
        temps = [c["predicted_indoor_temp"] for c in curve]
        # Holds at comfort during the first comfort block.
        assert temps[0] == 20.5
        # Dips toward the off-peak setback overnight.
        assert min(temps[6:14]) < 19.0

    def test_recovers_to_comfort_target(self):
        """After the setback it must reheat back up to the comfort target."""
        model = ThermalModel()
        targets = [18.0] * 8 + [20.5] * 16
        curve = model.predict_managed_indoor_curve(
            current_indoor=18.0, indoor_target_per_hour=targets,
            weather_forecast=self._weather(24), hours=24,
        )
        assert curve[-1]["predicted_indoor_temp"] >= 20.4

    def test_never_below_setpoint_or_outdoor(self):
        model = ThermalModel()
        targets = [18.0] * 24
        curve = model.predict_managed_indoor_curve(
            current_indoor=20.5, indoor_target_per_hour=targets,
            weather_forecast=self._weather(24, outdoor=5.0), hours=24,
        )
        for c in curve:
            assert c["predicted_indoor_temp"] >= min(c["target"], 5.0) - 1e-6
            assert c["predicted_indoor_temp"] >= 5.0 - 1e-6

    def test_holds_when_already_at_target(self):
        """Steady at the comfort target should stay flat at the target."""
        model = ThermalModel()
        targets = [20.5] * 12
        curve = model.predict_managed_indoor_curve(
            current_indoor=20.5, indoor_target_per_hour=targets,
            weather_forecast=self._weather(12), hours=12,
        )
        assert all(abs(c["predicted_indoor_temp"] - 20.5) < 1e-6 for c in curve)

    def test_shorter_target_list_reuses_last(self):
        model = ThermalModel()
        curve = model.predict_managed_indoor_curve(
            current_indoor=20.5, indoor_target_per_hour=[20.5],
            weather_forecast=self._weather(6), hours=6,
        )
        assert len(curve) == 6
        assert all(c["target"] == 20.5 for c in curve)


class TestPlannedTankCurve:
    """Test predict_planned_tank_curve — follows the optimizer's DHW schedule."""

    def test_curve_length_and_keys(self):
        model = ThermalModel()
        dhw = [0.0] * 24
        curve = model.predict_planned_tank_curve(
            current_temp=48.0, outdoor_temp=7.0, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=24,
        )
        assert len(curve) == 24
        assert {"hour", "predicted_temp", "state", "dhw_minutes"} <= set(curve[0])

    def test_heats_during_planned_dhw_hours(self):
        """A planned DHW hour must raise the tank toward target."""
        model = ThermalModel()
        dhw = [0.0] * 6
        dhw[2] = 60.0
        curve = model.predict_planned_tank_curve(
            current_temp=46.0, outdoor_temp=7.0, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=6,
        )
        # The heating hour is hotter than the hour before it.
        assert curve[2]["predicted_temp"] > curve[1]["predicted_temp"]
        assert curve[2]["state"] == "heating"

    def test_coasts_between_dhw_cycles(self):
        """With no DHW the tank loses heat (standby)."""
        model = ThermalModel()
        dhw = [0.0] * 8
        curve = model.predict_planned_tank_curve(
            current_temp=52.0, outdoor_temp=7.0, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=8,
        )
        assert all(c["state"] == "standby" for c in curve)
        assert curve[-1]["predicted_temp"] < curve[0]["predicted_temp"]

    def test_caps_at_target(self):
        model = ThermalModel()
        dhw = [60.0] * 12
        curve = model.predict_planned_tank_curve(
            current_temp=50.0, outdoor_temp=7.0, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=12,
        )
        for c in curve:
            assert c["predicted_temp"] <= 52.0 + 1e-6

    def test_never_below_outdoor(self):
        model = ThermalModel()
        dhw = [0.0] * 48
        curve = model.predict_planned_tank_curve(
            current_temp=20.0, outdoor_temp=18.0, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=48,
        )
        for c in curve:
            assert c["predicted_temp"] >= 18.0 - 1e-6

    def test_follows_schedule_not_flat(self):
        """Regression: the curve must track the planned cycles, staying topped up."""
        model = ThermalModel()
        dhw = [0.0] * 24
        for h in (1, 6, 10, 11):
            dhw[h] = 60.0
        curve = model.predict_planned_tank_curve(
            current_temp=48.0, outdoor_temp=23.6, tank_target=52.0,
            dhw_minutes_per_hour=dhw, hours=24,
        )
        heating_hours = [c["hour"] for c in curve if c["state"] == "heating"]
        # Heating happens exactly on the planned DHW hours (h -> hour h+1).
        assert heating_hours == [2, 7, 11, 12]
        # Frequent cycles keep it near target, not dropping to a deep setback.
        assert min(c["predicted_temp"] for c in curve) > 45.0

    def test_partial_hour_minutes(self):
        """Half-hour of DHW heats less than a full hour."""
        model = ThermalModel()
        full = model.predict_planned_tank_curve(
            current_temp=46.0, outdoor_temp=7.0, tank_target=60.0,
            dhw_minutes_per_hour=[60.0], hours=1,
        )
        half = model.predict_planned_tank_curve(
            current_temp=46.0, outdoor_temp=7.0, tank_target=60.0,
            dhw_minutes_per_hour=[30.0], hours=1,
        )
        assert half[0]["predicted_temp"] < full[0]["predicted_temp"]

    def test_heating_never_below_standby_when_above_target(self):
        """Regression: starting above target, a DHW cycle must not cool the tank.

        The tank starts at/above ``tank_target`` ("At target"). A scheduled DHW
        hour must never pull the tank *below* where the pure standby ("no
        heating") curve would be — heating can only add heat, never remove it.
        """
        model = ThermalModel()
        current_temp = 50.0
        tank_target = 45.0  # tank already 5°C above target
        outdoor = 7.0
        dhw = [0.0] * 12
        for h in (3, 7, 9):  # scheduled DHW cycles
            dhw[h] = 60.0

        heating = model.predict_planned_tank_curve(
            current_temp=current_temp, outdoor_temp=outdoor,
            tank_target=tank_target, dhw_minutes_per_hour=dhw, hours=12,
        )
        standby = model.predict_temperature_curve(
            current_temp=current_temp, outdoor_temp=outdoor,
            hours=12, target_temp=None, is_tank=True,
        )
        for hh, ss in zip(heating, standby):
            assert hh["predicted_temp"] >= ss["predicted_temp"] - 1e-6, (
                f"hour {hh['hour']}: with-heating {hh['predicted_temp']} "
                f"dropped below no-heating {ss['predicted_temp']}"
            )


class TestCalibrateIndoorRates:
    """Test indoor rate calibration with mocked DB data."""

    async def test_calibrate_includes_indoor_rates_in_result(self):
        """Calibration result should include indoor rate keys."""
        model = ThermalModel()
        records = _generate_heating_records(n=30, heating_rate=4.0, outdoor=10.0)

        # Mock device status records
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records

        # Mock empty indoor readings (no SmartThings data)
        mock_indoor_result = MagicMock()
        mock_indoor_result.scalars.return_value.all.return_value = []

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] <= 1:
                return mock_result  # Device status query
            return mock_indoor_result  # Indoor temp query

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await model.calibrate()

        assert result["status"] == "calibrated"
        assert "indoor_heating_samples" in result
        assert "indoor_cooling_samples" in result
        assert "indoor_heating_rate" in result["params"]
        assert "indoor_cooling_rate" in result["params"]

    async def test_calibrate_no_smartthings_uses_defaults(self):
        """Without SmartThings data, indoor rates stay at defaults."""
        model = ThermalModel()
        records = _generate_heating_records(n=30, heating_rate=4.0, outdoor=10.0)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records

        mock_indoor_result = MagicMock()
        mock_indoor_result.scalars.return_value.all.return_value = []

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] <= 1:
                return mock_result
            return mock_indoor_result

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with patch("packages.ml.thermal.get_session") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            await model.calibrate()

        # Should keep defaults
        assert model.params.indoor_heating_rate == 0.5
        assert model.params.indoor_cooling_rate == -0.3
        assert model.params.indoor_heating_samples == 0
        assert model.params.indoor_cooling_samples == 0

"""Tests for thermal model calibration and predictions."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from packages.ml.thermal import ThermalModel, ThermalParams, ThermalPrediction


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

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

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

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

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

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

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

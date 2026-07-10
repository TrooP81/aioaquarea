"""Thermal dynamics model: predicts tank and zone temperature changes over time.

Uses historical temperature deltas to model:
- Tank heating rate (°C/hour) as a function of outdoor temp and operating mode
- Tank cooling rate (standby loss) as a function of outdoor temp
- Zone heating/cooling rates

These predictions enable the optimizer to schedule actions at the optimal time
(e.g., start DHW 90 minutes before deadline rather than a fixed 2-hour window).

Phase 3 improvements:
- Filters out defrost intervals (unreliable temperature readings)
- Uses `direction` field to distinguish DHW heating from zone heating
- Separates tank heating rates by compressor mode (WATER vs PUMP)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import numpy as np
import structlog
from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord, IndoorTempReading

logger = structlog.get_logger(__name__)


@dataclass
class ThermalParams:
    """Learned thermal parameters for the system."""

    # Tank heating rate: °C/hour at reference outdoor temp (10°C)
    tank_heating_rate: float = 4.0
    # Tank heating rate sensitivity to outdoor temp: higher outdoor → faster heating (better COP)
    tank_heating_outdoor_factor: float = 0.08  # °C/hour per °C outdoor
    # Tank standby loss: °C/hour (negative = cooling)
    tank_standby_loss: float = -0.5
    # Tank standby loss sensitivity to outdoor temp: colder outdoor → faster loss
    tank_loss_outdoor_factor: float = 0.01  # reduced loss per °C warmer outdoor
    # Zone heating rate: °C/hour
    zone_heating_rate: float = 1.5
    # Zone cooling rate (standby): °C/hour
    zone_standby_loss: float = -0.3
    # Zone loss sensitivity to outdoor temp
    zone_loss_outdoor_factor: float = 0.015
    # Minimum samples required for learning
    min_samples: int = 20
    # Last calibration timestamp
    last_calibrated: Optional[dt.datetime] = None
    # Sample count used
    sample_count: int = 0
    # Defrost intervals filtered
    defrost_intervals_filtered: int = 0
    # Direction-aware stats
    dhw_heating_samples: int = 0
    zone_compressor_samples: int = 0
    # Indoor air temperature rates (learned from SmartThings data)
    indoor_heating_rate: float = 0.5  # °C/hour when zone heating active
    indoor_heating_outdoor_factor: float = 0.01  # sensitivity to outdoor temp
    indoor_cooling_rate: float = -0.3  # °C/hour standby (negative = cooling)
    indoor_cooling_outdoor_factor: float = 0.01  # reduced loss per °C warmer outdoor
    indoor_heating_samples: int = 0
    indoor_cooling_samples: int = 0


@dataclass
class ThermalPrediction:
    """Result of a thermal prediction."""

    current_temp: float
    target_temp: float
    outdoor_temp: float
    estimated_minutes: float
    heating_rate_per_hour: float
    confidence: str  # "learned" or "default"

    @property
    def estimated_hours(self) -> float:
        return self.estimated_minutes / 60.0


# Passive solar gain added to indoor-air predictions. Direct sun measurably
# warms a home; roughly 0.4 °C over an hour at full sun (1000 W/m²), clamped so
# it can never dominate the learned heating/cooling dynamics. This lets the
# indoor forecast reflect sunny vs overcast hours (e.g. SMHI cloud-derived
# irradiance) even before the comfort model is trained.
_SOLAR_GAIN_C_PER_1000_WM2 = 0.4
_SOLAR_GAIN_MAX_C_PER_H = 0.5


def _solar_gain_c(irradiance: float | None) -> float:
    """Indoor-air warming (°C over one hour) contributed by solar irradiance."""
    if not irradiance or irradiance <= 0:
        return 0.0
    gain = _SOLAR_GAIN_C_PER_1000_WM2 * (irradiance / 1000.0)
    return min(_SOLAR_GAIN_MAX_C_PER_H, gain)


class ThermalModel:
    """
    Models tank and zone thermal dynamics from historical data.

    Key insight: by measuring ΔT between consecutive status records,
    we can determine:
    - How fast the tank heats when the compressor is running
    - How fast the tank cools in standby
    - How these rates vary with outdoor temperature

    This allows the optimizer to calculate the exact start time for
    DHW heating or zone boosting, rather than using fixed time windows.
    """

    def __init__(self):
        self.params = ThermalParams()

    def reset(self) -> None:
        """Discard learned calibration and return to default thermal parameters."""
        self.params = ThermalParams()

    async def calibrate(self) -> dict:
        """
        Learn thermal parameters from historical device status data.

        Phase 3 improvements:
        - Filters out defrost intervals (unreliable readings)
        - Uses `direction` field to distinguish DHW vs zone heating
        - Only counts tank heating when direction=WATER (compressor heating tank)
        - Only counts zone heating when direction=PUMP (compressor heating zones)
        """
        async with get_session() as session:
            # Get last 90 days of device status, ordered by time
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
            result = await session.execute(
                select(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts >= since)
                .order_by(DeviceStatusRecord.ts)
            )
            records = result.scalars().all()

        if len(records) < self.params.min_samples:
            logger.warning(
                f"Insufficient data for calibration: {len(records)} records "
                f"(need {self.params.min_samples})"
            )
            return {
                "status": "insufficient_data",
                "records": len(records),
                "min_required": self.params.min_samples,
            }

        # Compute deltas between consecutive records
        tank_heating_deltas = []  # (delta_deg_per_hour, outdoor_temp)
        tank_cooling_deltas = []
        zone_heating_deltas = []
        zone_cooling_deltas = []
        defrost_filtered = 0

        # Build sliding-window pairs with a minimum 10-min gap to avoid
        # amplifying short-interval temperature noise into extreme hourly
        # rates (e.g. 1°C / 5 min → 12°C/h when it's really ~2°C/h).
        MIN_GAP_S_DEVICE = 600   # 10 min
        MAX_GAP_S_DEVICE = 7200  # 2 h

        j = 0  # trailing pointer
        for i in range(1, len(records)):
            curr = records[i]

            # Advance j past records that are too old
            while j < i and (curr.ts - records[j].ts).total_seconds() > MAX_GAP_S_DEVICE:
                j += 1

            prev = records[j]
            gap_s = (curr.ts - prev.ts).total_seconds()
            if gap_s < MIN_GAP_S_DEVICE or gap_s > MAX_GAP_S_DEVICE:
                continue

            dt_hours = gap_s / 3600.0

            # FILTER: Skip defrost intervals — temperature readings are unreliable
            if getattr(curr, 'defrost_active', None) or getattr(prev, 'defrost_active', None):
                defrost_filtered += 1
                continue

            outdoor = curr.outdoor_temp if curr.outdoor_temp is not None else 10.0
            curr_direction = getattr(curr, 'direction', None)
            prev_direction = getattr(prev, 'direction', None)

            # Tank temperature delta
            if (
                prev.tank_temp is not None
                and curr.tank_temp is not None
                and prev.tank_temp > 0
                and curr.tank_temp > 0
            ):
                tank_delta = (curr.tank_temp - prev.tank_temp) / dt_hours

                # With sliding-window gaps, direction may change mid-interval.
                # Accept heating samples when EITHER endpoint was in WATER mode.
                either_water = "WATER" in (curr_direction, prev_direction)
                either_idle = curr_direction in (None, "IDLE", "PUMP") and prev_direction in (None, "IDLE", "PUMP")

                if tank_delta > 0.5:
                    if curr_direction is None or either_water:
                        tank_heating_deltas.append((tank_delta, outdoor))
                elif tank_delta < -0.1:
                    if curr_direction is None or either_idle:
                        tank_cooling_deltas.append((tank_delta, outdoor))

            # Zone 1 temperature delta
            if (
                prev.zone1_temp is not None
                and curr.zone1_temp is not None
                and prev.zone1_temp > 0
                and curr.zone1_temp > 0
            ):
                zone_delta = (curr.zone1_temp - prev.zone1_temp) / dt_hours

                # Accept zone heating when EITHER endpoint was in PUMP mode
                either_pump = "PUMP" in (curr_direction, prev_direction)
                either_not_pump = curr_direction in (None, "IDLE", "WATER") and prev_direction in (None, "IDLE", "WATER")

                if zone_delta > 0.2:
                    if curr_direction is None or either_pump:
                        zone_heating_deltas.append((zone_delta, outdoor))
                elif zone_delta < -0.1:
                    if curr_direction is None or either_not_pump:
                        zone_cooling_deltas.append((zone_delta, outdoor))

        # --- Fit linear relationships ---

        # Tank heating rate: base_rate + factor * outdoor_temp
        if len(tank_heating_deltas) >= 5:
            deltas = np.array([d[0] for d in tank_heating_deltas])
            outdoors = np.array([d[1] for d in tank_heating_deltas])

            # Simple linear regression: rate = a + b * outdoor_temp
            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.tank_heating_outdoor_factor = float(coeffs[0])
                self.params.tank_heating_rate = float(coeffs[1])  # intercept (at outdoor=0)
            else:
                self.params.tank_heating_rate = float(np.mean(deltas))

        # Tank standby loss (clamped: a 200L tank rarely loses more than 3°C/h)
        if len(tank_cooling_deltas) >= 5:
            deltas = np.array([d[0] for d in tank_cooling_deltas])
            outdoors = np.array([d[1] for d in tank_cooling_deltas])

            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.tank_loss_outdoor_factor = float(coeffs[0])
                self.params.tank_standby_loss = max(-3.0, float(coeffs[1]))
            else:
                self.params.tank_standby_loss = max(-3.0, float(np.mean(deltas)))

        # Zone heating rate (clamped: water supply typically heats at 2-15°C/h)
        if len(zone_heating_deltas) >= 5:
            deltas = np.array([d[0] for d in zone_heating_deltas])
            outdoors = np.array([d[1] for d in zone_heating_deltas])

            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.zone_heating_rate = min(15.0, float(coeffs[1]))
            else:
                self.params.zone_heating_rate = min(15.0, float(np.mean(deltas)))

        # Zone standby loss
        if len(zone_cooling_deltas) >= 5:
            deltas = np.array([d[0] for d in zone_cooling_deltas])
            outdoors = np.array([d[1] for d in zone_cooling_deltas])

            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.zone_loss_outdoor_factor = float(coeffs[0])
                self.params.zone_standby_loss = float(coeffs[1])
            else:
                self.params.zone_standby_loss = float(np.mean(deltas))

        # --- Indoor air temperature rates (from SmartThings data) ---
        indoor_heating_deltas, indoor_cooling_deltas = await self._calibrate_indoor_rates(
            since, records
        )

        self.params.last_calibrated = dt.datetime.now(dt.timezone.utc)
        self.params.sample_count = len(records)
        self.params.defrost_intervals_filtered = defrost_filtered
        self.params.dhw_heating_samples = len(tank_heating_deltas)
        self.params.zone_compressor_samples = len(zone_heating_deltas)

        logger.info(
            "thermal_model_calibrated",
            tank_heating=round(self.params.tank_heating_rate, 2),
            tank_heating_outdoor_factor=round(self.params.tank_heating_outdoor_factor, 3),
            tank_heating_samples=len(tank_heating_deltas),
            tank_loss=round(self.params.tank_standby_loss, 2),
            tank_loss_samples=len(tank_cooling_deltas),
            zone_heating=round(self.params.zone_heating_rate, 2),
            zone_heating_samples=len(zone_heating_deltas),
            indoor_heating=round(self.params.indoor_heating_rate, 2),
            indoor_cooling=round(self.params.indoor_cooling_rate, 2),
            defrost_filtered=defrost_filtered,
        )

        return {
            "status": "calibrated",
            "samples": len(records),
            "tank_heating_samples": len(tank_heating_deltas),
            "tank_cooling_samples": len(tank_cooling_deltas),
            "zone_heating_samples": len(zone_heating_deltas),
            "zone_cooling_samples": len(zone_cooling_deltas),
            "indoor_heating_samples": len(indoor_heating_deltas),
            "indoor_cooling_samples": len(indoor_cooling_deltas),
            "defrost_intervals_filtered": defrost_filtered,
            "params": {
                "tank_heating_rate": self.params.tank_heating_rate,
                "tank_heating_outdoor_factor": self.params.tank_heating_outdoor_factor,
                "tank_standby_loss": self.params.tank_standby_loss,
                "tank_loss_outdoor_factor": self.params.tank_loss_outdoor_factor,
                "zone_heating_rate": self.params.zone_heating_rate,
                "zone_standby_loss": self.params.zone_standby_loss,
                "zone_loss_outdoor_factor": self.params.zone_loss_outdoor_factor,
                "indoor_heating_rate": self.params.indoor_heating_rate,
                "indoor_heating_outdoor_factor": self.params.indoor_heating_outdoor_factor,
                "indoor_cooling_rate": self.params.indoor_cooling_rate,
                "indoor_cooling_outdoor_factor": self.params.indoor_cooling_outdoor_factor,
            },
        }

    async def _calibrate_indoor_rates(
        self,
        since: dt.datetime,
        device_records: list,
    ) -> tuple[list, list]:
        """
        Learn indoor air temperature heating/cooling rates from SmartThings data.

        Joins IndoorTempReading with the nearest DeviceStatusRecord to determine
        whether the zone was actively heating (direction=PUMP/HEATING) or idle.

        Returns (indoor_heating_deltas, indoor_cooling_deltas) for calibration stats.
        """
        async with get_session() as session:
            result = await session.execute(
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= since)
                .where(IndoorTempReading.is_stale == False)  # noqa: E712
                .order_by(IndoorTempReading.timestamp)
            )
            readings = result.scalars().all()

        if len(readings) < 2:
            return [], []

        # Build lookup for device status → nearest-neighbor matching
        status_times = np.array([
            (r.ts - since).total_seconds() for r in device_records
        ]) if device_records else np.array([])

        indoor_heating_deltas: list[tuple[float, float]] = []  # (delta_per_hour, outdoor)
        indoor_cooling_deltas: list[tuple[float, float]] = []

        # Build a time-indexed list for sliding-window pairing.
        # We pair each reading with the one ~15-30 min earlier rather than
        # the immediate predecessor.  This avoids amplifying sensor
        # quantisation noise (e.g. 0.1°C / 5 min → spurious -1.2°C/h).
        MIN_GAP_S = 900   # 15 min minimum between paired readings
        MAX_GAP_S = 7200  # 2 h maximum

        j = 0  # trailing pointer
        for i in range(1, len(readings)):
            curr_reading = readings[i]

            # Advance j until the gap is at least MIN_GAP_S
            while j < i and (curr_reading.timestamp - readings[j].timestamp).total_seconds() > MAX_GAP_S:
                j += 1

            prev_reading = readings[j]
            gap_s = (curr_reading.timestamp - prev_reading.timestamp).total_seconds()
            if gap_s < MIN_GAP_S or gap_s > MAX_GAP_S:
                continue

            dt_hours = gap_s / 3600.0
            abs_delta = abs(curr_reading.temperature - prev_reading.temperature)
            # Skip deltas within sensor noise (±0.15°C)
            if abs_delta < 0.15:
                continue

            indoor_delta = (curr_reading.temperature - prev_reading.temperature) / dt_hours

            # Find nearest device status to determine heating state
            if len(status_times) == 0:
                continue
            t_sec = (curr_reading.timestamp - since).total_seconds()
            idx = int(np.argmin(np.abs(status_times - t_sec)))
            status = device_records[idx]
            gap = abs((status.ts - curr_reading.timestamp).total_seconds())
            if gap > 900:  # > 15 min gap — skip
                continue

            outdoor = status.outdoor_temp if status.outdoor_temp is not None else 10.0
            direction = getattr(status, 'direction', None)
            device_action = getattr(status, 'device_action', None)

            zone_active = (
                direction in ("PUMP", None)
                and device_action in ("HEATING", None)
            ) if direction is not None or device_action is not None else False

            if indoor_delta > 0.1 and zone_active:
                indoor_heating_deltas.append((indoor_delta, outdoor))
            elif indoor_delta < -0.05:
                indoor_cooling_deltas.append((indoor_delta, outdoor))

        # Fit indoor heating rate
        if len(indoor_heating_deltas) >= 5:
            deltas = np.array([d[0] for d in indoor_heating_deltas])
            outdoors = np.array([d[1] for d in indoor_heating_deltas])
            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.indoor_heating_outdoor_factor = float(coeffs[0])
                self.params.indoor_heating_rate = float(coeffs[1])
            else:
                self.params.indoor_heating_rate = float(np.mean(deltas))

        # Fit indoor cooling rate (clamped: a house rarely cools faster than 1°C/h)
        if len(indoor_cooling_deltas) >= 5:
            deltas = np.array([d[0] for d in indoor_cooling_deltas])
            outdoors = np.array([d[1] for d in indoor_cooling_deltas])
            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.indoor_cooling_outdoor_factor = float(coeffs[0])
                self.params.indoor_cooling_rate = max(-1.0, float(coeffs[1]))
            else:
                self.params.indoor_cooling_rate = max(-1.0, float(np.mean(deltas)))

        self.params.indoor_heating_samples = len(indoor_heating_deltas)
        self.params.indoor_cooling_samples = len(indoor_cooling_deltas)

        if indoor_heating_deltas or indoor_cooling_deltas:
            logger.info(
                "indoor_rates_calibrated",
                heating_samples=len(indoor_heating_deltas),
                cooling_samples=len(indoor_cooling_deltas),
                heating_rate=self.params.indoor_heating_rate,
                cooling_rate=self.params.indoor_cooling_rate,
            )

        return indoor_heating_deltas, indoor_cooling_deltas

    def predict_tank_heating_time(
        self, current_temp: float, target_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """
        Predict how many minutes it takes to heat the tank from current to target temp.

        The heating rate depends on outdoor temperature (COP effect):
        - Warmer outdoor → better COP → faster heating
        """
        rate = self._tank_heating_rate(outdoor_temp)

        if rate <= 0:
            # Safety: if we somehow get a negative rate, use default
            rate = 3.0

        delta_needed = target_temp - current_temp
        if delta_needed <= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=target_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=0.0,
                heating_rate_per_hour=rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence="learned" if self.params.last_calibrated else "default",
        )

    def predict_tank_cooling_time(
        self, current_temp: float, min_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """
        Predict how many minutes until the tank cools from current to minimum temp.

        This tells us "how long can we delay heating before the tank gets too cold?"
        """
        loss_rate = self._tank_loss_rate(outdoor_temp)

        if loss_rate >= 0:
            # Not cooling — infinite time
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=min_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=float("inf"),
                heating_rate_per_hour=loss_rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        # Can't cool below outdoor temperature
        effective_min = max(min_temp, outdoor_temp)

        delta_until_min = current_temp - effective_min
        if delta_until_min <= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=min_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=0.0 if current_temp <= min_temp else float("inf"),
                heating_rate_per_hour=loss_rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        hours_until_cold = delta_until_min / abs(loss_rate)
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=min_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_until_cold * 60.0,
            heating_rate_per_hour=loss_rate,
            confidence="learned" if self.params.last_calibrated else "default",
        )

    def predict_zone_heating_time(
        self, current_temp: float, target_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """Predict how long to heat a zone to target temperature."""
        rate = self._zone_heating_rate(outdoor_temp)

        if rate <= 0:
            rate = 1.0

        delta_needed = target_temp - current_temp
        if delta_needed <= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=target_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=0.0,
                heating_rate_per_hour=rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence="learned" if self.params.last_calibrated else "default",
        )

    def predict_indoor_heating_time(
        self, current_temp: float, target_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """Predict how many minutes to raise indoor air temp from current to target."""
        rate = self._indoor_heating_rate(outdoor_temp)

        delta_needed = target_temp - current_temp
        if delta_needed <= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=target_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=0.0,
                heating_rate_per_hour=rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence="learned" if self.params.last_calibrated else "default",
        )

    def predict_indoor_cooling_time(
        self, current_temp: float, min_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """
        Predict how many minutes until indoor air cools from current to min_temp.

        Indoor air can't cool below outdoor temperature.
        """
        loss_rate = self._indoor_cooling_rate(outdoor_temp)

        if loss_rate >= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=min_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=float("inf"),
                heating_rate_per_hour=loss_rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        effective_min = max(min_temp, outdoor_temp)
        delta_until_min = current_temp - effective_min
        if delta_until_min <= 0:
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=min_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=0.0 if current_temp <= min_temp else float("inf"),
                heating_rate_per_hour=loss_rate,
                confidence="learned" if self.params.last_calibrated else "default",
            )

        hours_until_cold = delta_until_min / abs(loss_rate)
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=min_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_until_cold * 60.0,
            heating_rate_per_hour=loss_rate,
            confidence="learned" if self.params.last_calibrated else "default",
        )

    def predict_indoor_curve(
        self,
        current_indoor: float,
        zone_water_temps: list[float],
        weather_forecast: list[dict],
        hours: int = 24,
    ) -> list[dict]:
        """
        Predict indoor air temperature evolution over the planning horizon.

        When the ComfortModel is trained, uses autoregressive multi-step
        predictions for higher accuracy. Falls back to learned linear
        heating/cooling rates otherwise.

        Args:
            current_indoor: current indoor air temperature (°C)
            zone_water_temps: planned water supply temp per hour (len >= hours)
            weather_forecast: list of dicts with keys: outdoor_temp, wind_speed,
                irradiance and precipitation
            hours: number of hours to predict (default 24)

        Returns:
            list of {hour, predicted_indoor_temp, source} entries
        """
        from packages.ml.comfort_model import comfort_model

        curve = []
        indoor = current_indoor

        for h in range(hours):
            water_temp = zone_water_temps[h] if h < len(zone_water_temps) else zone_water_temps[-1]
            wx = weather_forecast[h] if h < len(weather_forecast) else weather_forecast[-1]
            outdoor = wx.get("outdoor_temp", 5.0)
            wind = wx.get("wind_speed", 3.0)
            irradiance = wx.get("irradiance", 0.0)
            precipitation = wx.get("precipitation", 0.0)
            hour_of_day = wx.get("hour", (h % 24))

            if comfort_model.is_trained:
                predicted = comfort_model.predict_indoor_temp(
                    zone_water_temp=water_temp,
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    hour=hour_of_day,
                    indoor_temp=indoor,
                    precipitation=precipitation,
                )
                if predicted is not None:
                    indoor = predicted
                    curve.append({
                        "hour": h + 1,
                        "predicted_indoor_temp": round(indoor, 1),
                        "source": "comfort_model",
                    })
                    continue

            # Fallback: linear rates
            # Heuristic: if water temp is significantly above indoor, assume active heating
            if water_temp > indoor + 5.0:
                rate = self._indoor_heating_rate(outdoor)
            else:
                rate = self._indoor_cooling_rate(outdoor)
                # Scale cooling by delta-T (Newton's law): slows near outdoor
                delta = max(indoor - outdoor, 0.0)
                scale = delta / 15.0 if delta < 15.0 else 1.0
                rate = rate * scale

            # Passive solar gain warms the home on sunny hours (0 at night/overcast).
            indoor += rate + _solar_gain_c(irradiance)
            # Indoor temp physically bounded
            indoor = max(indoor, outdoor)

            curve.append({
                "hour": h + 1,
                "predicted_indoor_temp": round(indoor, 1),
                "source": "linear_rates",
            })

        return curve

    def predict_temperature_curve(
        self,
        current_temp: float,
        outdoor_temp: float,
        hours: int = 24,
        target_temp: float | None = None,
        is_tank: bool = True,
    ) -> list[dict]:
        """
        Predict temperature evolution over time.

        If target_temp is set, simulates heating to target then standby loss.
        If target_temp is None, simulates only standby loss from current temp.

        Returns list of {hour, predicted_temp, state} entries.
        """
        curve = []
        temp = current_temp
        state = "standby"

        heating_rate = (
            self._tank_heating_rate(outdoor_temp)
            if is_tank
            else self._zone_heating_rate(outdoor_temp)
        )
        loss_rate = (
            self._tank_loss_rate(outdoor_temp)
            if is_tank
            else self._zone_loss_rate(outdoor_temp)
        )

        for h in range(hours):
            if target_temp is not None and temp < target_temp and state == "standby":
                state = "heating"

            if state == "heating":
                temp += heating_rate
                if target_temp is not None and temp >= target_temp:
                    temp = target_temp
                    state = "standby"
            else:
                # Scale loss by delta-T: loss slows as temp approaches outdoor
                # (Newton's law of cooling). Reference delta is 30°C.
                delta = max(temp - outdoor_temp, 0.0)
                scale = delta / 30.0 if delta < 30.0 else 1.0
                temp += loss_rate * scale
                # Can never cool below outdoor temperature
                temp = max(temp, outdoor_temp)

            curve.append({
                "hour": h + 1,
                "predicted_temp": round(temp, 1),
                "state": state,
            })

        return curve

    def predict_managed_tank_curve(
        self,
        current_temp: float,
        outdoor_temp: float,
        tank_target: float,
        tank_min_per_hour: list[float],
        hours: int = 24,
    ) -> list[dict]:
        """Simulate the tank under the optimizer's deadband control.

        Unlike :meth:`predict_temperature_curve` (which holds the tank pinned at
        ``target_temp``), this mirrors how the optimizer actually runs the tank:
        the temperature is allowed to coast down on standby loss until it reaches
        a per-hour *floor*, then reheats back to ``tank_target``. The floor is
        lower during off-peak/overnight hours, so the curve dips toward that
        off-peak minimum overnight and cycles within ``[floor, target]`` during
        the day — instead of looking like a flat line at the target.

        Args:
            current_temp: current tank temperature (°C).
            outdoor_temp: outdoor temperature (°C) driving heat-loss/heating rates.
            tank_target: reheat ceiling — the tank is heated back up to this.
            tank_min_per_hour: per-hour reheat floor (e.g. ``tank_min_temp`` during
                comfort hours, ``tank_min_temp_offpeak`` overnight). Index ``h`` is
                the floor for hour ``h``; the last value is reused if shorter.
            hours: number of hours to simulate.

        Returns:
            list of ``{hour, predicted_temp, state, floor}`` entries.
        """
        curve = []
        temp = current_temp
        state = "standby"
        heating_rate = self._tank_heating_rate(outdoor_temp)
        loss_rate = self._tank_loss_rate(outdoor_temp)

        for h in range(hours):
            floor = (
                tank_min_per_hour[h]
                if h < len(tank_min_per_hour)
                else (tank_min_per_hour[-1] if tank_min_per_hour else tank_target)
            )

            # Start reheating once the tank has coasted down to (or below) the
            # current floor; keep heating until it reaches the target.
            if state == "standby" and temp <= floor:
                state = "heating"

            if state == "heating":
                temp += heating_rate
                if temp >= tank_target:
                    temp = tank_target
                    state = "standby"
            else:
                delta = max(temp - outdoor_temp, 0.0)
                scale = delta / 30.0 if delta < 30.0 else 1.0
                temp += loss_rate * scale
                # Never coast below the active floor (or outdoor temp).
                temp = max(temp, floor, outdoor_temp)

            curve.append({
                "hour": h + 1,
                "predicted_temp": round(temp, 1),
                "state": state,
                "floor": round(floor, 1),
            })

        return curve

    def predict_managed_indoor_curve(
        self,
        current_indoor: float,
        indoor_target_per_hour: list[float],
        weather_forecast: list[dict],
        hours: int = 24,
    ) -> list[dict]:
        """Simulate indoor air tracking the comfort-schedule setpoint.

        Unlike :meth:`predict_indoor_curve` driven by a *constant* zone water
        temp (which produces a flat line that ignores the schedule), this
        mirrors how the optimizer runs the home: indoor is maintained near the
        comfort target during comfort hours and allowed to coast down toward the
        lower off-peak/overnight target, then reheated back up in the morning.
        The curve therefore dips toward the overnight setback and recovers,
        matching the stepped comfort schedule instead of looking flat.

        Args:
            current_indoor: current indoor air temperature (°C).
            indoor_target_per_hour: per-hour comfort setpoint — ``comfort_temp_target``
                during comfort hours, ``comfort_temp_min`` overnight/off-peak.
                Index ``h`` is the setpoint for hour ``h``; the last value is
                reused if the list is shorter than ``hours``.
            weather_forecast: list of dicts with key ``outdoor_temp`` driving the
                learned indoor heating/cooling rates.
            hours: number of hours to simulate.

        Returns:
            list of ``{hour, predicted_indoor_temp, target, state, source}`` entries.
        """
        curve = []
        indoor = current_indoor

        for h in range(hours):
            target = (
                indoor_target_per_hour[h]
                if h < len(indoor_target_per_hour)
                else (indoor_target_per_hour[-1] if indoor_target_per_hour else current_indoor)
            )
            wx = (
                weather_forecast[h]
                if h < len(weather_forecast)
                else (weather_forecast[-1] if weather_forecast else {})
            )
            outdoor = wx.get("outdoor_temp", 5.0)

            if indoor < target:
                # Below the active setpoint → heat up toward it.
                indoor += self._indoor_heating_rate(outdoor)
                if indoor > target:
                    indoor = target
                state = "heating"
            else:
                # At/above setpoint → coast down toward it (Newton's law toward
                # outdoor), but never below the active setpoint or outdoor temp.
                # Passive solar gain offsets the loss on sunny hours.
                cooling = self._indoor_cooling_rate(outdoor)
                delta = max(indoor - outdoor, 0.0)
                scale = delta / 15.0 if delta < 15.0 else 1.0
                indoor += cooling * scale + _solar_gain_c(wx.get("irradiance"))
                indoor = max(indoor, target, outdoor)
                state = "standby"

            curve.append({
                "hour": h + 1,
                "predicted_indoor_temp": round(indoor, 1),
                "target": round(target, 1),
                "state": state,
                "source": "managed_schedule",
            })

        return curve

    def predict_planned_tank_curve(
        self,
        current_temp: float,
        outdoor_temp: float,
        tank_target: float,
        dhw_minutes_per_hour: list[float],
        hours: int = 24,
    ) -> list[dict]:
        """Simulate the tank following the optimizer's *actual* DHW schedule.

        The MILP optimizer decides *when* to reheat domestic hot water based on
        price and COP, emitting ``force_dhw_on`` actions with a per-slot duration
        (``dhw_minutes``). This walks that schedule hour by hour: during hours
        with planned DHW minutes the tank is heated toward ``tank_target``
        (proportional to the share of the hour spent heating), and during all
        other hours it coasts on standby loss bounded by the outdoor temperature.

        Unlike :meth:`predict_managed_tank_curve` (a generic comfort-schedule
        deadband), this reflects the *real* plan, so the chart matches the
        scheduled hot-water cycles instead of an assumed overnight setback.

        Args:
            current_temp: current tank temperature (°C).
            outdoor_temp: outdoor temperature (°C) driving heating/loss rates.
            tank_target: reheat ceiling the DHW cycle heats up to.
            dhw_minutes_per_hour: minutes of planned DHW heating in each hour
                (index ``h`` is hour ``h``; values are clamped to ``[0, 60]``).
            hours: number of hours to simulate.

        Returns:
            list of ``{hour, predicted_temp, state, dhw_minutes}`` entries.
        """
        curve = []
        temp = current_temp
        heating_rate = self._tank_heating_rate(outdoor_temp)
        loss_rate = self._tank_loss_rate(outdoor_temp)

        def _coast(value: float, fraction: float) -> float:
            delta = max(value - outdoor_temp, 0.0)
            scale = delta / 30.0 if delta < 30.0 else 1.0
            value += loss_rate * scale * fraction
            return max(value, outdoor_temp)

        for h in range(hours):
            minutes = dhw_minutes_per_hour[h] if h < len(dhw_minutes_per_hour) else 0.0
            minutes = max(0.0, min(minutes, 60.0))

            if minutes > 0:
                heat_frac = minutes / 60.0
                # What the tank would do coasting on standby loss for the whole
                # hour (the "no heating" behaviour).
                coasted = _coast(temp, 1.0)
                # What the tank does when the DHW cycle reheats toward target.
                heated = temp + heating_rate * heat_frac
                if heated > tank_target:
                    heated = tank_target
                # The remainder of the hour (if any) coasts on standby loss.
                if heat_frac < 1.0:
                    heated = _coast(heated, 1.0 - heat_frac)
                # Heating can only *add* energy: if the tank is already above the
                # target, the DHW thermostat won't call for heat and the tank
                # simply coasts. Taking the max guarantees the "with heating"
                # curve never dips below the "no heating" curve (the clamp to
                # ``tank_target`` must never pull an already-hot tank down).
                temp = max(heated, coasted)
                state = "heating"
            else:
                temp = _coast(temp, 1.0)
                state = "standby"

            curve.append({
                "hour": h + 1,
                "predicted_temp": round(temp, 1),
                "state": state,
                "dhw_minutes": round(minutes, 1),
            })

        return curve

    def optimal_start_time(
        self,
        current_temp: float,
        target_temp: float,
        deadline: dt.datetime,
        outdoor_temp: float,
        is_tank: bool = True,
    ) -> dt.datetime:
        """
        Calculate the latest time to start heating to reach target by deadline.

        This is the key function for the optimizer: instead of a fixed window,
        it calculates precisely when to start based on learned thermal dynamics.
        """
        prediction = (
            self.predict_tank_heating_time(current_temp, target_temp, outdoor_temp)
            if is_tank
            else self.predict_zone_heating_time(current_temp, target_temp, outdoor_temp)
        )

        # Add 15% buffer for safety
        buffer_minutes = prediction.estimated_minutes * 0.15
        total_minutes = prediction.estimated_minutes + buffer_minutes

        start_time = deadline - dt.timedelta(minutes=total_minutes)
        return start_time

    # --- Internal rate calculations ---

    def _tank_heating_rate(self, outdoor_temp: float) -> float:
        """Tank heating rate (°C/hour) adjusted for outdoor temp."""
        rate = (
            self.params.tank_heating_rate
            + self.params.tank_heating_outdoor_factor * outdoor_temp
        )
        return max(1.0, min(30.0, rate))

    def _tank_loss_rate(self, outdoor_temp: float) -> float:
        """Tank standby loss rate (°C/hour, negative)."""
        loss = (
            self.params.tank_standby_loss
            + self.params.tank_loss_outdoor_factor * outdoor_temp
        )
        return max(-3.0, min(0.0, loss))  # Capped to physically plausible range

    def _zone_heating_rate(self, outdoor_temp: float) -> float:
        """Zone heating rate (°C/hour)."""
        # Zones heat slower when it's very cold outside
        rate = self.params.zone_heating_rate + 0.03 * outdoor_temp
        return max(0.5, min(15.0, rate))

    def _zone_loss_rate(self, outdoor_temp: float) -> float:
        """Zone standby loss rate (°C/hour, negative)."""
        loss = (
            self.params.zone_standby_loss
            + self.params.zone_loss_outdoor_factor * outdoor_temp
        )
        return max(-3.0, min(0.0, loss))  # Capped to physically plausible range

    def _indoor_heating_rate(self, outdoor_temp: float) -> float:
        """Indoor air heating rate (°C/hour) when zone is actively heating."""
        rate = (
            self.params.indoor_heating_rate
            + self.params.indoor_heating_outdoor_factor * outdoor_temp
        )
        return max(0.1, min(3.0, rate))

    def _indoor_cooling_rate(self, outdoor_temp: float) -> float:
        """Indoor air cooling rate (°C/hour, negative) during standby.

        Clamped to [-1.0, 0.0]: a well-insulated house loses ~0.3°C/h,
        a poorly insulated one up to ~0.8°C/h.  Values beyond -1.0 are
        usually calibration noise, not real thermal behaviour.
        """
        loss = (
            self.params.indoor_cooling_rate
            + self.params.indoor_cooling_outdoor_factor * outdoor_temp
        )
        return max(-1.0, min(0.0, loss))


# Singleton instance
thermal_model = ThermalModel()

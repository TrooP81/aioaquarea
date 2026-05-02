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
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import structlog
from sqlalchemy import select, and_, func

from packages.core.database import get_session
from packages.core.models import DeviceStatusRecord

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
            # Get last 7 days of device status, ordered by time
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
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

        for i in range(1, len(records)):
            prev = records[i - 1]
            curr = records[i]

            # Time gap in hours
            dt_hours = (curr.ts - prev.ts).total_seconds() / 3600.0
            if dt_hours <= 0 or dt_hours > 2.0:
                # Skip gaps larger than 2 hours (data gap)
                continue

            # FILTER: Skip defrost intervals — temperature readings are unreliable
            if getattr(curr, 'defrost_active', None) or getattr(prev, 'defrost_active', None):
                defrost_filtered += 1
                continue

            outdoor = curr.outdoor_temp if curr.outdoor_temp is not None else 10.0
            curr_direction = getattr(curr, 'direction', None)

            # Tank temperature delta
            if (
                prev.tank_temp is not None
                and curr.tank_temp is not None
                and prev.tank_temp > 0
                and curr.tank_temp > 0
            ):
                tank_delta = (curr.tank_temp - prev.tank_temp) / dt_hours

                if tank_delta > 0.5:
                    # Tank is being actively heated
                    # If direction data is available, only count when compressor is on WATER
                    if curr_direction is None or curr_direction == "WATER":
                        tank_heating_deltas.append((tank_delta, outdoor))
                elif tank_delta < -0.1:
                    # Tank is cooling (standby loss) — only when NOT actively heating
                    if curr_direction is None or curr_direction in ("IDLE", "PUMP"):
                        tank_cooling_deltas.append((tank_delta, outdoor))

            # Zone 1 temperature delta
            if (
                prev.zone1_temp is not None
                and curr.zone1_temp is not None
                and prev.zone1_temp > 0
                and curr.zone1_temp > 0
            ):
                zone_delta = (curr.zone1_temp - prev.zone1_temp) / dt_hours

                if zone_delta > 0.2:
                    # Zone heating — only count when compressor is on PUMP
                    if curr_direction is None or curr_direction == "PUMP":
                        zone_heating_deltas.append((zone_delta, outdoor))
                elif zone_delta < -0.1:
                    # Zone cooling — only when NOT actively heating zone
                    if curr_direction is None or curr_direction in ("IDLE", "WATER"):
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

        # Tank standby loss
        if len(tank_cooling_deltas) >= 5:
            deltas = np.array([d[0] for d in tank_cooling_deltas])
            outdoors = np.array([d[1] for d in tank_cooling_deltas])

            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.tank_loss_outdoor_factor = float(coeffs[0])
                self.params.tank_standby_loss = float(coeffs[1])
            else:
                self.params.tank_standby_loss = float(np.mean(deltas))

        # Zone heating rate
        if len(zone_heating_deltas) >= 5:
            deltas = np.array([d[0] for d in zone_heating_deltas])
            outdoors = np.array([d[1] for d in zone_heating_deltas])

            if np.std(outdoors) > 0:
                coeffs = np.polyfit(outdoors, deltas, 1)
                self.params.zone_heating_rate = float(coeffs[1])
            else:
                self.params.zone_heating_rate = float(np.mean(deltas))

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

        self.params.last_calibrated = dt.datetime.now(dt.timezone.utc)
        self.params.sample_count = len(records)
        self.params.defrost_intervals_filtered = defrost_filtered
        self.params.dhw_heating_samples = len(tank_heating_deltas)
        self.params.zone_compressor_samples = len(zone_heating_deltas)

        logger.info(
            f"Thermal model calibrated: tank_heating={self.params.tank_heating_rate:.2f}°C/h, "
            f"tank_loss={self.params.tank_standby_loss:.2f}°C/h, "
            f"zone_heating={self.params.zone_heating_rate:.2f}°C/h, "
            f"defrost_filtered={defrost_filtered}"
        )

        return {
            "status": "calibrated",
            "samples": len(records),
            "tank_heating_samples": len(tank_heating_deltas),
            "tank_cooling_samples": len(tank_cooling_deltas),
            "zone_heating_samples": len(zone_heating_deltas),
            "zone_cooling_samples": len(zone_cooling_deltas),
            "defrost_intervals_filtered": defrost_filtered,
            "params": {
                "tank_heating_rate": self.params.tank_heating_rate,
                "tank_heating_outdoor_factor": self.params.tank_heating_outdoor_factor,
                "tank_standby_loss": self.params.tank_standby_loss,
                "tank_loss_outdoor_factor": self.params.tank_loss_outdoor_factor,
                "zone_heating_rate": self.params.zone_heating_rate,
                "zone_standby_loss": self.params.zone_standby_loss,
                "zone_loss_outdoor_factor": self.params.zone_loss_outdoor_factor,
            },
        }

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
        return max(1.0, rate)  # At least 1°C/hour

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
        return max(0.5, rate)

    def _zone_loss_rate(self, outdoor_temp: float) -> float:
        """Zone standby loss rate (°C/hour, negative)."""
        loss = (
            self.params.zone_standby_loss
            + self.params.zone_loss_outdoor_factor * outdoor_temp
        )
        return max(-3.0, min(0.0, loss))  # Capped to physically plausible range


# Singleton instance
thermal_model = ThermalModel()

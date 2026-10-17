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
import asyncio
from bisect import bisect_right
import math
import time
from dataclasses import asdict, dataclass, field, fields, replace
from typing import TYPE_CHECKING, Optional

import numpy as np
import structlog
from sqlalchemy import select

from packages.core.database import get_session
from packages.core.heating_evidence import classify_space_heating
from packages.core.models import (
    DeviceStatusRecord,
    IndoorTempReading,
    WeatherRecord,
)
from packages.ml.models_common import MODEL_DIR, is_finite_number

if TYPE_CHECKING:
    from packages.ml.lifecycle import Lifecycle

logger = structlog.get_logger(__name__)

# A successful tank calibration is not evidence that the separate indoor-air
# fallback has been learned. Keep confidence tied to each sub-model's samples.
MIN_LEARNED_RATE_SAMPLES = 20
THERMAL_MODEL_ARTIFACT = "thermal_params_v1.pkl"
THERMAL_MODEL_SCHEMA_VERSION = 5
THERMAL_LABEL_EMBARGO = dt.timedelta(hours=6)
RATE_VALIDATION_HOLDOUT_FRACTION = 0.3
RATE_MIN_HOLDOUT_SAMPLES = 3
RATE_MIN_RELATIVE_SKILL = 0.05
RATE_MIN_OUTDOOR_SPAN_FOR_SLOPE_C = 15.0
RATE_OUTDOOR_EXTRAPOLATION_MARGIN_C = 3.0
SEGMENT_MAX_GAP_S = 1200
HEATING_SEGMENT_HOURS = (0.25, 3.0)
STANDBY_SEGMENT_HOURS = (0.5, 6.0)
INDOOR_HEATING_RATE_MIN_C_PER_H = 0.1
INDOOR_HEATING_RATE_MAX_C_PER_H = 3.0
INDOOR_COOLING_RATE_MIN_C_PER_H = -1.0

# component -> (base parameter, outdoor-factor parameter, runtime rate method)
_RATE_COMPONENTS = {
    "tank_heating": ("tank_heating_rate", "tank_heating_outdoor_factor", "_tank_heating_rate"),
    "tank_cooling": ("tank_standby_loss", "tank_loss_outdoor_factor", "_tank_loss_rate"),
    "zone_heating": ("zone_heating_rate", "zone_heating_outdoor_factor", "_zone_heating_rate"),
    "zone_cooling": ("zone_standby_loss", "zone_loss_outdoor_factor", "_zone_loss_rate"),
    "indoor_heating": (
        "indoor_heating_rate",
        "indoor_heating_outdoor_factor",
        "_indoor_heating_rate",
    ),
    "indoor_cooling": (
        "indoor_cooling_rate",
        "indoor_cooling_outdoor_factor",
        "_indoor_cooling_rate",
    ),
}


def _validate_trajectory_inputs(
    hours: int,
    *values: object,
    series: tuple[list[float], ...] = (),
    weather: list[dict] | None = None,
) -> None:
    if not is_finite_number(hours) or hours < 0 or int(hours) != hours:
        raise ValueError("invalid_thermal_forecast_horizon")
    if not all(is_finite_number(value) for value in values):
        raise ValueError("nonfinite_thermal_forecast_input")
    if any(not is_finite_number(value) for sequence in series for value in sequence[:hours]):
        raise ValueError("nonfinite_thermal_forecast_input")
    if weather is not None:
        if hours > 0 and not weather:
            raise ValueError("weather_forecast_unavailable")
        for point in weather[:hours]:
            if not isinstance(point, dict) or not is_finite_number(point.get("outdoor_temp")):
                raise ValueError("nonfinite_thermal_weather_input")
            for key in (
                "wind_speed",
                "irradiance",
                "precipitation",
                "humidity",
                "cloud_cover",
                "hour",
            ):
                value = point.get(key)
                if value is not None and not is_finite_number(value):
                    raise ValueError("nonfinite_thermal_weather_input")


def assumed_heating_efficiency(outdoor_temp: float) -> float:
    """Return an assumed planning ratio, not measured heat-pump performance."""
    return max(1.5, min(6.0, 3.5 + 0.1 * outdoor_temp))


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
    zone_heating_outdoor_factor: float = 0.03
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
    # Per-component validation result from the latest calibration. Persisted so
    # the API process can explain a rejected candidate calibrated by optimizer.
    calibration_status: dict[str, str] = field(default_factory=dict)
    # Outdoor range (°C) each validated rate was observed in; defaults apply outside it.
    rate_outdoor_ranges: dict[str, list[float]] = field(default_factory=dict)


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
        self.validation: dict[str, dict] = {}
        self._qualified_reference = None
        self._committed_checked_at: float | None = None
        self._committed_refresh_lock = asyncio.Lock()

    def reset(self) -> None:
        """Discard learned calibration and return to default thermal parameters."""
        self.params = ThermalParams()
        self.validation = {}
        self._qualified_reference = None
        self._committed_checked_at = None

    @staticmethod
    def _fit_rate(samples: list[tuple[float, float, float]]) -> tuple[float, float]:
        """MAD-trimmed rate fit; an outdoor slope only when outdoor temperatures vary widely."""
        rates = np.array([sample[0] for sample in samples], dtype=float)
        outdoors = np.array([sample[1] for sample in samples], dtype=float)
        median = float(np.median(rates))
        spread = max(0.1, float(np.median(np.abs(rates - median))) * 1.4826)
        keep = np.abs(rates - median) <= 3.0 * spread
        rates, outdoors = rates[keep], outdoors[keep]
        if np.ptp(outdoors) >= RATE_MIN_OUTDOOR_SPAN_FOR_SLOPE_C:
            factor, base = np.polyfit(outdoors, rates, 1)
            return float(base), float(factor)
        return float(np.mean(rates)), 0.0

    @staticmethod
    def _partition_rate_samples(samples: list) -> tuple[list, list]:
        split = int(len(samples) * (1.0 - RATE_VALIDATION_HOLDOUT_FRACTION))
        train, holdout = samples[:split], samples[split:]
        if holdout and len(holdout[0]) == 5:
            embargo_cutoff = holdout[0][3] - THERMAL_LABEL_EMBARGO
            train = [sample for sample in train if sample[4] <= embargo_cutoff]
        return train, holdout

    def _validate_rate_component(
        self, component: str, samples: list[tuple[float, float, float]]
    ) -> None:
        """Adopt a fitted rate only if it beats both persistence and the default on later data.

        Samples are chronological ``(rate_c_per_h, outdoor_c, hours)``. The candidate is
        fitted on the earliest 70% and scored on the rest through the same clamped runtime
        rate used for control; otherwise the safe default is kept.
        """
        base_name, factor_name, runtime = _RATE_COMPONENTS[component]
        defaults = ThermalParams()
        setattr(self.params, base_name, getattr(defaults, base_name))
        setattr(self.params, factor_name, getattr(defaults, factor_name))
        self.params.rate_outdoor_ranges.pop(component, None)
        train, holdout = self._partition_rate_samples(samples)
        if len(train) < MIN_LEARNED_RATE_SAMPLES or len(holdout) < RATE_MIN_HOLDOUT_SAMPLES:
            self.params.calibration_status[component] = "insufficient_samples"
            self.validation[component] = {
                "samples": len(samples),
                "train_samples": len(train),
                "holdout_samples": len(holdout),
                "minimum_fit_segments": MIN_LEARNED_RATE_SAMPLES,
            }
            return

        def holdout_mae(params: ThermalParams) -> float:
            probe = ThermalModel()
            probe.params = params
            rate = getattr(probe, runtime)
            return float(
                np.mean(
                    [
                        abs(rate(outdoor) * hours - actual * hours)
                        for actual, outdoor, hours in (sample[:3] for sample in holdout)
                    ]
                )
            )

        base, factor = self._fit_rate(train)
        outdoors = [sample[1] for sample in train]
        evaluated_params = replace(
            defaults,
            **{base_name: base, factor_name: factor},
            rate_outdoor_ranges={component: [min(outdoors), max(outdoors)]},
        )
        candidate_mae = holdout_mae(evaluated_params)
        default_mae = holdout_mae(defaults)
        persistence_mae = float(np.mean([abs(sample[0] * sample[2]) for sample in holdout]))
        self.validation[component] = {
            "train_samples": len(train),
            "holdout_samples": len(holdout),
            "candidate_mae_c": round(candidate_mae, 4),
            "default_mae_c": round(default_mae, 4),
            "persistence_mae_c": round(persistence_mae, 4),
            "evaluated_parameters": [base, factor],
            "embargo_hours": THERMAL_LABEL_EMBARGO.total_seconds() / 3600,
            "holdout_features": [[sample[1], sample[2]] for sample in holdout],
            "holdout_targets": [sample[0] * sample[2] for sample in holdout],
            "fit_label_end": train[-1][4].isoformat() if len(train[-1]) == 5 else None,
            "holdout_issue_start": holdout[0][3].isoformat() if len(holdout[0]) == 5 else None,
        }
        probe = ThermalModel()
        probe.params = evaluated_params
        self.validation[component]["holdout_predictions"] = [
            getattr(probe, runtime)(sample[1]) * sample[2] for sample in holdout
        ]
        if candidate_mae < min(default_mae, persistence_mae) * (1.0 - RATE_MIN_RELATIVE_SKILL):
            setattr(self.params, base_name, base)
            setattr(self.params, factor_name, factor)
            self.params.rate_outdoor_ranges[component] = [min(outdoors), max(outdoors)]
            self.params.calibration_status[component] = "validated"
        else:
            self.params.calibration_status[component] = "rejected_no_skill_over_baseline"

    @staticmethod
    def _dhw_heating_active(record) -> bool:
        action = getattr(record, "device_action", None)
        if isinstance(action, str):
            return action == "HEATING_WATER"
        return getattr(record, "direction", None) == "WATER"

    @staticmethod
    def _zone_heating_active(record) -> bool:
        action = getattr(record, "device_action", None)
        if isinstance(action, str):
            return action == "HEATING"
        return getattr(record, "direction", None) == "PUMP"

    @staticmethod
    def _rate_segments(records, field_name: str, active, heating: bool, outdoor_at) -> list:
        """Whole heating or standby runs as ``(rate, outdoor, hours)`` samples.

        Rates over a complete run average out whole-degree sensor quantisation and do not
        depend on the sign of the change, unlike consecutive-step deltas.  A heating run is
        measured to its peak because the controller stops at the target.
        """
        low, high = HEATING_SEGMENT_HOURS if heating else STANDBY_SEGMENT_HOURS
        segments: list[tuple[float, float, float]] = []

        def flush(run: list) -> None:
            if len(run) < 2:
                return
            hours = (run[-1].ts - run[0].ts).total_seconds() / 3600.0
            if not low <= hours <= high:
                return
            start = float(getattr(run[0], field_name))
            if heating:
                peak = max(run, key=lambda record: float(getattr(record, field_name)))
                rise_hours = (peak.ts - run[0].ts).total_seconds() / 3600.0
                if rise_hours >= low:
                    hours = rise_hours
                    rate = (float(getattr(peak, field_name)) - start) / hours
                else:
                    rate = 0.0
            else:
                rate = (float(getattr(run[-1], field_name)) - start) / hours
            fallback = run[0].outdoor_temp if run[0].outdoor_temp is not None else 10.0
            segments.append(
                (rate, float(outdoor_at(run[0].ts, fallback)), hours, run[0].ts, run[-1].ts)
            )

        run: list = []
        for record in records:
            value = getattr(record, field_name, None)
            usable = (
                value is not None
                and value > 0
                and not getattr(record, "defrost_active", None)
                and active(record) == heating
            )
            device_id = getattr(record, "device_id", None)
            previous_device = getattr(run[-1], "device_id", None) if run else None
            if run and (
                not usable
                or (record.ts - run[-1].ts).total_seconds() > SEGMENT_MAX_GAP_S
                or (
                    isinstance(device_id, str)
                    and isinstance(previous_device, str)
                    and device_id != previous_device
                )
            ):
                flush(run)
                run = []
            if usable:
                run.append(record)
        flush(run)
        return segments

    @staticmethod
    def _artifact_path():
        return MODEL_DIR / THERMAL_MODEL_ARTIFACT

    def save(self) -> None:
        """Persist calibrated parameters for API, poller, and optimizer processes."""
        from packages.ml.safe_persistence import safe_dump

        safe_dump(
            {
                "schema_version": THERMAL_MODEL_SCHEMA_VERSION,
                "params": asdict(self.params),
                "validation": self.validation,
            },
            self._artifact_path(),
        )

    @staticmethod
    def _decode_params(params: object) -> ThermalParams:
        if not isinstance(params, dict):
            raise ValueError("missing thermal parameter payload")
        allowed = {field.name for field in fields(ThermalParams)}
        values = {key: value for key, value in params.items() if key in allowed}
        calibrated = values.get("last_calibrated")
        if isinstance(calibrated, str):
            calibrated_at = dt.datetime.fromisoformat(calibrated)
            values["last_calibrated"] = (
                calibrated_at.replace(tzinfo=dt.timezone.utc)
                if calibrated_at.tzinfo is None
                else calibrated_at
            )
        elif calibrated is not None and not isinstance(calibrated, dt.datetime):
            raise ValueError("invalid thermal calibration timestamp")
        return ThermalParams(**values)

    async def arefresh_committed(
        self, *, lifecycle: "Lifecycle | None" = None, scope: dict | None = None
    ) -> bool:
        """Read lifecycle authority only; unavailable authority means default rates."""
        from packages.ml.lifecycle import Lifecycle, reference_is_qualified
        from packages.ml.forecast_quality import (
            check_exact_fit,
            materialize_native_inputs,
            native_scope,
        )

        lifecycle = lifecycle if lifecycle is not None else Lifecycle()
        try:
            reference = (await lifecycle.read()).reference("thermal")
            if not reference_is_qualified(reference):
                raise ValueError("thermal_not_qualified")
            if scope is None:
                inputs = await materialize_native_inputs(
                    "thermal",
                    now=dt.datetime.now(dt.timezone.utc),
                    device_id=reference.manifest["device_id"],
                    capabilities=tuple(reference.manifest["capabilities"]),
                )
                if inputs is None:
                    raise ValueError("current_thermal_scope_unavailable")
                scope = native_scope(inputs)
            if not reference_is_qualified(reference, scope=scope):
                raise ValueError("thermal_scope_mismatch")
            payload = await lifecycle.load_committed(
                "thermal", expected_hash=reference.artifact_hash
            )
            if not isinstance(payload, dict) or payload.get("schema_version") != 5:
                raise ValueError("invalid committed thermal schema")
            await asyncio.to_thread(check_exact_fit, "thermal", payload)
            if (await lifecycle.read()).reference("thermal") != reference:
                raise ValueError("thermal_authority_changed")
            loaded = self._decode_params(payload.get("params"))
            defaults = ThermalParams()
            for component, (base_key, factor_key, _) in _RATE_COMPONENTS.items():
                if (
                    loaded.last_calibrated is None
                    or loaded.calibration_status.get(component) != "validated"
                    or component not in reference.qualification_scope["capabilities"]
                ):
                    setattr(loaded, base_key, getattr(defaults, base_key))
                    setattr(loaded, factor_key, getattr(defaults, factor_key))
        except Exception as error:
            self.reset()
            logger.warning("thermal_committed_unavailable", error_type=type(error).__name__)
            return False
        self.params = loaded
        self.validation = {}
        self._qualified_reference = reference
        return True

    async def arefresh_if_changed(self, *, force: bool = False) -> bool:
        async with self._committed_refresh_lock:
            checked = self._committed_checked_at
            if not force and checked is not None and 0 <= time.monotonic() - checked < 60:
                available = self.load_latest()
                self._committed_checked_at = checked
                return available
            available = await self.arefresh_committed()
            self._committed_checked_at = time.monotonic()
            return available

    def load_latest(self) -> bool:
        from packages.ml.lifecycle import reference_is_qualified

        if not reference_is_qualified(self._qualified_reference):
            self.reset()
            return False
        return True

    def confidence_for(self, component: str) -> str:
        """Return evidence-based confidence for one thermal sub-model."""
        from packages.ml.lifecycle import reference_is_qualified

        reference = self._qualified_reference
        if (
            not reference_is_qualified(reference)
            or component not in reference.manifest.get("capabilities", ())
            or component not in (reference.qualification_scope or {}).get("capabilities", ())
        ):
            return "default"
        if self.params.last_calibrated is None:
            return "default"
        validated = self.params.calibration_status.get(component) == "validated"
        return "learned" if validated else "default"

    @staticmethod
    def _nearest_prior_index_in_gap(
        times: np.ndarray,
        current_index: int,
        min_gap_seconds: float,
        max_gap_seconds: float,
    ) -> int | None:
        """Return the closest earlier sample in the requested time window.

        Calibration needs a long enough gap to overcome sensor quantisation,
        but it should use the *closest* valid sample.  Pairing every new row
        with the oldest sample still inside a two-hour window smears mode
        changes and biases the learned heating/cooling rates.
        """
        if current_index <= 0:
            return None
        target = times[current_index] - min_gap_seconds
        index = int(np.searchsorted(times[:current_index], target, side="right")) - 1
        if index < 0:
            return None
        gap = times[current_index] - times[index]
        return index if gap <= max_gap_seconds else None

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
            weather_result = await session.execute(
                select(WeatherRecord)
                .where(WeatherRecord.ts >= since - dt.timedelta(hours=2))
                .order_by(WeatherRecord.ts)
            )
            weather_rows = weather_result.scalars().all()

        if len(records) < self.params.min_samples:
            minimum = self.params.min_samples
            self.reset()
            self.params.min_samples = minimum
            logger.warning(
                f"Insufficient data for calibration: {len(records)} records "
                f"(need {self.params.min_samples})"
            )
            return {
                "status": "insufficient_data",
                "records": len(records),
                "min_required": self.params.min_samples,
            }

        weather_by_hour = {
            row.ts.replace(minute=0, second=0, microsecond=0): float(row.temperature)
            for row in weather_rows
            if row.temperature is not None
        }

        def weather_temperature_at(timestamp: dt.datetime, fallback: float) -> float:
            hour = timestamp.replace(minute=0, second=0, microsecond=0)
            candidates = [
                (abs(offset), weather_by_hour.get(hour + dt.timedelta(hours=offset)))
                for offset in (-2, -1, 0, 1, 2)
            ]
            available = [(gap, value) for gap, value in candidates if value is not None]
            return min(available, key=lambda item: item[0])[1] if available else fallback

        defrost_filtered = sum(1 for record in records if getattr(record, "defrost_active", None))
        segments = {
            "tank_heating": (self._dhw_heating_active, "tank_temp", True),
            "tank_cooling": (self._dhw_heating_active, "tank_temp", False),
            "zone_heating": (self._zone_heating_active, "zone1_temp", True),
            "zone_cooling": (self._zone_heating_active, "zone1_temp", False),
        }
        samples = {
            component: self._rate_segments(
                records, field_name, active, heating, weather_temperature_at
            )
            for component, (active, field_name, heating) in segments.items()
        }

        self.params.calibration_status = {}
        self.validation = {}
        for component, component_samples in samples.items():
            self._validate_rate_component(component, component_samples)
        tank_heating_deltas = samples["tank_heating"]
        tank_cooling_deltas = samples["tank_cooling"]
        zone_heating_deltas = samples["zone_heating"]
        zone_cooling_deltas = samples["zone_cooling"]

        # --- Indoor air temperature rates (from SmartThings data) ---
        indoor_heating_deltas, indoor_cooling_deltas = await self._calibrate_indoor_rates(
            since, records
        )

        self.params.last_calibrated = dt.datetime.now(dt.timezone.utc)
        self.params.sample_count = len(records)
        self.params.defrost_intervals_filtered = defrost_filtered
        self.params.dhw_heating_samples = len(tank_heating_deltas)
        self.params.zone_compressor_samples = len(zone_heating_deltas)
        self.save()

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
            "validation": self.validation,
            "calibration_status": dict(self.params.calibration_status),
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
        *,
        values: dict[str, str] | None = None,
    ) -> tuple[list, list]:
        """Learn indoor heating/standby rates, keeping only those that beat persistence.

        Rates come from one consistent indoor series (the comfort reference sensor
        or a five-minute cross-room median): pairing readings from different rooms
        turns fixed sensor offsets into fictitious rates.  Every interval is kept
        whatever its sign, because classifying rises as heating and falls as
        cooling manufactures rates out of sensor noise.  Each component must then
        predict a chronological hold-out better than "no change" or it is rejected
        and the safe default is kept.

        Returns (heating_samples, standby_samples) for calibration stats.
        """
        from packages.ml.comfort_model import ComfortModel
        from packages.core.settings_service import get_all_settings
        from packages.poller.smartthings import get_selected_device_ids

        as_of = dt.datetime.now(dt.timezone.utc)
        values = await get_all_settings() if values is None else values
        selected_ids = await get_selected_device_ids(values=values)
        reference_sensor_id = str(values.get("comfort_reference_sensor_id") or "").strip()
        if reference_sensor_id and selected_ids and reference_sensor_id not in selected_ids:
            return self._fit_indoor_rates([], device_records, [])
        query_sensor_ids = [reference_sensor_id] if reference_sensor_id else selected_ids
        async with get_session() as session:
            query = (
                select(IndoorTempReading)
                .where(IndoorTempReading.timestamp >= since)
                .where(IndoorTempReading.timestamp <= as_of)
                .where(IndoorTempReading.is_stale == False)  # noqa: E712
                .order_by(IndoorTempReading.timestamp)
                .limit(50_001)
            )
            if query_sensor_ids:
                query = query.where(IndoorTempReading.device_id.in_(query_sensor_ids))
            result = await session.execute(query)
            raw_readings = result.scalars().all()
            weather_result = await session.execute(
                select(WeatherRecord)
                .where(WeatherRecord.ts >= since - dt.timedelta(hours=2))
                .where(WeatherRecord.ts <= as_of)
                .order_by(WeatherRecord.ts)
                .limit(50_001)
            )
            weather_rows = weather_result.scalars().all()
            if max(len(raw_readings), len(weather_rows)) > 50_000:
                raise ValueError("Thermal indoor materialization overflow")

        readings, _, _ = ComfortModel._select_indoor_observations(
            raw_readings, reference_sensor_id, selected_ids=selected_ids, as_of=as_of
        )
        return self._fit_indoor_rates(readings, device_records, weather_rows)

    @staticmethod
    def _extract_indoor_rate_samples(readings, records, weather) -> tuple[list, list]:
        device_records, weather_rows = records, weather
        heating_samples: list = []
        standby_samples: list = []
        if len(readings) < 2 or not device_records:
            return heating_samples, standby_samples
        device_records = sorted(device_records, key=lambda row: row.ts)
        status_times = [row.ts for row in device_records]

        weather_by_hour = {
            row.ts.replace(minute=0, second=0, microsecond=0): float(row.temperature)
            for row in weather_rows
            if row.temperature is not None
        }

        MIN_GAP_S = 900  # 15 min minimum between paired readings
        MAX_GAP_S = 7200  # 2 h maximum

        reading_times = np.array([r.timestamp.timestamp() for r in readings])
        for i in range(1, len(readings)):
            prev_index = ThermalModel._nearest_prior_index_in_gap(
                reading_times, i, MIN_GAP_S, MAX_GAP_S
            )
            if prev_index is None:
                continue
            curr_reading = readings[i]
            prev_reading = readings[prev_index]
            heating_now, status = ThermalModel._continuous_indoor_state(
                device_records,
                prev_reading.timestamp,
                curr_reading.timestamp,
                status_times=status_times,
            )
            if heating_now is None:
                continue

            dt_hours = (reading_times[i] - reading_times[prev_index]) / 3600.0
            rate = (curr_reading.temperature - prev_reading.temperature) / dt_hours
            weather_hour = curr_reading.timestamp.replace(minute=0, second=0, microsecond=0)
            weather_candidates = [
                (abs(offset), weather_by_hour.get(weather_hour + dt.timedelta(hours=offset)))
                for offset in (-2, -1, 0, 1, 2)
            ]
            weather_available = [
                (gap_hours, value) for gap_hours, value in weather_candidates if value is not None
            ]
            outdoor = (
                min(weather_available, key=lambda item: item[0])[1]
                if weather_available
                else status.outdoor_temp
                if status.outdoor_temp is not None
                else 10.0
            )
            (heating_samples if heating_now else standby_samples).append(
                (
                    float(rate),
                    float(outdoor),
                    float(dt_hours),
                    prev_reading.timestamp,
                    curr_reading.timestamp,
                )
            )
        return heating_samples, standby_samples

    def _fit_indoor_rates(self, readings, device_records, weather_rows) -> tuple[list, list]:
        heating_samples, standby_samples = self._extract_indoor_rate_samples(
            readings, device_records, weather_rows
        )
        self._record_indoor_validation(heating_samples, standby_samples)
        if len(readings) < 2 or not device_records:
            return heating_samples, standby_samples
        logger.info(
            "indoor_rates_calibrated",
            heating_samples=len(heating_samples),
            standby_samples=len(standby_samples),
            heating_status=self.params.calibration_status.get("indoor_heating"),
            standby_status=self.params.calibration_status.get("indoor_cooling"),
            heating_rate=self.params.indoor_heating_rate,
            cooling_rate=self.params.indoor_cooling_rate,
        )
        return heating_samples, standby_samples

    @staticmethod
    def _continuous_indoor_state(
        records, start: dt.datetime, end: dt.datetime, *, status_times=None
    ):
        ordered = sorted(records, key=lambda row: row.ts) if status_times is None else records
        status_times = [row.ts for row in ordered] if status_times is None else status_times
        first = bisect_right(status_times, start) - 1
        last = bisect_right(status_times, end)
        if first < 0 or (start - ordered[first].ts).total_seconds() > 900:
            return None, None
        interval = ordered[first:last]
        if (end - interval[-1].ts).total_seconds() > 900:
            return None, None
        devices = {
            row.device_id for row in interval if isinstance(getattr(row, "device_id", None), str)
        }
        if len(devices) > 1 or any(
            (right.ts - left.ts).total_seconds() > 900
            for left, right in zip(interval, interval[1:])
        ):
            return None, None
        states = []
        for row in interval:
            persisted = getattr(row, "space_heating_active", None)
            reason = getattr(row, "space_heating_evidence", None)
            if reason == "not_confirmed" or getattr(row, "defrost_active", False) is True:
                return None, None
            if isinstance(persisted, bool):
                states.append(persisted)
            else:
                evidence = classify_space_heating(
                    **{
                        key: getattr(row, key, None)
                        for key in (
                            "operation_status",
                            "mode",
                            "direction",
                            "pump_duty",
                            "device_action",
                            "defrost_active",
                            "zone1_operation_status",
                            "zone2_operation_status",
                        )
                    }
                )
                if evidence.code == "not_confirmed":
                    return None, None
                states.append(evidence.active)
        if len(set(states)) != 1:
            return None, None
        return states[0], interval[-1]

    def _record_indoor_validation(
        self,
        heating_samples: list[tuple[float, float, float]],
        standby_samples: list[tuple[float, float, float]],
    ) -> None:
        self._validate_rate_component("indoor_heating", heating_samples)
        self._validate_rate_component("indoor_cooling", standby_samples)
        self.params.indoor_heating_samples = len(heating_samples)
        self.params.indoor_cooling_samples = len(standby_samples)

    def predict_tank_heating_time(
        self, current_temp: float, target_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """
        Predict how many minutes it takes to heat the tank from current to target temp.

        The heating rate depends on outdoor temperature (COP effect):
        - Warmer outdoor → better COP → faster heating
        """
        rate = self._tank_heating_rate(outdoor_temp)
        confidence = self.confidence_for("tank_heating")

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
                confidence=confidence,
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence=confidence,
        )

    def predict_tank_cooling_time(
        self, current_temp: float, min_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """
        Predict how many minutes until the tank cools from current to minimum temp.

        This tells us "how long can we delay heating before the tank gets too cold?"
        """
        loss_rate = self._tank_loss_rate(outdoor_temp)
        confidence = self.confidence_for("tank_cooling")

        if loss_rate >= 0:
            # Not cooling — infinite time
            return ThermalPrediction(
                current_temp=current_temp,
                target_temp=min_temp,
                outdoor_temp=outdoor_temp,
                estimated_minutes=float("inf"),
                heating_rate_per_hour=loss_rate,
                confidence=confidence,
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
                confidence=confidence,
            )

        hours_until_cold = delta_until_min / abs(loss_rate)
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=min_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_until_cold * 60.0,
            heating_rate_per_hour=loss_rate,
            confidence=confidence,
        )

    def predict_zone_heating_time(
        self, current_temp: float, target_temp: float, outdoor_temp: float
    ) -> ThermalPrediction:
        """Predict how long to heat a zone to target temperature."""
        rate = self._zone_heating_rate(outdoor_temp)
        confidence = self.confidence_for("zone_heating")

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
                confidence=confidence,
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence=confidence,
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
                confidence=self.confidence_for("indoor_heating"),
            )

        hours_needed = delta_needed / rate
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=target_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_needed * 60.0,
            heating_rate_per_hour=rate,
            confidence=self.confidence_for("indoor_heating"),
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
                confidence=self.confidence_for("indoor_cooling"),
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
                confidence=self.confidence_for("indoor_cooling"),
            )

        hours_until_cold = delta_until_min / abs(loss_rate)
        return ThermalPrediction(
            current_temp=current_temp,
            target_temp=min_temp,
            outdoor_temp=outdoor_temp,
            estimated_minutes=hours_until_cold * 60.0,
            heating_rate_per_hour=loss_rate,
            confidence=self.confidence_for("indoor_cooling"),
        )

    def predict_indoor_curve(
        self,
        current_indoor: float,
        zone_water_temps: list[float],
        weather_forecast: list[dict],
        hours: int = 24,
        use_learned_forecast: bool = False,
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

        _validate_trajectory_inputs(
            hours, current_indoor, series=(zone_water_temps,), weather=weather_forecast
        )
        if hours > 0 and not zone_water_temps:
            raise ValueError("zone_supply_forecast_unavailable")
        curve = []
        indoor = current_indoor

        for h in range(hours):
            water_temp = zone_water_temps[h] if h < len(zone_water_temps) else zone_water_temps[-1]
            wx = weather_forecast[h] if h < len(weather_forecast) else weather_forecast[-1]
            outdoor_raw = wx.get("outdoor_temp", 5.0)
            outdoor = float(5.0 if outdoor_raw is None else outdoor_raw)
            wind = float(wx.get("wind_speed", 3.0) or 0.0)
            irradiance = float(wx.get("irradiance", 0.0) or 0.0)
            precipitation = float(wx.get("precipitation", 0.0) or 0.0)
            humidity = float(wx.get("humidity", 60.0) or 60.0)
            cloud_cover = float(wx.get("cloud_cover", 0.5) or 0.5)
            hour_of_day = wx.get("hour", (h % 24))

            if use_learned_forecast and comfort_model.is_ready_for_control:
                use_direct = bool(comfort_model.direct_forecast_horizons_minutes)
                predicted = comfort_model.predict_indoor_temp(
                    zone_water_temp=water_temp,
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    hour=hour_of_day,
                    indoor_temp=current_indoor if use_direct else indoor,
                    precipitation=precipitation,
                    humidity=humidity,
                    cloud_cover=cloud_cover,
                    zone_target_temp=water_temp,
                    space_heating_fraction=1.0,
                    recent_heat_fraction=1.0,
                    forecast_horizon_minutes=(h + 1) * 60 if use_direct else None,
                )
                if predicted is not None:
                    indoor = predicted
                    curve.append(
                        {
                            "hour": h + 1,
                            "predicted_indoor_temp": round(indoor, 1),
                            "source": "comfort_model_direct" if use_direct else "comfort_model",
                        }
                    )
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
            _validate_trajectory_inputs(hours, rate)
            indoor += rate + _solar_gain_c(irradiance)
            _validate_trajectory_inputs(hours, indoor)
            # Indoor temp physically bounded
            indoor = max(indoor, outdoor)

            curve.append(
                {
                    "hour": h + 1,
                    "predicted_indoor_temp": round(indoor, 1),
                    "source": "linear_rates",
                }
            )

        return curve

    def predict_indoor_controlled_curve(
        self,
        current_indoor: float,
        zone_water_temps: list[float],
        heating_fractions: list[float],
        weather_forecast: list[dict],
        hours: int = 24,
        use_learned_forecast: bool = False,
        max_passive_change_c_per_hour: float = 0.5,
    ) -> list[dict]:
        """Predict indoor temperature under an explicit per-hour heat plan.

        ``zone_water_temps`` describes the available supply temperature while
        ``heating_fractions`` describes how much of each hour the plan actually
        asks for space heating.  A value of ``0`` is therefore the exact
        no-space-heating counterfactual, even if the heat pump remains in
        NORMAL or ECO mode.  This avoids treating a mode setting as evidence
        that the compressor will heat the house for the full hour.
        Fallback rates are diagnostic assumptions, not certified comfort bounds.
        """
        from packages.ml.comfort_model import comfort_model

        _validate_trajectory_inputs(
            hours,
            current_indoor,
            max_passive_change_c_per_hour,
            series=(zone_water_temps, heating_fractions),
            weather=weather_forecast,
        )
        if max_passive_change_c_per_hour < 0 or any(
            not 0 <= value <= 1 for value in heating_fractions[:hours]
        ):
            raise ValueError("invalid_thermal_forecast_limit")
        if hours > 0 and not zone_water_temps:
            raise ValueError("zone_supply_forecast_unavailable")
        passive_horizons = {60, 180, 360, 720}
        passive_artifacts_available = set(comfort_model._passive_direct_models) == passive_horizons
        try:
            no_heating_plan = all(
                max(0.0, min(1.0, float(value))) == 0.0 for value in heating_fractions[:hours]
            )
        except (TypeError, ValueError):
            no_heating_plan = False
        use_learned_for_curve = use_learned_forecast and not (
            no_heating_plan and not passive_artifacts_available
        )

        if use_learned_for_curve and comfort_model.is_ready_for_control:
            learned_curve = self._predict_indoor_hybrid_curve(
                current_indoor=current_indoor,
                zone_water_temps=zone_water_temps,
                heating_fractions=heating_fractions,
                weather_forecast=weather_forecast,
                hours=hours,
                max_passive_change_c_per_hour=max_passive_change_c_per_hour,
            )
            if learned_curve is not None:
                return learned_curve

        curve = []
        indoor = current_indoor
        recent_heat_fraction = 0.0

        for h in range(hours):
            water_temp = zone_water_temps[h] if h < len(zone_water_temps) else zone_water_temps[-1]
            fraction_raw = heating_fractions[h] if h < len(heating_fractions) else 0.0
            try:
                fraction = max(0.0, min(1.0, float(fraction_raw)))
            except (TypeError, ValueError):
                fraction = 0.0
            wx = weather_forecast[h] if h < len(weather_forecast) else weather_forecast[-1]
            outdoor = float(wx.get("outdoor_temp", 5.0))
            wind = float(wx.get("wind_speed", 3.0) or 0.0)
            irradiance = float(wx.get("irradiance", 0.0) or 0.0)
            precipitation = float(wx.get("precipitation", 0.0) or 0.0)
            humidity_raw = wx.get("humidity", 60.0)
            cloud_cover_raw = wx.get("cloud_cover", 0.5)
            humidity = float(60.0 if humidity_raw is None else humidity_raw)
            cloud_cover = float(0.5 if cloud_cover_raw is None else cloud_cover_raw)
            hour_of_day = wx.get("hour", h % 24)

            if use_learned_for_curve and comfort_model.is_ready_for_control:
                no_heat = comfort_model.predict_indoor_temp(
                    zone_water_temp=outdoor,
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    hour=hour_of_day,
                    indoor_temp=indoor,
                    precipitation=precipitation,
                    humidity=humidity,
                    cloud_cover=cloud_cover,
                    zone_target_temp=outdoor,
                    space_heating_fraction=0.0,
                    recent_heat_fraction=recent_heat_fraction,
                )
                with_heat = comfort_model.predict_indoor_temp(
                    zone_water_temp=water_temp,
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    hour=hour_of_day,
                    indoor_temp=indoor,
                    precipitation=precipitation,
                    humidity=humidity,
                    cloud_cover=cloud_cover,
                    zone_target_temp=water_temp,
                    space_heating_fraction=1.0,
                    recent_heat_fraction=recent_heat_fraction,
                )
                if no_heat is not None and with_heat is not None:
                    # Heating must not make the home colder than the same
                    # model's no-heating counterfactual.
                    with_heat = max(with_heat, no_heat)
                    indoor = no_heat + fraction * (with_heat - no_heat)
                    curve.append(
                        {
                            "hour": h + 1,
                            "predicted_indoor_temp": round(indoor, 1),
                            "source": "comfort_model_controlled",
                            "space_heating_fraction": round(fraction, 3),
                        }
                    )
                    recent_heat_fraction = round(0.65 * recent_heat_fraction + 0.35 * fraction, 3)
                    continue

            # In warm weather there may be no verified compressor-on samples,
            # which correctly keeps the *control* model observation-only.  Its
            # independently validated direct forecasts are still a much better
            # no-space-heat baseline than recursively applying a cooling rate.
            # They are trained on the configured room input plus weather at the
            # requested lead time, so a 12-hour forecast cannot accumulate 11
            # one-hour fallback errors.
            if use_learned_for_curve and fraction == 0.0:
                passive, readiness = comfort_model.predict_passive_indoor_temp(
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    hour=hour_of_day,
                    indoor_temp=current_indoor,
                    precipitation=precipitation,
                    humidity=humidity,
                    cloud_cover=cloud_cover,
                    forecast_horizon_minutes=(h + 1) * 60,
                    max_change_c_per_hour=max_passive_change_c_per_hour,
                )
                if passive is not None:
                    # A direct model should follow measured inertia, not make a
                    # physically implausible jump from one hourly forecast. A
                    # broad bound protects against an out-of-domain weather row
                    # while preserving real day-scale solar/passive gains.
                    maximum_change = 3.0 if h < 12 else 4.0
                    indoor = max(
                        current_indoor - maximum_change,
                        min(current_indoor + maximum_change, float(passive)),
                    )
                    curve.append(
                        {
                            "hour": h + 1,
                            "predicted_indoor_temp": round(indoor, 1),
                            "source": "comfort_model_passive_direct",
                            "direct_horizon_minutes": readiness.get("horizon_minutes"),
                            "space_heating_fraction": 0.0,
                        }
                    )
                    continue

            no_heat_rate = self._free_float_step(indoor, outdoor) - indoor
            heating_rate = no_heat_rate
            if water_temp > indoor + 5.0:
                heating_rate = self._indoor_heating_rate(outdoor) + _solar_gain_c(irradiance)

            _validate_trajectory_inputs(hours, no_heat_rate, heating_rate)
            indoor += no_heat_rate + fraction * (heating_rate - no_heat_rate)
            _validate_trajectory_inputs(hours, indoor)
            curve.append(
                {
                    "hour": h + 1,
                    "predicted_indoor_temp": round(indoor, 1),
                    "source": "linear_controlled",
                    "space_heating_fraction": round(fraction, 3),
                }
            )
            recent_heat_fraction = round(0.65 * recent_heat_fraction + 0.35 * fraction, 3)

        return curve

    def predict_indoor_candidate_curve(
        self,
        current_indoor: float,
        zone_water_temps: list[float],
        heating_fractions: list[float],
        weather_forecast: list[dict],
        hours: int = 24,
        max_passive_change_c_per_hour: float = 0.5,
    ) -> list[dict] | None:
        """Return a complete learned candidate without consulting the live gate."""
        from packages.ml.comfort_model import comfort_model

        _validate_trajectory_inputs(
            hours,
            current_indoor,
            max_passive_change_c_per_hour,
            series=(zone_water_temps, heating_fractions),
            weather=weather_forecast,
        )
        if max_passive_change_c_per_hour < 0 or any(
            not 0 <= value <= 1 for value in heating_fractions[:hours]
        ):
            raise ValueError("invalid_thermal_forecast_limit")
        if not comfort_model.is_ready_for_control or set(
            comfort_model.direct_forecast_horizons_minutes
        ) != {60, 180, 360, 720}:
            return None
        try:
            no_heating_plan = all(
                max(0.0, min(1.0, float(value))) == 0.0 for value in heating_fractions[:hours]
            )
        except (TypeError, ValueError):
            return None
        if no_heating_plan and set(comfort_model._passive_direct_models) != {60, 180, 360, 720}:
            return None
        return self._predict_indoor_hybrid_curve(
            current_indoor=current_indoor,
            zone_water_temps=zone_water_temps,
            heating_fractions=heating_fractions,
            weather_forecast=weather_forecast,
            hours=hours,
            max_passive_change_c_per_hour=max_passive_change_c_per_hour,
        )

    def _predict_indoor_hybrid_curve(
        self,
        *,
        current_indoor: float,
        zone_water_temps: list[float],
        heating_fractions: list[float],
        weather_forecast: list[dict],
        hours: int,
        max_passive_change_c_per_hour: float = 0.5,
    ) -> list[dict] | None:
        """Assemble v7 direct anchors, interpolated deltas, and anchored physics."""
        from packages.ml.comfort_model import comfort_model

        _validate_trajectory_inputs(
            hours,
            current_indoor,
            max_passive_change_c_per_hour,
            series=(zone_water_temps, heating_fractions),
            weather=weather_forecast,
        )
        if (
            not zone_water_temps
            or not weather_forecast
            or hours <= 0
            or not comfort_model.is_ready_for_control
            or set(comfort_model.direct_forecast_horizons_minutes) != {60, 180, 360, 720}
        ):
            return None

        def inputs(hour: int) -> tuple[float, float, dict, float]:
            index = min(hour - 1, len(weather_forecast) - 1)
            wx = weather_forecast[index]
            water = float(zone_water_temps[min(hour - 1, len(zone_water_temps) - 1)])
            fraction = (
                max(0.0, min(1.0, float(heating_fractions[hour - 1])))
                if hour <= len(heating_fractions)
                else 0.0
            )
            outdoor = wx.get("outdoor_temp", 5.0)
            return water, fraction, wx, float(5.0 if outdoor is None else outdoor)

        def weather_values(wx: dict) -> tuple[float, float, float, float, float, float, int]:
            return (
                float(0.0 if wx.get("wind_speed") is None else wx.get("wind_speed", 3.0)),
                float(0.0 if wx.get("irradiance") is None else wx.get("irradiance", 0.0)),
                float(0.0 if wx.get("precipitation") is None else wx.get("precipitation", 0.0)),
                float(60.0 if wx.get("humidity") is None else wx.get("humidity", 60.0)),
                float(0.5 if wx.get("cloud_cover") is None else wx.get("cloud_cover", 0.5)),
                float(5.0 if wx.get("outdoor_temp") is None else wx.get("outdoor_temp", 5.0)),
                int(0 if wx.get("hour") is None else wx.get("hour", 0)),
            )

        anchors: dict[int, float] = {}
        passive_anchors: dict[int, float] = {}
        use_passive = all(
            max(0.0, min(1.0, float(value))) == 0.0 for value in heating_fractions[:hours]
        )
        for anchor_hour in (1, 3, 6, 12):
            if anchor_hour > hours:
                continue
            water, fraction, wx, outdoor = inputs(anchor_hour)
            wind, irradiance, precipitation, humidity, cloud_cover, _, hour_of_day = weather_values(
                wx
            )
            recent = sum(
                max(0.0, min(1.0, float(value)))
                for value in heating_fractions[max(0, anchor_hour - 3) : anchor_hour]
            ) / min(3, anchor_hour)
            controlled = comfort_model.predict_indoor_temp(
                zone_water_temp=water,
                zone_target_temp=water,
                outdoor_temp=outdoor,
                wind_speed=wind,
                irradiance=irradiance,
                precipitation=precipitation,
                humidity=humidity,
                cloud_cover=cloud_cover,
                hour=hour_of_day,
                indoor_temp=current_indoor,
                space_heating_fraction=sum(heating_fractions[:anchor_hour]) / anchor_hour,
                recent_heat_fraction=recent,
                forecast_horizon_minutes=anchor_hour * 60,
            )
            if controlled is None:
                return None
            anchors[anchor_hour] = float(controlled) - current_indoor
            if use_passive:
                passive, passive_readiness = comfort_model.predict_passive_indoor_temp(
                    outdoor_temp=outdoor,
                    wind_speed=wind,
                    irradiance=irradiance,
                    precipitation=precipitation,
                    humidity=humidity,
                    cloud_cover=cloud_cover,
                    hour=hour_of_day,
                    indoor_temp=current_indoor,
                    forecast_horizon_minutes=anchor_hour * 60,
                    max_change_c_per_hour=max_passive_change_c_per_hour,
                )
                if passive is None or not passive_readiness.get("ready"):
                    return None
                passive_anchors[anchor_hour] = float(passive) - current_indoor

        curve: list[dict] = []
        learned_indoor = current_indoor
        passive_indoor = current_indoor
        previous_weather: tuple[float, float] | None = None
        for hour in range(1, min(hours, 12) + 1):
            if not any(anchor >= hour for anchor in anchors):
                return None
            lower = max(anchor for anchor in anchors if anchor <= hour)
            upper = min(anchor for anchor in anchors if anchor >= hour)
            weight = 0.0 if lower == upper else (hour - lower) / (upper - lower)
            controlled_delta = anchors[lower] + weight * (anchors[upper] - anchors[lower])
            learned_indoor = current_indoor + controlled_delta
            if use_passive:
                passive_delta = passive_anchors[lower] + weight * (
                    passive_anchors[upper] - passive_anchors[lower]
                )
                passive_indoor = current_indoor + passive_delta
            _water, fraction, wx, outdoor = inputs(hour)
            irradiance = float(wx.get("irradiance", 0.0) or 0.0)
            if (
                fraction == 0.0
                and previous_weather is not None
                and outdoor <= previous_weather[0]
                and irradiance <= previous_weather[1]
            ):
                passive_indoor = min(passive_indoor, curve[-1]["predicted_indoor_temp"])
                learned_indoor = min(learned_indoor, curve[-1]["predicted_indoor_temp"])
            previous_weather = (outdoor, irradiance)
            _validate_trajectory_inputs(hours, learned_indoor, passive_indoor)
            curve.append(
                {
                    "hour": hour,
                    "predicted_indoor_temp": round(
                        passive_indoor if use_passive else learned_indoor, 3
                    ),
                    "source": (
                        "comfort_model_passive_direct"
                        if use_passive
                        else "comfort_model_controlled"
                    ),
                    "segment_kind": "direct" if lower == upper else "interpolated",
                    "space_heating_fraction": round(fraction, 3),
                }
            )

        for hour in range(13, hours + 1):
            water, fraction, wx, outdoor = inputs(hour)
            irradiance = float(wx.get("irradiance", 0.0) or 0.0)
            no_heat_rate = self._free_float_step(learned_indoor, outdoor) - learned_indoor
            heating_rate = (
                self._indoor_heating_rate(outdoor) + _solar_gain_c(irradiance)
                if water > learned_indoor + 5.0
                else no_heat_rate
            )
            _validate_trajectory_inputs(hours, no_heat_rate, heating_rate)
            learned_indoor += no_heat_rate + fraction * (heating_rate - no_heat_rate)
            passive_indoor = self._free_float_step(passive_indoor, outdoor)
            _validate_trajectory_inputs(hours, learned_indoor, passive_indoor)
            if (
                use_passive
                and previous_weather is not None
                and outdoor <= previous_weather[0]
                and irradiance <= previous_weather[1]
            ):
                passive_indoor = min(passive_indoor, curve[-1]["predicted_indoor_temp"])
            previous_weather = (outdoor, irradiance)
            curve.append(
                {
                    "hour": hour,
                    "predicted_indoor_temp": round(
                        passive_indoor if use_passive else learned_indoor, 3
                    ),
                    "source": (
                        "comfort_model_passive_physics_continuation"
                        if use_passive
                        else "comfort_model_physics_continuation"
                    ),
                    "segment_kind": "physics_continuation",
                    "space_heating_fraction": round(fraction, 3),
                }
            )
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
        _validate_trajectory_inputs(hours, current_temp, outdoor_temp)
        if target_temp is not None:
            _validate_trajectory_inputs(hours, target_temp)
        curve = []
        temp = current_temp
        state = "standby"

        heating_rate = (
            self._tank_heating_rate(outdoor_temp)
            if is_tank
            else self._zone_heating_rate(outdoor_temp)
        )
        loss_rate = (
            self._tank_loss_rate(outdoor_temp) if is_tank else self._zone_loss_rate(outdoor_temp)
        )
        _validate_trajectory_inputs(hours, heating_rate, loss_rate)

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

            _validate_trajectory_inputs(hours, temp)
            curve.append(
                {
                    "hour": h + 1,
                    "predicted_temp": round(temp, 1),
                    "state": state,
                }
            )

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
        _validate_trajectory_inputs(
            hours, current_temp, outdoor_temp, tank_target, series=(tank_min_per_hour,)
        )
        curve = []
        temp = current_temp
        state = "standby"
        heating_rate = self._tank_heating_rate(outdoor_temp)
        loss_rate = self._tank_loss_rate(outdoor_temp)
        _validate_trajectory_inputs(hours, heating_rate, loss_rate)

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

            _validate_trajectory_inputs(hours, temp, floor)
            curve.append(
                {
                    "hour": h + 1,
                    "predicted_temp": round(temp, 1),
                    "state": state,
                    "floor": round(floor, 1),
                }
            )

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
        _validate_trajectory_inputs(
            hours, current_indoor, series=(indoor_target_per_hour,), weather=weather_forecast
        )
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
                rate = self._indoor_heating_rate(outdoor)
                _validate_trajectory_inputs(hours, rate)
                indoor += rate
                if indoor > target:
                    indoor = target
                state = "heating"
            else:
                # At/above setpoint → the heat pump idles and the home passively
                # free-floats toward the outdoor temperature (cooling when warmer
                # than outside, warming when cooler), held at the comfort floor
                # which the optimizer would actively defend if the house cooled
                # to it. Drifting gradually — instead of snapping up to outdoor —
                # keeps the summer forecast sane when outdoor exceeds indoor.
                indoor = self._free_float_step(indoor, outdoor)
                indoor += _solar_gain_c(wx.get("irradiance"))
                indoor = max(indoor, target)
                state = "standby"

            _validate_trajectory_inputs(hours, indoor)
            curve.append(
                {
                    "hour": h + 1,
                    "predicted_indoor_temp": round(indoor, 1),
                    "target": round(target, 1),
                    "state": state,
                    "source": "managed_schedule",
                }
            )

        return curve

    def predict_free_float_curve(
        self,
        current_indoor: float,
        weather_forecast: list[dict],
        hours: int = 24,
    ) -> list[dict]:
        """Indoor air with the heat pump fully off (passive free-float).

        A physically honest "no heating" baseline: with no heat input the home
        only exchanges heat with the outside through its envelope, so the indoor
        temperature drifts toward the (time-varying) outdoor temperature —
        cooling on cold nights, warming on hot afternoons — at a damped rate
        that never overshoots outdoor.

        This replaces the previous approach of running the comfort ML model with
        a synthetic ``water_temp = outdoor`` input. That input lies far outside
        the model's training range (it never saw "no heating"), so the model
        extrapolated and drifted the baseline *upward*, which — together with the
        managed curve snapping to outdoor — made the summer indoor forecast look
        like it tracked the outdoor temperature instead of the home's behaviour.

        Args:
            current_indoor: current indoor air temperature (°C).
            weather_forecast: list of dicts with key ``outdoor_temp`` per hour;
                the last value is reused if the list is shorter than ``hours``.
            hours: number of hours to simulate.

        Returns:
            list of ``{hour, predicted_indoor_temp, source}`` entries.
        """
        _validate_trajectory_inputs(hours, current_indoor, weather=weather_forecast or None)
        curve = []
        indoor = current_indoor
        for h in range(hours):
            wx = (
                weather_forecast[h]
                if h < len(weather_forecast)
                else (weather_forecast[-1] if weather_forecast else {})
            )
            outdoor = wx.get("outdoor_temp", 5.0)
            indoor = self._free_float_step(indoor, outdoor)
            _validate_trajectory_inputs(hours, indoor)
            curve.append(
                {
                    "hour": h + 1,
                    "predicted_indoor_temp": round(indoor, 1),
                    "source": "free_float",
                    "input_status": "observed" if weather_forecast else "assumed",
                    "assumed_fields": [] if weather_forecast else ["outdoor_temp"],
                }
            )
        return curve

    def _free_float_step(self, indoor: float, outdoor: float) -> float:
        """Advance indoor one hour with the heat pump off (passive free-float).

        The house drifts toward the outdoor temperature by conduction in *both*
        directions — cooling when warmer than outside, warming when cooler — at a
        damped Newton's-law rate (the step shrinks as indoor approaches outdoor)
        that never overshoots. This replaces the old ``max(indoor, outdoor)``
        clamp, which snapped the prediction straight up to the outdoor
        temperature on warm afternoons.
        """
        _validate_trajectory_inputs(1, indoor, outdoor)
        delta = outdoor - indoor
        if abs(delta) < 1e-9:
            return indoor
        # Envelope conductance from the learned standby-loss magnitude (°C/h).
        rate_mag = abs(self._indoor_cooling_rate(outdoor))
        _validate_trajectory_inputs(1, rate_mag)
        if rate_mag <= 0.0:
            rate_mag = 0.3
        # Newton's law: the step shrinks as indoor nears outdoor (ref 15°C)…
        scale = min(abs(delta) / 15.0, 1.0)
        step = rate_mag * scale
        # …and can never carry indoor past the outdoor temperature.
        step = min(step, abs(delta))
        return indoor + math.copysign(step, delta)

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
        _validate_trajectory_inputs(
            hours, current_temp, outdoor_temp, tank_target, series=(dhw_minutes_per_hour,)
        )
        curve = []
        temp = current_temp
        heating_rate = self._tank_heating_rate(outdoor_temp)
        loss_rate = self._tank_loss_rate(outdoor_temp)
        _validate_trajectory_inputs(hours, heating_rate, loss_rate)

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

            curve.append(
                {
                    "hour": h + 1,
                    "predicted_temp": round(temp, 1),
                    "state": state,
                    "dhw_minutes": round(minutes, 1),
                }
            )

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

    def _rate_params(self, component: str, outdoor_temp: float) -> ThermalParams:
        """Learned parameters inside their observed outdoor range, defaults outside it."""
        from packages.ml.lifecycle import reference_is_qualified

        if self._qualified_reference is not None and not reference_is_qualified(
            self._qualified_reference
        ):
            return _DEFAULT_PARAMS
        observed = self.params.rate_outdoor_ranges.get(component)
        if observed and not (
            observed[0] - RATE_OUTDOOR_EXTRAPOLATION_MARGIN_C
            <= outdoor_temp
            <= observed[1] + RATE_OUTDOOR_EXTRAPOLATION_MARGIN_C
        ):
            return _DEFAULT_PARAMS
        return self.params

    def _tank_heating_rate(self, outdoor_temp: float) -> float:
        """Tank heating rate (°C/hour) adjusted for outdoor temp."""
        p = self._rate_params("tank_heating", outdoor_temp)
        rate = p.tank_heating_rate + p.tank_heating_outdoor_factor * outdoor_temp
        return max(1.0, min(30.0, rate))

    def _tank_loss_rate(self, outdoor_temp: float) -> float:
        """Tank standby loss rate (°C/hour, negative)."""
        p = self._rate_params("tank_cooling", outdoor_temp)
        loss = p.tank_standby_loss + p.tank_loss_outdoor_factor * outdoor_temp
        return max(-3.0, min(0.0, loss))  # Capped to physically plausible range

    def _zone_heating_rate(self, outdoor_temp: float) -> float:
        """Zone heating rate (°C/hour)."""
        p = self._rate_params("zone_heating", outdoor_temp)
        rate = p.zone_heating_rate + p.zone_heating_outdoor_factor * outdoor_temp
        return max(0.5, min(15.0, rate))

    def _zone_loss_rate(self, outdoor_temp: float) -> float:
        """Zone standby loss rate (°C/hour, negative)."""
        p = self._rate_params("zone_cooling", outdoor_temp)
        loss = p.zone_standby_loss + p.zone_loss_outdoor_factor * outdoor_temp
        return max(-3.0, min(0.0, loss))  # Capped to physically plausible range

    def _indoor_heating_rate(self, outdoor_temp: float) -> float:
        """Indoor air heating rate (°C/hour) when zone is actively heating."""
        p = self._rate_params("indoor_heating", outdoor_temp)
        rate = p.indoor_heating_rate + p.indoor_heating_outdoor_factor * outdoor_temp
        return max(INDOOR_HEATING_RATE_MIN_C_PER_H, min(INDOOR_HEATING_RATE_MAX_C_PER_H, rate))

    def _indoor_cooling_rate(self, outdoor_temp: float) -> float:
        """Indoor air cooling rate (°C/hour, negative) during standby.

        Clamped to [-1.0, 0.0]: a well-insulated house loses ~0.3°C/h,
        a poorly insulated one up to ~0.8°C/h.  Values beyond -1.0 are
        usually calibration noise, not real thermal behaviour.
        """
        p = self._rate_params("indoor_cooling", outdoor_temp)
        loss = p.indoor_cooling_rate + p.indoor_cooling_outdoor_factor * outdoor_temp
        return max(INDOOR_COOLING_RATE_MIN_C_PER_H, min(0.0, loss))


async def materialize_thermal_inputs(
    device_id: str,
    reference_sensor_id: str,
    weather_provider: str,
    cutoff: dt.datetime,
    *,
    selected_ids: list[str] | None = None,
    session=None,
    context: dict | None = None,
    raw: bool = False,
) -> dict:
    from contextlib import nullcontext

    from packages.ml.comfort_model import ComfortModel, SENSOR_SELECTION_SCHEMA
    from packages.poller.smartthings import get_selected_device_ids

    if selected_ids is None:
        selected_ids = (
            await get_selected_device_ids(values=context["settings"])
            if context is not None
            else await get_selected_device_ids()
        )
    reference_blocked = bool(
        reference_sensor_id and selected_ids and reference_sensor_id not in selected_ids
    )
    query_sensor_ids = [reference_sensor_id] if reference_sensor_id else selected_ids
    since = cutoff - dt.timedelta(days=90)
    async with get_session() if session is None else nullcontext(session) as session:
        records = (
            (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == device_id,
                        DeviceStatusRecord.ts >= since,
                        DeviceStatusRecord.ts <= cutoff,
                    )
                    .order_by(DeviceStatusRecord.ts)
                    .limit(50_001)
                )
            )
            .scalars()
            .all()
        )
        weather = (
            (
                await session.execute(
                    select(WeatherRecord)
                    .where(
                        WeatherRecord.source == weather_provider,
                        WeatherRecord.ts >= since - dt.timedelta(hours=2),
                        WeatherRecord.ts <= cutoff,
                    )
                    .order_by(WeatherRecord.ts)
                    .limit(50_001)
                )
            )
            .scalars()
            .all()
        )
        reading_query = (
            select(IndoorTempReading)
            .where(
                IndoorTempReading.timestamp >= since,
                IndoorTempReading.timestamp <= cutoff,
                IndoorTempReading.is_stale.is_(False),
            )
            .order_by(IndoorTempReading.timestamp)
            .limit(50_001)
        )
        if query_sensor_ids:
            reading_query = reading_query.where(IndoorTempReading.device_id.in_(query_sensor_ids))
        raw_readings = (
            [] if reference_blocked else (await session.execute(reading_query)).scalars().all()
        )
        if max(len(records), len(weather), len(raw_readings)) > 50_000:
            raise ValueError("Thermal materialization overflow")
        from packages.core.database import copy_materialized_rows

        records = await copy_materialized_rows(
            records,
            [column.name for column in DeviceStatusRecord.__table__.columns],
            context=context,
        )
        weather = await copy_materialized_rows(
            weather, [column.name for column in WeatherRecord.__table__.columns], context=context
        )
        raw_readings = await copy_materialized_rows(
            raw_readings,
            [column.name for column in IndoorTempReading.__table__.columns],
            context=context,
        )
        if raw:
            readings = raw_readings
        else:
            readings, _, _ = ComfortModel._select_indoor_observations(
                raw_readings, reference_sensor_id, selected_ids=selected_ids, as_of=cutoff
            )
    return {
        "records": records,
        "weather": weather,
        "readings": readings,
        "device_id": device_id,
        "data_cutoff": cutoff,
        "sensor_selection_schema": SENSOR_SELECTION_SCHEMA,
        "selected_sensor_ids": query_sensor_ids if not reference_blocked else [],
        "indoor_blocker": "reference_sensor_not_selected" if reference_blocked else None,
        "raw": raw,
    }


def _rate_sample_counts(samples: list) -> dict:
    train, holdout = ThermalModel._partition_rate_samples(samples)
    return {
        "availability": "assessed",
        "total": len(samples),
        "train": len(train),
        "holdout": len(holdout),
        "minimum_train": MIN_LEARNED_RATE_SAMPLES,
        "minimum_holdout": RATE_MIN_HOLDOUT_SAMPLES,
        "remaining_train": max(0, MIN_LEARNED_RATE_SAMPLES - len(train)),
        "remaining_holdout": max(0, RATE_MIN_HOLDOUT_SAMPLES - len(holdout)),
        "reason": "insufficient_train"
        if len(train) < MIN_LEARNED_RATE_SAMPLES
        else "insufficient_holdout"
        if len(holdout) < RATE_MIN_HOLDOUT_SAMPLES
        else None,
    }


def _thermal_rate_samples(data: dict) -> dict[str, list]:
    records, weather = data["records"], data["weather"]

    def outdoor_at(timestamp, fallback):
        matches = [
            row
            for row in weather
            if row.temperature is not None and 0 <= (timestamp - row.ts).total_seconds() <= 7200
        ]
        return float(matches[-1].temperature) if matches else fallback

    samples = {}
    for component, active, field_name, heating in (
        ("tank_heating", ThermalModel._dhw_heating_active, "tank_temp", True),
        ("tank_cooling", ThermalModel._dhw_heating_active, "tank_temp", False),
        ("zone_heating", ThermalModel._zone_heating_active, "zone1_temp", True),
        ("zone_cooling", ThermalModel._zone_heating_active, "zone1_temp", False),
    ):
        samples[component] = ThermalModel._rate_segments(
            records, field_name, active, heating, outdoor_at
        )
    samples["indoor_heating"], samples["indoor_cooling"] = (
        ThermalModel._extract_indoor_rate_samples(data["readings"], records, weather)
    )
    return samples


def build_thermal_candidate(data: dict, *, diagnostics: dict | None = None) -> dict | None:
    model = ThermalModel()
    records, weather = data["records"], data["weather"]
    component_samples = _thermal_rate_samples(data)
    if diagnostics is not None:
        diagnostics.update(
            {
                "data_cutoff": data["data_cutoff"].isoformat(),
                "device_id": data["device_id"],
                "source_records": len(records),
                "minimum_source_records": model.params.min_samples,
                "components": {
                    name: _rate_sample_counts(samples)
                    for name, samples in component_samples.items()
                },
            }
        )
    if len(records) < model.params.min_samples:
        if diagnostics is not None:
            diagnostics["reason"] = "insufficient_status_records"
        return None
    for component, samples in component_samples.items():
        if not component.startswith("indoor_"):
            model._validate_rate_component(component, samples)
    model._fit_indoor_rates(data["readings"], records, weather)
    if diagnostics is not None:
        for name, counts in diagnostics["components"].items():
            counts["fit_status"] = model.params.calibration_status.get(name)
            counts["validation"] = {
                key: value
                for key, value in model.validation.get(name, {}).items()
                if key not in {"holdout_features", "holdout_targets", "holdout_predictions"}
            }
    declared = [
        name
        for name, status in model.params.calibration_status.items()
        if status == "validated" and name.startswith(("tank_", "indoor_"))
    ]
    if not declared:
        if diagnostics is not None:
            diagnostics["reason"] = (
                "rejected_no_skill_over_baseline"
                if "rejected_no_skill_over_baseline" in model.params.calibration_status.values()
                else "insufficient_training_data"
            )
        return None
    if diagnostics is not None:
        diagnostics["reason"] = None
        diagnostics["declared_components"] = declared
    model.params.last_calibrated = data["data_cutoff"]
    model.params.sample_count = len(records)
    return {
        "schema_version": THERMAL_MODEL_SCHEMA_VERSION,
        "params": asdict(model.params),
        "validation": model.validation,
        "declared_components": declared,
        "device_id": data["device_id"],
    }


_extract_indoor_rate_samples = ThermalModel._extract_indoor_rate_samples
_partition_rate_samples = ThermalModel._partition_rate_samples

_DEFAULT_PARAMS = ThermalParams()

# Singleton instance
thermal_model = ThermalModel()

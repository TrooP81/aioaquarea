"""Populated planning evidence shared by SQLite lifetime and owned-PG regressions."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

from packages.core.models import (
    ConsumptionRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    PriceRecord,
    SettingRecord,
    SpaceHeatingGateRecord,
    WeatherRecord,
)
from packages.core.space_heating_gate import HeatingGateConfig
from packages.core.time_slots import next_hour_boundary

UTC = dt.timezone.utc
DEVICE = "p17-device"


def planning_rows(*, layer="rules_only", gate="valid"):
    now = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
    start = next_hour_boundary(now)
    from packages.optimizer.represented_strategy import NATIVE_ASSUMPTION
    from packages.core.config import settings

    provenance = {
        "diagnostic_assumptions": {
            "auxiliary_power_kw": 0,
            "native_sh_provenance": {
                "kind": "explicit_conditional_assumption",
                "method": NATIVE_ASSUMPTION,
                "device_id": DEVICE,
                "captured_at": now.isoformat(),
                "horizon_start": start.isoformat(),
                "horizon_end": (start + dt.timedelta(hours=24)).isoformat(),
                "maximum_electrical_power_kw": settings.sh_max_power_kw,
                "hourly_electrical_kw": [settings.sh_max_power_kw * (15.0 / 35.0)] * 24,
            },
        },
    }
    rows = [
        DeviceStatusRecord(
            device_id=DEVICE,
            ts=now,
            outdoor_temp=5,
            heat_pump_outdoor_temp=5,
            holiday_mode=0,
            tank_temp=50,
            tank_target_temp=60,
            tank_heat_min=30,
            tank_heat_max=65,
            zone1_temp=35,
            zone1_target_temp=35,
            zone1_heat_min=20,
            zone1_heat_max=55,
            quiet_mode=0,
            special_status=0,
            special_status_supported=True,
            operation_status=1,
            tank_operation_status=1 if layer == "milp_preferred" else 0,
            zone1_operation_status=1,
            device_action="HEATING",
            mode="heat",
            force_dhw=0,
            native_policy_provenance=provenance,
        ),
        IndoorTempReading(timestamp=now, device_id="p17-room", temperature=22, is_stale=False),
    ]
    config = HeatingGateConfig()
    if gate != "absent":
        rows.append(
            SpaceHeatingGateRecord(
                device_id=DEVICE,
                state="ALLOWED",
                config_fingerprint=config.fingerprint if gate == "valid" else "mismatch",
                base_c=config.base_c,
                on_threshold_c=config.on_threshold_c,
                off_threshold_c=config.off_threshold_c,
                last_raw_outdoor_c=5,
                reason_code="p17_allowed",
                source_status_ts=now,
                evaluated_at=now,
            )
        )
    values = {
        "optimizer_layer": layer,
        "aquarea_username": "offline-p17",
        "aquarea_password": "offline-p17",
        "smartthings_device_ids": "p17-room",
        "device_status_max_age_minutes": "15",
        "poll_interval_seconds": "300",
        "price_provider": "tibber",
        "entsoe_area": "SE3",
        "weather_provider": "open-meteo",
        "timezone": "UTC",
        "comfort_temp_min": "10",
        "comfort_temp_max": "35",
        "comfort_temp_target": "21",
        "space_heating_baseline_mode": "off",
        "learning_mode": "false",
        "tank_min_temp": "30",
        "tank_min_temp_offpeak": "30",
    }
    rows.extend(SettingRecord(key=key, value=value) for key, value in values.items())
    for h in range(24):
        ts = start + dt.timedelta(hours=h)
        rows.extend(
            [
                PriceRecord(
                    ts=ts,
                    area="tibber",
                    price_eur_per_kwh=0.2,
                    price_currency="EUR",
                    price_source="tibber",
                    fetched_at=now,
                    interval_schema="hourly_duration_weighted_v1",
                ),
                WeatherRecord(
                    ts=ts,
                    source="open-meteo",
                    wind_speed=2.0,
                    irradiance=0.0,
                    temperature=5,
                    forecast_issued_at=now,
                    units_schema="weather_c_mps_wm2_mm_v1",
                ),
            ]
        )
    return rows


def consumption_rows(now, *, timezone="Europe/Stockholm"):
    # A source-day reset at local midnight, which is not UTC midnight.
    from zoneinfo import ZoneInfo

    local_midnight = now.astimezone(ZoneInfo(timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - dt.timedelta(days=2)
    boundary = local_midnight.astimezone(UTC)
    day = local_midnight.date()
    return [
        ConsumptionRecord(
            device_id=DEVICE,
            ts=boundary + dt.timedelta(hours=h),
            heat_kwh=value,
            source_date=day - dt.timedelta(days=1) if h == -1 else day,
            source_timezone=timezone,
        )
        for h, value in [(-1, 10.0), (1, 2.0), (2, 4.0)]
    ]


def isolate_model_evidence(monkeypatch):
    """Keep external model artifacts out; never replace ORM/status/gate readers or solvers."""
    from packages.ml import comfort_model, forecast_quality, thermal
    from packages.optimizer import main

    monkeypatch.setattr(thermal.thermal_model, "arefresh_if_changed", AsyncMock(return_value=False))
    monkeypatch.setattr(
        comfort_model.comfort_model, "arefresh_if_changed", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(main._demand_model, "arefresh_committed", AsyncMock(return_value=False))
    monkeypatch.setattr(forecast_quality, "get_forecast_scorecard", AsyncMock(return_value={}))
    gate = AsyncMock(return_value={"control_allowed": True, "status": "ready"})
    monkeypatch.setattr(forecast_quality, "evaluate_live_control_gate", gate)
    monkeypatch.setattr("packages.optimizer.rules_engine.evaluate_live_control_gate", gate)
    monkeypatch.setattr(
        forecast_quality,
        "control_adjustments_for_weather",
        lambda *args: {
            "control_allowed": True,
            "condition_margins_c": [0.0] * 24,
            "bias_corrections_c": [0.0] * 24,
            "hourly_regimes": ["normal"] * 24,
        },
    )
    monkeypatch.setattr(forecast_quality, "prediction_intervals_for_weather", lambda *args: [])

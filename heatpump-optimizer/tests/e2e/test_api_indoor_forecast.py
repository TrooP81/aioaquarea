"""Contract tests for the unified indoor-comfort forecast API."""

import datetime as dt
import json

import pytest
from httpx import AsyncClient

from packages.core.models import IndoorTempReading, PlanRecord


@pytest.mark.asyncio(loop_scope="session")
class TestIndoorForecast:
    async def test_forecast_includes_the_weather_used_for_every_hour(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
        seed_weather,
    ):
        db_session.add(
            IndoorTempReading(
                timestamp=dt.datetime.now(dt.timezone.utc),
                device_id="forecast-reference",
                temperature=21.2,
                is_stale=False,
            )
        )
        await db_session.commit()
        response = await client.get("/api/thermal/indoor-forecast?hours=4")

        assert response.status_code == 200
        data = response.json()
        assert [point["hour"] for point in data["forecast_with_plan"]] == [1, 2, 3, 4]
        assert [point["hour"] for point in data["forecast_no_heating"]] == [1, 2, 3, 4]
        assert [target["hour"] for target in data["target_schedule"]] == [1, 2, 3, 4]
        assert len(data["weather_forecast"]) == 4

        for weather in data["weather_forecast"]:
            assert set(weather) == {
                "ts",
                "hour",
                "outdoor_temp",
                "wind_speed",
                "irradiance",
                "precipitation",
                "input_status",
                "imputed_fields",
            }
            assert 0 <= weather["hour"] <= 23
            assert weather["input_status"] in {"observed", "imputed"}
            assert isinstance(weather["imputed_fields"], list)

        timestamps = [weather["ts"] for weather in data["weather_forecast"]]
        assert timestamps == sorted(timestamps)
        assert len(data["price_forecast"]) == 4
        assert data["forecast_source"] == "live_estimate"
        assert data["forecast_status"] == "available"
        assert data["display_status"] in {"fresh", "degraded"}

    async def test_active_plan_returns_its_saved_forecast_snapshot(
        self,
        client: AsyncClient,
        db_session,
        seed_device_status,
    ):
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        snapshot = {
            "version": "indoor_forecast_v1",
            "current_indoor": 21.2,
            "forecast": [],
            "forecast_with_plan": [],
            "forecast_no_heating": [],
            "target_schedule": [],
            "weather_forecast": [],
            "price_forecast": [],
        }
        for hour in range(1, 5):
            slot_start = now + dt.timedelta(hours=hour - 1)
            state_ts = slot_start + dt.timedelta(hours=1)
            snapshot["forecast"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "predicted_indoor_temp": 21.0 + hour}
            )
            snapshot["forecast_with_plan"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 21.0 + hour,
                    "source": "milp_solution",
                    "space_heating_fraction": 0.5,
                }
            )
            snapshot["forecast_no_heating"].append(
                {
                    "hour": hour,
                    "ts": state_ts.isoformat(),
                    "predicted_indoor_temp": 20.5 - hour,
                    "source": "milp_counterfactual",
                }
            )
            snapshot["target_schedule"].append(
                {"hour": hour, "ts": state_ts.isoformat(), "target": 20.5}
            )
            snapshot["weather_forecast"].append(
                {
                    "ts": slot_start.isoformat(),
                    "hour": slot_start.hour,
                    "outdoor_temp": 4.0,
                    "wind_speed": 3.0,
                    "irradiance": 0.0,
                    "precipitation": 1.2,
                }
            )
            snapshot["price_forecast"].append(
                {"ts": slot_start.isoformat(), "price_eur_per_kwh": 0.12}
            )

        plan = PlanRecord(
            horizon_start=now,
            horizon_end=now + dt.timedelta(hours=4),
            plan_json=json.dumps({"forecast_snapshot": snapshot}),
            optimizer_version="milp_v1",
        )
        db_session.add(plan)
        await db_session.commit()

        response = await client.get("/api/thermal/indoor-forecast?hours=2")

        assert response.status_code == 200
        data = response.json()
        assert data["forecast_source"] == "active_plan"
        assert data["plan_id"] == plan.id
        assert data["current_indoor"] == 21.2
        assert data["forecast_with_plan"][0]["predicted_indoor_temp"] == 22.0
        assert data["forecast_no_heating"][0]["predicted_indoor_temp"] == 19.5
        assert data["price_forecast"][0]["price_eur_per_kwh"] == 0.12

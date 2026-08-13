from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from packages.api.schemas import TestConnectionRequest, TestConnectionResponse
from packages.core.database import get_session
from packages.core.pricing import get_active_price_context
from packages.core.models import (
    AppLogRecord,
    AuditLogRecord,
    DeviceStatusRecord,
    IndoorTempReading,
)
from packages.core.settings_service import (
    SETTINGS_SCHEMA,
    get_all_settings,
    get_comfort_schedule,
    get_effective_schedule,
    get_float_setting,
    get_learned_usage,
    get_setting,
    get_string_setting,
    set_setting,
    set_settings_bulk,
    set_heat_curve_verification_state,
)
from packages.core.heat_curve import (
    HEAT_CURVE_SETTING_KEYS,
    HeatCurveConfig,
    start_heat_curve_verification,
)

router = APIRouter()

CURRENCY_META: dict[str, dict] = {
    "EUR": {"prefix": "EUR ", "suffix": "", "sub": "c", "multiplier": 100},
    "GBP": {"prefix": "GBP ", "suffix": "", "sub": "p", "multiplier": 100},
    "USD": {"prefix": "USD ", "suffix": "", "sub": "c", "multiplier": 100},
    "CHF": {"prefix": "", "suffix": " CHF", "sub": "Rp", "multiplier": 100},
    "SEK": {"prefix": "", "suffix": " kr", "sub": "ore", "multiplier": 1},
    "NOK": {"prefix": "", "suffix": " kr", "sub": "ore", "multiplier": 1},
    "DKK": {"prefix": "", "suffix": " kr", "sub": "ore", "multiplier": 1},
    "PLN": {"prefix": "", "suffix": " zl", "sub": "", "multiplier": 1},
    "CZK": {"prefix": "", "suffix": " Kc", "sub": "", "multiplier": 1},
    "HUF": {"prefix": "", "suffix": " Ft", "sub": "", "multiplier": 1},
}


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


class ComfortScheduleUpdate(BaseModel):
    weekday: list[int]
    weekend: list[int]


@router.get("/api/settings")
async def get_settings():
    values = await get_all_settings()
    result = {}
    for key, schema in SETTINGS_SCHEMA.items():
        if key.startswith("_"):
            continue
        val = values.get(key, "")
        if schema.get("type") == "secret" and val:
            display_val = val[:1] + "***" + val[-1:] if len(val) > 8 else "***"
        else:
            display_val = val
        result[key] = {
            "value": display_val,
            "type": schema["type"],
            "description": schema.get("description", ""),
            "options": schema.get("options"),
        }
    return result


@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    invalid_keys = [k for k in body.settings if k not in SETTINGS_SCHEMA]
    if invalid_keys:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {invalid_keys}")

    heat_curve_changed = any(key in HEAT_CURVE_SETTING_KEYS for key in body.settings)
    previous_curve: HeatCurveConfig | None = None
    applied_curve: HeatCurveConfig | None = None
    # Validate the curve as one unit.  A valid individual number can still
    # produce an impossible curve (for example, a warm point below the cold
    # point), so save neither half of a bad manual controller record.
    if heat_curve_changed:
        values = await get_all_settings()
        previous_curve = HeatCurveConfig.from_settings(values)
        values.update(body.settings)
        try:
            applied_curve = HeatCurveConfig.from_settings(values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    await set_settings_bulk(body.settings)

    if heat_curve_changed and applied_curve is not None:
        async with get_session() as session:
            status = (
                await session.execute(
                    select(DeviceStatusRecord).order_by(desc(DeviceStatusRecord.ts)).limit(1)
                )
            ).scalar_one_or_none()
            indoor_temp = (
                await session.execute(
                    select(IndoorTempReading.temperature)
                    .order_by(IndoorTempReading.timestamp.desc())
                    .limit(1)
                )
            ).scalar()
        await set_heat_curve_verification_state(
            start_heat_curve_verification(
                started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                previous_curve=previous_curve,
                applied_curve=applied_curve,
                baseline_indoor_temp=float(indoor_temp) if indoor_temp is not None else None,
                baseline_outdoor_temp=(
                    float(status.outdoor_temp)
                    if status is not None and status.outdoor_temp is not None
                    else None
                ),
                comfort_target=await get_float_setting("comfort_temp_target"),
            )
        )

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="update_settings",
                payload_json=json.dumps(
                    {
                        k: "***" if SETTINGS_SCHEMA[k].get("type") == "secret" else v
                        for k, v in body.settings.items()
                    }
                ),
                result="updated",
            )
        )

    return {
        "status": "updated",
        "count": len(body.settings),
        "heat_curve_verification_started": heat_curve_changed,
    }


@router.get("/api/comfort-schedule")
async def get_schedule():
    return await get_comfort_schedule()


@router.put("/api/comfort-schedule")
async def update_schedule(body: ComfortScheduleUpdate):
    for h in body.weekday + body.weekend:
        if not (0 <= h <= 23):
            raise HTTPException(status_code=400, detail=f"Invalid hour: {h}. Must be 0-23.")
    if len(set(body.weekday)) > 24 or len(set(body.weekend)) > 24:
        raise HTTPException(status_code=400, detail="Too many hours specified.")

    schedule = {"weekday": sorted(set(body.weekday)), "weekend": sorted(set(body.weekend))}
    await set_setting("comfort_schedule", json.dumps(schedule))

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="update_comfort_schedule",
                payload_json=json.dumps(schedule),
                result="updated",
            )
        )

    return schedule


@router.get("/api/comfort-schedule/learned")
async def get_learned_schedule(days: int = Query(14, ge=1, le=90)):
    learned = await get_learned_usage(days=days)
    return {
        day_type: {str(h): score for h, score in hours.items()}
        for day_type, hours in learned.items()
    }


@router.post("/api/comfort-schedule/apply-learned")
async def apply_learned_schedule(threshold: float = Query(0.3, ge=0.1, le=0.9)):
    merged = await get_effective_schedule(learned_threshold=threshold)
    await set_setting("comfort_schedule", json.dumps(merged))

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="system",
                action="apply_learned_schedule",
                payload_json=json.dumps({"threshold": threshold, "result": merged}),
                result="updated",
            )
        )

    return merged


@router.get("/api/audit", response_model=list[dict])
async def get_audit_log(limit: int = Query(50, ge=1, le=200)):
    async with get_session() as session:
        result = await session.execute(
            select(AuditLogRecord).order_by(desc(AuditLogRecord.ts)).limit(limit)
        )
        rows = result.scalars().all()

    return [
        {
            "ts": r.ts.isoformat(),
            "actor": r.actor,
            "action": r.action,
            "target_device": r.target_device,
            "payload": json.loads(r.payload_json) if r.payload_json else None,
            "result": r.result,
        }
        for r in rows
    ]


@router.get("/api/logs")
async def get_app_logs(
    minutes: int = Query(30, ge=1, le=1440),
    level: str | None = Query(None),
    service: str | None = Query(None),
):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)

    stmt = select(AppLogRecord).where(AppLogRecord.ts >= cutoff)
    if level:
        stmt = stmt.where(AppLogRecord.level == level.upper())
    if service:
        stmt = stmt.where(AppLogRecord.service == service)
    stmt = stmt.order_by(desc(AppLogRecord.ts)).limit(500)

    async with get_session() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "ts": r.ts.isoformat(),
            "level": r.level,
            "logger": r.logger_name,
            "event": r.event,
            "details": json.loads(r.details_json) if r.details_json else None,
            "service": r.service,
        }
        for r in rows
    ]


@router.get("/api/currency")
async def get_currency():
    configured_code = (await get_string_setting("currency") or "EUR").upper()
    context = await get_active_price_context()
    # Displaying an amount in a different currency without an exchange rate is
    # worse than declining the conversion: it silently corrupts plan costs.
    code = context.currency
    meta = CURRENCY_META.get(code, {"prefix": code + " ", "suffix": "", "sub": "", "multiplier": 1})
    m = meta["multiplier"]
    if m == 100:
        price_label = f"{meta['prefix']}{meta['sub']}/kWh"
    else:
        price_label = f"{code}/kWh"
    return {
        "code": code,
        "prefix": meta["prefix"],
        "suffix": meta["suffix"],
        "multiplier": m,
        "price_label": price_label,
        "source": context.source,
        "configured_code": configured_code,
        "conversion_available": configured_code == code,
        "warning": (
            f"Prices are supplied in {code}; displaying source currency until an FX rate is configured."
            if configured_code != code
            else None
        ),
    }


@router.get("/api/time-format")
async def get_time_format():
    fmt = await get_string_setting("time_format")
    return {"format": fmt, "hour12": fmt == "12h"}


@router.post("/api/test-connection", response_model=TestConnectionResponse)
async def test_connection(body: TestConnectionRequest):
    if body.service == "aquarea":
        return await _test_aquarea(body.username, body.password)
    if body.service == "entsoe":
        return await _test_entsoe(body.api_token, body.area)
    if body.service == "tibber":
        return await _test_tibber(body.api_token)
    if body.service == "smartthings":
        return await _test_smartthings(body.pat)
    raise HTTPException(status_code=400, detail=f"Unknown service: {body.service}")


async def _test_aquarea(username: Optional[str], password: Optional[str]) -> TestConnectionResponse:
    import aiohttp
    from aioaquarea import AquareaEnvironment, Client

    if not username:
        username = await get_setting("aquarea_username")
    if not password:
        password = await get_setting("aquarea_password")

    if not username or not password:
        return TestConnectionResponse(
            service="aquarea", success=False, message="Username and password are required"
        )

    try:
        async with aiohttp.ClientSession() as session:
            client = Client(
                session=session,
                username=username,
                password=password,
                device_direct=True,
                refresh_login=False,
                environment=AquareaEnvironment.PRODUCTION,
            )
            await client.login()
            devices = await client.get_devices()
            device_count = len(devices) if devices else 0

        return TestConnectionResponse(
            service="aquarea",
            success=True,
            message=f"Authentication successful. Found {device_count} device(s).",
            details={"device_count": device_count},
        )
    except Exception as e:
        return TestConnectionResponse(
            service="aquarea", success=False, message=f"Authentication failed: {str(e)}"
        )


async def _test_entsoe(api_token: Optional[str], area: Optional[str]) -> TestConnectionResponse:
    import httpx

    if not api_token:
        api_token = await get_setting("entsoe_api_token")
    if not area:
        area = await get_setting("entsoe_area")

    if not api_token:
        return TestConnectionResponse(
            service="entsoe", success=False, message="ENTSO-E API token is required"
        )

    now = dt.datetime.now(dt.timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + dt.timedelta(days=1)
    params = {
        "securityToken": api_token,
        "documentType": "A44",
        "in_Domain": area or "10YNL----------L",
        "out_Domain": area or "10YNL----------L",
        "periodStart": period_start.strftime("%Y%m%d%H00"),
        "periodEnd": period_end.strftime("%Y%m%d%H00"),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://web-api.tp.entsoe.eu/api", params=params)

        if resp.status_code == 401:
            return TestConnectionResponse(
                service="entsoe", success=False, message="Invalid API token (401 Unauthorized)"
            )
        if resp.status_code == 400:
            return TestConnectionResponse(
                service="entsoe",
                success=False,
                message="Bad request - check area code",
                details={"status_code": 400, "area": area},
            )

        resp.raise_for_status()
        return TestConnectionResponse(
            service="entsoe",
            success=True,
            message="ENTSO-E API connection successful. Price data available.",
            details={"status_code": resp.status_code, "area": area},
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            service="entsoe", success=False, message="Connection timed out after 30s"
        )
    except Exception as e:
        return TestConnectionResponse(
            service="entsoe", success=False, message=f"Connection failed: {str(e)}"
        )


async def _test_tibber(api_token: Optional[str]) -> TestConnectionResponse:
    import httpx

    if not api_token:
        api_token = await get_setting("tibber_api_token")

    if not api_token:
        return TestConnectionResponse(
            service="tibber", success=False, message="Tibber API token is required"
        )

    query = """
    {
      viewer {
        name
        homes {
          address {
            city
          }
        }
      }
    }
    """
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tibber.com/v1-beta/gql",
                json={"query": query},
                headers=headers,
            )

        if resp.status_code == 403:
            return TestConnectionResponse(
                service="tibber", success=False, message="Invalid API token (403 Forbidden)"
            )

        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            return TestConnectionResponse(
                service="tibber",
                success=False,
                message=f"API error: {data['errors'][0].get('message', 'Unknown')}",
            )

        viewer = data.get("data", {}).get("viewer", {})
        homes = viewer.get("homes", [])
        return TestConnectionResponse(
            service="tibber",
            success=True,
            message=f"Tibber connection successful. Account: {viewer.get('name', 'N/A')}, {len(homes)} home(s).",
            details={"name": viewer.get("name"), "home_count": len(homes)},
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            service="tibber", success=False, message="Connection timed out after 30s"
        )
    except Exception as e:
        return TestConnectionResponse(
            service="tibber", success=False, message=f"Connection failed: {str(e)}"
        )


async def _test_smartthings(pat: Optional[str]) -> TestConnectionResponse:
    from packages.poller.smartthings import SmartThingsAuthError, SmartThingsClient
    from packages.poller.smartthings_oauth import get_valid_access_token

    if pat and not pat.startswith("***"):
        access_token = pat
    else:
        access_token = await get_valid_access_token()

    if not access_token:
        return TestConnectionResponse(
            service="smartthings",
            success=False,
            message="SmartThings not connected - configure OAuth or provide a PAT",
        )

    try:
        client = SmartThingsClient(access_token)
        devices = await client.discover_temp_sensors()

        if not devices:
            return TestConnectionResponse(
                service="smartthings",
                success=True,
                message="Authentication successful but no temperature sensors found.",
                details={"device_count": 0},
            )

        names = [d.get("label", d.get("name", "?")) for d in devices[:5]]
        return TestConnectionResponse(
            service="smartthings",
            success=True,
            message=f"Connected. Found {len(devices)} sensor(s): {', '.join(names)}",
            details={"device_count": len(devices), "devices": names},
        )
    except SmartThingsAuthError as e:
        return TestConnectionResponse(
            service="smartthings", success=False, message=f"Authentication failed: {str(e)}"
        )
    except Exception as e:
        return TestConnectionResponse(
            service="smartthings", success=False, message=f"Connection failed: {str(e)}"
        )

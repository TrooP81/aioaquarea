from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, not_, select

from packages.api.schemas import IndoorTempResponse
from packages.core.database import get_session
from packages.core.models import IndoorTempReading
from packages.core.settings_service import get_setting, set_setting

router = APIRouter()


@router.get("/api/indoor-temp", response_model=list[IndoorTempResponse])
async def get_indoor_temp(
    hours: int = Query(24, ge=1, le=720),
    device_id: Optional[str] = Query(None),
):
    from packages.poller.smartthings import get_selected_device_ids

    selected = await get_selected_device_ids()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with get_session() as session:
        stmt = select(IndoorTempReading).where(IndoorTempReading.timestamp >= since)
        if device_id:
            stmt = stmt.where(IndoorTempReading.device_id == device_id)
        elif selected:
            stmt = stmt.where(IndoorTempReading.device_id.in_(selected))
        stmt = stmt.order_by(IndoorTempReading.timestamp)
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        IndoorTempResponse(
            id=r.id,
            timestamp=r.timestamp,
            device_id=r.device_id,
            device_label=r.device_label,
            room=r.room,
            temperature=r.temperature,
        )
        for r in rows
    ]


@router.get("/api/indoor-temp/latest")
async def get_latest_indoor_temp():
    from packages.poller.smartthings import get_selected_device_ids

    selected = await get_selected_device_ids()
    async with get_session() as session:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)
        agg_stmt = select(
            func.avg(IndoorTempReading.temperature),
            func.max(IndoorTempReading.timestamp),
            func.count(IndoorTempReading.id),
        ).where(IndoorTempReading.timestamp >= cutoff)

        fresh_stmt = select(func.max(IndoorTempReading.timestamp)).where(
            not_(IndoorTempReading.is_stale)
        )

        if selected:
            agg_stmt = agg_stmt.where(IndoorTempReading.device_id.in_(selected))
            fresh_stmt = fresh_stmt.where(IndoorTempReading.device_id.in_(selected))

        result = await session.execute(agg_stmt)
        row = result.one()

        fresh_result = await session.execute(fresh_stmt)
        last_fresh = fresh_result.scalar()

    return {
        "avg_temperature": round(row[0], 1) if row[0] is not None else None,
        "latest_reading": row[1].isoformat() if row[1] else None,
        "sensor_count": row[2] or 0,
        "last_fresh_reading": last_fresh.isoformat() if last_fresh else None,
    }


@router.get("/api/smartthings/devices")
async def list_smartthings_devices():
    from packages.poller.smartthings import SmartThingsAuthError, SmartThingsClient
    from packages.poller.smartthings_oauth import get_valid_access_token

    access_token = await get_valid_access_token()
    if not access_token:
        raise HTTPException(status_code=400, detail="SmartThings not connected (configure OAuth or PAT)")

    try:
        client = SmartThingsClient(access_token)
        devices = await client.discover_temp_sensors()
    except SmartThingsAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return {"devices": devices}


@router.get("/api/smartthings/oauth/authorize")
async def smartthings_oauth_authorize(request: Request):
    from packages.poller.smartthings_oauth import build_authorize_url

    client_id = await get_setting("smartthings_client_id")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="smartthings_client_id not configured - register an app via `smartthings apps:create`",
        )

    redirect_uri = await get_setting("smartthings_redirect_uri")
    if not redirect_uri:
        raise HTTPException(
            status_code=400,
            detail="smartthings_redirect_uri not configured - set it to your registered redirect URI",
        )

    url, state = build_authorize_url(client_id, redirect_uri)
    await set_setting("_smartthings_oauth_state", state)

    response = Response(content=json.dumps({"authorize_url": url, "state": state}), media_type="application/json")
    response.set_cookie(
        key="smartthings_oauth_state",
        value=state,
        max_age=600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@router.get("/api/smartthings/oauth/callback")
async def smartthings_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    if code is None or state is None:
        return {"status": "ok", "message": "SmartThings OAuth callback endpoint ready"}

    from packages.poller.smartthings_oauth import (
        SmartThingsOAuthError,
        exchange_code_for_tokens,
        save_tokens,
    )

    expected_state = request.cookies.get("smartthings_oauth_state") or ""
    if not expected_state:
        expected_state = await get_setting("_smartthings_oauth_state")
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (possible CSRF)")

    await set_setting("_smartthings_oauth_state", "")

    client_id = await get_setting("smartthings_client_id")
    client_secret = await get_setting("smartthings_client_secret")
    redirect_uri = await get_setting("smartthings_redirect_uri")
    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(status_code=400, detail="SmartThings OAuth credentials not configured")

    try:
        token_data = await exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    except SmartThingsOAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))

    await save_tokens(token_data)
    response = Response(status_code=302, headers={"Location": "/settings?smartthings_oauth=connected"})
    response.delete_cookie("smartthings_oauth_state")
    return response


@router.get("/api/smartthings/oauth/status")
async def smartthings_oauth_status():
    from packages.poller.smartthings_oauth import load_tokens

    tokens = await load_tokens()
    if tokens is None:
        pat = await get_setting("smartthings_pat")
        if pat:
            return {"connected": True, "method": "pat", "expires_at": None}
        return {"connected": False, "method": None, "expires_at": None}

    return {
        "connected": True,
        "method": "oauth",
        "expires_at": tokens["expires_at"].isoformat(),
        "scope": tokens.get("scope", ""),
    }


@router.delete("/api/smartthings/oauth/disconnect")
async def smartthings_oauth_disconnect():
    from packages.poller.smartthings_oauth import delete_tokens

    await delete_tokens()
    return {"disconnected": True}

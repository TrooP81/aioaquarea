from __future__ import annotations

import datetime as dt
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, model_validator
from sqlalchemy import desc, select

from packages.api.schemas import (
    PlanningSettingsPatch,
    PlanningSettingsPreviewRequest,
    TestConnectionRequest,
    TestConnectionResponse,
)
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
    get_effective_schedule,
    get_float_setting,
    get_learned_usage,
    get_setting,
    get_setting_spec,
    get_string_setting,
    is_masked_secret,
    normalize_contract_settings_updates,
    set_settings_bulk,
    set_heat_curve_verification_state,
    validate_setting_value,
)
from packages.core.comfort_schedule import normalize_schedule
from packages.core.house_contract import (
    ContractRevisionConflict,
    HousePlanningContract,
    _contract_hash,
    canonical_target_schedule,
    ensure_current_contract,
    policy_dependency_fingerprint,
    RESOLVER_SCHEMA,
)
from packages.core.models import HouseInstallationRecord
from packages.core.heat_curve import (
    HEAT_CURVE_SETTING_KEYS,
    HeatCurveConfig,
    start_heat_curve_verification,
)
from packages.core.space_heating_gate import HeatingGateConfig

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

    @model_validator(mode="before")
    @classmethod
    def validate_raw_schedule(cls, value):
        normalized = normalize_schedule(value)
        return {key: normalized[key] for key in value}


def _revision_from_if_match(value: str | None) -> int | None:
    if value is None or not isinstance(value, str):
        return None
    normalized = value.strip().removeprefix("W/").strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="If-Match must contain a contract revision"
        ) from exc
    if revision < 1:
        raise HTTPException(
            status_code=400, detail="If-Match must contain a positive contract revision"
        )
    return revision


async def _set_contract_etag(response: Response, session, settings: dict[str, str]) -> int:
    revision = await ensure_current_contract(session, settings)
    response.headers["ETag"] = f'"{revision.id}"'
    response.headers["X-Contract-Revision"] = str(revision.id)
    response.headers["X-Installation-ID"] = str(revision.contract_json["installation_id"])
    return revision.id


def _planning_contract_payload(revision) -> dict:
    contract = {
        key: value for key, value in revision.contract_json.items() if key != "field_sources"
    }
    return {
        **contract,
        "revision_id": revision.id,
        "contract_hash": revision.contract_hash,
        "effective_at": revision.effective_at.isoformat(),
        "source": revision.source,
        "field_sources": revision.contract_json.get("field_sources", {}),
        "validation": {
            "comfort_schedule_hours": {"minimum": 0, "maximum": 23, "unique": True},
            "comfort_temperature_c": {"minimum": 10, "maximum": 30},
            "timezone": "IANA",
        },
        "scope": {
            "protected_rooms": "not_configured_in_p2_1",
            "dhw_custom_schedule": "not_configured_in_p2_1",
            "tariff_contract": "unchanged_existing_price_scope",
            "controller_curve_attestation": "not_configured_in_p2_1",
        },
        "p2_defaults": {
            "protected_rooms": [],
            "dhw_schedule_mode": "inherit_comfort",
            "dhw_custom_schedule": None,
            "tariff_settings": None,
            "curve_attestation": "unknown_not_recorded_by_p2_1",
        },
        "ordinary_writes_allowed": False,
    }


async def _load_scoped_contract(session, installation_id: UUID):
    from packages.core.house_contract import _lock_contract

    await _lock_contract(session)
    installation = await session.get(HouseInstallationRecord, 1)
    if installation is None or installation.installation_id != installation_id:
        raise HTTPException(status_code=404, detail="Installation not found")
    values = await get_all_settings(session=session)
    try:
        revision = await ensure_current_contract(session, values)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "planning_contract_unavailable", "message": str(exc)},
        ) from exc
    if revision.contract_json.get("installation_id") != str(installation_id):
        raise HTTPException(status_code=404, detail="Installation not found")
    return installation, values, revision


def _planning_patch_values(body: PlanningSettingsPatch) -> dict[str, str]:
    try:
        patch = body.model_dump(exclude_unset=True)
        contract_fields = {
            "comfort_schedule",
            "comfort_temp_min",
            "comfort_temp_target",
            "comfort_temp_max",
            "timezone",
        }
        return normalize_contract_settings_updates(
            {key: value for key, value in patch.items() if key in contract_fields}
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/installations/{installation_id}/planning-settings")
async def get_planning_settings(installation_id: UUID, response: Response):
    """Read the configured installation's redacted, revisioned P2.1 contract."""
    async with get_session() as session:
        _, _, revision = await _load_scoped_contract(session, installation_id)
        response.headers["ETag"] = f'"{revision.id}"'
        response.headers["X-Contract-Revision"] = str(revision.id)
        response.headers["X-Installation-ID"] = str(installation_id)
        return _planning_contract_payload(revision)


@router.put("/api/installations/{installation_id}/planning-settings")
async def put_planning_settings(
    installation_id: UUID,
    body: PlanningSettingsPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Atomically update a partial P2.1 contract under its expected revision."""
    expected_revision_id = _revision_from_if_match(if_match)
    if expected_revision_id is None:
        raise HTTPException(status_code=428, detail="If-Match contract revision is required")
    updates = _planning_patch_values(body)
    async with get_session() as session:
        await _load_scoped_contract(session, installation_id)
        try:
            revision_id = await set_settings_bulk(
                updates,
                session=session,
                expected_revision_id=expected_revision_id,
                source="scoped_planning_settings_api",
            )
        except ContractRevisionConflict as exc:
            response.headers["ETag"] = f'"{exc.current_revision_id}"'
            response.headers["X-Contract-Revision"] = str(exc.current_revision_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "contract_revision_changed",
                    "current_revision": exc.current_revision_id,
                },
                headers={
                    "ETag": f'"{exc.current_revision_id}"',
                    "X-Contract-Revision": str(exc.current_revision_id),
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        values = await get_all_settings(session=session)
        try:
            revision = await ensure_current_contract(session, values)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail={"reason": "invalid_planning_contract", "message": str(exc)}
            ) from exc
        if revision.id != revision_id or revision.contract_json.get("installation_id") != str(
            installation_id
        ):
            raise HTTPException(status_code=404, detail="Installation not found")
        response.headers["ETag"] = f'"{revision.id}"'
        response.headers["X-Contract-Revision"] = str(revision.id)
        response.headers["X-Installation-ID"] = str(installation_id)
        return _planning_contract_payload(revision)


@router.post("/api/installations/{installation_id}/planning-settings/preview")
async def preview_planning_settings(
    installation_id: UUID,
    body: PlanningSettingsPreviewRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Resolve a proposed P2.1 contract without saving or invoking device services."""
    expected_revision_id = _revision_from_if_match(if_match)
    if expected_revision_id is None:
        raise HTTPException(status_code=428, detail="If-Match contract revision is required")
    updates = _planning_patch_values(body)
    async with get_session() as session:
        installation, current_values, current = await _load_scoped_contract(
            session, installation_id
        )
        if current.id != expected_revision_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "contract_revision_changed",
                    "current_revision": current.id,
                },
                headers={"ETag": f'"{current.id}"', "X-Contract-Revision": str(current.id)},
            )
        proposed_values = dict(current_values)
        proposed_values.update(updates)
        try:
            contract = HousePlanningContract.from_settings(
                proposed_values, installation.installation_id, installation.house_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        projection = contract.to_projection()
        digest = _contract_hash(projection)
        from packages.core.comfort_schedule import resolve_hourly_targets

        intervals = resolve_hourly_targets(
            projection["comfort_schedule"],
            projection["timezone"],
            body.horizon_start,
            body.horizon_end,
            scheduled_target_c=projection["comfort_temp_target"],
            unscheduled_target_c=projection["comfort_temp_min"],
            outer_max_c=projection["comfort_temp_max"],
        )
        for interval in intervals:
            interval["contract_revision_id"] = current.id
            interval["contract_hash"] = digest
        preview_snapshot = {
            **projection,
            "revision_id": current.id,
            "contract_hash": digest,
            "policy_dependency_fingerprint": policy_dependency_fingerprint(projection),
            "resolver_schema": RESOLVER_SCHEMA,
            "schedule_projection": intervals,
        }
        return {
            "installation_id": str(installation_id),
            "base_revision_id": current.id,
            "base_contract_hash": current.contract_hash,
            "proposed_contract_hash": digest,
            "proposed_contract": projection,
            "schedule_projection": intervals,
            "target_schedule": canonical_target_schedule(preview_snapshot),
            "preview_only": True,
            "ordinary_writes_allowed": False,
        }


async def get_baseline_promotion_readiness() -> bool:
    """Read promotion evidence without allowing scorer failures to promote."""
    try:
        from packages.ml.forecast_quality import get_forecast_scorecard

        scorecard = await get_forecast_scorecard()
        comparison = scorecard.get("baseline_comparison", {})
        return comparison.get("promotion_ready") is True
    except Exception:
        return False


@router.get("/api/settings")
async def get_settings(response: Response = None):
    response = response or Response()
    try:
        async with get_session() as session:
            values = await get_all_settings(session=session)
            await _set_contract_etag(response, session, values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = {}
    for key, schema in SETTINGS_SCHEMA.items():
        if key.startswith("_"):
            continue
        val = values.get(key, "")
        if schema.get("type") == "secret" and val:
            display_val = (
                "***"
                if schema.get("redaction") == "full"
                else val[:1] + "***" + val[-1:]
                if len(val) > 8
                else "***"
            )
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
async def update_settings(
    body: SettingsUpdate,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    response = response or Response()
    expected_revision_id = _revision_from_if_match(if_match)
    invalid_keys = [
        key for key in body.settings if key.startswith("_") or key not in SETTINGS_SCHEMA
    ]
    if invalid_keys:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {invalid_keys}")

    # Do not overwrite a real secret with the masked value returned by GET.
    cleaned = {
        key: value
        for key, value in body.settings.items()
        if not is_masked_secret(get_setting_spec(key), value)
    }

    errors: dict[str, str] = {}
    for key, value in cleaned.items():
        try:
            validate_setting_value(key, value)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise HTTPException(status_code=400, detail={"invalid_values": errors})

    if "space_heating_baseline_mode" in cleaned:
        persisted = await get_all_settings()
        current_mode = persisted.get("space_heating_baseline_mode", "shadow")
        requested_mode = cleaned["space_heating_baseline_mode"]
        if current_mode != "on" and requested_mode == "on":
            if not await get_baseline_promotion_readiness():
                raise HTTPException(
                    status_code=409,
                    detail="Baseline promotion evidence is not ready",
                )

    gate_setting_keys = {
        "heat_curve_heating_off_outdoor_c",
        "space_heating_gate_on_offset_c",
        "space_heating_gate_off_offset_c",
        "space_heating_behavior_profile",
    }
    heat_curve_changed = any(key in HEAT_CURVE_SETTING_KEYS for key in cleaned)
    previous_curve: HeatCurveConfig | None = None
    applied_curve: HeatCurveConfig | None = None
    # Validate the curve as one unit.  A valid individual number can still
    # produce an impossible curve (for example, a warm point below the cold
    # point), so save neither half of a bad manual controller record.
    if heat_curve_changed:
        values = await get_all_settings()
        previous_curve = HeatCurveConfig.from_settings(values)
        values.update(cleaned)
        try:
            applied_curve = HeatCurveConfig.from_settings(values)
            HeatingGateConfig(
                profile_id=values["space_heating_behavior_profile"],
                base_c=float(values["heat_curve_heating_off_outdoor_c"]),
                on_offset_c=float(values["space_heating_gate_on_offset_c"]),
                off_offset_c=float(values["space_heating_gate_off_offset_c"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if any(key in gate_setting_keys for key in cleaned) and not heat_curve_changed:
        values = await get_all_settings()
        values.update(cleaned)
        try:
            HeatingGateConfig(
                profile_id=values["space_heating_behavior_profile"],
                base_c=float(values["heat_curve_heating_off_outdoor_c"]),
                on_offset_c=float(values["space_heating_gate_on_offset_c"]),
                off_offset_c=float(values["space_heating_gate_off_offset_c"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if expected_revision_id is None:
            revision_id = await set_settings_bulk(cleaned)
        else:
            revision_id = await set_settings_bulk(
                cleaned,
                expected_revision_id=expected_revision_id,
                source="versioned_api",
            )
    except ContractRevisionConflict as exc:
        response.headers["X-Contract-Revision"] = str(exc.current_revision_id)
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "contract_revision_changed",
                "current_revision": exc.current_revision_id,
            },
            headers={
                "ETag": f'"{exc.current_revision_id}"',
                "X-Contract-Revision": str(exc.current_revision_id),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if revision_id is not None:
        response.headers["ETag"] = f'"{revision_id}"'
        response.headers["X-Contract-Revision"] = str(revision_id)
    else:
        async with get_session() as session:
            values = await get_all_settings(session=session)
            await _set_contract_etag(response, session, values)

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
                        for k, v in cleaned.items()
                    }
                ),
                result="updated",
            )
        )

    return {
        "status": "updated",
        "count": len(cleaned),
        "heat_curve_verification_started": heat_curve_changed,
    }


@router.get("/api/comfort-schedule")
async def get_schedule(response: Response = None):
    response = response or Response()
    async with get_session() as session:
        values = await get_all_settings(session=session)
        await _set_contract_etag(response, session, values)
        schedule = normalize_schedule(json.loads(values["comfort_schedule"]))
    return schedule


@router.put("/api/comfort-schedule")
async def update_schedule(
    body: ComfortScheduleUpdate,
    response: Response = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    response = response or Response()
    schedule = normalize_schedule(body.model_dump())
    expected_revision_id = _revision_from_if_match(if_match)
    try:
        if expected_revision_id is None:
            revision_id = await set_settings_bulk({"comfort_schedule": json.dumps(schedule)})
        else:
            revision_id = await set_settings_bulk(
                {"comfort_schedule": json.dumps(schedule)},
                expected_revision_id=expected_revision_id,
                source="versioned_api",
            )
    except ContractRevisionConflict as exc:
        response.headers["X-Contract-Revision"] = str(exc.current_revision_id)
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "contract_revision_changed",
                "current_revision": exc.current_revision_id,
            },
            headers={
                "ETag": f'"{exc.current_revision_id}"',
                "X-Contract-Revision": str(exc.current_revision_id),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="user",
                action="update_comfort_schedule",
                payload_json=json.dumps(schedule),
                result="updated",
            )
        )

    if revision_id is not None:
        response.headers["ETag"] = f'"{revision_id}"'
        response.headers["X-Contract-Revision"] = str(revision_id)
    return schedule


@router.get("/api/comfort-schedule/learned")
async def get_learned_schedule(days: int = Query(14, ge=1, le=90)):
    learned = await get_learned_usage(days=days)
    return {
        day_type: {str(h): score for h, score in hours.items()}
        for day_type, hours in learned.items()
    }


@router.post("/api/comfort-schedule/apply-learned")
async def apply_learned_schedule(
    response: Response = None,
    threshold: float = Query(0.3, ge=0.1, le=0.9),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    response = response or Response()
    merged = await get_effective_schedule(learned_threshold=threshold)
    try:
        revision_id = await set_settings_bulk(
            {"comfort_schedule": json.dumps(merged)},
            expected_revision_id=_revision_from_if_match(if_match),
            source="learned_schedule_opt_in",
        )
    except ContractRevisionConflict as exc:
        response.headers["X-Contract-Revision"] = str(exc.current_revision_id)
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "contract_revision_changed",
                "current_revision": exc.current_revision_id,
            },
            headers={
                "ETag": f'"{exc.current_revision_id}"',
                "X-Contract-Revision": str(exc.current_revision_id),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with get_session() as session:
        session.add(
            AuditLogRecord(
                actor="system",
                action="apply_learned_schedule",
                payload_json=json.dumps({"threshold": threshold, "result": merged}),
                result="updated",
            )
        )

    if revision_id is not None:
        response.headers["ETag"] = f'"{revision_id}"'
        response.headers["X-Contract-Revision"] = str(revision_id)
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

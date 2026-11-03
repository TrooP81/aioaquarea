from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from packages.api.routers import settings as settings_router
from packages.api.auth import require_auth
from packages.core.house_contract import (
    _contract_hash,
    _settings_projection,
    canonical_target_schedule,
    policy_dependency_fingerprint,
    RESOLVER_SCHEMA,
    snapshot_from_revision,
)
from packages.core.comfort_schedule import timezone_data_version
from packages.core.heat_curve import HeatCurveConfig
from packages.optimizer.main import (
    _apply_authoritative_contract_snapshot,
    _target_schedule_matches_contract,
)
from packages.optimizer.rules_engine import RulesOptimizer
from packages.api.routers.models_router import _plan_forecast_window

UTC = dt.timezone.utc
API_TOKEN = "p21-isolated-test-token"
INSTALLATION_ID = UUID("00000000-0000-0000-0000-000000000017")
HOUSE_ID = UUID("00000000-0000-0000-0000-000000000027")
CURRENT_SETTINGS = {
    "comfort_schedule": '{"weekday":[8],"weekend":[9]}',
    "comfort_temp_min": "18",
    "comfort_temp_target": "20.5",
    "comfort_temp_max": "25",
    "timezone": "UTC",
}


@pytest.mark.parametrize("route", ["canonical", "generic", "compatibility"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"weekday": [True], "weekend": []},
        {"weekday": [6, 6], "weekend": []},
        {"weekday": [24], "weekend": []},
        {"weekday": [-1], "weekend": []},
        {"weekday": ["6"], "weekend": []},
        {"weekday": "6", "weekend": []},
        {"weekday": [6.5], "weekend": []},
        {"weekday": [], "weekend": [], "unknown": []},
    ],
)
async def test_all_schedule_http_writers_reject_raw_invalid_without_mutation(
    monkeypatch, route, invalid
):
    session = _session_context(monkeypatch)
    read = AsyncMock(return_value=CURRENT_SETTINGS)
    write = AsyncMock()
    monkeypatch.setattr(settings_router, "get_all_settings", read)
    monkeypatch.setattr(settings_router, "set_settings_bulk", write)
    monkeypatch.setattr(
        settings_router, "ensure_current_contract", AsyncMock(return_value=_revision())
    )
    path, body = {
        "canonical": (
            f"/api/installations/{INSTALLATION_ID}/planning-settings",
            {"comfort_schedule": invalid},
        ),
        "generic": ("/api/settings", {"settings": {"comfort_schedule": json.dumps(invalid)}}),
        "compatibility": ("/api/comfort-schedule", invalid),
    }[route]
    response = await _request(
        _app(monkeypatch),
        "PUT",
        path,
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"7"'},
        json=body,
    )
    assert 400 <= response.status_code < 500, response.text
    write.assert_not_awaited()
    session.add.assert_not_called()


def _revision(revision_id: int = 7):
    projection = _settings_projection(CURRENT_SETTINGS, INSTALLATION_ID, HOUSE_ID)
    return SimpleNamespace(
        id=revision_id,
        contract_json={**projection, "field_sources": {}},
        contract_hash=_contract_hash(projection),
        effective_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
        source="effective_settings_import",
    )


def _session_context(monkeypatch, installation_id=INSTALLATION_ID):
    installation = SimpleNamespace(installation_id=installation_id, house_id=HOUSE_ID)
    session = MagicMock()
    session.get = AsyncMock(return_value=installation)
    session.execute = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(settings_router, "get_session", lambda: context)
    return session


def _app(monkeypatch) -> FastAPI:
    from packages.api import auth

    monkeypatch.setattr(auth.settings, "api_token", API_TOKEN)
    app = FastAPI(dependencies=[Depends(require_auth)])
    app.include_router(settings_router.router)
    return app


async def _request(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://p21.invalid",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_canonical_get_is_installation_scoped_authenticated_and_revisioned(monkeypatch):
    _session_context(monkeypatch)
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(return_value=CURRENT_SETTINGS),
    )
    revision = _revision()
    ensure = AsyncMock(return_value=revision)
    monkeypatch.setattr(settings_router, "ensure_current_contract", ensure)
    app = _app(monkeypatch)

    response = await _request(
        app,
        "GET",
        f"/api/installations/{INSTALLATION_ID}/planning-settings",
        headers={"Authorization": f"Bearer {API_TOKEN}", "X-Role": "admin"},
    )

    assert response.status_code == 200
    assert response.headers["ETag"] == '"7"'
    assert response.headers["X-Contract-Revision"] == "7"
    assert response.headers["X-Installation-ID"] == str(INSTALLATION_ID)
    assert response.json()["comfort_schedule"] == {"weekday": [8], "weekend": [9]}
    assert response.json()["ordinary_writes_allowed"] is False
    ensure.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "admin", "owner"])
@pytest.mark.parametrize(
    ("method", "suffix", "body", "if_match"),
    [
        ("GET", "", None, None),
        ("PUT", "", {"comfort_temp_target": 21}, '"7"'),
        (
            "POST",
            "/preview",
            {
                "comfort_schedule": {"weekday": [8], "weekend": [9]},
                "horizon_start": "2026-01-05T07:00:00Z",
                "horizon_end": "2026-01-05T08:00:00Z",
            },
            '"7"',
        ),
    ],
)
async def test_fake_roles_cannot_replace_bearer_authentication(
    monkeypatch, role, method, suffix, body, if_match
):
    session = _session_context(monkeypatch)
    app = _app(monkeypatch)
    path = f"/api/installations/{INSTALLATION_ID}/planning-settings{suffix}"
    headers = {"X-Role": role, "X-User": "untrusted-user"}
    if if_match:
        headers["If-Match"] = if_match

    missing = await _request(app, method, path, headers=headers, json=body)
    invalid = await _request(
        app,
        method,
        path,
        headers={**headers, "Authorization": "Bearer wrong-test-token"},
        json=body,
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 403
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_settings_rejects_invalid_bearer_token(monkeypatch):
    _session_context(monkeypatch)
    app = _app(monkeypatch)

    response = await _request(
        app,
        "GET",
        f"/api/installations/{INSTALLATION_ID}/planning-settings",
        headers={"Authorization": "Bearer not-the-test-token"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body", "if_match"),
    [
        ("GET", "", None, None),
        ("PUT", "", {"comfort_temp_target": 21}, '"7"'),
        (
            "POST",
            "/preview",
            {
                "comfort_schedule": {"weekday": [8], "weekend": [9]},
                "horizon_start": "2026-01-05T07:00:00Z",
                "horizon_end": "2026-01-05T08:00:00Z",
            },
            '"7"',
        ),
    ],
)
async def test_canonical_settings_rejects_wrong_installation_before_read_or_write(
    monkeypatch, method, suffix, body, if_match
):
    _session_context(monkeypatch, installation_id=uuid4())
    read = AsyncMock(return_value=CURRENT_SETTINGS)
    write = AsyncMock()
    monkeypatch.setattr(settings_router, "get_all_settings", read)
    monkeypatch.setattr(settings_router, "set_settings_bulk", write)
    app = _app(monkeypatch)

    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    if if_match:
        headers["If-Match"] = if_match
    response = await _request(
        app,
        method,
        f"/api/installations/{INSTALLATION_ID}/planning-settings{suffix}",
        headers=headers,
        json=body,
    )

    assert response.status_code == 404
    read.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_put_uses_shared_validation_and_if_match_transaction(monkeypatch):
    session = _session_context(monkeypatch)
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(return_value=CURRENT_SETTINGS),
    )
    previous, updated = _revision(7), _revision(8)
    monkeypatch.setattr(
        settings_router,
        "ensure_current_contract",
        AsyncMock(side_effect=[previous, updated]),
    )
    write = AsyncMock(return_value=8)
    monkeypatch.setattr(settings_router, "set_settings_bulk", write)
    app = _app(monkeypatch)

    response = await _request(
        app,
        "PUT",
        f"/api/installations/{INSTALLATION_ID}/planning-settings",
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"7"'},
        json={"comfort_schedule": {"weekday": [9, 7], "weekend": []}},
    )

    assert response.status_code == 200
    assert response.headers["X-Contract-Revision"] == "8"
    write.assert_awaited_once_with(
        {"comfort_schedule": '{"weekday":[7,9],"weekend":[]}'},
        session=session,
        expected_revision_id=7,
        source="scoped_planning_settings_api",
    )


@pytest.mark.asyncio
async def test_canonical_put_requires_if_match_and_reports_stale_revision(monkeypatch):
    _session_context(monkeypatch)
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(return_value=CURRENT_SETTINGS),
    )
    monkeypatch.setattr(
        settings_router, "ensure_current_contract", AsyncMock(return_value=_revision())
    )
    write = AsyncMock(side_effect=settings_router.ContractRevisionConflict(8))
    monkeypatch.setattr(settings_router, "set_settings_bulk", write)
    app = _app(monkeypatch)

    missing = await _request(
        app,
        "PUT",
        f"/api/installations/{INSTALLATION_ID}/planning-settings",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        json={"comfort_temp_target": 21},
    )
    stale = await _request(
        app,
        "PUT",
        f"/api/installations/{INSTALLATION_ID}/planning-settings",
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"7"'},
        json={"comfort_temp_target": 21},
    )

    assert missing.status_code == 428
    assert stale.status_code == 409
    assert stale.headers["X-Contract-Revision"] == "8"
    assert stale.json()["detail"]["reason"] == "contract_revision_changed"


@pytest.mark.asyncio
async def test_canonical_preview_parity_with_frozen_replay_snapshot(monkeypatch):
    session = _session_context(monkeypatch)
    proposed_settings = {
        **CURRENT_SETTINGS,
        "comfort_schedule": '{"weekday":[8],"weekend":[9]}',
    }
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(return_value=proposed_settings),
    )
    revision = _revision()
    monkeypatch.setattr(
        settings_router, "ensure_current_contract", AsyncMock(return_value=revision)
    )
    write = AsyncMock()
    monkeypatch.setattr(settings_router, "set_settings_bulk", write)
    app = _app(monkeypatch)
    start = dt.datetime(2026, 1, 5, 7, tzinfo=UTC)
    end = dt.datetime(2026, 1, 5, 10, tzinfo=UTC)

    response = await _request(
        app,
        "POST",
        f"/api/installations/{INSTALLATION_ID}/planning-settings/preview",
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"7"'},
        json={
            "comfort_schedule": {"weekday": [8], "weekend": [9]},
            "horizon_start": start.isoformat(),
            "horizon_end": end.isoformat(),
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["preview_only"] is True
    assert preview["ordinary_writes_allowed"] is False
    write.assert_not_awaited()

    session.get.side_effect = None
    session.get.return_value = revision
    snapshot = await snapshot_from_revision(
        session,
        revision.id,
        start,
        end,
    )
    canonical = canonical_target_schedule(snapshot)
    assert preview["target_schedule"] == canonical
    assert preview["proposed_contract_hash"] == snapshot["contract_hash"]
    assert preview["proposed_contract"]["comfort_schedule"] == {"weekday": [8], "weekend": [9]}
    assert snapshot["policy_dependency_fingerprint"] == policy_dependency_fingerprint(snapshot)
    assert snapshot["resolver_schema"] == RESOLVER_SCHEMA
    assert snapshot["tzdata_version"] == timezone_data_version()


@pytest.mark.asyncio
async def test_frozen_schedule_is_shared_by_rules_milp_fallback_forecast_and_replay():
    session = AsyncMock()
    revision = _revision()
    session.get.return_value = revision
    start = dt.datetime(2026, 1, 5, 8, tzinfo=UTC)
    end = start + dt.timedelta(hours=2)
    snapshot = await snapshot_from_revision(session, revision.id, start, end)
    canonical = canonical_target_schedule(snapshot)
    rules_forecast = RulesOptimizer._build_forecast_snapshot(
        prices=[(start, 0.1), (start + dt.timedelta(hours=1), 0.2)],
        weather=[(start, 2.0), (start + dt.timedelta(hours=1), 3.0)],
        weather_full=[],
        actions=[],
        horizon_start=start,
        current_indoor=None,
        current_water_temp=45.0,
        heat_curve=HeatCurveConfig(),
        comfort_schedule=snapshot["comfort_schedule"],
        comfort_temp_target=snapshot["comfort_temp_target"],
        comfort_temp_min=snapshot["comfort_temp_min"],
        tz_name=snapshot["timezone"],
    )
    assert _target_schedule_matches_contract(
        {"target_schedule": rules_forecast["target_schedule"]},
        canonical,
    )

    replayed = await snapshot_from_revision(session, revision.id, start, end)
    assert canonical_target_schedule(replayed) == canonical

    forecast_window = _plan_forecast_window(
        SimpleNamespace(
            contract_revision_id=revision.id,
            contract_hash=revision.contract_hash,
            horizon_start=start,
            horizon_end=end,
            plan_json=json.dumps(
                {
                    "house_contract_snapshot": snapshot,
                    "forecast_snapshot": {
                        "version": "indoor_forecast_v5",
                        "forecast_status": "unavailable",
                        "target_schedule": canonical,
                        "weather_forecast": [],
                        "price_forecast": [],
                    },
                }
            ),
        ),
        start,
        24,
    )
    assert forecast_window["target_schedule"] == canonical

    candidate_targets = [
        {key: interval[key] for key in ("ts", "target", "comfort_hour")} for interval in canonical
    ]
    for layer in ("rules", "milp", "milp_fallback"):
        forecast_snapshot = {"target_schedule": list(candidate_targets)}
        plan = {
            "layer": layer,
            "target_schedule": list(candidate_targets),
            "forecast_snapshot": forecast_snapshot,
        }
        assert _apply_authoritative_contract_snapshot(plan, snapshot) == canonical
        assert plan["target_schedule"] == canonical
        assert plan["forecast_snapshot"]["target_schedule"] == canonical
        assert plan["forecast_snapshot"]["house_contract_snapshot"] is snapshot
        assert plan["house_contract_snapshot"] is snapshot

    stale = {"target_schedule": [{**candidate_targets[0], "target": 19.0}]}
    assert not _target_schedule_matches_contract(stale, canonical)


@pytest.mark.asyncio
async def test_preview_rejects_stale_revision_or_missing_schedule_patch(monkeypatch):
    _session_context(monkeypatch)
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(return_value=CURRENT_SETTINGS),
    )
    monkeypatch.setattr(
        settings_router, "ensure_current_contract", AsyncMock(return_value=_revision(8))
    )
    app = _app(monkeypatch)
    body = {
        "comfort_schedule": {"weekday": [8], "weekend": [9]},
        "horizon_start": "2026-01-05T07:00:00Z",
        "horizon_end": "2026-01-05T08:00:00Z",
    }

    stale = await _request(
        app,
        "POST",
        f"/api/installations/{INSTALLATION_ID}/planning-settings/preview",
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"7"'},
        json=body,
    )
    empty = await _request(
        app,
        "POST",
        f"/api/installations/{INSTALLATION_ID}/planning-settings/preview",
        headers={"Authorization": f"Bearer {API_TOKEN}", "If-Match": '"8"'},
        json={key: value for key, value in body.items() if key not in {"comfort_schedule"}},
    )

    assert stale.status_code == 409
    assert empty.status_code == 422

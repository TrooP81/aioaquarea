from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from packages.core.backup_health import BackupPolicy, project_backup_health
from packages.core.operational_alerts import (
    get_operational_alerts,
    operational_webhook_payload,
)
from tests.test_backup_health import evidence, NOW


async def test_disabled_alert_delivery_does_not_hide_backup_health():
    health = project_backup_health(None, BackupPolicy(), now=NOW)
    with (
        patch("packages.core.operational_alerts.get_bool_setting", AsyncMock(return_value=False)),
        patch(
            "packages.core.operational_alerts.load_backup_health", AsyncMock(return_value=health)
        ),
    ):
        result = await get_operational_alerts(now=NOW)
    assert result["enabled"] is False
    assert result["backup"] == health
    assert not result["backup"]["ready"]


def test_webhook_is_explicit_nested_allowlist_and_stable_without_dynamic_ages():
    internal = {
        "enabled": True,
        "generated_at": NOW.isoformat(),
        "backup": {"credentials": "SENTINEL"},
        "password": "SENTINEL",
        "summary": {"warning": 1, "path": "SENTINEL"},
        "alerts": [
            {
                "id": "backup_protection_degraded",
                "severity": "warning",
                "title": "Database backup protection needs attention",
                "detail": "Backup evidence is degraded or has a replica warning.",
                "path": "SENTINEL",
                "details": {
                    "reason_codes": ["backup_attempt_failed", "SENTINEL"],
                    "last_success": NOW.isoformat(),
                    "archive_id": "SENTINEL",
                    "sha256": "SENTINEL",
                    "raw_error": "SENTINEL",
                    "success_age_seconds": 100,
                },
            }
        ],
    }
    first = operational_webhook_payload(internal)
    assert "SENTINEL" not in json.dumps(first)
    assert set(first) == {"source", "enabled", "generated_at", "summary", "alerts"}
    internal["alerts"][0]["details"]["success_age_seconds"] = 500
    assert first["alerts"] == operational_webhook_payload(internal)["alerts"]


@pytest.mark.parametrize("failed", [False, True])
async def test_readiness_shares_projection_on_both_200_and_503(failed):
    from packages.api.routers.models_router import readiness

    state = evidence()
    if failed:
        state["last_attempt"]["status"] = "failed"
    health = project_backup_health(state, BackupPolicy(), now=NOW)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [SimpleNamespace(service=service, updated_at=now) for service in ("poller", "optimizer")]
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        )
    )

    @asynccontextmanager
    async def context():
        yield session

    quality = {
        "ready": True,
        "timestamp": now,
        "credentials_configured": True,
        "age_seconds": 0,
        "threshold_seconds": 300,
        "reasons": [],
    }
    with (
        patch("packages.core.backup_health.load_backup_health", AsyncMock(return_value=health)),
        patch("packages.api.routers.models_router.get_session", context),
        patch(
            "packages.core.device_data_quality.get_device_data_quality",
            AsyncMock(return_value=quality),
        ),
        patch(
            "packages.core.planning_data_quality.get_planning_data_quality",
            AsyncMock(return_value={"control_allowed": True}),
        ),
    ):
        if failed:
            with pytest.raises(HTTPException) as error:
                await readiness()
            assert error.value.status_code == 503
            body = error.value.detail
            assert body["stale"] == ["backup"]
        else:
            body = await readiness()
    assert body["backup"] == health
    assert "private-" not in json.dumps(body, default=str)

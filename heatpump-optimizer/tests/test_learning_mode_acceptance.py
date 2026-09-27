from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from packages.api.routers import optimizer as optimizer_router
from packages.ml import seasonal_learning


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_forced_enable_persists_audit_snapshot_with_obligations():
    session = MagicMock()
    obligations = {
        "count": 1,
        "oldest_scheduled_at": dt.datetime(2026, 9, 27, 10, tzinfo=dt.timezone.utc),
        "oldest_age_seconds": 7200,
        "action_types": ["zone_temp_restore"],
    }
    with (
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=False)),
        patch.object(
            optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value=obligations)
        ),
        patch("packages.core.settings_service.set_settings_bulk", new=AsyncMock()) as write,
        patch.object(
            optimizer_router,
            "_learning_mode_status",
            new=AsyncMock(return_value={"enabled": True}),
        ),
        patch.object(optimizer_router, "get_session", return_value=_AsyncContext(session)),
    ):
        result = await optimizer_router.set_learning_mode(
            optimizer_router.LearningModeUpdate(enabled=True), force=True
        )

    assert result == {"enabled": True}
    updates = write.await_args.args[0]
    assert updates["learning_mode_enabled"] == "true"
    assert dt.datetime.fromisoformat(updates["learning_mode_since"]).tzinfo is not None
    audit = session.add.call_args.args[0]
    assert audit.action == "set_learning_mode"
    assert audit.result == "enabled"
    payload = json.loads(audit.payload_json)
    assert payload["enabled"] is True
    assert payload["force"] is True
    assert payload["obligations"]["count"] == 1
    assert payload["obligations"]["action_types"] == ["zone_temp_restore"]
    assert payload["obligations"]["oldest_scheduled_at"] == "2026-09-27T10:00:00+00:00"


@pytest.mark.asyncio
async def test_learning_mode_rejection_serializes_obligation_snapshot():
    session = MagicMock()
    obligations = {
        "count": 1,
        "oldest_scheduled_at": dt.datetime(2026, 9, 27, 10, tzinfo=dt.timezone.utc),
        "oldest_age_seconds": 7200,
        "action_types": ["zone_temp_restore"],
    }
    with (
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=False)),
        patch.object(
            optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value=obligations)
        ),
        patch.object(optimizer_router, "get_session", return_value=_AsyncContext(session)),
        pytest.raises(HTTPException) as error,
    ):
        await optimizer_router.set_learning_mode(
            optimizer_router.LearningModeUpdate(enabled=True), force=False
        )

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "unresolved_safety_reverts",
        "obligations": {
            "count": 1,
            "oldest_scheduled_at": "2026-09-27T10:00:00+00:00",
            "oldest_age_seconds": 7200,
            "action_types": ["zone_temp_restore"],
        },
    }
    json.dumps(error.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "obligations",
    [
        {"count": 0, "oldest_scheduled_at": None, "oldest_age_seconds": None, "action_types": []},
        {
            "count": 3,
            "oldest_scheduled_at": "2026-09-27T08:00:00+00:00",
            "oldest_age_seconds": 7200,
            "action_types": ["force_dhw_off", "zone_temp_restore"],
        },
        {
            "count": 1,
            "oldest_scheduled_at": "2026-09-27T12:01:00+00:00",
            "oldest_age_seconds": 0,
            "action_types": ["force_dhw_off"],
        },
        {
            "count": 1,
            "oldest_scheduled_at": "2026-09-27T10:00:00+00:00",
            "oldest_age_seconds": 7200,
            "action_types": ["force_dhw_off"],
        },
    ],
    ids=["zero", "multiple", "future", "overdue"],
)
async def test_learning_mode_status_preserves_obligation_contract(obligations):
    with (
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=False)),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value="")),
        patch.object(
            optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value=obligations)
        ),
        patch(
            "packages.ml.seasonal_learning.get_seasonal_calibration_status",
            new=AsyncMock(return_value={}),
        ),
        patch.object(optimizer_router, "get_session", return_value=_AsyncContext(MagicMock())),
    ):
        result = await optimizer_router._learning_mode_status()

    assert result["enabled"] is False
    assert result["effective_active"] is False
    assert result["sources"] == []
    assert result["open_revert_obligations"] == obligations
    assert result["state_reliable"] is True


@pytest.mark.asyncio
async def test_seasonal_learning_deferral_timestamp_is_set_then_cleared_and_reactivates():
    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    session = MagicMock()
    query_result = SimpleNamespace(one=lambda: (5.0, 24))
    unresolved = [
        {
            "count": 1,
            "oldest_scheduled_at": now - dt.timedelta(hours=1),
            "oldest_age_seconds": 3600,
            "action_types": ["force_dhw_off"],
        },
        {"count": 0, "oldest_scheduled_at": None, "oldest_age_seconds": None, "action_types": []},
    ]
    deferred_at = (now - dt.timedelta(minutes=30)).isoformat()
    with (
        patch.object(
            seasonal_learning,
            "_settings",
            new=AsyncMock(return_value=(True, 12.0, 7, True, True)),
        ),
        patch.object(seasonal_learning, "get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_summary",
            new=AsyncMock(side_effect=unresolved),
        ),
        patch.object(
            seasonal_learning, "get_setting", new=AsyncMock(side_effect=["", deferred_at])
        ),
        patch.object(seasonal_learning, "set_settings_bulk", new=AsyncMock()) as write,
    ):
        session.execute = AsyncMock(return_value=query_result)
        blocked = await seasonal_learning.get_seasonal_calibration_status(now=now)
        active = await seasonal_learning.get_seasonal_calibration_status(now=now)

    assert blocked["observe_only_active"] is False
    assert blocked["reason"] == "blocked_by_unresolved_safety_revert"
    assert blocked["seasonal_blocked_by_unresolved_safety_revert"] is True
    assert blocked["seasonal_first_deferred_at"] == now.isoformat()
    assert blocked["seasonal_deferred_seconds"] == 0
    assert active["observe_only_active"] is True
    assert active["seasonal_blocked_by_unresolved_safety_revert"] is False
    assert active["seasonal_first_deferred_at"] is None
    assert active["seasonal_deferred_seconds"] is None
    assert write.await_args_list[0].args == (
        {"_seasonal_calibration_safety_deferred_since": now.isoformat()},
    )
    assert write.await_args_list[1].args == ({"_seasonal_calibration_safety_deferred_since": ""},)

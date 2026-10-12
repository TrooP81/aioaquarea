from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from packages.api.routers import optimizer as optimizer_router
from packages.core.learning_state import LearningStateSnapshot, get_learning_state_details
from packages.ml import seasonal_learning
from packages.optimizer.executor_core import LearningModeState, resolve_learning_mode_state


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


@pytest.mark.parametrize("case", ["cold", "restore", "warming", "optout", "lock_timeout"])
async def test_UR4_SR3_cycle_atomic_latch_and_precedence(case):
    from packages.core.settings_service import SETTING_SPECS

    now = dt.datetime(2026, 10, 11, 12, tzinfo=dt.timezone.utc)
    values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    values["seasonal_calibration_enabled"] = "true"
    values["seasonal_calibration_max_outdoor_c"] = "12"
    values["seasonal_calibration_window_days"] = "7"
    evidence = {
        "average_outdoor": 5.0,
        "outdoor_readings": 24,
        "open_revert_obligations": {"count": 0},
    }
    if case == "warming":
        values["_seasonal_calibration_campaign_since"] = (now - dt.timedelta(days=2)).isoformat()
        evidence["average_outdoor"] = 18.0
    session = AsyncMock()
    entered = []

    class Context(_AsyncContext):
        async def __aexit__(self, *args):
            entered.append("closed")
            return False

    async def execute(statement, *_args):
        sql = str(statement)
        entered.append(sql)
        if case == "lock_timeout" and "LOCK TABLE" in sql:
            raise TimeoutError("owned contention")
        return SimpleNamespace(one=lambda: (evidence["average_outdoor"], 24))

    async def read_values(**_kwargs):
        if case == "optout" and any("LOCK TABLE" in entry for entry in entered):
            return {**values, "seasonal_calibration_enabled": "false"}
        return dict(values)

    async def write(updates, *, session):
        assert entered.index(
            next(entry for entry in entered if "pg_advisory" in entry)
        ) < entered.index(next(entry for entry in entered if "LOCK TABLE" in entry))
        values.update(updates)

    session.execute.side_effect = execute
    with (
        patch.object(seasonal_learning, "get_session", return_value=Context(session)),
        patch.object(seasonal_learning, "get_all_settings", side_effect=read_values),
        patch(
            "packages.core.safety_reverts.unresolved_revert_summary",
            new=AsyncMock(return_value={"count": 1 if case == "restore" else 0}),
        ),
        patch.object(seasonal_learning, "set_settings_bulk", side_effect=write) as writes,
        patch.object(seasonal_learning, "get_bool_setting", new=AsyncMock(return_value=False)),
        patch("packages.ml.lifecycle.enqueue_maintenance_request", new=AsyncMock()) as enqueue,
    ):
        result = await seasonal_learning.run_seasonal_calibration_cycle(now=now)
    enqueue.assert_not_awaited()
    if case == "cold":
        assert result["observe_only_active"] is True
        assert result["campaign_since"] == now.isoformat()
        assert result["cycle"] == "maintenance_disabled"
        writes.assert_awaited_once()
        assert writes.await_args.kwargs["session"] is session
    elif case == "restore":
        assert result["observe_only_active"] is False
        assert result["seasonal_first_deferred_at"] == now.isoformat()
        assert not values["_seasonal_calibration_campaign_since"]
    elif case == "warming":
        assert result["observe_only_active"] is True
        assert result["campaign_since"] == (now - dt.timedelta(days=2)).isoformat()
        writes.assert_not_awaited()
    else:
        assert result["observe_only_active"] is False
        writes.assert_not_awaited()
    assert "closed" in entered


@pytest.mark.asyncio
async def test_manual_learning_survives_seasonal_lookup_failure():
    with (
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=True)),
        patch(
            "packages.ml.seasonal_learning.get_seasonal_calibration_status",
            new=AsyncMock(side_effect=RuntimeError("seasonal unavailable")),
        ) as seasonal,
    ):
        snapshot, details = await get_learning_state_details()
        executor_state = await resolve_learning_mode_state()

    assert snapshot == LearningStateSnapshot(True, False, True)
    assert details == {}
    assert executor_state is LearningModeState.ACTIVE
    seasonal.assert_not_awaited()


@pytest.mark.asyncio
async def test_learning_mode_endpoint_keeps_manual_toggle_when_seasonal_lookup_fails():
    with (
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=True)),
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value="")),
        patch(
            "packages.ml.seasonal_learning.get_seasonal_calibration_status",
            new=AsyncMock(side_effect=RuntimeError("seasonal unavailable")),
        ) as seasonal,
        patch.object(optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value={})),
        patch.object(optimizer_router, "get_session", return_value=_AsyncContext(MagicMock())),
    ):
        result = await optimizer_router._learning_mode_status()

    assert result["enabled"] is True
    assert result["effective_active"] is True
    assert result["state_reliable"] is True
    seasonal.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_learning_read_failure_remains_unknown():
    with patch(
        "packages.core.settings_service.get_bool_setting",
        new=AsyncMock(side_effect=RuntimeError("settings unavailable")),
    ):
        snapshot, _ = await get_learning_state_details()
        executor_state = await resolve_learning_mode_state()

    assert snapshot == LearningStateSnapshot(False, False, False)
    assert executor_state is LearningModeState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manual_enabled, manual_raises, seasonal_active, seasonal_raises, expected_snapshot, expected_executor, seasonal_called",
    [
        (
            True,
            False,
            True,
            False,
            LearningStateSnapshot(True, False, True),
            LearningModeState.ACTIVE,
            False,
        ),
        (
            True,
            False,
            False,
            False,
            LearningStateSnapshot(True, False, True),
            LearningModeState.ACTIVE,
            False,
        ),
        (
            True,
            False,
            False,
            True,
            LearningStateSnapshot(True, False, True),
            LearningModeState.ACTIVE,
            False,
        ),
        (
            False,
            False,
            True,
            False,
            LearningStateSnapshot(False, True, True),
            LearningModeState.ACTIVE,
            True,
        ),
        (
            False,
            False,
            False,
            False,
            LearningStateSnapshot(False, False, True),
            LearningModeState.INACTIVE,
            True,
        ),
        (
            False,
            False,
            False,
            True,
            LearningStateSnapshot(False, False, False),
            LearningModeState.UNKNOWN,
            True,
        ),
        (
            False,
            True,
            True,
            False,
            LearningStateSnapshot(False, False, False),
            LearningModeState.UNKNOWN,
            False,
        ),
        (
            False,
            True,
            False,
            False,
            LearningStateSnapshot(False, False, False),
            LearningModeState.UNKNOWN,
            False,
        ),
        (
            False,
            True,
            False,
            True,
            LearningStateSnapshot(False, False, False),
            LearningModeState.UNKNOWN,
            False,
        ),
    ],
    ids=[
        "manual-seasonal-active",
        "manual-seasonal-inactive",
        "manual-seasonal-error",
        "seasonal-active",
        "inactive",
        "seasonal-error",
        "manual-error-seasonal-active",
        "manual-error-seasonal-inactive",
        "manual-error-seasonal-error",
    ],
)
async def test_real_learning_state_matrix_preserves_executor_semantics(
    manual_enabled,
    manual_raises,
    seasonal_active,
    seasonal_raises,
    expected_snapshot,
    expected_executor,
    seasonal_called,
):
    manual = AsyncMock(
        side_effect=RuntimeError("settings unavailable") if manual_raises else None,
        return_value=manual_enabled,
    )
    seasonal = AsyncMock(
        side_effect=RuntimeError("seasonal unavailable") if seasonal_raises else None,
        return_value={"observe_only_active": seasonal_active},
    )
    with (
        patch("packages.core.settings_service.get_bool_setting", new=manual),
        patch("packages.ml.seasonal_learning.get_seasonal_calibration_status", new=seasonal),
    ):
        snapshot, _ = await get_learning_state_details()
        executor_state = await resolve_learning_mode_state()

    assert snapshot == expected_snapshot
    assert executor_state is expected_executor
    if seasonal_called:
        assert seasonal.await_count == 2
    else:
        seasonal.assert_not_awaited()


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
        patch("packages.core.settings_service.get_setting", new=AsyncMock(return_value="")),
        patch(
            "packages.core.learning_state.get_learning_state_details",
            new=AsyncMock(return_value=(LearningStateSnapshot(False, False, True), {})),
        ),
        patch.object(
            optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value=obligations)
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
async def test_learning_mode_status_preserves_wire_semantics_for_manual_and_seasonal_sources():
    seasonal = {
        "seasonal_blocked_by_unresolved_safety_revert": False,
        "seasonal_first_deferred_at": None,
        "seasonal_deferred_seconds": None,
    }
    with (
        patch(
            "packages.core.settings_service.get_setting",
            new=AsyncMock(return_value="2026-09-27T12:00:00+00:00"),
        ),
        patch(
            "packages.core.learning_state.get_learning_state_details",
            new=AsyncMock(return_value=(LearningStateSnapshot(True, True, True), seasonal)),
        ),
        patch.object(optimizer_router, "unresolved_revert_summary", new=AsyncMock(return_value={})),
        patch.object(optimizer_router, "get_session", return_value=_AsyncContext(MagicMock())),
    ):
        result = await optimizer_router._learning_mode_status()

    assert result["enabled"] is True
    assert result["effective_active"] is True
    assert result["sources"] == ["manual", "seasonal"]
    assert result["state_reliable"] is True
    assert result["build_policy"] == "observe_only"
    assert result["ordinary_writes_allowed"] is False
    assert result["optimization_requires_new_build"] is True
    assert result["linked_restores_eligible"] is False
    assert set(result) == {
        "enabled",
        "build_policy",
        "ordinary_writes_allowed",
        "optimization_requires_new_build",
        "linked_restores_eligible",
        "since",
        "days_elapsed",
        "effective_active",
        "sources",
        "state_reliable",
        "open_revert_obligations",
        "seasonal_blocked_by_unresolved_safety_revert",
        "seasonal_first_deferred_at",
        "seasonal_deferred_seconds",
    }


@pytest.mark.asyncio
async def test_seasonal_learning_deferral_timestamp_is_set_then_cleared_and_reactivates():
    from packages.core.settings_service import SETTING_SPECS

    now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
    session = MagicMock()
    query_result = SimpleNamespace(one=lambda: (5.0, 24))
    values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    values.update(
        {
            "seasonal_calibration_enabled": "true",
            "seasonal_calibration_max_outdoor_c": "12",
            "seasonal_calibration_window_days": "7",
        }
    )
    unresolved = {
        "count": 1,
        "oldest_scheduled_at": now - dt.timedelta(hours=1),
        "oldest_age_seconds": 3600,
        "action_types": ["force_dhw_off"],
    }

    async def write_settings(updates, *, session):
        values.update(updates)

    with (
        patch.object(
            seasonal_learning,
            "get_all_settings",
            new=AsyncMock(side_effect=lambda **kwargs: dict(values)),
        ),
        patch.object(seasonal_learning, "get_session", return_value=_AsyncContext(session)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_summary",
            new=AsyncMock(side_effect=lambda *args, **kwargs: dict(unresolved)),
        ),
        patch.object(
            seasonal_learning, "set_settings_bulk", new=AsyncMock(side_effect=write_settings)
        ) as write,
        patch.object(seasonal_learning, "get_bool_setting", new=AsyncMock(return_value=False)),
    ):
        session.execute = AsyncMock(return_value=query_result)
        before_cycle = await seasonal_learning.get_seasonal_calibration_status(now=now)
        assert before_cycle["observe_only_active"] is False
        assert before_cycle["seasonal_first_deferred_at"] is None
        write.assert_not_awaited()
        blocked = await seasonal_learning.run_seasonal_calibration_cycle(now=now)
        unresolved.update(
            count=0, oldest_scheduled_at=None, oldest_age_seconds=None, action_types=[]
        )
        pending = await seasonal_learning.get_seasonal_calibration_status(now=now)
        assert pending["observe_only_active"] is False
        assert pending["reason"] == "awaiting_explicit_seasonal_cycle"
        assert write.await_count == 1
        await seasonal_learning.run_seasonal_calibration_cycle(now=now)
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
    assert write.await_args_list[1].args == (
        {
            "_seasonal_calibration_campaign_since": now.isoformat(),
            "_seasonal_calibration_safety_deferred_since": "",
        },
    )
    assert write.await_count == 2
    assert all(call.kwargs["session"] is session for call in write.await_args_list)


@pytest.mark.asyncio
async def test_seasonal_success_warming_and_manual_training_never_clear_observation():
    from packages.api.routers import models_router
    from packages.core.settings_service import SETTING_SPECS

    now = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
    persisted = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    persisted.update(
        {
            "seasonal_calibration_enabled": "true",
            "learning_mode_enabled": "true",
            "seasonal_calibration_max_outdoor_c": "12",
            "seasonal_calibration_window_days": "7",
        }
    )
    outdoor = [5.0, 24]
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(one=lambda: tuple(outdoor)))
    demand = MagicMock(is_trained=True, arefresh_committed=AsyncMock(return_value=True))
    demand.training_data_quality = AsyncMock(
        return_value={
            "ready_to_train": True,
            "usable_samples": 168,
            "minimum_samples": 168,
            "remaining_samples": 0,
        }
    )
    demand.train = AsyncMock(side_effect=AssertionError("training must be owned by lifecycle"))
    thermal = MagicMock(arefresh_if_changed=AsyncMock(return_value=True))
    thermal.params.indoor_heating_samples = 20
    thermal.confidence_for.return_value = "learned"
    enqueue = AsyncMock(
        side_effect=lambda component, source, **kwargs: {
            "request_id": f"{source}-{component}",
            "status": "queued",
            "control_ready": False,
        }
    )

    async def write_settings(updates, *, session):
        persisted.update(updates)

    with (
        patch.object(
            seasonal_learning,
            "get_all_settings",
            new=AsyncMock(side_effect=lambda **kwargs: dict(persisted)),
        ),
        patch.object(seasonal_learning, "get_session", return_value=_AsyncContext(session)),
        patch.object(seasonal_learning, "set_settings_bulk", new=write_settings),
        patch.object(seasonal_learning, "get_bool_setting", new=AsyncMock(return_value=True)),
        patch.object(
            seasonal_learning,
            "get_seasonal_calibration_progress",
            new=AsyncMock(
                return_value={
                    "demand": {"trained": True},
                    "indoor_heating": {"samples": 64, "remaining_samples": 0},
                }
            ),
        ),
        patch("packages.ml.models.demand_model", demand),
        patch("packages.ml.thermal.thermal_model", thermal),
        patch("packages.ml.lifecycle.enqueue_maintenance_request", enqueue),
        patch.object(models_router, "enqueue_maintenance_request", enqueue),
        patch.object(models_router, "get_session", return_value=_AsyncContext(session)),
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=True)),
        patch(
            "packages.core.safety_reverts.unresolved_revert_summary",
            new=AsyncMock(return_value={"count": 0}),
        ),
    ):
        cold = await seasonal_learning.get_seasonal_calibration_status(now=now)
        assert cold["observe_only_active"] is False
        completed_fit = await seasonal_learning.run_seasonal_calibration_cycle(now=now)
        manual_training = await models_router.trigger_ml_training()
        manual_thermal = await models_router.calibrate_thermal_model()
        manual_comfort = await models_router.trigger_comfort_model_training()
        manual_state, _ = await get_learning_state_details()
        outdoor[:] = [18.0, 24]
        warm = await seasonal_learning.get_seasonal_calibration_status(
            now=now + dt.timedelta(days=1)
        )
        outdoor[:] = [None, 0]
        missing = await seasonal_learning.get_seasonal_calibration_status(
            now=now + dt.timedelta(days=2)
        )

    assert completed_fit["cycle"] == "queued"
    assert completed_fit["demand"]["trained"] is True
    assert manual_training["demand"]["control_ready"] is False
    assert manual_thermal["control_ready"] is manual_comfort["control_ready"] is False
    assert manual_state == LearningStateSnapshot(True, False, True)
    demand.train.assert_not_awaited()
    demand.arefresh_committed.assert_not_awaited()
    thermal.arefresh_if_changed.assert_not_awaited()
    assert completed_fit["observe_only_active"] is True
    assert warm["heating_season_detected"] is False
    assert missing["outdoor_readings"] == 0
    assert warm["observe_only_active"] is missing["observe_only_active"] is True
    assert completed_fit["campaign_since"] == warm["campaign_since"] == missing["campaign_since"]
    assert cold["auto_exit"] is warm["auto_exit"] is missing["auto_exit"] is False
    assert persisted["seasonal_calibration_enabled"] == "true"
    assert persisted["learning_mode_enabled"] == "true"


@pytest.mark.asyncio
async def test_persisted_seasonal_observation_survives_outdoor_lookup_failure():
    from packages.core.settings_service import SETTING_SPECS

    values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    values.update(
        {
            "seasonal_calibration_enabled": "true",
            "_seasonal_calibration_campaign_since": "2026-10-09T12:00:00+00:00",
        }
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("outdoor telemetry unavailable"))
    )
    with (
        patch.object(seasonal_learning, "get_all_settings", new=AsyncMock(return_value=values)),
        patch.object(seasonal_learning, "get_session", return_value=_AsyncContext(session)),
        patch.object(seasonal_learning, "set_settings_bulk", new=AsyncMock()) as write,
        patch("packages.core.settings_service.get_bool_setting", new=AsyncMock(return_value=False)),
    ):
        status = await seasonal_learning.get_seasonal_calibration_status()
        learning, _ = await get_learning_state_details()
        executor = await resolve_learning_mode_state()
    assert status["observe_only_active"] is True
    assert status["outdoor_data_reliable"] is False
    assert learning == LearningStateSnapshot(False, True, True)
    assert executor is LearningModeState.ACTIVE
    write.assert_not_awaited()

"""Independent captured-evidence contracts for the seasonal exception fallback."""

from __future__ import annotations

import copy
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from packages.core.learning_state import get_learning_state_details
from packages.core.settings_service import SETTING_SPECS
from packages.ml.model_status import _unavailable_readiness
from packages.ml.seasonal_learning import get_seasonal_calibration_progress


@pytest.mark.parametrize("campaign_active", [False, True])
@pytest.mark.parametrize(
    "count,next_step",
    [
        (0, "readiness_unavailable"),
        (2, "resolve_current_safety_obligations"),
        (None, "safety_obligations_unavailable"),
    ],
)
async def test_exception_retains_retirement_and_positive_observation(
    monkeypatch, campaign_active, count, next_step
):
    now = dt.datetime(2026, 10, 16, 8, tzinfo=dt.timezone.utc)
    values = {key: spec.serialize_default() for key, spec in SETTING_SPECS.items()}
    values.update(
        learning_mode_enabled="true",
        learning_mode_since="2025-10-16T06:00:00+00:00",
        seasonal_calibration_enabled="true",
        _seasonal_calibration_campaign_since=(
            "2025-10-16T06:00:00+00:00" if campaign_active else ""
        ),
    )
    history = {
        "available": True,
        "data_reset_at": "2026-10-16T06:30:00+00:00",
        "historical_retirement": {
            "disposition": "operator_invalidated",
            "reason": "reset_superseded",
            "physical_state_verified": False,
            "device_write": False,
            "retired_at": "2026-10-16T06:29:00+00:00",
        },
    }
    safety = {"count": count, "action_types": ["force_dhw_off"], "oldest_age_seconds": 60}
    snapshot = _unavailable_readiness(
        "readiness_unavailable",
        as_of=now,
        context={"settings": values, "as_of": now},
        seasonal_inputs={"open_revert_obligations": safety, "reset_history": history},
    )
    before = copy.deepcopy(snapshot)
    failed_status = AsyncMock(side_effect=RuntimeError("isolated seasonal evaluation failure"))
    monkeypatch.setattr(
        "packages.ml.seasonal_learning.get_seasonal_calibration_status", failed_status
    )

    progress = await get_seasonal_calibration_progress(snapshot=snapshot)

    failed_status.assert_awaited_once()
    assert progress["open_revert_obligations"] == safety
    assert progress["reset_history"] == history
    assert progress["next_step"] == next_step
    assert progress["enabled"] is True
    assert progress["observe_only_active"] is campaign_active
    assert progress["reason"] == "seasonal_reporting_unavailable"
    assert progress["auto_exit"] is False
    assert progress["control_ready"] is progress["reporting_ready"] is False
    assert snapshot == before, "read-only fallback changed captured inputs/settings"
    # Positive manual observation remains authoritative despite failed seasonal data.
    state, _ = await get_learning_state_details(
        context=snapshot["context"], materialized=snapshot["seasonal_inputs"]
    )
    assert state.active and state.manual_enabled and state.reliable
    failed_status.assert_awaited_once()

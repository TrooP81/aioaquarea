"""Shared persisted learning-mode state for API and executor consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class LearningStateSnapshot:
    manual_enabled: bool
    seasonal_active: bool
    reliable: bool

    @property
    def active(self) -> bool:
        return self.manual_enabled or self.seasonal_active


async def get_learning_state() -> LearningStateSnapshot:
    """Read manual and seasonal observe-only state without changing it."""

    snapshot, _ = await get_learning_state_details()
    return snapshot


async def get_learning_state_details() -> tuple[LearningStateSnapshot, dict[str, Any]]:
    """Read learning sources and retain seasonal detail for the API response."""

    try:
        from packages.core.settings_service import get_bool_setting

        manual_enabled = await get_bool_setting("learning_mode_enabled")
        if manual_enabled:
            return LearningStateSnapshot(True, False, True), {}
    except Exception as exc:  # noqa: BLE001 - callers must fail closed on incomplete state
        logger.error("learning_state_lookup_failed", error_type=type(exc).__name__, error=str(exc))
        return LearningStateSnapshot(
            manual_enabled=False, seasonal_active=False, reliable=False
        ), {}

    try:
        from packages.ml.seasonal_learning import get_seasonal_calibration_status

        seasonal = await get_seasonal_calibration_status()
        return (
            LearningStateSnapshot(
                manual_enabled=manual_enabled,
                seasonal_active=bool(seasonal.get("observe_only_active")),
                reliable=True,
            ),
            seasonal,
        )
    except Exception as exc:  # noqa: BLE001 - callers must fail closed on incomplete state
        logger.error("learning_state_lookup_failed", error_type=type(exc).__name__, error=str(exc))
        return LearningStateSnapshot(
            manual_enabled=False, seasonal_active=False, reliable=False
        ), {}

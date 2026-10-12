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


async def get_learning_state(
    *, context=None, materialized=None, session=None
) -> LearningStateSnapshot:
    """Read manual and seasonal observe-only state without changing it."""

    snapshot, _ = await get_learning_state_details(
        context=context, materialized=materialized, session=session
    )
    return snapshot


async def get_learning_state_details(
    *, context=None, materialized=None, session=None
) -> tuple[LearningStateSnapshot, dict[str, Any]]:
    """Read learning sources and retain seasonal detail for the API response."""

    try:
        from packages.core.settings_service import SETTING_SPECS, get_bool_setting

        if context is not None:
            manual_enabled = bool(
                SETTING_SPECS["learning_mode_enabled"].parse(
                    context["settings"]["learning_mode_enabled"]
                )
            )
        elif session is not None:
            manual_enabled = await get_bool_setting("learning_mode_enabled", session=session)
        else:
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

        if context is None and materialized is None and session is None:
            seasonal = await get_seasonal_calibration_status()
        else:
            seasonal = await get_seasonal_calibration_status(
                now=context.get("as_of") if context else None,
                context=context,
                materialized=materialized,
                session=session,
            )
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

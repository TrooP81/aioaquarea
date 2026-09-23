"""Compatibility wrapper for the plan executor package split."""

from __future__ import annotations

import asyncio

from . import executor_core
from .executor_core import (
    MAX_ACTIONS_PER_CYCLE,
    VERIFY_POLL_INTERVAL_S,
    VERIFY_REDISPATCH_ATTEMPTS,
    VERIFY_TIMEOUT_S,
    PlanExecutor as _CorePlanExecutor,
    is_learning_mode_active,
)


def get_session(*args, **kwargs):
    return executor_core.get_session(*args, **kwargs)


class PlanExecutor(_CorePlanExecutor):
    """Compatibility subclass that preserves module-level patch points for tests."""

    def __init__(self, wrapper):
        super().__init__(
            wrapper,
            session_factory=lambda *args, **kwargs: get_session(*args, **kwargs),
            sleep=lambda delay: asyncio.sleep(delay),
            learning_check=lambda: is_learning_mode_active(),
        )


__all__ = [
    "MAX_ACTIONS_PER_CYCLE",
    "VERIFY_POLL_INTERVAL_S",
    "VERIFY_REDISPATCH_ATTEMPTS",
    "VERIFY_TIMEOUT_S",
    "PlanExecutor",
    "asyncio",
    "get_session",
    "is_learning_mode_active",
]

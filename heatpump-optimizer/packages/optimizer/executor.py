"""Compatibility wrapper for the plan executor package split."""

from __future__ import annotations

import asyncio

from packages.core.database import get_session

from .executor_core import (
    MAX_ACTIONS_PER_CYCLE,
    VERIFY_POLL_INTERVAL_S,
    VERIFY_REDISPATCH_ATTEMPTS,
    VERIFY_TIMEOUT_S,
    PlanExecutor as _CorePlanExecutor,
)


class PlanExecutor(_CorePlanExecutor):
    """Compatibility subclass that preserves module-level patch points for tests."""

    async def execute_due_actions(self) -> None:
        original_get_session = self.__class__.__mro__[1].execute_due_actions.__globals__["get_session"]
        self.__class__.__mro__[1].execute_due_actions.__globals__["get_session"] = get_session
        try:
            await super().execute_due_actions()
        finally:
            self.__class__.__mro__[1].execute_due_actions.__globals__["get_session"] = original_get_session

    async def _execute_action(self, action) -> None:
        core_method = self.__class__.__mro__[1]._execute_action
        original_get_session = core_method.__globals__["get_session"]
        core_method.__globals__["get_session"] = get_session
        try:
            await super()._execute_action(action)
        finally:
            core_method.__globals__["get_session"] = original_get_session

    async def _verify_with_retry(self, action, payload: dict, expected_state: dict[str, object]) -> None:
        core_method = self.__class__.__mro__[1]._verify_with_retry
        original_get_session = core_method.__globals__["get_session"]
        core_method.__globals__["get_session"] = get_session
        try:
            await super()._verify_with_retry(action, payload, expected_state)
        finally:
            core_method.__globals__["get_session"] = original_get_session

    async def _poll_until_verified(self, *, action_id: int, handler, payload: dict, expected_state: dict[str, object], attempts: int):
        core_method = self.__class__.__mro__[1]._poll_until_verified
        original_asyncio = core_method.__globals__["asyncio"]
        core_method.__globals__["asyncio"] = asyncio
        try:
            return await super()._poll_until_verified(
                action_id=action_id,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=attempts,
            )
        finally:
            core_method.__globals__["asyncio"] = original_asyncio

    async def _store_verification_progress(self, action_id: int, attempts: int, result) -> None:
        core_method = self.__class__.__mro__[1]._store_verification_progress
        original_get_session = core_method.__globals__["get_session"]
        core_method.__globals__["get_session"] = get_session
        try:
            await super()._store_verification_progress(action_id, attempts, result)
        finally:
            core_method.__globals__["get_session"] = original_get_session

    async def _mark_verified(self, action, attempts: int, result) -> None:
        core_method = self.__class__.__mro__[1]._mark_verified
        original_get_session = core_method.__globals__["get_session"]
        original_asyncio = core_method.__globals__["asyncio"]
        core_method.__globals__["get_session"] = get_session
        core_method.__globals__["asyncio"] = asyncio
        try:
            await super()._mark_verified(action, attempts, result)
        finally:
            core_method.__globals__["get_session"] = original_get_session
            core_method.__globals__["asyncio"] = original_asyncio

    async def _mark_failed(self, action, attempts: int, result) -> None:
        core_method = self.__class__.__mro__[1]._mark_failed
        original_get_session = core_method.__globals__["get_session"]
        original_asyncio = core_method.__globals__["asyncio"]
        core_method.__globals__["get_session"] = get_session
        core_method.__globals__["asyncio"] = asyncio
        try:
            await super()._mark_failed(action, attempts, result)
        finally:
            core_method.__globals__["get_session"] = original_get_session
            core_method.__globals__["asyncio"] = original_asyncio

    async def expire_stale_actions(self) -> None:
        core_method = self.__class__.__mro__[1].expire_stale_actions
        original_get_session = core_method.__globals__["get_session"]
        core_method.__globals__["get_session"] = get_session
        try:
            await super().expire_stale_actions()
        finally:
            core_method.__globals__["get_session"] = original_get_session


__all__ = [
    "MAX_ACTIONS_PER_CYCLE",
    "VERIFY_POLL_INTERVAL_S",
    "VERIFY_REDISPATCH_ATTEMPTS",
    "VERIFY_TIMEOUT_S",
    "PlanExecutor",
    "asyncio",
    "get_session",
]

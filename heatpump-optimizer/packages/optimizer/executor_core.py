"""Plan executor: dispatches actions and verifies the device applied them.

Verification consumes Panasonic API read budget. The executor therefore keeps batches small
(`MAX_ACTIONS_PER_CYCLE`) and polls at a bounded cadence (`VERIFY_POLL_INTERVAL_S`) so a single
cycle stays within the wrapper's rate limiter while still marking mismatches as failures instead
of silently succeeding.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from enum import StrEnum
from uuid import uuid4

import structlog
from sqlalchemy import and_, or_, select, text, update

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.device_data_quality import (
    _as_utc,
    device_status_clock_issue,
    get_device_data_quality,
    observation_clock_issue,
)
from packages.core.learning_state import get_learning_state_details
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    ExecutorVerificationReadRecord,
    SpaceHeatingGateRecord,
)
from packages.core.plan_lifecycle import ACTIVE_PLAN_STATUS, DIAGNOSTIC_PLAN_STATUS
from packages.core.safety_attempts import (
    SafetyAttempt,
    SafetyAttemptDeadline,
    SafetyAttemptLost,
    safety_connection,
)
from packages.core.safety_reverts import (
    complete_linked_shower,
    dhw_embargoed,
    is_restorative_action,
    normalize_zone_id,
    restore_baseline_target,
    unresolved_revert_predicate,
    validate_action_pair,
    validate_restore_baseline,
    zone_embargoed,
    zone_matches_baseline,
)
from packages.core.services import AquareaWrapper
from packages.core.settings_service import get_space_heating_gate_config
from packages.core.space_heating_gate import SpaceHeatingGateState, resolve_effective_gate
from packages.optimizer.actions import (
    ActionType,
    VerificationObservation,
    VerifyResult,
    get_action_handler,
)
from packages.optimizer.executor_gate import is_room_heating_increase

logger = structlog.get_logger()

MAX_ACTIONS_PER_CYCLE = 3
EXECUTOR_VERIFY_READ_LIMIT = 5
INITIAL_VERIFY_CHECKPOINTS_S = (15, 30, 60)
INITIAL_LIVE_CHECKPOINTS_S = frozenset({15, 60})
REDISPATCH_VERIFY_CHECKPOINTS_S = (15, 30, 60)
REDISPATCH_LIVE_CHECKPOINTS_S = frozenset({60})
SAFETY_VERIFY_CHECKPOINTS_S = (15, 30, 60)
SAFETY_LIVE_CHECKPOINTS_S = frozenset({15, 60})
VERIFY_POLL_INTERVAL_S = 10
VERIFY_TIMEOUT_S = 60
VERIFY_REDISPATCH_ATTEMPTS = 1
SHUTDOWN_CANCEL_REASON = "shutdown_cancelled"
SAFETY_DISPATCH_MARGIN_S = 30
SAFETY_ACTION_STUCK_AFTER = dt.timedelta(minutes=2)
assert SAFETY_ACTION_STUCK_AFTER > dt.timedelta(
    seconds=max(SAFETY_VERIFY_CHECKPOINTS_S) + SAFETY_DISPATCH_MARGIN_S
)


class _VerificationEvidenceUnavailable(Exception):
    pass


class _VerificationLedgerUnavailable(Exception):
    pass


class _VerificationQuotaExhausted(Exception):
    pass


class _SafetyAuthorityLost(PermissionError):
    pass


class LearningModeState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


async def resolve_learning_mode_state() -> LearningModeState:
    """Resolve observe-only learning mode without treating lookup errors as inactive.

    A lookup failure leaves ordinary control unsafe, but a pending restorative action
    remains eligible for the executor's separate safety lane.
    """
    state, seasonal = await get_learning_state_details()
    if state.seasonal_active:
        logger.info("executor_seasonal_calibration_active", **seasonal)
    if state.active:
        return LearningModeState.ACTIVE
    if not state.reliable:
        return LearningModeState.UNKNOWN
    return LearningModeState.INACTIVE


async def is_learning_mode_active() -> bool:
    """Compatibility wrapper for callers that only need positive learning evidence."""
    return await resolve_learning_mode_state() is LearningModeState.ACTIVE


class PlanExecutor:
    """Executes pending plan actions respecting overrides and rate limits."""

    def __init__(
        self,
        wrapper: AquareaWrapper,
        *,
        session_factory=get_session,
        sleep=asyncio.sleep,
        learning_check=resolve_learning_mode_state,
        device_quality_check=get_device_data_quality,
        connection_factory=safety_connection,
    ):
        self._wrapper = wrapper
        self._session_factory = session_factory
        self._sleep = sleep
        self._learning_check = learning_check
        self._device_quality_check = device_quality_check
        self._connection_factory = connection_factory
        self._safety_attempts: dict = {}
        self._last_device_quality: dict = {}

    async def execute_due_actions(self) -> None:
        """Execute only existing linked restores outside positive observation."""
        now = dt.datetime.now(dt.timezone.utc)

        try:
            learning_state = await self._learning_check()
        except Exception as exc:  # noqa: BLE001 - injected checks must not admit ordinary work
            logger.error(
                "learning_mode_state_unknown",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            learning_state = LearningModeState.UNKNOWN
        if learning_state is True:
            learning_state = LearningModeState.ACTIVE
        elif learning_state is False:
            learning_state = LearningModeState.INACTIVE

        # Positive observe-only learning leaves every action untouched, including
        # restores. Unknown state permits only the existing single safety lane.
        if learning_state is LearningModeState.ACTIVE:
            logger.info(
                "executor_learning_mode_active",
                reason="observe-only training mode",
                skipping=0,
            )
            return

        safety_action = await self._claim_due_safety_action(now)
        if safety_action is not None:
            await self._execute_safety_action(safety_action)
            return

        logger.info(
            "executor_ordinary_writes_prohibited",
            learning_state=learning_state,
            reason="observe_only_build",
        )

    async def _claim_due_safety_action(self, now: dt.datetime) -> PlanActionRecord | None:
        """Claim one oldest linked restore without depending on its parent plan."""
        async with self._session_factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(PlanActionRecord)
                        .where(
                            unresolved_revert_predicate(),
                            PlanActionRecord.scheduled_ts <= now,
                            (PlanActionRecord.safety_next_retry_at.is_(None))
                            | (PlanActionRecord.safety_next_retry_at <= now),
                            or_(
                                PlanActionRecord.status == "pending",
                                PlanActionRecord.safety_claimed_at.is_(None),
                                PlanActionRecord.safety_claimed_at
                                <= now - SAFETY_ACTION_STUCK_AFTER,
                            ),
                        )
                        .order_by(PlanActionRecord.scheduled_ts, PlanActionRecord.id)
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
        for candidate in candidates:
            if not candidate.device_id:
                logger.warning(
                    "safety_restore_admission_deferred",
                    action_id=candidate.id,
                    reason="unknown_device_identity",
                )
                continue
            attempt = None
            try:
                attempt = await SafetyAttempt.acquire(
                    candidate.device_id, self._connection_factory, now=now
                )
                if attempt is None:
                    continue
                action = await attempt.run(self._claim_safety_candidate(candidate, attempt, now))
                if action is not None:
                    self._safety_attempts[action.safety_claim_token] = attempt
                    return action
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "safety_restore_admission_deferred",
                    action_id=candidate.id,
                    reason=type(exc).__name__,
                )
            finally:
                if attempt is not None and attempt.token not in self._safety_attempts:
                    await attempt.release()
        return None

    @staticmethod
    def _confirmed_drain(action) -> bool:
        try:
            metadata = json.loads(action.result_json or "{}").get("safety_attempt", {})
        except (AttributeError, TypeError, ValueError):
            return False
        token = getattr(action, "safety_claim_token", None)
        return (
            token is not None
            and isinstance(metadata, dict)
            and metadata.get("schema") == "safety_attempt_v1"
            and metadata.get("claim_token") == str(token)
            and metadata.get("action_id") == action.id
            and metadata.get("source_id") == action.reverts_action_id
            and metadata.get("device_id") == action.device_id
            and metadata.get("drain") == "confirmed"
            and metadata.get("owned_tasks_done") is True
            and metadata.get("fence_lost") is False
        )

    async def _claim_safety_candidate(self, candidate, attempt, now):
        async with self._session_factory() as session:
            stored = (
                await session.execute(
                    select(PlanActionRecord)
                    .where(
                        PlanActionRecord.id == candidate.id,
                        unresolved_revert_predicate(),
                        PlanActionRecord.device_id == attempt.device_id,
                        PlanActionRecord.scheduled_ts <= now,
                        or_(
                            PlanActionRecord.safety_next_retry_at.is_(None),
                            PlanActionRecord.safety_next_retry_at <= now,
                        ),
                    )
                    .with_for_update(nowait=True)
                )
            ).scalar_one_or_none()
            if stored is None:
                return None
            source = await session.get(PlanActionRecord, stored.reverts_action_id)
            if source is None or source.status not in ("executed", "executed_unverified", "failed"):
                raise _SafetyAuthorityLost("Linked source has not settled")
            validate_action_pair(source, stored)
            validate_restore_baseline(source, stored)
            actors = (
                (
                    await session.execute(
                        select(PlanActionRecord)
                        .where(
                            or_(
                                PlanActionRecord.device_id == attempt.device_id,
                                PlanActionRecord.device_id.is_(None),
                            ),
                            or_(
                                PlanActionRecord.status.in_(["executing", "dispatched"]),
                                and_(
                                    PlanActionRecord.status == "pending",
                                    PlanActionRecord.safety_claim_token.is_not(None),
                                ),
                            ),
                        )
                        .order_by(PlanActionRecord.id)
                        .limit(501)
                        .with_for_update(nowait=True)
                    )
                )
                .scalars()
                .all()
            )
            if len(actors) > 500 or any(not self._confirmed_drain(actor) for actor in actors):
                logger.warning(
                    "safety_restore_admission_deferred",
                    action_id=stored.id,
                    reason="unsettled_device_owner",
                )
                return None
            if stored.status != "pending" and not self._confirmed_drain(stored):
                return None
            old_token, old_status = stored.safety_claim_token, stored.status
            token = uuid4()
            attempt.bind(
                stored.id,
                source.id,
                token,
                payload_json=stored.payload_json,
                source_payload_json=source.payload_json,
            )
            metadata = attempt.metadata()
            metadata["possible_dispatch"] = old_status != "pending"
            result_json = self._safety_result(stored, safety_attempt=metadata)
            result = await session.execute(
                update(PlanActionRecord)
                .where(
                    PlanActionRecord.id == stored.id,
                    PlanActionRecord.safety_claim_token == old_token
                    if old_token is not None
                    else PlanActionRecord.safety_claim_token.is_(None),
                    PlanActionRecord.status == old_status,
                    PlanActionRecord.reverts_action_id == source.id,
                    PlanActionRecord.device_id == attempt.device_id,
                    PlanActionRecord.payload_json == stored.payload_json,
                )
                .values(
                    status="executing",
                    safety_claim_token=token,
                    safety_claimed_at=now,
                    result_json=result_json,
                )
            )
            if result.rowcount != 1:
                raise _SafetyAuthorityLost("Restore admission CAS lost authority")
            stored.status, stored.safety_claim_token = "executing", token
            stored.safety_claimed_at, stored.result_json = now, result_json
            return stored

    async def _execute_safety_action(self, action: PlanActionRecord) -> None:
        """Dispatch one safety restore with priority capacity and no redispatch."""
        if not is_restorative_action(action):
            raise PermissionError("Only existing linked restores may enter the safety lane")
        attempt = self._attempt_for(action)
        try:
            attempt.authority_check = lambda: self._assert_safety_authority(action)
            attempt.start_monitor()
            outcome = await attempt.run(self._preflight_safety_dispatch(action))
            if outcome.get("defer"):
                await attempt.drain()
                await self._requeue_safety_action(action, outcome["defer"])
            elif outcome.get("already_safe"):
                await attempt.drain()
                await self._mark_verified(action, outcome.get("reads", 0), outcome["result"])
            else:
                expected_state = outcome["expected_state"]
                settled_at = dt.datetime.now(dt.timezone.utc)
                attempt.dispatch_settled_monotonic = time.monotonic()
                attempt.dispatch_settled_at = settled_at
                await attempt.run(
                    self._update_safety_action(
                        action,
                        {
                            "status": "dispatched",
                            "expected_state_json": json.dumps(expected_state),
                            "result_json": self._safety_result(
                                action,
                                safety_attempt={
                                    **attempt.metadata(outcome=outcome["outcome"]),
                                    "dispatch_outcome": outcome["outcome"],
                                    "dispatch_settled_at": settled_at.isoformat(),
                                    "dispatch_settled_monotonic": attempt.dispatch_settled_monotonic,
                                    "possible_dispatch": True,
                                },
                            ),
                        },
                    )
                )
                payload = json.loads(action.payload_json)
                result, reads = await attempt.run(
                    self._poll_until_verified(
                        action=action,
                        handler=get_action_handler(ActionType(action.action_type)),
                        payload=payload,
                        expected_state=expected_state,
                        attempts=outcome.get("reads", 0),
                        lane="safety",
                        phase="safety",
                        checkpoints=SAFETY_VERIFY_CHECKPOINTS_S,
                        live_checkpoints=SAFETY_LIVE_CHECKPOINTS_S - attempt.consumed_checkpoints,
                        evidence_after=settled_at,
                    ),
                    verification=True,
                )
                await attempt.drain()
                if result.ok:
                    await self._mark_verified(action, reads, result)
                else:
                    await self._requeue_safety_action(action, result.reason)
        except asyncio.CancelledError:
            if await attempt.drain() and not attempt._lost:
                try:
                    await self._requeue_safety_action(action, SHUTDOWN_CANCEL_REASON)
                except (SafetyAttemptLost, SafetyAttemptDeadline, _SafetyAuthorityLost):
                    pass
            raise
        except Exception as exc:
            drained = await attempt.drain()
            if drained and not attempt._lost:
                try:
                    await self._requeue_safety_action(action, type(exc).__name__)
                except (SafetyAttemptLost, SafetyAttemptDeadline, _SafetyAuthorityLost):
                    pass
            logger.warning(
                "safety_restore_deferred",
                action_id=action.id,
                reason=type(exc).__name__,
                drainage_proven=drained,
            )
        finally:
            await attempt.release()
            self._safety_attempts.pop(attempt.token, None)

    def _attempt_for(self, action) -> SafetyAttempt:
        attempt = self._safety_attempts.get(getattr(action, "safety_claim_token", None))
        if attempt is None:
            raise _SafetyAuthorityLost("Restore has no live, device-fenced attempt owner")
        return attempt

    async def _preflight_safety_dispatch(self, action):
        attempt = self._attempt_for(action)
        try:
            learning_state = await self._learning_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            learning_state = LearningModeState.UNKNOWN
        if learning_state is LearningModeState.ACTIVE or learning_state is True:
            return {"defer": "learning_mode_active"}
        action, source = await self._load_claimed_restore(action)
        attempt.source_settled_at = self._source_settled_bound(
            source, action.device_id, as_of=dt.datetime.now(dt.timezone.utc)
        )
        payload = json.loads(action.payload_json)
        action_type = ActionType(action.action_type)
        precondition = await self._dispatch_precondition(action, action_type, payload)
        if precondition is not None:
            return {"defer": str(precondition.get("reason", "precondition"))}
        expected = (
            {"force_dhw": "OFF"}
            if action_type is ActionType.FORCE_DHW_OFF
            else {
                "zone_id": normalize_zone_id(payload.get("zone_id")),
                "temperature": validate_restore_baseline(source, action),
            }
        )
        if await self._safety_already_safe(action, action_type, payload):
            return {
                "already_safe": True,
                "result": VerifyResult(ok=True, reason="already_safe"),
                "reads": 0,
            }
        handler = get_action_handler(action_type)
        await self._assert_safety_authority(action)
        admission_delay = max(
            0.0,
            attempt.claimed_monotonic + SAFETY_VERIFY_CHECKPOINTS_S[0] - time.monotonic(),
        )
        if attempt.remaining() <= admission_delay:
            raise SafetyAttemptDeadline("Admission checkpoint cannot extend restore budget")
        await self._sleep(admission_delay)
        proof = await self._read_safety_observation(action, SAFETY_VERIFY_CHECKPOINTS_S[0])
        result = handler.verify(proof, payload, expected)
        if result.ok:
            return {"already_safe": True, "result": result, "reads": 1}
        await self._update_safety_action(
            action,
            {
                "result_json": self._safety_result(
                    action,
                    safety_attempt={
                        **attempt.metadata(outcome="dispatch_admitted"),
                        "possible_dispatch": True,
                        "dispatch_started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
            },
        )
        await self._assert_safety_authority(action)
        try:
            attempt.dispatch_started_monotonic = time.monotonic()
            response = await self._wrapper._dispatch_linked_restore(action, source, attempt=attempt)
        except asyncio.CancelledError:
            raise
        except (SafetyAttemptLost, SafetyAttemptDeadline, _SafetyAuthorityLost):
            raise
        except Exception as exc:
            logger.warning(
                "safety_dispatch_ambiguous", action_id=action.id, error_type=type(exc).__name__
            )
            return {"expected_state": expected, "outcome": "ambiguous", "reads": 1}
        if response.get("skip"):
            return {"defer": str(response.get("reason", "live_precondition"))}
        return {"expected_state": response or expected, "outcome": "dispatched", "reads": 1}

    async def _read_safety_observation(self, action, checkpoint):
        attempt = self._attempt_for(action)
        await self._assert_safety_authority(action)
        if checkpoint in attempt.consumed_checkpoints:
            raise _VerificationEvidenceUnavailable("Checkpoint already consumed")
        if not await self._reserve_verification_read(action, "safety", "safety", checkpoint):
            raise _VerificationQuotaExhausted()
        await self._assert_safety_authority(action)
        proof = await self._wrapper._refresh_restore_status(attempt)
        as_of = dt.datetime.now(dt.timezone.utc)
        monotonic_now = time.monotonic()
        max_age = min(
            60.0,
            float(
                self._last_device_quality.get(action.device_id, {}).get("threshold_seconds", 900)
            ),
        )
        read_started_at = _as_utc(proof.read_started_at)
        read_finished_at = _as_utc(proof.read_finished_at)
        evidence_after = attempt.dispatch_settled_at or attempt.source_settled_at
        clock_issue = observation_clock_issue(
            as_of=as_of,
            captured_at=proof.read_finished_at,
            observed_at=proof.observed_at,
            retrieved_at=proof.retrieved_at,
            max_age_seconds=max_age,
            evidence_after=evidence_after,
        )
        if (
            proof.device_id != action.device_id
            or not proof.live
            or read_started_at is None
            or read_finished_at is None
            or read_started_at < _as_utc(action.safety_claimed_at)
            or read_started_at > read_finished_at
            or read_finished_at > as_of
            or proof.started_monotonic < attempt.claimed_monotonic
            or (
                attempt.verify_deadline is not None
                and proof.started_monotonic < attempt.dispatch_settled_monotonic
            )
            or proof.finished_monotonic < proof.started_monotonic
            or proof.finished_monotonic > monotonic_now
            or monotonic_now - proof.finished_monotonic > max_age
            or clock_issue
        ):
            raise _VerificationEvidenceUnavailable("Post-claim live provenance is not admissible")
        observation = self._normalize_live_observation(
            proof.device,
            as_of=as_of,
            captured_at=read_finished_at,
            max_age_seconds=max_age,
            evidence_after=evidence_after,
        )
        await self._assert_safety_authority(action)
        await self._update_safety_action(
            action,
            {
                "result_json": self._safety_result(
                    action,
                    safety_attempt={
                        **attempt.metadata(),
                        "live_read": {
                            "checkpoint": checkpoint,
                            "device_id": proof.device_id,
                            "live": proof.live,
                            "read_started_at": proof.read_started_at.isoformat(),
                            "read_finished_at": proof.read_finished_at.isoformat(),
                            "started_monotonic": proof.started_monotonic,
                            "finished_monotonic": proof.finished_monotonic,
                            "observed_at": proof.observed_at.isoformat()
                            if proof.observed_at is not None
                            else None,
                            "retrieved_at": proof.retrieved_at.isoformat()
                            if proof.retrieved_at is not None
                            else None,
                        },
                    },
                )
            },
        )
        return observation

    async def _load_claimed_restore(
        self, action: PlanActionRecord
    ) -> tuple[PlanActionRecord, PlanActionRecord]:
        """Reload the persisted claim and linked source before admitting a restore."""
        await self._assert_safety_authority(action)
        if action.reverts_action_id is None or action.safety_claimed_at is None:
            raise PermissionError("Restore is not linked and claimed")
        async with self._session_factory() as session:
            stored = (
                await session.execute(
                    select(PlanActionRecord).where(
                        self._safety_claim_predicate(action),
                        PlanActionRecord.scheduled_ts <= dt.datetime.now(dt.timezone.utc),
                    )
                )
            ).scalar_one_or_none()
            if stored is None:
                raise PermissionError("Restore claim is no longer current")
            source = await session.get(PlanActionRecord, stored.reverts_action_id)
        if source is None:
            raise PermissionError("Linked restore source is missing")
        validate_action_pair(source, stored)
        validate_restore_baseline(source, stored)
        if source.payload_json != self._attempt_for(action).source_payload_json:
            raise _SafetyAuthorityLost("Linked source changed after claim")
        for field in (
            "result_json",
            "expected_state_json",
            "verify_attempts",
            "safety_attempt_count",
            "safety_next_retry_at",
        ):
            setattr(action, field, getattr(stored, field, None))
        return action, source

    @staticmethod
    def _source_settled_bound(source, device_id, *, as_of):
        try:
            provenance = json.loads(source.result_json or "{}").get("dispatch_provenance", {})
            source_bound = _as_utc(provenance.get("settled_at"))
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not isinstance(provenance, dict)
            or provenance.get("schema") != "source_dispatch_v1"
            or provenance.get("drain") != "confirmed"
            or provenance.get("device_id") != device_id
            or source_bound is None
            or source_bound > as_of
        ):
            return None
        return source_bound

    async def _safety_already_safe(
        self, action: PlanActionRecord, action_type: ActionType, payload: dict
    ) -> bool:
        """Use fresh persisted evidence to finish a linked revert without a write."""

        as_of = dt.datetime.now(dt.timezone.utc)
        quality = self._last_device_quality.get(action.device_id, {})
        cutoff = as_of - dt.timedelta(seconds=quality.get("threshold_seconds", 900))
        async with self._session_factory() as session:
            status = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == action.device_id,
                        DeviceStatusRecord.ts >= cutoff,
                        DeviceStatusRecord.ts <= as_of,
                    )
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            source_action_id = getattr(action, "reverts_action_id", None)
            source = (
                await session.get(PlanActionRecord, source_action_id)
                if source_action_id is not None
                else None
            )
        if status is None or not quality.get("ready") or not cutoff <= _as_utc(status.ts) <= as_of:
            return False
        source_bound = self._source_settled_bound(source, action.device_id, as_of=as_of)
        if source_bound is None or device_status_clock_issue(
            status,
            as_of=as_of,
            max_age_seconds=quality.get("threshold_seconds", 900),
            evidence_after=source_bound,
            require_remote_clock=True,
        ):
            return False
        if action_type is ActionType.FORCE_DHW_OFF:
            return status.force_dhw == 0
        if action_type is not ActionType.ZONE_TEMP_RESTORE:
            return False
        baseline = restore_baseline_target(action)
        source_payload = json.loads(source.payload_json) if source and source.payload_json else {}
        source_baseline = source_payload.get("baseline_temperature")
        if source_baseline is not None and not zone_matches_baseline(source_baseline, baseline):
            logger.error(
                "safety_restore_baseline_mismatch",
                action_id=action.id,
                source_action_id=source_action_id,
                source_baseline=source_baseline,
                restore_baseline=baseline,
            )
            raise ValueError("safety_restore_baseline_mismatch")
        zone_id = normalize_zone_id(payload.get("zone_id"))
        current_target = getattr(status, f"zone{zone_id}_target_temp", None)
        return zone_matches_baseline(baseline, current_target)

    async def _assert_safety_authority(self, action: PlanActionRecord, *, cleanup=False) -> None:
        attempt = self._attempt_for(action)
        attempt.assert_live(action, cleanup=cleanup)
        if cleanup and not attempt._drained:
            raise _SafetyAuthorityLost("Restore actors have not positively drained")
        await attempt.check_fence(cleanup=cleanup)

        async def check_row():
            async with self._session_factory() as session:
                stored = (
                    await session.execute(
                        select(PlanActionRecord).where(self._safety_claim_predicate(action))
                    )
                ).scalar_one_or_none()
                if stored is None:
                    raise _SafetyAuthorityLost(
                        "Restore UUID/status/link/device is no longer current"
                    )

        await attempt._bounded(check_row(), time.monotonic() + attempt.remaining(cleanup=cleanup))

    @staticmethod
    def _safety_claim_predicate(action: PlanActionRecord):
        token = getattr(action, "safety_claim_token", None)
        if token is None or not action.device_id or not action.reverts_action_id:
            raise _SafetyAuthorityLost("Restore has no scoped claim token")
        return and_(
            PlanActionRecord.id == action.id,
            PlanActionRecord.safety_claim_token == token,
            PlanActionRecord.status == action.status,
            PlanActionRecord.reverts_action_id == action.reverts_action_id,
            PlanActionRecord.device_id == action.device_id,
            PlanActionRecord.payload_json == action.payload_json,
        )

    @staticmethod
    def _safety_result(action: PlanActionRecord, **changes) -> str:
        raw = getattr(action, "result_json", None)
        try:
            existing = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            existing = {"previous_result": raw}
        if not isinstance(existing, dict):
            existing = {"previous_result": existing}
        if existing.get("reason") and changes.get("reason") != existing["reason"]:
            existing.setdefault("original_reason", existing["reason"])
        metadata = changes.get("safety_attempt")
        previous = existing.get("safety_attempt")
        if isinstance(metadata, dict) and isinstance(previous, dict):
            if previous.get("claim_token") == metadata.get("claim_token"):
                changes["safety_attempt"] = {**previous, **metadata}
            else:
                existing["previous_safety_attempt"] = previous
        existing.update(changes)
        return json.dumps(existing)

    async def _update_safety_action(
        self, action: PlanActionRecord, values: dict, *, audit=None, cleanup=False
    ):
        await self._assert_safety_authority(action, cleanup=cleanup)
        attempt = self._attempt_for(action)

        async def mutate():
            async with self._session_factory() as session:
                stored = (
                    await session.execute(
                        select(PlanActionRecord)
                        .where(self._safety_claim_predicate(action))
                        .with_for_update(nowait=True)
                    )
                ).scalar_one_or_none()
                if stored is None:
                    raise _SafetyAuthorityLost("Restore progress has no current locked row")
                if "result_json" in values:
                    proposed = json.loads(values["result_json"])
                    if values.get("status") != "pending":
                        proposed.pop("reason", None)
                    proposed.pop("original_reason", None)
                    values["result_json"] = self._safety_result(stored, **proposed)
                result = await session.execute(
                    update(PlanActionRecord)
                    .where(self._safety_claim_predicate(action))
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise _SafetyAuthorityLost("Restore claim CAS affected no current action")
                if values.get("status") == "executed" and audit is not None:
                    await complete_linked_shower(session, stored, verified_at=values["executed_at"])
                await attempt.check_fence(cleanup=cleanup)
                if audit is not None:
                    session.add(audit)

        await attempt._bounded(mutate(), time.monotonic() + attempt.remaining(cleanup=cleanup))
        for name, value in values.items():
            setattr(action, name, value)

    async def _requeue_safety_action(self, action: PlanActionRecord, reason: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        attempts = int(action.safety_attempt_count or 0) + 1
        delay = dt.timedelta(minutes=1 if attempts <= 3 else 15)
        await self._update_safety_action(
            action,
            {
                "status": "pending",
                "safety_attempt_count": attempts,
                "safety_next_retry_at": now + delay,
                "result_json": self._safety_result(
                    action,
                    reason=reason,
                    safety_attempt=self._attempt_for(action).metadata(outcome="deferred"),
                ),
            },
            cleanup=True,
        )
        logger.warning(
            "safety_revert_requeued", action_id=action.id, attempts=attempts, reason=reason
        )

    async def _reconcile_claimed_batch_cancelled(
        self, claimed_actions: list[PlanActionRecord]
    ) -> None:
        """Cancel any still-executing actions from a claimed batch after interruption."""
        if not claimed_actions:
            return
        claimed_actions = [
            action for action in claimed_actions if not is_restorative_action(action)
        ]
        if not claimed_actions:
            return

        claimed_ids = [action.id for action in claimed_actions]
        action_by_id = {action.id: action for action in claimed_actions}
        now = dt.datetime.now(dt.timezone.utc)

        async with self._session_factory() as session:
            executing_result = await session.execute(
                select(PlanActionRecord.id).where(
                    and_(
                        PlanActionRecord.id.in_(claimed_ids),
                        PlanActionRecord.status == "executing",
                        PlanActionRecord.reverts_action_id.is_(None),
                    )
                )
            )
            executing_ids = list(executing_result.scalars().all())
            if not executing_ids:
                return

            await session.execute(
                update(PlanActionRecord)
                .where(
                    and_(
                        PlanActionRecord.id.in_(executing_ids),
                        PlanActionRecord.status == "executing",
                        PlanActionRecord.reverts_action_id.is_(None),
                    )
                )
                .values(
                    status="cancelled",
                    executed_at=now,
                    result_json=json.dumps(
                        {
                            "reason": SHUTDOWN_CANCEL_REASON,
                            "detail": "Executor shutdown interrupted action verification",
                        }
                    ),
                )
            )

            for action_id in executing_ids:
                action = action_by_id.get(action_id)
                if action is None:
                    continue
                session.add(
                    AuditLogRecord(
                        actor="optimizer",
                        action=action.action_type,
                        payload_json=action.payload_json,
                        result="cancelled",
                    )
                )

    async def _execute_action(self, action: PlanActionRecord) -> None:
        """Refuse ordinary dispatch, including direct and legacy entry points."""
        raise PermissionError("Ordinary writes are prohibited in this observe-only build")

    async def _skip_action(self, action: PlanActionRecord, result: dict[str, object]) -> None:
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Linked restores require fenced safety transitions")
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="skipped",
                    executed_at=dt.datetime.now(dt.timezone.utc),
                    result_json=json.dumps(result),
                )
            )

    async def _defer_action(self, action: PlanActionRecord, result: dict[str, object]) -> None:
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Linked restores require fenced safety transitions")
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .where(PlanActionRecord.status == "executing")
                .values(status="pending", result_json=json.dumps(result))
            )
        logger.warning(
            "action_deferred_device_data_quality",
            action_id=action.id,
            action_type=action.action_type,
            reason=result["reason"],
        )

    async def _dispatch_precondition(
        self, action, action_type, payload
    ) -> dict[str, object] | None:
        as_of = dt.datetime.now(dt.timezone.utc)
        try:
            selected_device_id = await self._wrapper.get_selected_device_id()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"reason": "action_device_unresolvable", "identity_error": type(exc).__name__}
        if action.device_id and action.device_id != selected_device_id:
            return {"reason": "action_device_unresolvable", "device_id": action.device_id}
        try:
            quality = await self._device_quality_check(now=as_of)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - readiness failures must block control
            logger.error(
                "action_device_data_quality_check_failed",
                action_id=action.id,
                action_type=action_type.value,
                reason="quality_check_failed",
                error_type=type(exc).__name__,
            )
            return {"reason": "quality_check_failed", "device_id": selected_device_id}
        self._last_device_quality[selected_device_id] = quality
        if not quality["ready"]:
            if is_restorative_action(action) and set(quality["reasons"]) <= {
                "device_status_missing",
                "device_status_stale",
            }:
                return None
            return {"reason": quality["reasons"][0], "device_id": selected_device_id}
        cutoff = as_of - dt.timedelta(seconds=quality["threshold_seconds"])
        async with self._session_factory() as session:
            status = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == selected_device_id,
                        DeviceStatusRecord.ts >= cutoff,
                        DeviceStatusRecord.ts <= as_of,
                    )
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if status is not None and (
                clock_issue := device_status_clock_issue(
                    status, as_of=as_of, max_age_seconds=quality["threshold_seconds"]
                )
            ):
                return {"reason": clock_issue, "device_id": selected_device_id}
            if status is None:
                if is_restorative_action(action):
                    return None
                return {"reason": "device_status_stale", "device_id": selected_device_id}
            if action.device_id is None:
                fresh_ids = (
                    (
                        await session.execute(
                            select(DeviceStatusRecord.device_id)
                            .where(DeviceStatusRecord.ts >= cutoff, DeviceStatusRecord.ts <= as_of)
                            .distinct()
                        )
                    )
                    .scalars()
                    .all()
                )
                if set(fresh_ids) != {selected_device_id}:
                    return {"reason": "action_device_unresolvable", "device_id": selected_device_id}
            heating_increase = is_room_heating_increase(action_type, payload, status)
            if not is_restorative_action(action) and (
                action_type is ActionType.FORCE_DHW_ON or heating_increase
            ):
                unresolved_actions = (
                    (
                        await session.execute(
                            select(PlanActionRecord).where(unresolved_revert_predicate())
                        )
                    )
                    .scalars()
                    .all()
                )
                embargo_action = {
                    "action_type": action_type,
                    "device_id": selected_device_id,
                    "payload": payload,
                }
                if (
                    action_type is ActionType.FORCE_DHW_ON
                    and dhw_embargoed(unresolved_actions, selected_device_id)
                ) or zone_embargoed(unresolved_actions, embargo_action, status):
                    logger.warning(
                        "action_blocked_by_unresolved_revert",
                        action_id=action.id,
                        action_type=action.action_type,
                        device_id=selected_device_id,
                    )
                    return {
                        "reason": "blocked_by_unresolved_revert",
                        "device_id": selected_device_id,
                    }
            if not heating_increase:
                return None
            gate_row = (
                await session.execute(
                    select(SpaceHeatingGateRecord).where(
                        SpaceHeatingGateRecord.device_id == selected_device_id
                    )
                )
            ).scalar_one_or_none()
        evidence = resolve_effective_gate(gate_row, await get_space_heating_gate_config())
        if evidence.state is not SpaceHeatingGateState.ALLOWED:
            return {
                "reason": "space_heating_gate_blocked"
                if evidence.state is SpaceHeatingGateState.BLOCKED
                else "space_heating_gate_unknown",
                "device_id": selected_device_id,
                "profile_id": evidence.profile_id,
                "gate_reason": evidence.reason_code,
            }
        return None

    async def _mark_cancelled(self, action: PlanActionRecord) -> None:
        """Reconcile in-flight cancellation so dispatched actions do not get stranded."""
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Linked restores must drain and requeue, never cancel")
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="cancelled",
        )
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="cancelled",
                    executed_at=now,
                    result_json=json.dumps(
                        {
                            "reason": SHUTDOWN_CANCEL_REASON,
                            "detail": "Executor shutdown interrupted action verification",
                        }
                    ),
                )
            )
            session.add(audit_record)

    async def _verify_with_retry(
        self,
        action: PlanActionRecord,
        payload: dict,
        expected_state: dict[str, object],
        dispatched_at: dt.datetime | None = None,
    ) -> None:
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Linked restores require the owned safety verification lane")
        action_type = ActionType(action.action_type)
        handler = get_action_handler(action_type)
        dispatched_at = dispatched_at or dt.datetime.now(dt.timezone.utc)
        if not handler.verification_supported:
            await self._mark_executed_unverified(action, "verification_not_supported")
            return
        try:
            initial, attempts = await self._poll_until_verified(
                action=action,
                handler=handler,
                payload=payload,
                expected_state=expected_state,
                attempts=0,
                lane="ordinary",
                phase="initial",
                checkpoints=INITIAL_VERIFY_CHECKPOINTS_S,
                live_checkpoints=INITIAL_LIVE_CHECKPOINTS_S,
                evidence_after=dispatched_at,
            )
        except asyncio.CancelledError:
            raise
        except _VerificationQuotaExhausted:
            await self._mark_executed_unverified(action, "verification_quota_exhausted")
            return
        except _VerificationLedgerUnavailable:
            await self._mark_executed_unverified(action, "verification_ledger_unavailable")
            return
        except _VerificationEvidenceUnavailable:
            await self._mark_executed_unverified(action, "verification_evidence_unavailable")
            return
        if initial.ok:
            await self._mark_verified(action, attempts, initial)
            return
        await self._mark_executed_unverified(action, "ordinary_write_prohibited")

    async def _poll_until_verified(
        self,
        *,
        action: PlanActionRecord,
        handler,
        payload: dict,
        expected_state: dict[str, object],
        attempts: int,
        lane: str,
        phase: str,
        checkpoints: tuple[int, ...],
        live_checkpoints: frozenset[int],
        evidence_after: dt.datetime,
    ) -> tuple[VerifyResult, int]:
        if is_restorative_action(action) and lane != "safety":
            raise _SafetyAuthorityLost("Linked restores cannot use ordinary verification")
        result = VerifyResult(ok=False, expected_value=expected_state, reason="not_verified")
        previous_checkpoint = 0
        for checkpoint in checkpoints:
            if lane == "safety":
                await self._assert_safety_authority(action)
                attempt = self._attempt_for(action)
                if attempt.dispatch_started_monotonic is None:
                    raise _SafetyAuthorityLost("Safety checkpoint has no owned SDK-call origin")
                delay = max(0.0, attempt.dispatch_started_monotonic + checkpoint - time.monotonic())
                if attempt.remaining() <= delay:
                    raise SafetyAttemptDeadline("Checkpoint cannot extend restore budget")
            else:
                delay = checkpoint - previous_checkpoint
            await self._sleep(delay)
            previous_checkpoint = checkpoint
            if lane == "safety":
                await self._assert_safety_authority(action)
            try:
                persisted = await self._load_persisted_observation(action.device_id, evidence_after)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "verification_ledger_unavailable",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="persisted",
                    error_type=type(exc).__name__,
                )
                raise _VerificationLedgerUnavailable() from exc
            if persisted is not None:
                result = handler.verify(persisted, payload, expected_state)
                await self._store_verification_progress(action, attempts, result)
                logger.info(
                    "verification_evidence",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="persisted",
                    admission_result="not_required",
                    reason=result.reason,
                )
                if result.ok:
                    return result, attempts
            if checkpoint not in live_checkpoints:
                continue
            if lane == "safety":
                observation = await self._read_safety_observation(action, checkpoint)
                result = handler.verify(observation, payload, expected_state)
                attempts += 1
                await self._store_verification_progress(action, attempts, result)
                if result.ok:
                    return result, attempts
                continue
            try:
                admitted = await self._reserve_verification_read(action, lane, phase, checkpoint)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "verification_ledger_unavailable",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="live",
                    error_type=type(exc).__name__,
                )
                raise _VerificationLedgerUnavailable() from exc
            if not admitted:
                logger.info(
                    "verification_reservation",
                    lane=lane,
                    action_id=action.id,
                    action_type=action.action_type,
                    checkpoint=checkpoint,
                    evidence_source="live",
                    admission_result="denied",
                    reason="verification_quota_exhausted",
                )
                raise _VerificationQuotaExhausted()
            try:
                from packages.core.services import ReadQuotaContext

                device = await self._wrapper.refresh_device(
                    quota_context=ReadQuotaContext.EXECUTOR_VERIFICATION
                )
                result = handler.verify(
                    self._normalize_live_observation(device), payload, expected_state
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _VerificationEvidenceUnavailable() from exc
            attempts += 1
            await self._store_verification_progress(action, attempts, result)
            logger.info(
                "verification_evidence",
                lane=lane,
                action_id=action.id,
                action_type=action.action_type,
                checkpoint=checkpoint,
                evidence_source="live",
                admission_result="admitted",
                reason=result.reason,
            )
            if result.ok:
                return result, attempts
        return result, attempts

    async def _reserve_verification_read(
        self, action, lane: str, phase: str, checkpoint: int
    ) -> bool:
        if is_restorative_action(action) and lane != "safety":
            raise _SafetyAuthorityLost("Linked restores cannot reserve ordinary reads")
        if lane == "safety":
            await self._assert_safety_authority(action)
        async with self._session_factory() as session:
            if lane == "safety":
                stored = (
                    await session.execute(
                        select(PlanActionRecord)
                        .where(self._safety_claim_predicate(action))
                        .with_for_update(nowait=True)
                    )
                ).scalar_one_or_none()
                if stored is None:
                    raise _SafetyAuthorityLost("Verification reservation lost the restore claim")
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(text("SELECT pg_advisory_xact_lock(1163280966, 1)"))
            if lane == "safety":
                self._attempt_for(action).assert_live(action)
            db_now = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
            await session.execute(
                text("DELETE FROM executor_verification_reads WHERE reserved_at < :cutoff"),
                {"cutoff": db_now - dt.timedelta(days=7)},
            )
            total = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM executor_verification_reads WHERE reserved_at > :cutoff"
                    ),
                    {"cutoff": db_now - dt.timedelta(hours=1)},
                )
            ).scalar_one()
            ordinary = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM executor_verification_reads WHERE lane = 'ordinary' AND reserved_at > :cutoff"
                    ),
                    {"cutoff": db_now - dt.timedelta(hours=1)},
                )
            ).scalar_one()
            reserve = settings.executor_safety_read_reserve
            admitted = total < EXECUTOR_VERIFY_READ_LIMIT and (
                lane != "ordinary" or ordinary < EXECUTOR_VERIFY_READ_LIMIT - reserve
            )
            if admitted:
                if lane == "safety":
                    attempt = self._attempt_for(action)
                    attempt.assert_live(action)
                    metadata = attempt.metadata()
                    metadata["checkpoints"] = sorted(attempt.consumed_checkpoints | {checkpoint})
                    progress = self._safety_result(action, safety_attempt=metadata)
                    changed = await session.execute(
                        update(PlanActionRecord)
                        .where(self._safety_claim_predicate(action))
                        .values(result_json=progress)
                    )
                    if changed.rowcount != 1:
                        raise _SafetyAuthorityLost("Verification reservation CAS lost authority")
                session.add(
                    ExecutorVerificationReadRecord(
                        reserved_at=db_now,
                        device_id=action.device_id,
                        lane=lane,
                        action_id=action.id,
                        phase=phase,
                        checkpoint_seconds=checkpoint,
                    )
                )
        if admitted and lane == "safety":
            attempt.consumed_checkpoints.add(checkpoint)
            action.result_json = progress
        return admitted

    async def _load_persisted_observation(self, device_id: str | None, evidence_after: dt.datetime):
        if not device_id:
            return None
        as_of = dt.datetime.now(dt.timezone.utc)
        evidence_after = _as_utc(evidence_after)
        if evidence_after is None or evidence_after > as_of:
            return None
        async with self._session_factory() as session:
            record = (
                await session.execute(
                    select(DeviceStatusRecord)
                    .where(
                        DeviceStatusRecord.device_id == device_id,
                        DeviceStatusRecord.ts > evidence_after,
                        DeviceStatusRecord.ts <= as_of,
                    )
                    .order_by(DeviceStatusRecord.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        max_age = self._last_device_quality.get(device_id, {}).get("threshold_seconds", 900)
        if (
            record is None
            or device_status_clock_issue(
                record,
                as_of=as_of,
                max_age_seconds=max_age,
                evidence_after=evidence_after,
                require_remote_clock=True,
            )
            or _as_utc(record.ts) <= evidence_after
            or (as_of - _as_utc(record.ts)).total_seconds() > max_age
        ):
            return None
        try:
            from aioaquarea.data import SpecialStatus

            special_status = (
                SpecialStatus(record.special_status).name
                if record.special_status is not None
                else None
            )
        except (TypeError, ValueError):
            return None
        return VerificationObservation(
            record.force_dhw,
            record.quiet_mode,
            special_status,
            record.tank_temp,
            record.tank_target_temp,
            {0: record.zone1_target_temp, 1: record.zone1_target_temp, 2: record.zone2_target_temp},
        )

    @staticmethod
    def _normalize_live_observation(
        device,
        *,
        as_of=None,
        captured_at=None,
        max_age_seconds=900,
        evidence_after=None,
    ) -> VerificationObservation:
        as_of = _as_utc(as_of) or dt.datetime.now(dt.timezone.utc)
        if observation_clock_issue(
            as_of=as_of,
            captured_at=captured_at,
            observed_at=getattr(device, "status_observed_at", None),
            retrieved_at=getattr(device, "status_retrieved_at", None),
            max_age_seconds=max_age_seconds,
            evidence_after=evidence_after,
        ):
            raise _VerificationEvidenceUnavailable("Live observation clocks are not admissible")
        tank = getattr(device, "tank", None)
        zones = getattr(device, "zones", {}) or {}
        return VerificationObservation(
            getattr(getattr(device, "force_dhw", None), "value", None),
            getattr(getattr(device, "quiet_mode", None), "value", None),
            getattr(getattr(device, "special_status", None), "name", None),
            getattr(tank, "temperature", None),
            getattr(tank, "target_temperature", None),
            {
                int(zone_id): getattr(zone, "heat_target_temperature", None)
                for zone_id, zone in zones.items()
            },
        )

    async def _store_verification_progress(
        self, action: PlanActionRecord, attempts: int, result: VerifyResult
    ) -> None:
        if is_restorative_action(action):
            await self._update_safety_action(
                action,
                {
                    "verify_attempts": attempts,
                    "last_observed_json": json.dumps(result.as_dict()),
                    "result_json": self._safety_result(
                        action,
                        safety_attempt=self._attempt_for(action).metadata(outcome="verifying"),
                    ),
                },
            )
            return
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                )
            )

    async def _mark_verified(
        self, action: PlanActionRecord, attempts: int, result: VerifyResult
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="success",
        )
        if is_restorative_action(action):
            audit_record.target_device = action.device_id
            await self._assert_safety_authority(action, cleanup=True)
            await self._update_safety_action(
                action,
                {
                    "status": "executed",
                    "executed_at": now,
                    "verify_attempts": attempts,
                    "last_observed_json": json.dumps(result.as_dict()),
                    "result_json": self._safety_result(
                        action,
                        success=True,
                        verified=True,
                        safety_attempt=self._attempt_for(action).metadata(outcome="verified"),
                    ),
                },
                audit=audit_record,
                cleanup=True,
            )
            logger.info(
                "action_verified",
                action_type=action.action_type,
                action_id=action.id,
                verify_attempts=attempts,
            )
            return
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="executed",
                    executed_at=now,
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                    result_json=json.dumps({"success": True, "verified": True}),
                )
            )
            session.add(audit_record)
        logger.info(
            "action_verified",
            action_type=action.action_type,
            action_id=action.id,
            verify_attempts=attempts,
        )

    async def _mark_executed_unverified(self, action: PlanActionRecord, reason: str) -> None:
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Unverified linked restores must remain unresolved")
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="executed_unverified",
                    executed_at=dt.datetime.now(dt.timezone.utc),
                    result_json=json.dumps({"success": True, "verified": False, "reason": reason}),
                )
            )
        logger.warning(
            "action_executed_unverified",
            action_id=action.id,
            action_type=action.action_type,
            reason=reason,
        )

    async def _mark_failed(
        self, action: PlanActionRecord, attempts: int, result: VerifyResult
    ) -> None:
        if is_restorative_action(action):
            raise _SafetyAuthorityLost("Linked restore failures must remain unresolved")
        now = dt.datetime.now(dt.timezone.utc)
        audit_record = AuditLogRecord(
            actor="optimizer",
            action=action.action_type,
            payload_json=action.payload_json,
            result="failed",
        )
        async with self._session_factory() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.id == action.id)
                .values(
                    status="failed",
                    executed_at=now,
                    verify_attempts=attempts,
                    last_observed_json=json.dumps(result.as_dict()),
                    result_json=json.dumps(
                        {
                            "success": False,
                            "verified": False,
                            "reason": result.reason,
                            "observed": result.observed_value,
                            "expected": result.expected_value,
                        }
                    ),
                )
            )
            session.add(audit_record)
        logger.error(
            "action_verification_failed",
            action_type=action.action_type,
            action_id=action.id,
            verify_attempts=attempts,
            observed=result.observed_value,
            expected=result.expected_value,
            reason=result.reason,
        )

    async def expire_stale_actions(self) -> None:
        """Mark stale pending actions as expired with a diagnostic reason.

        Runs periodically to catch actions that the executor never picked up
        (e.g. scheduled during an override window, or from a superseded plan).
        Only expires actions older than 2 minutes to avoid racing with
        execute_due_actions.
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - SAFETY_ACTION_STUCK_AFTER
        device_quality = await self._device_quality_check(now=now)

        async with self._session_factory() as session:
            result = await session.execute(
                select(PlanActionRecord)
                .join(PlanRecord, PlanActionRecord.plan_id == PlanRecord.id)
                .where(
                    and_(
                        PlanActionRecord.status.in_(["pending", "executing"]),
                        PlanActionRecord.scheduled_ts <= cutoff,
                        ~unresolved_revert_predicate(),
                        PlanRecord.status == ACTIVE_PLAN_STATUS,
                    )
                )
                .order_by(PlanActionRecord.scheduled_ts)
                .limit(20)
            )
            stale = result.scalars().all()

            if not stale:
                return

            latest_plan_result = await session.execute(
                select(PlanRecord.id)
                .where(PlanRecord.status != DIAGNOSTIC_PLAN_STATUS)
                .order_by(PlanRecord.created_at.desc())
                .limit(1)
            )
            latest_plan_id = latest_plan_result.scalar_one_or_none()

            for action in stale:
                reason = await self._diagnose_missed(session, action, latest_plan_id, now)
                if not device_quality["ready"] and is_restorative_action(action):
                    logger.info(
                        "action_expiry_deferred_device_data_quality",
                        action_id=action.id,
                        reasons=device_quality["reasons"],
                    )
                    continue
                await session.execute(
                    update(PlanActionRecord)
                    .where(PlanActionRecord.id == action.id)
                    .where(PlanActionRecord.status.in_(["pending", "executing"]))
                    .values(
                        status="expired",
                        executed_at=now,
                        result_json=json.dumps(reason),
                    )
                )
                logger.info(
                    "action_expired",
                    action_id=action.id,
                    action_type=action.action_type,
                    diagnosis=reason.get("reason"),
                )

    @staticmethod
    async def _diagnose_missed(
        session, action: PlanActionRecord, latest_plan_id: int | None, now
    ) -> dict:
        """Determine why a pending action was never executed."""
        scheduled = action.scheduled_ts
        gap_minutes = round((now - scheduled).total_seconds() / 60, 1)

        if latest_plan_id and action.plan_id != latest_plan_id:
            return {
                "reason": "superseded",
                "gap_minutes": gap_minutes,
                "detail": f"Replaced by plan #{latest_plan_id}",
            }

        window_end = scheduled + dt.timedelta(minutes=2)
        override_result = await session.execute(
            select(OverrideRecord)
            .where(
                and_(
                    OverrideRecord.ts_from <= window_end,
                    OverrideRecord.ts_to >= scheduled,
                )
            )
            .limit(1)
        )
        blocking_override = override_result.scalar_one_or_none()
        if blocking_override:
            return {
                "reason": "override_active",
                "override": blocking_override.reason or "manual override",
                "gap_minutes": gap_minutes,
                "detail": f"Override '{blocking_override.reason}' was active at scheduled time",
            }

        plan_window_start = scheduled - dt.timedelta(minutes=1)
        plan_window_end = scheduled + dt.timedelta(minutes=3)
        plan_result = await session.execute(
            select(PlanRecord.id, PlanRecord.created_at)
            .where(
                and_(
                    PlanRecord.created_at >= plan_window_start,
                    PlanRecord.created_at <= plan_window_end,
                    PlanRecord.status != DIAGNOSTIC_PLAN_STATUS,
                )
            )
            .order_by(PlanRecord.created_at.desc())
            .limit(1)
        )
        concurrent_plan = plan_result.one_or_none()
        if concurrent_plan:
            return {
                "reason": "optimization_overlap",
                "concurrent_plan_id": concurrent_plan[0],
                "gap_minutes": gap_minutes,
                "detail": (
                    f"Plan #{concurrent_plan[0]} was being generated at "
                    f"{concurrent_plan[1].strftime('%H:%M:%S')} - may have blocked the executor"
                ),
            }

        if gap_minutes > 10:
            return {
                "reason": "executor_gap",
                "gap_minutes": gap_minutes,
                "detail": f"Executor did not run for ~{round(gap_minutes)} min after scheduled time",
            }

        return {
            "reason": "timing",
            "gap_minutes": gap_minutes,
            "detail": (
                f"Action was due {gap_minutes} min ago but was never picked up "
                f"- possible event-loop delay or transient DB error"
            ),
        }

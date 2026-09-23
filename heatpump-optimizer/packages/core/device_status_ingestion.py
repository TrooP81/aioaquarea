"""Atomic, serialized persistence for device status and gate evidence."""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import DeviceStatusRecord, SpaceHeatingGateRecord
from packages.core.settings_service import get_space_heating_gate_config
from packages.core.space_heating_gate import GateEvidence, SpaceHeatingGateState, transition_gate


def _device_lock_key(device_id: str) -> int:
    digest = hashlib.sha256(f"space-heating-gate:{device_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def ingest_device_status(session: AsyncSession, record: DeviceStatusRecord) -> GateEvidence:
    """Add a status and its raw-sensor gate transition in one DB transaction."""
    config = await get_space_heating_gate_config()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _device_lock_key(record.device_id)}
    )
    row = (
        await session.execute(
            select(SpaceHeatingGateRecord)
            .where(SpaceHeatingGateRecord.device_id == record.device_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    session.add(record)
    await session.flush()
    previous_state = (
        row.state
        if row is not None and row.config_fingerprint == config.fingerprint
        else SpaceHeatingGateState.UNKNOWN
    )
    evidence = transition_gate(previous_state, record.heat_pump_outdoor_temp, config)
    now = dt.datetime.now(dt.timezone.utc)
    if row is None:
        row = SpaceHeatingGateRecord(device_id=record.device_id)
        session.add(row)
    transitioned = row.state != evidence.state if getattr(row, "state", None) else True
    row.state = evidence.state
    row.config_fingerprint = evidence.config_fingerprint
    row.base_c = evidence.base_c
    row.on_threshold_c = evidence.on_threshold_c
    row.off_threshold_c = evidence.off_threshold_c
    row.last_raw_outdoor_c = evidence.last_raw_outdoor_c
    row.reason_code = evidence.reason_code
    row.source_status_ts = record.ts
    row.evaluated_at = now
    row.transitioned_at = now if transitioned else row.transitioned_at
    if evidence.last_raw_outdoor_c is None:
        row.consecutive_evaluation_failures = (row.consecutive_evaluation_failures or 0) + 1
        row.failure_since = row.failure_since or now
        row.last_failure_at = now
    else:
        row.consecutive_evaluation_failures = 0
        row.failure_since = None
        row.last_failure_at = None
    return evidence

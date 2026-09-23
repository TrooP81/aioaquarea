import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.device_status_ingestion import _device_lock_key, ingest_device_status
from packages.core.models import DeviceStatusRecord
from packages.core.space_heating_gate import HeatingGateConfig, SpaceHeatingGateState


@pytest.mark.asyncio
async def test_ingestion_serializes_gate_transition_before_locked_row_read():
    session = SimpleNamespace()
    row_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    session.execute = AsyncMock(side_effect=[SimpleNamespace(), row_result])
    session.add = MagicMock()
    session.flush = AsyncMock()
    record = DeviceStatusRecord(
        ts=dt.datetime(2026, 9, 17, 10, tzinfo=dt.timezone.utc),
        device_id="device-a",
        heat_pump_outdoor_temp=12.9,
    )
    config = HeatingGateConfig()

    with patch(
        "packages.core.device_status_ingestion.get_space_heating_gate_config",
        new=AsyncMock(return_value=config),
    ):
        evidence = await ingest_device_status(session, record)

    lock_statement = session.execute.await_args_list[0].args[0]
    row_statement = session.execute.await_args_list[1].args[0]
    assert "pg_advisory_xact_lock" in str(lock_statement)
    assert getattr(row_statement, "_for_update_arg", None) is not None
    assert evidence.state is SpaceHeatingGateState.ALLOWED
    session.flush.assert_awaited_once()
    assert session.add.call_count == 2


def test_ingestion_lock_key_is_stable_per_device_and_separates_devices():
    assert _device_lock_key("device-a") == _device_lock_key("device-a")
    assert _device_lock_key("device-a") != _device_lock_key("device-b")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_state", [SpaceHeatingGateState.BLOCKED, SpaceHeatingGateState.ALLOWED]
)
async def test_ingestion_discards_old_fingerprint_state_until_outside_band_reconfirms(old_state):
    old_config = HeatingGateConfig(base_c=12.0)
    config = HeatingGateConfig(base_c=13.0)
    row = SimpleNamespace(
        device_id="device-a",
        state=old_state,
        config_fingerprint=old_config.fingerprint,
        transitioned_at=None,
    )
    row_result = SimpleNamespace(scalar_one_or_none=lambda: row)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[SimpleNamespace(), row_result, SimpleNamespace(), row_result]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    in_band = DeviceStatusRecord(
        ts=dt.datetime(2026, 9, 17, 10, tzinfo=dt.timezone.utc),
        device_id="device-a",
        heat_pump_outdoor_temp=15.0,
    )
    outside_band = DeviceStatusRecord(
        ts=dt.datetime(2026, 9, 17, 11, tzinfo=dt.timezone.utc),
        device_id="device-a",
        heat_pump_outdoor_temp=13.0,
    )

    with patch(
        "packages.core.device_status_ingestion.get_space_heating_gate_config",
        new=AsyncMock(return_value=config),
    ):
        evidence = await ingest_device_status(session, in_band)
        reconfirmed = await ingest_device_status(session, outside_band)

    assert evidence.state is SpaceHeatingGateState.UNKNOWN
    assert row.state is SpaceHeatingGateState.ALLOWED
    assert row.config_fingerprint == config.fingerprint
    assert reconfirmed.state is SpaceHeatingGateState.ALLOWED

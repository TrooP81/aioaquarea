from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.heating_evidence import classify_space_heating
from packages.core.panasonic_control_state import classify_panasonic_operation_mode


FROZEN_STORED_MODES = (
    None,
    "0",
    "1",
    "2",
    "3",
    "4",
    "heat",
    "cool",
    "auto_heat",
    "AUTO_COOL",
    "ExtendedOperationMode.HEAT",
    "ExtendedOperationMode.AUTO_COOL",
    "malformed",
)

FROZEN_MODE_CLASSIFICATIONS = {
    None: "other",
    "0": "other",
    "1": "heating",
    "2": "cooling",
    "3": "heating",
    "4": "cooling",
    "heat": "heating",
    "cool": "cooling",
    "auto_heat": "heating",
    "auto_cool": "cooling",
    "extendedoperationmode.heat": "heating",
    "extendedoperationmode.auto_cool": "cooling",
    "malformed": "other",
}


def _frozen_pre_refactor_space_heating_active(mode, operation_status):
    """Archived classification path before the shared-mode-helper refactor."""

    if str(mode) in {"1", "3"}:
        return True
    if operation_status == 0:
        return False
    return False


def _frozen_expected_mode(mode):
    return FROZEN_MODE_CLASSIFICATIONS.get(str(mode).strip().lower(), "other")


@pytest.mark.asyncio(loop_scope="session")
class TestPanasonicModeParity:
    async def test_classifier_parity_for_distinct_stored_mode_operation_values(
        self, db_session: AsyncSession, seed_panasonic_mode_parity_statuses
    ):
        rows = await db_session.execute(
            text("SELECT DISTINCT mode, operation_status FROM device_status")
        )
        distinct_rows = {(mode, operation_status) for mode, operation_status in rows}
        expected_rows = {
            (mode, operation_status)
            for mode in FROZEN_STORED_MODES
            for operation_status in (None, 0, 1)
        }

        assert distinct_rows == expected_rows
        for mode, operation_status in distinct_rows:
            evidence = classify_space_heating(
                operation_status=operation_status,
                mode=mode,
                direction="PUMP",
                pump_duty=1,
                device_action="OFF",
                defrost_active=False,
                zone1_operation_status=1,
            )
            assert evidence.active is _frozen_pre_refactor_space_heating_active(
                mode, operation_status
            )
            assert classify_panasonic_operation_mode(mode) == _frozen_expected_mode(mode)

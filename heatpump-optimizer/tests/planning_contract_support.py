from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

from packages.core.comfort_schedule import TIMEZONE_POLICY, timezone_data_version


def local_contract_revision(revision_id=7):
    contract_hash = f"{revision_id:064x}"
    projection = {
        "schema_version": 2,
        "installation_id": "00000000-0000-0000-0000-000000000001",
        "house_id": "00000000-0000-0000-0000-000000000002",
        "timezone": "UTC",
        "comfort_schedule": {"weekday": [], "weekend": []},
        "comfort_temp_min": 19.0,
        "comfort_temp_target": 19.0,
        "comfort_temp_max": 25.0,
        "timezone_policy": TIMEZONE_POLICY,
        "tzdata_version": timezone_data_version(),
    }
    revision = SimpleNamespace(
        id=revision_id,
        contract_hash=contract_hash,
        contract_json=projection,
        effective_at=dt.datetime.now(dt.timezone.utc),
        source="unit_test",
        demand_dependency_generation=0,
        indoor_dependency_generation=1,
    )
    return revision


def install_local_contract_revision(monkeypatch, *, session=None, revision_id=7):
    from packages.core import house_contract
    from packages.optimizer import main

    revision = local_contract_revision(revision_id)

    async def ensure_current_contract(_session, _settings=None, **_options):
        return revision

    monkeypatch.setattr(
        main,
        "_freeze_contract_revision",
        AsyncMock(return_value=(revision.id, revision.contract_hash)),
    )
    monkeypatch.setattr(house_contract, "ensure_current_contract", ensure_current_contract)
    if session is not None:
        session.get = AsyncMock(return_value=revision)
    return revision

"""Database heartbeat detail helpers."""

import json
from types import SimpleNamespace

from packages.core.service_health import (
    merge_service_heartbeat_details,
    service_heartbeat_details,
)


def test_heartbeat_details_merge_preserves_adapter_state() -> None:
    existing = json.dumps({"panasonic_adapter": {"status": "available"}})

    merged = merge_service_heartbeat_details(existing, {"poll_interval_seconds": 300})

    assert json.loads(merged) == {
        "panasonic_adapter": {"status": "available"},
        "poll_interval_seconds": 300,
    }


def test_invalid_heartbeat_details_are_replaced_safely() -> None:
    merged = merge_service_heartbeat_details("not-json", {"healthy": True})

    assert json.loads(merged) == {"healthy": True}
    assert service_heartbeat_details(SimpleNamespace(details_json="[]")) == {}

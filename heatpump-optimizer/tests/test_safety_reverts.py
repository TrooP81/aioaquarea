from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.safety_reverts import (
    dhw_embargoed,
    normalize_zone_id,
    validate_action_pair,
    zone_embargoed,
    zone_matches_baseline,
)


def _action(action_type, *, device_id="device-a", zone_id=1, status="pending", linked=True):
    return {
        "action_type": action_type,
        "device_id": device_id,
        "payload": {"zone_id": zone_id},
        "status": status,
        "reverts_action_id": 10 if linked else None,
    }


class TestSafetyRevertPairs:
    def test_p2_ac5_rejects_unpaired_action_types(self):
        with pytest.raises(ValueError, match="invalid safety action pair"):
            validate_action_pair(_action("force_dhw_on"), _action("zone_temp_restore"))

    def test_p2_ac5_rejects_cross_device_pair(self):
        with pytest.raises(ValueError, match="same device"):
            validate_action_pair(
                _action("force_dhw_on", device_id="device-a"),
                _action("force_dhw_off", device_id="device-b"),
            )

    def test_p2_ac5_rejects_cross_zone_pair(self):
        with pytest.raises(ValueError, match="same zone"):
            validate_action_pair(
                _action("zone_temp_boost", zone_id=1),
                _action("zone_temp_restore", zone_id=2),
            )


class TestSafetyRevertEvidence:
    @pytest.mark.parametrize(
        ("baseline", "current", "expected"),
        [(35, 35, True), (35.0, 35, True), (35, 36, False), (None, 35, False), (True, 1, False)],
    )
    def test_p2_ac14_zone_matches_baseline_truth_table(self, baseline, current, expected):
        assert zone_matches_baseline(baseline, current) is expected

    @pytest.mark.parametrize("zone_id", [None, 0, 1, False, "bad"])
    def test_normalizes_legacy_primary_zone(self, zone_id):
        assert normalize_zone_id(zone_id) == 1


class TestSafetyRevertEmbargo:
    def test_p2_ac9_dhw_embargo_is_device_scoped(self):
        actions = [_action("force_dhw_off", device_id="device-a")]
        assert dhw_embargoed(actions, "device-a")
        assert not dhw_embargoed(actions, "device-b")

    def test_p2_ac9_zone_embargo_is_device_and_zone_scoped(self):
        actions = [_action("zone_temp_restore", device_id="device-a", zone_id=2)]
        status = SimpleNamespace(zone1_target_temp=30, zone2_target_temp=30)
        increase = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-a",
            "payload": {"zone_id": 2, "temperature": 31},
        }
        other_zone = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-a",
            "payload": {"zone_id": 1, "temperature": 31},
        }
        other_device = {
            "type": "set_zone_heat_temperature",
            "device_id": "device-b",
            "payload": {"zone_id": 2, "temperature": 31},
        }

        assert zone_embargoed(actions, increase, status)
        assert not zone_embargoed(actions, other_zone, status)
        assert not zone_embargoed(actions, other_device, status)

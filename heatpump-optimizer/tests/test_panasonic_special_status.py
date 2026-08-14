"""Tests for safe optimizer use of Panasonic special status."""

from types import SimpleNamespace

from aioaquarea.data import SpecialStatus

from packages.core.panasonic_special_status import optimizer_special_status_supported


def _zone(
    *,
    supports: bool = True,
    eco_heat: int | None = -2,
    comfort_heat: int | None = 2,
):
    return SimpleNamespace(
        supports_special_status=supports,
        heat_target_temperature=-5,
        cool_target_temperature=None,
        temperature_modifiers={
            SpecialStatus.ECO: SimpleNamespace(heat=eco_heat, cool=None),
            SpecialStatus.COMFORT: SimpleNamespace(heat=comfort_heat, cool=None),
        },
    )


def _device(zone, *, supports: bool = True):
    return SimpleNamespace(support_special_status=supports, zones={1: zone})


def test_accepts_complete_energy_saving_heating_modifiers() -> None:
    assert optimizer_special_status_supported(_device(_zone()))


def test_rejects_device_or_zone_without_special_status_support() -> None:
    assert not optimizer_special_status_supported(_device(_zone(), supports=False))
    assert not optimizer_special_status_supported(_device(_zone(supports=False)))


def test_rejects_missing_or_inverted_heating_modifiers() -> None:
    assert not optimizer_special_status_supported(_device(_zone(eco_heat=None)))
    assert not optimizer_special_status_supported(_device(_zone(eco_heat=2)))
    assert not optimizer_special_status_supported(_device(_zone(comfort_heat=-2)))


def test_rejects_partial_multi_zone_support() -> None:
    device = SimpleNamespace(
        support_special_status=True,
        zones={1: _zone(), 2: _zone(supports=False)},
    )

    assert not optimizer_special_status_supported(device)

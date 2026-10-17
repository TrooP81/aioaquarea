from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aioaquarea.auth import CCAppVersion, PanasonicSettings
from aioaquarea.data import (
    DeviceAction,
    DeviceDirection,
    DeviceInfo,
    DeviceModeStatus,
    DeviceStatus,
    DeviceZoneInfo,
    DeviceZoneStatus,
    ExtendedOperationMode,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationMode,
    OperationStatus,
    PowerfulTime,
    PumpDuty,
    QuietMode,
    SensorMode,
    SpecialStatus,
    StatusDataMode,
    UpdateOperationMode,
    ZoneSensor,
    ZoneType,
)
from aioaquarea.device_manager import DeviceManager
from aioaquarea.entities import DeviceImpl
from aioaquarea.errors import DeviceUnavailableError, RequestFailedError


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture
def device_info():
    return DeviceInfo(
        device_id="device-1",
        name="Aquarea",
        long_id="device-1",
        mode=OperationMode.Heat,
        has_tank=True,
        firmware_version="1.0",
        model="WH-MDC05",
        zones=[
            DeviceZoneInfo(
                zone_id=1,
                name="Zone 1",
                type=ZoneType.ROOM,
                cool_mode=True,
                zone_sensor=ZoneSensor.INTERNAL,
                heat_sensor=SensorMode.DIRECT,
                cool_sensor=SensorMode.DIRECT,
            )
        ],
        status_data_mode=StatusDataMode.LIVE,
    )


@pytest.fixture
def device_manager():
    settings = PanasonicSettings()
    settings.access_token = "token-123"
    client = SimpleNamespace(_api_client=SimpleNamespace(request=AsyncMock()))
    return DeviceManager(
        client=client,
        settings=settings,
        app_version=CCAppVersion(),
        logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )


@pytest.mark.asyncio
async def test_get_devices_parses_group_list_into_device_info(device_manager):
    device_manager._client._api_client.request.return_value = FakeResponse(
        {
            "groupList": [
                {
                    "deviceList": [
                        {
                            "deviceType": "2",
                            "deviceGuid": "guid-1",
                            "deviceName": "Main Unit",
                            "operationMode": 3,
                            "tankStatus": {"temperatureNow": 48},
                            "zoneStatus": [{"zoneId": 1, "heatMin": 20, "heatMax": 55}],
                        }
                    ]
                }
            ]
        }
    )

    devices = await device_manager.get_devices()

    assert len(devices) == 1
    assert devices[0].device_id == "guid-1"
    assert devices[0].has_tank is True
    assert devices[0].zones[0].zone_id == 1


@pytest.mark.asyncio
async def test_get_devices_accepts_guid_strings_in_device_id_list(device_manager):
    device_manager._client._api_client.request.return_value = FakeResponse(
        {"groupList": [{"deviceIdList": ["guid-1"]}]}
    )

    devices = await device_manager.get_devices()

    assert len(devices) == 1
    assert devices[0].device_id == "guid-1"
    assert devices[0].name == "Unknown Device"


@pytest.mark.asyncio
async def test_get_devices_excludes_dict_without_device_type(device_manager):
    device_manager._client._api_client.request.return_value = FakeResponse(
        {"groupList": [{"deviceList": [{"deviceGuid": "guid-1"}]}]}
    )

    assert await device_manager.get_devices() == []


@pytest.mark.asyncio
async def test_get_device_status_parses_status_payload(device_manager, device_info):
    device_manager._client._api_client.request.return_value = FakeResponse(
        {
            "status": {
                "operationStatus": 1,
                "deiceStatus": 0,
                "outdoorNow": 7,
                "operationMode": 1,
                "faultStatus": [{"errorMessage": "fault", "errorCode": "F01"}],
                "direction": 1,
                "pumpDuty": 1,
                "tankStatus": {
                    "operationStatus": 1,
                    "temperatureNow": 49,
                    "heatMax": 55,
                    "heatMin": 40,
                    "heatSet": 47,
                },
                "zoneStatus": [
                    {
                        "zoneId": 1,
                        "temperatureNow": 35,
                        "operationStatus": 1,
                        "heatMax": 45,
                        "heatMin": 25,
                        "heatSet": 33,
                        "coolMax": 20,
                        "coolMin": 5,
                        "coolSet": 18,
                        "comfortCool": 19,
                        "comfortHeat": 34,
                        "ecoCool": 21,
                        "ecoHeat": 30,
                    }
                ],
                "quietMode": 2,
                "forceDHW": 1,
                "forceHeater": 0,
                "holidayTimer": 1,
                "powerful": 3,
                "specialStatus": 1,
            }
        }
    )

    status = await device_manager.get_device_status(device_info)

    assert status.operation_status == OperationStatus.ON
    assert status.operation_status_present is True
    assert status.operation_status_valid is True
    assert status.device_status == DeviceModeStatus.NORMAL
    assert status.operation_mode == ExtendedOperationMode.HEAT
    assert status.direction == DeviceDirection.PUMP
    assert status.pump_duty == PumpDuty.ON
    assert status.quiet_mode == QuietMode.LEVEL2
    assert status.force_dhw == ForceDHW.ON
    assert status.force_heater == ForceHeater.OFF
    assert status.holiday_timer == HolidayTimer.ON
    assert status.powerful_time == PowerfulTime.ON_90MIN
    assert status.special_status == SpecialStatus.ECO
    assert status.tank_status[0].temperature == 49
    assert status.zones[0].heat_set == 33
    assert status.fault_status[0].error_code == "F01"
    assert status.status_data_mode == StatusDataMode.LIVE


@pytest.mark.parametrize(
    ("direction", "pump_duty"),
    [(None, None), (99, 99)],
)
@pytest.mark.asyncio
async def test_get_device_status_defaults_invalid_direction_and_pump_duty(
    device_manager, device_info, direction, pump_duty
):
    device_manager._client._api_client.request.return_value = FakeResponse(
        _minimal_status_payload(direction=direction, pumpDuty=pump_duty)
    )

    status = await device_manager.get_device_status(device_info)

    assert status.direction == DeviceDirection.IDLE
    assert status.pump_duty == PumpDuty.OFF


@pytest.mark.asyncio
async def test_get_device_status_marks_cached_fallback(device_manager, device_info):
    device_manager._client._api_client.request.side_effect = [
        RuntimeError("adaptor unavailable"),
        FakeResponse(_minimal_status_payload()),
    ]

    status = await device_manager.get_device_status(device_info)

    assert status.status_data_mode == StatusDataMode.CACHED
    assert device_manager._client._api_client.request.await_count == 2
    live_request = device_manager._client._api_client.request.await_args_list[0]
    cached_request = device_manager._client._api_client.request.await_args_list[1]
    assert "deviceDirect=1" in live_request.kwargs["json"]["apiName"]
    assert "deviceDirect=0" in cached_request.kwargs["json"]["apiName"]


@pytest.mark.asyncio
async def test_get_device_status_can_require_live_data(device_manager, device_info):
    failure = RuntimeError("adaptor unavailable")
    device_manager._client._api_client.request.side_effect = failure

    with pytest.raises(
        DeviceUnavailableError, match="Panasonic adaptor unavailable"
    ) as exc:
        await device_manager.get_device_status(device_info, allow_cached_fallback=False)

    assert isinstance(exc.value, RequestFailedError)
    assert exc.value.device_id == device_info.device_id
    assert exc.value.reason == "adaptor unavailable"
    assert exc.value.__cause__ is failure
    assert device_manager._client._api_client.request.await_count == 1
    live_request = device_manager._client._api_client.request.await_args
    assert "deviceDirect=1" in live_request.kwargs["json"]["apiName"]


def _minimal_status_payload(**overrides):
    """Build a minimal but valid device-status payload for parsing tests."""
    status = {
        "operationStatus": 1,
        "specialStatus": 0,
        "deiceStatus": 0,
        "operationMode": 1,
        "direction": 1,
        "pumpDuty": 1,
    }
    status.update(overrides)
    return {"status": status}


def _device_status(
    *zones: DeviceZoneStatus,
    operation_status_present: bool | None = None,
    operation_status_valid: bool | None = None,
) -> DeviceStatus:
    return DeviceStatus(
        long_id="device-1",
        operation_status=OperationStatus.ON,
        device_status=DeviceModeStatus.NORMAL,
        temperature_outdoor=7,
        operation_mode=ExtendedOperationMode.HEAT,
        fault_status=[],
        direction=DeviceDirection.PUMP,
        pump_duty=PumpDuty.ON,
        tank_status=[],
        zones=list(zones),
        quiet_mode=QuietMode.OFF,
        force_dhw=ForceDHW.OFF,
        force_heater=ForceHeater.OFF,
        holiday_timer=HolidayTimer.OFF,
        powerful_time=PowerfulTime.OFF,
        special_status=None,
        operation_status_present=operation_status_present,
        operation_status_valid=operation_status_valid,
    )


def _zone_status(
    zone_id: int,
    temperature: int,
    operation_status: OperationStatus = OperationStatus.ON,
) -> DeviceZoneStatus:
    return DeviceZoneStatus(
        zone_id=zone_id,
        temperature=temperature,
        operation_status=operation_status,
        heat_max=45,
        heat_min=25,
        heat_set=33,
        cool_max=20,
        cool_min=5,
        cool_set=18,
        comfort_heat=34,
        comfort_cool=19,
        eco_heat=30,
        eco_cool=21,
    )


def _zone_info(zone_id: int, name: str) -> DeviceZoneInfo:
    return DeviceZoneInfo(
        zone_id=zone_id,
        name=name,
        type=ZoneType.ROOM,
        cool_mode=True,
        zone_sensor=ZoneSensor.INTERNAL,
        heat_sensor=SensorMode.DIRECT,
        cool_sensor=SensorMode.DIRECT,
    )


def _external_zone_info(zone_id: int, name: str) -> DeviceZoneInfo:
    return DeviceZoneInfo(
        zone_id=zone_id,
        name=name,
        type=ZoneType.ROOM,
        cool_mode=True,
        zone_sensor=ZoneSensor.EXTERNAL,
        heat_sensor=SensorMode.DIRECT,
        cool_sensor=SensorMode.DIRECT,
    )


def _device_impl(
    device_id: str, zones_info: list[DeviceZoneInfo], status: DeviceStatus, client
) -> DeviceImpl:
    return DeviceImpl(
        device_id=device_id,
        long_id=device_id,
        name=device_id,
        firmware_version="1.0",
        model="WH-MDC05",
        has_tank=False,
        zones_info=zones_info,
        status=status,
        client=client,
    )


def test_device_zones_are_isolated_between_instances():
    client = SimpleNamespace()
    first = _device_impl(
        "device-1",
        [_zone_info(1, "First zone")],
        _device_status(_zone_status(1, 21)),
        client,
    )
    second = _device_impl(
        "device-2",
        [_zone_info(2, "Second zone")],
        _device_status(_zone_status(2, 22)),
        client,
    )

    assert list(first.zones) == [1]
    assert first.zones[1].name == "First zone"
    assert list(second.zones) == [2]
    assert second.zones[2].name == "Second zone"


@pytest.mark.asyncio
async def test_set_special_status_skips_external_sensor_zones():
    client = SimpleNamespace(post_device_set_special_status=AsyncMock())
    device = _device_impl(
        "device-1",
        [_zone_info(1, "Internal"), _external_zone_info(2, "External")],
        _device_status(_zone_status(1, 21), _zone_status(2, 22)),
        client,
    )

    await device.set_special_status(SpecialStatus.ECO)

    zones = client.post_device_set_special_status.await_args.args[2]
    assert [zone.zone_id for zone in zones] == [1]
    assert device.zones[2].temperature_modifiers == {}


@pytest.mark.asyncio
async def test_refresh_data_rebuilds_zones_without_extra_api_calls():
    client = SimpleNamespace(get_device_status=AsyncMock())
    initial_status = _device_status(_zone_status(1, 21), _zone_status(2, 22))
    refreshed_status = _device_status(
        _zone_status(1, 26, OperationStatus.OFF),
        operation_status_present=False,
        operation_status_valid=False,
    )
    client.get_device_status.return_value = refreshed_status
    device = _device_impl(
        "device-1",
        [_zone_info(1, "Living room"), _zone_info(2, "Bedroom")],
        initial_status,
        client,
    )

    await device.refresh_data()

    assert device.zones[1].temperature == 26
    assert device.zones[1].operation_status == OperationStatus.OFF
    assert device.zones[1].name == "Living room"
    assert device.zones[2].temperature == 0
    assert device.zones[2].operation_status == OperationStatus.OFF
    assert device.zones[2].name == "Bedroom"
    assert device.operation_status_present is False
    assert device.operation_status_valid is False
    client.get_device_status.assert_awaited_once_with(
        device._info, allow_cached_fallback=True
    )
    assert not hasattr(client, "get_device_consumption")


@pytest.mark.asyncio
async def test_refresh_data_preserves_zone_metadata_when_status_is_missing():
    client = SimpleNamespace(get_device_status=AsyncMock(return_value=_device_status()))
    device = _device_impl(
        "device-1",
        [_zone_info(1, "Living room")],
        _device_status(_zone_status(1, 21)),
        client,
    )

    await device.refresh_data()

    assert list(device.zones) == [1]
    assert device.zones[1].name == "Living room"
    assert device.zones[1].temperature == 0


@pytest.mark.asyncio
async def test_failed_refresh_preserves_existing_zones():
    client = SimpleNamespace(
        get_device_status=AsyncMock(side_effect=RuntimeError("offline"))
    )
    device = _device_impl(
        "device-1",
        [_zone_info(1, "Living room")],
        _device_status(_zone_status(1, 21)),
        client,
    )

    with pytest.raises(RuntimeError, match="offline"):
        await device.refresh_data()

    assert device.zones[1].temperature == 21


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0, None),  # 0 → no special mode active
        (1, SpecialStatus.ECO),
        (2, SpecialStatus.COMFORT),
        (99, None),  # unknown value → treated as no special status
    ],
)
@pytest.mark.asyncio
async def test_get_device_status_parses_special_status(
    device_manager, device_info, raw, expected
):
    device_manager._client._api_client.request.return_value = FakeResponse(
        _minimal_status_payload(specialStatus=raw)
    )

    status = await device_manager.get_device_status(device_info)

    assert status.special_status == expected


@pytest.mark.asyncio
async def test_get_device_status_special_status_absent_is_none(
    device_manager, device_info
):
    payload = _minimal_status_payload()
    payload["status"].pop("specialStatus")
    device_manager._client._api_client.request.return_value = FakeResponse(payload)

    status = await device_manager.get_device_status(device_info)

    assert status.special_status is None


@pytest.mark.asyncio
async def test_get_device_status_operation_status_independent_of_special(
    device_manager, device_info
):
    """Regression: operation_status must come from ``operationStatus`` and not be
    conflated with ``specialStatus`` (previously ECO made the device read as ON).
    """
    device_manager._client._api_client.request.return_value = FakeResponse(
        _minimal_status_payload(operationStatus=0, specialStatus=1)
    )

    status = await device_manager.get_device_status(device_info)

    assert status.operation_status == OperationStatus.OFF
    assert status.special_status == SpecialStatus.ECO


@pytest.mark.asyncio
async def test_get_device_status_records_missing_or_invalid_operation_status_provenance(
    device_manager, device_info
):
    payload = _minimal_status_payload(operationStatus="unexpected")
    device_manager._client._api_client.request.return_value = FakeResponse(payload)

    invalid = await device_manager.get_device_status(device_info)

    assert invalid.operation_status == OperationStatus.OFF
    assert invalid.operation_status_present is True
    assert invalid.operation_status_valid is False

    payload["status"].pop("operationStatus")
    device_manager._client._api_client.request.return_value = FakeResponse(payload)
    missing = await device_manager.get_device_status(device_info)

    assert missing.operation_status == OperationStatus.OFF
    assert missing.operation_status_present is False
    assert missing.operation_status_valid is False


@pytest.mark.asyncio
async def test_get_device_status_operation_status_defaults_off_when_absent(
    device_manager, device_info
):
    payload = _minimal_status_payload()
    payload["status"].pop("operationStatus")
    device_manager._client._api_client.request.return_value = FakeResponse(payload)

    status = await device_manager.get_device_status(device_info)

    assert status.operation_status == OperationStatus.OFF


@pytest.mark.asyncio
async def test_device_with_declared_tank_tolerates_missing_live_tank_status(
    device_manager, device_info
):
    device_manager._client._api_client.request.return_value = FakeResponse(
        _minimal_status_payload(direction=DeviceDirection.WATER.value)
    )
    status = await device_manager.get_device_status(device_info)
    client = SimpleNamespace(post_device_operation_update=AsyncMock())
    device = DeviceImpl(
        device_id=device_info.device_id,
        long_id=device_info.long_id,
        name=device_info.name,
        firmware_version=device_info.firmware_version,
        model=device_info.model,
        has_tank=True,
        zones_info=device_info.zones,
        status=status,
        client=client,
    )

    assert device.tank is None
    assert device.current_action == DeviceAction.IDLE

    await device.set_mode(UpdateOperationMode.HEAT)

    assert client.post_device_operation_update.await_args.args[4] == OperationStatus.OFF


@pytest.mark.parametrize(
    ("present", "valid", "expected"),
    [
        (True, True, DeviceAction.OFF),
        (None, None, DeviceAction.OFF),
        (False, False, DeviceAction.HEATING),
        (True, False, DeviceAction.HEATING),
    ],
)
def test_current_action_ignores_defaulted_off_operation_status(
    present, valid, expected
):
    status = replace(
        _device_status(
            _zone_status(1, 21),
            operation_status_present=present,
            operation_status_valid=valid,
        ),
        operation_status=OperationStatus.OFF,
    )
    device = _device_impl(
        "device-1", [_zone_info(1, "Living room")], status, SimpleNamespace()
    )

    assert device.current_action == expected

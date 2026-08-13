from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aioaquarea.auth import CCAppVersion, PanasonicSettings
from aioaquarea.data import (
    DeviceDirection,
    DeviceInfo,
    DeviceModeStatus,
    DeviceZoneInfo,
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
    ZoneSensor,
    ZoneType,
)
from aioaquarea.device_manager import DeviceManager


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
async def test_get_device_status_operation_status_defaults_off_when_absent(
    device_manager, device_info
):
    payload = _minimal_status_payload()
    payload["status"].pop("operationStatus")
    device_manager._client._api_client.request.return_value = FakeResponse(payload)

    status = await device_manager.get_device_status(device_info)

    assert status.operation_status == OperationStatus.OFF

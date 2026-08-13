from unittest.mock import AsyncMock

import pytest

from aioaquarea.data import (
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationStatus,
    PowerfulTime,
    QuietMode,
    UpdateOperationMode,
    ZoneTemperatureSetUpdate,
)
from aioaquarea.device_control import AquareaDeviceControl


@pytest.fixture
def device_control():
    return AquareaDeviceControl(api_client=AsyncMock(), base_url="https://example.test")


@pytest.mark.asyncio
async def test_post_device_tank_temperature_uses_transfer_payload(device_control):
    await device_control.post_device_tank_temperature("device-1", 47)

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {"gwid": "device-1", "tankStatus": {"heatSet": 47}},
        },
        throw_on_error=True,
    )


@pytest.mark.asyncio
async def test_post_device_operation_update_includes_zone_temperature_updates(
    device_control,
):
    await device_control.post_device_operation_update(
        long_id="device-1",
        mode=UpdateOperationMode.HEAT,
        zones={1: OperationStatus.ON, 2: OperationStatus.OFF},
        operation_status=OperationStatus.ON,
        tank_operation_status=OperationStatus.OFF,
        zone_temperature_updates=[
            ZoneTemperatureSetUpdate(zone_id=1, heat_set=35, cool_set=None),
            ZoneTemperatureSetUpdate(zone_id=2, heat_set=None, cool_set=18),
        ],
    )

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {
                "gwid": "device-1",
                "operationMode": UpdateOperationMode.HEAT.value,
                "operationStatus": OperationStatus.ON.value,
                "zoneStatus": [
                    {
                        "zoneId": 1,
                        "operationStatus": OperationStatus.ON.value,
                        "heatSet": 35,
                    },
                    {
                        "zoneId": 2,
                        "operationStatus": OperationStatus.OFF.value,
                        "coolSet": 18,
                    },
                ],
                "tankStatus": {"operationStatus": OperationStatus.OFF.value},
            },
        },
        throw_on_error=True,
    )


@pytest.mark.asyncio
async def test_post_device_set_quiet_mode_uses_transfer_payload(device_control):
    await device_control.post_device_set_quiet_mode("device-1", QuietMode.LEVEL2)

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {"gwid": "device-1", "quietMode": QuietMode.LEVEL2.value},
        },
        throw_on_error=True,
    )


@pytest.mark.asyncio
async def test_post_device_set_powerful_time_uses_transfer_payload(device_control):
    await device_control.post_device_set_powerful_time(
        "device-1", PowerfulTime.ON_60MIN
    )

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {
                "gwid": "device-1",
                "powerfulRequest": PowerfulTime.ON_60MIN.value,
            },
        },
        throw_on_error=True,
    )


@pytest.mark.parametrize(
    ("method_name", "value", "field"),
    [
        ("post_device_force_dhw", ForceDHW.ON, "forceDHW"),
        ("post_device_force_heater", ForceHeater.ON, "forceHeater"),
        ("post_device_holiday_timer", HolidayTimer.ON, "holidayTimer"),
    ],
)
@pytest.mark.asyncio
async def test_post_device_switch_commands_use_transfer_payload(
    device_control, method_name, value, field
):
    await getattr(device_control, method_name)("device-1", value)

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {"gwid": "device-1", field: value.value},
        },
        throw_on_error=True,
    )


@pytest.mark.asyncio
async def test_post_device_request_defrost_uses_transfer_payload(device_control):
    await device_control.post_device_request_defrost("device-1")

    device_control._api_client.request.assert_awaited_once_with(
        "POST",
        "remote/v1/app/common/transfer",
        json={
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {"gwid": "device-1", "forcedefrost": 1},
        },
        throw_on_error=True,
    )

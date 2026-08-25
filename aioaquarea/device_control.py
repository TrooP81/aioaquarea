from __future__ import annotations

from typing import TYPE_CHECKING

from .auth import PanasonicRequestHeader
from .command_result import PanasonicCommandResult
from .const import AQUAREA_SERVICE_A2W_STATUS_DISPLAY, AQUAREA_SERVICE_DEVICES
from .data import (
    DeviceZoneStatus,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationStatus,
    PowerfulTime,
    QuietMode,
    SpecialStatus,
    UpdateOperationMode,
    ZoneTemperatureSetUpdate,
)

if TYPE_CHECKING:
    from .api_client import AquareaAPIClient


class AquareaDeviceControl:
    """Handles device control operations."""

    _TRANSFER_URL = "remote/v1/app/common/transfer"
    _TRANSFER_API_NAME = "/remote/v1/api/devices"

    def __init__(self, api_client: AquareaAPIClient, base_url: str):
        self._api_client = api_client
        self._base_url = base_url

    @staticmethod
    def _build_transfer_request(
        long_id: str, **body_params: object
    ) -> dict[str, object]:
        return {
            "apiName": AquareaDeviceControl._TRANSFER_API_NAME,
            "requestMethod": "POST",
            "bodyParam": {"gwid": long_id, **body_params},
        }

    async def _post_transfer(
        self, payload: dict[str, object]
    ) -> PanasonicCommandResult:
        response = await self._api_client.request(
            "POST",
            self._TRANSFER_URL,
            json=payload,
            throw_on_error=True,
        )
        return await PanasonicCommandResult.from_response(response)

    async def post_device_operation_status(
        self, long_device_id: str, new_operation_status: OperationStatus
    ) -> PanasonicCommandResult:
        """Post device operation status."""
        data = {
            "status": [
                {
                    "deviceGuid": long_device_id,
                    "operationStatus": new_operation_status.value,
                }
            ]
        }

        response = await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_device_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}",
            ),
            json=data,
        )
        return await PanasonicCommandResult.from_response(response)

    async def post_device_tank_temperature(
        self, long_device_id: str, new_temperature: int
    ) -> PanasonicCommandResult:
        """Post device tank temperature."""
        return await self._post_transfer(
            self._build_transfer_request(
                long_device_id,
                tankStatus={"heatSet": new_temperature},
            )
        )

    async def post_device_tank_operation_status(
        self,
        long_device_id: str,
        new_operation_status: OperationStatus,
        zones: list[DeviceZoneStatus],
    ) -> PanasonicCommandResult:
        """Post device tank operation status."""
        zone_status_list = []
        for zone in zones:
            zone_status_list.append(
                {
                    "zoneId": zone.zone_id,
                    "operationStatus": zone.operation_status.value,
                }
            )

        return await self._post_transfer(
            self._build_transfer_request(
                long_device_id,
                zoneStatus=zone_status_list,
                tankStatus={"operationStatus": new_operation_status.value},
            )
        )

    async def post_device_operation_update(
        self,
        long_id: str,
        mode: UpdateOperationMode,
        zones: dict[int, OperationStatus],
        operation_status: OperationStatus,
        tank_operation_status: OperationStatus,
        zone_temperature_updates: (
            list[ZoneTemperatureSetUpdate] | None
        ) = None,  # New parameter
    ) -> PanasonicCommandResult:
        """Post device operation update."""
        # Construct zoneStatus list based on provided zones and optional temperature updates
        zone_status_list = []
        for zone_id, op_status in zones.items():
            zone_data = {
                "zoneId": zone_id,
                "operationStatus": op_status.value,
            }
            if zone_temperature_updates:
                for temp_update in zone_temperature_updates:
                    if temp_update.zone_id == zone_id:
                        if temp_update.heat_set is not None:
                            zone_data["heatSet"] = temp_update.heat_set
                        if temp_update.cool_set is not None:
                            zone_data["coolSet"] = temp_update.cool_set
                        break
            zone_status_list.append(zone_data)

        return await self._post_transfer(
            self._build_transfer_request(
                long_id,
                operationMode=mode.value,
                operationStatus=operation_status.value,
                zoneStatus=zone_status_list,
                tankStatus={"operationStatus": tank_operation_status.value},
            )
        )

    async def post_device_set_special_status(
        self,
        long_id: str,
        special_status: SpecialStatus | None,
        zones: list[ZoneTemperatureSetUpdate],
    ) -> PanasonicCommandResult:
        """Post device operation update."""
        data = {
            "status": [
                {
                    "deviceGuid": long_id,
                    "specialStatus": special_status.value if special_status else 0,
                    "zoneStatus": [
                        {
                            "zoneId": zone.zone_id,
                            "heatSet": zone.heat_set,
                            **(
                                {"coolSet": zone.cool_set}
                                if zone.cool_set is not None
                                else {}
                            ),
                        }
                        for zone in zones
                    ],
                }
            ]
        }

        response = await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}",
            ),
            json=data,
        )
        return await PanasonicCommandResult.from_response(response)

    async def post_device_zone_heat_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> PanasonicCommandResult:
        """Post device zone heat temperature."""
        return await self._post_device_zone_temperature(
            long_id, zone_id, temperature, "heatSet"
        )

    async def post_device_zone_cool_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> PanasonicCommandResult:
        """Post device zone cool temperature."""
        return await self._post_device_zone_temperature(
            long_id, zone_id, temperature, "coolSet"
        )

    async def _post_device_zone_temperature(
        self, long_id: str, zone_id: int, temperature: int, key: str
    ) -> PanasonicCommandResult:
        """Post device zone temperature."""
        return await self._post_transfer(
            self._build_transfer_request(
                long_id,
                zoneStatus=[
                    {
                        "zoneId": zone_id,
                        key: temperature,
                    }
                ],
            )
        )

    async def post_device_set_quiet_mode(
        self, long_id: str, mode: QuietMode
    ) -> PanasonicCommandResult:
        """Post quiet mode."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, quietMode=mode.value)
        )

    async def post_device_force_dhw(
        self, long_id: str, force_dhw: ForceDHW
    ) -> PanasonicCommandResult:
        """Post force DHW command."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, forceDHW=force_dhw.value)
        )

    async def post_device_force_heater(
        self, long_id: str, force_heater: ForceHeater
    ) -> PanasonicCommandResult:
        """Post force heater command."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, forceHeater=force_heater.value)
        )

    async def post_device_holiday_timer(
        self, long_id: str, holiday_timer: HolidayTimer
    ) -> PanasonicCommandResult:
        """Post holidayTimer command."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, holidayTimer=holiday_timer.value)
        )

    async def post_device_request_defrost(
        self, long_id: str
    ) -> PanasonicCommandResult:
        """Post forcedefrost command."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, forcedefrost=1)
        )

    async def post_device_set_powerful_time(
        self, long_id: str, powerful_time: PowerfulTime
    ) -> PanasonicCommandResult:
        """Post powerful time."""
        return await self._post_transfer(
            self._build_transfer_request(long_id, powerfulRequest=powerful_time.value)
        )

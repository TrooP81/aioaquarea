"""Data models for aioaquarea."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .const import PANASONIC

# These imports intentionally preserve the historical ``aioaquarea.data``
# public API after the enum/model split.
from .data_enums import (  # noqa: F401
    AirSwingAutoMode,
    AirSwingLR,
    AirSwingUD,
    DataMode,
    DeviceAction,
    DeviceDirection,
    DeviceModeStatus,
    EcoFunctionMode,
    EcoMode,
    EcoNaviMode,
    ExtendedOperationMode,
    FanSpeed,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    IAutoXMode,
    NanoeMode,
    OperationMode,
    OperationStatus,
    Power,
    PowerfulTime,
    PumpDuty,
    QuietMode,
    SensorMode,
    SpecialStatus,
    StatusDataMode,
    UpdateOperationMode,
    ZoneMode,
    ZoneSensor,
    ZoneType,
)
from .data_models import (  # noqa: F401
    DeviceInfo,
    DeviceOperationStatusUpdate,
    DeviceStatus,
    DeviceZoneInfo,
    DeviceZoneStatus,
    FaultError,
    OperationStatusUpdate,
    TankStatus,
    TemperatureModifiers,
    ZoneTemperatureSetUpdate,
)
from .statistics import Consumption, ConsumptionType
from .util import LimitedSizeDict, limit_range


class DeviceZone:
    """Device zone"""

    _info: DeviceZoneInfo
    _status: DeviceZoneStatus | None

    def __init__(self, info: DeviceZoneInfo, status: DeviceZoneStatus | None) -> None:
        self._info = info
        self._status = status

        if self.supports_special_status:
            eco_heat = self._status.eco_heat if self._status else None
            eco_cool = self._status.eco_cool if self._status else None
            comfort_heat = self._status.comfort_heat if self._status else None
            comfort_cool = self._status.comfort_cool if self._status else None

            self._temperature_modifiers = {
                SpecialStatus.ECO: TemperatureModifiers(eco_heat, eco_cool),
                SpecialStatus.COMFORT: TemperatureModifiers(comfort_heat, comfort_cool),
            }

    @property
    def zone_id(self) -> int:
        """Zone ID"""
        return self._info.zone_id

    @property
    def name(self) -> str:
        """Zone name"""
        return self._info.name

    @property
    def operation_status(self) -> OperationStatus:
        """Gets the zone operation status (ON/OFF)"""
        return self._status.operation_status if self._status else OperationStatus.OFF

    @property
    def temperature(self) -> int:
        """Gets the zone temperature"""
        return self._status.temperature if self._status else 0

    @property
    def cool_mode(self) -> bool:
        """Gets if the zone supports cool mode"""
        return self._info.cool_mode

    @property
    def type(self) -> ZoneType:
        """Gets the zone type"""
        return self._info.type

    @property
    def sensor_mode(self) -> ZoneSensor:
        """Gets the zone sensor mode"""
        return self._info.zone_sensor

    @property
    def heat_sensor_mode(self) -> SensorMode:
        """Gets the heat sensor mode"""
        return self._info.heat_sensor

    @property
    def cool_sensor_mode(self) -> SensorMode | None:
        """Gets the heat sensor mode"""
        return self._info.cool_sensor

    @property
    def cool_target_temperature(self) -> int | None:
        """Gets the target temperature for cool mode of the zone"""
        return self._status.cool_set if self._status else None

    @property
    def heat_target_temperature(self) -> int | None:
        """Gets the target temperature for heat mode of the zone"""
        return self._status.heat_set if self._status else None

    @property
    def cool_max(self) -> int | None:
        """Gets the maximum allowed temperature for cool mode of the zone"""
        return self._status.cool_max if self._status else None

    @property
    def cool_min(self) -> int | None:
        """Gets the minimum allowed temperature for cool mode of the zone"""
        return self._status.cool_min if self._status else None

    @property
    def heat_max(self) -> int | None:
        """Gets the maximum allowed temperature for heat mode of the zone"""
        return self._status.heat_max if self._status else None

    @property
    def heat_min(self) -> int | None:
        """Gets the minimum allowed temperature for heat mode of the zone"""
        return self._status.heat_min if self._status else None

    @property
    def supports_set_temperature(self) -> bool:
        """Gets if the zone supports setting the temperature"""
        return self.sensor_mode != ZoneSensor.EXTERNAL

    @property
    def supports_special_status(self) -> bool:
        """Gets if the zone supports special status"""
        return self.sensor_mode != ZoneSensor.EXTERNAL

    @property
    def eco(self) -> TemperatureModifiers:
        """Gets the eco temperature modifiers for the zone"""
        return self.temperature_modifiers[SpecialStatus.ECO]

    @property
    def comfort(self) -> TemperatureModifiers:
        """Gets the confort temperature modifiers for the zone"""
        return self.temperature_modifiers[SpecialStatus.COMFORT]

    @property
    def temperature_modifiers(self) -> dict[SpecialStatus, TemperatureModifiers]:
        """Gets the temperature modifiers for the zone"""
        return self._temperature_modifiers


class Tank(ABC):
    """Tank"""

    _status: TankStatus

    def __init__(self, tank_status: TankStatus, device: Device) -> None:
        self._status = tank_status
        self._device = device
        super().__init__()

    @property
    def operation_status(self) -> OperationStatus:
        """The operation status of the tank"""
        return self._status.operation_status

    @property
    def temperature(self) -> int:
        """The temperature of the tank"""
        return self._status.temperature

    @property
    def heat_max(self) -> int:
        """The maximum heat temperature of the tank"""
        return self._status.heat_max

    @property
    def heat_min(self) -> int:
        """The minimum heat temperature of the tank"""
        return self._status.heat_min

    @property
    def target_temperature(self) -> int:
        """The target temperature of the tank"""
        return self._status.heat_set

    async def set_target_temperature(self, value: int):
        """Sets the target temperature of the tank if supported"""
        if self.target_temperature != value and self.heat_min <= value <= self.heat_max:
            await self.__set_target_temperature__(value)

    @abstractmethod
    async def __set_target_temperature__(self, value: int) -> None:
        """Sets the target temperature of the tank if supported"""

    @abstractmethod
    async def __set_operation_status__(self, status: OperationStatus) -> None:
        """Set the operation status of the device"""

    async def turn_off(self) -> None:
        """Turn off the tank"""
        if self.operation_status == OperationStatus.ON:
            await self.__set_operation_status__(OperationStatus.OFF)

    async def turn_on(self) -> None:
        """Turn on the tank"""
        if self.operation_status == OperationStatus.OFF:
            # Check if device has any active zones
            await self.__set_operation_status__(OperationStatus.ON)


class Device(ABC):
    """Aquarea Device"""

    _zones: dict[int, DeviceZone] = {}

    def __init__(self, info: DeviceInfo, status: DeviceStatus) -> None:
        self._info = info  # Store the DeviceInfo object
        self._status = status
        self.manufacturer = (
            PANASONIC  # This can remain a direct assignment if it's constant
        )
        self._tank: Tank | None = None
        self._consumption: dict[datetime, Consumption] = LimitedSizeDict(5)
        self.__build_zones__(info.zones)  # Use info.zones directly

    def __build_zones__(self, zones_info: list[DeviceZoneInfo]) -> None:
        for zone in zones_info:
            zone_id = zone.zone_id
            # pylint: disable=cell-var-from-loop
            zone_status = next(
                filter(lambda z: z.zone_id == zone_id, self._status.zones), None
            )
            self._zones[zone_id] = DeviceZone(zone, zone_status)

    @abstractmethod
    async def refresh_data(self) -> None:
        """Refresh device data"""

    @property
    def mode(self) -> ExtendedOperationMode:
        """The operation mode of the device"""
        return self._status.operation_mode

    @property
    def temperature_outdoor(self) -> int:
        """The outdoor temperature"""
        return self._status.temperature_outdoor

    @property
    def is_on_error(self) -> bool:
        """True if the device is in an error state"""
        return len(self._status.fault_status) > 0

    @property
    def current_error(self) -> FaultError | None:
        """The current error of the device"""
        return self._status.fault_status[0] if self.is_on_error else None

    @property
    def operation_status(self) -> OperationStatus:
        """The operation status of the device"""
        return self._status.operation_status

    @property
    def device_id(self) -> str:
        return self._info.device_id

    @property
    def long_id(self) -> str:
        return self._info.long_id

    @property
    def device_name(self) -> str:
        return self._info.name

    @property
    def firmware_version(self) -> str:
        return self._info.firmware_version

    @property
    def model(self) -> str:
        return self._info.model

    @property
    def has_tank(self) -> bool:
        """True if the device has a tank"""
        return self._info.has_tank

    @property
    def tank(self) -> Tank | None:
        """The tank of the device"""
        if self.has_tank:
            return self._tank
        return None

    @property
    def pump_duty(self) -> int:
        """The pump duty of the device"""
        return self._status.pump_duty

    @property
    def current_direction(self) -> DeviceDirection:
        """The current direction of the device"""
        return DeviceDirection(self._status.direction)

    @property
    def current_action(self) -> DeviceAction:
        """The current action the device is performing"""
        if self.operation_status == OperationStatus.OFF:
            return DeviceAction.OFF

        direction = self.current_direction
        if direction == DeviceDirection.IDLE:
            return DeviceAction.IDLE

        if (
            self.has_tank
            and direction == DeviceDirection.WATER
            and self.tank.operation_status == OperationStatus.ON
        ):
            return DeviceAction.HEATING_WATER

        mode = self.mode
        if direction == DeviceDirection.PUMP and mode != ExtendedOperationMode.OFF:
            return (
                DeviceAction.HEATING
                if mode in (ExtendedOperationMode.HEAT, ExtendedOperationMode.AUTO_HEAT)
                else DeviceAction.COOLING
            )

        return DeviceAction.IDLE

    @property
    def zones(self) -> dict[int, DeviceZone]:
        """The zones of the device"""
        return self._zones

    @property
    def quiet_mode(self) -> QuietMode:
        """The quiet mode of the device"""
        return self._status.quiet_mode

    @property
    def force_dhw(self) -> ForceDHW:
        """The force DHW of the device"""
        return self._status.force_dhw

    @property
    def force_heater(self) -> ForceHeater:
        """The force heater of the device"""
        return self._status.force_heater

    @property
    def device_mode_status(self) -> DeviceModeStatus:
        """The mode of the device"""
        return self._status.device_status

    @property
    def holiday_timer(self) -> HolidayTimer:
        """Specifies if the holiday timer is enabled"""
        return self._status.holiday_timer

    @property
    def powerful_time(self) -> PowerfulTime:
        """Specifies if the powerful time is enabled and for how long"""
        return self._status.powerful_time

    @property
    def special_status(self) -> SpecialStatus | None:
        """Specifies if the device is in a special status"""
        return self._status.special_status

    def support_cooling(self, zone_id: int = 1) -> bool:
        """True if the device supports cooling in the given zone"""
        zone = self.zones.get(zone_id, None)
        return zone is not None and zone.cool_mode

    @property
    def support_special_status(self) -> bool:
        """True if the device supports special status"""
        return any(zone.supports_special_status for zone in self.zones.values())

    async def set_special_status(self, special_status: SpecialStatus | None) -> None:
        """Set the special status.
        :param special_status: Special status to set
        """

        if not self.support_special_status:
            raise Exception("Device does not support special status")

        if self.special_status == special_status:
            return

        zones: list[ZoneTemperatureSetUpdate] = [
            self.__calculate_zone_special_status_update__(zone, special_status)
            for zone in self.zones.values()
        ]

        await self.__set_special_status__(special_status, zones)

    @abstractmethod
    async def __set_special_status__(
        self,
        special_status: SpecialStatus | None,
        zones: list[ZoneTemperatureSetUpdate],
    ) -> None:
        """Set the special status.
        :param special_status: Special status to set
        :param zones: Zones to set the special status for
        """

    def __calculate_zone_special_status_update__(
        self, zone: DeviceZone, special_status: SpecialStatus | None
    ) -> ZoneTemperatureSetUpdate:
        """Calculate the zone temperature set update based on the special status.
        :param zone: The zone for which to calculate the update
        :param special_status: The special status to set
        :return: The zone temperature set update
        """

        current_status = self.special_status
        cool_set = zone.cool_target_temperature
        heat_set = zone.heat_target_temperature

        """If the zone is already on a special status, we need to revert the temperature to normal first"""
        if current_status is not None:
            modifiers = zone.temperature_modifiers[current_status]
            cool_set = (
                limit_range(cool_set - modifiers.cool, zone.cool_min, zone.cool_max)
                if cool_set is not None
                else None
            )
            heat_set = (
                limit_range(heat_set - modifiers.heat, zone.heat_min, zone.heat_max)
                if heat_set is not None
                else None
            )

        """If we're setting a special status, we need to apply the modifiers"""
        if special_status is not None:
            modifiers = zone.temperature_modifiers[special_status]
            cool_set = (
                limit_range(cool_set + modifiers.cool, zone.cool_min, zone.cool_max)
                if cool_set is not None
                else None
            )
            heat_set = (
                limit_range(heat_set + modifiers.heat, zone.heat_min, zone.heat_max)
                if heat_set is not None
                else None
            )

        return ZoneTemperatureSetUpdate(zone.zone_id, cool_set, heat_set)

    @abstractmethod
    async def __set_operation_status__(self, status: OperationStatus) -> None:
        """Set the operation status of the device"""

    async def turn_off(self) -> None:
        """Turn off the device"""
        if self.operation_status == OperationStatus.ON or self.is_on_error:
            await self.__set_operation_status__(OperationStatus.OFF)

    async def turn_on(self) -> None:
        """Turn on the device"""
        if self.operation_status == OperationStatus.OFF:
            await self.__set_operation_status__(OperationStatus.ON)

    @abstractmethod
    async def set_mode(
        self, mode: UpdateOperationMode, zone_id: int | None = None
    ) -> None:
        """Set the operation mode of the device. If the zone_id is provided,
         it'll try to affect only the given zone.
        Some devices don't support different modes per zone, so the specified mode (heat or cool)
         will affect the whole device.
        We will try however to turn the zone 'on' or 'off' if possible as part of the mode change.

        If we're turning the last active zone off, the device will be turned off completely,
        unless it has an active tank.

        :param mode: The mode to set
        :param zone_id: The zone id to set the mode for
        """

    @abstractmethod
    async def set_temperature(
        self, temperature: int, zone_id: int | None = None
    ) -> None:
        """Set the temperature of the zone provided for the current device mode (heat/cool).
        :param temperature: The temperature to set
        :param zone_id: The zone id to set the temperature for
        """

    @abstractmethod
    async def set_quiet_mode(self, mode: QuietMode) -> None:
        """Set the quiet mode.
        :param mode: Quiet mode to set
        """

    @abstractmethod
    async def get_and_refresh_consumption(
        self, date: datetime, consumption_type: ConsumptionType
    ) -> float | None:
        """Retrieves consumption data and asyncronously refreshes if necessary for the specified date and type.
        :param date: The date to get the consumption for
        :param consumption_type: The consumption type to get"""

    @abstractmethod
    def get_or_schedule_consumption(
        self, date: datetime, consumption_type: ConsumptionType
    ) -> float | None:
        """Gets available consumption data or schedules retrieval for the next refresh cycle.
        :param date: The date to get the consumption for
        :param consumption_type: The consumption type to get"""

    @abstractmethod
    async def set_force_dhw(self, force_dhw: ForceDHW) -> None:
        """Set the force dhw.
        :param force_dhw: Set the Force DHW mode if the device has a tank.
        """

    @abstractmethod
    async def set_force_heater(self, force_heater: ForceHeater) -> None:
        """Set the force heater configuration.
        :param force_heater: The force heater mode.
        """

    @abstractmethod
    async def request_defrost(self) -> None:
        """Request defrost"""

    @abstractmethod
    async def set_holiday_timer(self, holiday_timer: HolidayTimer) -> None:
        """Enables or disables the holiday timer mode.

        :param holiday_timer: The holiday timer option
        """

    @abstractmethod
    async def set_powerful_time(self, powerful_time: PowerfulTime) -> None:
        """Set the powerful time.

        :param powerful_time: Time to enable powerful mode
        """

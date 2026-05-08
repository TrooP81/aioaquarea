"""Dataclasses for aioaquarea data models."""

from __future__ import annotations

from dataclasses import dataclass

from .data_enums import (
    DeviceDirection,
    DeviceModeStatus,
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


@dataclass
class TemperatureModifiers:
    heat: int | None
    cool: int | None


@dataclass
class TankStatus:
    """Tank status"""

    operation_status: OperationStatus
    temperature: int
    heat_max: int
    heat_min: int
    heat_set: int


@dataclass
class FaultError:
    error_message: str
    error_code: str


@dataclass
class DeviceZoneInfo:
    """Device zone info"""

    zone_id: int
    name: str
    type: ZoneType
    cool_mode: bool
    zone_sensor: ZoneSensor
    heat_sensor: SensorMode
    cool_sensor: SensorMode | None


@dataclass
class DeviceZoneStatus:
    """Device zone status"""

    zone_id: int
    temperature: int
    operation_status: OperationStatus
    heat_max: int | None
    heat_min: int | None
    heat_set: int | None
    cool_max: int | None
    cool_min: int | None
    cool_set: int | None
    comfort_heat: int | None
    comfort_cool: int | None
    eco_heat: int | None
    eco_cool: int | None


@dataclass
class DeviceInfo:
    """Aquarea device info"""

    device_id: str
    name: str
    long_id: str
    mode: OperationMode
    has_tank: bool
    firmware_version: str
    model: str
    zones: list[DeviceZoneInfo]
    status_data_mode: StatusDataMode


@dataclass()
class DeviceStatus:
    """Device status

    Parameters
    ----------
    special_status : SpecialStatus  | None
        Current special status of the device. As of now it only supports one value at a time.
    """

    long_id: str
    operation_status: OperationStatus
    device_status: DeviceModeStatus
    temperature_outdoor: int
    operation_mode: ExtendedOperationMode
    fault_status: list[FaultError]
    direction: DeviceDirection
    pump_duty: PumpDuty
    tank_status: list[TankStatus]
    zones: list[DeviceZoneStatus]
    quiet_mode: QuietMode
    force_dhw: ForceDHW
    force_heater: ForceHeater
    holiday_timer: HolidayTimer
    powerful_time: PowerfulTime
    special_status: SpecialStatus | None


@dataclass
class ZoneTemperatureSetUpdate:
    zone_id: int
    cool_set: int | None
    heat_set: int | None


@dataclass
class DeviceOperationStatusUpdate:
    """Device operation status update"""

    # pylint: disable=invalid-name
    deviceGuid: str
    operationStatus: OperationStatus


@dataclass
class OperationStatusUpdate:
    """Operation status update for a lista of devices"""

    status: list[DeviceOperationStatusUpdate]

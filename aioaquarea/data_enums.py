"""Enum types for aioaquarea data models."""

from __future__ import annotations

from enum import Enum, IntEnum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - exercised by the Python 3.10 CI job
    from strenum import StrEnum


class ZoneSensor(StrEnum):
    """Zone sensor types"""

    EXTERNAL = "External"
    INTERNAL = "Internal"
    WATER_TEMPERATURE = "Water temperature"
    THERMISTOR = "Thermistor"


class SensorMode(StrEnum):
    """Sensor mode"""

    DIRECT = "Direct"
    COMPENSATION_CURVE = "Compensation curve"


class OperationMode(Enum):
    Auto = 0
    Dry = 1
    Cool = 2
    Heat = 3
    Fan = 4


class Power(Enum):
    Off = 0
    On = 1


class AirSwingUD(Enum):
    Auto = -1
    Up = 0
    UpMid = 3
    Mid = 2
    DownMid = 4
    Down = 1
    Swing = 5


class AirSwingLR(Enum):
    Auto = -1
    Left = 1
    LeftMid = 5
    Mid = 2
    RightMid = 4
    Right = 0
    Unavailable = 6


class EcoMode(Enum):
    Auto = 0
    Powerful = 1
    Quiet = 2


class AirSwingAutoMode(Enum):
    Disabled = 1
    Both = 0
    AirSwingLR = 3
    AirSwingUD = 2


class FanSpeed(Enum):
    Auto = 0
    Low = 1
    LowMid = 2
    Mid = 3
    HighMid = 4
    High = 5


class DataMode(Enum):
    Day = 0
    Week = 1
    Month = 2
    Year = 4


class NanoeMode(Enum):
    Unavailable = 0
    Off = 1
    On = 2
    ModeG = 3
    All = 4


class EcoNaviMode(Enum):
    Unavailable = 0
    Off = 1
    On = 2


class EcoFunctionMode(Enum):
    Unavailable = 0
    Off = 1
    On = 2


class ZoneMode(Enum):
    Off = 0
    On = 1


class IAutoXMode(Enum):
    Unavailable = 0
    Off = 1
    On = 2


class StatusDataMode(Enum):
    LIVE = 0
    CACHED = 1


class OperationStatus(IntEnum):
    """Operation status"""

    ON = 1
    OFF = 0
    UNKNOWN = 2


class ZoneType(StrEnum):
    ROOM = "Room"


class ExtendedOperationMode(IntEnum):
    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO_HEAT = 3
    AUTO_COOL = 4


class UpdateOperationMode(IntEnum):
    """Values used to change the operation mode of the device"""

    OFF = 0
    HEAT = 2
    COOL = 3
    AUTO = 8


class DeviceAction(IntEnum):
    """Device action"""

    OFF = 0
    IDLE = 1
    HEATING = 2
    COOLING = 3
    HEATING_WATER = 4


class DeviceDirection(IntEnum):
    """Device direction"""

    IDLE = 0
    PUMP = 1
    WATER = 2


class PumpDuty(IntEnum):
    """Pump duty"""

    OFF = 0
    ON = 1


class QuietMode(IntEnum):
    """Quiet mode level"""

    OFF = 0
    LEVEL1 = 1
    LEVEL2 = 2
    LEVEL3 = 3


class ForceDHW(IntEnum):
    """Force DHW"""

    OFF = 0
    ON = 1


class ForceHeater(IntEnum):
    """Force Heater"""

    OFF = 0
    ON = 1


class HolidayTimer(IntEnum):
    """Holiday Timer"""

    OFF = 0
    ON = 1


class DeviceModeStatus(IntEnum):
    """Device mode status"""

    NORMAL = 0
    DEFROST = 1


class PowerfulTime(IntEnum):
    """Powerful time"""

    OFF = 0
    ON_30MIN = 1
    ON_60MIN = 2
    ON_90MIN = 3


class SpecialStatus(IntEnum):
    """Special status"""

    ECO = 1
    COMFORT = 2

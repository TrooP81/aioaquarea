# aioaquarea library reference

Reference for the public API of the `aioaquarea` package (version `1.0.12`).
Everything listed here is importable from the top-level package unless noted.

- Python: `>=3.10`
- Runtime dependencies: `aiohttp>=3.14.3,<4`, `beautifulsoup4>=4.12,<5`,
  `soupsieve>=2.9,<3`, `StrEnum>=0.4.15` (Python 3.10 only)
- Typed package (`py.typed`).

For the wire-level Panasonic endpoints and payload fields, see
[panasonic-aquarea-api.md](panasonic-aquarea-api.md).

## Contents

- [Client](#client)
- [Device](#device)
- [DeviceZone](#devicezone)
- [Tank](#tank)
- [Enums](#enums)
- [Data classes](#data-classes)
- [PanasonicCommandResult](#panasoniccommandresult)
- [Consumption](#consumption)
- [Weekly timer](#weekly-timer)
- [Errors](#errors)
- [Constants](#constants)

---

## Client

`aioaquarea.Client` is an alias of `aioaquarea.core.AquareaClient`.

### Constructor

```python
Client(
    session: aiohttp.ClientSession,
    username: str | None = None,
    password: str | None = None,
    refresh_login: bool = True,
    logger: logging.Logger | None = None,
    environment: AquareaEnvironment = AquareaEnvironment.PRODUCTION,
    device_direct: bool = True,
    timezone: datetime.tzinfo = datetime.timezone.utc,
)
```

| Parameter | Description |
| --- | --- |
| `session` | Caller-owned `aiohttp.ClientSession` used for every request. |
| `username`, `password` | Panasonic ID credentials. Required when `environment` is `PRODUCTION`. |
| `refresh_login` | Exposed as `is_refresh_login_enabled`. |
| `logger` | Logger instance. Default: `logging.getLogger("aioaquarea")`. |
| `environment` | `AquareaEnvironment.PRODUCTION` or `AquareaEnvironment.DEMO`. |
| `device_direct` | Request live adaptor status (`deviceDirect=1`). Forced to `False` in `DEMO`. |
| `timezone` | Timezone used to build the `osTimezone` offset for consumption requests. |

Raises `ValueError` when `environment` is `PRODUCTION` and `username` or
`password` is empty.

### Properties

| Property | Type | Description |
| --- | --- | --- |
| `username` | `str \| None` | Configured username. |
| `password` | `str \| None` | Configured password. |
| `is_refresh_login_enabled` | `bool` | Value of `refresh_login`. |
| `token_expiration` | `datetime \| None` | Access-token expiry (UTC). |
| `is_logged` | `bool` | `True` when an access token exists and has not expired. A token without an expiry is treated as valid. |
| `logger` | `logging.Logger` | Active logger. |

### Session methods

| Method | Returns | Description |
| --- | --- | --- |
| `async login()` | `None` | Initializes the app version and runs the OAuth2/PKCE login. In `DEMO`, issues one unauthenticated request and sets a 1-day expiry. Serialized with `refresh_login()`. |
| `async refresh_login()` | `None` | Refreshes the access token with the stored refresh token. Serialized with `login()`. |
| `async close()` | `None` | Closes the `aiohttp.ClientSession` passed to the constructor. |

Tokens are held in memory only. There is no public API to inject or export
tokens.

### Device methods

All methods below are wrapped by `@auth_required`: they call `login()` first
when `is_logged` is `False`.

| Method | Returns |
| --- | --- |
| `async get_devices()` | `list[DeviceInfo]` |
| `async get_device_status(device_info: DeviceInfo, allow_cached_fallback: bool = True)` | `DeviceStatus` |
| `async get_device(device_info: DeviceInfo \| None = None, device_id: str \| None = None, consumption_refresh_interval: timedelta \| None = None, timezone: tzinfo = timezone.utc)` | `Device` |
| `async get_device_consumption(long_id: str, aggregation: DateType, date_input: str)` | `list[Consumption] \| None` |
| `async get_device_weekly_timer(device_id: str)` | `WeeklyTimerSettings \| None` |

`get_device()`:

- Requires `device_info` or `device_id`; raises `ValueError` if neither is
  given or `device_id` is not found.
- When `consumption_refresh_interval` is set, loads the current month's
  consumption before returning.
- `timezone` controls how consumption days are bucketed on the returned device.

`get_device_status()` with `allow_cached_fallback=False` raises
`DeviceUnavailableError` instead of returning cloud-cached data when the live
adaptor cannot be reached.

`get_device_consumption()`:

- `date_input` is `YYYYMMDD`; use `YYYYMM01` for `DateType.MONTH`.
- `DateType.DAY` → `dataMode 0`, `MONTH` → `1`, `YEAR` → `2`. `WEEK` falls back
  to `0`.
- Returns `None` when Panasonic returns no `historyDataList` or on `ApiError`.
- Re-raises `AuthenticationError`.

### Low-level command methods

These send one Panasonic command each and are used by `Device`/`Tank`. They
are `@auth_required` and return `PanasonicCommandResult`.

| Method | Command |
| --- | --- |
| `post_device_operation_status(long_device_id, new_operation_status)` | Device on/off |
| `post_device_operation_update(long_id, mode, zones, operation_status, tank_operation_status, zone_temperature_updates=None)` | Mode, zone on/off and targets, tank on/off |
| `post_device_tank_temperature(long_device_id, new_temperature)` | Tank target |
| `post_device_tank_operation_status(long_device_id, new_operation_status, zones)` | Tank on/off |
| `post_device_zone_heat_temperature(long_id, zone_id, temperature)` | Zone heat target |
| `post_device_zone_cool_temperature(long_id, zone_id, temperature)` | Zone cool target |
| `post_device_set_special_status(long_id, special_status, zones)` | Eco / comfort / normal |
| `post_device_set_quiet_mode(long_id, mode)` | Quiet mode |
| `post_device_force_dhw(long_id, force_dhw)` | Force DHW |
| `post_device_force_heater(long_id, force_heater)` | Auxiliary heater |
| `post_device_holiday_timer(long_id, holiday_timer)` | Holiday timer |
| `post_device_request_defrost(long_id)` | Forced defrost |
| `post_device_set_powerful_time(long_id, powerful_time)` | Powerful mode |

---

## Device

`aioaquarea.Device` is an abstract base class. `Client.get_device()` returns
the concrete `aioaquarea.entities.DeviceImpl`.

### Identity properties

| Property | Type | Description |
| --- | --- | --- |
| `device_id` | `str` | Device GUID from discovery. |
| `long_id` | `str` | Identifier sent as `gwid`. Discovery sets it to the same value as `device_id`. |
| `device_name` | `str` | User-assigned name. |
| `model` | `str` | Model string. |
| `firmware_version` | `str` | Firmware version. |
| `manufacturer` | `str` | Attribute, always `"Panasonic"`. |
| `has_tank` | `bool` | Device reports a DHW tank. |

### State properties

| Property | Type | Description |
| --- | --- | --- |
| `mode` | `ExtendedOperationMode` | Current operation mode. |
| `operation_status` | `OperationStatus` | Device on/off. |
| `operation_status_present` | `bool \| None` | Whether the last response contained `operationStatus`. |
| `operation_status_valid` | `bool \| None` | Whether `operationStatus` mapped to a known value. |
| `current_action` | `DeviceAction` | Derived action; see below. |
| `current_direction` | `DeviceDirection` | Compressor direction. |
| `pump_duty` | `int` | Pump duty. |
| `temperature_outdoor` | `int` | Outdoor sensor, °C. |
| `status_data_mode` | `StatusDataMode` | `LIVE` or `CACHED`. |
| `is_on_error` | `bool` | At least one fault is reported. |
| `current_error` | `FaultError \| None` | First reported fault. |
| `zones` | `dict[int, DeviceZone]` | Zones keyed by `zone_id`. |
| `tank` | `Tank \| None` | `None` when `has_tank` is `False` or no tank status was returned. |
| `quiet_mode` | `QuietMode` | |
| `force_dhw` | `ForceDHW` | |
| `force_heater` | `ForceHeater` | |
| `holiday_timer` | `HolidayTimer` | |
| `powerful_time` | `PowerfulTime` | |
| `device_mode_status` | `DeviceModeStatus` | `NORMAL` or `DEFROST`. |
| `special_status` | `SpecialStatus \| None` | `None` means normal. |
| `support_special_status` | `bool` | At least one zone supports special status. |
| `heat_max`, `cool_max` | `int \| None` | Zone 1 limits (`DeviceImpl` only). |

`current_action` resolution order:

1. `OFF` when `operation_status` is `OFF` and `operation_status_valid` is not
   `False`.
2. `IDLE` when `current_direction` is `IDLE`.
3. `HEATING_WATER` when direction is `WATER` and the tank is on.
4. `HEATING` or `COOLING` when direction is `PUMP` and `mode` is not `OFF`.
5. Otherwise `IDLE`.

### Methods

| Method | Returns | Behaviour |
| --- | --- | --- |
| `async refresh_data(allow_cached_fallback: bool = True)` | `None` | Reloads status, zones, and tank; refreshes consumption when an interval was set. |
| `async turn_on()` | `None` | Sends ON only when currently OFF. |
| `async turn_off()` | `None` | Sends OFF when ON or in error. |
| `async set_mode(mode: UpdateOperationMode, zone_id: int \| None = None)` | `None` | Sets the mode; with `zone_id`, only that zone is switched on/off. Device goes OFF only when every zone and the tank are off. |
| `async set_temperature(temperature: int, zone_id: int \| None = None)` | `None` | Sets the heat or cool target according to `mode`. Logs and returns without sending when `zone_id` is missing/unknown or the zone uses an external sensor. |
| `async set_special_status(special_status: SpecialStatus \| None)` | `None` | Recomputes zone targets from eco/comfort modifiers. Raises `Exception` when unsupported. No-op when unchanged. |
| `async set_quiet_mode(mode: QuietMode)` | `PanasonicCommandResult` | Always sends. |
| `async set_force_dhw(force_dhw: ForceDHW)` | `PanasonicCommandResult` | Always sends. |
| `async set_force_heater(force_heater: ForceHeater)` | `None` | Sends only when changed. |
| `async set_holiday_timer(holiday_timer: HolidayTimer)` | `None` | Sends only when changed. |
| `async set_powerful_time(powerful_time: PowerfulTime)` | `None` | Sends only when changed. |
| `async request_defrost()` | `None` | Sends only when not already defrosting. |
| `async get_weekly_timer()` | `WeeklyTimerSettings \| None` | Read-only. |
| `async get_and_refresh_consumption(date: datetime, consumption_type: ConsumptionType)` | `float \| None` | Refreshes if due, then returns the day's kWh. Raises `DataNotAvailableError` if the day is missing. |
| `get_or_schedule_consumption(date: datetime, consumption_type: ConsumptionType)` | `float \| None` | Synchronous. Returns cached kWh; raises `DataNotAvailableError` if the day is missing. |

Consumption is cached per day for the current month and refreshed at most
once per `consumption_refresh_interval`, or immediately when the month changes.

---

## DeviceZone

Returned via `Device.zones`. Read-only.

| Property | Type | Description |
| --- | --- | --- |
| `zone_id` | `int` | |
| `name` | `str` | |
| `type` | `ZoneType` | |
| `operation_status` | `OperationStatus` | `OFF` when no status was returned. |
| `temperature` | `int` | `0` when no status was returned. |
| `heat_target_temperature`, `cool_target_temperature` | `int \| None` | |
| `heat_min`, `heat_max`, `cool_min`, `cool_max` | `int \| None` | |
| `cool_mode` | `bool` | Zone supports cooling. |
| `sensor_mode` | `ZoneSensor` | |
| `heat_sensor_mode`, `cool_sensor_mode` | `SensorMode`, `SensorMode \| None` | |
| `supports_set_temperature` | `bool` | `False` for `ZoneSensor.EXTERNAL`. |
| `supports_special_status` | `bool` | `False` for `ZoneSensor.EXTERNAL`. |
| `eco`, `comfort` | `TemperatureModifiers` | Heat/cool offsets for each special status. |
| `temperature_modifiers` | `dict[SpecialStatus, TemperatureModifiers]` | Empty when unsupported. |

---

## Tank

`aioaquarea.Tank` is abstract; the device exposes `aioaquarea.entities.TankImpl`.

| Member | Type / Returns | Behaviour |
| --- | --- | --- |
| `operation_status` | `OperationStatus` | |
| `temperature` | `int` | Current tank temperature, °C. |
| `target_temperature` | `int` | Current `heatSet`. |
| `heat_min`, `heat_max` | `int` | Device-reported limits. |
| `async set_target_temperature(value: int)` | `None` | Sends only when `value` differs and `heat_min <= value <= heat_max`; otherwise silently does nothing. |
| `async turn_on()` | `None` | Sends only when OFF. |
| `async turn_off()` | `None` | Sends only when ON. |

---

## Enums

Integer values are Panasonic API values. Do not change them.

| Enum | Members |
| --- | --- |
| `AquareaEnvironment` | `PRODUCTION=0`, `DEMO=1` |
| `OperationStatus` | `OFF=0`, `ON=1`, `UNKNOWN=2` |
| `ExtendedOperationMode` (status) | `OFF=0`, `HEAT=1`, `COOL=2`, `AUTO_HEAT=3`, `AUTO_COOL=4` |
| `UpdateOperationMode` (commands) | `OFF=0`, `HEAT=2`, `COOL=3`, `AUTO=8` |
| `DeviceAction` | `OFF=0`, `IDLE=1`, `HEATING=2`, `COOLING=3`, `HEATING_WATER=4` |
| `DeviceDirection` | `IDLE=0`, `PUMP=1`, `WATER=2` |
| `DeviceModeStatus` | `NORMAL=0`, `DEFROST=1` |
| `PumpDuty` | `OFF=0`, `ON=1` |
| `QuietMode` | `OFF=0`, `LEVEL1=1`, `LEVEL2=2`, `LEVEL3=3` |
| `ForceDHW` | `OFF=0`, `ON=1` |
| `ForceHeater` | `OFF=0`, `ON=1` |
| `HolidayTimer` | `OFF=0`, `ON=1` |
| `PowerfulTime` | `OFF=0`, `ON_30MIN=1`, `ON_60MIN=2`, `ON_90MIN=3` |
| `SpecialStatus` | `ECO=1`, `COMFORT=2` (normal is `None`) |
| `DayOfWeek` | `MONDAY=1` … `SUNDAY=7` |
| `DateType` | `DAY="date"`, `WEEK="week"`, `MONTH="month"`, `YEAR="year"` |
| `ConsumptionType` | `HEAT="Heat"`, `COOL="AC"`, `WATER_TANK="HW"`, `TOTAL="Consume"` |
| `AuthenticationErrorCodes` | `SESSION_CLOSED="1001-0001"`, `INVALID_USERNAME_OR_PASSWORD="1001-1401"`, `INVALID_CREDENTIALS="1000-1401"`, `LOGGED_OUT_SYSTEM_ERROR="1000-0999"`, `API_ERROR="API_ERROR"`, `TOKEN_EXPIRED="TOKEN_EXPIRED"` |

Available from `aioaquarea.data` but not re-exported at the top level:
`StatusDataMode` (`LIVE=0`, `CACHED=1`), `ZoneSensor`, `SensorMode`,
`ZoneType`, `OperationMode`, and legacy air-conditioner enums.

---

## Data classes

Exported: `DeviceInfo`, `DeviceStatus`. Also available from `aioaquarea.data`:
`DeviceZoneInfo`, `DeviceZoneStatus`, `TankStatus`, `FaultError`,
`TemperatureModifiers`, `ZoneTemperatureSetUpdate`.

`DeviceInfo` fields: `device_id`, `name`, `long_id`, `mode`, `has_tank`,
`firmware_version`, `model`, `zones: list[DeviceZoneInfo]`, `status_data_mode`.

`ZoneTemperatureSetUpdate(zone_id, cool_set, heat_set)` is the payload element
for special-status and mode updates.

---

## PanasonicCommandResult

Frozen dataclass describing a Panasonic write response.

| Field | Type | Source |
| --- | --- | --- |
| `http_status` | `int \| None` | HTTP status |
| `response_code` | `str \| int \| None` | `code` or `resultCode` in the JSON body |
| `request_id` | `str \| None` | `requestId`, `requestID`, or `request_id` |

| Method | Returns |
| --- | --- |
| `classmethod async from_response(response: aiohttp.ClientResponse)` | `PanasonicCommandResult` |
| `audit_fields()` | `dict` with non-`None` values under `panasonic_http_status`, `panasonic_response_code`, `panasonic_request_id` |

---

## Consumption

`aioaquarea.Consumption` wraps one `historyDataList` entry.

| Property | Type | Unit |
| --- | --- | --- |
| `data_time` | `str \| None` | `YYYYMMDD` |
| `heat_consumption` | `float \| None` | kWh |
| `cool_consumption` | `float \| None` | kWh |
| `tank_consumption` | `float \| None` | kWh |
| `total_consumption` | `float \| None` | kWh |
| `heat_cost`, `cool_cost`, `tank_cost` | `float \| None` | Panasonic-reported cost |
| `outdoor_temp` | `float \| None` | °C |

---

## Weekly timer

Weekly timer support is **read-only**. No write method exists.

`WeeklyTimerSettings` (frozen):

| Member | Type / Returns |
| --- | --- |
| `enabled` | `bool` |
| `slots` | `tuple[WeeklyTimerSlot, ...]` |
| `active_slots(at: datetime, timezone: str \| ZoneInfo)` | `tuple[WeeklyTimerSlot, ...]` |

`active_slots()` returns `()` when the timer is disabled, raises `ValueError`
for a naive `at`, and handles slots that span midnight. A slot with
`start == end` never matches.

`WeeklyTimerSlot` (frozen): `day: DayOfWeek`, `zone_id: int`,
`start: time`, `end: time`, `heat_set: float | None = None`,
`cool_set: float | None = None`, `enabled: bool = True`.

`get_device_weekly_timer()` returns `None` when the request fails, the
response is not JSON, or it contains no `schedule` list. Malformed slots and
slots with `zone_id < 1` are dropped.

---

## Errors

```text
Exception
├── ClientError
│   ├── RequestFailedError(response)          .response
│   │   └── DeviceUnavailableError(device_id, reason=None)   .device_id, .reason
│   ├── ApiError(error_code, error_message)   .error_code, .error_message
│   │   └── AuthenticationError
│   └── InvalidData(data)                     .data   (not exported)
└── DataNotAvailableError
```

| Error | Raised when |
| --- | --- |
| `RequestFailedError` | HTTP failure, including a non-JSON response with status `>= 400`. |
| `DeviceUnavailableError` | Live status was required and the Panasonic adaptor is unreachable. |
| `ApiError` | Panasonic returned an error body. |
| `AuthenticationError` | Login, refresh, or token errors. Propagates from `get_device_consumption()`. |
| `DataNotAvailableError` | Requested consumption day is not cached. Not a `ClientError`. |

---

## Constants

`aioaquarea.const`:

| Name | Value |
| --- | --- |
| `AQUAREA_SERVICE_BASE` | `https://accsmart.panasonic.com/` |
| `AQUAREA_SERVICE_DEMO_BASE` | `https://accsmart.panasonic.com/` (same host as production) |

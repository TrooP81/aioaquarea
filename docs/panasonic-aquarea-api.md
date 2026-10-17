# Panasonic Aquarea Smart Cloud API map

This project uses Panasonic's private application API. Panasonic does not
publish a supported third-party contract, so every command must remain behind
tests, rate limits, live-status checks, and an explicit application policy.

No command in this document was sent to the production heat pump while the API
was mapped. The payloads come from the local `aioaquarea` implementation and
were cross-checked against the maintained Home Assistant integration.

## Transport

Most current commands are wrapped in a request to:

- outer request: `POST remote/v1/app/common/transfer`
- inner API: `/remote/v1/api/devices`
- inner method: `POST`
- inner body: `{"gwid": "<device id>", ...command fields}`

Status uses the same transfer endpoint with an inner `GET`:

- live adaptor: `/remote/v1/api/devices?gwid=<id>&deviceDirect=1`
- cloud cache: `/remote/v1/api/devices?gwid=<id>&deviceDirect=0`

Device discovery reads `GET https://accsmart.panasonic.com/device/group`.
Consumption uses the transfer endpoint with inner API `/remote/v1/api/consumption`.

## Read endpoints

| Function | Outer request | Inner API / method | Library method |
| --- | --- | --- | --- |
| Device discovery | `GET https://accsmart.panasonic.com/device/group` | — | `Client.get_devices()` |
| Live status | transfer | `/remote/v1/api/devices?gwid=<id>&deviceDirect=1`, `GET` | `Client.get_device_status()` |
| Cached status | transfer | `/remote/v1/api/devices?gwid=<id>&deviceDirect=0`, `GET` | `Client.get_device_status()` fallback |
| Consumption | transfer | `/remote/v1/api/consumption`, `POST` with `gwid`, `dataMode` (0 day, 1 month, 2 year), `date`, `osTimezone` | `Client.get_device_consumption()` |
| Weekly timer | transfer | `/remote/v1/api/weeklytimer`, `GET` with `gwid` | `Client.get_device_weekly_timer()` |

The weekly timer response is parsed from `schedule[]` entries with
`dayOfWeek` (1 Monday … 7 Sunday), `zoneId`, `startTime`, `endTime`,
`heatSet`, `coolSet`, and `enabled`. Weekly timer writes are intentionally not
implemented.

## Mapped command fields

| Function | Command field | Values | Optimizer policy |
| --- | --- | --- | --- |
| Device/mode update | `operationMode`, `operationStatus` | mode and on/off enums | Automated with freshness guardrails |
| Zone enable and target | `zoneStatus[]` | `zoneId`, `operationStatus`, `heatSet`, `coolSet` | Heat target automated; cooling not planned |
| Tank enable and target | `tankStatus` | `operationStatus`, `heatSet` | Automated for DHW planning |
| Quiet mode | `quietMode` | 0 off, 1-3 levels | Level 1/off automated |
| Force DHW | `forceDHW` | 0 off, 1 on | Automated for bounded DHW windows |
| Powerful mode | `powerfulRequest` | 0 off, 1 30 min, 2 60 min, 3 90 min | Wrapper only; no automatic plan yet |
| Auxiliary heater | `forceHeater` | 0 off, 1 on | Wrapper only; never enable automatically |
| Holiday timer | `holidayTimer` | 0 off, 1 on | Wrapper only; requires occupancy intent |
| Forced defrost | `forcedefrost` | 1 request | Wrapper only; suppress if already defrosting |
| Special status | `specialStatus`, `zoneStatus[]` | 0 normal, 1 eco, 2 comfort; zone `heatSet`/`coolSet` | Not automated |

`specialStatus` (normal/eco/comfort) still uses the older direct device
endpoint: `POST remote/v1/api/devices/<long id>` with body
`{"status": [{"deviceGuid", "specialStatus", "zoneStatus": [...]}]}`.
Zones with an external sensor are omitted, and unset `heatSet`/`coolSet` values
are not sent. Its state is parsed when Panasonic returns it, but some devices or
cached responses may omit it.

## Safety classification

- `powerfulRequest` is time-bounded but can increase demand. It needs a measured
  COP/cost policy before automatic scheduling.
- `forceHeater` can engage direct electric backup heat. Automatic ON is excluded
  because it can materially increase electricity use.
- `holidayTimer` encodes user occupancy intent. It must not be inferred from
  price or weather alone.
- `forcedefrost` is an equipment intervention. It remains explicit and relies on
  the device entity's already-defrosting guard.
- every new wrapper command consumes the shared Panasonic write budget.

## Runtime capability discovery

`GET /api/panasonic/capabilities` reports the mapped command surface, observed
tank/zones, safety policy, and current command availability. It reads the last
poller-owned live observation and poller heartbeat from the database; it never
opens a second Panasonic session or spends the cloud request budget.

The response also includes an `adapter` block sourced from the poller heartbeat:
`status` (`available`, `unavailable`, `backoff`, or `unknown`), consecutive
failures, observation age, and the absolute next retry time. Provider error text
is reduced to stable reason codes such as `adaptor_communication_failed` before
it reaches the shared database state or API. A fresh outage state changes the
availability reason to `adapter_unavailable` or `adapter_backoff`; an old or
malformed state is exposed as stale and cannot override a fresh live observation.

Operational alerts use the same projected state. A known adaptor outage replaces
the generic stale-device warning with one actionable alert while automatic
commands remain paused. Regular poller heartbeats preserve this diagnostic state,
and the next successful live status refresh resets it to `available`.

Every wrapper write also requires a live adaptor status no older than 60
seconds. A stale object is refreshed first, and a cloud-cached response blocks
the command before a write-rate-limit token is consumed.

Tank targets use the library's tank entity (`device.tank.set_target_temperature`)
rather than a non-existent device-level method. The wrapper requires live
device-reported `heat_min` and `heat_max` bounds, rejects targets outside that
range before consuming the write budget, and skips a write when the live target
already matches. The observed range is exposed under the command's
`constraints.observed_range` capability field.

## Primary implementation references

- `aioaquarea/device_control.py` and `aioaquarea/entities.py` in this repository
- [maintained Home Assistant Aquarea select entities](https://github.com/wpatrik14/home-assistant-aquarea/blob/main/custom_components/aquarea/select.py)
- [maintained Home Assistant Aquarea switches](https://github.com/wpatrik14/home-assistant-aquarea/blob/main/custom_components/aquarea/switch.py)
- [maintained Home Assistant Aquarea defrost button](https://github.com/wpatrik14/home-assistant-aquarea/blob/main/custom_components/aquarea/button.py)

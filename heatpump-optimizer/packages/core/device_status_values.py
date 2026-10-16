"""Detached status values shared by planning and status-consuming helpers."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol


class DeviceStatusValues(Protocol):
    """Structural contract for the status evidence consumed by planners."""

    @property
    def device_id(self) -> str: ...

    @property
    def ts(self) -> dt.datetime: ...

    @property
    def outdoor_temp(self) -> float | None: ...

    @property
    def heat_pump_outdoor_temp(self) -> float | None: ...

    @property
    def holiday_mode(self) -> int | None: ...

    @property
    def tank_temp(self) -> float | None: ...

    @property
    def tank_target_temp(self) -> int | None: ...

    @property
    def tank_heat_min(self) -> int | None: ...

    @property
    def tank_heat_max(self) -> int | None: ...

    @property
    def zone1_temp(self) -> float | None: ...

    @property
    def zone1_target_temp(self) -> float | None: ...

    @property
    def zone1_heat_min(self) -> int | None: ...

    @property
    def zone1_heat_max(self) -> int | None: ...

    @property
    def quiet_mode(self) -> int | None: ...

    @property
    def special_status(self) -> int | None: ...

    @property
    def special_status_supported(self) -> bool | None: ...

    @property
    def operation_status(self) -> int | None: ...

    @property
    def tank_operation_status(self) -> int | None: ...

    @property
    def zone1_operation_status(self) -> int | None: ...

    @property
    def device_action(self) -> str | None: ...

    @property
    def mode(self) -> str | None: ...

    @property
    def force_dhw(self) -> int | None: ...

    @property
    def native_policy_provenance(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class DeviceStatusSnapshot:
    """Status captured before a read-only rollback expires the source row."""

    device_id: str
    ts: dt.datetime
    outdoor_temp: float | None
    heat_pump_outdoor_temp: float | None
    holiday_mode: int | None
    tank_temp: float | None
    tank_target_temp: int | None
    tank_heat_min: int | None
    tank_heat_max: int | None
    zone1_temp: float | None
    zone1_target_temp: float | None
    zone1_heat_min: int | None
    zone1_heat_max: int | None
    quiet_mode: int | None
    special_status: int | None
    special_status_supported: bool | None
    operation_status: int | None
    tank_operation_status: int | None
    zone1_operation_status: int | None
    device_action: str | None
    mode: str | None
    force_dhw: int | None
    native_policy_provenance: dict[str, Any] | None

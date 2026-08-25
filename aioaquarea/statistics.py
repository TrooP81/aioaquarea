"""Statistics models for Aquarea"""

from __future__ import annotations

import math

try:
    from enum import StrEnum
except ImportError:
    from strenum import StrEnum


class DateType(StrEnum):
    """Date types"""

    DAY = "date"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class AggregationType(StrEnum):
    """Aggregation type"""

    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


class ConsumptionType(StrEnum):
    """Data type"""

    HEAT = "Heat"
    COOL = "AC"
    WATER_TANK = "HW"
    TOTAL = "Consume"


def _sanitize_float(value: object) -> float | None:
    """Return a finite numeric API value, or ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class Consumption:
    """Consumption"""

    def __init__(self, data: dict[str, object]):
        self._data = data
        self._heat_consumption = _sanitize_float(data.get("heatConsumption"))
        self._cool_consumption = _sanitize_float(data.get("coolConsumption"))
        self._tank_consumption = _sanitize_float(data.get("tankConsumption"))
        self._heat_cost = _sanitize_float(data.get("heatCost"))
        self._cool_cost = _sanitize_float(data.get("coolCost"))
        self._tank_cost = _sanitize_float(data.get("tankCost"))
        self._data_time = data.get("dataTime")
        self._outdoor_temp = _sanitize_float(data.get("outdoorTemp"))

    @property
    def heat_consumption(self) -> float | None:
        """Heat consumption in kWh"""
        return self._heat_consumption

    @property
    def cool_consumption(self) -> float | None:
        """Cool consumption in kWh"""
        return self._cool_consumption

    @property
    def tank_consumption(self) -> float | None:
        """Tank consumption in kWh"""
        return self._tank_consumption

    @property
    def heat_cost(self) -> float | None:
        """Heat cost in configured currency"""
        return self._heat_cost

    @property
    def cool_cost(self) -> float | None:
        """Cool cost in configured currency"""
        return self._cool_cost

    @property
    def tank_cost(self) -> float | None:
        """Tank cost in configured currency"""
        return self._tank_cost

    @property
    def data_time(self) -> str | None:
        """Time of the data point"""
        return self._data_time

    @property
    def outdoor_temp(self) -> float | None:
        """Outdoor temperature"""
        return self._outdoor_temp

    @property
    def total_consumption(self) -> float | None:
        """Total consumption in kWh (sum of heat, cool, and tank consumption)"""
        components = (
            self._heat_consumption,
            self._cool_consumption,
            self._tank_consumption,
        )
        if all(component is None for component in components):
            return None
        return sum(component for component in components if component is not None)

    @property
    def raw_data(self) -> dict[str, object]:
        """Raw data from the API response"""
        return self._data

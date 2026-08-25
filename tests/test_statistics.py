import pytest

from aioaquarea.statistics import Consumption


def test_consumption_coerces_finite_numeric_strings() -> None:
    consumption = Consumption(
        {
            "heatConsumption": "10.5",
            "coolConsumption": 2,
            "tankConsumption": 0,
            "heatCost": "3.25",
            "outdoorTemp": "-4.5",
        }
    )

    assert consumption.heat_consumption == 10.5
    assert consumption.cool_consumption == 2.0
    assert consumption.tank_consumption == 0.0
    assert consumption.heat_cost == 3.25
    assert consumption.outdoor_temp == -4.5
    assert consumption.total_consumption == 12.5


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "NaN", "invalid", [], True],
)
def test_consumption_rejects_non_finite_and_invalid_values(value: object) -> None:
    consumption = Consumption({"heatConsumption": value})

    assert consumption.heat_consumption is None
    assert consumption.total_consumption is None


def test_consumption_preserves_valid_zero_total() -> None:
    consumption = Consumption(
        {"heatConsumption": 0, "coolConsumption": 0, "tankConsumption": 0}
    )

    assert consumption.total_consumption == 0.0


def test_consumption_distinguishes_missing_from_partial_data() -> None:
    assert Consumption({}).total_consumption is None
    assert Consumption({"tankConsumption": 1.25}).total_consumption == 1.25


def test_consumption_preserves_raw_response_for_diagnostics() -> None:
    raw = {"heatConsumption": "NaN"}

    assert Consumption(raw).raw_data is raw

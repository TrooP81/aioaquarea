from aioaquarea import data_enums, data_models
from aioaquarea.data import DeviceInfo, OperationStatus, ZoneTemperatureSetUpdate


def test_data_module_reexports_split_types():
    assert DeviceInfo is data_models.DeviceInfo
    assert OperationStatus is data_enums.OperationStatus
    assert ZoneTemperatureSetUpdate is data_models.ZoneTemperatureSetUpdate

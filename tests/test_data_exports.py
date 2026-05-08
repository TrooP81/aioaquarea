from aioaquarea.data import DeviceInfo, OperationStatus, ZoneTemperatureSetUpdate
from aioaquarea.data_enums import OperationStatus as SplitOperationStatus
from aioaquarea.data_models import DeviceInfo as SplitDeviceInfo
from aioaquarea.data_models import ZoneTemperatureSetUpdate as SplitZoneTemperatureSetUpdate


def test_data_module_reexports_split_types():
    assert DeviceInfo is SplitDeviceInfo
    assert OperationStatus is SplitOperationStatus
    assert ZoneTemperatureSetUpdate is SplitZoneTemperatureSetUpdate

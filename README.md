Aioaquarea
===================

Asynchronous library to control Panasonic Aquarea devices

## Requirements

- Python >= 3.10
- `aiohttp`, `beautifulsoup4`, `soupsieve` (and `StrEnum` on Python 3.10), installed automatically

## Documentation

- [Library reference](docs/library-reference.md) – client, device, tank, enums, errors
- [Panasonic Aquarea API map](docs/panasonic-aquarea-api.md) – endpoints and command payload fields
- [Dependency management](docs/dependency-management.md) – lock files, audits, and regeneration
- [Heat Pump Optimizer](heatpump-optimizer/README.md) – cost-optimizing controller built on this library

## Usage
The library supports the production environment of the Panasonic Aquarea Smart Cloud API and also the Demo environment. One of the main usages of this library is to integrate the Panasonic Aquarea Smart Cloud API with Home Assistant via [home-assistant-aquarea](https://github.com/cjaliaga/home-assistant-aquarea)

Here is a simple example of how to use the library via getting a device object to interact with it:

```python
from aioaquarea import (
    Client,
    AquareaEnvironment,
    UpdateOperationMode
)

import aiohttp
import asyncio
import logging
from datetime import timedelta

async def main():
    async with aiohttp.ClientSession() as session:
        client = Client(
            username="USERNAME",
            password="PASSWORD",
            session=session,
            device_direct=True,
            refresh_login=True,
            environment=AquareaEnvironment.PRODUCTION,
        )

        # The library is designed to retrieve a device object and interact with it:
        devices = await client.get_devices()

        # Picking the first device associated with the account:
        device_info = devices[0]

        device = await client.get_device(
            device_info=device_info, consumption_refresh_interval=timedelta(minutes=1)
        )

        # Or the device can also be retrieved by its long id if we know it:
        device = await client.get_device(
            device_id="LONG ID", consumption_refresh_interval=timedelta(minutes=1)
        )

        # Then we can interact with the device:
        await device.set_mode(UpdateOperationMode.HEAT)

        # The device can automatically refresh its data:
        await device.refresh_data()
```

Commands that would not change device state (for example setting the current
tank target again) are skipped. See the [library reference](docs/library-reference.md)
for which methods skip unchanged values and which always send.

## Development

```bash
python -m pip install -e ".[dev]"

python -m pytest tests -q
python -m black --check aioaquarea tests
python -m isort --check-only aioaquarea tests
python -m pylint --errors-only aioaquarea
```

CI (`.github/workflows/library-checks.yml`) runs the tests on Python 3.10–3.13,
plus lint, a package build with `twine check`, and `pip-audit`.

## Acknowledgements

Big thanks to [ronhks](https://github.com/ronhks) for his awesome work on the [Panasonic Aquaera Smart Cloud integration with MQTT](https://github.com/ronhks/panasonic-aquarea-smart-cloud-mqtt).
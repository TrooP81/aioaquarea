from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from packages.api.routers import settings


@pytest.mark.asyncio
async def test_update_settings_returns_bad_request_for_bulk_validation_error():
    with patch.object(
        settings, "set_settings_bulk", new=AsyncMock(side_effect=ValueError("invalid batch"))
    ):
        with pytest.raises(HTTPException) as exc_info:
            await settings.update_settings(
                settings.SettingsUpdate(settings={"price_provider": "manual"})
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "invalid batch"

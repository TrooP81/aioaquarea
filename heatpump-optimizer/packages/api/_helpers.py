"""Shared API helpers."""

from __future__ import annotations

from packages.core.config import settings
from packages.core.settings_service import get_string_setting


async def get_price_area() -> str:
    """Return the price-record area for the currently configured provider."""
    provider = await get_string_setting("price_provider")
    if provider == "entsoe":
        return (await get_string_setting("entsoe_area")) or settings.entsoe_area
    if provider == "manual":
        return "manual"
    return "tibber"

"""Shared API helpers."""

from __future__ import annotations

from packages.core.pricing import get_active_price_area


async def get_price_area() -> str:
    """Return the price-record area for the currently configured provider."""
    return await get_active_price_area()

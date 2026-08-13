"""Price-source context shared by polling, planning and display code."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import desc, select

from packages.core.config import settings
from packages.core.database import get_session
from packages.core.models import PriceRecord
from packages.core.settings_service import get_string_setting


@dataclass(frozen=True, slots=True)
class PriceContext:
    area: str
    currency: str
    source: str


async def get_active_price_area() -> str:
    provider = await get_string_setting("price_provider")
    if provider == "entsoe":
        return (await get_string_setting("entsoe_area")) or settings.entsoe_area
    if provider == "manual":
        return "manual"
    return "tibber"


async def get_active_price_context() -> PriceContext:
    """Return source currency for the selected price feed.

    Rows created before source provenance was recorded are deliberately treated
    as EUR. The next poll replaces them with a source-qualified row.
    """
    area = await get_active_price_area()
    async with get_session() as session:
        row = (
            await session.execute(
                select(PriceRecord.price_currency, PriceRecord.price_source)
                .where(PriceRecord.area == area)
                .order_by(desc(PriceRecord.ts))
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        configured = await get_string_setting("currency")
        return PriceContext(area=area, currency=(configured or "EUR").upper(), source="unavailable")
    return PriceContext(
        area=area,
        currency=(row.price_currency or "EUR").upper(),
        source=row.price_source or "legacy",
    )

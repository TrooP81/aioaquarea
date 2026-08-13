"""Explain price uncertainty and the stability policy of an immutable plan."""

from __future__ import annotations

from collections.abc import Iterable


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def price_risk_summary(
    price_forecast: Iterable[dict[str, object]],
    input_quality: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a transparent price-volatility and replanning explanation."""

    values: list[float] = []
    for point in price_forecast:
        value = point.get("price_eur_per_kwh")
        if isinstance(value, (int, float)):
            values.append(float(value))
    limited = bool((input_quality or {}).get("price_horizon_limited"))
    reoptimizes = bool((input_quality or {}).get("reoptimization_when_prices_extend"))
    if not values:
        return {
            "status": "unavailable",
            "level": "unknown",
            "note": "No saved hourly price values are available for this plan.",
        }

    median = _percentile(values, 0.5)
    p10 = _percentile(values, 0.1)
    p90 = _percentile(values, 0.9)
    spread = p90 - p10
    relative_spread = spread / max(abs(median), 0.01)
    level = "high" if relative_spread >= 1.0 else "moderate" if relative_spread >= 0.4 else "low"
    return {
        "status": "partial_prices" if limited else "published_prices",
        "level": level,
        "hours": len(values),
        "median_price": round(median, 5),
        "p10_price": round(p10, 5),
        "p90_price": round(p90, 5),
        "spread": round(spread, 5),
        "near_term_policy": "Actions in the next two hours are kept stable unless a safety condition changes.",
        "future_policy": (
            "Later actions are regenerated when newly published prices materially change the plan."
            if reoptimizes or limited
            else "Later actions may be regenerated only when a material input change is detected."
        ),
        "note": (
            "Only published market prices were used; the plan will refresh when the remaining price horizon becomes available."
            if limited
            else "Price risk describes the spread of published hourly prices, not a retail-price forecast."
        ),
    }

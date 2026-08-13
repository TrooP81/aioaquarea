from packages.core.plan_risk import price_risk_summary


def test_price_risk_explains_limited_price_horizon_and_stable_actions():
    result = price_risk_summary(
        [
            {"price_eur_per_kwh": 0.10},
            {"price_eur_per_kwh": 0.20},
            {"price_eur_per_kwh": 0.80},
        ],
        {"price_horizon_limited": True, "reoptimization_when_prices_extend": True},
    )

    assert result["status"] == "partial_prices"
    assert result["level"] == "high"
    assert result["hours"] == 3
    assert "next two hours" in result["near_term_policy"]
    assert "newly published prices" in result["future_policy"]


def test_price_risk_handles_missing_saved_prices():
    result = price_risk_summary([])

    assert result == {
        "status": "unavailable",
        "level": "unknown",
        "note": "No saved hourly price values are available for this plan.",
    }

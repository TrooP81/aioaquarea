import { expect, test } from "./fixtures";

test("missing telemetry is unavailable, not measured zero consumption", async ({ page }) => {
    await page.route("**/api/dashboard", (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
            current_status: null,
            current_status_fresh: false,
            current_price: null,
            today_kwh: 0,
            today_telemetry_status: "unavailable",
            today_cost_eur: null,
            today_cost_currency: "SEK",
            today_cost_priced_kwh: 0,
            today_cost_unpriced_kwh: 0,
            today_cost_priced_amount: null,
            today_cost_coverage_pct: null,
            today_cost_complete: false,
            active_plan: null,
        }),
    }));
    await page.goto("/");
    const consumption = page.locator(".card").filter({ hasText: "Today's Consumption" });
    await expect(consumption).toBeVisible();
    await expect(consumption).toContainText(/unavailable|missing telemetry/i);
    await expect(consumption.locator(".card-value")).not.toHaveText(/0(?:\.0)? kWh/);
});

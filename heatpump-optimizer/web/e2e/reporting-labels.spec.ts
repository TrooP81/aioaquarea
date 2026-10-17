import { test, expect } from "./fixtures";
import { forecastDisplayStatusLabel, reasonLabel, LAYER_LABELS } from "../lib/constants";

// These are the same mappings used by the mocked dashboard/status views.
test("reporting labels preserve planner compatibility and unknown fallback", async () => {
    expect(LAYER_LABELS.milp_v1).toBe("Optimized");
    expect(LAYER_LABELS["milp_v1+ml"]).toBe("Optimized + ML");
    expect(LAYER_LABELS.milp_v2).toBe("Optimized");
    expect(LAYER_LABELS["milp_v2+ml"]).toBe("Optimized + ML");
    expect(LAYER_LABELS.milp_preferred).toBe("Optimized");
    expect(reasonLabel("reference_sensor")).toBe("Reference sensor");
    expect(reasonLabel("forecast_unavailable_or_legacy")).toBe("Forecast unavailable or legacy");
    expect(reasonLabel("future_reason")).toContain("future_reason");
    expect(forecastDisplayStatusLabel("future_status")).toContain("future_status");
});

for (const [code, label] of [["degraded", "Degraded forecast"], ["future_status", "Unrecognized status (future_status)"]]) {
    test(`dashboard forecast label: ${code}`, async ({ page }) => {
        await page.route("**/api/thermal/indoor-forecast?*", (route) => route.fulfill({
            contentType: "application/json", body: JSON.stringify({
                display_status: code, forecast_status: "unavailable", forecast_with_plan: [],
                forecast_no_heating: [], target_schedule: [], planned_actions: [], weather_forecast: [], price_forecast: [],
            }),
        }));
        await page.goto("/");
        await expect(page.locator(".decision-summary")).toContainText(label);
        await expect(page.getByText("Ordinary writes are prohibited in this build.", { exact: true })).toBeVisible();
    });
}

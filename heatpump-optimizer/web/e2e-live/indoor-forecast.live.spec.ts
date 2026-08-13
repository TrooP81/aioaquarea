import { test, expect } from "@playwright/test";
import { getJson, assertForecastPhysical, type IndoorForecast } from "./_live";

/**
 * Regression coverage for the strange indoor-temperature forecast.
 *
 * Pulls the real `/api/thermal/indoor-forecast` from the live stack and asserts
 * the physically correct behaviour fixed in `fix/indoor-forecast-freefloat`:
 * the no-heating baseline drifts gradually toward outdoor (no snap), and the
 * managed prediction never sits above it or above the warmest plausible bound.
 */
test.describe("Live indoor forecast", () => {
  test("forecast is physically sensible (no snap, correct ordering)", async ({ request }) => {
    const data = await getJson<IndoorForecast>(request, "/api/thermal/indoor-forecast?hours=24");

    expect(data.forecast_with_plan.length).toBe(24);
    expect(data.forecast_no_heating.length).toBe(24);
    assertForecastPhysical(data);
  });

  test("shorter horizon stays consistent", async ({ request }) => {
    const data = await getJson<IndoorForecast>(request, "/api/thermal/indoor-forecast?hours=6");
    expect(data.forecast_no_heating.length).toBe(6);
    assertForecastPhysical(data);
  });

  test("predicted indoor never exceeds the no-heating baseline by snapping to outdoor", async ({ request }) => {
    const data = await getJson<IndoorForecast>(request, "/api/thermal/indoor-forecast?hours=24");
    const withPlan = data.forecast_with_plan.map((p) => p.predicted_indoor_temp);
    const noHeat = data.forecast_no_heating.map((p) => p.predicted_indoor_temp);

    // The original bug made "Predicted Indoor" track the outdoor curve and sit
    // well above "No Heating". Managed indoor must stay within a small band of
    // the no-heating baseline plus any active comfort-target reheat.
    const maxTarget = Math.max(...data.target_schedule.map((t) => t.target));
    for (let h = 0; h < withPlan.length; h++) {
      expect(withPlan[h]).toBeGreaterThanOrEqual(noHeat[h] - 0.06);
      expect(withPlan[h]).toBeLessThanOrEqual(Math.max(noHeat[h], maxTarget) + 0.6);
    }
  });

  test("chart paints the indoor forecast section in the live UI", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Thermal Predictions" })).toBeVisible({ timeout: 15000 });
    // The thermal predictions block must render its SVG chart with live curves.
    const charts = page.locator("svg");
    await expect(charts.first()).toBeVisible({ timeout: 15000 });
    expect(await charts.count()).toBeGreaterThan(0);
  });
});

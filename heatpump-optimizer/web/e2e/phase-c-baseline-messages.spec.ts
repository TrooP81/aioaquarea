import { expect, type Page, test } from "./fixtures";

const now = new Date();
const forecastTs = new Date(now.getTime() + 60 * 60 * 1000).toISOString();

type Scenario = "shadow" | "history" | "default" | "none" | "explicit" | "legacy";

function indoorForecast(scenario: Scenario) {
    const legacy = scenario === "legacy";
    const point = {
        hour: 1,
        ts: forecastTs,
        predicted_indoor_temp: scenario === "explicit" ? 21 : 20,
        ...(legacy
            ? {}
            : {
                baseline_heating_fraction: scenario === "history" ? 0.8 : scenario === "default" ? 0.35 : 0,
                baseline_heating_source: scenario === "history" ? "history" : scenario === "default" ? "default" : "none",
                space_heating_source: scenario === "history" || scenario === "default" ? "baseline" : scenario === "explicit" ? "explicit_override" : "none",
            }),
    };
    return {
        current_indoor: 20,
        outdoor_temp: 5,
        forecast_status: "available",
        forecast_source: "active_plan",
        plan_id: 1,
        plan_age_seconds: 0,
        forecast_with_plan: [point],
        forecast_no_heating: [{ hour: 1, ts: forecastTs, predicted_indoor_temp: 20 }],
        target_schedule: [{ hour: 1, ts: forecastTs, target: 20, comfort_hour: true }],
        weather_forecast: [{ ts: forecastTs, outdoor_temp: 5, wind_speed: 2, irradiance: 0, precipitation: 0 }],
        price_forecast: [{ ts: forecastTs, price_eur_per_kwh: 0.1 }],
        planned_actions: [],
        ...(legacy ? {} : { space_heating_baseline: { effective_mode: scenario === "shadow" ? "shadow" : "on" } }),
    };
}

async function mockDashboard(page: Page, scenario: Scenario) {
    await page.route("**/api/**", (route) =>
        route.fulfill({ status: 404, contentType: "application/json", body: "{}" }),
    );
    await page.route("**/api/dashboard", (route) =>
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                current_status: {
                    ts: now.toISOString(),
                    device_id: "test-device",
                    mode: "heat",
                    operation_status: 1,
                    outdoor_temp: 5,
                    tank_temp: 48,
                    tank_target_temp: 50,
                    zone1_temp: 20,
                    zone1_target_temp: 20,
                    quiet_mode: 0,
                    powerful_mode: 0,
                },
                current_price: 0.1,
                today_kwh: 0,
                today_cost_eur: 0,
                active_plan: {
                    id: 1,
                    created_at: now.toISOString(),
                    actions_count: 0,
                    optimizer_version: "rules_v3",
                },
                has_override: false,
            }),
        }),
    );
    await page.route("**/api/optimizer/status", (route) =>
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ active_layer: "rules_v3", cop_model: { trained: false }, demand_model: { trained: false }, thermal_model: { calibrated: false } }),
        }),
    );
    await page.route("**/api/thermal/indoor-forecast*", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(indoorForecast(scenario)) }),
    );
    await page.route("**/api/currency", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }) }),
    );
    await page.route("**/api/time-format", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hour12: false }) }),
    );
    await page.route(/\/api\/(prices|consumption\/history|status\/history|weather|indoor-temp|plans|plan-activity|operations\/alerts)(\?|$)/, (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
    );
}

test("chart_message_uses_v5_source_metadata_and_legacy_overlap_fallback", async ({ page }) => {
    const expected = {
        shadow: "Automatic room heat is being evaluated but does not affect this displayed plan.",
        history: "Expected automatic room heat from recent history is included in this plan.",
        default: "Expected automatic room heat uses the configured default in this plan.",
        none: "No room heating is expected in this plan.",
        legacy: "No room heating is planned, so Plan forecast and No heating are the same scenario.",
    } as const;

    for (const scenario of ["shadow", "history", "default", "none", "explicit", "legacy"] as const) {
        await mockDashboard(page, scenario);
        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        const chart = page.getByRole("region", { name: "Indoor comfort, weather and price forecast" });
        await expect(chart).toBeVisible();
        if (scenario === "explicit") {
            await expect(chart.getByText(/Automatic room heat|Expected automatic room heat|No room heating is expected/)).toHaveCount(0);
        } else {
            await expect(chart.getByText(expected[scenario], { exact: true })).toBeVisible();
        }
    }
});

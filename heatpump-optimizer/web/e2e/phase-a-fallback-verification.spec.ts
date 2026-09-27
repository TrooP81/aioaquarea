import { expect, test } from "@playwright/test";

test("renders fallback indoor forecast warning without browser errors", async ({ page }) => {
    const browserErrors: string[] = [];
    page.on("console", (message) => {
        if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => browserErrors.push(`page: ${error.message}`));

    await page.route("**/api/**", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
    );
    const json = (body: unknown) => ({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
    });
    await page.route("**/api/dashboard", (route) =>
        route.fulfill(
            json({
                current_status: {
                    ts: new Date().toISOString(),
                    device_id: "test-device",
                    mode: "heat",
                    operation_status: 1,
                    outdoor_temp: 6,
                    tank_temp: 48,
                    tank_target_temp: 50,
                    zone1_temp: 21,
                    zone1_target_temp: 21.5,
                    quiet_mode: 0,
                    powerful_mode: 0,
                },
                current_price: 0.1,
                today_kwh: 0,
                today_cost_eur: 0,
                active_plan: null,
                has_override: false,
            }),
        ),
    );
    await page.route("**/api/optimizer/status", (route) =>
        route.fulfill(json({ active_layer: "rules_v3", cop_model: { trained: false }, demand_model: { trained: false }, thermal_model: { calibrated: false } })),
    );
    await page.route("**/api/sensors/diagnostics", (route) =>
        route.fulfill(json({ summary: "No sensor diagnostics", room_spread_c: null, sensors: [] })),
    );
    await page.route("**/api/thermal/forecast-scorecard", (route) =>
        route.fulfill(
            json({
                plans_scored: 0,
                overall: { samples: 0, mae: null, bias: null, p90_abs_error: null },
                horizons: [],
                regimes: {},
                quality_gate: { status: "observing", control_allowed: false, reason: "No forecast evidence" },
                note: "No forecast evidence",
            }),
        ),
    );
    await page.route("**/api/thermal/status", (route) =>
        route.fulfill(
            json({
                current: { tank_temp: 48, tank_target: 50, outdoor_temp: 6, zone1_temp: 21, timestamp: new Date().toISOString() },
                predictions: {
                    tank_heating: { minutes_to_target: 30, heating_rate_per_hour: 2, confidence: "low" },
                    tank_cooling: { minutes_until_min: null, loss_rate_per_hour: 0.1, confidence: "low" },
                    zone_boost: { minutes_for_2deg: 60, heating_rate_per_hour: 2, confidence: "low" },
                },
                model_params: { tank_heating_rate: 2, tank_standby_loss: 0.1, zone_heating_rate: 1, last_calibrated: null, sample_count: 0 },
            }),
        ),
    );
    await page.route(/\/api\/thermal\/curve(?:\?|$)/, (route) =>
        route.fulfill(
            json({
                current: { tank_temp: 48, tank_target: 50, outdoor_temp: 6, zone1_temp: 21 },
                curves: {
                    tank_standby: [{ hour: 1, predicted_temp: 47.9, state: "standby" }],
                    tank_heating: [{ hour: 1, predicted_temp: 48.2, state: "heating" }],
                    zone_standby: [{ hour: 1, predicted_temp: 20.9, state: "standby" }],
                },
            }),
        ),
    );
    await page.route(/\/api\/thermal\/indoor-forecast(?:\?|$)/, (route) =>
        route.fulfill(
            json({
                current_indoor: 21,
                outdoor_temp: 6,
                forecast_status: "fallback",
                forecast_quality: { status: "fallback", control_allowed: false, reason: "quality_gate_missing" },
                forecast_with_plan: [{ hour: 1, predicted_indoor_temp: 20.8 }],
                forecast_no_heating: [{ hour: 1, predicted_indoor_temp: 20.5 }],
                target_schedule: [{ hour: 1, target: 21.5, comfort_hour: true }],
                weather_forecast: [{ ts: new Date().toISOString(), outdoor_temp: 6, wind_speed: 3, irradiance: 0, precipitation: 0 }],
                price_forecast: [{ ts: new Date().toISOString(), price_eur_per_kwh: 0.1 }],
                planned_actions: [],
                observed_history: [{ hour: 0, ts: new Date().toISOString(), temperature: 21 }],
                comfort_assessment: { state: "degraded", summary: "Indoor forecast is using the rules fallback." },
            }),
        ),
    );
    await page.route(/\/api\/indoor-temp(?:\?|$)/, (route) => route.fulfill(json([])));
    await page.route("**/api/currency", (route) => route.fulfill(json({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" })));
    await page.route("**/api/time-format", (route) => route.fulfill(json({ hour12: false })));
    await page.route(/\/api\/(prices|consumption\/history|status\/history|weather)(\?|$)/, (route) => route.fulfill(json([])));
    await page.route(/\/api\/(plans|plan-activity|operations\/alerts)(\?|$)/, (route) => route.fulfill(json([])));

    await page.goto("/");
    const chartsTab = page.locator('button[aria-controls="dashboard-panel-charts"]:visible');
    await expect(chartsTab).toBeVisible();
    await chartsTab.evaluate((element) => (element as HTMLButtonElement).click());
    await expect(page.locator("#dashboard-panel-charts")).toBeVisible();
    await expect(page.locator("#dashboard-panel-charts").getByText("Comfort warning")).toBeVisible();
    await expect(page.locator("#dashboard-panel-charts .recharts-wrapper").first()).toBeVisible();
    expect(browserErrors).toEqual([]);
});

import { test, expect } from "@playwright/test";

const mockPrices = Array.from({ length: 48 }, (_, i) => ({
  ts: new Date(Date.now() - (12 - i) * 3600000).toISOString(),
  price_eur_per_kwh: 0.05 + Math.sin(i / 4) * 0.1,
}));

const mockConsumption = Array.from({ length: 12 }, (_, i) => ({
  ts: new Date(Date.now() - i * 3600000).toISOString(),
  heat_kwh: 1.2 + Math.random() * 0.5,
  cool_kwh: 0,
  tank_kwh: 0.5 + Math.random() * 0.3,
  total_kwh: 1.7 + Math.random() * 0.8,
  outdoor_temp: 5 + i * 0.5,
}));

const mockWeather = Array.from({ length: 60 }, (_, i) => ({
  ts: new Date(Date.now() + (i - 12) * 3600000).toISOString(),
  temperature: 8 + Math.sin(i / 6) * 4,
  wind_speed: 3,
  humidity: 70,
  cloud_cover: 0.5,
  irradiance: 0,
  precipitation: 0,
}));

const mockStatusHistory = [24, 1, 0].map((hoursAgo) => ({
  ts: new Date(Date.now() - hoursAgo * 3600000).toISOString(),
  device_id: "test-device",
  tank_temp: 48,
  tank_target_temp: 50,
  zone1_temp: 21,
  zone1_target_temp: 22,
  outdoor_temp: 5,
}));

const mockIndoorHistory = [
  { timestamp: new Date(Date.now() - 24 * 3600000).toISOString(), temperature: 19 },
  { timestamp: new Date().toISOString(), temperature: 21.5 },
];

const mockDashboard = {
  current_status: {
    ts: new Date().toISOString(),
    device_id: "test-device",
    mode: "heat",
    operation_status: 1,
    outdoor_temp: 5.0,
    tank_temp: 48.5,
    tank_target_temp: 50,
    zone1_temp: 21.0,
    zone1_target_temp: 22,
    quiet_mode: 0,
    powerful_mode: 0,
  },
  current_price: 0.085,
  today_kwh: 12.5,
  today_cost_eur: 1.23,
  active_plan: {
    id: 42,
    optimizer_version: "rules_v1",
    cost_estimate_eur: 2.85,
    price_currency: "EUR",
    actions_count: 3,
  },
  has_override: false,
};

const mockIndoorForecast = {
  current_indoor: 21.5,
  outdoor_temp: 5,
  forecast_with_plan: [
    { hour: 1, predicted_indoor_temp: 21.3 },
    { hour: 2, predicted_indoor_temp: 21.1 },
  ],
  forecast_no_heating: [
    { hour: 1, predicted_indoor_temp: 21.2 },
    { hour: 2, predicted_indoor_temp: 20.8 },
  ],
  target_schedule: [
    { hour: 1, target: 20.5, comfort_hour: true },
    { hour: 2, target: 20.5, comfort_hour: true },
  ],
  weather_forecast: [
    { ts: new Date(Date.now() + 3600000).toISOString(), outdoor_temp: 5, wind_speed: 3, irradiance: 0, precipitation: 0 },
    { ts: new Date(Date.now() + 7200000).toISOString(), outdoor_temp: 4.8, wind_speed: 3.1, irradiance: 0, precipitation: 0.4 },
  ],
  price_forecast: [
    { ts: new Date(Date.now() + 3600000).toISOString(), price_eur_per_kwh: 0.08 },
    { ts: new Date(Date.now() + 7200000).toISOString(), price_eur_per_kwh: 0.11 },
  ],
  forecast_source: "active_plan",
  plan_id: 42,
  planned_actions: [{ hour: 2, action_type: "normal_mode_on", status: "pending" }],
};

test.describe("Price Chart", () => {
  test.beforeEach(async ({ page }) => {
    page.on("pageerror", (error) => console.error("PAGE_ERROR", error.stack));
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockDashboard),
      })
    );
    await page.route("**/api/prices*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockPrices),
      })
    );
    await page.route("**/api/consumption*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockConsumption),
      })
    );
    await page.route("**/api/consumption/history*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockConsumption),
      })
    );
    await page.route("**/api/weather*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockWeather),
      })
    );
    await page.route(/\/api\/status\/history(?:\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockStatusHistory) })
    );
    await page.route(/\/api\/indoor-temp(?:\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockIndoorHistory) })
    );
    await page.route("**/api/plans*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );
    await page.route("**/api/thermal/indoor-forecast*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockIndoorForecast) })
    );
    await page.route("**/api/currency", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }),
      })
    );
    await page.route("**/api/indoor-temp/latest", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ avg_temperature: 21.5, latest_reading: new Date().toISOString(), sensor_count: 1, last_fresh_reading: new Date().toISOString() }),
      })
    );
    await page.route("**/api/learning-mode", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: false }) })
    );
    await page.route("**/api/time-format", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hour12: false }) })
    );
    await page.route(/\/api\/plan-activity(?:\?|$)/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{
          id: 7,
          plan_id: 42,
          plan_created_at: new Date().toISOString(),
          optimizer_version: "rules_v3",
          scheduled_ts: new Date().toISOString(),
          action_type: "eco_mode_on",
          status: "executed",
          executed_at: new Date().toISOString(),
          payload: {},
          result: { verified: true },
        }]),
      })
    );
  });

  test("renders price chart section", async ({ page }) => {
    await page.goto("/");
    const chartsTab = page.getByRole("tab", { name: "Charts" });
    const comfortChart = page.getByRole("region", { name: "Indoor comfort, weather and price forecast" });
    await chartsTab.click();
    try {
      await expect(comfortChart).toBeVisible({ timeout: 5000 });
    } catch {
      // Next dev can reload once while compiling the initial client bundle.
      // Re-select the tab after that reload so this test checks the chart, not
      // the development-server startup race.
      await chartsTab.click();
      await expect(comfortChart).toBeVisible({ timeout: 5000 });
    }
    await expect(comfortChart.getByText("Indoor Comfort, Weather & Price — 2h", { exact: true })).toBeVisible();
    await expect(comfortChart.locator(".recharts-area-area")).toHaveCount(1);
    // Additional weather/price overlays are allowed; the core comfort view
    // must retain at least its four explanatory forecast curves.
    expect(await comfortChart.locator(".recharts-line-curve").count()).toBeGreaterThanOrEqual(4);
    await expect(page.getByRole("button", { name: "Show hot water" })).toBeVisible();
  });

  test("marks now and explains the price history and forecast window", async ({ page }) => {
    const pageErrors: Error[] = [];
    page.on("pageerror", (error) => pageErrors.push(error));

    await page.goto("/");
    await page.getByRole("tab", { name: "Charts" }).click();
    await page.getByText("Show raw weather, price and temperature history").click();

    const priceChart = page.getByRole("region", { name: "Electricity price chart" });
    await expect(priceChart).toBeVisible();
    await expect(
      priceChart.getByText("Electricity Price (EUR c/kWh) — Past 24h / Next 24h"),
    ).toBeVisible();
    await expect(priceChart.getByText("Now", { exact: true })).toBeVisible();
    await expect(priceChart).toContainText(
      "Past prices are left of Now; forecast prices are right of it.",
    );

    const weatherChart = page.getByRole("region", { name: "Weather forecast chart" });
    await expect(weatherChart.getByText("Weather — Past 12h / Next 48h")).toBeVisible();
    await expect(weatherChart.getByText("Now", { exact: true })).toBeVisible();
    await expect(weatherChart).toContainText(
      "Past conditions are left of Now; forecast conditions are right of it.",
    );

    const temperatureChart = page.getByRole("region", { name: "Temperature history chart" });
    await expect(temperatureChart.getByText("Temperature History — Past 24h")).toBeVisible();
    expect(pageErrors.map((error) => error.message)).toEqual([]);
  });

  test("page renders all main sections", async ({ page }) => {
    await page.goto("/");
    // Wait for main content to load
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    // Dashboard cards should be visible
    await expect(page.locator(".status-badge.online")).toBeVisible();
  });

  test("switches between dashboard workspaces without scrolling through all sections", async ({ page }) => {
    await page.goto("/");

    const planTab = page.getByRole("tab", { name: "Plan" });
    await expect(planTab).toHaveAttribute("aria-selected", "false");
    await planTab.click();

    await expect(planTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "What actually happened" })).toBeVisible();
    await expect(page.locator("#dashboard-panel-overview")).toBeHidden();

    const modelsTab = page.getByRole("tab", { name: "Models" });
    await modelsTab.click();
    await expect(modelsTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "How the optimizer is deciding" })).toBeVisible();
  });
});

test.describe("Plan View", () => {
  test("shows active plan info", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockDashboard),
      })
    );
    await page.route("**/api/prices*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/consumption*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/currency", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "SEK", prefix: "", suffix: " kr", multiplier: 1, price_label: "SEK/kWh" }),
      })
    );
    await page.route("**/api/plans/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 42,
          created_at: new Date().toISOString(),
          horizon_start: new Date().toISOString(),
          horizon_end: new Date(Date.now() + 86400000).toISOString(),
          optimizer_version: "rules_v1",
          cost_estimate_eur: 2.85,
          actions_count: 3,
          actions: [
            {
              id: 1,
              scheduled_ts: new Date(Date.now() + 7200000).toISOString(),
              action_type: "force_dhw_on",
              payload: {},
              status: "pending",
              executed_at: null,
            },
          ],
        }),
      })
    );
    await page.route("**/api/plans", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 42,
            created_at: new Date().toISOString(),
            horizon_start: new Date().toISOString(),
            horizon_end: new Date(Date.now() + 86400000).toISOString(),
            optimizer_version: "rules_v1",
            cost_estimate_eur: 2.85,
            actions_count: 3,
          },
        ]),
      })
    );

    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");

    // Next action card should be visible in dashboard area
    await expect(page.locator(".next-action-card").first()).toBeVisible({ timeout: 10000 });

    await page.getByRole("tab", { name: "Plan" }).click();

    // Active Plan section should show the plan header
    await expect(page.locator("text=Active Plan")).toBeVisible();
    await expect(page.locator(".plan-cost-value")).toContainText("EUR");
    await expect(page.locator(".plan-cost-value")).not.toContainText("kr");

    // Should show human-readable status "Scheduled" instead of raw "pending"
    await expect(page.locator("text=Scheduled").first()).toBeVisible({ timeout: 5000 });

    // Should show human-readable action label
    await expect(
      page.locator("#dashboard-panel-plan .plan-action-type").getByText("Heat hot water"),
    ).toBeVisible();
  });

  test("handles no active plan gracefully", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...mockDashboard, active_plan: null }),
      })
    );
    await page.route("**/api/prices*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/consumption*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/plans*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );

    await page.goto("/");
    // Should not crash — page loads normally
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    await expect(page.locator(".status-badge.online")).toBeVisible();
    // Empty next action card should be present
    await expect(page.locator(".next-action-card--empty")).toBeVisible({ timeout: 5000 });
  });

  test("shows error state when plan actions fail to load", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockDashboard),
      })
    );
    await page.route("**/api/prices*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/consumption*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    // Plan actions endpoint returns 500
    await page.route("**/api/plans/**", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: "{}" })
    );
    await page.route("**/api/plans", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );

    await page.goto("/");
    await page.getByRole("tab", { name: "Plan" }).click();
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    // Should display error message instead of empty list
    await expect(page.locator(".plan-error")).toBeVisible({ timeout: 5000 });
  });
});

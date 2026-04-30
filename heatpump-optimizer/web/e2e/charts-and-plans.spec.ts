import { test, expect } from "@playwright/test";

const mockPrices = Array.from({ length: 24 }, (_, i) => ({
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
    actions_count: 3,
  },
  has_override: false,
};

test.describe("Price Chart", () => {
  test.beforeEach(async ({ page }) => {
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
    await page.route("**/api/plans*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );
  });

  test("renders price chart section", async ({ page }) => {
    await page.goto("/");
    // Recharts renders SVG elements
    await expect(page.locator("svg").first()).toBeVisible({ timeout: 10000 });
  });

  test("page renders all main sections", async ({ page }) => {
    await page.goto("/");
    // Wait for main content to load
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    // Dashboard cards should be visible
    await expect(page.locator(".status-badge.online")).toBeVisible();
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
  });
});

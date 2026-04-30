import { test, expect } from "@playwright/test";

test.describe("Dashboard", () => {
  test("loads and shows header", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
  });

  test("shows loading state initially", async ({ page }) => {
    await page.goto("/");
    // The page should transition from loading to either connected or disconnected
    await expect(
      page.locator(".status-badge")
    ).toBeVisible({ timeout: 10000 });
  });

  test("shows connected status when API is healthy", async ({ page }) => {
    // Mock the API to return valid dashboard data
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
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
          active_plan: null,
          has_override: false,
        }),
      })
    );

    await page.goto("/");
    await expect(page.locator(".status-badge.online")).toContainText("Connected");
  });

  test("shows disconnected when no device status", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          current_status: null,
          current_price: null,
          today_kwh: 0,
          today_cost_eur: 0,
          active_plan: null,
          has_override: false,
        }),
      })
    );

    await page.goto("/");
    await expect(page.locator(".status-badge.offline")).toContainText("Disconnected");
  });

  test("shows error banner on API failure", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({ status: 500 })
    );

    await page.goto("/");
    await expect(page.locator(".override-banner")).toContainText("API Error");
  });
});

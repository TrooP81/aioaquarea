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

  test("shows stale readings messaging for an old device sample", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          current_status: {
            ts: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
            device_id: "test-device",
            mode: "heat",
            outdoor_temp: 5,
            tank_temp: 48,
            tank_target_temp: 50,
            zone1_temp: 21,
            quiet_mode: 0,
            space_heating_active: false,
          },
          current_status_fresh: false,
          current_status_age_seconds: 86400,
          current_price: null,
          today_kwh: 0,
          today_cost_eur: 0,
          active_plan: null,
          has_override: false,
        }),
      })
    );

    await page.goto("/");
    await expect(page.locator(".status-badge.stale")).toContainText("Stale");
    await expect(page.getByText("Latest readings", { exact: true })).toBeVisible();
    await expect(page.getByText(/Heat-pump readings are stale/)).toBeVisible();
  });

  test("shows error banner on API failure", async ({ page }) => {
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({ status: 500 })
    );

    await page.goto("/");
    await expect(page.locator(".override-banner")).toContainText("API Error");
  });

  test("auto-dismisses successful poll-result banner and keeps failure banner manually dismissible", async ({ page }) => {
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
          current_status_fresh: true,
          current_status_age_seconds: 0,
          current_price: 0.085,
          today_kwh: 12.5,
          today_cost_eur: 1.23,
          active_plan: null,
          has_override: false,
        }),
      })
    );

    await page.route("**/api/learning-mode", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, since: null, days_elapsed: null }),
      })
    );

    let pollNowCalls = 0;
    await page.route("**/api/poll-now", (route) => {
      pollNowCalls += 1;
      if (pollNowCalls === 1) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "ok", results: {} }),
        });
      }

      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "partial",
          results: {
            weather: { success: false, message: "timeout" },
          },
        }),
      });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Poll Now" }).click();
    const successBanner = page.locator(".override-banner", { hasText: "All data fetched successfully" });
    await expect(successBanner).toBeVisible();
    await expect(successBanner).toHaveCount(0, { timeout: 9000 });

    await page.getByRole("button", { name: "Poll Now" }).click();
    const failureBanner = page.locator(".override-banner", { hasText: "weather: timeout" });
    await expect(failureBanner).toBeVisible();
    await page.getByRole("button", { name: "Dismiss" }).click();
    await expect(failureBanner).toHaveCount(0);
  });
});

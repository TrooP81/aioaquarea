import { test, expect } from "@playwright/test";

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
  active_plan: null,
  has_override: false,
};

const optimizerStatus = {
  configured_layer: "rules_only",
  active_layer: "rules_v3",
  fallback_layer: "rules_v3",
  learning_mode: { enabled: false, since: null, days_elapsed: null },
  cop_model: { trained: false, last_trained: null, samples: 0 },
  demand_model: { trained: false, last_trained: null, samples: 0 },
  thermal_model: { calibrated: false, tank_heating_rate: 0, confidence: "default", last_calibrated: null },
};

async function mockCommon(page: import("@playwright/test").Page) {
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockDashboard) })
  );
  for (const pattern of ["**/api/prices*", "**/api/consumption*", "**/api/plans*", "**/api/weather*"]) {
    await page.route(pattern, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
  }
  await page.route("**/api/optimizer/status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(optimizerStatus) })
  );
}

test.describe("Learning Mode", () => {
  test("shows learning-mode banner when enabled", async ({ page }) => {
    await mockCommon(page);
    await page.route("**/api/learning-mode", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: true, since: new Date().toISOString(), days_elapsed: 3 }),
      })
    );

    await page.goto("/");
    await expect(page.locator(".override-banner")).toContainText("Learning mode active");
  });

  test("no learning-mode banner when disabled", async ({ page }) => {
    await mockCommon(page);
    await page.route("**/api/learning-mode", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, since: null, days_elapsed: null }),
      })
    );

    await page.goto("/");
    await expect(page.locator(".override-banner")).toHaveCount(0);
  });

  test("can turn learning mode on from the card", async ({ page }) => {
    await mockCommon(page);

    let enabled = false;
    await page.route("**/api/learning-mode", (route) => {
      if (route.request().method() === "POST") {
        enabled = true;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ enabled: true, since: new Date().toISOString(), days_elapsed: 0 }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled, since: null, days_elapsed: null }),
      });
    });

    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/");
    await page.getByRole("tab", { name: "Controls" }).click();
    const toggle = page.getByRole("button", { name: "Turn On Learning Mode" });
    await expect(toggle).toBeVisible({ timeout: 5000 });
    await toggle.click();

    await expect(page.getByRole("button", { name: "Turn Off Learning Mode" })).toBeVisible();
    await expect(page.locator(".override-banner")).toContainText("Learning mode active");
  });
});

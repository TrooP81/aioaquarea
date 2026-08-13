import { expect, test, type Page } from "@playwright/test";

async function mockSettingsPage(page: Page) {
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        outcome_experiments_enabled: {
          value: "false",
          type: "bool",
          description: "Enable optional manual heat-curve trials. Suggestions require your review and never send heat-pump commands.",
        },
        outcome_experiment_max_curve_step_c: {
          value: "0.5",
          type: "float",
          description: "Largest heat-curve adjustment suggested for one manual trial (°C)",
        },
      }),
    })
  );
  await page.route("**/api/version", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ version: "0.12.0", api_contract: "2026-07-28.3" }) })
  );
  await page.route("**/api/currency", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }) })
  );
  await page.route("**/api/comfort-schedule", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: [], weekend: [] }) })
  );
  await page.route("**/api/comfort-schedule/learned", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: {}, weekend: {} }) })
  );
  await page.route("**/api/smartthings/oauth/status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false, method: null, expires_at: null }) })
  );
  await page.route("**/api/smartthings/devices", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ devices: [] }) })
  );
}

test("shows the live release and its change history on Settings", async ({ page }) => {
  await mockSettingsPage(page);
  await page.goto("/settings");
  await page.getByRole("tab", { name: "System" }).click();

  await expect(page.getByTestId("app-version")).toContainText("v0.12.0");
  await expect(page.getByRole("heading", { name: "Release History" })).toBeVisible();
  await expect(page.getByTestId("dashboard-version")).toHaveText("v0.12.0");
  await expect(page.getByTestId("api-version")).toHaveText("v0.12.0");
  await expect(page.getByTestId("api-contract")).toHaveText("2026-07-28.3");
  await expect(page.getByRole("heading", { name: "Manual trial suggestions" })).toBeVisible();
  await expect(page.getByLabel("Enable optional manual heat-curve trials. Suggestions require your review and never send heat-pump commands.")).not.toBeChecked();
});

test("opens manual trial suggestions from the direct settings link", async ({ page }) => {
  await mockSettingsPage(page);
  await page.goto("/settings?tab=system#manual-trial-suggestions");

  await expect(page.getByRole("tab", { name: "System" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "Manual trial suggestions" })).toBeVisible();
});

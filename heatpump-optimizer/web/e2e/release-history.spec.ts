import { expect, test, type Page } from "@playwright/test";

async function mockSettingsPage(page: Page) {
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/version", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ version: "0.3.0" }) })
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

  await expect(page.getByTestId("app-version")).toContainText("v0.3.0");
  await expect(page.getByRole("heading", { name: "Release History" })).toBeVisible();
  await expect(page.getByTestId("dashboard-version")).toHaveText("v0.3.0");
  await expect(page.getByTestId("api-version")).toHaveText("v0.3.0");
  await expect(page.getByText("Comfort-model training now uses only measurements known at the forecast time, avoiding future-data leakage in its accuracy validation.")).toBeVisible();
});

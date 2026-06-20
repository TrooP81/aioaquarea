import { test, expect, type Page } from "@playwright/test";

function settingsPayload() {
  return {
    smartthings_enabled: { value: "true", type: "bool", description: "Enable SmartThings indoor temperature polling", options: ["true", "false"] },
  };
}

async function mockSettings(page: Page) {
  // Catch-all default so unrelated settings-page fetches resolve cleanly.
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(settingsPayload()) })
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

test.describe("Reset data card", () => {
  test("posts only the selected scopes and shows a summary", async ({ page }) => {
    await mockSettings(page);

    let resetBody: unknown = null;
    await page.route("**/api/admin/reset", async (route) => {
      resetBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          scopes: ["indoor_temp"],
          deleted_rows: { indoor_temp_reading: 42 },
          total_rows_deleted: 42,
          models_reset: true,
          deleted_models: ["cop_model_1.pkl"],
        }),
      });
    });

    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/settings");

    await expect(page.getByText("Danger Zone — Reset Data")).toBeVisible();

    const indoor = page
      .locator("label", { hasText: "Indoor temperature readings" })
      .locator("input[type=checkbox]");
    await indoor.check();

    await expect(
      page.getByText(/Trained ML models.*will also be reset/),
    ).toBeVisible();

    await page.getByRole("button", { name: "Reset Selected Data" }).click();

    await expect(page.getByText(/Deleted 42 record\(s\)/)).toBeVisible();
    expect(resetBody).toEqual({ scopes: ["indoor_temp"] });
  });

  test("select all chooses every scope", async ({ page }) => {
    await mockSettings(page);

    let resetBody: any = null;
    await page.route("**/api/admin/reset", async (route) => {
      resetBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          scopes: resetBody?.scopes ?? [],
          deleted_rows: {},
          total_rows_deleted: 0,
          models_reset: true,
          deleted_models: [],
        }),
      });
    });

    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/settings");

    await page
      .locator("label", { hasText: "Start everything fresh (select all)" })
      .locator("input[type=checkbox]")
      .check();

    await page.getByRole("button", { name: "Reset Selected Data" }).click();

    await expect(page.getByText(/Deleted 0 record\(s\)/)).toBeVisible();
    expect(resetBody?.scopes.length).toBe(7);
  });
});

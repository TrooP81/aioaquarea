import { test, expect, type Page } from "./fixtures";

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
          deleted_models: ["demand_model_1.pkl"],
        }),
      });
    });

    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/settings");
    await page.getByRole("tab", { name: "System" }).click();

    await expect(page.getByText("Danger Zone — Reset Data")).toBeVisible();

    const indoor = page
      .locator("label", { hasText: "Indoor temperature readings" })
      .locator("input[type=checkbox]");
    await indoor.check();

    await expect(
      page.getByText(/Retained models.*will also be reset/),
    ).toBeVisible();

    await page.getByRole("button", { name: "Reset Selected Data" }).click();

    await expect(page.getByText(/Deleted 42 record\(s\)/)).toBeVisible();
    await expect(page.getByText(/COP artifacts and all model baselines are retained/)).toBeVisible();
    expect(resetBody).toEqual({ scopes: ["indoor_temp"] });
  });

  test("select all chooses every scope", async ({ page }) => {
    await mockSettings(page);

    let resetBody: { scopes: string[] } = { scopes: [] };
    await page.route("**/api/admin/reset", async (route) => {
      resetBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          scopes: resetBody.scopes,
          deleted_rows: {},
          total_rows_deleted: 0,
          models_reset: true,
          deleted_models: [],
        }),
      });
    });

    let confirmation = "";
    page.on("dialog", (dialog) => {
      confirmation = dialog.message();
      return dialog.accept();
    });

    await page.goto("/settings");
    await page.getByRole("tab", { name: "System" }).click();

    await page
      .locator("label", { hasText: "Select all resettable data" })
      .locator("input[type=checkbox]")
      .check();

    await page.getByRole("button", { name: "Reset Selected Data" }).click();

    await expect(page.getByText(/Deleted 0 record\(s\)/)).toBeVisible();
    expect(resetBody.scopes.sort()).toEqual(["device_status", "energy", "indoor_temp", "logs", "plans", "prices", "weather"]);
    expect(resetBody.scopes).not.toContain("manual_cop");
    expect(confirmation).toContain("COP history, COP artifacts and all model baselines are kept");
    expect(confirmation).not.toContain("ALL collected data");
  });

  test("energy reset describes consumption only and retained COP history", async ({ page }) => {
    await mockSettings(page);
    await page.goto("/settings");
    await page.getByRole("tab", { name: "System" }).click();
    const energy = page.locator("label", { hasText: "Energy history" });
    await expect(energy).toContainText("Consumption records only; retired COP history is retained");
    await energy.locator("input[type=checkbox]").check();
    await expect(page.getByText(/Retained models \(demand, comfort, thermal\)/)).toBeVisible();
    await expect(page.getByText("Energy & COP history", { exact: true })).toHaveCount(0);
  });

  test("manual COP ledger reset is separate and posts only its own scope", async ({ page }) => {
    await mockSettings(page);
    const bodies: unknown[] = [];
    await page.route("**/api/admin/reset", async (route) => {
      bodies.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          scopes: ["manual_cop"],
          deleted_rows: { manual_cop_observations: 182 },
          total_rows_deleted: 182,
          manual_cop_monitoring_enabled: false,
          models_reset: false,
          deleted_models: [],
          model_lifecycle: null,
        }),
      });
    });
    let confirmation = "";
    page.on("dialog", (dialog) => {
      confirmation = dialog.message();
      return dialog.accept();
    });

    await page.goto("/settings");
    await page.getByRole("tab", { name: "System" }).click();
    const manual = page.getByTestId("manual-cop-reset");
    await expect(manual).toContainText("never included in \"Select all\"");
    await expect(page.locator("label", { hasText: "Manual controller-COP" })).toHaveCount(0);

    await manual.getByRole("button", { name: "Reset manual COP readings" }).click();

    await expect(manual.getByRole("status")).toHaveText(
      "Deleted 182 manual reading(s); experimental COP forecasting is off.",
    );
    expect(bodies).toEqual([{ scopes: ["manual_cop"] }]);
    expect(confirmation).toContain("for all devices");
    expect(confirmation).toContain("turn experimental COP forecasting off");
    expect(confirmation).toContain("pending safety restores are kept");
    await expect(page.getByRole("button", { name: "Reset Selected Data" })).toBeDisabled();
  });

  test("declined manual COP confirmation sends nothing", async ({ page }) => {
    await mockSettings(page);
    let requests = 0;
    await page.route("**/api/admin/reset", async (route) => {
      requests += 1;
      await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
    });
    page.on("dialog", (dialog) => dialog.dismiss());
    await page.goto("/settings");
    await page.getByRole("tab", { name: "System" }).click();
    await page.getByRole("button", { name: "Reset manual COP readings" }).click();
    await expect(page.getByTestId("manual-cop-reset").getByRole("status")).toHaveCount(0);
    expect(requests).toBe(0);
  });

  for (const refusal of [
    { status: 503, body: { detail: { code: "manual_cop_reset_unavailable" } }, message: "Manual COP reset unavailable: nothing was deleted. Try again later." },
    { status: 400, body: { detail: "The manual_cop reset scope must be requested alone" }, message: "The manual_cop reset scope must be requested alone" },
    { status: 200, body: { status: "ok", total_rows_deleted: "182" }, message: "Manual COP reset response is invalid" },
  ]) {
    test(`manual COP reset renders safe outcome for ${refusal.status} ${JSON.stringify(refusal.body)}`, async ({ page }) => {
      await mockSettings(page);
      await page.route("**/api/admin/reset", (route) => route.fulfill({
        status: refusal.status, contentType: "application/json", body: JSON.stringify(refusal.body),
      }));
      page.on("dialog", (dialog) => dialog.accept());
      await page.goto("/settings");
      await page.getByRole("tab", { name: "System" }).click();
      const manual = page.getByTestId("manual-cop-reset");
      await manual.getByRole("button", { name: "Reset manual COP readings" }).click();
      await expect(manual.getByRole("alert")).toHaveText(refusal.message);
      await expect(manual.getByRole("status")).toHaveCount(0);
      await expect(manual.getByRole("button", { name: "Reset manual COP readings" })).toBeEnabled();
    });
  }
});

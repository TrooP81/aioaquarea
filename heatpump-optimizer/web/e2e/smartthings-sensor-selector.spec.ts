import { test, expect, type Page } from "@playwright/test";

const sensors = [
  { device_id: "dev-living", label: "Living Room Sensor", room_id: "room-1" },
  { device_id: "dev-bedroom", label: "Bedroom Sensor", room_id: "room-2" },
];

function settingsPayload(deviceIds: string) {
  return {
    smartthings_enabled: { value: "true", type: "bool", description: "Enable SmartThings indoor temperature polling", options: ["true", "false"] },
    smartthings_device_ids: { value: deviceIds, type: "str", description: "Indoor temperature sensors to poll (none selected = poll all discovered)", options: null },
    smartthings_poll_interval: { value: "300", type: "int", description: "SmartThings poll interval in seconds", options: null },
  };
}

async function mockSettings(page: Page, deviceIds: string) {
  // Catch-all default so unrelated settings-page fetches resolve cleanly.
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/settings", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(settingsPayload(deviceIds)) })
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
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: true, method: "oauth", expires_at: null }) })
  );
  await page.route("**/api/smartthings/devices", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ devices: sensors }) })
  );
}

test.describe("SmartThings sensor selector", () => {
  test("lists discovered sensors with current selection checked", async ({ page }) => {
    await mockSettings(page, "dev-living");
    await page.goto("/settings");
    await page.getByRole("tab", { name: "Integrations" }).click();

    await expect(page.getByText("Living Room Sensor")).toBeVisible();
    await expect(page.getByText("Bedroom Sensor")).toBeVisible();

    const living = page.locator("label", { hasText: "Living Room Sensor" }).locator("input[type=checkbox]");
    const bedroom = page.locator("label", { hasText: "Bedroom Sensor" }).locator("input[type=checkbox]");
    await expect(living).toBeChecked();
    await expect(bedroom).not.toBeChecked();
  });

  test("saving sends the selected device ids", async ({ page }) => {
    await mockSettings(page, "");

    let savedDeviceIds: string | undefined;
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        const body = JSON.parse(route.request().postData() || "{}");
        savedDeviceIds = body.settings?.smartthings_device_ids;
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "updated", count: 1 }) });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(settingsPayload("")) });
    });

    await page.goto("/settings");
    await page.getByRole("tab", { name: "Integrations" }).click();
    await page.locator("label", { hasText: "Bedroom Sensor" }).locator("input[type=checkbox]").check();
    await page.getByRole("button", { name: /^Save \d+ changes?$/ }).click();

    await expect.poll(() => savedDeviceIds).toBe("dev-bedroom");
  });

  test("shows an error when discovery fails", async ({ page }) => {
    await mockSettings(page, "");
    await page.route("**/api/smartthings/devices", (route) =>
      route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "SmartThings not connected" }) })
    );

    await page.goto("/settings");
    await page.getByRole("tab", { name: "Integrations" }).click();
    await expect(page.getByText("SmartThings not connected")).toBeVisible();
  });
});

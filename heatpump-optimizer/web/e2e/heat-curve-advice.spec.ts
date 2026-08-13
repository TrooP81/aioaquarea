import { expect, test } from "@playwright/test";

const curveSettings = {
  heat_curve_outdoor_cold_c: { value: "5", type: "float", description: "Cold outdoor point" },
  heat_curve_supply_cold_c: { value: "47", type: "float", description: "Supply water at cold point" },
  heat_curve_outdoor_warm_c: { value: "15", type: "float", description: "Warm outdoor point" },
  heat_curve_supply_warm_c: { value: "23", type: "float", description: "Supply water at warm point" },
  heat_curve_heating_off_outdoor_c: { value: "13", type: "float", description: "Heating-off outdoor temperature" },
  heat_curve_delta_t_c: { value: "4", type: "float", description: "Heat-pump ΔT" },
};

test("copies a bounded heat-curve recommendation into the Settings draft", async ({ page }) => {
  let saved: Record<string, string> | undefined;
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      saved = JSON.parse(route.request().postData() || "{}").settings;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "updated", count: 6 }) });
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(curveSettings) });
  });
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/thermal/heat-curve-advice", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "too_warm",
        indoor_error_c: 2.8,
        current: { outdoor_cold_c: 5, supply_cold_c: 47, outdoor_warm_c: 15, supply_warm_c: 23, heating_off_outdoor_c: 13, delta_t_c: 4 },
        suggested: { outdoor_cold_c: 5, supply_cold_c: 45, outdoor_warm_c: 15, supply_warm_c: 21, heating_off_outdoor_c: 12, delta_t_c: 4 },
        reasons: ["Indoor temperature is 2.8°C above the 21.5°C comfort target."],
        manual_only: true,
        readings: { indoor_temp_c: 24.3, comfort_target_c: 21.5, outdoor_temp_c: 21, curve_supply_target_c: 23, controller_heating_enabled: false },
        verification: { status: "not_started", recommendation_available: true, summary: "No heat-curve verification is active yet.", reasons: [] },
      }),
    })
  );
  await page.route("**/api/comfort-schedule", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: [], weekend: [] }) })
  );
  await page.route("**/api/comfort-schedule/learned", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: {}, weekend: {} }) })
  );
  await page.route("**/api/currency", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }) })
  );

  await page.goto("/settings");

  await expect(page.getByRole("heading", { name: "Controller Heat Curve & Comfort Plan" })).toBeVisible();
  await expect(page.getByText("Heating is currently off at this outdoor temperature")).toBeVisible();
  await page.getByRole("button", { name: "Use recommendation as draft" }).click();
  await expect(page.locator("#setting-heat_curve_supply_cold_c")).toHaveValue("45");
  await expect(page.locator("#setting-heat_curve_supply_warm_c")).toHaveValue("21");
  await expect(page.locator("#setting-heat_curve_heating_off_outdoor_c")).toHaveValue("12");

  await page.getByRole("button", { name: /^Save \d+ changes?$/ }).click();
  await expect.poll(() => saved?.heat_curve_supply_cold_c).toBe("45");
  await expect.poll(() => saved?.heat_curve_heating_off_outdoor_c).toBe("12");
});

test("keeps new curve recommendations locked while verification is pending", async ({ page }) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/settings", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(curveSettings) })
  );
  await page.route("**/api/logs*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/thermal/heat-curve-advice", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "verification_pending",
        indoor_error_c: 2.8,
        current: { outdoor_cold_c: 5, supply_cold_c: 45, outdoor_warm_c: 15, supply_warm_c: 21, heating_off_outdoor_c: 12, delta_t_c: 4 },
        suggested: null,
        reasons: ["Waiting for cooler outdoor readings below the controller heating-off threshold."],
        manual_only: true,
        readings: { indoor_temp_c: 24.0, comfort_target_c: 21.5, outdoor_temp_c: 21, curve_supply_target_c: 21, controller_heating_enabled: false },
        verification: {
          status: "pending",
          recommendation_available: false,
          summary: "Measuring the effect of the latest controller change before another recommendation.",
          reasons: ["Waiting for cooler outdoor readings below the controller heating-off threshold."],
          elapsed_hours: 4.5,
          minimum_hours: 24,
          indoor_sample_count: 3,
          minimum_indoor_samples: 6,
          heating_condition_samples: 0,
          minimum_heating_condition_samples: 3,
        },
      }),
    })
  );
  await page.route("**/api/comfort-schedule", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: [], weekend: [] }) })
  );
  await page.route("**/api/comfort-schedule/learned", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: {}, weekend: {} }) })
  );
  await page.route("**/api/currency", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }) })
  );

  await page.goto("/settings");

  await expect(page.getByText("Verification in progress")).toBeVisible();
  await expect(page.getByText("4.5 / 24 hours")).toBeVisible();
  await expect(page.getByRole("button", { name: "Use recommendation as draft" })).toHaveCount(0);
});

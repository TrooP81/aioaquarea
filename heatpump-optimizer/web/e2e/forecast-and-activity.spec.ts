import { expect, test } from "@playwright/test";

const now = new Date();

test("shows rainfall and separates actual activity from plan revisions", async ({ page }) => {
  const activePlan = {
    id: 42,
    created_at: now.toISOString(),
    horizon_start: now.toISOString(),
    horizon_end: new Date(now.getTime() + 24 * 60 * 60 * 1000).toISOString(),
    optimizer_version: "rules_v3",
    cost_estimate_eur: 2.85,
    actions_count: 1,
  };

  // Default for non-essential dashboard requests. More-specific routes below override it.
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" })
  );
  await page.route("**/api/optimizer/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        active_layer: "rules_v3",
        cop_model: { trained: false },
        demand_model: { trained: false },
        thermal_model: { calibrated: false },
      }),
    })
  );
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        current_status: {
          ts: now.toISOString(), device_id: "test-device", mode: "heat", operation_status: 1,
          outdoor_temp: 5, tank_temp: 48, tank_target_temp: 50, zone1_temp: 21,
          zone1_target_temp: 22, quiet_mode: 0, powerful_mode: 0,
        },
        current_price: 0.085,
        today_kwh: 12.5,
        today_cost_eur: 1.23,
        active_plan: activePlan,
        has_override: false,
      }),
    })
  );
  await page.route(/\/api\/prices(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route(/\/api\/consumption\/history(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route(/\/api\/status\/history(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route(/\/api\/indoor-temp(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.route("**/api/currency", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }),
    })
  );
  await page.route("**/api/time-format", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hour12: false }) })
  );
  await page.route(/\/api\/weather(?:\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        ts: now.toISOString(), temperature: 6, wind_speed: 4, humidity: 82,
        cloud_cover: 0.8, irradiance: 25, precipitation: 1.6,
      }]),
    })
  );
  await page.route(/\/api\/thermal\/indoor-forecast(?:\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        current_indoor: 21,
        outdoor_temp: 6,
        forecast_with_plan: [{ hour: 1, predicted_indoor_temp: 20.8 }],
        forecast_no_heating: [{ hour: 1, predicted_indoor_temp: 20.5 }],
        target_schedule: [{ hour: 1, target: 20.5, comfort_hour: true }],
        weather_forecast: [{ ts: now.toISOString(), outdoor_temp: 6, wind_speed: 4, irradiance: 25, precipitation: 1.6 }],
        price_forecast: [{ ts: now.toISOString(), price_eur_per_kwh: 0.085 }],
        forecast_source: "active_plan",
        plan_id: 42,
        planned_actions: [],
      }),
    })
  );
  await page.route(/\/api\/plan-activity(?:\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        id: 7, plan_id: 17, plan_created_at: now.toISOString(), optimizer_version: "rules_v3",
        scheduled_ts: now.toISOString(), action_type: "force_dhw_on", status: "executed",
        executed_at: now.toISOString(), payload: {}, result: { verified: true },
      }]),
    })
  );
  await page.route(/\/api\/plans\/42(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...activePlan, actions: [] }) })
  );
  await page.route(/\/api\/plans(?:\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([activePlan]) })
  );

  await page.goto("/");

  await page.getByRole("tab", { name: "Charts" }).click();
  await expect(page.getByRole("region", { name: "Indoor comfort, weather and price forecast" })).toBeVisible();
  await page.getByText("Show raw weather, price and temperature history").click();
    await expect(
      page.locator(".recharts-bar-rectangle .recharts-rectangle").first(),
    ).toBeVisible();
  await expect(page.getByText("Blue bars show rain in mm/h.")).toBeVisible();
  await page.getByRole("tab", { name: "Plan" }).click();
  await expect(page.getByTestId("plan-activity")).toBeVisible();
  await expect(page.getByText("Heat hot water")).toBeVisible();
  await expect(page.getByText("Command completed and verified")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Plan change history" })).toBeVisible();
});

import { test, expect } from "./fixtures";

// G12 / P1.4: the Thermal Predictions card must name the real forecast limitation
// instead of always asking for a fresh room sensor.
const baseForecast = {
  current_indoor: null,
  outdoor_temp: null,
  forecast: [],
  forecast_with_plan: [],
  forecast_no_heating: [],
  target_schedule: [],
  planned_actions: [],
  weather_forecast: [],
  price_forecast: [],
  forecast_source: "unavailable",
  display_status: "unavailable",
  plan_id: null,
  comfort_assessment: { state: "unavailable", summary: "" },
};

const sensorSentence = "A fresh trusted room reading is required";
const observeOnlySentence = "Observe-only build: heat-pump writes are prohibited";

async function openThermalPredictions(page: import("@playwright/test").Page, body: object) {
  await page.route("**/api/thermal/indoor-forecast*", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(body) }),
  );
  await page.goto("/?view=under-the-hood");
  const card = page.locator(".plan-section").filter({
    has: page.getByRole("heading", { name: "Thermal Predictions" }),
  });
  await expect(card).toBeVisible();
  return card;
}

test.describe("Thermal Predictions forecast limitation reason", () => {
  test("missing sensor still asks for a fresh trusted room reading", async ({ page }) => {
    const card = await openThermalPredictions(page, {
      ...baseForecast,
      forecast_status: "unavailable",
      forecast_unavailable_reason: "no_fresh_selected_sensor_readings",
    });
    await expect(card).toContainText("Indoor forecast paused: no fresh selected sensor readings.");
    await expect(card).toContainText(sensorSentence);
    await expect(card).not.toContainText(observeOnlySentence);
  });

  test("non-sensor limitation names its reason without a sensor warning", async ({ page }) => {
    const card = await openThermalPredictions(page, {
      ...baseForecast,
      forecast_status: "unavailable",
      forecast_unavailable_reason: "nonfinite_weather_forecast_input",
    });
    await expect(card).toContainText("Indoor forecast paused: nonfinite weather forecast input.");
    await expect(card).not.toContainText(sensorSentence);
  });

  test("unavailable without a reason does not invent a sensor failure", async ({ page }) => {
    const card = await openThermalPredictions(page, {
      ...baseForecast,
      forecast_status: "unavailable",
      forecast_unavailable_reason: null,
    });
    await expect(card).toContainText("Indoor forecast paused: forecast unavailable.");
    await expect(card).not.toContainText(sensorSentence);
  });

  test("observe-only build is explained separately from sensor freshness", async ({ page }) => {
    const card = await openThermalPredictions(page, {
      ...baseForecast,
      current_indoor: 21.8,
      forecast_status: "fallback",
      forecast_unavailable_reason: null,
      forecast_quality: {
        control_allowed: false,
        control_reason: "observe_only_build",
        reason: "insufficient_scored_forecasts",
      },
    });
    await expect(card).toContainText(observeOnlySentence);
    await expect(card).not.toContainText("Indoor forecast paused");
    await expect(card).not.toContainText(sensorSentence);
  });
});

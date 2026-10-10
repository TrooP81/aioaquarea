import { test, expect, modelLifecycleStatus, retiredCopModel } from "./fixtures";
import type { ModelReportingState } from "../lib/api-types";

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
  model_lifecycle: {
    demand: modelLifecycleStatus(), comfort: modelLifecycleStatus(), thermal: modelLifecycleStatus(),
  },
  cop_model: retiredCopModel,
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
  await page.route("**/api/control-state", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ state: "observing", headline: "Observe-only build", detail: "Ordinary writes are prohibited in this build.", reason_code: "observe_only_build", primary_action: null, notices: [] }) })
  );
  await page.route("**/api/indoor-temp/latest", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ avg_temperature: null, latest_reading: null, sensor_count: 0, last_fresh_reading: null }) })
  );
}

test.describe("Learning Mode", () => {
  for (const [demandTrained, thermalCalibrated] of [[false, false], [true, false], [false, true], [true, true]] as const) {
    test(`never infers reporting qualification from trained flags: ${demandTrained}/${thermalCalibrated}`, async ({ page }) => {
      await mockCommon(page);
      await page.route("**/api/optimizer/status", (route) => route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...optimizerStatus,
          cop_model: { ...retiredCopModel, trained: true },
          demand_model: { ...optimizerStatus.demand_model, trained: demandTrained },
          thermal_model: { ...optimizerStatus.thermal_model, calibrated: thermalCalibrated },
        }),
      }));
      await page.route("**/api/learning-mode", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true, since: new Date().toISOString(), days_elapsed: 3 }) }));
      await page.goto("/");
      await expect(page.getByText("Qualified for reporting: 0/3", { exact: true })).toBeVisible();
      await page.getByText("Heat pump details", { exact: true }).click();
      await expect(page.locator(".ml-status-label")).toHaveText("0/3 qualified for reporting");
      await expect(page.locator('[aria-label^="COP efficiency model"]')).toHaveCount(0);
    });
  }

  const reportingStates: ModelReportingState[] = ["unqualified", "candidate", "qualified-for-reporting", "expired", "unavailable"];
  for (const reportingState of reportingStates) {
    test(`test_five_reporting_states_never_offer_control [${reportingState}]`, async ({ page }, testInfo) => {
      await mockCommon(page);
      const mutations: string[] = [];
      page.on("request", (request) => {
        if (request.method() !== "GET" && new URL(request.url()).pathname.startsWith("/api/")) {
          mutations.push(new URL(request.url()).pathname);
        }
      });
      const reports = {
        demand: modelLifecycleStatus(reportingState), comfort: modelLifecycleStatus(reportingState), thermal: modelLifecycleStatus(reportingState),
      };
      await page.route("**/api/optimizer/status", (route) => route.fulfill({
        contentType: "application/json", body: JSON.stringify({
          ...optimizerStatus, model_lifecycle: reports,
          demand_model: { ...optimizerStatus.demand_model, trained: true },
          thermal_model: { ...optimizerStatus.thermal_model, calibrated: true },
          seasonal_calibration: {
            enabled: true, observe_only_active: true, heating_season_detected: false,
            reason: "observe_only_seasonal_calibration_active", auto_exit: false
          },
        }),
      }));
      await page.route("**/api/learning-mode", (route) => route.fulfill({
        contentType: "application/json", body: JSON.stringify({
          enabled: false, effective_active: true, state_reliable: true, sources: ["seasonal"],
          since: null, days_elapsed: null,
          open_revert_obligations: { count: 1, oldest_age_seconds: 3600, action_types: ["force_dhw_off"] },
        }),
      }));
      await page.goto("/");
      await expect(page.getByRole("status").filter({ hasText: "Learning (seasonal)" })).toBeVisible();
      await expect(page.getByText("Seasonal observation remains active until explicit human exit.")).toBeVisible();
      await expect(page.getByText("Pending safety restores: 1")).toBeVisible();
      await page.getByRole("tab", { name: "Under the Hood" }).click();
      await page.getByRole("button", { name: "Show diagnostics" }).click();
      await expect(page.getByText(/Seasonal calibration: Observation persists until explicit human exit/)).toBeVisible();
      await expect(page.getByText("Model maintenance is disabled.")).toBeVisible();
      const seasonalSummary = page.getByTestId("seasonal-reporting-summary");
      await expect(seasonalSummary).toBeVisible();
      expect(await seasonalSummary.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        return [...element.children].every((child) => {
          const childBounds = child.getBoundingClientRect();
          return childBounds.left >= bounds.left - 1 && childBounds.right <= bounds.right + 1;
        });
      })).toBe(true);
      for (const component of ["demand", "comfort", "thermal"]) {
        const report = page.getByTestId(`lifecycle-${component}`);
        await expect(report).toHaveAttribute("data-reporting-state", reportingState);
        await expect(report).toContainText("Incumbent:");
        await expect(report).toContainText("Candidate:");
        await expect(report).toContainText("Next maintenance: Disabled");
        await expect(report).toContainText("Last valid outcome:");
        await expect(report).toContainText("Next decision checkpoint:");
        await expect(report).toContainText("Ordinary writes are prohibited in this build.");
        await expect(report).toContainText("Optimization requires a new reviewed, approved build.");
        await expect(report.locator("..")).not.toContainText("undefined");
        if (["candidate", "qualified-for-reporting", "expired"].includes(reportingState)) {
          await expect(report).toContainText("Paired outcomes: 48; missing: 12");
          await expect(report).toContainText("Outdoor bins missing: 1");
          await expect(report).toContainText(/Exclusions:.*unknown execution.*3/i);
          await expect(report).toContainText("Next decision checkpoint: #1");
        }
      }
      await expect(page.getByRole("button", { name: /resume.*automatic|enable.*control/i })).toHaveCount(0);
      await expect(page.locator("body")).not.toContainText(/approved for control|resume automatic control|ready for control/i);
      expect(mutations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`obs6-${reportingState}.png`), fullPage: true });
    });
  }

  test("shows unavailable thermal learning state without treating the plan as active", async ({ page }) => {
    await mockCommon(page);
    await page.route("**/api/thermal/status", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ model_params: { last_calibrated: null, sample_count: 0 }, predictions: { tank_heating: { minutes_to_target: 0, heating_rate_per_hour: 0, confidence: "low" }, tank_cooling: { minutes_until_min: null, loss_rate_per_hour: 0, confidence: "low" }, zone_boost: { minutes_for_2deg: 0, heating_rate_per_hour: 0, confidence: "low" } } }) }));
    await page.route("**/api/thermal/curve?hours=24", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ current: { tank_temp: 48, tank_target: 52, outdoor_temp: 7, zone1_temp: 20, plan_driven: false, learning_mode: false, learning_mode_state: "unknown", learning_mode_reliable: false }, curves: { tank_standby: [], tank_heating: [], zone_standby: [] } }) }));

    await page.goto("/");
    await page.getByRole("tab", { name: "Under the Hood" }).click();
    await expect(page.getByText("Learning-state data is unavailable")).toBeVisible();
  });

  test("thermal confidence is head-specific after indoor-only qualification", async ({ page }, testInfo) => {
    await mockCommon(page);
    await page.route("**/api/thermal/status", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify({
        reporting_state: "qualified-for-reporting", control_ready: false, ordinary_writes_allowed: false,
        model_params: { last_calibrated: new Date().toISOString(), sample_count: 200 },
        predictions: {
          tank_heating: { minutes_to_target: 50, heating_rate_per_hour: 4.8, confidence: "default" },
          tank_cooling: { minutes_until_min: 600, loss_rate_per_hour: -0.5, confidence: "default" },
          zone_boost: { minutes_for_2deg: 80, heating_rate_per_hour: 1.5, confidence: "default" },
          indoor: {
            current_indoor_temp: 21, minutes_to_heat_1deg: 100, minutes_to_cool_2deg: 400,
            indoor_heating_rate: 0.6, indoor_cooling_rate: -0.3,
            indoor_heating_samples: 100, indoor_cooling_samples: 100,
            confidence: "learned", heating_confidence: "learned", cooling_confidence: "default",
          },
        },
      }),
    }));
    await page.route("**/api/thermal/curve?hours=24", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify({
        current: { tank_temp: 48, tank_target: 52, outdoor_temp: 7, zone1_temp: 20, plan_driven: false, learning_mode: true },
        curves: { tank_standby: [], tank_heating: [], zone_standby: [] },
      }),
    }));
    await page.goto("/");
    await page.getByRole("tab", { name: "Under the Hood" }).click();
    const thermal = page.locator(".plan-section").filter({ has: page.getByRole("heading", { name: "Thermal Predictions", exact: true }) });
    await expect(thermal.locator(".metric-card").filter({ hasText: /Tank.*Target/ })).toContainText("(default)");
    await expect(thermal.locator(".metric-card").filter({ hasText: "Tank Standby Loss" })).toContainText("(default)");
    const indoor = thermal.locator(".metric-card").filter({ hasText: "Indoor Temperature" });
    await expect(indoor).toContainText("heat (learned)");
    await expect(indoor).toContainText("cool (default)");
    await expect(thermal).toContainText("Historical fit: 200 samples.");
    await expect(thermal).not.toContainText("based on learned heating/cooling rates");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await thermal.screenshot({ path: testInfo.outputPath("fix1-thermal-head-confidence.png") });
  });

  test("uses the legacy learning boolean when tri-state fields are absent", async ({ page }) => {
    await mockCommon(page);
    await page.route("**/api/thermal/status", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ model_params: { last_calibrated: null, sample_count: 0 }, predictions: { tank_heating: { minutes_to_target: 0, heating_rate_per_hour: 0, confidence: "low" }, tank_cooling: { minutes_until_min: null, loss_rate_per_hour: 0, confidence: "low" }, zone_boost: { minutes_for_2deg: 0, heating_rate_per_hour: 0, confidence: "low" } } }) }));
    await page.route("**/api/thermal/curve?hours=24", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ current: { tank_temp: 48, tank_target: 52, outdoor_temp: 7, zone1_temp: 20, plan_driven: false, learning_mode: true }, curves: { tank_standby: [], tank_heating: [], zone_standby: [] } }) }));

    await page.goto("/");
    await page.getByRole("tab", { name: "Under the Hood" }).click();
    await expect(page.getByText("Learning mode is on")).toBeVisible();
  });

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
    await page.getByRole("tab", { name: "Home" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Learning" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Turn Off Learning Mode" })).toBeVisible();
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
    await page.getByRole("tab", { name: "Home" }).click();
    await expect(page.locator(".banner").filter({ hasText: "Learning mode enabled" })).toHaveCount(0);
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
    await page.getByRole("tab", { name: "Home" }).click();
    const toggle = page.getByRole("button", { name: "Turn On Learning Mode" });
    await expect(toggle).toBeVisible({ timeout: 5000 });
    await toggle.click();

    await expect(page.getByRole("button", { name: "Turn Off Learning Mode" })).toBeVisible();
    await expect(page.locator(".banner").filter({ hasText: "Learning mode enabled" })).toBeVisible();
  });

  test("shows pending restore warning and cancels force activation", async ({ page }) => {
    await mockCommon(page);
    let postCount = 0;
    await page.route("**/api/learning-mode", (route) => {
      if (route.request().method() === "POST") {
        postCount += 1;
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ detail: { code: "unresolved_safety_reverts", obligations: { count: 1 } } }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, since: null, days_elapsed: null, open_revert_obligations: { count: 1, oldest_age_seconds: 180, action_types: ["force_dhw_off"] } }),
      });
    });
    let dialogs = 0;
    page.on("dialog", (dialog) => {
      dialogs += 1;
      return dialogs === 1 ? dialog.accept() : dialog.dismiss();
    });
    await page.goto("/");
    await page.getByRole("tab", { name: "Home" }).click();
    await page.getByRole("button", { name: "Turn On Learning Mode" }).click();

    await expect(page.getByText("Safety restores are held while observation is active.")).toBeVisible();
    expect(postCount).toBe(1);
  });

  test("confirms and retries forced activation after a 409", async ({ page }) => {
    await mockCommon(page);
    const requests: string[] = [];
    await page.route("**/api/learning-mode*", (route) => {
      if (route.request().method() === "POST") {
        requests.push(route.request().url());
        if (requests.length === 1) {
          return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { obligations: { count: 1 } } }) });
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true, since: new Date().toISOString(), days_elapsed: 0, open_revert_obligations: { count: 1, oldest_age_seconds: 180, action_types: ["force_dhw_off"] } }) });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: false, since: null, days_elapsed: null, open_revert_obligations: { count: 1, oldest_age_seconds: 180, action_types: ["force_dhw_off"] } }) });
    });
    page.on("dialog", (dialog) => dialog.accept());
    await page.goto("/");
    await page.getByRole("tab", { name: "Home" }).click();
    await page.getByRole("button", { name: "Turn On Learning Mode" }).click();

    await expect(page.getByRole("button", { name: "Turn Off Learning Mode" })).toBeVisible();
    expect(requests[1]).toContain("force=true");
  });

  test("learning off never offers ordinary control", async ({ page }, testInfo) => {
    await mockCommon(page);
    let enabled = true;
    const mutations: string[] = [];
    page.on("request", (request) => {
      if (request.method() !== "GET" && new URL(request.url()).pathname.startsWith("/api/")) {
        mutations.push(new URL(request.url()).pathname);
      }
    });
    await page.route("**/api/learning-mode", (route) => {
      if (route.request().method() === "POST") enabled = false;
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ enabled, since: null, days_elapsed: null }) });
    });
    page.on("dialog", (dialog) => {
      expect(dialog.message()).toContain("ordinary writes remain prohibited");
      expect(dialog.message()).not.toContain("resume sending commands");
      return dialog.accept();
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Turn Off Learning Mode" }).click();
    await expect(page.getByRole("button", { name: "Turn On Learning Mode" })).toBeVisible();
    const card = page.locator(".plan-section").filter({ has: page.getByRole("heading", { name: "Learning Mode", exact: true }) });
    await expect(card).toContainText("Optimization requires a new reviewed, approved build");
    expect(mutations).toEqual(["/api/learning-mode"]);
    await page.screenshot({ path: testInfo.outputPath("observe-only-policy.png"), fullPage: true });
  });
});

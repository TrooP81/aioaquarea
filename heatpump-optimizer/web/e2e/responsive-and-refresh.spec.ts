import { test, expect, type Page } from "./fixtures";

const jsonResponse = (body: unknown) => ({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

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

const mockIndoorTemp = {
  avg_temperature: 21.5,
  latest_reading: new Date().toISOString(),
  sensor_count: 1,
  last_fresh_reading: new Date().toISOString(),
};

const mockOptimizerStatus = {
  configured_layer: "rules",
  active_layer: "rules",
  fallback_layer: "rules",
  last_plan: null,
  cop_model: { trained: false, last_trained: null },
  demand_model: { trained: false, last_trained: null },
  thermal_model: {
    calibrated: false,
    tank_heating_rate: 0,
    confidence: "low",
    indoor_heating_confidence: "low",
    indoor_heating_samples: 0,
    last_calibrated: null,
  },
};

async function mockDashboardRequests(page: Page) {
  const routes: Array<[string | RegExp, unknown]> = [
    ["**/api/dashboard", mockDashboard],
    ["**/api/indoor-temp/latest", mockIndoorTemp],
    ["**/api/learning-mode", { enabled: false, since: null, days_elapsed: null }],
    ["**/api/optimizer/status", mockOptimizerStatus],
    ["**/api/comfort-model/status", { trained: false, control_ready: false, control_readiness: null, last_trained: null, training_samples: 0, metrics: null }],
    ["**/api/thermal/forecast-scorecard", { plans_scored: 0, overall: { samples: 0, mae: null, bias: null, p90_abs_error: null }, horizons: [], regimes: {}, quality_gate: { status: "observing", control_allowed: false, reason: "No forecast evidence" }, note: "No forecast evidence" }],
    ["**/api/sensors/diagnostics", { mode: "shadow", controls_unchanged: true, sensor_count: 0, summary: "No sensor diagnostics", sensors: [] }],
    ["**/api/operations/alerts", []],
    ["**/api/outcomes/summary*", null],
    ["**/api/plans*", []],
    ["**/api/plan-activity*", []],
    ["**/api/thermal/indoor-forecast*", {
      current_indoor: null,
      outdoor_temp: null,
      forecast_with_plan: [],
      forecast_no_heating: [],
      weather_forecast: [],
      price_forecast: [],
      target_schedule: [],
      planned_actions: [],
      forecast_status: "unavailable",
      forecast_unavailable_reason: "missing_trusted_input",
    }],
    ["**/api/consumption/history*", []],
    ["**/api/thermal/status", {
      current: { tank_temp: 48.5, tank_target: 50, outdoor_temp: 5, zone1_temp: 21, timestamp: new Date().toISOString() },
      predictions: {
        tank_heating: { minutes_to_target: 0, heating_rate_per_hour: 0, confidence: "low" },
        tank_cooling: { minutes_until_min: null, loss_rate_per_hour: 0, confidence: "low" },
        zone_boost: { minutes_for_2deg: 0, heating_rate_per_hour: 0, confidence: "low" },
      },
      model_params: { tank_heating_rate: 0, tank_standby_loss: 0, zone_heating_rate: 0, last_calibrated: null, sample_count: 0 },
    }],
    ["**/api/thermal/curve*", {
      current: { tank_temp: 48.5, tank_target: 50, outdoor_temp: 5, zone1_temp: 21 },
      curves: { tank_standby: [], tank_heating: [], zone_standby: [] },
    }],
    ["**/api/indoor-temp?*", []],
    ["**/api/currency", { code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EUR c/kWh" }],
    ["**/api/time-format", { hour12: false }],
  ];

  for (const [url, body] of routes) {
    await page.route(url, (route) => route.fulfill(jsonResponse(body)));
  }
}

test.describe("Responsive Layout", () => {
  test.beforeEach(async ({ page }) => {
    await mockDashboardRequests(page);
  });

  test("desktop layout loads correctly", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    await expect(page.locator(".header-actions .status-badge.online", { hasText: "Connected" })).toBeVisible();
  });

  test("tablet layout renders without overflow", async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");

    // No horizontal scroll
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth + 1);
  });

  test("mobile layout renders without overflow", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");

    // No horizontal scroll
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth + 1);
  });

  test("workspace tabs remain visible and use roving tab focus", async ({ page }) => {
    for (const width of [375, 768, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto("/");
      const tabs = page.getByRole("tab");
      await expect(tabs).toHaveCount(3);
      await expect(page.locator('[role="tab"][tabindex="0"]')).toHaveCount(1);
      const active = page.getByRole("tab", { selected: true });
      const rectangles = await active.evaluate((tab) => {
        const nav = tab.closest("nav")!.getBoundingClientRect();
        const rect = tab.getBoundingClientRect();
        return { nav, rect };
      });
      expect(rectangles.rect.left).toBeGreaterThanOrEqual(rectangles.nav.left);
      expect(rectangles.rect.right).toBeLessThanOrEqual(rectangles.nav.right);
    }
  });

  test("workspace tabs support roving keyboard focus and URL history", async ({ page }) => {
    await page.goto("/?view=charts");
    const tabs = page.getByRole("tab");
    const charts = page.getByRole("tab", { name: "Under the hood" });
    await expect(charts).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#dashboard-panel-under-the-hood")).toBeVisible();

    await charts.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Home", exact: true })).toBeFocused();
    await expect(page.getByRole("tab", { name: "Home", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Home");
    await expect(tabs.first()).toBeFocused();
    await page.keyboard.press("End");
    await expect(tabs.last()).toBeFocused();

    await page.evaluate(() => {
      window.history.pushState({}, "", "/?view=plan");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#dashboard-panel-timeline")).toBeVisible();

    await page.goto("/?view=not-a-section");
    await expect(page.getByRole("tab", { name: "Home", exact: true })).toHaveAttribute("aria-selected", "true");
  });

  test("loading and normal dashboard states each expose exactly one main", async ({ page }) => {
    await page.unroute("**/api/dashboard");
    await page.route("**/api/dashboard", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.fulfill(jsonResponse(mockDashboard));
    });

    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("main")).toHaveCount(1);
    await expect(page.locator("main#main-content")).toContainText("Loading...");

    await expect(page.locator("main#main-content")).toContainText("Heat Pump Optimizer");
    await expect(page.locator("main")).toHaveCount(1);
  });

  test("poll results use status for success and alert for failure", async ({ page }) => {
    let succeed = true;
    await page.route("**/api/poll-now", async (route) => {
      if (succeed) {
        return route.fulfill(jsonResponse({ status: "ok", results: {} }));
      }
      return route.fulfill(jsonResponse({ status: "partial", results: { prices: { success: false, message: "Prices unavailable" } } }));
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Refresh from heat pump" }).click();
    await expect(page.getByRole("status", { name: "Heat pump refresh result" })).toContainText("All data fetched successfully");

    succeed = false;
    await page.getByRole("button", { name: "Refresh from heat pump" }).click();
    await expect(page.getByRole("alert", { name: "Heat pump refresh result" })).toContainText("prices: Prices unavailable");
  });

  test("status indicators expose text or accessible labels without relying on color", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".header-actions .status-badge")).toContainText("Connected");
    await expect(page.locator('[aria-label^="COP efficiency model"]')).toHaveCount(0);
    await expect(page.locator('[aria-label^="Comfort forecast model"]')).toHaveCount(1);
    await expect(page.locator('[aria-label^="Demand heating model"]')).toHaveCount(1);
    await expect(page.locator('[aria-label^="Thermal heat-up model"]')).toHaveCount(1);
    await expect(page.locator(".ml-status-label")).toContainText("/3 qualified for reporting");
  });

  test("skip link moves focus to the main content target", async ({ page }) => {
    await page.goto("/");
    const skipLink = page.locator(".skip-link");
    await skipLink.focus();
    await expect(skipLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("main#main-content")).toBeFocused();
  });

  test("reduced motion uses instant tab scrolling", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => {
      const calls: unknown[] = [];
      Object.defineProperty(window, "__scrollCalls", { value: calls, writable: true });
      window.scrollTo = ((...args: unknown[]) => calls.push(args)) as typeof window.scrollTo;
    });
    await page.goto("/");
    await page.getByRole("tab", { name: "Under the hood" }).click();

    const scrollCalls = await page.evaluate(() => (window as unknown as Window & { __scrollCalls: unknown[] }).__scrollCalls);
    expect(scrollCalls.some((args) => JSON.stringify(args).includes('"behavior":"auto"'))).toBe(true);
    expect(scrollCalls.some((args) => JSON.stringify(args).includes('"behavior":"smooth"'))).toBe(false);
  });

  test("responsive grids and primary controls fit the three required viewports", async ({ page }) => {
    for (const [width, expectedColumns] of [[375, 2], [768, 2], [1280, 4]] as const) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto("/");
      const grid = page.locator("#dashboard-panel-home > .grid").first();
      const metrics = await grid.evaluate((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return {
          columns: style.gridTemplateColumns.split(" ").filter(Boolean).length,
          width: rect.width,
          scrollWidth: element.scrollWidth,
        };
      });
      expect(metrics.columns).toBe(expectedColumns);
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.width + 1);

      const pollNow = page.getByRole("button", { name: "Refresh from heat pump" });
      const pollRect = await pollNow.boundingBox();
      expect(pollRect?.height).toBeGreaterThanOrEqual(44);
    }
  });
});

test.describe("Auto-refresh", () => {
  test("dashboard refreshes data periodically", async ({ page }) => {
    let fetchCount = 0;
    const now = new Date("2026-10-15T12:00:00Z");
    await page.clock.install({ time: now });
    await page.clock.pauseAt(now);

    await mockDashboardRequests(page);
    await page.route("**/api/dashboard", (route) => {
      fetchCount++;
      return route.fulfill(jsonResponse({ ...mockDashboard, today_kwh: 12.5 + fetchCount }));
    });

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "GET" &&
          new URL(response.url()).pathname === "/api/dashboard",
      ),
      page.goto("/"),
    ]);
    const initialCount = fetchCount;

    expect(initialCount).toBeGreaterThanOrEqual(1);
    await expect(page.getByRole("tab", { name: "Home", exact: true })).toBeVisible();
    await page.clock.fastForward(30_000);
    await expect.poll(() => fetchCount).toBe(initialCount + 1);
    await expect(page.locator("#dashboard-panel-home")).toContainText(`${(12.5 + initialCount + 1).toFixed(1)} kWh`);
    await page.clock.fastForward(30_000);
    await expect.poll(() => fetchCount).toBe(initialCount + 2);
  });

  for (const trigger of ["manual", "timed"] as const) {
    for (const plan of ["constant", "null"] as const) {
      for (const outcome of ["at_risk", "unavailable"] as const) {
        test(`${trigger} refresh replaces on-target decision evidence with ${outcome} for ${plan} plan`, async ({ page }) => {
          const now = new Date("2026-10-15T12:00:00Z");
          await page.clock.install({ time: now });
          await page.clock.pauseAt(now);
          await mockDashboardRequests(page);
          const activePlan = plan === "constant"
            ? { id: 42, status: "active", optimizer_version: "rules_v7", cost_estimate_eur: 2, actions_count: 0 } : null;
          await page.route("**/api/dashboard", (route) => route.fulfill(jsonResponse({ ...mockDashboard, active_plan: activePlan })));
          await page.route("**/api/poll-now", (route) => route.fulfill(jsonResponse({ status: "ok", results: {} })));
          let updated = false;
          let updatedRequests = 0;
          let release!: () => void;
          const pending = new Promise<void>((resolve) => { release = resolve; });
          await page.route("**/api/thermal/indoor-forecast*", async (route) => {
            const refresh = updated;
            if (refresh) {
              updatedRequests++;
              await pending;
            }
            await route.fulfill(jsonResponse({
              forecast_with_plan: [], forecast_no_heating: [], target_schedule: [],
              planned_actions: [], weather_forecast: [], price_forecast: [],
              forecast_status: refresh && outcome === "unavailable" ? "unavailable" : "available",
              display_status: refresh ? outcome === "at_risk" ? "diverged" : "unavailable" : "fresh",
              comfort_assessment: {
                state: refresh ? outcome : "on_target",
                summary: refresh ? `Updated comfort evidence: ${outcome}` : "Initial on-target evidence",
                ...(refresh && outcome === "at_risk" ? { worst_miss: { shortfall_c: 1.5 } } : {}),
              },
            }));
          });
          await page.goto("/");
          const summary = page.getByRole("region", { name: "Current optimizer decision" });
          await expect(summary).toContainText("Forecast stays within the active target");
          await expect(summary.locator(".forecast-trust-badge")).toHaveText("Fresh forecast");
          updated = true;
          if (trigger === "manual") await page.getByRole("button", { name: "Refresh from heat pump" }).click();
          else await page.clock.fastForward(30_000);
          await expect.poll(() => updatedRequests).toBe(1);
          await expect(summary.locator(".forecast-trust-badge")).toHaveText("Checking forecast");
          await expect(summary).not.toContainText("Initial on-target evidence");
          await expect(summary).not.toContainText("Forecast stays within the active target");
          release();
          await expect(summary).toContainText(`Updated comfort evidence: ${outcome}`);
          await expect(summary.locator(".forecast-trust-badge")).toHaveText(outcome === "at_risk" ? "Forecast diverged" : "Forecast unavailable");
          await expect(summary).not.toContainText("Forecast stays within the active target");
          if (outcome === "at_risk") {
            await expect(summary).toHaveClass(/decision-summary--risk/);
            await expect(summary).toContainText("1.5°C below target");
          } else {
            await expect(summary).not.toHaveClass(/decision-summary--risk/);
            await expect(summary).toContainText("Comfort impact is unavailable until forecast evidence is ready");
          }
        });
      }
    }
  }

  test("a delayed superseded decision forecast cannot overwrite newer refresh evidence", async ({ page }) => {
    await mockDashboardRequests(page);
    await page.route("**/api/poll-now", (route) => route.fulfill(jsonResponse({ status: "ok", results: {} })));
    let phase: "initial" | "older" | "newer" = "initial";
    let olderRequests = 0;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    let finished!: () => void;
    const olderFinished = new Promise<void>((resolve) => { finished = resolve; });
    await page.route("**/api/thermal/indoor-forecast*", async (route) => {
      const requestPhase = phase;
      if (requestPhase === "older") { olderRequests++; await pending; }
      try {
        await route.fulfill(jsonResponse({
          display_status: requestPhase === "newer" ? "diverged" : "fresh",
          comfort_assessment: {
            state: requestPhase === "newer" ? "at_risk" : "on_target",
            summary: `${requestPhase} forecast evidence`,
            worst_miss: { shortfall_c: 2 },
          },
          forecast_with_plan: [], forecast_no_heating: [], target_schedule: [],
          planned_actions: [], weather_forecast: [], price_forecast: [],
        }));
      } catch (error) {
        // A superseded request is aborted by the component before fulfillment.
        if (requestPhase !== "older") throw error;
      } finally {
        if (requestPhase === "older") finished();
      }
    });
    await page.goto("/");
    const summary = page.getByRole("region", { name: "Current optimizer decision" });
    await expect(summary).toContainText("initial forecast evidence");
    phase = "older";
    await page.getByRole("button", { name: "Refresh from heat pump" }).click();
    await expect.poll(() => olderRequests).toBe(1);
    await expect(summary.locator(".forecast-trust-badge")).toHaveText("Checking forecast");
    phase = "newer";
    await page.getByRole("button", { name: "Refresh from heat pump" }).click();
    await expect(summary).toContainText("newer forecast evidence");
    release();
    await olderFinished;
    await expect(summary).toContainText("newer forecast evidence");
    await expect(summary).not.toContainText("older forecast evidence");
    await expect(summary.locator(".forecast-trust-badge")).toHaveText("Forecast diverged");
    await expect(summary).toHaveClass(/decision-summary--risk/);
  });
});

test.describe("Accessibility", () => {
  test.beforeEach(async ({ page }) => {
    await mockDashboardRequests(page);
  });

  test("page has proper heading hierarchy", async ({ page }) => {
    await page.goto("/");
    const h1 = await page.locator("h1").count();
    expect(h1).toBe(1);
  });

  test("page title is set", async ({ page }) => {
    await page.goto("/");
    const title = await page.title();
    expect(title).toBeTruthy();
  });

  test("status badge has text content for screen readers", async ({ page }) => {
    await page.goto("/");
    const badge = page.locator(".header-actions .status-badge.online", { hasText: "Connected" });
    await expect(badge).toBeVisible();
    const text = await badge.textContent();
    expect(text?.length).toBeGreaterThan(0);
  });
});

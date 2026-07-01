import { test, expect } from "@playwright/test";
import { getJson } from "./_live";

/**
 * Verifies the live API is healthy and the core endpoints the dashboard depends
 * on return real, well-shaped data from the running stack (API + DB + models).
 */
test.describe("Live API health", () => {
  test("health endpoint reports DB connected", async ({ request }) => {
    const health = await getJson<{ status: string; db: string }>(request, "/health");
    expect(health.status).toBe("ok");
    expect(health.db).toBe("connected");
  });

  test("dashboard returns a real snapshot", async ({ request }) => {
    const d = await getJson<Record<string, unknown>>(request, "/api/dashboard");
    expect(d).toHaveProperty("current_status");
    expect(d).toHaveProperty("today_kwh");
    expect(typeof d.today_kwh).toBe("number");
    expect(d).toHaveProperty("has_override");
    expect(typeof d.has_override).toBe("boolean");
  });

  test("prices feed returns a non-empty time series", async ({ request }) => {
    const prices = await getJson<{ ts: string; price_eur_per_kwh: number }[]>(request, "/api/prices");
    expect(Array.isArray(prices)).toBeTruthy();
    expect(prices.length).toBeGreaterThan(0);
    for (const p of prices.slice(0, 5)) {
      expect(typeof p.ts).toBe("string");
      expect(Number.isFinite(p.price_eur_per_kwh)).toBeTruthy();
    }
  });

  test("plans history returns plan records with cost estimates", async ({ request }) => {
    const plans = await getJson<{ id: number; optimizer_version: string; actions_count: number }[]>(
      request,
      "/api/plans",
    );
    expect(Array.isArray(plans)).toBeTruthy();
    if (plans.length > 0) {
      const p = plans[0];
      expect(typeof p.id).toBe("number");
      expect(typeof p.optimizer_version).toBe("string");
      expect(typeof p.actions_count).toBe("number");
    }
  });

  test("optimizer status exposes layer + model training state", async ({ request }) => {
    const s = await getJson<Record<string, any>>(request, "/api/optimizer/status");
    expect(typeof s.active_layer).toBe("string");
    expect(s).toHaveProperty("learning_mode");
    expect(typeof s.learning_mode.enabled).toBe("boolean");
    for (const m of ["cop_model", "demand_model", "thermal_model"]) {
      expect(s, `optimizer status has ${m}`).toHaveProperty(m);
    }
  });

  test("thermal status reports calibrated rates and current temps", async ({ request }) => {
    const t = await getJson<Record<string, any>>(request, "/api/thermal/status");
    expect(t).toHaveProperty("current");
    expect(t).toHaveProperty("predictions");
    expect(t).toHaveProperty("model_params");
    expect(Number.isFinite(t.current.outdoor_temp)).toBeTruthy();
  });

  test("learning-mode endpoint returns a boolean flag", async ({ request }) => {
    const lm = await getJson<{ enabled: boolean }>(request, "/api/learning-mode");
    expect(typeof lm.enabled).toBe("boolean");
  });

  test("settings endpoint returns the keyed settings map", async ({ request }) => {
    const settings = await getJson<Record<string, { value: string; type: string }>>(request, "/api/settings");
    expect(typeof settings).toBe("object");
    expect(Object.keys(settings).length).toBeGreaterThan(0);
    // Every entry must carry a value + type so the settings UI can render it.
    for (const meta of Object.values(settings).slice(0, 5)) {
      expect(meta).toHaveProperty("value");
      expect(meta).toHaveProperty("type");
    }
  });
});

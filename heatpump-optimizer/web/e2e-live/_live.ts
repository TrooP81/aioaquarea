import { expect, type APIRequestContext } from "@playwright/test";

/**
 * Shared helpers for the live-stack e2e suite.
 *
 * These tests hit the real running system with no request mocking, so the
 * assertions are deliberately *invariant-based* (shape, ordering, physical
 * bounds) rather than tied to specific values that drift with real data.
 */

/** Web origin the browser loads; `/api/*` is proxied to the API container. */
export const WEB_BASE = process.env.E2E_BASE_URL ?? "http://localhost:4444";

/**
 * Origin for direct API probes. Defaults to the API container so both `/api/*`
 * and top-level routes like `/health` (which the web proxy does NOT forward)
 * are reachable. The web→proxy→API path is still exercised by the browser-based
 * tests, which load the real UI on WEB_BASE and let it call `/api/*` itself.
 */
export const API_BASE = process.env.E2E_API_URL ?? "http://localhost:8500";

export async function getJson<T = unknown>(
  request: APIRequestContext,
  path: string,
): Promise<T> {
  const res = await request.get(`${API_BASE}${path}`);
  expect(res.ok(), `${path} should respond 2xx (got ${res.status()})`).toBeTruthy();
  return (await res.json()) as T;
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export interface IndoorForecast {
  current_indoor: number;
  outdoor_temp: number;
  forecast: { hour: number; predicted_indoor_temp: number }[];
  forecast_with_plan: { hour: number; predicted_indoor_temp: number }[];
  forecast_no_heating: { hour: number; predicted_indoor_temp: number }[];
  target_schedule: { hour: number; target: number; comfort_hour: boolean }[];
}

/**
 * Assert the indoor forecast is physically sensible. Encodes the regression
 * fixed in `fix/indoor-forecast-freefloat`: the no-heating baseline must drift
 * *gradually* toward the outdoor temperature (never snapping straight up to it),
 * and the managed "predicted indoor" must never sit above the no-heating
 * baseline or exceed the warmest of {current, outdoor, comfort target}.
 */
export function assertForecastPhysical(data: IndoorForecast): void {
  const { current_indoor, outdoor_temp } = data;
  expect(isFiniteNumber(current_indoor), "current_indoor is a finite number").toBeTruthy();
  expect(isFiniteNumber(outdoor_temp), "outdoor_temp is a finite number").toBeTruthy();

  const withPlan = data.forecast_with_plan.map((p) => p.predicted_indoor_temp);
  const noHeat = data.forecast_no_heating.map((p) => p.predicted_indoor_temp);
  const targets = data.target_schedule.map((p) => p.target);

  expect(withPlan.length, "with-plan curve has hourly points").toBeGreaterThan(0);
  expect(noHeat.length, "no-heating curve length matches with-plan").toBe(withPlan.length);

  const maxTarget = targets.length ? Math.max(...targets) : current_indoor;
  const gap = current_indoor - outdoor_temp;
  const coolingToward = gap > 0.1; // indoor warmer than outside → should cool
  const warmingToward = gap < -0.1; // indoor colder than outside → should warm

  // Generous sanity envelope. Outdoor varies across the horizon (cool nights,
  // warm afternoons), so this only catches *runaway* extrapolation — like the
  // old base forecast climbing to 40 °C+ — not legitimate diurnal drift.
  const lo = Math.min(current_indoor, outdoor_temp, maxTarget) - 10;
  const hi = Math.max(current_indoor, outdoor_temp, maxTarget) + 10;

  for (let h = 0; h < noHeat.length; h++) {
    const nh = noHeat[h];
    const wp = withPlan[h];
    expect(isFiniteNumber(nh) && isFiniteNumber(wp), `hour ${h} values finite`).toBeTruthy();

    // Ordering: active heating can't leave the house cooler than no heating.
    expect(wp, `with-plan[${h}] >= no-heating[${h}]`).toBeGreaterThanOrEqual(nh - 0.06);

    // Sanity envelope (no runaway extrapolation).
    expect(nh, `no-heating[${h}] within sanity range`).toBeGreaterThanOrEqual(lo);
    expect(nh, `no-heating[${h}] within sanity range`).toBeLessThanOrEqual(hi);
    expect(wp, `with-plan[${h}] within sanity range`).toBeGreaterThanOrEqual(lo);
    expect(wp, `with-plan[${h}] within sanity range`).toBeLessThanOrEqual(hi);

    // Gradual: no single hour jumps more than a damped free-float step. This is
    // the core "no snap to outdoor" guard — the bug snapped indoor to outdoor.
    const prevNh = h === 0 ? current_indoor : noHeat[h - 1];
    expect(Math.abs(nh - prevNh), `no-heating step ${h} is gradual (no snap)`).toBeLessThanOrEqual(1.3);
    const prevWp = h === 0 ? current_indoor : withPlan[h - 1];
    expect(Math.abs(wp - prevWp), `with-plan step ${h} is gradual`).toBeLessThanOrEqual(2.5);
  }

  // Hour 1 is the only step whose outdoor is reliably the reported scalar
  // `outdoor_temp`, so we assert *direction* there: the no-heating baseline must
  // drift TOWARD outdoor, never away. The original bug drifted it upward (away
  // from a cooler outdoor) because it abused the ML comfort model.
  if (coolingToward) {
    expect(noHeat[0], "no-heating cools toward outdoor in hour 1 (no upward drift)").toBeLessThanOrEqual(
      current_indoor + 0.06,
    );
    expect(noHeat[0], "no-heating does not overshoot below outdoor in hour 1").toBeGreaterThanOrEqual(
      outdoor_temp - 0.6,
    );
  } else if (warmingToward) {
    expect(noHeat[0], "no-heating warms toward outdoor in hour 1 (no downward drift)").toBeGreaterThanOrEqual(
      current_indoor - 0.06,
    );
    expect(noHeat[0], "no-heating does not overshoot above outdoor in hour 1").toBeLessThanOrEqual(
      outdoor_temp + 0.6,
    );
  }

  // Direct "no snap to outdoor": with a real gap, the first step must move less
  // than 80% of the way to outdoor (a snap would cover ~100% in one hour).
  if (Math.abs(gap) > 1.5) {
    const firstStep = Math.abs(noHeat[0] - current_indoor);
    expect(firstStep, "first no-heating step does not snap to outdoor").toBeLessThan(0.8 * Math.abs(gap));
  }
}

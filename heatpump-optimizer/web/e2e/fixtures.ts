import { expect, test as base, type Page } from "@playwright/test";

const jsonResponse = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
});

const optimizerStatus = {
    configured_layer: "rules_only",
    active_layer: "rules_v3",
    fallback_layer: "rules_v3",
    last_plan: null,
    cop_model: { trained: false, last_trained: null },
    demand_model: { trained: false, last_trained: null },
    thermal_model: { calibrated: false, last_calibrated: null },
};

const controlState = {
    state: "automatic",
    headline: "Scheduled control remains active",
    detail: "Automatic dispatch remains active.",
    reason_code: "automatic",
    since: null,
    until: null,
    override_id: null,
    active_override_count: 0,
    primary_action: null,
    notices: [],
    resolved_at: "2026-01-01T00:00:00+00:00",
};

const dashboard = {
    current_status: null,
    current_status_fresh: false,
    current_status_age_seconds: null,
    current_price: null,
    today_kwh: 0,
    today_cost_eur: null,
    today_cost_currency: "EUR",
    today_cost_priced_kwh: 0,
    today_cost_unpriced_kwh: 0,
    today_cost_priced_amount: 0,
    today_cost_coverage_pct: 0,
    today_cost_complete: false,
    active_plan: null,
    has_override: false,
    override_id: null,
    space_heating_gate: { state: "blocked", reason: "no_device_status", profile_id: null },
};

const indoorForecast = {
    current_indoor: null,
    outdoor_temp: null,
    forecast_with_plan: [],
    forecast_no_heating: [],
    target_schedule: [],
    planned_actions: [],
    weather_forecast: [],
    price_forecast: [],
    forecast_source: "unavailable",
    forecast_status: "unavailable",
    forecast_unavailable_reason: "No forecast data",
    display_status: "unavailable",
    plan_id: null,
    comfort_assessment: { state: "unavailable" },
};

const sensorDiagnostics = {
    mode: "shadow",
    controls_unchanged: true,
    sensor_count: 0,
    room_spread_c: null,
    summary: "No indoor sensors configured.",
    sensors: [],
};

const emptyArrayPaths = new Set([
    "/api/consumption/history",
    "/api/indoor-temp",
    "/api/operations/alerts",
    "/api/plan-activity",
    "/api/plans",
    "/api/prices",
    "/api/status/history",
    "/api/thermal/curve",
    "/api/weather",
]);

const defaultBody = (path: string, method: string): unknown => {
    if (path === "/api/control-state") return controlState;
    if (path === "/api/dashboard") return dashboard;
    if (path === "/api/thermal/indoor-forecast") return indoorForecast;
    if (path === "/api/thermal/status" || path === "/api/thermal/curve") return null;
    if (path === "/api/thermal/forecast-scorecard") return null;
    if (path === "/api/sensors/diagnostics") return sensorDiagnostics;
    if (path === "/api/learning-mode") return null;
    if (path === "/api/indoor-temp/latest") {
        return { avg_temperature: null, latest_reading: null, sensor_count: 0, last_fresh_reading: null };
    }
    if (emptyArrayPaths.has(path)) return [];
    if (method !== "GET") return { success: true, message: "stubbed" };
    return {};
};

export const test = base.extend({
    page: async ({ page }, apply) => {
        await page.route("**/api/**", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            return route.fulfill(jsonResponse(defaultBody(url.pathname, request.method())));
        });
        await page.route("**/api/optimizer/status", (route) =>
            route.fulfill(jsonResponse(optimizerStatus))
        );
        await page.route("**/api/outcomes/summary*", (route) => route.fulfill(jsonResponse(null)));
        await apply(page);
    },
});

export { expect };
export type { Page };
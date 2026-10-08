import { expect, test as base, type Page } from "@playwright/test";

const jsonResponse = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
});

export const retiredCopModel = {
    trained: false,
    last_trained: null,
    samples: 0,
    source_records: 0,
    metrics: null,
    status: "retired",
    supported: false,
    unavailable_reason: "retired",
    reason: "unverified_thermal_output",
};

export const demandDataQuality = {
    raw_records: 0,
    intervals: 0,
    usable_samples: 0,
    minimum_samples: 168,
    rejected_nonpositive: 0,
    rejected_rate_bounds: 0,
    weather_matches: 0,
    remaining_samples: 168,
    ready_to_train: false,
};

export const manualCOPResponse = {
    devices: ["fixture-device"],
    default_timezone: "Europe/Stockholm",
    device_id: null as string | null,
    granularity: "day" as "day" | "month",
    issue_at: "2026-10-04T08:00:00Z",
    observations: [] as Array<Record<string, unknown>>,
    next_before: null as string | null,
    forecasting_enabled: false,
    monitoring: {
        status: "disabled",
        reason: "forecasting_disabled",
        issue_at: "2026-10-04T08:00:00Z",
        local_issue_at: "2026-10-04T10:00:00+02:00",
        target_date: null as string | null,
        timezone: "Europe/Stockholm",
        fingerprint: "fixture-daily-revisions",
        counts: { daily_rows: 0, available_closed_days: 0, required_closed_days: 28, required_folds: 7, minimum_fold_rows: 20, fold_training_rows: [] as number[] },
        forecast: null as null | { value: number; lower: number; upper: number; log_daily_trend: number; daily_trend_pct: number; band_label: string },
    },
};

export function manualCOPObservation(periodDate = "2026-10-04", value = 9.7, revision = "9007199254740993", granularity: "day" | "month" = "day") {
    return {
        device_id: "fixture-device", granularity, period_date: periodDate,
        timezone: "Europe/Stockholm",
        period_start: granularity === "month" ? "2026-09-30T22:00:00Z" : periodDate === "2026-10-03" ? "2026-10-02T22:00:00Z" : "2026-10-03T22:00:00Z",
        period_end: granularity === "month" ? "2026-10-31T23:00:00Z" : periodDate === "2026-10-03" ? "2026-10-03T22:00:00Z" : "2026-10-04T22:00:00Z",
        value, observed_at: "2026-10-04T08:00:00Z", completeness: "partial",
        created_at: "2026-10-04T08:00:00Z", updated_at: "2026-10-04T08:00:00Z", revision,
        source: "controller_estimate", acquisition: "manual_entry", scope: "total",
        measured: false, accuracy: "approximate", energy_boundary: "unknown",
    };
}

const optimizerStatus = {
    configured_layer: "rules_only",
    active_layer: "rules_v7",
    fallback_layer: "rules_v7",
    last_plan: null,
    cop_model: retiredCopModel,
    demand_model: { trained: false, last_trained: null, data_quality: demandDataQuality },
    thermal_model: {
        calibrated: false,
        last_calibrated: null,
        tank_heating_rate: 0,
        confidence: "default",
        indoor_heating_confidence: "default",
        indoor_heating_samples: 0,
    },
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
    space_heating_gate: { state: "BLOCKED", reason: "no_device_status", profile_id: "default", on_operator: "<", off_operator: ">=", base_c: 15, on_threshold_c: 13, off_threshold_c: 15, last_raw_outdoor_c: null, fingerprint_matches: true },
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

export const defaultBody = (path: string, method: string): unknown => {
    if (path === "/api/cop/manual") return manualCOPResponse;
    if (path === "/api/control-state") return controlState;
    if (path === "/api/optimizer/status") return optimizerStatus;
    if (path === "/api/dashboard") return dashboard;
    if (path === "/api/thermal/indoor-forecast") return indoorForecast;
    if (path === "/api/thermal/status" || path === "/api/thermal/curve") return null;
    if (path === "/api/thermal/forecast-scorecard") return null;
    if (path === "/api/sensors/diagnostics") return sensorDiagnostics;
    if (path === "/api/learning-mode") return null;
    if (path === "/api/comfort-model/status") {
        return { trained: false, control_ready: false, control_readiness: null, last_trained: null, training_samples: 0, metrics: null };
    }
    if (path === "/api/ml/train" && method === "POST") {
        return {
            cop: { status: "retired", supported: false, error: "cop_model_retired", reason: "unverified_thermal_output" },
            demand: { version: "fixture-demand" },
        };
    }
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
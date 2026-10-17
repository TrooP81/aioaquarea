import { expect, test as base, type Page } from "@playwright/test";
import type { ControlState, ModelLifecycleStatus, ModelReportingProgress, ModelReportingState, SeasonalReadiness } from "../lib/api-types";

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

export function modelLifecycleStatus(state: ModelReportingState = "unqualified"): ModelLifecycleStatus {
    const available = state !== "unavailable";
    const hasFit = available && state !== "unqualified";
    const counts: NonNullable<ModelLifecycleStatus["counts"]> = hasFit ? {
        pairs: 48, device_day_clusters: 12, issue_cohorts: 18,
        horizons: { "60": 48 }, outdoor_bins: { "0": 24, "2": 24 },
    } : { pairs: 0, device_day_clusters: 0, issue_cohorts: 0, horizons: {}, outdoor_bins: {} };
    const progress: ModelReportingProgress = {
        maintenance_enabled: false, cadence_hours: 6, cadence_reason: "maintenance_disabled",
        next_cadence_at: null, evidence_for: hasFit ? state === "candidate" ? "candidate" : "incumbent" : null,
        evidence_as_of: available ? "2026-10-09T12:00:00Z" : null,
        counts: available ? counts : null,
        missing_counts: available ? {
            paired_outcomes: 60 - counts.pairs, device_day_clusters: 14 - counts.device_day_clusters,
            issue_cohorts: 20 - counts.issue_cohorts, elapsed_days: hasFit ? 0 : null,
        } : null,
        missing_diversity: available ? {
            populated_outdoor_bins: hasFit ? 1 : 3,
            pairs_per_horizon: hasFit ? { "60": 0 } : {}, pairs_per_outdoor_bin: {},
        } : null,
        exclusion_counts: hasFit ? { unknown_execution: 3 } : {},
        pending_outcomes: available ? 2 : null,
        next_decision_checkpoint: hasFit ? {
            index: 0, at: "2026-10-10T12:00:00Z", due: false, policy: "prospective_native_fixed_daily_v1",
        } : null,
    };
    return {
        ...progress, status: state,
        incumbent_status: state === "candidate" ? "unqualified" : state,
        candidate_status: state === "candidate" ? "candidate" : null,
        candidate_progress: state === "candidate" ? { ...progress, status: "candidate", control_ready: false } : null,
        last_valid_outcome_at: hasFit ? "2026-10-09T11:00:00Z" : null,
        qualification_expires_at: state === "qualified-for-reporting" || state === "expired" ? "2026-10-12T11:00:00Z" : null,
        control_ready: false, ordinary_writes_allowed: false,
        control_readiness: { ready: false, reason: "observe_only_build" },
    };
}

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
        source: "controller_estimate", acquisition: "manual_entry", scope: "unknown",
        measured: false, accuracy: "approximate", energy_boundary: "unknown",
    };
}

const optimizerStatus = {
    configured_layer: "rules_only",
    build_policy: "observe_only",
    ordinary_writes_allowed: false,
    control_ready: false,
    active_layer: "rules_v7",
    fallback_layer: "rules_v7",
    last_plan: null,
    model_lifecycle: {
        demand: modelLifecycleStatus(), comfort: modelLifecycleStatus(), thermal: modelLifecycleStatus(),
    },
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

export function readinessOptimizerStatus({ enabled = null, preference = "unknown", available = true }: {
    enabled?: boolean | null;
    preference?: SeasonalReadiness["maintenance"]["preference"];
    available?: boolean;
} = {}) {
    const readiness: SeasonalReadiness = {
        as_of: available ? "2026-10-11T12:00:00Z" : null,
        generation: available ? "season-readiness-test-generation" : null,
        context_digest: available ? "c".repeat(64) : null,
        source_watermarks: available ? { consumption: { copied_rows: 391, first_at: "2026-09-25T06:00:00Z", last_at: "2026-10-11T12:00:00Z", content_digest: "d".repeat(64) } } : {},
        availability: available ? "assessed" : "unavailable",
        reason: available ? null : "snapshot_materialization_unavailable",
        components: {},
        maintenance: {
            enabled, preference, queue_count: null, running: null, phase: null,
            last_attempt_at: null, next_due_at: null, reason: "running_request_proof_unavailable"
        },
    };
    for (const [name, total, train, holdout, minimumTrain, minimumHoldout] of [
        ["demand", 390, 308, 78, 168, 24], ["indoor_heating", 64, 28, 20, 20, 3],
        ["indoor_cooling", 50, 19, 15, 20, 3],
    ] as const) {
        const fitted = name === "indoor_heating" && available;
        readiness.components[name] = {
            collection: {
                availability: available ? "assessed" : "unavailable",
                total: available ? total : null, train: available ? train : null, holdout: available ? holdout : null,
                minimum_train: minimumTrain, minimum_holdout: minimumHoldout,
                remaining_train: available ? Math.max(0, minimumTrain - train) : null,
                remaining_holdout: available ? Math.max(0, minimumHoldout - holdout) : null,
                reason: available ? null : "snapshot_materialization_unavailable"
            },
            fit: {
                availability: fitted ? "persisted" : available ? "not_assessed" : "unavailable",
                status: fitted ? "validated" : available ? "awaiting_fit" : null,
                data_cutoff: fitted ? "2026-10-10T12:00:00Z" : null,
                artifact_hash: fitted ? "a".repeat(64) : null, role: fitted ? "candidate" : null,
                request_id: fitted ? "sr-indoor-family-request" : null, reason: null
            },
            reporting: fitted ? modelLifecycleStatus("candidate") : {
                counts: null, missing_counts: null, missing_diversity: null, pending_outcomes: null,
                qualification_expires_at: null, next_decision_checkpoint: null,
                evidence_reason: "no_selected_reference",
            },
            next_step: available ? fitted ? "await_prospective_reporting_qualification" :
                name === "indoor_cooling" ? "collect_space_heating_evidence" : "await_fit_or_validation" : "readiness_unavailable",
        };
    }
    return {
        ...optimizerStatus,
        model_lifecycle: { ...optimizerStatus.model_lifecycle, thermal: modelLifecycleStatus(available ? "candidate" : "unavailable") },
        demand_model: {
            ...optimizerStatus.demand_model, source_records: available ? 391 : null,
            data_quality: {
                ...demandDataQuality, usable_samples: available ? 390 : null,
                remaining_samples: available ? 0 : null, ready_to_train: available
            }
        },
        seasonal_calibration: {
            enabled: true, observe_only_active: true, heating_season_detected: true,
            reason: "observe_only_seasonal_calibration_active", average_outdoor_c: 5,
            auto_train: preference === "unknown" ? null : preference === "automatic", auto_exit: false,
            next_step: available ? "await_successful_demand_training" : "readiness_unavailable", readiness
        },
    };
}

export const controlState: ControlState = {
    state: "observing",
    headline: "Observe-only build",
    detail: "Ordinary writes are prohibited in this build. Optimization requires a new reviewed, approved build; only existing linked restores may run outside positive observation.",
    reason_code: "observe_only_build",
    build_policy: "observe_only",
    control_ready: false,
    ordinary_writes_allowed: false,
    optimization_requires_new_build: true,
    since: null,
    until: null,
    override_id: null,
    active_override_count: 0,
    primary_action: { kind: "link", label: "View plan", href: "/?view=timeline" },
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
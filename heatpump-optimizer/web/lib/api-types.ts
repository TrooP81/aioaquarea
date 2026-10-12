export type ModelReportingState =
    | "unqualified"
    | "candidate"
    | "qualified-for-reporting"
    | "expired"
    | "unavailable";

export type ModelComponent = "demand" | "comfort" | "thermal";

export interface ReadinessCollection {
    availability: "assessed" | "not_assessed" | "unavailable";
    total: number | null;
    train: number | null;
    holdout: number | null;
    minimum_train: number | null;
    minimum_holdout: number | null;
    remaining_train: number | null;
    remaining_holdout: number | null;
    reason: string | null;
}

export interface ReadinessComponent {
    collection: ReadinessCollection;
    fit: {
        availability: "persisted" | "not_assessed" | "unavailable";
        status: string | null;
        data_cutoff?: string | null;
        artifact_hash?: string | null;
        role?: string | null;
        request_id?: string | null;
        requested_family?: string | null;
        validation?: Record<string, unknown> | null;
        reason?: string | null;
    };
    reporting: ModelReportingProgress & {
        status?: ModelReportingState;
        artifact_hash?: string | null;
        scope?: Record<string, unknown> | null;
        current_scope_match?: boolean | null;
        qualification_expires_at?: string | null;
        reason?: string | null;
    };
    next_step: string;
}

export interface SeasonalReadiness {
    as_of: string | null;
    generation: string | null;
    context_digest: string | null;
    source_watermarks: Record<string, {
        copied_rows: number;
        first_at: string | null;
        last_at: string | null;
        content_digest: string;
    }>;
    availability: "assessed" | "unavailable";
    reason: string | null;
    components: Partial<Record<"demand" | "indoor_heating" | "indoor_cooling", ReadinessComponent>>;
    maintenance: {
        enabled: boolean | null;
        preference: "automatic" | "manual" | "unknown";
        queue_count: number | null;
        running: boolean | null;
        phase: string | null;
        request_id?: string | null;
        last_attempt_at: string | null;
        next_due_at: string | null;
        reason: string | null;
    };
}

export interface ModelReportingProgress {
    last_valid_outcome_at?: string | null;
    maintenance_enabled?: boolean | null;
    cadence_hours?: number | null;
    cadence_reason?: string | null;
    next_cadence_at?: string | null;
    evidence_for?: string | null;
    evidence_as_of?: string | null;
    evidence_reason?: string | null;
    counts?: {
        pairs: number;
        device_day_clusters: number;
        issue_cohorts: number;
        horizons: Record<string, number>;
        outdoor_bins: Record<string, number>;
    } | null;
    missing_counts?: {
        paired_outcomes: number;
        device_day_clusters: number;
        issue_cohorts: number;
        elapsed_days: number | null;
    } | null;
    missing_diversity?: {
        populated_outdoor_bins: number;
        pairs_per_horizon: Record<string, number>;
        pairs_per_outdoor_bin: Record<string, number>;
    } | null;
    exclusion_counts?: Record<string, number>;
    pending_outcomes?: number | null;
    next_decision_checkpoint?: { index: number; at: string; due: boolean; policy: string } | null;
    last_decision?: { index: number; status: string | null; reason: string | null } | null;
}

export interface ModelLifecycleStatus extends ModelReportingProgress {
    status: ModelReportingState;
    incumbent_status?: ModelReportingState;
    candidate_status?: ModelReportingState | null;
    candidate_progress?: ModelReportingProgress & { status: ModelReportingState; control_ready: false } | null;
    last_trained?: string | null;
    samples?: number;
    last_valid_outcome_at?: string | null;
    qualification_expires_at?: string | null;
    qualification_scope?: { horizons?: number[]; capabilities?: string[] };
    control_ready: false;
    ordinary_writes_allowed: false;
    control_readiness?: { ready: false; reason: string };
}

export type ModelLifecycleStatuses = Partial<Record<ModelComponent, ModelLifecycleStatus>>;

export type ControlStateName =
    | "paused_by_user"
    | "observing"
    | "holding"
    | "comfort_at_risk"
    | "automatic";

export interface ControlStateNotice {
    code: string;
    severity: "info" | "warning" | "danger";
    detail: string;
}

export interface ControlStateAction {
    kind: "link" | "request";
    label: string;
    href?: string | null;
    endpoint?: string | null;
    method?: string | null;
}

export interface ControlState {
    state: ControlStateName;
    headline: string;
    detail: string;
    reason_code: string;
    since: string | null;
    until: string | null;
    override_id: number | null;
    active_override_count: number;
    primary_action: ControlStateAction | null;
    notices: ControlStateNotice[];
    resolved_at: string;
    model_lifecycle?: ModelLifecycleStatuses;
    build_policy?: "observe_only";
    control_ready?: false;
    ordinary_writes_allowed?: false;
    optimization_requires_new_build?: true;
}

export interface SpaceHeatingGate {
    state: "ALLOWED" | "BLOCKED" | "UNKNOWN";
    reason: string;
    profile_id: string;
    on_operator: string;
    off_operator: string;
    base_c: number;
    on_threshold_c: number;
    off_threshold_c: number;
    last_raw_outdoor_c: number | null;
    fingerprint_matches: boolean;
}

export interface ReadQuotaResponse {
    enabled: boolean;
    reliable: boolean;
    remaining: number | null;
    capacity: number;
    manual_required: number;
    retry_after_seconds: number;
    counters: Record<"status" | "consumption" | "weekly_timer" | "manual", number>;
    observed_at: string;
}
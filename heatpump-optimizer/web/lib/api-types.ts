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
}
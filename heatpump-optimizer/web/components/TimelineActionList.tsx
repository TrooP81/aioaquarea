"use client";

import { ACTION_LABELS, STATUS_DISPLAY } from "@/lib/constants";
import { actionReason, type TimelineAction } from "@/lib/timeline-data";
import { summariseActivity, type PlanActivity } from "./PlanActivityTimeline";
import { useTimeFormat, formatTime } from "./useTimeFormat";

export function TimelineActionList({ actions, activePlanId }: { actions: TimelineAction[]; activePlanId: number | null | undefined }) {
    const time = useTimeFormat();
    if (!actions.length) return <section className="plan-section"><h2 className="chart-title">Actions and outcomes</h2><p className="chart-caption">No planned actions or recorded outcomes in this window.</p></section>;
    const entries = summariseActivity(actions.map((action): PlanActivity => ({
        ...action,
        plan_id: action.plan_id ?? activePlanId ?? 0,
        plan_created_at: "",
        optimizer_version: "",
        executed_at: action.executed_at ?? null,
        result: action.result ?? null,
        lateness_seconds: null,
    })));
    return <section className="plan-section timeline-action-list"><h2 className="chart-title">Actions and outcomes</h2><ol>{entries.map((entry) => {
        if (entry.kind === "replacement") return <li key={`replacement-${entry.planId}`}><strong>Plan replaced</strong><span className="plan-action-status skipped">Cancelled</span><p>{entry.cancelled.length} pending action{entry.cancelled.length === 1 ? "" : "s"} cancelled before a newer plan became active.</p></li>;
        const action = entry.action; const status = STATUS_DISPLAY[action.status] || { text: action.status, className: "" }; const occurredAt = action.executed_at || action.scheduled_ts; const historical = activePlanId != null && action.plan_id !== activePlanId;
        return <li key={action.id}><time>{formatTime(new Date(occurredAt), time.hour12)}</time><strong>{ACTION_LABELS[action.action_type]?.label || action.action_type}</strong><span className={`plan-action-status ${status.className}`}>{status.text}</span>{historical && <span className="timeline-historical-outcome">Historical outcome (Plan #{action.plan_id})</span>}<p>{actionReason(action)}</p></li>;
    })}</ol></section>;
}
"use client";

import { useEffect, useRef, useState } from "react";
import { useRefresh } from "./RefreshContext";
import { ExplanationTimeline } from "./ExplanationTimeline";
import { TimelineActionList } from "./TimelineActionList";
import { TimelineDataTable } from "./TimelineDataTable";
import type { PlanActivity } from "./PlanActivityTimeline";
import { buildTimelineData, type TimelineAction } from "@/lib/timeline-data";
import type { ControlState } from "@/lib/api-types";

interface TimelineState { prices: Array<{ ts: string; price_eur_per_kwh?: number | null }>; indoor: Array<{ timestamp: string; temperature?: number | null }>; forecast: Record<string, unknown> | null; actions: TimelineAction[]; timelineActions: TimelineAction[]; activity: PlanActivity[]; comfortMin?: number; comfortMax?: number; errors: string[]; }
const EMPTY: TimelineState = { prices: [], indoor: [], forecast: null, actions: [], timelineActions: [], activity: [], errors: [] };
type TimelineSource = "prices" | "indoor" | "forecast" | "activity" | "plan" | "settings";

async function json(path: string, signal: AbortSignal): Promise<unknown> { const response = await fetch(path, { signal }); if (!response.ok) throw new Error(`${path} (${response.status})`); return response.json(); }
function setting(settings: unknown, key: string): number | undefined { const value = settings && typeof settings === "object" ? (settings as Record<string, { value?: string }>)[key]?.value : undefined; const parsed = Number(value); return Number.isFinite(parsed) ? parsed : undefined; }
const ACTIVITY_STATUSES = ["executed", "executed_unverified", "failed", "expired", "skipped", "skipped_peak", "cancelled", "pending", "executing", "dispatched"];

function timelineActions(actions: TimelineAction[], activity: PlanActivity[]): TimelineAction[] {
    const byId = new Map(actions.map((action) => [action.id, action]));
    // Activity outcomes are the fresher record when an action exists in both sources.
    activity.forEach((action) => byId.set(action.id, action));
    return [...byId.values()];
}

export function TimelineWorkspace({ planId, controlState, onActivity, onActions }: { planId: number | null | undefined; controlState: ControlState | null; onActivity?: (actions: PlanActivity[], error: string | null, loading: boolean) => void; onActions?: (actions: TimelineAction[], error: string | null, loading: boolean) => void }) {
    const { refreshEpoch } = useRefresh();
    const [state, setState] = useState<TimelineState>(EMPTY);
    const [loading, setLoading] = useState(true);
    const generation = useRef(0);
    const activityCallback = useRef(onActivity);
    const actionsCallback = useRef(onActions);
    activityCallback.current = onActivity;
    actionsCallback.current = onActions;
    useEffect(() => {
        const controller = new AbortController(); const current = ++generation.current; setLoading(true); activityCallback.current?.([], null, true); actionsCallback.current?.([], null, true);
        const activityQuery = new URLSearchParams({ limit: "200" }); ACTIVITY_STATUSES.forEach((status) => activityQuery.append("status", status));
        const requests: Array<[TimelineSource, Promise<unknown>]> = [["prices", json("/api/prices?hours=48", controller.signal)], ["indoor", json("/api/indoor-temp?hours=24", controller.signal)], ["forecast", json("/api/thermal/indoor-forecast?hours=24", controller.signal)], ["activity", json(`/api/plan-activity?${activityQuery}`, controller.signal)], ["settings", json("/api/settings", controller.signal)]];
        if (planId != null) requests.push(["plan", json(`/api/plans/${planId}`, controller.signal)]);
        void Promise.allSettled(requests.map(([, request]) => request)).then((results) => { if (controller.signal.aborted || current !== generation.current) return; const next: TimelineState = { ...EMPTY, errors: [] }; results.forEach((result, index) => { const [key] = requests[index]; if (result.status === "rejected") { next.errors.push(`${key} unavailable`); return; } if (key === "settings") { next.comfortMin = setting(result.value, "comfort_temp_min"); next.comfortMax = setting(result.value, "comfort_temp_max"); } else if (key === "activity" && Array.isArray(result.value)) next.activity = result.value as PlanActivity[]; else if (key === "plan" && result.value && typeof result.value === "object") next.actions = (((result.value as { actions?: TimelineAction[] }).actions) || []).map((action) => ({ ...action, plan_id: planId ?? undefined })); else if (key === "prices" || key === "indoor" || key === "forecast") (next[key] as unknown) = result.value; }); next.timelineActions = timelineActions(next.actions, next.activity); const activityError = next.errors.includes("activity unavailable") ? "Failed to load recent activity" : null; const actionsError = next.errors.includes("plan unavailable") ? "Failed to load plan actions" : null; setState(next); setLoading(false); activityCallback.current?.(next.activity, activityError, false); actionsCallback.current?.(next.actions, actionsError, false); });
        return () => controller.abort();
    }, [planId, refreshEpoch]);
    const points = buildTimelineData({ prices: state.prices, indoor: state.indoor, forecast: state.forecast as never, comfortMin: state.comfortMin, comfortMax: state.comfortMax });
    return <>{state.errors.length > 0 && <p className="timeline-source-errors" role="status">Unavailable: {state.errors.join(", ")}. Other timeline data remains visible.</p>}{loading && points.length === 0 ? <section className="plan-section"><div className="chart-skeleton" /></section> : <><ExplanationTimeline points={points} actions={state.timelineActions} forecast={state.forecast} controlState={controlState} /><TimelineActionList actions={state.timelineActions} activePlanId={planId} /><TimelineDataTable points={points} actions={state.timelineActions} /></>}</>;
}
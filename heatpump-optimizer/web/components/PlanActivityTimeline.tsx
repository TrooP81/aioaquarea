"use client";

import { useEffect, useRef, useState } from "react";
import { ACTION_LABELS, LAYER_LABELS, STATUS_DISPLAY, formatTime } from "@/lib/constants";
import { useRefresh } from "./RefreshContext";
import { useTimeFormat } from "./useTimeFormat";

export interface PlanActivity {
  id: number;
  plan_id: number;
  plan_created_at: string;
  optimizer_version: string;
  scheduled_ts: string;
  action_type: string;
  status: string;
  executed_at: string | null;
  lateness_seconds: number | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
}

const SUCCESS_STATUSES = new Set(["executed", "executed_unverified"]);
const OUTCOME_STATUSES = [
  "executed",
  "executed_unverified",
  "failed",
  "expired",
  "skipped",
  "skipped_peak",
  "cancelled",
];
const SAFETY_STATUSES = ["pending", "executing", "dispatched"];
const ACTIVITY_FILTERS = ["meaningful", "failed", "executed", "safety", "all"] as const;

type ActivityFilter = (typeof ACTIVITY_FILTERS)[number];

function locationFilter(): ActivityFilter {
  const requested = new URLSearchParams(window.location.search).get("activity");
  return ACTIVITY_FILTERS.includes(requested as ActivityFilter)
    ? requested as ActivityFilter
    : "meaningful";
}

function statusesForFilter(filter: ActivityFilter): string[] {
  if (filter === "failed") return ["failed", "expired"];
  if (filter === "executed") return ["executed", "executed_unverified"];
  if (filter === "safety") return SAFETY_STATUSES;
  if (filter === "all") return [...OUTCOME_STATUSES, ...SAFETY_STATUSES];
  return OUTCOME_STATUSES;
}

export type TimelineEntry =
  | { kind: "action"; action: PlanActivity }
  | { kind: "replacement"; planId: number; cancelled: PlanActivity[] };

export function summariseActivity(activity: PlanActivity[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  const replacements = new Map<number, Extract<TimelineEntry, { kind: "replacement" }>>();

  for (const item of activity) {
    const wasSuperseded = item.status === "cancelled" && item.result?.reason === "superseded";
    if (!wasSuperseded) {
      entries.push({ kind: "action", action: item });
      continue;
    }

    let replacement = replacements.get(item.plan_id);
    if (!replacement) {
      replacement = { kind: "replacement", planId: item.plan_id, cancelled: [] };
      replacements.set(item.plan_id, replacement);
      entries.push(replacement);
    }
    replacement.cancelled.push(item);
  }

  return entries;
}

function activityDetail(activity: PlanActivity): string {
  if (SUCCESS_STATUSES.has(activity.status)) {
    return activity.result?.verified === true
      ? "Command completed and verified"
      : "Command completed";
  }

  if (activity.status === "failed") {
    const error = activity.result?.error;
    return typeof error === "string" ? error : "Command could not be completed";
  }

  if (activity.status === "skipped") return "Skipped by the optimizer";
  if (activity.status === "cancelled") {
    const detail = activity.result?.detail;
    if (typeof detail === "string" && detail.trim().length > 0) return detail;

    const reason = activity.result?.reason;
    if (typeof reason === "string" && reason.trim().length > 0) {
      return reason.replace(/_/g, " ");
    }

    return "Cancelled by the optimizer";
  }
  return "Recorded by the optimizer";
}

function formatActivityDate(iso: string, hour12: boolean): string {
  return new Date(iso).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12,
  });
}

export function PlanActivityTimeline({ activityData, activityError, activityLoading }: { activityData?: PlanActivity[]; activityError?: string | null; activityLoading?: boolean } = {}) {
  const { refreshEpoch } = useRefresh();
  const [activity, setActivity] = useState<PlanActivity[]>([]);
  const [filter, setFilter] = useState<ActivityFilter>(locationFilter);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deepLinkTarget, setDeepLinkTarget] = useState<string | null>(null);
  const [deepLinkNotice, setDeepLinkNotice] = useState<string | null>(null);
  const [deepLinkHash, setDeepLinkHash] = useState(() => window.location.hash);
  const highlightedHash = useRef<string | null>(null);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    if (activityData) return;
    const controller = new AbortController();

    const loadActivity = async () => {
      try {
        await Promise.resolve();
        if (controller.signal.aborted) return;
        const params = new URLSearchParams({ limit: "200" });
        statusesForFilter(filter).forEach((status) => params.append("status", status));
        const response = await fetch(`/api/plan-activity?${params}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`API error (${response.status})`);
        const data: PlanActivity[] = await response.json();
        if (controller.signal.aborted) return;
        setActivity(data);
        setError(null);
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "Failed to load recent activity");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    void loadActivity();
    return () => {
      controller.abort();
    };
  }, [activityData, filter, refreshEpoch]);

  const displayedActivity = activityData ?? activity;
  const displayedError = activityError ?? error;
  const displayedLoading = activityLoading ?? loading;

  useEffect(() => {
    const applyFilter = () => setFilter(locationFilter());
    window.addEventListener("popstate", applyFilter);
    return () => window.removeEventListener("popstate", applyFilter);
  }, []);

  useEffect(() => {
    const applyHash = () => {
      highlightedHash.current = null;
      setDeepLinkTarget(null);
      setDeepLinkNotice(null);
      setDeepLinkHash(window.location.hash);
    };
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, []);

  useEffect(() => {
    const hash = deepLinkHash;
    const match = /^#plan-action-(\d+)$/.exec(hash);
    if (!match || displayedLoading || displayedError || highlightedHash.current === hash) return;

    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(`plan-action-${match[1]}`);
      if (!target) {
        setDeepLinkNotice("Linked action is no longer in recent activity.");
        return;
      }
      highlightedHash.current = hash;
      setDeepLinkNotice(null);
      setDeepLinkTarget(target.id);
      target.scrollIntoView({ block: "center" });
    });
    const timeout = window.setTimeout(() => setDeepLinkTarget(null), 8_000);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timeout);
    };
  }, [deepLinkHash, displayedError, displayedLoading, displayedActivity]);

  useEffect(() => {
    if (!deepLinkTarget) return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(deepLinkTarget)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [deepLinkTarget]);

  if (displayedLoading) {
    return (
      <section className="plan-section">
        <h2 className="chart-title">Recent Activity</h2>
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "75%" }} />
        </div>
      </section>
    );
  }

  if (displayedError) {
    return (
      <section className="plan-section">
        <h2 className="chart-title">Recent Activity</h2>
        <p className="plan-error">Could not load recent activity: {displayedError}</p>
      </section>
    );
  }

  if (displayedActivity.length === 0) {
    return (
      <section className="plan-section">
        <h2 className="chart-title">Recent Activity</h2>
        {deepLinkNotice && <p className="plan-error" role="status">{deepLinkNotice}</p>}
        <p className="chart-caption">No completed, failed, skipped, or replaced optimizer actions yet.</p>
      </section>
    );
  }

  return (
    <section className="plan-section" data-testid="plan-activity">
      <div className="plan-history-heading">
        <div>
          <h2 className="chart-title">What actually happened</h2>
          <p className="chart-caption">
            Executed, failed, and user-relevant outcomes. Routine replacements are grouped.
          </p>
        </div>
        <div className="activity-filters" aria-label="Filter plan activity">
          {ACTIVITY_FILTERS.map((value) => (
            <button
              key={value}
              className={`btn btn-sm ${filter === value ? "btn-primary" : ""}`}
              onClick={() => setFilter(value)}
              aria-pressed={filter === value}
            >
              {value === "meaningful" ? "Outcome" : value[0].toUpperCase() + value.slice(1)}
            </button>
          ))}
        </div>
      </div>
      {deepLinkNotice && <p className="plan-error" role="status">{deepLinkNotice}</p>}
      <ol className="plan-activity-list">
        {summariseActivity(
          displayedActivity.filter((item) => statusesForFilter(filter).includes(item.status)),
        ).map((entry) => {
          if (entry.kind === "replacement") {
            const occurredAt = entry.cancelled[0].executed_at || entry.cancelled[0].scheduled_ts;
            return (
              <li key={`replacement-${entry.planId}`} className="plan-activity-item">
                <span className="plan-activity-marker skipped" aria-hidden="true" />
                <time className="plan-activity-time" dateTime={occurredAt}>
                  {formatActivityDate(occurredAt, timeFormat.hour12)}
                </time>
                <div className="plan-activity-card">
                  <div className="plan-activity-main">
                    <strong>↻ Plan replaced</strong>
                    <span className="plan-action-status skipped">Cancelled</span>
                  </div>
                  <p>{entry.cancelled.length} pending action{entry.cancelled.length === 1 ? "" : "s"} cancelled before a newer plan became active.</p>
                  <div className="plan-activity-meta"><span>Plan #{entry.planId}</span></div>
                </div>
              </li>
            );
          }

          const item = entry.action;
          const info = ACTION_LABELS[item.action_type];
          const status = STATUS_DISPLAY[item.status] || { text: item.status, className: "" };
          const occurredAt = item.executed_at || item.scheduled_ts;
          const layer = LAYER_LABELS[item.optimizer_version] || item.optimizer_version;
          return (
            <li
              key={item.id}
              id={`plan-action-${item.id}`}
              className={`plan-activity-item ${deepLinkTarget === `plan-action-${item.id}` ? "deep-link-target" : ""}`}
              tabIndex={deepLinkTarget === `plan-action-${item.id}` ? -1 : undefined}
              aria-current={deepLinkTarget === `plan-action-${item.id}` ? "true" : undefined}
            >
              <span className={`plan-activity-marker ${status.className}`} aria-hidden="true" />
              <time className="plan-activity-time" dateTime={occurredAt}>
                {formatActivityDate(occurredAt, timeFormat.hour12)}
              </time>
              <div className="plan-activity-card">
                <div className="plan-activity-main">
                  <strong>
                    {info ? <><span role="img" aria-label={info.label}>{info.emoji}</span> {info.label}</> : item.action_type}
                  </strong>
                  <span className={`plan-action-status ${status.className}`}>{status.text}</span>
                </div>
                <p>{activityDetail(item)}</p>
                <div className="plan-activity-meta">
                  <span>Plan #{item.plan_id}</span>
                  <span>{layer}</span>
                  {item.executed_at && <span>Scheduled {formatTime(item.scheduled_ts, timeFormat.hour12)}</span>}
                  {item.lateness_seconds != null && <span>{item.lateness_seconds <= 120 ? "On time" : `${Math.ceil(item.lateness_seconds / 60)} min late`}</span>}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

"use client";

import { useEffect, useState } from "react";
import { ACTION_LABELS, LAYER_LABELS, STATUS_DISPLAY, formatTime } from "@/lib/constants";
import { useTimeFormat } from "./useTimeFormat";

interface PlanActivity {
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

type TimelineEntry =
  | { kind: "action"; action: PlanActivity }
  | { kind: "replacement"; planId: number; cancelled: PlanActivity[] };

function summariseActivity(activity: PlanActivity[]): TimelineEntry[] {
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
  if (activity.status === "cancelled") return "Cancelled because a newer plan replaced this one";
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

export function PlanActivityTimeline() {
  const [activity, setActivity] = useState<PlanActivity[]>([]);
  const [filter, setFilter] = useState<"meaningful" | "failed" | "executed" | "all">("meaningful");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    let active = true;

    const loadActivity = async () => {
      try {
        const statuses = filter === "failed"
          ? "&status=failed&status=expired"
          : filter === "executed"
            ? "&status=executed&status=executed_unverified"
            : "";
        const response = await fetch(`/api/plan-activity?limit=100${statuses}`);
        if (!response.ok) throw new Error(`API error (${response.status})`);
        const data: PlanActivity[] = await response.json();
        if (active) {
          setActivity(data);
          setError(null);
          window.setTimeout(() => {
            if (window.location.hash.startsWith("#plan-action-")) {
              document.querySelector(window.location.hash)?.scrollIntoView({ block: "center" });
            }
          }, 0);
        }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Failed to load recent activity");
      } finally {
        if (active) setLoading(false);
      }
    };

    loadActivity();
    const interval = window.setInterval(loadActivity, 30_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [filter]);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("activity");
    if (requested === "failed" || requested === "executed" || requested === "all") {
      setFilter(requested);
    }
  }, []);

  if (loading) {
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

  if (error) {
    return (
      <section className="plan-section">
        <h2 className="chart-title">Recent Activity</h2>
        <p className="plan-error">Could not load recent activity: {error}</p>
      </section>
    );
  }

  if (activity.length === 0) {
    return (
      <section className="plan-section">
        <h2 className="chart-title">Recent Activity</h2>
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
          {(["meaningful", "failed", "executed", "all"] as const).map((value) => (
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
      <ol className="plan-activity-list">
        {summariseActivity(
          filter === "meaningful"
            ? activity.filter((item) => item.status !== "cancelled" || item.result?.reason !== "superseded")
            : activity,
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
            <li key={item.id} id={`plan-action-${item.id}`} className="plan-activity-item">
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

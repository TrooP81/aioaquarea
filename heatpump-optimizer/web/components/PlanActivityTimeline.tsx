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
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
}

const SUCCESS_STATUSES = new Set(["executed", "executed_unverified"]);

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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    let active = true;

    const loadActivity = async () => {
      try {
        const response = await fetch("/api/plan-activity?limit=25");
        if (!response.ok) throw new Error(`API error (${response.status})`);
        const data: PlanActivity[] = await response.json();
        if (active) {
          setActivity(data);
          setError(null);
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
        <p className="chart-caption">No completed, failed, or skipped optimizer actions yet.</p>
      </section>
    );
  }

  return (
    <section className="plan-section" data-testid="plan-activity">
      <h2 className="chart-title">Recent Activity</h2>
      <p className="chart-caption">
        What the optimizer actually did. Pending actions stay in the current plan; every generated plan is available below.
      </p>
      <ol className="plan-activity-list">
        {activity.map((item) => {
          const info = ACTION_LABELS[item.action_type];
          const status = STATUS_DISPLAY[item.status] || { text: item.status, className: "" };
          const occurredAt = item.executed_at || item.scheduled_ts;
          const layer = LAYER_LABELS[item.optimizer_version] || item.optimizer_version;
          return (
            <li key={item.id} className="plan-activity-item">
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
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

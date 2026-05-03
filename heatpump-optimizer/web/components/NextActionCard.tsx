"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";
import { useTimeFormat } from "./useTimeFormat";
import { ACTION_LABELS, LAYER_LABELS, LAYER_TOOLTIPS, formatRelativeTime, formatTime } from "@/lib/constants";

interface NextActionCardProps {
  plan: {
    id: number;
    optimizer_version: string;
    cost_estimate_eur: number | null;
    actions_count: number;
    horizon_start?: string;
    horizon_end?: string;
  } | null;
}

interface PlanAction {
  id: number;
  scheduled_ts: string;
  action_type: string;
  payload: Record<string, any>;
  status: string;
}

export function NextActionCard({ plan }: NextActionCardProps) {
  const [nextAction, setNextAction] = useState<PlanAction | null>(null);
  const [relTime, setRelTime] = useState("");
  const currency = useCurrency();
  const timeFormat = useTimeFormat();

  useEffect(() => {
    if (!plan?.id) {
      setNextAction(null);
      return;
    }
    fetch(`/api/plans/${plan.id}`)
      .then((r) => r.json())
      .then((data) => {
        const now = new Date();
        const pending = (data.actions || []).find(
          (a: PlanAction) => a.status === "pending" && new Date(a.scheduled_ts) > now
        );
        setNextAction(pending || null);
      })
      .catch(() => setNextAction(null));
  }, [plan?.id]);

  // Update relative time every 30 seconds
  useEffect(() => {
    if (!nextAction) return;
    const update = () => setRelTime(formatRelativeTime(nextAction.scheduled_ts));
    update();
    const interval = setInterval(update, 30000);
    return () => clearInterval(interval);
  }, [nextAction?.scheduled_ts]);

  if (!plan) {
    return (
      <div className="card next-action-card next-action-card--empty">
        <div className="card-header">
          <span className="card-title">Next Action</span>
        </div>
        <div className="card-value" style={{ fontSize: "1rem", color: "var(--text-muted)" }}>
          No active plan
        </div>
        <div className="card-subtitle">
          Optimizer will generate one when price data is available.
        </div>
      </div>
    );
  }

  const info = nextAction ? ACTION_LABELS[nextAction.action_type] : null;

  return (
    <div className="card next-action-card">
      <div className="card-header">
        <span className="card-title">Next Action</span>
        {nextAction && (
          <span className="next-action-countdown">{relTime}</span>
        )}
      </div>
      {nextAction ? (
        <>
          <div className="next-action-main">
            <span role="img" aria-label={info?.label || nextAction.action_type} className="next-action-emoji">
              {info?.emoji || "⚡"}
            </span>
            <span className="next-action-label">
              {info?.label || nextAction.action_type}
            </span>
          </div>
          <div className="card-subtitle">
            at {formatTime(nextAction.scheduled_ts, timeFormat.hour12)} · Plan est. cost: {formatCost(plan.cost_estimate_eur, currency)}
          </div>
        </>
      ) : (
        <>
          <div className="card-value" style={{ fontSize: "1.25rem" }}>
            All caught up
          </div>
          <div className="card-subtitle">
            No pending actions · Plan est. cost: {formatCost(plan.cost_estimate_eur, currency)}
          </div>
        </>
      )}
      <div className="next-action-layer">
        <span
          title={LAYER_TOOLTIPS[plan.optimizer_version] || plan.optimizer_version}
          className={`opt-layer-badge ${plan.optimizer_version.includes("ml") ? "opt-layer-badge--ml" : plan.optimizer_version.includes("milp") ? "opt-layer-badge--milp" : ""}`}
        >
          {LAYER_LABELS[plan.optimizer_version] || plan.optimizer_version}
        </span>
      </div>
    </div>
  );
}

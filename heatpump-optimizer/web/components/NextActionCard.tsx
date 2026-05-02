"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";

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

const ACTION_LABELS: Record<string, { emoji: string; label: string }> = {
  force_dhw_on: { emoji: "🔥", label: "Hot Water Heating ON" },
  force_dhw_off: { emoji: "⏹", label: "Hot Water Heating OFF" },
  quiet_mode_on: { emoji: "🤫", label: "Quiet Mode ON" },
  quiet_mode_off: { emoji: "🔊", label: "Quiet Mode OFF" },
  zone_temp_boost: { emoji: "⬆️", label: "Zone Temp Boost +2 °C" },
  zone_temp_restore: { emoji: "↩️", label: "Zone Temp Restore" },
  set_tank_temp: { emoji: "🌡️", label: "Set Tank Temperature" },
  eco_mode: { emoji: "🌿", label: "Eco Mode" },
  comfort_mode: { emoji: "☀️", label: "Comfort Mode" },
};

function formatRelativeTime(iso: string): string {
  const diffMs = new Date(iso).getTime() - Date.now();
  if (diffMs < 0) return "now";
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "< 1 min";
  if (mins < 60) return `in ${mins} min`;
  const hrs = Math.floor(mins / 60);
  const remainMins = mins % 60;
  if (remainMins === 0) return `in ${hrs} h`;
  return `in ${hrs} h ${remainMins} min`;
}

function formatTime(iso: string | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function NextActionCard({ plan }: NextActionCardProps) {
  const [nextAction, setNextAction] = useState<PlanAction | null>(null);
  const [relTime, setRelTime] = useState("");
  const currency = useCurrency();

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
            at {formatTime(nextAction.scheduled_ts)} · Est. cost: {formatCost(plan.cost_estimate_eur, currency)}
          </div>
        </>
      ) : (
        <>
          <div className="card-value" style={{ fontSize: "1.25rem" }}>
            All caught up
          </div>
          <div className="card-subtitle">
            No pending actions · Est. cost: {formatCost(plan.cost_estimate_eur, currency)}
          </div>
        </>
      )}
    </div>
  );
}

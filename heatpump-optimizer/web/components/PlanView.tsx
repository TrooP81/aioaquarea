"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";

interface PlanProps {
  plan: {
    id: number;
    optimizer_version: string;
    cost_estimate_eur: number | null;
    actions_count: number;
    horizon_start?: string;
    horizon_end?: string;
    created_at?: string;
  } | null;
}

interface PlanAction {
  id: number;
  scheduled_ts: string;
  action_type: string;
  payload: Record<string, any>;
  status: string;
  executed_at: string | null;
}

const LAYER_LABELS: Record<string, string> = {
  rules_v3: "Rules Engine",
  milp_v1: "MILP Optimizer",
  "milp_v1+ml": "MILP + ML",
};

function LayerBadge({ version }: { version: string }) {
  const isMl = version.includes("ml");
  const isMilp = version.includes("milp");
  return (
    <span
      style={{
        padding: "0.2rem 0.6rem",
        borderRadius: "9999px",
        fontSize: "0.7rem",
        fontWeight: 600,
        background: isMl
          ? "rgba(34,197,94,0.15)"
          : isMilp
          ? "rgba(59,130,246,0.15)"
          : "rgba(148,163,184,0.15)",
        color: isMl
          ? "var(--success)"
          : isMilp
          ? "var(--accent)"
          : "var(--text-muted)",
      }}
    >
      {LAYER_LABELS[version] || version}
    </span>
  );
}

function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.5rem" }}>
      <div
        style={{
          flex: 1,
          height: 6,
          borderRadius: 3,
          background: "var(--bg)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            borderRadius: 3,
            background: pct === 100 ? "var(--success)" : "var(--accent)",
            transition: "width 0.3s",
          }}
        />
      </div>
      <span style={{ fontSize: "0.7rem", color: "var(--text-muted)", whiteSpace: "nowrap" }}>
        {completed}/{total} done
      </span>
    </div>
  );
}

export function PlanView({ plan }: PlanProps) {
  const [actions, setActions] = useState<PlanAction[]>([]);
  const currency = useCurrency();

  useEffect(() => {
    if (plan?.id) {
      fetch(`/api/plans/${plan.id}`)
        .then((r) => r.json())
        .then((data) => setActions(data.actions || []))
        .catch(() => {});
    }
  }, [plan?.id]);

  const formatAction = (type: string) => {
    const labels: Record<string, string> = {
      force_dhw_on: "🔥 DHW Heating ON",
      force_dhw_off: "⏹ DHW Heating OFF",
      quiet_mode_on: "🤫 Quiet Mode ON",
      quiet_mode_off: "🔊 Quiet Mode OFF",
      zone_temp_boost: "⬆️ Zone Temp +2°C",
      zone_temp_restore: "↩️ Zone Temp Restore",
      set_tank_temp: "🌡️ Set Tank Temp",
      eco_mode: "🌿 Eco Mode",
      comfort_mode: "☀️ Comfort Mode",
    };
    return labels[type] || type;
  };

  const formatTime = (iso: string | undefined) => {
    if (!iso) return "—";
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  };

  const completedCount = actions.filter((a) => a.status === "executed" || a.status === "executed_unverified").length;
  const pendingCount = actions.filter((a) => a.status === "pending").length;
  const now = new Date();

  // Determine next upcoming action
  const nextAction = actions.find(
    (a) => a.status === "pending" && new Date(a.scheduled_ts) > now
  );

  return (
    <div className="plan-section">
      <div className="plan-header">
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.25rem" }}>
            <h2 className="chart-title" style={{ margin: 0 }}>Active Plan</h2>
            {plan && <LayerBadge version={plan.optimizer_version} />}
          </div>
          {plan ? (
            <>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "1.5rem", color: "var(--text-muted)", fontSize: "0.8rem", marginTop: "0.5rem" }}>
                <span>Est. cost: <strong style={{ color: "var(--text)" }}>{formatCost(plan.cost_estimate_eur, currency)}</strong></span>
                <span>Horizon: {formatTime(plan.horizon_start)} – {formatTime(plan.horizon_end)}</span>
                <span>{plan.actions_count} actions ({completedCount} done, {pendingCount} pending)</span>
              </div>
              <ProgressBar completed={completedCount} total={plan.actions_count} />
            </>
          ) : (
            <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginTop: "0.25rem" }}>
              No active plan. Optimizer will generate one when price data is available.
            </p>
          )}
        </div>
      </div>

      {/* Next action highlight */}
      {nextAction && (
        <div
          style={{
            marginTop: "1rem",
            padding: "0.75rem 1rem",
            background: "rgba(59,130,246,0.08)",
            borderRadius: "0.5rem",
            border: "1px solid rgba(59,130,246,0.2)",
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
          }}
        >
          <span style={{ fontSize: "0.75rem", color: "var(--accent)", fontWeight: 600 }}>NEXT</span>
          <span style={{ fontSize: "0.85rem", fontWeight: 500 }}>
            {formatAction(nextAction.action_type)}
          </span>
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginLeft: "auto" }}>
            @ {formatTime(nextAction.scheduled_ts)}
          </span>
        </div>
      )}

      {actions.length > 0 && (
        <div className="plan-actions" style={{ marginTop: "0.75rem" }}>
          {actions.slice(0, 12).map((action) => {
            const isPast = new Date(action.scheduled_ts) <= now;
            const isNext = action.id === nextAction?.id;
            return (
              <div
                key={action.id}
                className="plan-action"
                style={{
                  opacity: action.status === "executed" || action.status === "executed_unverified" ? 0.6 : 1,
                  borderLeft: isNext ? "3px solid var(--accent)" : "3px solid transparent",
                  paddingLeft: "0.5rem",
                }}
              >
                <span className="plan-action-time">
                  {formatTime(action.scheduled_ts)}
                </span>
                <span className="plan-action-type">
                  {formatAction(action.action_type)}
                </span>
                {action.payload && Object.keys(action.payload).length > 0 && (
                  <span style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                    {Object.entries(action.payload).map(([k, v]) => `${k}: ${v}`).join(", ")}
                  </span>
                )}
                <span className={`plan-action-status ${action.status}`} style={{ marginLeft: "auto" }}>
                  {action.status === "executed" ? "✓" : action.status === "executed_unverified" ? "✓?" : action.status}
                </span>
              </div>
            );
          })}
          {actions.length > 12 && (
            <p style={{ color: "var(--text-muted)", fontSize: "0.75rem", paddingTop: "0.5rem" }}>
              + {actions.length - 12} more actions
            </p>
          )}
        </div>
      )}
    </div>
  );
}

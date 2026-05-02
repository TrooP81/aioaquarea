"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";

interface PlanSummary {
  id: number;
  created_at: string;
  horizon_start: string;
  horizon_end: string;
  optimizer_version: string;
  cost_estimate_eur: number | null;
  actions_count: number;
}

interface PlanAction {
  id: number;
  scheduled_ts: string;
  action_type: string;
  payload: Record<string, any>;
  status: string;
  executed_at: string | null;
}

const ACTION_LABELS: Record<string, { emoji: string; label: string }> = {
  force_dhw_on:     { emoji: "🔥", label: "Hot Water Heating ON" },
  force_dhw_off:    { emoji: "⏹", label: "Hot Water Heating OFF" },
  quiet_mode_on:    { emoji: "🤫", label: "Quiet Mode ON" },
  quiet_mode_off:   { emoji: "🔊", label: "Quiet Mode OFF" },
  zone_temp_boost:  { emoji: "⬆️", label: "Zone Temp Boost +2 °C" },
  zone_temp_restore:{ emoji: "↩️", label: "Zone Temp Restore" },
  set_tank_temp:    { emoji: "🌡️", label: "Set Tank Temperature" },
  eco_mode:         { emoji: "🌿", label: "Eco Mode" },
  comfort_mode:     { emoji: "☀️", label: "Comfort Mode" },
};

const STATUS_DISPLAY: Record<string, { text: string; className: string }> = {
  pending:              { text: "Scheduled",           className: "pending" },
  executed:             { text: "Done",                className: "executed" },
  executed_unverified:  { text: "Done (unconfirmed)",  className: "executed" },
  failed:               { text: "Failed",              className: "failed" },
  skipped:              { text: "Skipped",             className: "skipped" },
};

const LAYER_LABELS: Record<string, string> = {
  rules_v3: "Basic",
  milp_v1: "Smart",
  "milp_v1+ml": "Advanced",
};

function formatTime(iso: string | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

function statusSummary(actions: PlanAction[]): { done: number; failed: number; skipped: number; pending: number } {
  const done = actions.filter((a) => a.status === "executed" || a.status === "executed_unverified").length;
  const failed = actions.filter((a) => a.status === "failed").length;
  const skipped = actions.filter((a) => a.status === "skipped").length;
  const pending = actions.filter((a) => a.status === "pending").length;
  return { done, failed, skipped, pending };
}

export function PlanHistory() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedActions, setExpandedActions] = useState<PlanAction[]>([]);
  const [loadingActions, setLoadingActions] = useState(false);
  const currency = useCurrency();

  useEffect(() => {
    fetch("/api/plans?limit=20")
      .then((r) => {
        if (!r.ok) throw new Error(`API error (${r.status})`);
        return r.json();
      })
      .then((data) => setPlans(data))
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load plan history"))
      .finally(() => setLoading(false));
  }, []);

  const toggleExpand = (planId: number) => {
    if (expandedId === planId) {
      setExpandedId(null);
      setExpandedActions([]);
      return;
    }
    setExpandedId(planId);
    setLoadingActions(true);
    fetch(`/api/plans/${planId}`)
      .then((r) => {
        if (!r.ok) throw new Error(`API error (${r.status})`);
        return r.json();
      })
      .then((data) => setExpandedActions(data.actions || []))
      .catch(() => setExpandedActions([]))
      .finally(() => setLoadingActions(false));
  };

  if (loading) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Plan History</h2>
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "80%" }} />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Plan History</h2>
        <div className="plan-error"><span>{error}</span></div>
      </div>
    );
  }

  if (plans.length === 0) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Plan History</h2>
        <p className="plan-empty-msg">No past plans yet.</p>
      </div>
    );
  }

  return (
    <div className="plan-section">
      <h2 className="chart-title">Plan History</h2>
      <div className="plan-history-list">
        {plans.map((plan) => {
          const isExpanded = expandedId === plan.id;
          const layerLabel = LAYER_LABELS[plan.optimizer_version] || plan.optimizer_version;
          return (
            <div key={plan.id} className="plan-history-item">
              <button
                className="plan-history-row"
                onClick={() => toggleExpand(plan.id)}
                aria-expanded={isExpanded}
              >
                <span className="plan-history-date">{formatDate(plan.created_at)}</span>
                <span className="plan-history-horizon">
                  {formatTime(plan.horizon_start)} – {formatTime(plan.horizon_end)}
                </span>
                <span className="plan-history-layer">{layerLabel}</span>
                <span className="plan-history-cost">{formatCost(plan.cost_estimate_eur, currency)}</span>
                <span className="plan-history-count">{plan.actions_count} actions</span>
                <span className="plan-history-chevron" aria-hidden="true">
                  {isExpanded ? "▾" : "▸"}
                </span>
              </button>

              {isExpanded && (
                <div className="plan-history-detail">
                  {loadingActions ? (
                    <div className="plan-loading">
                      <div className="plan-loading-skeleton" />
                      <div className="plan-loading-skeleton" style={{ width: "60%" }} />
                    </div>
                  ) : expandedActions.length === 0 ? (
                    <p className="plan-empty-msg" style={{ padding: "0.5rem 0" }}>No actions in this plan.</p>
                  ) : (
                    <>
                      {/* Summary bar */}
                      <div className="plan-history-summary">
                        {(() => {
                          const s = statusSummary(expandedActions);
                          return (
                            <>
                              {s.done > 0 && <span className="plan-action-status executed">{s.done} done</span>}
                              {s.failed > 0 && <span className="plan-action-status failed">{s.failed} failed</span>}
                              {s.skipped > 0 && <span className="plan-action-status skipped">{s.skipped} skipped</span>}
                              {s.pending > 0 && <span className="plan-action-status pending">{s.pending} scheduled</span>}
                            </>
                          );
                        })()}
                      </div>

                      {/* Action rows */}
                      {expandedActions.map((action) => {
                        const info = ACTION_LABELS[action.action_type];
                        const status = STATUS_DISPLAY[action.status] || { text: action.status, className: "" };
                        const isDone = action.status === "executed" || action.status === "executed_unverified";
                        return (
                          <div
                            key={action.id}
                            className="plan-action"
                            style={{ opacity: isDone ? 0.7 : 1 }}
                          >
                            <span className="plan-action-time">{formatTime(action.scheduled_ts)}</span>
                            <span className="plan-action-type">
                              {info ? (
                                <>
                                  <span role="img" aria-label={info.label}>{info.emoji}</span>{" "}
                                  {info.label}
                                </>
                              ) : action.action_type}
                            </span>
                            {action.executed_at && (
                              <span className="plan-history-executed-at">
                                ran {formatTime(action.executed_at)}
                              </span>
                            )}
                            <span className={`plan-action-status ${status.className}`}>{status.text}</span>
                          </div>
                        );
                      })}
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

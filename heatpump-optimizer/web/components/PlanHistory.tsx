"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";
import { useTimeFormat } from "./useTimeFormat";
import { ACTION_LABELS, LAYER_LABELS, STATUS_DISPLAY, formatTime } from "@/lib/constants";

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
  result: Record<string, any> | null;
}

function formatReason(payload: Record<string, any>): string | null {
  const reason = payload?.reason;
  if (!reason || typeof reason !== "string") return null;

  // thermal_optimized_before_7:00
  const thermalMatch = reason.match(/^thermal_optimized_before_(\d+:\d+)$/);
  if (thermalMatch) return `Cheapest slot before ${thermalMatch[1]} comfort window`;

  // peak_price_0.3000_eur_kwh
  const peakMatch = reason.match(/^peak_price_([\d.]+)_eur_kwh$/);
  if (peakMatch) return `Price peak at ${parseFloat(peakMatch[1]).toFixed(2)} /kWh`;

  // comfort_hour_but_peak_price_0.3000
  const comfortPeakMatch = reason.match(/^comfort_hour_but_peak_price_([\d.]+)$/);
  if (comfortPeakMatch) return `Comfort hour skipped — price at ${parseFloat(comfortPeakMatch[1]).toFixed(2)} /kWh`;

  // eco_hour_but_cheap_price_0.0500
  const ecoUpgradeMatch = reason.match(/^eco_hour_but_cheap_price_([\d.]+)$/);
  if (ecoUpgradeMatch) return `Eco upgraded — cheap price ${parseFloat(ecoUpgradeMatch[1]).toFixed(2)} /kWh`;

  const REASON_MAP: Record<string, string> = {
    dhw_target_reached: "Tank reached target temperature",
    thermal_preheat_before_cold: "Pre-heating before forecast cold spell",
    preheat_complete: "Pre-heat complete",
    peak_avoidance_end: "Peak price window ended",
    night_quiet_schedule: "Night quiet hours",
    night_quiet_end: "Night quiet hours ended",
    comfort_schedule: "Comfort schedule active",
    outside_comfort_schedule: "Outside comfort hours",
  };

  if (REASON_MAP[reason]) return REASON_MAP[reason];

  // Fallback: humanize underscore-separated string
  return reason.replace(/_/g, " ");
}

function formatPayloadExtras(payload: Record<string, any>): string | null {
  const extras: string[] = [];
  if (payload?.predicted_minutes != null) extras.push(`~${payload.predicted_minutes} min heating`);
  if (payload?.heating_rate != null) extras.push(`${payload.heating_rate} °C/h`);
  if (payload?.confidence != null) extras.push(`${Math.round(payload.confidence * 100)}% confidence`);
  if (payload?.offset != null) extras.push(`${payload.offset > 0 ? "+" : ""}${payload.offset} °C`);
  if (payload?.level != null) extras.push(`level ${payload.level}`);
  return extras.length > 0 ? extras.join(" · ") : null;
}

function effectiveStatus(action: PlanAction): string {
  if (action.status === "pending" && new Date(action.scheduled_ts) < new Date()) {
    return "expired";
  }
  return action.status;
}

/**
 * Explain WHY an action ended up in its current state.
 * Covers executed, failed, expired (not-executed), and still-pending actions.
 */
function statusExplanation(action: PlanAction, eStatus: string, isLatestPlan: boolean): string | null {
  const result = action.result;

  switch (eStatus) {
    case "executed":
      if (result?.verified === true) return "Sent to heat pump and verified";
      return "Sent to heat pump successfully";

    case "executed_unverified":
      return "Sent to heat pump but state change could not be confirmed";

    case "failed": {
      const err = result?.error;
      if (typeof err === "string") {
        // Humanize common errors
        if (err.includes("timeout") || err.includes("Timeout")) return "Heat pump did not respond in time";
        if (err.includes("connection") || err.includes("Connection")) return "Lost connection to heat pump";
        if (err.includes("rate") || err.includes("Rate")) return "API rate limit reached";
        return `Error: ${err}`;
      }
      return "Execution failed (no details recorded)";
    }

    case "expired": {
      // Server-side diagnosis available?
      if (result?.detail && typeof result.detail === "string") {
        return result.detail;
      }
      // Client-side fallback for actions not yet swept by the expiry task
      if (!isLatestPlan) {
        return "Superseded by a newer plan before this action's scheduled time";
      }
      return "Scheduled time passed — waiting for diagnostic sweep";
    }

    case "skipped": {
      const overrideReason = result?.override;
      if (result?.reason === "override_active" && typeof overrideReason === "string") {
        const OVERRIDE_REASONS: Record<string, string> = {
          comfort_schedule: "Comfort schedule override was active",
          night_quiet_schedule: "Night quiet override was active",
          manual: "Manual override was active",
        };
        return OVERRIDE_REASONS[overrideReason] || `Override was active: ${overrideReason.replace(/_/g, " ")}`;
      }
      return "Skipped by the optimizer";
    }

    case "pending":
      return "Waiting to execute at scheduled time";

    default:
      return null;
  }
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

function statusSummary(actions: PlanAction[]): { done: number; failed: number; skipped: number; pending: number; expired: number } {
  const statuses = actions.map(effectiveStatus);
  const done = statuses.filter((s) => s === "executed" || s === "executed_unverified").length;
  const failed = statuses.filter((s) => s === "failed").length;
  const skipped = statuses.filter((s) => s === "skipped").length;
  const pending = statuses.filter((s) => s === "pending").length;
  const expired = statuses.filter((s) => s === "expired").length;
  return { done, failed, skipped, pending, expired };
}

export function PlanHistory() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedActions, setExpandedActions] = useState<PlanAction[]>([]);
  const [loadingActions, setLoadingActions] = useState(false);
  const currency = useCurrency();
  const timeFormat = useTimeFormat();

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
                  {formatTime(plan.horizon_start, timeFormat.hour12)} – {formatTime(plan.horizon_end, timeFormat.hour12)}
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
                              {s.expired > 0 && <span className="plan-action-status skipped">{s.expired} missed</span>}
                              {s.pending > 0 && <span className="plan-action-status pending">{s.pending} scheduled</span>}
                            </>
                          );
                        })()}
                      </div>

                      {/* Action rows */}
                      {expandedActions.map((action) => {
                        const info = ACTION_LABELS[action.action_type];
                        const eStatus = effectiveStatus(action);
                        const status = STATUS_DISPLAY[eStatus] || { text: eStatus, className: "" };
                        const isDone = eStatus === "executed" || eStatus === "executed_unverified";
                        const isExpired = eStatus === "expired";
                        const isFailed = eStatus === "failed";
                        const reason = formatReason(action.payload);
                        const extras = formatPayloadExtras(action.payload);
                        const isLatest = plans.length > 0 && plan.id === plans[0].id;
                        const explanation = statusExplanation(action, eStatus, isLatest);
                        return (
                          <div
                            key={action.id}
                            className="plan-action plan-action--history"
                            style={{ opacity: isDone || isExpired ? 0.5 : 1 }}
                          >
                            <div className="plan-action-main-row">
                              <span className="plan-action-time">{formatTime(action.scheduled_ts, timeFormat.hour12)}</span>
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
                                  ran {formatTime(action.executed_at, timeFormat.hour12)}
                                </span>
                              )}
                              <span className={`plan-action-status ${status.className}`}>{status.text}</span>
                            </div>
                            {(reason || extras) && (
                              <div className="plan-action-reason-row">
                                {reason && <span className="plan-action-reason">{reason}</span>}
                                {extras && <span className="plan-action-extras">{extras}</span>}
                              </div>
                            )}
                            {(isExpired || isFailed || eStatus === "skipped") && explanation && (
                              <div className="plan-action-reason-row plan-action-explanation-row">
                                <span className={`plan-action-explanation ${isFailed ? "plan-action-explanation--failed" : ""}`}>
                                  {isFailed ? "⚠ " : "ℹ "}{explanation}
                                </span>
                              </div>
                            )}
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

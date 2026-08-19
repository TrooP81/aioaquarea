"use client";

import { useCallback, useEffect, useState } from "react";
import { useCurrency, formatCostInCurrency } from "./useCurrency";
import { useTimeFormat } from "./useTimeFormat";
import { PlanAction, usePlanActions } from "./usePlanActions";
import { ACTION_LABELS, LAYER_LABELS, STATUS_DISPLAY, formatTime } from "@/lib/constants";

interface PlanSummary {
  id: number;
  created_at: string;
  horizon_start: string;
  horizon_end: string;
  optimizer_version: string;
  cost_estimate_eur: number | null;
  price_currency?: string;
  price_source?: string;
  actions_count: number;
  status: "active" | "superseded" | "completed" | string;
  status_reason: string | null;
  superseded_by_plan_id: number | null;
}

function formatReason(payload: Record<string, unknown>): string | null {
  const reason = payload?.reason;
  if (!reason || typeof reason !== "string") return null;

  // thermal_optimized_before_7:00
  const thermalMatch = reason.match(/^thermal_optimized_before_(\d+:\d+)$/);
  if (thermalMatch) return `Cheapest slot before ${thermalMatch[1]} comfort window`;

  // peak_price_0.3000_eur_kwh
  const peakMatch = reason.match(/^peak_price_([\d.]+)_eur_kwh$/);
  if (peakMatch) return `Price peak at ${parseFloat(peakMatch[1]).toFixed(2)} /kWh`;

  const forecastSatisfiedMatch = reason.match(/^(?:comfort|setback)_satisfied_forecast_([\d.]+)_target_([\d.]+)$/);
  if (forecastSatisfiedMatch) {
    return `Indoor forecast ${parseFloat(forecastSatisfiedMatch[1]).toFixed(1)}°C already exceeds ${parseFloat(forecastSatisfiedMatch[2]).toFixed(1)}°C target`;
  }

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

function formatPayloadExtras(payload: Record<string, unknown>): string | null {
  const extras: string[] = [];
  const predictedMinutes = payload.predicted_minutes;
  const heatingRate = payload.heating_rate;
  const confidence = payload.confidence;
  const offset = payload.offset;
  const level = payload.level;
  if (typeof predictedMinutes === "number") extras.push(`~${predictedMinutes} min heating`);
  if (typeof heatingRate === "number") extras.push(`${heatingRate} °C/h`);
  if (typeof confidence === "number") extras.push(`${Math.round(confidence * 100)}% confidence`);
  if (typeof confidence === "string") extras.push(`${confidence.replace(/_/g, " ")} confidence`);
  if (typeof offset === "number") extras.push(`${offset > 0 ? "+" : ""}${offset} °C`);
  if (typeof level === "number" || typeof level === "string") extras.push(`level ${level}`);
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

    case "cancelled":
      return typeof result?.detail === "string"
        ? result.detail
        : "Cancelled because a newer plan replaced this one";

    case "pending":
      return "Waiting to execute at scheduled time";

    default:
      return null;
  }
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatHorizon(start: string, end: string, hour12: boolean): string {
  const startDate = new Date(start);
  const endDate = new Date(end);
  const dateFormat: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  return `${startDate.toLocaleDateString([], dateFormat)} ${formatTime(start, hour12)} – ${endDate.toLocaleDateString([], dateFormat)} ${formatTime(end, hour12)}`;
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

function formatLateness(seconds: number | null | undefined): string | null {
  if (seconds == null) return null;
  if (seconds <= 120) return "on time";
  return `${Math.ceil(seconds / 60)} min late`;
}

export function PlanHistory() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [showAllRevisions, setShowAllRevisions] = useState(false);
  const [visibleCount, setVisibleCount] = useState(12);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const {
    actions: expandedActions,
    outcome,
    changeSummary,
    provenance,
    loading: loadingActions,
    error: actionsError,
  } = usePlanActions(expandedId);
  const currency = useCurrency();
  const timeFormat = useTimeFormat();

  const loadPlans = useCallback(() => {
    fetch("/api/plans?limit=50")
      .then((r) => {
        if (!r.ok) throw new Error(`API error (${r.status})`);
        return r.json();
      })
      .then((data) => setPlans(data))
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load plan history"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadPlans();
    const interval = window.setInterval(loadPlans, 30_000);
    return () => window.clearInterval(interval);
  }, [loadPlans]);

  const toggleExpand = (planId: number) => {
    if (expandedId === planId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(planId);
  };

  const groupedPlans = plans.reduce<Array<{ plan: PlanSummary; grouped: PlanSummary[] }>>((groups, plan) => {
    const previousGroup = groups[groups.length - 1];
    const previous = previousGroup?.grouped[previousGroup.grouped.length - 1] ?? previousGroup?.plan;
    const closeInTime = previous
      ? Math.abs(new Date(previous.created_at).getTime() - new Date(plan.created_at).getTime()) <= 2 * 60 * 60 * 1000
      : false;
    const sameCost = previous?.cost_estimate_eur == null || plan.cost_estimate_eur == null
      ? previous?.cost_estimate_eur === plan.cost_estimate_eur
      : Math.abs(previous.cost_estimate_eur - plan.cost_estimate_eur) <= 0.02;
    const routineReplacement = Boolean(
      previousGroup
      && plan.status === "superseded"
      && previous?.status === "superseded"
      && plan.optimizer_version === previous.optimizer_version
      && plan.actions_count === previous.actions_count
      && sameCost
      && closeInTime,
    );
    if (routineReplacement) {
      previousGroup!.grouped.push(plan);
    } else {
      groups.push({ plan, grouped: [] });
    }
    return groups;
  }, []);
  const historyRows = (showAllRevisions
    ? plans.map((plan) => ({ plan, grouped: [] as PlanSummary[] }))
    : groupedPlans
  ).slice(0, visibleCount);

  if (loading) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Plan Revisions</h2>
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
        <h2 className="chart-title">Plan Revisions</h2>
        <div className="plan-error"><span>{error}</span></div>
      </div>
    );
  }

  if (plans.length === 0) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Plan Revisions</h2>
        <p className="plan-empty-msg">No past plans yet.</p>
      </div>
    );
  }

  return (
    <div className="plan-section">
      <div className="plan-history-heading">
        <div>
          <h2 className="chart-title">Plan change history</h2>
          <p className="chart-caption">Routine recalculations are grouped. Expand a row for commands, causes, and measured outcome.</p>
        </div>
        <label className="plan-history-toggle">
          <input type="checkbox" checked={showAllRevisions} onChange={(event) => {
            setShowAllRevisions(event.target.checked);
            setVisibleCount(12);
          }} />
          Show every technical revision
        </label>
      </div>
      <div className="plan-history-list">
        {historyRows.map(({ plan, grouped }, rowIndex) => {
          const isExpanded = expandedId === plan.id;
          const layerLabel = LAYER_LABELS[plan.optimizer_version] || plan.optimizer_version;
          const previousPlan = historyRows[rowIndex + 1]?.plan;
          const actionDelta = previousPlan ? plan.actions_count - previousPlan.actions_count : null;
          const planCurrency = plan.price_currency || currency.code;
          const previousCurrency = previousPlan?.price_currency || currency.code;
          const sameCurrency = !previousPlan || planCurrency === previousCurrency;
          const costDelta = sameCurrency && previousPlan?.cost_estimate_eur != null && plan.cost_estimate_eur != null
            ? plan.cost_estimate_eur - previousPlan.cost_estimate_eur
            : null;
          return (
            <div key={plan.id} className="plan-history-item">
              <button
                className="plan-history-row"
                onClick={() => toggleExpand(plan.id)}
                aria-expanded={isExpanded}
              >
                <span className="plan-history-date">{formatDate(plan.created_at)}</span>
                <span className="plan-history-horizon">
                  {formatHorizon(plan.horizon_start, plan.horizon_end, timeFormat.hour12)}
                </span>
                <span className="plan-history-layer">{layerLabel}</span>
                <span className={`status-badge ${plan.status === "active" ? "online" : "loading"}`}>
                  {plan.status === "superseded" ? `Replaced by #${plan.superseded_by_plan_id ?? "newer plan"}` : plan.status}
                </span>
                <span className="plan-history-cost">{formatCostInCurrency(plan.cost_estimate_eur, planCurrency, currency)}</span>
                <span className="plan-history-count">{plan.actions_count} actions</span>
                <span className="plan-history-chevron" aria-hidden="true">
                  {isExpanded ? "▾" : "▸"}
                </span>
              </button>
              <div className="plan-history-inline-diff">
                {grouped.length > 0 && <span>↻ {grouped.length + 1} equivalent recalculations grouped</span>}
                {actionDelta != null && (
                  <span>{actionDelta === 0 ? "No action-count change" : `${Math.abs(actionDelta)} action${Math.abs(actionDelta) === 1 ? "" : "s"} ${actionDelta > 0 ? "added" : "removed"}`}</span>
                )}
                {costDelta != null && Math.abs(costDelta) >= 0.005 && (
                  <span>Estimated cost {costDelta > 0 ? "+" : "−"}{formatCostInCurrency(Math.abs(costDelta), planCurrency, currency)}</span>
                )}
                {previousPlan && !sameCurrency && (
                  <span>Currency changed {previousCurrency} → {planCurrency}</span>
                )}
              </div>

              {isExpanded && (
                <div className="plan-history-detail">
                  {plan.status_reason && (
                    <p className="chart-caption">Revision reason: {plan.status_reason.replace(/_/g, " ")}</p>
                  )}
                  {plan.price_source && (
                    <p className="chart-caption">Planning price input: {plan.price_source} · {plan.price_currency || currency.code}</p>
                  )}
                  {provenance?.input_quality && (
                    <p className="chart-caption">
                      Saved inputs: {provenance.input_quality.price?.contiguous_hours ?? 0}h prices
                      {provenance.input_quality.price?.latest_fetched_at ? ` fetched ${formatDate(provenance.input_quality.price.latest_fetched_at)}` : ""}
                      {" · "}{provenance.input_quality.weather?.contiguous_hours ?? 0}h weather
                      {provenance.input_quality.weather?.latest_issued_at ? ` issued ${formatDate(provenance.input_quality.weather.latest_issued_at)}` : ""}
                    </p>
                  )}
                  {provenance?.price_risk && (
                    <p className="chart-caption">
                      Price risk: {provenance.price_risk.level ?? "unknown"}
                      {provenance.price_risk.hours ? ` · ${provenance.price_risk.hours} published hours` : ""}
                      {provenance.price_risk.near_term_policy ? ` · ${provenance.price_risk.near_term_policy}` : ""}
                      {provenance.price_risk.future_policy ? ` ${provenance.price_risk.future_policy}` : ""}
                    </p>
                  )}
                  {changeSummary?.message && (
                    <p className="chart-caption">What changed: {changeSummary.message}</p>
                  )}
                  {changeSummary?.drivers && changeSummary.drivers.length > 0 && (
                    <p className="chart-caption">Drivers: {changeSummary.drivers.map((driver) => driver.replace(/_/g, " ")).join(" · ")}</p>
                  )}
                  {outcome?.timing?.measured_actions ? (
                    <p className="chart-caption">
                      Execution: {outcome.timing.on_time_actions ?? 0}/{outcome.timing.measured_actions} on time
                      {outcome.timing.average_lateness_seconds != null ? ` · average delay ${Math.round(outcome.timing.average_lateness_seconds / 60)} min` : ""}
                      {outcome.verified_actions ? ` · ${outcome.verified_actions} verified` : ""}
                    </p>
                  ) : null}
                  {outcome?.measurement && (
                    <div className="plan-history-summary" style={{ marginTop: "0.5rem" }}>
                      {outcome.measurement.state === "not_started" ? (
                        <span className="text-muted text-xs">Outcome measurement starts when this plan window begins.</span>
                      ) : (
                        <>
                          <span className="text-muted text-xs">
                            Measured outcome {outcome.measurement.progress_pct != null ? `· ${outcome.measurement.progress_pct}% of plan window` : ""}
                          </span>
                          <span className="text-muted text-xs">
                            {outcome.measurement.cost?.measured_kwh?.toFixed(2) ?? "0.00"} kWh
                            {outcome.measurement.cost?.actual_cost != null ? ` · ${formatCostInCurrency(outcome.measurement.cost.actual_cost, planCurrency, currency)}` : " · awaiting complete price data"}
                            {outcome.measurement.cost?.coverage_pct != null ? ` (${outcome.measurement.cost.coverage_pct}% priced)` : ""}
                          </span>
                          {outcome.measurement.cost?.estimated_price_shift_savings != null && (
                            <span className="plan-action-status executed">
                              Estimated price-shift saving {formatCostInCurrency(outcome.measurement.cost.estimated_price_shift_savings, planCurrency, currency)}
                            </span>
                          )}
                          {outcome.measurement.comfort?.samples ? (
                            <span className="text-muted text-xs">
                              Comfort: {outcome.measurement.comfort.within_range_pct ?? "—"}% in range
                              {outcome.measurement.comfort.average_c != null ? ` · avg ${outcome.measurement.comfort.average_c.toFixed(1)}°C` : ""}
                            </span>
                          ) : (
                            <span className="text-muted text-xs">Comfort: awaiting trusted indoor readings</span>
                          )}
                        </>
                      )}
                    </div>
                  )}
                  {outcome?.measurement?.baseline_method && (
                    <p className="chart-caption">{outcome.measurement.baseline_method}</p>
                  )}
                  {loadingActions ? (
                    <div className="plan-loading">
                      <div className="plan-loading-skeleton" />
                      <div className="plan-loading-skeleton" style={{ width: "60%" }} />
                    </div>
                  ) : actionsError ? (
                    <p className="plan-error">Could not load plan actions: {actionsError}</p>
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
                                  ran {formatTime(action.executed_at, timeFormat.hour12)} · {formatLateness((new Date(action.executed_at).getTime() - new Date(action.scheduled_ts).getTime()) / 1000)}
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
      {visibleCount < (showAllRevisions ? plans.length : groupedPlans.length) && (
        <button className="btn btn-sm plan-history-load-more" onClick={() => setVisibleCount((count) => count + 12)}>
          Load more changes
        </button>
      )}
    </div>
  );
}

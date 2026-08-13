"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";
import { useTimeFormat } from "./useTimeFormat";
import { PlanAction, usePlanActions } from "./usePlanActions";
import {
  ACTION_LABELS,
  LAYER_LABELS,
  LAYER_TOOLTIPS,
  STATUS_DISPLAY,
  formatRelativeTime,
  formatTime,
  formatPayload,
} from "@/lib/constants";

interface PlanProps {
  plan: {
    id: number;
    optimizer_version: string;
    cost_estimate_eur: number | null;
    price_currency?: string;
    price_source?: string;
    actions_count: number;
    horizon_start?: string;
    horizon_end?: string;
    created_at?: string;
  } | null;
}

/* ── Time-of-day groups ── */
type TimeGroup = "morning" | "afternoon" | "evening" | "night";
function getTimeGroup(iso: string): TimeGroup {
  const h = new Date(iso).getHours();
  if (h >= 6 && h < 12) return "morning";
  if (h >= 12 && h < 18) return "afternoon";
  if (h >= 18 && h < 22) return "evening";
  return "night";
}
const TIME_GROUP_LABELS: Record<TimeGroup, string> = {
  morning: "Morning",
  afternoon: "Afternoon",
  evening: "Evening",
  night: "Night",
};

/* ── Helpers ── */

const DEFAULT_VISIBLE = 12;

/* ── Sub-components ── */

function LayerBadge({ version }: { version: string }) {
  const isMl = version.includes("ml");
  const isMilp = version.includes("milp");
  const tooltip = LAYER_TOOLTIPS[version] || version;
  return (
    <span
      title={tooltip}
      className="plan-layer-badge"
      style={{
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
    <div
      style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.5rem" }}
    >
      <div
        role="progressbar"
        aria-valuenow={completed}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label={`${completed} of ${total} actions completed`}
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
      <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", whiteSpace: "nowrap" }}>
        {completed}/{total} done
      </span>
    </div>
  );
}

function formatPlanHorizon(start: string | undefined, end: string | undefined, hour12: boolean): string {
  if (!start || !end) return "24-hour horizon";
  const startDate = new Date(start);
  const endDate = new Date(end);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) return "24-hour horizon";

  const dateFormat: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  return `${startDate.toLocaleDateString([], dateFormat)} ${formatTime(start, hour12)} – ${endDate.toLocaleDateString([], dateFormat)} ${formatTime(end, hour12)}`;
}

function ActionRow({
  action,
  isNext,
  hour12,
}: {
  action: PlanAction;
  isNext: boolean;
  hour12: boolean;
}) {
  const info = ACTION_LABELS[action.action_type];
  const isPastDuePending = action.status === "pending" && new Date(action.scheduled_ts) < new Date();
  const isExpired = action.status === "expired";
  const isSkipped = action.status === "skipped";
  const displayStatus = isPastDuePending ? "expired" : action.status;
  const status = STATUS_DISPLAY[displayStatus] || { text: displayStatus, className: "" };
  const isDone = action.status === "executed" || action.status === "executed_unverified";
  const isFaded = isDone || isPastDuePending || isExpired || isSkipped;
  const hasPayload = action.payload && Object.keys(action.payload).length > 0;

  // Show diagnostic detail for missed/expired/skipped actions
  const detail = (isExpired || isSkipped) && action.result?.detail
    ? String(action.result.detail)
    : null;

  return (
    <div
      className={`plan-action ${isNext ? "plan-action--next" : ""}`}
      style={{ opacity: isFaded ? 0.6 : 1 }}
    >
      <span className="plan-action-time">{formatTime(action.scheduled_ts, hour12)}</span>
      <span className="plan-action-type">
        {info ? (
          <>
            <span role="img" aria-label={info.label}>{info.emoji}</span>{" "}
            {info.label}
          </>
        ) : (
          action.action_type
        )}
      </span>
      {hasPayload && (
        <span className="plan-action-payload">{formatPayload(action.payload)}</span>
      )}
      {isNext && (
        <span className="plan-action-relative">{formatRelativeTime(action.scheduled_ts)}</span>
      )}
      <span className={`plan-action-status ${status.className}`}>{status.text}</span>
      {detail && (
        <span className="plan-action-detail" style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginLeft: "0.5rem" }}>
          — {detail}
        </span>
      )}
    </div>
  );
}

function TimeGroupDivider({ group }: { group: TimeGroup }) {
  return (
    <div className="plan-time-divider">
      <span>{TIME_GROUP_LABELS[group]}</span>
    </div>
  );
}

/* ── Main Component ── */

export function PlanView({ plan }: PlanProps) {
  const [showAll, setShowAll] = useState(false);
  const { actions, loading, error: fetchError } = usePlanActions(plan?.id);
  const currency = useCurrency();
  const timeFormat = useTimeFormat();

  useEffect(() => {
    setShowAll(false);
  }, [plan?.id]);

  const now = new Date();
  const completedCount = actions.filter(
    (a) => a.status === "executed" || a.status === "executed_unverified"
  ).length;
  const skippedCount = actions.filter(
    (a) => a.status === "skipped" || (a.status === "pending" && new Date(a.scheduled_ts) < now)
  ).length;
  const pendingCount = actions.filter(
    (a) => a.status === "pending" && new Date(a.scheduled_ts) >= now
  ).length;
  const sendingCount = actions.filter(
    (a) => a.status === "executing" || a.status === "dispatched"
  ).length;
  // The action endpoint can arrive before the dashboard refresh after a plan
  // replacement. Never render an impossible denominator during that short gap.
  const totalActions = Math.max(plan?.actions_count ?? 0, actions.length);

  const nextAction = actions.find(
    (a) => a.status === "pending" && new Date(a.scheduled_ts) > now
  );

  // Group actions by time-of-day for display
  const visibleActions = showAll ? actions : actions.slice(0, DEFAULT_VISIBLE);
  const grouped: { group: TimeGroup; actions: PlanAction[] }[] = [];
  let lastGroup: TimeGroup | null = null;
  for (const a of visibleActions) {
    const g = getTimeGroup(a.scheduled_ts);
    if (g !== lastGroup) {
      grouped.push({ group: g, actions: [a] });
      lastGroup = g;
    } else {
      grouped[grouped.length - 1].actions.push(a);
    }
  }

  return (
    <div className="plan-section">
      {/* ── Header ── */}
      <div className="plan-header">
        <div style={{ flex: 1 }}>
          <div className="plan-title-row">
            <h2 className="chart-title" style={{ margin: 0 }}>Active Plan</h2>
            {plan && <LayerBadge version={plan.optimizer_version} />}
          </div>

          {plan ? (
            <>
              {/* Cost highlight */}
              <div className="plan-cost-highlight">
                <span className="plan-cost-value">{formatCost(plan.cost_estimate_eur, currency)}</span>
                <span className="plan-cost-label">estimated cost</span>
              </div>

              {/* Meta row */}
              <div className="plan-meta-row">
                <span>
                  {formatPlanHorizon(plan.horizon_start, plan.horizon_end, timeFormat.hour12)}
                </span>
                <span className="plan-meta-sep" aria-hidden="true">·</span>
                {plan.price_source && (
                  <>
                    <span title="Price source captured when this plan was created">
                      {plan.price_source} · {plan.price_currency || currency.code}
                    </span>
                    <span className="plan-meta-sep" aria-hidden="true">·</span>
                  </>
                )}
                <span>
                  {completedCount} done{pendingCount > 0 ? `, ${pendingCount} scheduled` : ""}
                  {sendingCount > 0 ? `, ${sendingCount} sending` : ""}
                  {skippedCount > 0 ? `, ${skippedCount} skipped` : ""}
                </span>
              </div>

              <ProgressBar completed={completedCount} total={totalActions} />
            </>
          ) : (
            <p className="plan-empty-msg">
              No active plan. Optimizer will generate one when price data is available.
            </p>
          )}
        </div>
      </div>

      {/* ── Next action callout ── */}
      {nextAction && (
        <div className="plan-next-callout">
          <span className="plan-next-badge">NEXT</span>
          <span className="plan-next-label">
            <span role="img" aria-label={ACTION_LABELS[nextAction.action_type]?.label || nextAction.action_type}>
              {ACTION_LABELS[nextAction.action_type]?.emoji || "⚡"}
            </span>{" "}
            {ACTION_LABELS[nextAction.action_type]?.label || nextAction.action_type}
          </span>
          <span className="plan-next-time">
            {formatTime(nextAction.scheduled_ts, timeFormat.hour12)}
            <span className="plan-next-relative"> ({formatRelativeTime(nextAction.scheduled_ts)})</span>
          </span>
        </div>
      )}

      {/* ── Loading state ── */}
      {loading && (
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "70%" }} />
          <div className="plan-loading-skeleton" style={{ width: "85%" }} />
        </div>
      )}

      {/* ── Error state ── */}
      {fetchError && (
        <div className="plan-error">
          <span>Could not load plan actions: {fetchError}</span>
        </div>
      )}

      {/* ── Actions timeline ── */}
      {!loading && !fetchError && actions.length > 0 && (
        <div className="plan-actions" style={{ marginTop: "0.75rem" }}>
          {grouped.map((g, gi) => (
            <div key={`${g.group}-${gi}`}>
              <TimeGroupDivider group={g.group} />
              {g.actions.map((action) => (
                <ActionRow
                  key={action.id}
                  action={action}
                  isNext={action.id === nextAction?.id}
                  hour12={timeFormat.hour12}
                />
              ))}
            </div>
          ))}

          {/* Show all / collapse toggle */}
          {actions.length > DEFAULT_VISIBLE && (
            <button
              className="plan-show-all-btn"
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll
                ? "Show less"
                : `Show all ${actions.length} actions (+${actions.length - DEFAULT_VISIBLE} more)`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

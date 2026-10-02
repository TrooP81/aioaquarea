/**
 * Shared UI constants and formatting helpers for the Heat Pump Optimizer dashboard.
 *
 * These maps translate raw backend identifiers (optimizer layer versions, plan
 * action types, action statuses, settings options) into friendly, human-readable
 * labels, tooltips and icons. Keeping them in one place keeps wording consistent
 * across the dashboard, plan views and settings.
 *
 * Backend sources of truth:
 *   - optimizer layer versions:  packages/optimizer/{rules_engine,milp,main}.py
 *   - optimizer layer options:   packages/core/settings_service.py (SETTINGS_SCHEMA)
 *   - action types:              packages/optimizer/actions/types.py (ActionType)
 *   - action statuses:           packages/optimizer/executor_core.py, shower_mode.py
 */

/* ────────────────────────────────────────────────────────────────────────────
 * Dashboard section navigation
 * ──────────────────────────────────────────────────────────────────────────── */

export interface Section {
  id: string;
  label: string;
  description: string;
}

export const SECTIONS = [
  { id: "home", label: "Home", description: "Live readings, controls, and operational health" },
  { id: "timeline", label: "Timeline", description: "Current schedule, outcomes, and plan revisions" },
  { id: "under-the-hood", label: "Under the hood", description: "Raw charts and optimizer diagnostics" },
] as const;

export type SectionId = (typeof SECTIONS)[number]["id"];

export const LEGACY_SECTION_ALIASES: Record<string, { section: SectionId; hash?: string }> = {
  overview: { section: "home" },
  controls: { section: "home", hash: "#controls" },
  plan: { section: "timeline" },
  charts: { section: "under-the-hood", hash: "#raw-charts" },
  status: { section: "under-the-hood", hash: "#models" },
};

/* ────────────────────────────────────────────────────────────────────────────
 * Optimizer layers
 *
 * `optimizer_version` / `active_layer` come back from the API as version strings
 * (e.g. "rules_v3", "milp_v1", "milp_v1+ml"). `configured_layer` and the settings
 * dropdown use the configuration keys ("rules_only", "milp_preferred", "auto").
 * ──────────────────────────────────────────────────────────────────────────── */

export const LAYER_LABELS: Record<string, string> = {
  // Runtime version strings
  rules_v3: "Rules",
  milp_v1: "Optimized",
  "milp_v1+ml": "Optimized + ML",
  // Configuration keys (shown in a few places as a fallback)
  rules_only: "Rules",
  milp_preferred: "Optimized",
  auto: "Automatic",
};

export const LAYER_TOOLTIPS: Record<string, string> = {
  rules_v3:
    "Rule-based scheduler: deterministic heuristics using electricity prices, comfort windows and quiet hours.",
  milp_v1:
    "Cost optimizer (MILP): searches for the lowest-cost schedule across the whole planning horizon.",
  "milp_v1+ml":
    "Cost optimizer enhanced with machine-learning predictions of efficiency (COP) and hot-water demand.",
  rules_only: "Deterministic rule-based scheduling. Most predictable; no ML required.",
  milp_preferred:
    "Use the MILP cost optimizer when possible, falling back to rules on any error.",
  auto:
    "Use the MILP optimizer once ML models are trained on enough data; otherwise fall back to rules.",
};

/** Options for the `optimizer_layer` setting dropdown. */
export const OPTIMIZER_LAYER_OPTIONS: Record<string, { label: string; description: string }> = {
  rules_only: {
    label: "Rules only",
    description: "Deterministic rule-based scheduling. Most predictable; no ML required.",
  },
  milp_preferred: {
    label: "Cost optimizer (MILP)",
    description: "Use the MILP cost optimizer when possible, falling back to rules on error.",
  },
  auto: {
    label: "Automatic",
    description:
      "Use the MILP optimizer once ML models are trained on ≥14 days of data; otherwise use rules.",
  },
};

/* ────────────────────────────────────────────────────────────────────────────
 * Plan action types
 *
 * Keys match ActionType (packages/optimizer/actions/types.py). Each entry pairs a
 * glanceable emoji with a plain-language label. Emoji are decorative only — always
 * render the label alongside them for accessibility.
 * ──────────────────────────────────────────────────────────────────────────── */

export interface ActionInfo {
  emoji: string;
  label: string;
}

export const ACTION_LABELS: Record<string, ActionInfo> = {
  force_dhw_on: { emoji: "🚿", label: "Heat hot water" },
  force_dhw_off: { emoji: "💧", label: "Stop water heating" },
  quiet_mode_on: { emoji: "🤫", label: "Quiet mode on" },
  quiet_mode_off: { emoji: "🔊", label: "Quiet mode off" },
  zone_temp_boost: { emoji: "🔥", label: "Boost heating" },
  zone_temp_restore: { emoji: "↩️", label: "Restore heating" },
  set_tank_temp: { emoji: "🌡️", label: "Set tank temperature" },
  set_zone_heat_temperature: { emoji: "🌡️", label: "Set room temperature" },
  eco_mode_on: { emoji: "🍃", label: "Eco mode on" },
  eco_mode_off: { emoji: "🍃", label: "Eco mode off" },
  normal_mode_on: { emoji: "⚙️", label: "Normal mode" },
  comfort_mode_on: { emoji: "☀️", label: "Comfort mode" },
};

/* ────────────────────────────────────────────────────────────────────────────
 * Action statuses
 *
 * `className` matches the `.plan-action-status.<name>` styles in globals.css
 * (pending / executed / failed / skipped). Statuses without a dedicated colour
 * reuse the closest existing one.
 * ──────────────────────────────────────────────────────────────────────────── */

export interface StatusDisplay {
  text: string;
  className: string;
}

export const STATUS_DISPLAY: Record<string, StatusDisplay> = {
  pending: { text: "Scheduled", className: "pending" },
  executing: { text: "Sending…", className: "pending" },
  dispatched: { text: "Sending…", className: "pending" },
  active: { text: "Active", className: "executed" },
  executed: { text: "Done", className: "executed" },
  executed_unverified: { text: "Done (unverified)", className: "executed" },
  failed: { text: "Failed", className: "failed" },
  skipped: { text: "Skipped", className: "skipped" },
  skipped_peak: { text: "Skipped (peak price)", className: "skipped" },
  cancelled: { text: "Cancelled", className: "skipped" },
  expired: { text: "Missed", className: "skipped" },
};

function recognizedStatus(code: string | null | undefined, labels: Record<string, string>): string {
  if (!code || !labels[code]) return `Unrecognized status (${code ?? "unknown"})`;
  return labels[code];
}

export function actionStatusLabel(code: string | null | undefined): string {
  return recognizedStatus(code, Object.fromEntries(Object.entries(STATUS_DISPLAY).map(([key, value]) => [key, value.text])));
}

export function reasonLabel(code: string | null | undefined): string {
  return recognizedStatus(code, {
    automatic: "Automatic scheduling",
    active_override: "Active pause",
    learning_mode_active: "Learning mode",
    comfort_at_risk: "Comfort risk forecast",
    new_plans_paused: "New plans paused",
    control_state_unavailable: "Control state unavailable",
  });
}

export function outdoorFallbackReasonLabel(code: string | null | undefined): string {
  return recognizedStatus(code, {
    weather_unavailable: "Weather data is unavailable",
    heat_pump_sensor_unavailable: "Heat-pump outdoor sensor is unavailable",
    weather_stale: "Weather data is stale",
  });
}

export function gateStateLabel(code: string | null | undefined): string {
  return recognizedStatus(code, {
    ALLOWED: "Allowed",
    BLOCKED: "Blocked",
    UNKNOWN: "Unknown",
  });
}

export function forecastDisplayStatusLabel(code: string | null | undefined): string {
  return recognizedStatus(code, {
    fresh: "Fresh forecast",
    aging: "Aging forecast",
    stale: "Stale forecast",
    diverged: "Forecast diverged",
    unavailable: "Forecast unavailable",
    checking: "Checking forecast",
  });
}

export function seasonalReasonLabel(code: string | null | undefined): string {
  return recognizedStatus(code, {
    inactive: "Seasonal calibration is inactive",
    active: "Seasonal calibration is active",
    blocked_by_unresolved_safety_revert: "Waiting for unresolved safety restores",
    waiting_for_heating_season: "Waiting for heating season",
  });
}

/* ────────────────────────────────────────────────────────────────────────────
 * Payload labels + formatting
 * ──────────────────────────────────────────────────────────────────────────── */

export const PAYLOAD_LABELS: Record<string, string> = {
  target_temp: "Target",
  tank_temp: "Tank",
  temperature: "Temp",
  temp: "Temp",
  level: "Level",
  offset: "Offset",
  zone_id: "Zone",
  reason: "Reason",
};

/** Humanize a raw optimizer `reason` string into readable text. */
function humanizeReason(reason: string): string {
  const thermalMatch = reason.match(/^thermal_optimized_before_(\d+:\d+)$/);
  if (thermalMatch) return `Cheapest slot before ${thermalMatch[1]}`;

  const peakMatch = reason.match(/^peak_price_([\d.]+)_eur_kwh$/);
  if (peakMatch) return `Price peak (${parseFloat(peakMatch[1]).toFixed(2)}/kWh)`;

  const forecastSatisfiedMatch = reason.match(/^(?:comfort|setback)_satisfied_forecast_([\d.]+)_target_([\d.]+)$/);
  if (forecastSatisfiedMatch) {
    return `Indoor forecast ${parseFloat(forecastSatisfiedMatch[1]).toFixed(1)}°C already exceeds ${parseFloat(forecastSatisfiedMatch[2]).toFixed(1)}°C target`;
  }

  const map: Record<string, string> = {
    dhw_target_reached: "Tank reached target",
    thermal_preheat_before_cold: "Pre-heat before cold spell",
    preheat_complete: "Pre-heat complete",
    peak_avoidance_end: "Peak window ended",
    night_quiet_schedule: "Night quiet hours",
    night_quiet_end: "Night quiet hours ended",
    comfort_schedule: "Comfort schedule",
    outside_comfort_schedule: "Outside comfort hours",
  };
  if (map[reason]) return map[reason];
  return reason.replace(/_/g, " ");
}

/**
 * Render a plan action payload as a compact, human-readable summary.
 * Falls back to a humanized `reason` when no concrete parameters are present.
 */
export function formatPayload(payload: Record<string, unknown> | null | undefined): string {
  if (!payload || typeof payload !== "object") return "";
  const parts: string[] = [];

  const temp = payload.target_temp ?? payload.temperature ?? payload.temp ?? payload.tank_temp;
  if (typeof temp === "number") parts.push(`→ ${temp}°C`);

  if (typeof payload.level === "number" || typeof payload.level === "string") {
    parts.push(`level ${payload.level}`);
  }
  if (typeof payload.offset === "number") {
    parts.push(`${payload.offset > 0 ? "+" : ""}${payload.offset}°C`);
  }
  if (parts.length === 0 && (typeof payload.zone_id === "number" || typeof payload.zone_id === "string")) {
    parts.push(`zone ${payload.zone_id}`);
  }
  if (parts.length === 0 && typeof payload.reason === "string") {
    parts.push(humanizeReason(payload.reason));
  }

  return parts.join(" · ");
}

/* ────────────────────────────────────────────────────────────────────────────
 * Time formatting
 *
 * These accept an ISO-8601 string (or Date) so they can be used directly with
 * API timestamps. For formatting a local `Date` (e.g. "last updated") with the
 * user's clock preference, use `formatTime` from components/useTimeFormat instead.
 * ──────────────────────────────────────────────────────────────────────────── */

/** Format an ISO timestamp as a short time (respecting 12/24h preference). */
export function formatTime(
  iso: string | Date | null | undefined,
  hour12: boolean = false,
): string {
  if (!iso) return "—";
  const d = iso instanceof Date ? iso : new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12 });
}

/**
 * Format an ISO timestamp relative to now, in either direction:
 *   future → "in 5m", "in 2h", "in 3d"
 *   past   → "5m ago", "2h ago", "3d ago"
 */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";

  const diffMs = d.getTime() - Date.now();
  const future = diffMs >= 0;
  const absMs = Math.abs(diffMs);
  const mins = Math.round(absMs / 60000);

  const phrase = (n: number, unit: string) => (future ? `in ${n}${unit}` : `${n}${unit} ago`);

  if (mins < 1) return future ? "now" : "just now";
  if (mins < 60) return phrase(mins, "m");

  const hrs = Math.round(mins / 60);
  if (hrs < 24) return phrase(hrs, "h");

  const days = Math.round(hrs / 24);
  return phrase(days, "d");
}

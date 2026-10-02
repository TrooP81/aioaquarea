"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Dashboard } from "@/components/Dashboard";
import { PriceChart } from "@/components/PriceChart";
import { TemperatureChart } from "@/components/TemperatureChart";
import { ConsumptionChart } from "@/components/ConsumptionChart";
import { ForecastChart } from "@/components/ForecastChart";
import { ThermalPredictionChart } from "@/components/ThermalPredictionChart";
import { PlanActivityTimeline } from "@/components/PlanActivityTimeline";
import { PlanHistory } from "@/components/PlanHistory";
import { Controls } from "@/components/Controls";
import { LearningModeCard } from "@/components/LearningModeCard";
import { OptimizerStatus } from "@/components/OptimizerStatus";
import { OutcomeSummary } from "@/components/OutcomeSummary";
import { OperationalAlerts } from "@/components/OperationalAlerts";
import { AppVersionBadge } from "@/components/AppVersionBadge";
import { TabNavigation } from "@/components/TabNavigation";
import { DecisionSummary } from "@/components/DecisionSummary";
import { Banner } from "@/components/Banner";
import { DataAge } from "@/components/DataAge";
import { RefreshProvider, useRefresh } from "@/components/RefreshContext";
import { TimelineWorkspace } from "@/components/TimelineWorkspace";
import { PlanView } from "@/components/PlanView";
import type { PlanActivity } from "@/components/PlanActivityTimeline";
import type { PlanAction } from "@/components/usePlanActions";
import type { ControlState, ReadQuotaResponse, SpaceHeatingGate } from "@/lib/api-types";
import { LEGACY_SECTION_ALIASES, SECTIONS, SectionId } from "@/lib/constants";
import Link from "next/link";

interface DashboardData {
  current_status: {
    ts: string;
    device_id: string;
    mode: string | null;
    operation_status: number | null;
    outdoor_temp: number | null;
    heat_pump_outdoor_temp?: number | null;
    weather_outdoor_temp?: number | null;
    outdoor_temp_source?: string | null;
    outdoor_temp_provider?: string | null;
    outdoor_temp_compensation_c?: number | null;
    outdoor_temp_fallback_reason?: string | null;
    tank_temp: number | null;
    tank_target_temp: number | null;
    zone1_temp: number | null;
    zone1_target_temp: number | null;
    quiet_mode: number | null;
    powerful_mode: number | null;
    device_action?: string | null;
    direction?: string | null;
    space_heating_active: boolean | null;
  } | null;
  current_status_fresh: boolean;
  current_status_age_seconds: number | null;
  current_price: number | null;
  today_kwh: number;
  today_cost_eur: number | null;
  today_cost_currency: string;
  today_cost_priced_kwh: number;
  today_cost_unpriced_kwh: number;
  today_cost_priced_amount: number;
  today_cost_coverage_pct: number;
  today_cost_complete: boolean;
  active_plan: {
    id: number;
    optimizer_version: string;
    cost_estimate_eur: number | null;
    actions_count: number;
    horizon_start?: string;
    horizon_end?: string;
    created_at?: string;
  } | null;
  has_override: boolean;
  override_id: number | null;
  space_heating_gate?: SpaceHeatingGate | null;
}

interface PollResult {
  success: boolean;
  message: string;
  tone?: "warning" | "danger";
}

interface PollNowTaskResult {
  success?: boolean;
  message?: string;
}

interface PollNowResponse {
  status?: string;
  results?: Record<string, PollNowTaskResult>;
}

interface PollNowErrorResponse {
  detail?: {
    code?: string;
    retry_after_seconds?: number;
  };
}

interface IndoorTempData {
  avg_temperature: number | null;
  latest_reading: string | null;
  sensor_count: number;
  last_fresh_reading: string | null;
}

const POLL_RESULT_SUCCESS_AUTO_DISMISS_MS = 6000;
const CONTROL_STATE_NAMES = new Set<ControlState["state"]>([
  "paused_by_user",
  "observing",
  "holding",
  "comfort_at_risk",
  "automatic",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isControlState(value: unknown): value is ControlState {
  if (
    !isRecord(value) ||
    typeof value.headline !== "string" ||
    typeof value.detail !== "string" ||
    typeof value.state !== "string" ||
    !CONTROL_STATE_NAMES.has(value.state as ControlState["state"]) ||
    !Array.isArray(value.notices)
  ) {
    return false;
  }
  if (!value.notices.every((notice) => isRecord(notice) && typeof notice.detail === "string")) {
    return false;
  }
  if (value.primary_action === null) return true;
  if (!isRecord(value.primary_action) || typeof value.primary_action.label !== "string") return false;
  if (value.primary_action.kind === "link") return true;
  return (
    value.primary_action.kind === "request" &&
    typeof value.primary_action.endpoint === "string" &&
    typeof value.primary_action.method === "string"
  );
}

async function fetchOptionalJson<T>(
  path: string,
  validate?: (value: unknown) => value is T,
  signal?: AbortSignal,
): Promise<T | null> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 1_000);
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  try {
    const response = await fetch(path, { signal: controller.signal });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return !validate || validate(body) ? body as T : null;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export default function Home() {
  return (
    <RefreshProvider>
      <DashboardPage />
    </RefreshProvider>
  );
}

function DashboardPage() {
  const { refreshEpoch, refreshNow } = useRefresh();
  const [data, setData] = useState<DashboardData | null>(null);
  const [indoorTemp, setIndoorTemp] = useState<IndoorTempData | null>(null);
  const [controlState, setControlState] = useState<ControlState | null>(null);
  const [readQuota, setReadQuota] = useState<ReadQuotaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [pollResult, setPollResult] = useState<PollResult | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [activeSection, setActiveSection] = useState<SectionId>("home");
  const [showRawChartDetails, setShowRawChartDetails] = useState(false);
  const [locationHash, setLocationHash] = useState("");
  const [timelineActivity, setTimelineActivity] = useState<PlanActivity[]>([]);
  const [timelineActivityError, setTimelineActivityError] = useState<string | null>(null);
  const [timelineActivityLoading, setTimelineActivityLoading] = useState(true);
  const [timelineActions, setTimelineActions] = useState<PlanAction[]>([]);
  const [timelineActionsError, setTimelineActionsError] = useState<string | null>(null);
  const [timelineActionsLoading, setTimelineActionsLoading] = useState(true);
  const fetchGeneration = useRef(0);

  const selectSection = (section: SectionId) => {
    setActiveSection(section);
    const url = new URL(window.location.href);
    url.searchParams.set("view", section);
    window.history.pushState({ view: section }, "", url);
  };

  const fetchData = useCallback(async (signal?: AbortSignal) => {
    const generation = ++fetchGeneration.current;
    try {
      const [dashRes, tempRes, controlRes, quotaRes] = await Promise.all([
        fetch("/api/dashboard", { signal }),
        fetchOptionalJson<IndoorTempData>("/api/indoor-temp/latest", undefined, signal),
        fetchOptionalJson("/api/control-state", isControlState, signal),
        fetchOptionalJson<ReadQuotaResponse>("/api/panasonic/read-quota", undefined, signal),
      ]);
      if (!dashRes.ok) throw new Error(`API error: ${dashRes.status}`);
      const json = await dashRes.json();
      if (signal?.aborted || generation !== fetchGeneration.current) return;
      setData(json);
      setIndoorTemp(tempRes);
      setControlState(controlRes);
      setReadQuota(quotaRes);
      setError(null);
      setLastUpdated(new Date());
    } catch (e) {
      if (!signal?.aborted && generation === fetchGeneration.current) {
        setError(e instanceof Error ? e.message : "Failed to fetch data");
      }
    } finally {
      if (!signal?.aborted && generation === fetchGeneration.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchData(controller.signal);
    return () => controller.abort();
  }, [fetchData, refreshEpoch]);

  useEffect(() => {
    const applyLocation = () => {
      const url = new URL(window.location.href);
      const requested = url.searchParams.get("view");
      const canonical = SECTIONS.find((section) => section.id === requested);
      const alias = requested ? LEGACY_SECTION_ALIASES[requested] : undefined;
      const section = canonical?.id ?? alias?.section ?? "home";

      if (alias) {
        url.searchParams.set("view", alias.section);
        if (!url.hash && alias.hash) url.hash = alias.hash;
      } else if (requested && !canonical) {
        url.searchParams.delete("view");
      }
      if (url.href !== window.location.href) window.history.replaceState({}, "", url);
      setLocationHash(url.hash);
      if (requested === "charts") setShowRawChartDetails(true);
      setActiveSection(section);
    };

    applyLocation();
    window.addEventListener("popstate", applyLocation);
    window.addEventListener("hashchange", applyLocation);
    return () => {
      window.removeEventListener("popstate", applyLocation);
      window.removeEventListener("hashchange", applyLocation);
    };
  }, []);

  useEffect(() => {
    const hash = locationHash;
    if (!hash || /^#plan-action-\d+$/.test(hash)) return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(hash.slice(1))?.scrollIntoView({ block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSection, loading, locationHash, showRawChartDetails]);

  const pollNow = async () => {
    setPolling(true);
    setPollResult(null);
    try {
      const res = await fetch("/api/poll-now", { method: "POST" });
      if (!res.ok) {
        const errorBody: PollNowErrorResponse | null = await res.json().catch(() => null);
        const retryAfterHeader = Number(res.headers.get("Retry-After"));
        const retryAfterSeconds = Number.isFinite(retryAfterHeader) && retryAfterHeader >= 0
          ? retryAfterHeader
          : errorBody?.detail?.retry_after_seconds;

        if (res.status === 429) {
          const wait = typeof retryAfterSeconds === "number"
            ? ` — try again in ~${Math.max(1, Math.ceil(retryAfterSeconds / 60))} min`
            : " — try again later";
          setPollResult({
            success: false,
            tone: "warning",
            message: `Panasonic read allowance used up${wait}`,
          });
        } else if (res.status === 503 && errorBody?.detail?.code === "panasonic_read_quota_unavailable") {
          setPollResult({
            success: false,
            tone: "danger",
            message: "Read quota service unavailable — refresh is paused, background polling continues",
          });
        } else {
          setPollResult({ success: false, tone: "danger", message: `Refresh failed (HTTP ${res.status})` });
        }
        return;
      }
      const json: PollNowResponse = await res.json();
      if (json.status === "ok") {
        setPollResult({ success: true, message: "All data fetched successfully" });
      } else {
        const msgs = Object.entries(json.results || {})
          .filter(([, v]) => !v?.success)
          .map(([k, v]) => `${k}: ${v?.message ?? "failed"}`)
          .join("; ");
        setPollResult({ success: false, message: msgs || "Partial success" });
      }
    } catch {
      setPollResult({ success: false, message: "Network error — is the API running?" });
    } finally {
      refreshNow();
      setPolling(false);
    }
  };

  useEffect(() => {
    if (!pollResult?.success) return;

    const timeoutId = window.setTimeout(() => {
      setPollResult((current) => (current?.success ? null : current));
    }, POLL_RESULT_SUCCESS_AUTO_DISMISS_MS);

    return () => window.clearTimeout(timeoutId);
  }, [pollResult]);

  const cancelOverride = async () => {
    const action = controlState?.primary_action;
    if (
      action?.kind !== "request" ||
      action.method !== "DELETE" ||
      typeof action.endpoint !== "string" ||
      !/^\/api\/overrides\/\d+$/.test(action.endpoint)
    ) return;
    try {
      const res = await fetch(action.endpoint, { method: action.method });
      if (!res.ok) throw new Error(`API error: ${res.status}`);
      refreshNow();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to cancel override");
    }
  };

  if (loading) {
    return (
      <main id="main-content" className="dashboard" tabIndex={-1}>
        <div className="header">
          <h1>Heat Pump Optimizer</h1>
          <span className="status-badge loading">Loading...</span>
        </div>
        <div className="chart-container">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "60%" }} />
        </div>
      </main>
    );
  }

  const headerStatus = data?.current_status
    ? (data.current_status_fresh === false ? "stale" : "online")
    : "offline";

  const headerStatusLabel =
    headerStatus === "online"
      ? "● Connected"
      : headerStatus === "stale"
        ? "● Stale"
        : "● Disconnected";

  const activeSectionMeta = SECTIONS.find((section) => section.id === activeSection) ?? SECTIONS[0];
  const quotaBlocksRefresh = readQuota?.enabled === true && (
    readQuota.reliable === false ||
    readQuota.remaining === null ||
    readQuota.remaining < readQuota.manual_required
  );

  return (
    <main id="main-content" className="dashboard" tabIndex={-1}>
      <div className="header">
        <h1>Heat Pump Optimizer</h1>
        <div className="header-actions">
          <AppVersionBadge />
          {lastUpdated && (
            <DataAge timestamp={lastUpdated.toISOString()} />
          )}
          <button className="btn" onClick={pollNow} disabled={polling || quotaBlocksRefresh}>
            {polling ? "Refreshing..." : "Refresh from heat pump"}
          </button>
          <Link href="/settings" className="btn">Settings</Link>
          <span className={`status-badge ${headerStatus}`}>
            {headerStatusLabel}
          </span>
        </div>
      </div>
      <p className="refresh-allowance">
        {readQuota?.enabled
          ? readQuota.reliable && readQuota.remaining !== null
            ? `${readQuota.remaining} / ${readQuota.capacity} Panasonic reads available.`
            : "Panasonic read quota is temporarily unavailable."
          : "Uses the Panasonic hourly read allowance."}
      </p>
      <TabNavigation
        activeId={activeSection}
        ariaLabel="Dashboard workspace"
        idPrefix="dashboard"
        items={SECTIONS}
        onChange={selectSection}
      />
      <p className="tab-context" aria-live="polite">
        <strong>{activeSectionMeta.label}</strong>
        <span>{activeSectionMeta.description}</span>
      </p>

      {pollResult && (
        <Banner tone={pollResult.success ? "info" : pollResult.tone ?? "warning"}>
          <p>{pollResult.message}</p>
          <button className="btn btn-sm" onClick={() => setPollResult(null)}>
            Dismiss
          </button>
        </Banner>
      )}

      {error && (
        <Banner tone="danger"><p>API Error: {error}</p></Banner>
      )}

      {controlState && (
        <Banner tone={controlState.state === "comfort_at_risk" ? "warning" : controlState.state === "automatic" ? "info" : "warning"}>
          <p><strong>{controlState.headline}</strong> {controlState.detail}</p>
          {controlState.primary_action?.kind === "request" && controlState.active_override_count === 1 && (
            <button className="btn btn-danger" onClick={cancelOverride}>{controlState.primary_action.label}</button>
          )}
          {controlState.notices.map((notice) => <p key={notice.code}>{notice.detail}</p>)}
        </Banner>
      )}

      {/* ── Overview section ── */}
      <section
        id="dashboard-panel-home"
        className="workspace-panel"
        role="tabpanel"
        aria-labelledby="dashboard-tab-home"
        hidden={activeSection !== "home"}
      >
        <DecisionSummary
          plan={data?.active_plan ?? null}
          indoorTemp={indoorTemp?.avg_temperature ?? null}
          indoorTimestamp={indoorTemp?.latest_reading ?? null}
          indoorStale={indoorTemp?.last_fresh_reading !== indoorTemp?.latest_reading}
          controlState={controlState}
          onRetry={fetchData}
        />
        <Dashboard data={data} />
        <OperationalAlerts />
        <OutcomeSummary />
        <div id="controls">
          <Controls controlState={controlState} onChanged={async () => refreshNow()} />
          <LearningModeCard onChange={refreshNow} />
        </div>
      </section>

      <section
        id="dashboard-panel-timeline"
        className="workspace-panel"
        role="tabpanel"
        aria-labelledby="dashboard-tab-timeline"
        hidden={activeSection !== "timeline"}
      >
        {activeSection === "timeline" && (
          <>
            <PlanView
              plan={data?.active_plan ?? null}
              controlledActions={timelineActions}
              controlledError={timelineActionsError}
              controlledLoading={timelineActionsLoading}
            />
            <TimelineWorkspace
              planId={data?.active_plan?.id}
              controlState={controlState}
              onActivity={(actions, activityError, activityLoading) => { setTimelineActivity(actions); setTimelineActivityError(activityError); setTimelineActivityLoading(activityLoading); }}
              onActions={(actions, actionsError, actionsLoading) => { setTimelineActions(actions); setTimelineActionsError(actionsError); setTimelineActionsLoading(actionsLoading); }}
            />
            <PlanActivityTimeline activityData={timelineActivity} activityError={timelineActivityError} activityLoading={timelineActivityLoading} />
            <PlanHistory />
          </>
        )}
      </section>

      <section
        id="dashboard-panel-under-the-hood"
        className="workspace-panel"
        role="tabpanel"
        aria-labelledby="dashboard-tab-under-the-hood"
        hidden={activeSection !== "under-the-hood"}
      >
        <ConsumptionChart />
        <div id="raw-charts">
          <div style={{ marginBottom: "1rem" }}>
            <button className="btn btn-sm" onClick={() => setShowRawChartDetails((value) => !value)}>
              {showRawChartDetails
                ? "Hide raw weather, price and temperature history"
                : "Show raw weather, price and temperature history"}
            </button>
          </div>
          {showRawChartDetails && (
            <>
              <PriceChart />
              <TemperatureChart />
              <ForecastChart />
            </>
          )}
        </div>
        <ThermalPredictionChart />
        <div id="models">
          <OptimizerStatus controlState={controlState} spaceHeatingGate={data?.space_heating_gate ?? null} />
        </div>
      </section>
    </main>
  );
}

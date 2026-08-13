"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Dashboard } from "@/components/Dashboard";
import { PriceChart } from "@/components/PriceChart";
import { TemperatureChart } from "@/components/TemperatureChart";
import { ConsumptionChart } from "@/components/ConsumptionChart";
import { ForecastChart } from "@/components/ForecastChart";
import { ThermalPredictionChart } from "@/components/ThermalPredictionChart";
import { ComfortImpactChart } from "@/components/ComfortImpactChart";
import { PlanView } from "@/components/PlanView";
import { PlanHistory } from "@/components/PlanHistory";
import { PlanActivityTimeline } from "@/components/PlanActivityTimeline";
import { NextActionCard } from "@/components/NextActionCard";
import { Controls } from "@/components/Controls";
import { LearningModeCard } from "@/components/LearningModeCard";
import { OptimizerStatus } from "@/components/OptimizerStatus";
import { OutcomeSummary } from "@/components/OutcomeSummary";
import { OperationalAlerts } from "@/components/OperationalAlerts";
import { AppVersionBadge } from "@/components/AppVersionBadge";
import { TabNavigation } from "@/components/TabNavigation";
import { DecisionSummary } from "@/components/DecisionSummary";
import { SECTIONS, SectionId } from "@/lib/constants";
import { useTimeFormat, formatTime } from "@/components/useTimeFormat";

interface DashboardData {
  current_status: {
    ts: string;
    device_id: string;
    mode: string | null;
    operation_status: number | null;
    outdoor_temp: number | null;
    tank_temp: number | null;
    tank_target_temp: number | null;
    zone1_temp: number | null;
    zone1_target_temp: number | null;
    quiet_mode: number | null;
    powerful_mode: number | null;
    device_action?: string | null;
    direction?: string | null;
    space_heating_active: boolean | null;
    space_heating_evidence: string | null;
  } | null;
  current_price: number | null;
  today_kwh: number;
  today_cost_eur: number | null;
  today_cost_priced_kwh: number;
  today_cost_unpriced_kwh: number;
  today_cost_priced_amount: number;
  today_cost_coverage_pct: number;
  today_cost_complete: boolean;
  active_plan: {
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
  has_override: boolean;
  override_id: number | null;
}

interface PollResult {
  success: boolean;
  message: string;
}

interface PollStepResult {
  success?: boolean;
  message?: string;
}

interface IndoorTempData {
  avg_temperature: number | null;
  latest_reading: string | null;
  sensor_count: number;
  last_fresh_reading: string | null;
}

interface LearningModeData {
  enabled: boolean;
  since: string | null;
  days_elapsed: number | null;
}

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [indoorTemp, setIndoorTemp] = useState<IndoorTempData | null>(null);
  const [learningMode, setLearningMode] = useState<LearningModeData | null>(null);
  const [loading, setLoading] = useState(true);
  const timeFormat = useTimeFormat();
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [pollResult, setPollResult] = useState<PollResult | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [activeSection, setActiveSection] = useState<SectionId>("overview");
  const [showRawChartDetails, setShowRawChartDetails] = useState(false);
  const [showHotWaterDetails, setShowHotWaterDetails] = useState(false);
  const activeSectionMeta = SECTIONS.find((section) => section.id === activeSection) ?? SECTIONS[0];

  const selectSection = (section: SectionId) => {
    setActiveSection(section);
    const url = new URL(window.location.href);
    url.searchParams.set("view", section);
    window.history.pushState({ view: section }, "", url);
  };

  const fetchData = async () => {
    try {
      const [dashRes, tempRes] = await Promise.all([
        fetch("/api/dashboard"),
        fetch("/api/indoor-temp/latest").then((r) => (r.ok ? r.json() : null)).catch(() => null),
      ]);
      if (!dashRes.ok) throw new Error(`API error: ${dashRes.status}`);
      const json = await dashRes.json();
      setData(json);
      setIndoorTemp(tempRes);
      setError(null);
      setLastUpdated(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch data");
    } finally {
      setLoading(false);
    }
  };

  const fetchLearningMode = async () => {
    try {
      const res = await fetch("/api/learning-mode");
      setLearningMode(res.ok ? await res.json() : null);
    } catch {
      /* non-critical: banner just won't show */
    }
  };

  useEffect(() => {
    fetchData();
    fetchLearningMode();
    const interval = setInterval(() => {
      fetchData();
      fetchLearningMode();
    }, 30000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const applyLocation = () => {
      const requested = new URLSearchParams(window.location.search).get("view");
      if (SECTIONS.some((section) => section.id === requested)) {
        setActiveSection(requested as SectionId);
      }
    };
    applyLocation();
    window.addEventListener("popstate", applyLocation);
    return () => window.removeEventListener("popstate", applyLocation);
  }, []);

  /* ── Auto-dismiss a successful poll banner after a few seconds ── */
  useEffect(() => {
    if (pollResult?.success) {
      const t = setTimeout(() => setPollResult(null), 6000);
      return () => clearTimeout(t);
    }
  }, [pollResult]);

  const pollNow = async () => {
    setPolling(true);
    setPollResult(null);
    try {
      const res = await fetch("/api/poll-now", { method: "POST" });
      const json = await res.json();
      if (json.status === "ok") {
        setPollResult({ success: true, message: "All data fetched successfully" });
      } else {
        const results = (json.results ?? {}) as Record<string, PollStepResult>;
        const msgs = Object.entries(results)
          .filter(([, result]) => !result.success)
          .map(([name, result]) => `${name}: ${result.message ?? "failed"}`)
          .join("; ");
        setPollResult({ success: false, message: msgs || "Partial success" });
      }
      await fetchData();
    } catch {
      setPollResult({ success: false, message: "Network error — is the API running?" });
    } finally {
      setPolling(false);
    }
  };

  const cancelOverride = async () => {
    if (!data?.override_id) return;
    try {
      const res = await fetch(`/api/overrides/${data.override_id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`API error: ${res.status}`);
      await fetchData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to cancel override");
    }
  };

  if (loading) {
    return (
      <div className="dashboard">
        <div className="header">
          <h1>Heat Pump Optimizer</h1>
          <div className="header-actions">
            <AppVersionBadge />
            <span className="status-badge loading">Loading...</span>
          </div>
        </div>
        <div className="chart-container">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "60%" }} />
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <TabNavigation
        activeId={activeSection}
        ariaLabel="Dashboard views"
        idPrefix="dashboard"
        items={SECTIONS}
        onChange={selectSection}
      />

      <div className="header">
        <h1>Heat Pump Optimizer</h1>
        <div className="header-actions">
          <AppVersionBadge />
          {lastUpdated && (
            <span className="last-updated">
              Updated {formatTime(lastUpdated, timeFormat.hour12, { seconds: true })}
            </span>
          )}
          <button
            className="btn btn-primary"
            onClick={pollNow}
            disabled={polling}
            title="Fetch the latest prices, weather and device status right now"
          >
            {polling ? "Polling..." : "Poll Now"}
          </button>
          <Link href="/settings" className="btn">Settings</Link>
          <span
            className={`status-badge ${data?.current_status ? "online" : "offline"}`}
            title={data?.current_status ? "Receiving live data from the heat pump" : "No recent data from the heat pump"}
          >
            {data?.current_status ? "● Connected" : "● Disconnected"}
          </span>
        </div>
      </div>

      <div className="tab-context" aria-live="polite">
        <strong>{activeSectionMeta.label}</strong>
        <span>{activeSectionMeta.description}</span>
      </div>

      {pollResult && (
        <div
          className="override-banner"
          style={{
            borderColor: pollResult.success ? "var(--success)" : "var(--warning, orange)",
            background: pollResult.success ? "rgba(34,197,94,0.1)" : "rgba(251,191,36,0.1)",
          }}
        >
          <p style={{ color: pollResult.success ? "var(--success)" : "var(--warning)" }}>
            {pollResult.message}
          </p>
          <button
            className="btn btn-sm"
            onClick={() => setPollResult(null)}
            aria-label="Dismiss"
          >
            Dismiss
          </button>
        </div>
      )}

      {error && (
        <div className="override-banner" style={{ borderColor: "var(--danger)", background: "rgba(239,68,68,0.1)" }}>
          <p style={{ color: "var(--danger)" }}>API Error: {error}</p>
        </div>
      )}

      {data?.has_override && (
        <div className="override-banner">
          <p>⚠ Manual override active — optimizer paused</p>
          <button className="btn btn-danger" onClick={cancelOverride}>Cancel Override</button>
        </div>
      )}

      {learningMode?.enabled && (
        <div
          className="override-banner"
          style={{ borderColor: "var(--success)", background: "rgba(34,197,94,0.1)" }}
        >
          <p style={{ color: "var(--success)" }}>
            🎓 Learning mode active — optimizer is observing only (no device commands)
            {learningMode.days_elapsed != null
              ? ` · collecting data for ${
                  learningMode.days_elapsed < 1
                    ? `${Math.round(learningMode.days_elapsed * 24)}h`
                    : `${Math.floor(learningMode.days_elapsed)}d`
                }`
              : ""}
          </p>
        </div>
      )}

      {activeSection === "overview" && (
        <section id="dashboard-panel-overview" className="workspace-panel" role="tabpanel" aria-labelledby="dashboard-tab-overview">
          <DecisionSummary plan={data?.active_plan ?? null} indoorTemp={indoorTemp?.avg_temperature ?? null} />
          <OperationalAlerts />
          <div className="overview-secondary">
            <NextActionCard plan={data?.active_plan ?? null} />
          </div>
          <Dashboard data={data} indoorTemp={indoorTemp?.avg_temperature ?? null} indoorSensorCount={indoorTemp?.sensor_count ?? 0} lastFreshReading={indoorTemp?.last_fresh_reading ?? null} latestReading={indoorTemp?.latest_reading ?? null} />
          <OutcomeSummary />
        </section>
      )}

      {activeSection === "controls" && (
        <section id="dashboard-panel-controls" className="workspace-panel" role="tabpanel" aria-labelledby="dashboard-tab-controls">
          <Controls />
          <LearningModeCard onChange={fetchLearningMode} />
        </section>
      )}

      {activeSection === "plan" && (
        <section id="dashboard-panel-plan" className="workspace-panel" role="tabpanel" aria-labelledby="dashboard-tab-plan">
          <PlanView plan={data?.active_plan ?? null} />
          <PlanActivityTimeline />
          <PlanHistory />
        </section>
      )}

      {activeSection === "charts" && (
        <section id="dashboard-panel-charts" className="workspace-panel" role="tabpanel" aria-labelledby="dashboard-tab-charts">
          <ComfortImpactChart />
          <ConsumptionChart />
          <details className="chart-details" onToggle={(event) => setShowRawChartDetails(event.currentTarget.open)}>
            <summary>Show raw weather, price and temperature history</summary>
            {showRawChartDetails && (
              <div className="chart-detail-content">
                <TemperatureChart />
                <ForecastChart />
                <PriceChart />
              </div>
            )}
          </details>
          <details className="chart-details chart-details--hot-water" onToggle={(event) => setShowHotWaterDetails(event.currentTarget.open)}>
            <summary>Show hot-water and tank details</summary>
            {showHotWaterDetails && (
              <div className="chart-detail-content">
                <ThermalPredictionChart />
              </div>
            )}
          </details>
        </section>
      )}

      {activeSection === "status" && (
        <section id="dashboard-panel-status" className="workspace-panel" role="tabpanel" aria-labelledby="dashboard-tab-status">
          <OptimizerStatus />
        </section>
      )}
    </div>
  );
}

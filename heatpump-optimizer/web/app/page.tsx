"use client";

import { useEffect, useState, useRef } from "react";
import { Dashboard } from "@/components/Dashboard";
import { PriceChart } from "@/components/PriceChart";
import { TemperatureChart } from "@/components/TemperatureChart";
import { ConsumptionChart } from "@/components/ConsumptionChart";
import { ForecastChart } from "@/components/ForecastChart";
import { ThermalPredictionChart } from "@/components/ThermalPredictionChart";
import { PlanView } from "@/components/PlanView";
import { PlanHistory } from "@/components/PlanHistory";
import { NextActionCard } from "@/components/NextActionCard";
import { Controls } from "@/components/Controls";
import { LearningModeCard } from "@/components/LearningModeCard";
import { OptimizerStatus } from "@/components/OptimizerStatus";
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
  } | null;
  current_price: number | null;
  today_kwh: number;
  today_cost_eur: number;
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
}

interface PollResult {
  success: boolean;
  message: string;
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
  const mainRef = useRef<HTMLDivElement>(null);

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

  /* ── Intersection observer for active section highlighting ── */
  useEffect(() => {
    const ids = SECTIONS.map((s) => s.id);
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActiveSection(entry.target.id as SectionId);
          }
        }
      },
      { rootMargin: "-40% 0px -55% 0px" },
    );
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [loading]);

  const scrollTo = (id: SectionId) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

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
        const msgs = Object.entries(json.results || {})
          .filter(([, v]: [string, any]) => !v?.success)
          .map(([k, v]: [string, any]) => `${k}: ${v?.message}`)
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
          <span className="status-badge loading">Loading...</span>
        </div>
        <div className="chart-container">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "60%" }} />
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard" ref={mainRef}>
      {/* ── Sticky section nav ── */}
      <nav className="section-nav" aria-label="Dashboard sections">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            className={`section-nav-item ${activeSection === s.id ? "active" : ""}`}
            onClick={() => scrollTo(s.id)}
          >
            {s.label}
          </button>
        ))}
      </nav>

      <div className="header">
        <h1>Heat Pump Optimizer</h1>
        <div className="header-actions">
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
          <a href="/settings" className="btn">Settings</a>
          <span
            className={`status-badge ${data?.current_status ? "online" : "offline"}`}
            title={data?.current_status ? "Receiving live data from the heat pump" : "No recent data from the heat pump"}
          >
            {data?.current_status ? "● Connected" : "● Disconnected"}
          </span>
        </div>
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

      {/* ── Overview section ── */}
      <section id="overview">
        <Dashboard data={data} indoorTemp={indoorTemp?.avg_temperature ?? null} indoorSensorCount={indoorTemp?.sensor_count ?? 0} lastFreshReading={indoorTemp?.last_fresh_reading ?? null} latestReading={indoorTemp?.latest_reading ?? null} />
        <NextActionCard plan={data?.active_plan ?? null} />
      </section>

      {/* ── Controls (moved up — emergency actions should be accessible) ── */}
      <section id="controls">
        <Controls />
        <LearningModeCard onChange={fetchLearningMode} />
      </section>

      {/* ── Plan section ── */}
      <section id="plan">
        <PlanView plan={data?.active_plan ?? null} />
        <PlanHistory />
      </section>

      {/* ── Charts section ── */}
      <section id="charts">
        <PriceChart />
        <TemperatureChart />
        <ConsumptionChart />
        <ForecastChart />
        <ThermalPredictionChart />
      </section>

      {/* ── Status section ── */}
      <section id="status">
        <OptimizerStatus />
      </section>
    </div>
  );
}

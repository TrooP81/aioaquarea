"use client";

import { useEffect, useState } from "react";
import { Dashboard } from "@/components/Dashboard";
import { PriceChart } from "@/components/PriceChart";
import { TemperatureChart } from "@/components/TemperatureChart";
import { ConsumptionChart } from "@/components/ConsumptionChart";
import { ForecastChart } from "@/components/ForecastChart";
import { ThermalPredictionChart } from "@/components/ThermalPredictionChart";
import { PlanView } from "@/components/PlanView";
import { Controls } from "@/components/Controls";

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
  } | null;
  has_override: boolean;
}

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [pollResult, setPollResult] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const res = await fetch("/api/dashboard");
      if (!res.ok) throw new Error(`API error: ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch data");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000); // Refresh every 30s
    return () => clearInterval(interval);
  }, []);

  const pollNow = async () => {
    setPolling(true);
    setPollResult(null);
    try {
      const res = await fetch("/api/poll-now", { method: "POST" });
      const json = await res.json();
      if (json.status === "ok") {
        setPollResult("All data fetched successfully");
      } else {
        const msgs = Object.entries(json.results || {})
          .filter(([, v]: [string, any]) => !v?.success)
          .map(([k, v]: [string, any]) => `${k}: ${v?.message}`)
          .join("; ");
        setPollResult(msgs || "Partial success");
      }
      await fetchData();
    } catch {
      setPollResult("Network error — is the API running?");
    } finally {
      setPolling(false);
    }
  };

  if (loading) {
    return (
      <div className="dashboard">
        <div className="header">
          <h1>Heat Pump Optimizer</h1>
          <span className="status-badge offline">Loading...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <div className="header">
        <h1>Heat Pump Optimizer</h1>
        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <button className="btn btn-primary" onClick={pollNow} disabled={polling}>
            {polling ? "Polling..." : "Poll Now"}
          </button>
          <a href="/settings" className="btn">Settings</a>
          <span className={`status-badge ${data?.current_status ? "online" : "offline"}`}>
            {data?.current_status ? "● Connected" : "● Disconnected"}
          </span>
        </div>
      </div>

      {pollResult && (
        <div
          className="override-banner"
          style={{
            borderColor: pollResult.includes("successfully") ? "var(--success)" : "var(--warning, orange)",
            background: pollResult.includes("successfully") ? "rgba(34,197,94,0.1)" : "rgba(251,191,36,0.1)",
          }}
        >
          <p>{pollResult}</p>
        </div>
      )}

      {error && (
        <div className="override-banner">
          <p>API Error: {error}</p>
        </div>
      )}

      {data?.has_override && (
        <div className="override-banner">
          <p>⚠ Manual override active — optimizer paused</p>
          <button className="btn btn-danger">Cancel Override</button>
        </div>
      )}

      <Dashboard data={data} />
      <PriceChart />
      <TemperatureChart />
      <ConsumptionChart />
      <ForecastChart />
      <ThermalPredictionChart />
      <PlanView plan={data?.active_plan ?? null} />
      <Controls />
    </div>
  );
}

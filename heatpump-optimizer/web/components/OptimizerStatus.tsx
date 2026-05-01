"use client";

import { useEffect, useState } from "react";

interface ModelInfo {
  trained: boolean;
  last_trained: string | null;
  samples: number;
}

interface ThermalInfo {
  calibrated: boolean;
  tank_heating_rate: number;
  confidence: string;
  last_calibrated: string | null;
}

interface ComfortInfo {
  trained: boolean;
  last_trained: string | null;
  training_samples: number;
  metrics: Record<string, number> | null;
}

interface IndoorTempLatest {
  avg_temperature: number | null;
  latest_reading: string | null;
  sensor_count: number;
}

interface OptimizerStatusData {
  configured_layer: string;
  active_layer: string;
  fallback_layer: string;
  cop_model: ModelInfo;
  demand_model: ModelInfo;
  thermal_model: ThermalInfo;
}

const LAYER_LABELS: Record<string, string> = {
  rules_v3: "Rules Engine",
  milp_v1: "MILP Optimizer",
  "milp_v1+ml": "MILP + ML Models",
};

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 10,
        height: 10,
        borderRadius: "50%",
        background: ok ? "var(--success)" : "var(--text-muted)",
        marginRight: "0.5rem",
        flexShrink: 0,
      }}
    />
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function OptimizerStatus() {
  const [status, setStatus] = useState<OptimizerStatusData | null>(null);
  const [comfort, setComfort] = useState<ComfortInfo | null>(null);
  const [indoorTemp, setIndoorTemp] = useState<IndoorTempLatest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [training, setTraining] = useState<Record<string, boolean>>({});
  const [trainMsg, setTrainMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const refresh = () =>
    Promise.all([
      fetch("/api/optimizer/status").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/comfort-model/status").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/indoor-temp/latest").then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([opt, cmf, temp]) => {
        setStatus(opt);
        setComfort(cmf);
        setIndoorTemp(temp);
      })
      .catch(() => setError("Failed to load optimizer status"));

  useEffect(() => { refresh(); }, []);

  const trainMl = async () => {
    setTraining((p) => ({ ...p, ml: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/ml/train", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Training failed");
      const fmtResult = (r: any) =>
        r?.error ? `✗ ${r.error} (${r.samples ?? 0} samples)` : r?.version ? `✓ trained` : "unknown";
      const copStatus = fmtResult(data.cop);
      const demandStatus = fmtResult(data.demand);
      setTrainMsg({ text: `COP: ${copStatus} · Demand: ${demandStatus}`, ok: !data.cop?.error && !data.demand?.error });
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Training failed", ok: false });
    } finally {
      setTraining((p) => ({ ...p, ml: false }));
    }
  };

  const trainComfort = async () => {
    setTraining((p) => ({ ...p, comfort: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/comfort-model/train", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Training failed");
      setTrainMsg({ text: `Comfort model: ${data.status ?? "done"}`, ok: true });
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Training failed", ok: false });
    } finally {
      setTraining((p) => ({ ...p, comfort: false }));
    }
  };

  const calibrateThermal = async () => {
    setTraining((p) => ({ ...p, thermal: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/thermal/calibrate", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Calibration failed");
      setTrainMsg({ text: "Thermal model calibrated", ok: true });
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Calibration failed", ok: false });
    } finally {
      setTraining((p) => ({ ...p, thermal: false }));
    }
  };

  if (error) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Optimizer & ML Status</h2>
        <p style={{ color: "var(--danger)" }}>{error}</p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Optimizer & ML Status</h2>
        <p style={{ color: "var(--text-muted)" }}>Loading...</p>
      </div>
    );
  }

  const models = [
    {
      label: "COP Model",
      trained: status.cop_model.trained,
      lastTrained: status.cop_model.last_trained,
      detail: `${status.cop_model.samples} samples`,
    },
    {
      label: "Demand Model",
      trained: status.demand_model.trained,
      lastTrained: status.demand_model.last_trained,
      detail: `${status.demand_model.samples} samples`,
    },
    {
      label: "Thermal Model",
      trained: status.thermal_model.calibrated,
      lastTrained: status.thermal_model.last_calibrated,
      detail: `Rate: ${status.thermal_model.tank_heating_rate} °C/h · ${status.thermal_model.confidence}`,
    },
  ];

  if (comfort) {
    const metricsStr = comfort.metrics
      ? Object.entries(comfort.metrics)
          .map(([k, v]) => `${k}: ${typeof v === "number" ? v.toFixed(3) : v}`)
          .join(" · ")
      : "";
    models.push({
      label: "Comfort Model",
      trained: comfort.trained,
      lastTrained: comfort.last_trained,
      detail: `${comfort.training_samples} samples${metricsStr ? ` · ${metricsStr}` : ""}`,
    });
  }

  return (
    <div className="plan-section">
      <h2 className="chart-title">Optimizer & ML Status</h2>

      {/* Active layer badge */}
      <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "1.25rem" }}>
        <span style={{ color: "var(--text-muted)", fontSize: "0.875rem" }}>Active layer</span>
        <span
          style={{
            padding: "0.25rem 0.75rem",
            borderRadius: "9999px",
            fontSize: "0.75rem",
            fontWeight: 600,
            background: status.active_layer.includes("ml")
              ? "rgba(34,197,94,0.15)"
              : status.active_layer.includes("milp")
              ? "rgba(59,130,246,0.15)"
              : "rgba(148,163,184,0.15)",
            color: status.active_layer.includes("ml")
              ? "var(--success)"
              : status.active_layer.includes("milp")
              ? "var(--accent)"
              : "var(--text-muted)",
          }}
        >
          {LAYER_LABELS[status.active_layer] || status.active_layer}
        </span>
        <span style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}>
          (configured: {status.configured_layer})
        </span>
      </div>

      {/* Model cards */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "1rem",
        }}
      >
        {models.map((m) => (
          <div
            key={m.label}
            style={{
              background: "var(--bg)",
              borderRadius: "0.5rem",
              padding: "1rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", marginBottom: "0.5rem" }}>
              <StatusDot ok={m.trained} />
              <span style={{ fontWeight: 600, fontSize: "0.875rem" }}>{m.label}</span>
            </div>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", lineHeight: 1.6 }}>
              <div>Status: {m.trained ? "Trained" : "Not trained"}</div>
              <div>Last trained: {formatDate(m.lastTrained)}</div>
              <div>{m.detail}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Training controls */}
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", marginTop: "1.25rem" }}>
        <button
          className="btn"
          onClick={trainMl}
          disabled={training.ml}
          style={{ fontSize: "0.8rem", padding: "0.35rem 0.75rem" }}
        >
          {training.ml ? "Training..." : "Train COP & Demand"}
        </button>
        <button
          className="btn"
          onClick={trainComfort}
          disabled={training.comfort}
          style={{ fontSize: "0.8rem", padding: "0.35rem 0.75rem" }}
        >
          {training.comfort ? "Training..." : "Train Comfort Model"}
        </button>
        <button
          className="btn"
          onClick={calibrateThermal}
          disabled={training.thermal}
          style={{ fontSize: "0.8rem", padding: "0.35rem 0.75rem" }}
        >
          {training.thermal ? "Calibrating..." : "Calibrate Thermal"}
        </button>
      </div>
      {trainMsg && (
        <p style={{
          fontSize: "0.8rem",
          marginTop: "0.5rem",
          color: trainMsg.ok ? "var(--success)" : "var(--danger)",
        }}>
          {trainMsg.text}
        </p>
      )}

      {/* SmartThings indoor temperature */}
      {indoorTemp && indoorTemp.avg_temperature != null && (
        <>
          <h3 style={{ fontSize: "0.875rem", fontWeight: 600, marginTop: "1.25rem", marginBottom: "0.75rem" }}>
            SmartThings Indoor Temperature
          </h3>
          <div
            style={{
              display: "flex",
              gap: "1rem",
              alignItems: "center",
              background: "var(--bg)",
              borderRadius: "0.5rem",
              padding: "0.75rem 1rem",
            }}
          >
            <div style={{ fontSize: "1.5rem", fontWeight: 700, color: "var(--warning)" }}>
              {indoorTemp.avg_temperature.toFixed(1)}°C
            </div>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", lineHeight: 1.6 }}>
              <div>Average across {indoorTemp.sensor_count} sensor{indoorTemp.sensor_count !== 1 ? "s" : ""}</div>
              <div>Last reading: {formatDate(indoorTemp.latest_reading)}</div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

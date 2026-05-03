"use client";

import { useEffect, useState } from "react";
import { LAYER_LABELS } from "@/lib/constants";

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

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span className={`status-dot ${ok ? "status-dot--ok" : ""}`} />
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function layerBadgeClass(layer: string): string {
  if (layer.includes("ml")) return "opt-layer-badge opt-layer-badge--ml";
  if (layer.includes("milp")) return "opt-layer-badge opt-layer-badge--milp";
  return "opt-layer-badge";
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
        <p className="text-danger">{error}</p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Optimizer & ML Status</h2>
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "60%" }} />
        </div>
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
      <div className="opt-layer-row">
        <span className="text-muted text-sm">Active layer</span>
        <span className={layerBadgeClass(status.active_layer)}>
          {LAYER_LABELS[status.active_layer] || status.active_layer}
        </span>
        <span className="text-muted text-xs">
          (configured: {status.configured_layer})
        </span>
      </div>

      {/* Model cards */}
      <div className="model-grid">
        {models.map((m) => (
          <div key={m.label} className="model-card">
            <div className="model-card-header">
              <StatusDot ok={m.trained} />
              <span className="model-card-name">{m.label}</span>
            </div>
            <div className="model-card-details">
              <div>Status: {m.trained ? "Trained" : "Not trained"}</div>
              <div>Last trained: {formatDate(m.lastTrained)}</div>
              <div>{m.detail}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Training controls */}
      <div className="training-controls">
        <button
          className="btn btn-sm"
          onClick={trainMl}
          disabled={training.ml}
          aria-busy={training.ml}
        >
          {training.ml ? "Training..." : "Train COP & Demand"}
        </button>
        <button
          className="btn btn-sm"
          onClick={trainComfort}
          disabled={training.comfort}
          aria-busy={training.comfort}
        >
          {training.comfort ? "Training..." : "Train Comfort Model"}
        </button>
        <button
          className="btn btn-sm"
          onClick={calibrateThermal}
          disabled={training.thermal}
          aria-busy={training.thermal}
        >
          {training.thermal ? "Calibrating..." : "Calibrate Thermal"}
        </button>
      </div>
      {trainMsg && (
        <p className={`train-msg ${trainMsg.ok ? "train-msg--ok" : "train-msg--err"}`}>
          {trainMsg.text}
        </p>
      )}

      {/* SmartThings indoor temperature */}
      {indoorTemp && indoorTemp.avg_temperature != null && (
        <>
          <h3 className="indoor-temp-heading">SmartThings Indoor Temperature</h3>
          <div className="indoor-temp-card">
            <div className="indoor-temp-value">
              {indoorTemp.avg_temperature.toFixed(1)}°C
            </div>
            <div className="model-card-details">
              <div>Average across {indoorTemp.sensor_count} sensor{indoorTemp.sensor_count !== 1 ? "s" : ""}</div>
              <div>Last reading: {formatDate(indoorTemp.latest_reading)}</div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

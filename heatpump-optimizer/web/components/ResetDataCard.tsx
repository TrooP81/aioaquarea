"use client";

import { useState } from "react";

interface ScopeOption {
  key: string;
  label: string;
  description: string;
  feedsModels?: boolean;
}

const SCOPES: ScopeOption[] = [
  {
    key: "indoor_temp",
    label: "Indoor temperature readings",
    description: "SmartThings sensor history (clear this to drop wrong-sensor data)",
    feedsModels: true,
  },
  {
    key: "energy",
    label: "Energy & COP history",
    description: "Consumption records and computed COP values",
    feedsModels: true,
  },
  {
    key: "device_status",
    label: "Device status history",
    description: "Heat pump status, shower events, and fault records",
    feedsModels: true,
  },
  {
    key: "weather",
    label: "Weather history",
    description: "Stored weather observations and forecasts",
    feedsModels: true,
  },
  {
    key: "prices",
    label: "Price history",
    description: "Electricity spot price records",
  },
  {
    key: "plans",
    label: "Plans & overrides",
    description: "Optimizer plans, scheduled actions, and manual overrides",
  },
  {
    key: "logs",
    label: "Audit & app logs",
    description: "Audit trail and application log entries",
  },
];

interface ResetResult {
  deleted_rows: Record<string, number>;
  total_rows_deleted: number;
  models_reset: boolean;
  deleted_models: string[];
}

export function ResetDataCard() {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ResetResult | null>(null);

  const allSelected = selected.size === SCOPES.length;

  const toggle = (key: string) => {
    setResult(null);
    setError(null);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const toggleAll = () => {
    setResult(null);
    setError(null);
    setSelected((prev) =>
      prev.size === SCOPES.length ? new Set() : new Set(SCOPES.map((s) => s.key))
    );
  };

  const handleReset = async () => {
    if (selected.size === 0) return;
    const scopes = SCOPES.filter((s) => selected.has(s.key)).map((s) => s.label);
    const message = allSelected
      ? "Start completely fresh? This permanently deletes ALL collected data and trained models. Settings and your SmartThings connection are kept. This cannot be undone."
      : `Permanently delete the following? This cannot be undone.\n\n- ${scopes.join("\n- ")}`;
    if (!confirm(message)) return;

    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/admin/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scopes: [...selected] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || "Reset failed");
        return;
      }
      setResult(await res.json());
      setSelected(new Set());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setSubmitting(false);
    }
  };

  const modelsWillReset = SCOPES.some((s) => s.feedsModels && selected.has(s.key));

  return (
    <div
      className="plan-section"
      style={{ borderColor: "var(--danger)", borderWidth: 1, borderStyle: "solid" }}
    >
      <h2 className="chart-title" style={{ color: "var(--danger)" }}>
        Danger Zone — Reset Data
      </h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Permanently delete collected data so models can train from scratch. Your settings,
        credentials, and SmartThings connection are always kept. This cannot be undone.
      </p>

      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.6rem",
          fontSize: "0.875rem",
          fontWeight: 600,
          marginBottom: "0.75rem",
          cursor: "pointer",
        }}
      >
        <input type="checkbox" checked={allSelected} onChange={toggleAll} />
        Start everything fresh (select all)
      </label>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
        {SCOPES.map((scope) => (
          <label
            key={scope.key}
            style={{ display: "flex", alignItems: "flex-start", gap: "0.6rem", cursor: "pointer" }}
          >
            <input
              type="checkbox"
              checked={selected.has(scope.key)}
              onChange={() => toggle(scope.key)}
              style={{ marginTop: "0.2rem" }}
            />
            <span>
              <span style={{ fontSize: "0.875rem", fontWeight: 500 }}>{scope.label}</span>
              <span style={{ display: "block", fontSize: "0.75rem", color: "var(--text-muted)" }}>
                {scope.description}
              </span>
            </span>
          </label>
        ))}
      </div>

      {modelsWillReset && (
        <p style={{ fontSize: "0.75rem", color: "var(--warning, orange)", marginTop: "0.75rem" }}>
          ⓘ Trained ML models (COP, demand, comfort, thermal) will also be reset, since the data
          they learned from is being cleared.
        </p>
      )}

      {error && (
        <p style={{ color: "var(--danger)", fontSize: "0.8rem", marginTop: "0.75rem" }}>{error}</p>
      )}

      {result && (
        <div
          className="override-banner"
          style={{ borderColor: "var(--success)", background: "rgba(34,197,94,0.1)", marginTop: "0.75rem" }}
        >
          <p style={{ color: "var(--success)", fontSize: "0.85rem" }}>
            Deleted {result.total_rows_deleted} record(s)
            {result.models_reset ? " and reset trained models." : "."}
          </p>
        </div>
      )}

      <button
        className="btn"
        onClick={handleReset}
        disabled={submitting || selected.size === 0}
        style={{
          marginTop: "1rem",
          borderColor: "var(--danger)",
          color: "var(--danger)",
        }}
      >
        {submitting ? "Resetting..." : "Reset Selected Data"}
      </button>
    </div>
  );
}

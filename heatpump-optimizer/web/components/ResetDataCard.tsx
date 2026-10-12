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
    label: "Energy history",
    description: "Consumption records only; retired COP history is retained",
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

function resetErrorMessage(data: unknown): string {
  if (typeof data !== "object" || data === null || Array.isArray(data)) return "Reset failed";
  const detail = (data as Record<string, unknown>).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return "Reset failed";
  const refusal = detail as Record<string, unknown>;
  if (refusal.code === "unresolved_safety_reverts") {
    const actionId = refusal.action_id;
    const action = typeof actionId === "number" && Number.isSafeInteger(actionId) && actionId > 0
      ? ` (action ${actionId})` : "";
    return `Reset blocked: an unresolved safety restore${action} must be settled first.`;
  }
  if (refusal.code === "ml_lifecycle_unavailable") {
    return "Reset unavailable: model lifecycle storage could not be reached. Try again later.";
  }
  if (refusal.code === "manual_cop_reset_unavailable") {
    return "Manual COP reset unavailable: nothing was deleted. Try again later.";
  }
  return "Reset failed";
}

const MANUAL_COP_RESET_MESSAGE =
  "Delete every manual controller-COP reading for all devices and turn experimental COP forecasting off? " +
  "Revision tokens are never reused. All other data, settings, models, COP history and pending safety restores are kept. " +
  "This cannot be undone.";

export function ResetDataCard() {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ResetResult | null>(null);
  const [manualSubmitting, setManualSubmitting] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);
  const [manualDeleted, setManualDeleted] = useState<number | null>(null);

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
      ? "Reset all resettable data and affected retained models? COP history, COP artifacts and all model baselines are kept. Manual controller-COP readings and their revision sequence are kept, along with settings and your SmartThings connection. This cannot be undone."
      : `Permanently delete the following? COP history, COP artifacts and all model baselines are kept. Manual controller-COP readings and their revision sequence are kept. This cannot be undone.\n\n- ${scopes.join("\n- ")}`;
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
        setError(resetErrorMessage(data));
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

  const handleManualReset = async () => {
    if (!confirm(MANUAL_COP_RESET_MESSAGE)) return;
    setManualSubmitting(true);
    setManualError(null);
    setManualDeleted(null);
    try {
      const res = await fetch("/api/admin/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scopes: ["manual_cop"] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setManualError(resetErrorMessage(data));
        return;
      }
      const data: unknown = await res.json();
      const deleted = typeof data === "object" && data !== null
        ? (data as Record<string, unknown>).total_rows_deleted : undefined;
      if (typeof deleted !== "number" || !Number.isSafeInteger(deleted) || deleted < 0) {
        setManualError("Manual COP reset response is invalid");
        return;
      }
      setManualDeleted(deleted);
    } catch (err: unknown) {
      setManualError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setManualSubmitting(false);
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
        Permanently delete selected data and reset affected retained models. COP history,
        COP artifacts and all model baselines are retained. Manual controller-COP readings
        and their revision sequence are also retained, along with settings, credentials
        and your SmartThings connection. This cannot be undone.
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
        Select all resettable data
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
          ⓘ Retained models (demand, comfort, thermal) will also be reset, since the data
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
            {result.models_reset ? " and reset retained models." : "."}
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

      <div
        data-testid="manual-cop-reset"
        style={{ marginTop: "1.5rem", paddingTop: "1rem", borderTop: "1px solid var(--border)" }}
      >
        <h3 style={{ fontSize: "0.95rem", margin: "0 0 0.5rem" }}>Manual controller-COP ledger</h3>
        <p style={{ color: "var(--text-muted)", fontSize: "0.8rem", marginBottom: "0.75rem" }}>
          Separate from the scopes above and never included in &quot;Select all&quot;. Deletes the
          manual COP readings of every device and turns experimental COP forecasting off.
          Revision tokens are never reused; other data, settings, models and pending safety
          restores are kept.
        </p>
        {manualError && (
          <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8rem" }}>{manualError}</p>
        )}
        {manualDeleted !== null && (
          <p role="status" style={{ color: "var(--success)", fontSize: "0.85rem" }}>
            Deleted {manualDeleted} manual reading(s); experimental COP forecasting is off.
          </p>
        )}
        <button
          className="btn"
          onClick={handleManualReset}
          disabled={manualSubmitting || submitting}
          style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
        >
          {manualSubmitting ? "Resetting..." : "Reset manual COP readings"}
        </button>
      </div>
    </div>
  );
}

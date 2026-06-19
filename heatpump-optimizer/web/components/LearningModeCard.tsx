"use client";

import { useEffect, useState } from "react";

interface LearningModeState {
  enabled: boolean;
  since: string | null;
  days_elapsed: number | null;
}

interface ModelsReady {
  ready: number;
  total: number;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function formatDuration(days: number | null): string {
  if (days == null) return "—";
  if (days < 1) {
    const hours = Math.round(days * 24);
    return `${hours} hour${hours !== 1 ? "s" : ""}`;
  }
  const whole = Math.floor(days);
  return `${whole} day${whole !== 1 ? "s" : ""}`;
}

export function LearningModeCard({ onChange }: { onChange?: () => void }) {
  const [state, setState] = useState<LearningModeState | null>(null);
  const [models, setModels] = useState<ModelsReady | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);

  const refresh = () =>
    Promise.all([
      fetch("/api/learning-mode").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/optimizer/status").then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([lm, opt]) => {
        setState(lm);
        if (opt) {
          const ready = [
            opt.cop_model?.trained,
            opt.demand_model?.trained,
            opt.thermal_model?.calibrated,
          ].filter(Boolean).length;
          setModels({ ready, total: 3 });
        }
        setError(null);
      })
      .catch(() => setError("Failed to load learning mode status"));

  useEffect(() => {
    refresh();
  }, []);

  const toggle = async () => {
    if (!state) return;
    const next = !state.enabled;
    const confirmMsg = next
      ? "Enable learning mode? The optimizer will keep planning but will not send any commands to the heat pump, so it runs naturally while training data is collected. This stays on until you turn it off."
      : "Disable learning mode? The optimizer will resume sending commands to the heat pump.";
    if (!window.confirm(confirmMsg)) return;

    setSaving(true);
    setMessage(null);
    try {
      const res = await fetch("/api/learning-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to update learning mode");
      }
      const data: LearningModeState = await res.json();
      setState(data);
      setMessage({
        text: next ? "Learning mode enabled" : "Learning mode disabled",
        ok: true,
      });
      onChange?.();
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : "Update failed", ok: false });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="plan-section">
      <h2 className="chart-title">Learning Mode</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Train the system over a long period. While on, the optimizer observes only — it keeps
        generating plans but sends no commands to the heat pump, so natural usage data is collected
        for the ML models. Toggle off to let the optimizer act.
      </p>

      {error && <p className="text-danger">{error}</p>}

      {!state && !error && (
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "60%" }} />
        </div>
      )}

      {state && (
        <>
          <div className="controls" style={{ display: "flex", alignItems: "center", gap: "1rem", flexWrap: "wrap" }}>
            <span
              className={`status-badge ${state.enabled ? "online" : "offline"}`}
              role="status"
            >
              {state.enabled ? "● Learning" : "● Off"}
            </span>
            <button
              className={`btn ${state.enabled ? "btn-danger" : "btn-primary"}`}
              onClick={toggle}
              disabled={saving}
              aria-busy={saving}
            >
              {saving
                ? "Saving..."
                : state.enabled
                  ? "Turn Off Learning Mode"
                  : "Turn On Learning Mode"}
            </button>
          </div>

          {state.enabled && (
            <div className="model-card-details" style={{ marginTop: "1rem" }}>
              <div>Started: {formatDate(state.since)}</div>
              <div>Collecting data for: {formatDuration(state.days_elapsed)}</div>
              {models && (
                <div>
                  Models ready: {models.ready}/{models.total}
                </div>
              )}
            </div>
          )}

          {message && (
            <p
              className={`train-msg ${message.ok ? "train-msg--ok" : "train-msg--err"}`}
              style={{ marginTop: "0.75rem" }}
            >
              {message.text}
            </p>
          )}
        </>
      )}
    </div>
  );
}

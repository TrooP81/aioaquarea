"use client";

import { useEffect, useState } from "react";
import { Banner } from "./Banner";

interface LearningModeState {
  enabled: boolean;
  since: string | null;
  days_elapsed: number | null;
  effective_active: boolean;
  sources: string[];
  state_reliable: boolean;
  open_revert_obligations: {
    count: number;
    oldest_scheduled_at: string | null;
    oldest_age_seconds: number | null;
    action_types: string[];
  };
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

function formatAge(seconds: number | null): string {
  if (seconds == null) return "unknown";
  if (seconds < 60) return `${seconds} seconds`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} minute${minutes !== 1 ? "s" : ""}`;
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
      let res = await fetch("/api/learning-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (res.status === 409 && next) {
        const blocked = await res.json().catch(() => ({}));
        const obligations = blocked.detail?.obligations;
        const count = obligations?.count ?? "an";
        setMessage({
          text: `${count} unresolved safety restore${count === 1 ? "" : "s"} must wait until control resumes.`,
          ok: false,
        });
        const force = window.confirm(
          "Safety restores are still unresolved. Force learning mode and delay those restores until learning mode is turned off?"
        );
        if (!force) return;
        res = await fetch("/api/learning-mode?force=true", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        });
      }
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

      {error && <Banner tone="danger"><p>{error}</p></Banner>}

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

          {state.open_revert_obligations?.count > 0 && (
            <div className="model-card-details" style={{ marginTop: "1rem" }} role="status">
              <div>
                Pending safety restores: {state.open_revert_obligations.count}
              </div>
              <div>
                Oldest restore age: {formatAge(state.open_revert_obligations.oldest_age_seconds)}
              </div>
              <div>
                Restore types: {state.open_revert_obligations.action_types.join(", ") || "unknown"}
              </div>
              <div>Safety restores wait until control resumes.</div>
            </div>
          )}

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

          {message && <Banner tone={message.ok ? "info" : "warning"}><p>{message.text}</p></Banner>}
        </>
      )}
    </div>
  );
}

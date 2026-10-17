"use client";

import { useEffect, useState } from "react";
import { Banner } from "./Banner";
import { ResetHistoryNotice } from "./ResetHistoryNotice";
import type { ModelLifecycleStatuses, ResetHistory } from "@/lib/api-types";

interface LearningModeState {
  enabled: boolean;
  since: string | null;
  days_elapsed: number | null;
  effective_active: boolean;
  sources: string[];
  state_reliable: boolean;
  reset_history?: ResetHistory | null;
  open_revert_obligations: {
    count: number | null;
    oldest_scheduled_at: string | null;
    oldest_age_seconds: number | null;
    action_types: string[];
  };
}

interface ModelsReady {
  ready: number;
  total: number;
  unavailable: number;
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
          const lifecycle: ModelLifecycleStatuses = opt.model_lifecycle ?? {};
          const states = [lifecycle.demand?.status, lifecycle.comfort?.status, lifecycle.thermal?.status];
          setModels({
            ready: states.filter((value) => value === "qualified-for-reporting").length,
            unavailable: states.filter((value) => !value || value === "unavailable").length,
            total: 3,
          });
        } else {
          setModels(null);
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
      : "Disable manual learning mode? Seasonal observation, if active, stays on. Existing linked safety restores may run only outside all positive observation; ordinary writes remain prohibited in this build.";
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
          text: `${count} unresolved safety restore${count === 1 ? "" : "s"} will be held while observation is active.`,
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
        text: next ? "Learning mode enabled" : data.effective_active
          ? "Manual learning mode disabled; seasonal observation remains active"
          : "Learning mode disabled",
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
        Ordinary writes are prohibited in this build. Optimization requires a new reviewed,
        approved build. Learning mode also holds existing linked safety restores.
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
              className={`status-badge ${(state.effective_active ?? state.enabled) ? "online" : "offline"}`}
              role="status"
            >
              {state.state_reliable === false ? (state.effective_active ?? state.enabled)
                ? "Learning (other source unavailable)" : "Observation state unavailable" : (state.effective_active ?? state.enabled)
                ? state.enabled ? "Learning" : "Learning (seasonal)"
                : "Off"}
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

          {state.open_revert_obligations?.count == null && (
            <p role="status">Current safety obligations unavailable — not known clear.</p>
          )}
          {state.open_revert_obligations?.count != null && state.open_revert_obligations.count > 0 && (
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
              <div>Safety restores are held while observation is active.</div>
            </div>
          )}
          <ResetHistoryNotice history={state.reset_history} />

          {(state.effective_active ?? state.enabled) && (
            <div className="model-card-details" style={{ marginTop: "1rem" }}>
              <div>Retained campaign started: {formatDate(state.since)}</div>
              <div>Campaign elapsed (not evidence age): {formatDuration(state.days_elapsed)}</div>
              {state.sources?.includes("seasonal") && <div>Seasonal observation remains active until explicit human exit.</div>}
              {models && (
                <div>
                  Qualified for reporting: {models.ready}/{models.total}
                  {models.unavailable > 0 && ` (${models.unavailable} unavailable)`}
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

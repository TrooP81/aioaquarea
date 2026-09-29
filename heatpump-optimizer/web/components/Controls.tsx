"use client";

import { useState } from "react";
import { formatTime } from "@/lib/constants";
import type { ControlState } from "@/lib/api-types";

export function Controls({ controlState, onChanged }: { controlState: ControlState | null; onChanged: () => Promise<void> }) {
  const [overrideHours, setOverrideHours] = useState(2);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const [confirming, setConfirming] = useState(false);

  const createOverride = async () => {
    const now = new Date();
    const end = new Date(now.getTime() + overrideHours * 60 * 60 * 1000);

    try {
      const res = await fetch("/api/overrides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ts_from: now.toISOString(),
          ts_to: end.toISOString(),
          action_type: "pause_all",
          reason: `Manual pause for ${overrideHours}h`,
        }),
      });
      if (res.ok) {
        setMessage({ text: `Optimizer paused for ${overrideHours} hours`, ok: true });
        setConfirming(false);
        await onChanged();
      } else {
        setMessage({ text: "Failed to create override", ok: false });
      }
    } catch {
      setMessage({ text: "Network error", ok: false });
    }
  };

  return (
    <div className="plan-section">
      <h2 className="chart-title">Manual Controls</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Override the optimizer temporarily. Manual overrides always take priority.
      </p>

      <div className="controls">
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <label htmlFor="pause-duration" style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
            Pause optimizer for:
          </label>
          <select
            id="pause-duration"
            value={overrideHours}
            onChange={(e) => setOverrideHours(Number(e.target.value))}
            className="form-select"
          >
            <option value={1}>1 hour</option>
            <option value={2}>2 hours</option>
            <option value={4}>4 hours</option>
            <option value={8}>8 hours</option>
            <option value={24}>24 hours</option>
          </select>
          <button className="btn btn-primary" onClick={() => setConfirming(true)} disabled={controlState?.state === "paused_by_user"}>
            Pause Optimizer
          </button>
        </div>
      </div>

      {controlState?.state === "paused_by_user" && (
        <p className="text-warning text-sm">
          Paused until {formatTime(controlState.until)}. {controlState.active_override_count > 1 ? "Another active override remains after a resume." : "Resume is available in the header."}
        </p>
      )}
      {confirming && (
        <div className="banner banner--warning">
          <p>Pause for {overrideHours} hour{overrideHours === 1 ? "" : "s"}, ending at {formatTime(new Date(Date.now() + overrideHours * 3_600_000))}?</p>
          <button className="btn btn-primary" onClick={createOverride}>Confirm pause</button>
          <button className="btn" onClick={() => setConfirming(false)}>Cancel</button>
        </div>
      )}

      {message && (
        <p style={{ color: message.ok ? "var(--success)" : "var(--danger)", fontSize: "0.875rem", marginTop: "1rem" }}>
          {message.text}
        </p>
      )}
    </div>
  );
}

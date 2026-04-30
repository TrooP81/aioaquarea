"use client";

import { useState } from "react";

export function Controls() {
  const [overrideHours, setOverrideHours] = useState(2);
  const [message, setMessage] = useState<string | null>(null);

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
        setMessage(`Optimizer paused for ${overrideHours} hours`);
      } else {
        setMessage("Failed to create override");
      }
    } catch {
      setMessage("Network error");
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
          <label style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
            Pause optimizer for:
          </label>
          <select
            value={overrideHours}
            onChange={(e) => setOverrideHours(Number(e.target.value))}
            style={{
              background: "var(--bg)",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: "0.375rem",
              padding: "0.375rem 0.75rem",
              fontSize: "0.875rem",
            }}
          >
            <option value={1}>1 hour</option>
            <option value={2}>2 hours</option>
            <option value={4}>4 hours</option>
            <option value={8}>8 hours</option>
            <option value={24}>24 hours</option>
          </select>
          <button className="btn btn-primary" onClick={createOverride}>
            Pause Optimizer
          </button>
        </div>
      </div>

      {message && (
        <p style={{ color: "var(--success)", fontSize: "0.875rem", marginTop: "1rem" }}>
          {message}
        </p>
      )}
    </div>
  );
}

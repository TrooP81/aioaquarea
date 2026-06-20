"use client";

import { useEffect, useState } from "react";

interface Sensor {
  device_id: string;
  label: string;
  room_id: string | null;
}

interface Props {
  value: string;
  onChange: (next: string) => void;
}

function parseIds(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function SmartThingsSensorSelector({ value, onChange }: Props) {
  const [sensors, setSensors] = useState<Sensor[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const selected = parseIds(value);

  const discover = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/smartthings/devices");
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || "Failed to discover sensors");
        setSensors(null);
        return;
      }
      const data = await res.json();
      setSensors(data.devices || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to discover sensors");
      setSensors(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    discover();
  }, []);

  const commit = (ids: string[]) => {
    // Preserve discovered order, then append any selected-but-unavailable ids.
    const discovered = (sensors || []).map((s) => s.device_id);
    const ordered = discovered.filter((id) => ids.includes(id));
    for (const id of ids) {
      if (!ordered.includes(id)) ordered.push(id);
    }
    onChange(ordered.join(","));
  };

  const toggle = (id: string) => {
    const set = new Set(selected);
    if (set.has(id)) {
      set.delete(id);
    } else {
      set.add(id);
    }
    commit([...set]);
  };

  const selectAll = () => {
    onChange((sensors || []).map((s) => s.device_id).join(","));
  };

  const clearAll = () => onChange("");

  // Selected ids that are no longer reported by discovery (e.g. removed sensor).
  const unavailable = selected.filter(
    (id) => sensors !== null && !sensors.some((s) => s.device_id === id)
  );

  const wrapStyle: React.CSSProperties = {
    padding: "0.75rem 1rem",
    border: "1px solid var(--border)",
    borderRadius: "0.5rem",
    background: "var(--surface)",
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
  };

  return (
    <div style={wrapStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.875rem", fontWeight: 500 }}>Sensors to poll</span>
        <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
          {selected.length === 0
            ? "None selected — all discovered sensors are polled automatically"
            : `${selected.length} selected`}
        </span>
        <button
          type="button"
          className="btn"
          onClick={discover}
          disabled={loading}
          style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem", marginLeft: "auto" }}
        >
          {loading ? "Scanning..." : "Rescan"}
        </button>
      </div>

      {loading && (
        <p style={{ fontSize: "0.8rem", color: "var(--text-muted)", margin: 0 }}>
          Discovering SmartThings temperature sensors...
        </p>
      )}

      {!loading && error && (
        <p style={{ fontSize: "0.8rem", color: "var(--danger)", margin: 0 }}>
          {error}
        </p>
      )}

      {!loading && !error && sensors !== null && sensors.length === 0 && (
        <p style={{ fontSize: "0.8rem", color: "var(--text-muted)", margin: 0 }}>
          No temperature sensors found on your SmartThings account.
        </p>
      )}

      {!loading && !error && sensors !== null && sensors.length > 0 && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
            {sensors.map((s) => {
              const checked = selected.includes(s.device_id);
              return (
                <label
                  key={s.device_id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.6rem",
                    fontSize: "0.875rem",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(s.device_id)}
                  />
                  <span style={{ fontWeight: 500 }}>{s.label || "(unnamed sensor)"}</span>
                  <span style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                    {s.device_id}
                  </span>
                </label>
              );
            })}
          </div>

          <div style={{ display: "flex", gap: "0.5rem" }}>
            <button
              type="button"
              className="btn"
              onClick={selectAll}
              style={{ fontSize: "0.8rem", padding: "0.2rem 0.7rem" }}
            >
              Select all
            </button>
            <button
              type="button"
              className="btn"
              onClick={clearAll}
              style={{ fontSize: "0.8rem", padding: "0.2rem 0.7rem" }}
            >
              Clear
            </button>
          </div>
        </>
      )}

      {unavailable.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          <span style={{ fontSize: "0.75rem", color: "var(--warning, orange)" }}>
            Selected but not currently discovered:
          </span>
          {unavailable.map((id) => (
            <label
              key={id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.6rem",
                fontSize: "0.8rem",
                cursor: "pointer",
              }}
            >
              <input type="checkbox" checked onChange={() => toggle(id)} />
              <span style={{ color: "var(--text-muted)" }}>{id}</span>
            </label>
          ))}
        </div>
      )}

      <p style={{ fontSize: "0.7rem", color: "var(--text-muted)", margin: 0 }}>
        Remember to press Save Settings to apply your selection.
      </p>
    </div>
  );
}

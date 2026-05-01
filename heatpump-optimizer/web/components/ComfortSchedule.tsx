"use client";

import { useEffect, useState, useCallback } from "react";

const HOURS = Array.from({ length: 24 }, (_, i) => i);
const DAY_TYPES = ["weekday", "weekend"] as const;
type DayType = (typeof DAY_TYPES)[number];

interface Schedule {
  weekday: number[];
  weekend: number[];
}

interface LearnedSchedule {
  weekday: Record<string, number>;
  weekend: Record<string, number>;
}

function formatHour(h: number): string {
  return `${h.toString().padStart(2, "0")}`;
}

export function ComfortSchedule() {
  const [schedule, setSchedule] = useState<Schedule>({ weekday: [], weekend: [] });
  const [learned, setLearned] = useState<LearnedSchedule | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const [dragging, setDragging] = useState<{ dayType: DayType; adding: boolean } | null>(null);

  const fetchSchedule = useCallback(async () => {
    try {
      const [schedRes, learnedRes] = await Promise.all([
        fetch("/api/comfort-schedule"),
        fetch("/api/comfort-schedule/learned"),
      ]);
      if (schedRes.ok) setSchedule(await schedRes.json());
      if (learnedRes.ok) setLearned(await learnedRes.json());
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    fetchSchedule();
  }, [fetchSchedule]);

  const toggleHour = (dayType: DayType, hour: number) => {
    setSchedule((prev) => {
      const hours = prev[dayType];
      const next = hours.includes(hour)
        ? hours.filter((h) => h !== hour)
        : [...hours, hour].sort((a, b) => a - b);
      return { ...prev, [dayType]: next };
    });
  };

  const handleCellEnter = (dayType: DayType, hour: number) => {
    if (!dragging || dragging.dayType !== dayType) return;
    setSchedule((prev) => {
      const hours = prev[dayType];
      if (dragging.adding && !hours.includes(hour)) {
        return { ...prev, [dayType]: [...hours, hour].sort((a, b) => a - b) };
      }
      if (!dragging.adding && hours.includes(hour)) {
        return { ...prev, [dayType]: hours.filter((h) => h !== hour) };
      }
      return prev;
    });
  };

  const handleMouseDown = (dayType: DayType, hour: number) => {
    const isActive = schedule[dayType].includes(hour);
    setDragging({ dayType, adding: !isActive });
    toggleHour(dayType, hour);
  };

  useEffect(() => {
    const handleUp = () => setDragging(null);
    window.addEventListener("mouseup", handleUp);
    return () => window.removeEventListener("mouseup", handleUp);
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const res = await fetch("/api/comfort-schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(schedule),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
      setMessage({ text: "Schedule saved", type: "success" });
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : "Save failed", type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const applyLearned = async () => {
    try {
      const res = await fetch("/api/comfort-schedule/apply-learned", { method: "POST" });
      if (!res.ok) throw new Error("Failed to apply");
      const data = await res.json();
      setSchedule(data);
      setMessage({ text: "Applied learned schedule", type: "success" });
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : "Failed", type: "error" });
    }
  };

  const getLearnedScore = (dayType: DayType, hour: number): number => {
    if (!learned) return 0;
    return learned[dayType]?.[hour.toString()] ?? 0;
  };

  return (
    <div className="plan-section">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
        <h2 className="chart-title">Comfort Schedule</h2>
        {learned && (
          <button className="btn" onClick={applyLearned} style={{ fontSize: "0.75rem" }}>
            Apply Learned Schedule
          </button>
        )}
      </div>
      <p style={{ color: "var(--text-muted)", fontSize: "0.813rem", marginBottom: "1rem" }}>
        Click or drag to mark hours when you want comfort mode. The system learns from actual heating
        usage and adapts — blue dots below show learned activity.
      </p>

      {message && (
        <div
          style={{
            padding: "0.5rem 1rem",
            marginBottom: "1rem",
            borderRadius: "0.375rem",
            fontSize: "0.813rem",
            background: message.type === "success" ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
            color: message.type === "success" ? "var(--success)" : "var(--danger)",
            border: `1px solid ${message.type === "success" ? "var(--success)" : "var(--danger)"}`,
          }}
        >
          {message.text}
        </div>
      )}

      <div
        style={{ userSelect: "none", overflowX: "auto" }}
        onMouseLeave={() => setDragging(null)}
      >
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: "0.75rem",
            tableLayout: "fixed",
          }}
        >
          <thead>
            <tr>
              <th style={{ width: "80px", textAlign: "left", padding: "0.25rem", color: "var(--text-muted)" }} />
              {HOURS.map((h) => (
                <th
                  key={h}
                  style={{
                    padding: "0.25rem 0",
                    textAlign: "center",
                    color: "var(--text-muted)",
                    fontWeight: 400,
                  }}
                >
                  {formatHour(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {DAY_TYPES.map((dayType) => (
              <tr key={dayType}>
                <td
                  style={{
                    padding: "0.5rem 0.25rem",
                    fontWeight: 500,
                    textTransform: "capitalize",
                    fontSize: "0.813rem",
                  }}
                >
                  {dayType}
                </td>
                {HOURS.map((h) => {
                  const active = schedule[dayType].includes(h);
                  const learnedScore = getLearnedScore(dayType, h);
                  return (
                    <td
                      key={h}
                      onMouseDown={() => handleMouseDown(dayType, h)}
                      onMouseEnter={() => handleCellEnter(dayType, h)}
                      style={{
                        padding: "2px",
                        textAlign: "center",
                        cursor: "pointer",
                      }}
                    >
                      <div
                        style={{
                          position: "relative",
                          height: "32px",
                          borderRadius: "4px",
                          background: active ? "var(--accent)" : "var(--card-bg)",
                          border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
                          opacity: active ? 1 : 0.6,
                          transition: "background 0.1s, border-color 0.1s",
                        }}
                      >
                        {/* Learned usage indicator dot */}
                        {learnedScore > 0 && (
                          <div
                            style={{
                              position: "absolute",
                              bottom: "2px",
                              left: "50%",
                              transform: "translateX(-50%)",
                              width: `${Math.min(6 + learnedScore * 4, 14)}px`,
                              height: `${Math.min(6 + learnedScore * 4, 14)}px`,
                              borderRadius: "50%",
                              background: active
                                ? "rgba(255,255,255,0.4)"
                                : `rgba(59,130,246,${Math.min(0.3 + learnedScore * 0.15, 0.9)})`,
                            }}
                            title={`Learned activity: ${(learnedScore * 100).toFixed(0)}%`}
                          />
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginTop: "1rem" }}>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save Schedule"}
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", fontSize: "0.75rem", color: "var(--text-muted)" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}>
            <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 3, background: "var(--accent)" }} />
            Comfort
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}>
            <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 3, background: "var(--card-bg)", border: "1px solid var(--border)" }} />
            Eco
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}>
            <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "rgba(59,130,246,0.6)" }} />
            Learned usage
          </span>
        </div>
      </div>
    </div>
  );
}

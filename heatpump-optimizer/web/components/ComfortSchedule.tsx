"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useTimeFormat, formatHourLabel } from "./useTimeFormat";

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

function normalizeHours(hours: unknown): number[] {
  if (!Array.isArray(hours)) return [];
  return [...new Set(hours.filter((hour): hour is number => Number.isInteger(hour) && hour >= 0 && hour < 24))]
    .sort((first, second) => first - second);
}

function normalizeSchedule(value: unknown): Schedule {
  const schedule = value && typeof value === "object" ? value as Partial<Schedule> : {};
  return {
    weekday: normalizeHours(schedule.weekday),
    weekend: normalizeHours(schedule.weekend),
  };
}

export function ComfortSchedule() {
  const [schedule, setSchedule] = useState<Schedule>({ weekday: [], weekend: [] });
  const [learned, setLearned] = useState<LearnedSchedule | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const [dragging, setDragging] = useState<{ dayType: DayType; adding: boolean } | null>(null);
  const [activeCell, setActiveCell] = useState({ dayIndex: 0, hour: 0 });
  const cellRefs = useRef(new Map<string, HTMLDivElement>());
  const timeFormat = useTimeFormat();

  const fetchSchedule = useCallback(async () => {
    try {
      const [schedRes, learnedRes] = await Promise.all([
        fetch("/api/comfort-schedule"),
        fetch("/api/comfort-schedule/learned"),
      ]);
      if (schedRes.ok) setSchedule(normalizeSchedule(await schedRes.json()));
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
    setActiveCell({ dayIndex: DAY_TYPES.indexOf(dayType), hour });
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
      const normalizedSchedule = normalizeSchedule(schedule);
      setSchedule(normalizedSchedule);
      const res = await fetch("/api/comfort-schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(normalizedSchedule),
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
      setSchedule(normalizeSchedule(data));
      setMessage({ text: "Applied learned schedule", type: "success" });
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : "Failed", type: "error" });
    }
  };

  const getLearnedScore = (dayType: DayType, hour: number): number => {
    if (!learned) return 0;
    return learned[dayType]?.[hour.toString()] ?? 0;
  };

  const moveFocus = (dayIndex: number, hour: number) => {
    const next = { dayIndex: Math.max(0, Math.min(DAY_TYPES.length - 1, dayIndex)), hour: Math.max(0, Math.min(23, hour)) };
    setActiveCell(next);
    cellRefs.current.get(`${DAY_TYPES[next.dayIndex]}-${next.hour}`)?.focus();
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
          role={message.type === "error" ? "alert" : "status"}
          aria-live={message.type === "success" ? "polite" : undefined}
        >
          {message.text}
        </div>
      )}

      <div
        className="comfort-schedule-scroller"
        onMouseLeave={() => setDragging(null)}
      >
        <table className="comfort-schedule-table">
          <colgroup>
            <col className="comfort-schedule-label-column" />
            {HOURS.map((hour) => <col key={hour} className="comfort-schedule-hour-column" />)}
          </colgroup>
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
                  {formatHourLabel(h, timeFormat.hour12)}
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
                      }}
                    >
                      <div
                        role="checkbox"
                        aria-checked={active}
                        aria-label={`${dayType} ${formatHourLabel(h, timeFormat.hour12)}: ${active ? "comfort selected" : "eco selected"}`}
                        tabIndex={activeCell.dayIndex === DAY_TYPES.indexOf(dayType) && activeCell.hour === h ? 0 : -1}
                        ref={(element) => {
                          const key = `${dayType}-${h}`;
                          if (element) cellRefs.current.set(key, element);
                          else cellRefs.current.delete(key);
                        }}
                        onFocus={() => setActiveCell({ dayIndex: DAY_TYPES.indexOf(dayType), hour: h })}
                        onKeyDown={(e) => {
                          if (e.key === " " || e.key === "Enter") {
                            e.preventDefault();
                            toggleHour(dayType, h);
                            return;
                          }
                          if (e.key === "ArrowRight") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType), h + 1); }
                          if (e.key === "ArrowLeft") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType), h - 1); }
                          if (e.key === "ArrowDown") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType) + 1, h); }
                          if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType) - 1, h); }
                          if (e.key === "Home") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType), 0); }
                          if (e.key === "End") { e.preventDefault(); moveFocus(DAY_TYPES.indexOf(dayType), 23); }
                        }}
                        style={{
                          position: "relative",
                          minWidth: "44px",
                          height: "44px",
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

"use client";

import { useEffect, useLayoutEffect, useState, useCallback, useRef } from "react";
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

interface ScheduleConflict {
  saved: Schedule;
  revision: string;
  view: ContractView | null;
}

interface ContractView {
  timezone: string;
  target: string;
  minimum: string;
  maximum: string;
}

interface TargetPreview {
  schedule: Array<{
    comfort_hour: boolean;
    target: number;
    local_start: string;
    local_end: string;
    interval_start_utc: string;
    interval_end_utc: string;
  }>;
  revision: number;
  timezone: string;
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

function hasScheduleMask(value: unknown): value is Schedule {
  if (!value || typeof value !== "object") return false;
  const mask = value as Partial<Schedule>;
  return [mask.weekday, mask.weekend].every((hours) =>
    Array.isArray(hours) && hours.every((hour) => typeof hour === "number" && Number.isFinite(hour)));
}

async function readContract(response: Response) {
  const contract = await response.json().catch(() => null);
  const revision = response.headers.get("X-Contract-Revision")
    ?? response.headers.get("ETag")?.replaceAll('"', "")
    ?? (contract?.revision_id != null ? String(contract.revision_id) : null);
  // Never pair metadata from one revision with a different response revision.
  const complete = revision && /^\d+$/.test(revision) && contract
    && (contract.revision_id == null || String(contract.revision_id) === revision)
    && hasScheduleMask(contract.comfort_schedule)
    && [...contract.comfort_schedule.weekday, ...contract.comfort_schedule.weekend]
      .every((hour) => Number.isInteger(hour) && hour >= 0 && hour < 24)
    && typeof contract.timezone === "string" && contract.timezone.length > 0
    && [contract.comfort_temp_target, contract.comfort_temp_min, contract.comfort_temp_max]
      .every((value) => typeof value === "number" && Number.isFinite(value));
  const view: ContractView | null = complete ? {
    timezone: contract.timezone,
    target: String(contract.comfort_temp_target),
    minimum: String(contract.comfort_temp_min),
    maximum: String(contract.comfort_temp_max),
  } : null;
  return { saved: normalizeSchedule(contract?.comfort_schedule), revision: view ? revision : null, view };
}

export function ComfortSchedule({ contractChange = 0 }: { contractChange?: number }) {
  const [schedule, setSchedule] = useState<Schedule>({ weekday: [], weekend: [] });
  const [learned, setLearned] = useState<LearnedSchedule | null>(null);
  const [contractView, setContractView] = useState<ContractView | null>(null);
  const [installationId, setInstallationId] = useState<string | null>(null);
  const [revision, setRevision] = useState<string | null>(null);
  const [targetPreview, setTargetPreview] = useState<TargetPreview | null>(null);
  const [conflict, setConflict] = useState<ScheduleConflict | null>(null);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(true);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const [dragging, setDragging] = useState<{ dayType: DayType; adding: boolean } | null>(null);
  const [activeCell, setActiveCell] = useState({ dayIndex: 0, hour: 0 });
  const cellRefs = useRef(new Map<string, HTMLDivElement>());
  const previewGeneration = useRef(0);
  const contractGeneration = useRef(0);
  const draftLoaded = useRef(false);
  const mutationPending = useRef(false);
  const refreshQueued = useRef(false);
  const learnedOutcomeUnknown = useRef(false);
  const learnedAcknowledgement = useRef<{ saved: Schedule; revision: string | null } | null>(null);
  const timeFormat = useTimeFormat();
  const writesUnavailable = saving || refreshing || !revision || !contractView || !!conflict;

  const invalidatePreview = useCallback(() => {
    previewGeneration.current += 1;
    setTargetPreview(null);
  }, []);

  useEffect(() => {
    invalidatePreview();
    return () => { previewGeneration.current += 1; };
  }, [schedule, revision, installationId, invalidatePreview]);

  const fetchSchedule = useCallback(async (generation: number) => {
    try {
      const [settingsRes, learnedRes] = await Promise.all([
        fetch("/api/settings"),
        fetch("/api/comfort-schedule/learned"),
      ]);
      if (generation !== contractGeneration.current) return;
      if (learnedRes.ok) {
        const suggestion = await learnedRes.json();
        if (generation !== contractGeneration.current) return;
        setLearned(suggestion);
      }
      if (settingsRes.ok) {
        const id = settingsRes.headers.get("X-Installation-ID");
        if (!id) throw new Error("The active installation is unavailable");
        const contractRes = await fetch(`/api/installations/${encodeURIComponent(id)}/planning-settings`);
        if (!contractRes.ok) throw new Error("Failed to load planning settings");
        const contract = await readContract(contractRes);
        if (generation !== contractGeneration.current) return;
        if (!contract.revision || !contract.view) throw new Error("Planning settings are incomplete");
        setInstallationId(id);
        if (!draftLoaded.current) {
          setSchedule(contract.saved);
          draftLoaded.current = true;
        }
        // A lost POST response is not proof of rejection. Require an explicit
        // comparison before another write can overwrite a possibly committed result.
        const acknowledged = learnedAcknowledgement.current;
        const acknowledgementChanged = acknowledged && (
          (acknowledged.revision !== null && contract.revision !== acknowledged.revision)
          || JSON.stringify(contract.saved) !== JSON.stringify(acknowledged.saved));
        if (learnedOutcomeUnknown.current || acknowledgementChanged) {
          setConflict({ ...contract, revision: contract.revision });
          setRevision(acknowledged?.revision ?? null);
          setContractView(acknowledged ? null : contract.view);
          if (acknowledgementChanged) {
            setMessage({ text: "Saved settings changed elsewhere. Your acknowledged schedule is kept; compare before continuing.", type: "error" });
          }
        } else {
          setRevision(contract.revision);
          setContractView(contract.view);
        }
      } else {
        throw new Error("Failed to load active installation settings");
      }
    } catch (error) {
      if (generation !== contractGeneration.current) return;
      const failure = error instanceof Error ? error.message : "Planning settings are unavailable";
      setMessage({ text: learnedOutcomeUnknown.current
        ? `${failure}. Learned update outcome is unknown. Reload planning settings and compare the saved schedule before continuing.`
        : failure, type: "error" });
    } finally {
      if (generation === contractGeneration.current) setRefreshing(false);
    }
  }, []);

  const reloadContract = useCallback(() => {
    invalidatePreview();
    setRevision(learnedAcknowledgement.current?.revision ?? null);
    setContractView(null);
    setConflict(null);
    setRefreshing(true);
    // Parent acceptance must withdraw stale context, but must not obsolete a
    // child mutation response. Drain this refresh only after that mutation settles.
    if (mutationPending.current) {
      refreshQueued.current = true;
      return;
    }
    refreshQueued.current = false;
    const generation = ++contractGeneration.current;
    void fetchSchedule(generation);
  }, [fetchSchedule, invalidatePreview]);

  // Before paint, withdraw the old context without remounting or replacing the draft.
  useLayoutEffect(() => {
    reloadContract();
  }, [contractChange, reloadContract]);

  useLayoutEffect(() => {
    return () => {
      contractGeneration.current += 1;
      previewGeneration.current += 1;
    };
  }, []);

  const toggleHour = (dayType: DayType, hour: number) => {
    if (mutationPending.current) return;
    draftLoaded.current = true;
    invalidatePreview();
    setSchedule((prev) => {
      const hours = prev[dayType];
      const next = hours.includes(hour)
        ? hours.filter((h) => h !== hour)
        : [...hours, hour].sort((a, b) => a - b);
      return { ...prev, [dayType]: next };
    });
  };

  const handleCellEnter = (dayType: DayType, hour: number) => {
    if (mutationPending.current || !dragging || dragging.dayType !== dayType) return;
    draftLoaded.current = true;
    invalidatePreview();
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
    if (mutationPending.current) return;
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

  const handleSave = async (revisionOverride = revision) => {
    if ((!contractView && revisionOverride !== conflict?.revision) || (conflict && revisionOverride !== conflict.revision) || saving || refreshing || mutationPending.current || !revisionOverride
      || (revisionOverride !== revision && revisionOverride !== conflict?.revision)) return;
    invalidatePreview();
    if (!installationId) {
      setMessage({ text: "The active installation is unavailable", type: "error" });
      return;
    }
    setSaving(true);
    mutationPending.current = true;
    const generation = contractGeneration.current;
    setMessage(null);
    try {
      const normalizedSchedule = normalizeSchedule(schedule);
      setSchedule(normalizedSchedule);
      const path = `/api/installations/${encodeURIComponent(installationId)}/planning-settings`;
      const res = await fetch(path, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "If-Match": revisionOverride,
        },
        body: JSON.stringify({ comfort_schedule: normalizedSchedule }),
      });
      if (generation !== contractGeneration.current) return;
      if (res.status === 409) {
        setConflict(null);
        const latest = await fetch(path);
        if (latest.ok) {
          const latestContract = await readContract(latest);
          if (generation !== contractGeneration.current) return;
          if (latestContract.revision) {
            setConflict({ ...latestContract, revision: latestContract.revision });
          }
        }
        setMessage({ text: "Settings changed elsewhere. Your edits are kept; compare before choosing how to continue.", type: "error" });
        return;
      }
      if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
      let accepted = await readContract(res);
      if (generation !== contractGeneration.current) return;
      invalidatePreview();
      setRevision(accepted.revision);
      setContractView(accepted.view);
      setTargetPreview(null);
      setConflict(null);
      if (!accepted.view) {
        // Some writers may omit the contract body. Refresh without touching the draft.
        const refreshed = await fetch(path).catch(() => null);
        if (refreshed?.ok) {
          accepted = await readContract(refreshed);
          if (generation !== contractGeneration.current) return;
          invalidatePreview();
          setRevision(accepted.revision);
          setContractView(accepted.view);
        }
      }
      learnedAcknowledgement.current = null;
      setMessage({ text: "Schedule saved", type: "success" });
    } catch (e) {
      if (generation !== contractGeneration.current) return;
      setMessage({ text: e instanceof Error ? e.message : "Save failed", type: "error" });
    } finally {
      mutationPending.current = false;
      setSaving(false);
      if (refreshQueued.current) reloadContract();
    }
  };

  const applyLearned = async () => {
    if (writesUnavailable || !installationId || mutationPending.current) return;
    mutationPending.current = true;
    setSaving(true);
    const generation = contractGeneration.current;
    invalidatePreview();
    try {
      const res = await fetch("/api/comfort-schedule/apply-learned", {
        method: "POST",
        headers: { "If-Match": revision },
      });
      if (generation !== contractGeneration.current) return;
      if (res.status === 409) {
        setRevision(null);
        setContractView(null);
        setMessage({ text: "Settings changed elsewhere. Reload before applying this learned suggestion.", type: "error" });
        return;
      }
      if (!res.ok) throw new Error("Failed to apply");
      setRevision(null);
      setContractView(null);
      const data = await res.json();
      if (generation !== contractGeneration.current) return;
      // Legacy responses contain the two masks, not a full contract. Validate
      // their presence/types before normalization can turn missing fields into [].
      if (!hasScheduleMask(data)) throw new Error("Learned schedule response is incomplete");
      const bodyRevision = "revision_id" in data ? data.revision_id : null;
      const responseRevision = res.headers.get("X-Contract-Revision")
        ?? res.headers.get("ETag")?.replaceAll('"', "")
        ?? (bodyRevision != null ? String(bodyRevision) : null);
      if (responseRevision !== null && (!/^\d+$/.test(responseRevision)
        || (bodyRevision != null && String(bodyRevision) !== responseRevision))) {
        throw new Error("Learned schedule response revision is invalid");
      }
      invalidatePreview();
      const acknowledgedSchedule = normalizeSchedule(data);
      learnedAcknowledgement.current = responseRevision === null
        ? null : { saved: acknowledgedSchedule, revision: responseRevision };
      setSchedule(acknowledgedSchedule);
      setRevision(responseRevision);
      setConflict(null);
      if (installationId) {
        const contractRes = await fetch(`/api/installations/${encodeURIComponent(installationId)}/planning-settings`).catch(() => null);
        if (contractRes?.ok) {
          const contract = await readContract(contractRes);
          if (generation !== contractGeneration.current) return;
          invalidatePreview();
          // A GET is not the acknowledgement of this POST. Never silently
          // pair its competing revision/metadata with the acknowledged masks.
          if (contract.revision && ((responseRevision !== null && contract.revision !== responseRevision)
            || JSON.stringify(contract.saved) !== JSON.stringify(acknowledgedSchedule))) {
            learnedOutcomeUnknown.current = true; // Require explicit comparison on reload too.
            if (contract.revision) setConflict({ ...contract, revision: contract.revision });
            setMessage({ text: `Learned schedule acknowledged at revision ${responseRevision ?? "unknown"}, but saved settings changed elsewhere. Your acknowledged schedule is kept; compare before continuing.`, type: "error" });
            return;
          }
          // Legacy mask-only acknowledgements may recover metadata only when
          // the GET confirms those exact masks; versioned acknowledgements stay pinned.
          setRevision(responseRevision ?? contract.revision);
          setContractView(contract.view);
        }
      }
      setConflict(null);
      setMessage({ text: "Applied learned schedule", type: "success" });
    } catch (e) {
      if (generation !== contractGeneration.current) return;
      learnedOutcomeUnknown.current = true;
      setRevision(null);
      setContractView(null);
      setMessage({ text: `${e instanceof Error ? e.message : "Failed"}. Learned update outcome is unknown. Reload planning settings and compare the saved schedule before continuing.`, type: "error" });
    } finally {
      mutationPending.current = false;
      setSaving(false);
      if (refreshQueued.current) reloadContract();
    }
  };

  const previewSchedule = async () => {
    invalidatePreview();
    const generation = previewGeneration.current;
    if (!installationId || !revision || !contractView) {
      setMessage({ text: "Load the active installation before previewing", type: "error" });
      return;
    }
    const start = new Date();
    start.setUTCMinutes(0, 0, 0);
    start.setUTCHours(start.getUTCHours() + 1);
    const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
    try {
      const response = await fetch(
        `/api/installations/${encodeURIComponent(installationId)}/planning-settings/preview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "If-Match": revision },
          body: JSON.stringify({
            comfort_schedule: normalizeSchedule(schedule),
            horizon_start: start.toISOString(),
            horizon_end: end.toISOString(),
          }),
        },
      );
      if (generation !== previewGeneration.current) return;
      if (response.status === 409) {
        setMessage({ text: "Settings changed elsewhere. Reload before previewing this draft.", type: "error" });
        return;
      }
      if (!response.ok) throw new Error("Failed to preview planning settings");
      const preview = await response.json();
      if (generation !== previewGeneration.current) return;
      setTargetPreview({
        schedule: preview.target_schedule,
        revision: preview.base_revision_id,
        timezone: preview.proposed_contract.timezone,
      });
      setMessage(null);
    } catch (error) {
      if (generation !== previewGeneration.current) return;
      setMessage({ text: error instanceof Error ? error.message : "Preview failed", type: "error" });
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
          // Keep keyboard focus during the refresh; the handler also enforces this gate.
          <button className="btn" onClick={applyLearned} aria-disabled={writesUnavailable}
            style={{ fontSize: "0.75rem", opacity: writesUnavailable ? 0.5 : undefined }}>
            Apply Learned Schedule
          </button>
        )}
      </div>
      <p style={{ color: "var(--text-muted)", fontSize: "0.813rem", marginBottom: "1rem" }}>
        Click or drag to set your preferred comfort hours. Learned heating activity is only a suggestion;
        it never changes this schedule unless you explicitly choose “Apply Learned Schedule.”
        {saving && " Schedule editing is paused until this save finishes."}
      </p>
      {contractView ? (
        <p style={{ color: "var(--text-muted)", fontSize: "0.813rem", marginBottom: "1rem" }}>
          Planner timezone: {contractView.timezone}. Scheduled target: {contractView.target} °C;
          outside-schedule minimum: {contractView.minimum} °C; outer maximum: {contractView.maximum} °C.
        </p>
      ) : (
        <p>Planning target and timezone metadata unavailable. Reload planning settings before relying on these values.</p>
      )}
      {(!revision || !contractView) && (
        <button className="btn" onClick={reloadContract} disabled={saving || refreshing}>
          Reload planning settings
        </button>
      )}
      <button className="btn" onClick={() => void previewSchedule()} disabled={!installationId || !revision || !contractView}>
        Preview next 24 hours
      </button>
      {targetPreview && (
        <div role="status" style={{ color: "var(--text-muted)", fontSize: "0.813rem", marginTop: "0.5rem" }}>
          <p>
            Preview from revision {targetPreview.revision} in {targetPreview.timezone};{" "}
            {targetPreview.schedule.reduce((hours, interval) => hours + (interval.comfort_hour
              ? (Date.parse(interval.interval_end_utc) - Date.parse(interval.interval_start_utc)) / 3_600_000
              : 0), 0)} scheduled hours.
          </p>
          <ul>
            {targetPreview.schedule.map((interval) => (
              <li key={interval.local_start}>
                {interval.local_start}–{interval.local_end}: {interval.target} °C
                {interval.comfort_hour ? " (comfort)" : " (setback)"}
              </li>
            ))}
          </ul>
        </div>
      )}

      {conflict && (
        <div role="alert" style={{ padding: "0.75rem", marginBottom: "1rem", border: "1px solid var(--warning)", borderRadius: "0.375rem" }}>
          <p>Saved schedule: weekday {conflict.saved.weekday.join(", ") || "none"}; weekend {conflict.saved.weekend.join(", ") || "none"}.</p>
          <p>Your unsaved schedule: weekday {schedule.weekday.join(", ") || "none"}; weekend {schedule.weekend.join(", ") || "none"}.</p>
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
            <button className="btn" disabled={saving || refreshing || !conflict.view} onClick={() => {
              learnedOutcomeUnknown.current = false;
              learnedAcknowledgement.current = null;
              invalidatePreview();
              setSchedule(conflict.saved);
              setRevision(conflict.revision);
              setContractView(conflict.view);
              setTargetPreview(null);
              setConflict(null);
              setMessage(null);
            }}>
              Load saved schedule
            </button>
            <button className="btn" disabled={saving || refreshing || !conflict.view} onClick={() => {
              learnedOutcomeUnknown.current = false;
              learnedAcknowledgement.current = null;
              invalidatePreview();
              setRevision(conflict.revision);
              setContractView(conflict.view);
              setTargetPreview(null);
              void handleSave(conflict.revision);
            }}>
              Save my schedule over this revision
            </button>
          </div>
        </div>
      )}

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
                        aria-disabled={saving}
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
        <button className="btn btn-primary" onClick={() => void handleSave()} disabled={writesUnavailable}>
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

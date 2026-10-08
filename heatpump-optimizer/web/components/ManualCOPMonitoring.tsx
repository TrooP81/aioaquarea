"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ChevronLeft, ChevronRight, Pencil, RefreshCw, Save, Trash2, X } from "lucide-react";

type Granularity = "day" | "month";

interface Observation {
    device_id: string;
    granularity: Granularity;
    period_date: string;
    timezone: string;
    period_start: string;
    period_end: string;
    value: number;
    observed_at: string;
    completeness: "partial" | "closed" | "unknown";
    created_at: string;
    updated_at: string;
    revision: string;
    source: "controller_estimate";
    acquisition: "manual_entry";
    scope: "total";
    measured: false;
    accuracy: "approximate";
    energy_boundary: "unknown";
}

interface Monitoring {
    status: "disabled" | "no_data" | "insufficient_data" | "stale" | "ready" | "unavailable";
    reason: string;
    issue_at: string;
    target_date: string | null;
    timezone: string | null;
    fingerprint: string;
    counts: { available_closed_days: number; required_closed_days: number; fold_training_rows: number[] };
    forecast: {
        value: number;
        lower: number;
        upper: number;
        log_daily_trend: number;
        daily_trend_pct: number;
        band_label: string;
    } | null;
}

interface ManualResponse {
    devices: string[];
    default_timezone: string;
    device_id: string | null;
    granularity: Granularity;
    observations: Observation[];
    next_before: string | null;
    forecasting_enabled: boolean;
    monitoring: Monitoring;
}

const statusLabels: Record<Monitoring["status"], string> = {
    disabled: "Forecasting off",
    no_data: "No daily readings",
    insufficient_data: "Insufficient daily evidence",
    stale: "Daily evidence is stale",
    ready: "Experimental forecast",
    unavailable: "Forecast unavailable",
};

function utcInputValue() {
    return new Date().toISOString().slice(0, 19);
}

function calendarDate(timezone: string, granularity: Granularity) {
    try {
        const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
            timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
        }).formatToParts(new Date()).map((part) => [part.type, part.value]));
        return granularity === "month" ? `${parts.year}-${parts.month}` : `${parts.year}-${parts.month}-${parts.day}`;
    } catch {
        return "";
    }
}

export function ManualCOPMonitoring() {
    const [device, setDevice] = useState("");
    const [granularity, setGranularity] = useState<Granularity>("day");
    const [cursors, setCursors] = useState<Record<string, string | null>>({});
    const [data, setData] = useState<ManualResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [forecastInvalidated, setForecastInvalidated] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const [refreshNonce, setRefreshNonce] = useState(0);
    const [editing, setEditing] = useState<Observation | null>(null);
    const [period, setPeriod] = useState(() => calendarDate("UTC", "day"));
    const [value, setValue] = useState("");
    const [observedAt, setObservedAt] = useState(utcInputValue);
    const [completeness, setCompleteness] = useState<Observation["completeness"]>("partial");
    const [timezone, setTimezone] = useState("UTC");
    const [timezoneConfirmed, setTimezoneConfirmed] = useState(false);
    const [pendingEnabled, setPendingEnabled] = useState<boolean | null>(null);
    const generation = useRef(0);
    const request = useRef<AbortController | null>(null);
    const timezoneInitialized = useRef(false);
    const seriesKey = `${device}:${granularity}`;
    const cursor = cursors[seriesKey] ?? null;

    useEffect(() => {
        const current = ++generation.current;
        const controller = new AbortController();
        request.current = controller;
        async function load() {
            setLoading(true);
            setForecastInvalidated(true);
            setError(null);
            try {
                const query = new URLSearchParams();
                if (device) query.set("device_id", device);
                query.set("granularity", granularity);
                if (cursor) query.set("before", cursor);
                const response = await fetch(`/api/cop/manual?${query}`, { signal: controller.signal, cache: "no-store" });
                if (!response.ok) throw new Error(`Manual readings unavailable (${response.status})`);
                const body: ManualResponse = await response.json();
                if (!Array.isArray(body.observations) || body.observations.some((row) => typeof row.revision !== "string")) {
                    throw new Error("Manual readings response is invalid");
                }
                if (current !== generation.current || controller.signal.aborted) return;
                setData(body);
                setForecastInvalidated(false);
                setPendingEnabled(null);
                if (!timezoneInitialized.current) {
                    timezoneInitialized.current = true;
                    setTimezone(body.default_timezone);
                    setPeriod(calendarDate(body.default_timezone, granularity));
                }
            } catch (failure) {
                if (current !== generation.current || controller.signal.aborted) return;
                setError(failure instanceof Error ? failure.message : "Manual readings unavailable");
            } finally {
                if (current === generation.current) setLoading(false);
            }
        }
        void load();
        return () => {
            controller.abort();
            generation.current += 1;
        };
    }, [device, granularity, cursor, refreshNonce]);

    function resetEditor(nextGranularity = granularity) {
        setEditing(null);
        setValue("");
        setCompleteness("partial");
        setObservedAt(utcInputValue());
        setPeriod(calendarDate(timezone, nextGranularity));
        setTimezoneConfirmed(false);
    }

    function invalidateSnapshot() {
        request.current?.abort();
        setForecastInvalidated(true);
        setLoading(true);
        generation.current += 1;
    }

    function beginMutation() {
        invalidateSnapshot();
        setLoading(false);
        setBusy(true);
        setError(null);
        setNotice(null);
        return generation.current;
    }

    async function mutate(row: Observation | null, method: "PUT" | "DELETE", payload: unknown) {
        const current = beginMutation();
        const periodDate = row?.period_date ?? (granularity === "month" ? `${period}-01` : period);
        const path = `/api/cop/manual/${encodeURIComponent(device)}/${granularity}/${periodDate}`;
        try {
            const response = await fetch(path, {
                method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
            });
            if (current !== generation.current) return;
            if (response.status === 409) {
                setNotice("Reading changed. Current readings refreshed.");
                resetEditor();
                setRefreshNonce((previous) => previous + 1);
                return;
            }
            if (!response.ok) throw new Error(`Reading ${method === "DELETE" ? "deletion" : "save"} failed (${response.status})`);
            if (method === "PUT") {
                const saved: Observation = await response.json();
                if (typeof saved.revision !== "string") throw new Error("Saved revision is invalid");
                if (current !== generation.current) return;
            }
            resetEditor();
            setCursors((previous) => ({ ...previous, [seriesKey]: null }));
            setRefreshNonce((previous) => previous + 1);
        } catch (failure) {
            if (current === generation.current) setError(failure instanceof Error ? failure.message : "Reading mutation failed");
        } finally {
            if (current === generation.current) setBusy(false);
        }
    }

    async function save(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        const numericValue = Number(value);
        if (!device || !timezoneConfirmed || !Number.isFinite(numericValue) || numericValue <= 0) {
            setError("A device, confirmed timezone and positive finite value are required");
            return;
        }
        await mutate(editing, "PUT", {
            value: numericValue,
            observed_at: new Date(`${observedAt}Z`).toISOString(),
            completeness,
            timezone,
            expected_revision: editing?.revision ?? "0",
        });
    }

    async function toggleForecast(enabled: boolean) {
        const current = beginMutation();
        setPendingEnabled(enabled);
        try {
            const response = await fetch("/api/settings", {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ settings: { manual_cop_monitoring_enabled: enabled ? "true" : "false" } }),
            });
            if (current !== generation.current) return;
            if (!response.ok) throw new Error(`Forecast setting failed (${response.status})`);
            setRefreshNonce((previous) => previous + 1);
        } catch (failure) {
            if (current === generation.current) {
                setPendingEnabled(null);
                setError(failure instanceof Error ? failure.message : "Forecast setting failed");
            }
        } finally {
            if (current === generation.current) setBusy(false);
        }
    }

    function edit(row: Observation) {
        timezoneInitialized.current = true;
        setEditing(row);
        setPeriod(granularity === "month" ? row.period_date.slice(0, 7) : row.period_date);
        setValue(String(row.value));
        setObservedAt(new Date(row.observed_at).toISOString().slice(0, 19));
        setCompleteness(row.completeness);
        setTimezone(row.timezone);
        setTimezoneConfirmed(true);
        setError(null);
        setNotice(null);
    }

    const enabled = pendingEnabled ?? data?.forecasting_enabled ?? false;
    const monitoring = !forecastInvalidated && !loading && !busy && !error && enabled
        && data?.device_id === (device || null) && data.granularity === granularity ? data.monitoring : null;
    const forecast = monitoring?.status === "ready" ? monitoring.forecast : null;

    return (
        <section className="manual-cop-monitoring" aria-labelledby="manual-cop-heading" aria-busy={loading || busy}>
            <h3 id="manual-cop-heading">Manual controller COP</h3>
            <div className="manual-provenance text-muted">
                <span>Controller estimate</span><span>Total</span><span>Approximate</span><span>Manual entry</span>
                <span>Measured: no</span><span>Energy boundary: unknown</span>
            </div>
            <div className="manual-toolbar">
                <label className="manual-field">Device
                    <select value={device} disabled={busy} onChange={(event) => { invalidateSnapshot(); setDevice(event.target.value); resetEditor(); }}>
                        <option value="">Select device</option>
                        {(data?.devices ?? []).map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                    </select>
                </label>
                <div className="manual-segments" role="group" aria-label="Period series">
                    {(["day", "month"] as const).map((series) => <button type="button" className="btn btn-sm" key={series}
                        aria-pressed={granularity === series} disabled={busy}
                        onClick={() => { if (series !== granularity) { invalidateSnapshot(); setGranularity(series); resetEditor(series); } }}>{series === "day" ? "Day" : "Month"}</button>)}
                </div>
                <label className="manual-toggle">
                    <input type="checkbox" checked={enabled} disabled={busy || !data} onChange={(event) => void toggleForecast(event.target.checked)} />
                    Experimental forecasting
                </label>
                <button type="button" className="btn btn-sm manual-icon" aria-label="Refresh readings" title="Refresh readings"
                    disabled={busy || loading} onClick={() => { invalidateSnapshot(); setRefreshNonce((previous) => previous + 1); }}><RefreshCw size={16} /></button>
            </div>
            {error && <p className="text-danger" role="alert">{error}</p>}
            {notice && <p className="text-warning" role="status">{notice}</p>}
            <div className="manual-forecast" data-testid="manual-forecast">
                <h4>{enabled ? (monitoring ? statusLabels[monitoring.status] : "Forecast unavailable") : "Forecasting off"}</h4>
                {forecast && <dl className="manual-metrics">
                    <div><dt>Tomorrow ({monitoring?.target_date})</dt><dd data-testid="manual-projection">{forecast.value.toFixed(2)}</dd></div>
                    <div><dt>Daily log trend</dt><dd>{forecast.log_daily_trend.toFixed(4)}</dd></div>
                    <div><dt>Daily trend</dt><dd>{forecast.daily_trend_pct.toFixed(2)}%</dd></div>
                    <div><dt>Experimental empirical log-error band, uncalibrated</dt><dd>{forecast.lower.toFixed(2)} - {forecast.upper.toFixed(2)}</dd></div>
                </dl>}
                {monitoring && <div className="text-muted manual-evidence">
                    <span>Closed days: {monitoring.counts.available_closed_days}/{monitoring.counts.required_closed_days}</span>
                    <span>Fold rows: {monitoring.counts.fold_training_rows.join(", ") || "none"}</span>
                    <span>{monitoring.reason.replaceAll("_", " ")}</span>
                    <time dateTime={monitoring.issue_at}>Issued (UTC): {new Date(monitoring.issue_at).toISOString()}</time>
                </div>}
            </div>
            <form onSubmit={(event) => void save(event)} className="manual-form">
                <label className="manual-field">{granularity === "day" ? "Period date" : "Period month"}
                    <input type={granularity === "day" ? "date" : "month"} value={period} required disabled={busy || Boolean(editing)} onChange={(event) => setPeriod(event.target.value)} />
                </label>
                <label className="manual-field">COP value
                    <input type="number" step="any" min="0" value={value} required disabled={busy} onChange={(event) => setValue(event.target.value)} />
                </label>
                <label className="manual-field">Observed at (UTC)
                    <input type="datetime-local" step="1" value={observedAt} required disabled={busy} onChange={(event) => setObservedAt(event.target.value)} />
                </label>
                <label className="manual-field">Completeness
                    <select value={completeness} disabled={busy} onChange={(event) => setCompleteness(event.target.value as Observation["completeness"])}>
                        <option value="partial">Partial</option><option value="closed">Closed (user-attested)</option><option value="unknown">Unknown</option>
                    </select>
                </label>
                <label className="manual-field">Controller timezone
                    <input value={timezone} required disabled={busy} onChange={(event) => { timezoneInitialized.current = true; setTimezone(event.target.value); setTimezoneConfirmed(false); }} />
                </label>
                <label className="manual-toggle manual-confirm">
                    <input type="checkbox" checked={timezoneConfirmed} required disabled={busy} onChange={(event) => setTimezoneConfirmed(event.target.checked)} />
                    Controller timezone confirmed
                </label>
                <div className="manual-actions">
                    <button type="submit" className="btn btn-sm" disabled={busy || !device}><Save size={16} />{editing ? "Save correction" : "Save reading"}</button>
                    {editing && <button type="button" className="btn btn-sm manual-icon" aria-label="Cancel correction" title="Cancel correction" disabled={busy} onClick={() => resetEditor()}><X size={16} /></button>}
                </div>
            </form>
            <div className="manual-readings" aria-label={`${granularity === "day" ? "Daily" : "Monthly"} manual readings`}>
                {(data?.device_id === device && data.granularity === granularity ? data.observations : []).map((row) => (
                    <div className="manual-reading" key={`${row.device_id}:${row.granularity}:${row.period_date}`}>
                        <div className="manual-reading-period"><strong>{row.period_date}</strong><span>{row.completeness} / {row.timezone}</span><time dateTime={row.observed_at}>Observed: {new Date(row.observed_at).toISOString()}</time></div>
                        <span className="manual-value">{row.value}</span>
                        <div className="manual-row-actions">
                            <button type="button" className="btn btn-sm manual-icon" aria-label={`Edit reading ${row.period_date}`} title="Edit reading" disabled={busy || loading} onClick={() => edit(row)}><Pencil size={16} /></button>
                            <button type="button" className="btn btn-sm manual-icon" aria-label={`Delete reading ${row.period_date}`} title="Delete reading" disabled={busy || loading}
                                onClick={() => { if (window.confirm("Delete this manual reading?")) void mutate(row, "DELETE", { expected_revision: row.revision }); }}><Trash2 size={16} /></button>
                        </div>
                    </div>
                ))}
                {!loading && !error && device && data?.observations.length === 0 && <p className="text-muted">No readings.</p>}
            </div>
            <div className="manual-pagination">
                <button type="button" className="btn btn-sm manual-icon" aria-label="Newest readings" title="Newest readings" disabled={busy || loading || !cursor} onClick={() => { invalidateSnapshot(); setCursors((previous) => ({ ...previous, [seriesKey]: null })); }}><ChevronLeft size={16} /></button>
                <button type="button" className="btn btn-sm" disabled={busy || loading || !data?.next_before} onClick={() => { invalidateSnapshot(); setCursors((previous) => ({ ...previous, [seriesKey]: data?.next_before ?? null })); }}>Older readings<ChevronRight size={16} /></button>
            </div>
            <style jsx>{`
        .manual-cop-monitoring { min-width: 0; }
        .manual-provenance, .manual-evidence { display: flex; flex-wrap: wrap; gap: .35rem 1rem; font-size: .8rem; overflow-wrap: anywhere; }
        .manual-toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: .75rem; margin: .75rem 0; }
        .manual-toolbar .manual-field { flex: 1 1 12rem; }
        .manual-field { display: flex; flex-direction: column; gap: .3rem; min-width: 0; font-size: .8rem; }
        .manual-field input, .manual-field select { box-sizing: border-box; min-width: 0; width: 100%; max-width: 100%; min-height: 2.5rem; font: inherit; color: inherit; background: var(--card-bg); border: 1px solid var(--border); border-radius: 4px; padding: .4rem; }
        .manual-toggle { display: flex; align-items: center; gap: .45rem; min-height: 2.5rem; font-size: .8rem; }
        .manual-toggle input { flex: 0 0 auto; }
        .manual-segments { display: flex; flex: 0 0 auto; }
        .manual-segments [aria-pressed="true"] { border-color: var(--accent); background: var(--bg); }
        .manual-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr)); gap: .75rem; padding: .75rem 0; border-top: 1px solid var(--border); }
        .manual-actions, .manual-row-actions, .manual-pagination { display: flex; align-items: center; gap: .4rem; }
        .manual-actions { grid-column: 1 / -1; }
        .manual-icon { flex: 0 0 2.5rem; width: 2.5rem; height: 2.5rem; display: inline-flex; align-items: center; justify-content: center; padding: 0; }
        .manual-forecast { min-height: 3rem; padding: .5rem 0; }
        .manual-forecast h4 { font-size: .9rem; margin: 0 0 .5rem; }
        .manual-metrics { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; margin: 0 0 .5rem; }
        .manual-metrics div { min-width: 0; max-width: 100%; }
        .manual-metrics dt { font-size: .8rem; overflow-wrap: anywhere; }
        .manual-metrics dd { margin: .2rem 0; font-variant-numeric: tabular-nums; }
        .manual-reading { display: grid; grid-template-columns: minmax(0, 1fr) 4rem auto; align-items: center; gap: .5rem; padding: .65rem 0; border-top: 1px solid var(--border); }
        .manual-reading-period { min-width: 0; display: flex; flex-direction: column; gap: .2rem; overflow-wrap: anywhere; font-size: .8rem; }
        .manual-value { font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }
        .manual-pagination { justify-content: end; margin-top: .5rem; }
      `}</style>
        </section>
    );
}
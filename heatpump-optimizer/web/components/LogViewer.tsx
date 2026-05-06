"use client";

import { useEffect, useState, useRef, useCallback } from "react";

interface LogEntry {
  ts: string;
  level: string;
  logger: string | null;
  event: string;
  details: Record<string, unknown> | null;
  service: string;
}

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "var(--text-muted)",
  INFO: "var(--accent)",
  WARNING: "var(--warning)",
  ERROR: "var(--danger)",
  CRITICAL: "var(--danger)",
};

const SERVICE_COLORS: Record<string, string> = {
  optimizer: "#a78bfa",
  poller: "#34d399",
  ml: "#fbbf24",
  api: "#60a5fa",
};

export function LogViewer() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [minutes, setMinutes] = useState(30);
  const [levelFilter, setLevelFilter] = useState<string>("all");
  const [serviceFilter, setServiceFilter] = useState<string>("all");
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());
  const [searchText, setSearchText] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ minutes: minutes.toString() });
      if (levelFilter !== "all") params.set("level", levelFilter);
      if (serviceFilter !== "all") params.set("service", serviceFilter);
      const res = await fetch(`/api/logs?${params}`);
      if (res.ok) {
        const data: LogEntry[] = await res.json();
        setLogs(data);
      }
    } catch {
      // silently ignore fetch errors
    } finally {
      setLoading(false);
    }
  }, [minutes, levelFilter, serviceFilter]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(fetchLogs, 10_000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchLogs]);

  const toggleRow = (idx: number) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const formatTime = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  const filtered = searchText
    ? logs.filter(
        (l) =>
          l.event.toLowerCase().includes(searchText.toLowerCase()) ||
          (l.details && JSON.stringify(l.details).toLowerCase().includes(searchText.toLowerCase()))
      )
    : logs;

  return (
    <div className="plan-section">
      <h2 className="chart-title">Application Logs</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Live log stream from optimizer, poller, and ML services
      </p>

      {/* Controls */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.75rem",
          alignItems: "center",
          marginBottom: "1rem",
        }}
      >
        <select
          value={minutes}
          onChange={(e) => setMinutes(Number(e.target.value))}
          className="form-select"
          style={{ width: "auto", minWidth: "120px" }}
        >
          <option value={5}>Last 5 min</option>
          <option value={15}>Last 15 min</option>
          <option value={30}>Last 30 min</option>
          <option value={60}>Last 1 hour</option>
          <option value={360}>Last 6 hours</option>
          <option value={1440}>Last 24 hours</option>
        </select>

        <select
          value={levelFilter}
          onChange={(e) => setLevelFilter(e.target.value)}
          className="form-select"
          style={{ width: "auto", minWidth: "100px" }}
        >
          <option value="all">All Levels</option>
          <option value="DEBUG">Debug</option>
          <option value="INFO">Info</option>
          <option value="WARNING">Warning</option>
          <option value="ERROR">Error</option>
        </select>

        <select
          value={serviceFilter}
          onChange={(e) => setServiceFilter(e.target.value)}
          className="form-select"
          style={{ width: "auto", minWidth: "110px" }}
        >
          <option value="all">All Services</option>
          <option value="optimizer">Optimizer</option>
          <option value="poller">Poller</option>
          <option value="ml">ML</option>
        </select>

        <input
          type="text"
          placeholder="Search logs..."
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          className="form-input"
          style={{ width: "auto", minWidth: "160px", flex: "1" }}
        />

        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.85rem", color: "var(--text-muted)", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Auto-refresh
        </label>

        <button className="btn" onClick={fetchLogs} disabled={loading} style={{ padding: "0.4rem 0.75rem" }}>
          {loading ? "⟳" : "Refresh"}
        </button>
      </div>

      {/* Log count */}
      <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginBottom: "0.5rem" }}>
        {filtered.length} log entries
        {filtered.length !== logs.length && ` (${logs.length} total)`}
      </div>

      {/* Log entries */}
      <div
        ref={containerRef}
        style={{
          maxHeight: "500px",
          overflowY: "auto",
          background: "rgba(0,0,0,0.3)",
          borderRadius: "0.5rem",
          border: "1px solid var(--border)",
          fontFamily: "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
          fontSize: "0.8rem",
          lineHeight: "1.6",
        }}
      >
        {filtered.length === 0 ? (
          <div style={{ padding: "2rem", textAlign: "center", color: "var(--text-muted)" }}>
            {loading ? "Loading logs..." : "No log entries found"}
          </div>
        ) : (
          filtered.map((log, idx) => {
            const isExpanded = expandedRows.has(idx);
            const hasDetails = log.details && Object.keys(log.details).length > 0;
            return (
              <div
                key={idx}
                onClick={() => hasDetails && toggleRow(idx)}
                style={{
                  padding: "0.3rem 0.75rem",
                  borderBottom: "1px solid rgba(51,65,85,0.5)",
                  cursor: hasDetails ? "pointer" : "default",
                  background: isExpanded ? "rgba(59,130,246,0.05)" : "transparent",
                }}
              >
                <div style={{ display: "flex", gap: "0.75rem", alignItems: "baseline", flexWrap: "wrap" }}>
                  <span style={{ color: "var(--text-muted)", flexShrink: 0 }}>
                    {formatTime(log.ts)}
                  </span>
                  <span
                    style={{
                      color: LEVEL_COLORS[log.level] || "var(--text)",
                      fontWeight: log.level === "ERROR" || log.level === "WARNING" ? 600 : 400,
                      width: "3.5rem",
                      flexShrink: 0,
                    }}
                  >
                    {log.level}
                  </span>
                  <span
                    style={{
                      color: SERVICE_COLORS[log.service] || "var(--text-muted)",
                      fontSize: "0.75rem",
                      width: "5rem",
                      flexShrink: 0,
                    }}
                  >
                    [{log.service}]
                  </span>
                  <span style={{ color: "var(--text)", flex: 1 }}>
                    {log.event}
                    {hasDetails && !isExpanded && (
                      <span style={{ color: "var(--text-muted)", marginLeft: "0.5rem" }}>▸</span>
                    )}
                  </span>
                </div>

                {isExpanded && hasDetails && (
                  <pre
                    style={{
                      margin: "0.3rem 0 0.2rem 10rem",
                      padding: "0.4rem 0.6rem",
                      background: "rgba(0,0,0,0.3)",
                      borderRadius: "0.25rem",
                      color: "var(--text-muted)",
                      fontSize: "0.75rem",
                      overflowX: "auto",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-all",
                    }}
                  >
                    {JSON.stringify(log.details, null, 2)}
                  </pre>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

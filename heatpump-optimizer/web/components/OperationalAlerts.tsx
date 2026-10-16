"use client";

import { useEffect, useState } from "react";
import { Banner } from "./Banner";
import { useRefresh } from "./RefreshContext";
import type { BackupHealth } from "../lib/backup-health";

interface OperationalAlert {
  id: string;
  severity: "critical" | "warning";
  title: string;
  detail: string;
  action?: string | null;
  plan_id?: number | null;
  action_id?: number | null;
  href?: string | null;
}

interface OperationalAlertData {
  enabled: boolean;
  alerts: OperationalAlert[];
  backup?: BackupHealth | null;
}

function eventTime(value: string | null | undefined) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Unavailable";
  return new Date(value).toLocaleString();
}

function BackupSummary({ backup }: { backup?: BackupHealth | null }) {
  const complete = Boolean(
    backup?.last_success && backup.last_attempt?.started_at && backup.last_verified
    && backup.restore_qualified && backup.status === "healthy"
  );
  return (
    <div aria-label="Database backup health" className="text-sm">
      <strong>Database backup: {backup?.ready && complete ? "Healthy" : backup ? "Needs attention" : "Unavailable"}</strong>
      <dl>
        <dt>Successful backup</dt><dd>{eventTime(backup?.last_success)}</dd>
        <dt>Latest attempt</dt><dd>{eventTime(backup?.last_attempt?.started_at)} ({backup?.last_attempt?.status ?? "unknown"})</dd>
        <dt>Verified restore</dt><dd>{eventTime(backup?.last_verified)}</dd>
        <dt>Next backup due</dt><dd>{eventTime(backup?.next_due_at)}</dd>
      </dl>
      {backup?.reason_codes?.length ? <p>{backup.reason_codes.join(", ")}</p> : null}
    </div>
  );
}

/** Compact, auto-refreshing operational health summary for the Overview tab. */
export function OperationalAlerts() {
  const { refreshEpoch } = useRefresh();
  const [data, setData] = useState<OperationalAlertData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch("/api/operations/alerts", { signal: controller.signal });
        if (!response.ok) throw new Error(`Operational alerts returned ${response.status}`);
        setData(await response.json());
      } catch {
        if (!controller.signal.aborted) setError("Operational alerts could not be loaded.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => {
      controller.abort();
    };
  }, [refreshEpoch, retry]);

  if (loading) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><p className="text-muted text-sm">Loading operational alerts...</p></section>;
  if (error) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><Banner tone="warning"><p>{error}</p><button className="btn btn-sm" onClick={() => setRetry((value) => value + 1)}>Retry</button></Banner></section>;
  if (!data) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><p>Operational health unavailable.</p></section>;
  return (
    <section className="plan-section" aria-live="polite" aria-label="Operational alerts">
      <h2 className="chart-title">Operational health</h2>
      <BackupSummary backup={data.backup} />
      {!data.enabled ? (
        <p className="text-muted text-sm">Operational alert delivery is disabled. Backup health is shown independently.</p>
      ) : data.alerts.length === 0 ? (
        <p className="text-muted text-sm">No active operational alerts. This alone does not establish backup health.</p>
      ) : (
        <div className="plan-history-summary">
          {data.alerts.map((alert) => (
            <div key={alert.id} className={alert.severity === "critical" ? "text-danger text-sm" : "text-warning text-sm"}>
              <strong>{alert.title}</strong>
              <span>: {alert.detail}</span>
              {alert.action && <span> {alert.action}</span>}
              {alert.href && (
                <a className="btn btn-sm operational-alert-link" href={alert.href}>
                  Open exact event
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

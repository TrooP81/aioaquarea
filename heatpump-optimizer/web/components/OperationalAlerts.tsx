"use client";

import { useEffect, useState } from "react";
import { Banner } from "./Banner";

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
}

/** Compact, auto-refreshing operational health summary for the Overview tab. */
export function OperationalAlerts() {
  const [data, setData] = useState<OperationalAlertData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = () => {
      setLoading(true);
      setError(null);
      fetch("/api/operations/alerts")
        .then((response) => {
          if (!response.ok) throw new Error(`Operational alerts returned ${response.status}`);
          return response.json();
        })
        .then((value) => {
          if (alive) setData(value);
        })
        .catch(() => {
          if (alive) setError("Operational alerts could not be loaded.");
        })
        .finally(() => { if (alive) setLoading(false); });
    };
    load();
    const interval = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, [retry]);

  if (loading) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><p className="text-muted text-sm">Loading operational alerts...</p></section>;
  if (error) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><Banner tone="warning"><p>{error}</p><button className="btn btn-sm" onClick={() => setRetry((value) => value + 1)}>Retry</button></Banner></section>;
  if (!data || !data.enabled) return <section className="plan-section"><h2 className="chart-title">Operational health</h2><p className="text-muted text-sm">Operational alerts are not enabled.</p></section>;
  return (
    <section className="plan-section" aria-live="polite" aria-label="Operational alerts">
      <h2 className="chart-title">Operational health</h2>
      {data.alerts.length === 0 ? (
        <p className="text-muted text-sm">No active operational warnings. Data, planning, and recent plan actions look healthy.</p>
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

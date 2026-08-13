"use client";

import { useEffect, useState } from "react";

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

  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch("/api/operations/alerts")
        .then((response) => (response.ok ? response.json() : null))
        .then((value) => {
          if (alive) setData(value);
        })
        .catch(() => {
          if (alive) setData(null);
        });
    };
    load();
    const interval = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, []);

  if (!data || !data.enabled) return null;
  return (
    <section className="plan-section" aria-live="polite" aria-label="Operational alerts">
      <h2 className="chart-title">Operational health</h2>
      {data.alerts.length === 0 ? (
        <p className="text-muted text-sm">No active operational warnings. Data, planning, and recent plan actions look healthy.</p>
      ) : (
        <div className="plan-history-summary">
          {data.alerts.map((alert) => (
            <div key={alert.id} className={alert.severity === "critical" ? "text-danger text-sm" : "text-warning text-sm"}>
              <strong>{alert.severity === "critical" ? "●" : "▲"} {alert.title}</strong>
              <span> — {alert.detail}</span>
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

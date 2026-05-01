"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatCost } from "./useCurrency";

interface PlanProps {
  plan: {
    id: number;
    optimizer_version: string;
    cost_estimate_eur: number | null;
    actions_count: number;
  } | null;
}

interface PlanAction {
  id: number;
  scheduled_ts: string;
  action_type: string;
  payload: Record<string, any>;
  status: string;
  executed_at: string | null;
}

export function PlanView({ plan }: PlanProps) {
  const [actions, setActions] = useState<PlanAction[]>([]);
  const currency = useCurrency();

  useEffect(() => {
    if (plan?.id) {
      fetch(`/api/plans/${plan.id}`)
        .then((r) => r.json())
        .then((data) => setActions(data.actions || []))
        .catch(() => {});
    }
  }, [plan?.id]);

  const formatAction = (type: string) => {
    const labels: Record<string, string> = {
      force_dhw_on: "🔥 DHW Heating ON",
      force_dhw_off: "⏹ DHW Heating OFF",
      quiet_mode_on: "🤫 Quiet Mode ON",
      quiet_mode_off: "🔊 Quiet Mode OFF",
      zone_temp_boost: "⬆️ Zone Temp +2°C",
      zone_temp_restore: "↩️ Zone Temp Restore",
      set_tank_temp: "🌡️ Set Tank Temp",
    };
    return labels[type] || type;
  };

  return (
    <div className="plan-section">
      <div className="plan-header">
        <div>
          <h2 className="chart-title">Active Plan</h2>
          {plan ? (
            <p style={{ color: "var(--text-muted)", fontSize: "0.875rem" }}>
              {plan.optimizer_version} • Est. cost: {formatCost(plan.cost_estimate_eur, currency)} • {plan.actions_count} actions
            </p>
          ) : (
            <p style={{ color: "var(--text-muted)", fontSize: "0.875rem" }}>
              No active plan. Optimizer will generate one when price data is available.
            </p>
          )}
        </div>
      </div>

      {actions.length > 0 && (
        <div className="plan-actions">
          {actions.slice(0, 10).map((action) => (
            <div key={action.id} className="plan-action">
              <span className="plan-action-time">
                {new Date(action.scheduled_ts).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                })}
              </span>
              <span className="plan-action-type">
                {formatAction(action.action_type)}
              </span>
              <span className={`plan-action-status ${action.status}`}>
                {action.status}
              </span>
            </div>
          ))}
          {actions.length > 10 && (
            <p style={{ color: "var(--text-muted)", fontSize: "0.75rem", paddingTop: "0.5rem" }}>
              + {actions.length - 10} more actions
            </p>
          )}
        </div>
      )}
    </div>
  );
}

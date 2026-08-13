"use client";

import { useEffect, useState } from "react";
import { formatCost, useCurrency } from "./useCurrency";

interface OutcomeSummaryData {
  days: number;
  cost?: {
    measured_kwh?: number;
    actual_cost?: number | null;
    coverage_pct?: number;
    estimated_price_shift_savings?: number | null;
  };
  comfort?: {
    samples?: number;
    within_range_pct?: number | null;
    average_c?: number | null;
  };
  baseline_method?: string;
  weather_matched_comparison?: {
    status?: string;
    candidate_windows?: number;
    matched_average_energy_kwh?: number;
    energy_delta_vs_matched_kwh?: number;
    note?: string;
  };
  experiment?: {
    enabled?: boolean;
    status?: string;
    maximum_curve_step_c?: number;
    conditions?: {
      ready?: boolean;
      reason?: string;
      outdoor_temp_c?: number | null;
      heating_off_outdoor_c?: number | null;
    };
  };
}

/** A truthful, recent outcome view rather than a claim of per-command causality. */
export function OutcomeSummary() {
  const [data, setData] = useState<OutcomeSummaryData | null>(null);
  const [days, setDays] = useState(7);
  const currency = useCurrency();

  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/outcomes/summary?days=${days}`, { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) setData(null);
      });
    return () => controller.abort();
  }, [days]);

  return (
    <section className="plan-section outcome-summary" aria-label="Measured optimization outcome">
      <div className="settings-panel-header outcome-summary-header">
        <div className="outcome-summary-copy">
          <h2 className="chart-title">Measured outcome</h2>
          <p className="chart-caption">What the heat pump actually used and how indoor readings tracked the comfort band.</p>
        </div>
        <div className="training-controls outcome-summary-controls" aria-label="Outcome period">
          {[1, 7, 30].map((value) => (
            <button
              key={value}
              className="btn btn-sm"
              aria-pressed={days === value}
              onClick={() => setDays(value)}
            >
              {value === 1 ? "Today" : `${value} days`}
            </button>
          ))}
        </div>
      </div>
      {!data ? (
        <p className="text-muted text-sm">Outcome data is loading or not available yet.</p>
      ) : (
        <>
          <dl className="outcome-summary-metrics">
            <div className="outcome-summary-metric">
              <dt>Energy</dt>
              <dd>{data.cost?.measured_kwh?.toFixed(1) ?? "0.0"} kWh</dd>
            </div>
            <div className="outcome-summary-metric">
              <dt>Cost</dt>
              <dd>
                {data.cost?.actual_cost != null ? formatCost(data.cost.actual_cost, currency) : "awaiting complete price data"}
                {data.cost?.coverage_pct != null ? ` · ${data.cost.coverage_pct}% priced` : ""}
              </dd>
            </div>
            {data.comfort?.samples ? (
              <div className="outcome-summary-metric">
                <dt>Comfort</dt>
                <dd>
                  {data.comfort.within_range_pct ?? "—"}% in range
                  {data.comfort.average_c != null ? ` · avg ${data.comfort.average_c.toFixed(1)}°C` : ""}
                </dd>
              </div>
            ) : (
              <div className="outcome-summary-metric">
                <dt>Comfort</dt>
                <dd>Awaiting trusted indoor readings</dd>
              </div>
            )}
            {data.weather_matched_comparison?.status === "observational_comparison" && (
              <div className="outcome-summary-metric">
                <dt>Similar-weather energy</dt>
                <dd>{data.weather_matched_comparison.energy_delta_vs_matched_kwh != null
                  ? `${data.weather_matched_comparison.energy_delta_vs_matched_kwh >= 0 ? "−" : "+"}${Math.abs(data.weather_matched_comparison.energy_delta_vs_matched_kwh).toFixed(1)} kWh`
                  : "—"}
                  {data.weather_matched_comparison.candidate_windows ? ` vs ${data.weather_matched_comparison.candidate_windows} earlier windows` : ""}
                </dd>
              </div>
            )}
          </dl>
          {data.cost?.estimated_price_shift_savings != null && (
            <div className="outcome-summary-savings">
              Estimated price-shift saving {formatCost(data.cost.estimated_price_shift_savings, currency)}
            </div>
          )}
        </>
      )}
      <div className="outcome-summary-notes">
        {data?.baseline_method && <p className="chart-caption">{data.baseline_method}</p>}
        {data?.weather_matched_comparison?.note && (
          <p className="chart-caption">{data.weather_matched_comparison.note}</p>
        )}
        {data?.experiment && (
          <p className="chart-caption">
          {data.experiment.enabled ? (
            data.experiment.status === "waiting_for_heating_conditions" ? (
              <>Manual trial suggestions are on, but paused until safe heating conditions return{data.experiment.conditions?.outdoor_temp_c != null && data.experiment.conditions?.heating_off_outdoor_c != null ? ` (outside ${data.experiment.conditions.outdoor_temp_c.toFixed(1)}°C; heating-off threshold ${data.experiment.conditions.heating_off_outdoor_c.toFixed(1)}°C)` : ""}. Nothing is sent to the heat pump automatically.</>
            ) : (
              <>Manual trial suggestions: on — review-only, with a maximum {data.experiment.maximum_curve_step_c?.toFixed(1) ?? "—"}°C heat-curve step. Nothing is sent to the heat pump automatically.</>
            )
          ) : (
            <>
              Manual trial suggestions: off. <a className="chart-caption-link" href="/settings?tab=system#manual-trial-suggestions">Open the setting</a> to see optional, limited heat-curve trials.
            </>
          )}
          </p>
        )}
      </div>
    </section>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatPricePerKwh, formatCostInCurrency } from "./useCurrency";
import { DataAge } from "./DataAge";
import { Banner } from "./Banner";
import { LAYER_LABELS, LAYER_TOOLTIPS, outdoorFallbackReasonLabel, gateStateLabel } from "@/lib/constants";

interface DashboardProps {
  data: {
    current_status: {
      ts: string;
      mode: string | null;
      outdoor_temp: number | null;
      heat_pump_outdoor_temp?: number | null;
      weather_outdoor_temp?: number | null;
      outdoor_temp_source?: string | null;
      outdoor_temp_provider?: string | null;
      outdoor_temp_compensation_c?: number | null;
      outdoor_temp_fallback_reason?: string | null;
      tank_temp: number | null;
      tank_target_temp: number | null;
      zone1_temp: number | null;
      quiet_mode: number | null;
      operation_status?: number | null;
      device_action?: string | null;
      direction?: string | null;
      space_heating_active: boolean | null;
    } | null;
    current_status_fresh: boolean;
    current_status_age_seconds: number | null;
    current_price: number | null;
    today_kwh: number;
    today_cost_eur: number | null;
    today_cost_currency: string;
    today_cost_priced_kwh: number;
    today_cost_unpriced_kwh: number;
    today_cost_priced_amount: number;
    today_cost_coverage_pct: number;
    today_cost_complete: boolean;
    space_heating_gate?: {
      state?: string;
      reason?: string;
      profile_id?: string;
    };
  } | null;
}

interface OptimizerBrief {
  active_layer: string;
  cop_trained: boolean;
  demand_trained: boolean;
  thermal_calibrated: boolean;
  planningData: { control_allowed: boolean; reasons: string[] } | null;
}

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown";
  const diffMs = Date.now() - d.getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

/** Turn a raw heat-pump mode string into a readable label. */
function formatMode(mode: string | null | undefined): string {
  if (!mode) return "Unknown";
  return mode
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

type PumpStatus = NonNullable<NonNullable<DashboardProps["data"]>["current_status"]>;

function formatPumpState(status: PumpStatus | null | undefined): string {
  if (!status) return "Unknown";
  const action = status.device_action?.toUpperCase();
  const labels: Record<string, string> = {
    OFF: "Off",
    IDLE: "Standby",
    HEATING: "Room heating",
    COOLING: "Cooling",
    HEATING_WATER: "Heating hot water",
  };
  if (action && labels[action]) return labels[action];
  if (status.space_heating_active) return "Room heating";
  if (status.operation_status === 0) return "Standby";
  if (status.operation_status === 1) return "Running";
  return /^\d+$/.test(status.mode ?? "") ? "Status available" : formatMode(status.mode);
}

export function Dashboard({ data }: DashboardProps) {
  const currency = useCurrency();
  const status = data?.current_status;
  const statusFresh = status != null && data?.current_status_fresh !== false;
  const [optBrief, setOptBrief] = useState<OptimizerBrief | null>(null);
  const [optimizerLoading, setOptimizerLoading] = useState(true);
  const [optimizerError, setOptimizerError] = useState<string | null>(null);

  const loadOptimizerBrief = () => {
    setOptimizerLoading(true);
    setOptimizerError(null);
    fetch("/api/optimizer/status")
      .then((r) => {
        if (!r.ok) throw new Error(`Optimizer status returned ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (!d) return;
        setOptBrief({
          active_layer: d.active_layer,
          cop_trained: d.cop_model?.trained ?? false,
          demand_trained: d.demand_model?.trained ?? false,
          thermal_calibrated: d.thermal_model?.calibrated ?? false,
          planningData: d.planning_data_quality ?? null,
        });
      })
      .catch(() => setOptimizerError("Optimizer status could not be loaded."))
      .finally(() => setOptimizerLoading(false));
  };

  useEffect(() => { loadOptimizerBrief(); }, []);

  return (
    <>
      {currency.warning && <Banner tone="warning"><p>{currency.warning}</p></Banner>}
      <h3 className="card-group-label">Cost today</h3>
      <div className="grid">
        <div className="card">
          <div className="card-header">
            <span className="card-title">Today&apos;s Consumption</span>
          </div>
          <div className="card-value kwh">
            {data?.today_kwh?.toFixed(1) ?? "0"} kWh
          </div>
          <div className="card-subtitle">
            {data?.today_cost_complete
              ? `Cost: ${formatCostInCurrency(data.today_cost_eur, data.today_cost_currency, currency)}`
              : `Known cost: ${formatCostInCurrency(data?.today_cost_priced_amount, data?.today_cost_currency, currency)} · ${data?.today_cost_coverage_pct ?? 0}% priced`}
          </div>
          {!data?.today_cost_complete && (data?.today_cost_unpriced_kwh ?? 0) > 0 && (
            <div className="card-subtitle text-warning text-sm">
              {(data?.today_cost_unpriced_kwh ?? 0).toFixed(1)} kWh awaiting price data
            </div>
          )}
        </div>
      </div>
      <details className="heat-pump-details">
        <summary>Heat pump details</summary>
        {/* ── Live readings ── */}
        <h3 className="card-group-label">{statusFresh ? "Live readings" : "Latest readings"}</h3>
        {status && !statusFresh && (
          <p className="text-warning text-sm">
            Heat-pump readings are stale · last device sample {formatRelativeTime(status.ts)}.
          </p>
        )}
        <DataAge timestamp={status?.ts ?? null} stale={!statusFresh} />
        <div className="grid">
          <div className="card">
            <div className="card-header">
              <span className="card-title">Current Price</span>
            </div>
            <div className="card-value price">
              {formatPricePerKwh(data?.current_price, currency)}
            </div>
            <div className="card-subtitle">per kWh</div>
          </div>

          <div className="card">
            <div className="card-header">
              <span className="card-title">Outdoor Temperature</span>
            </div>
            <div className="card-value temp">
              {status?.outdoor_temp != null ? `${status.outdoor_temp.toFixed(1)}°C` : "—"}
            </div>
            <div className="card-subtitle">
              {status?.outdoor_temp_source === "weather"
                ? `Weather report · ${status.outdoor_temp_provider?.toUpperCase() ?? "provider"}`
                : status?.outdoor_temp_source === "heat_pump"
                  ? "Heat-pump sensor selected"
                  : status?.outdoor_temp_source === "heat_pump_fallback"
                    ? "Heat-pump fallback · weather unavailable"
                    : "Current effective value"}
            </div>
            {status?.heat_pump_outdoor_temp != null && status.outdoor_temp_source !== "heat_pump" && (
              <div className="card-subtitle text-sm">
                Pump sensor: {status.heat_pump_outdoor_temp.toFixed(1)}°C
                {status.outdoor_temp_compensation_c != null
                  ? ` · compensated ${status.outdoor_temp_compensation_c > 0 ? "+" : ""}${status.outdoor_temp_compensation_c.toFixed(1)}°C`
                  : ""}
              </div>
            )}
            {status?.outdoor_temp_fallback_reason && (
              <div className="card-subtitle text-warning text-sm">
                {outdoorFallbackReasonLabel(status.outdoor_temp_fallback_reason)}
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-header">
              <span className="card-title">Tank Temperature</span>
            </div>
            <div className="card-value temp">
              {status?.tank_temp != null ? `${status.tank_temp.toFixed(1)}°C` : "—"}
            </div>
            <div className="card-subtitle">
              {status?.tank_target_temp != null ? `Target: ${status.tank_target_temp}°C` : "Target: —"}
            </div>
          </div>

          <div className="card">
            <div className="card-header">
              <span className="card-title">Zone 1 Temperature</span>
            </div>
            <div className="card-value temp">
              {status?.zone1_temp != null ? `${status.zone1_temp.toFixed(1)}°C` : "—"}
            </div>
            <div className="card-subtitle">Heating zone</div>
          </div>

        </div>

        {/* ── System ── */}
        <h3 className="card-group-label">System</h3>
        <div className="grid">
          <div className="card">
            <div className="card-header">
              <span className="card-title">Heat Pump</span>
            </div>
            <div className="card-value" style={{ fontSize: "1.5rem" }}>
              {formatPumpState(status)}
            </div>
            <div className="card-subtitle">
              Quiet mode: {status?.quiet_mode === 1 ? "On" : "Off"}
            </div>
            <div className="card-subtitle">
              Space heating: {status?.space_heating_active ? "confirmed active" : "not active"}
            </div>
            <div className="card-subtitle" data-testid="space-heating-gate">
              Eligibility gate: {gateStateLabel(data?.space_heating_gate?.state)}
              {data?.space_heating_gate?.profile_id ? ` · ${data.space_heating_gate.profile_id}` : ""}
            </div>
            {optBrief?.planningData && !optBrief.planningData.control_allowed && (
              <div className="card-subtitle text-warning text-sm">
                New plans paused: {optBrief.planningData.reasons.join(" ")}
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-header">
              <span className="card-title">Optimizer</span>
            </div>
            <div className="card-value" style={{ fontSize: "1.25rem" }}>
              {optimizerLoading ? "Loading..." : optimizerError ? "Unavailable" : optBrief ? (
                <span
                  title={LAYER_TOOLTIPS[optBrief.active_layer] || optBrief.active_layer}
                  className={`opt-layer-badge ${optBrief.active_layer.includes("ml") ? "opt-layer-badge--ml" : optBrief.active_layer.includes("milp") ? "opt-layer-badge--milp" : ""}`}
                >
                  {LAYER_LABELS[optBrief.active_layer] || optBrief.active_layer}
                </span>
              ) : "—"}
            </div>
            <div className="card-subtitle">
              {optimizerLoading ? "Loading optimizer status..." : optimizerError ? (
                <button className="btn btn-sm" onClick={loadOptimizerBrief}>Retry</button>
              ) : optBrief ? (
                <span className="ml-status-dots">
                  <span className={`status-dot ${optBrief.cop_trained ? "status-dot--ok" : ""}`} aria-label={`COP efficiency model ${optBrief.cop_trained ? "ready" : "not ready"}`} title="COP (efficiency) model" />
                  <span className={`status-dot ${optBrief.demand_trained ? "status-dot--ok" : ""}`} aria-label={`Demand hot-water model ${optBrief.demand_trained ? "ready" : "not ready"}`} title="Demand (hot-water) model" />
                  <span className={`status-dot ${optBrief.thermal_calibrated ? "status-dot--ok" : ""}`} aria-label={`Thermal heat-up model ${optBrief.thermal_calibrated ? "ready" : "not ready"}`} title="Thermal (heat-up rate) model" />
                  <span className="ml-status-label">
                    {[optBrief.cop_trained, optBrief.demand_trained, optBrief.thermal_calibrated].filter(Boolean).length}/3 learning models ready
                  </span>
                </span>
              ) : "No optimizer status is available yet."}
            </div>
          </div>
        </div>
      </details>
    </>
  );
}

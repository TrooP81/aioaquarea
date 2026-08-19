"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatPricePerKwh, formatCostInCurrency } from "./useCurrency";
import { LAYER_LABELS, LAYER_TOOLTIPS } from "@/lib/constants";

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
  } | null;
  indoorTemp: number | null;
  indoorSensorCount: number;
  lastFreshReading: string | null;
  latestReading: string | null;
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

export function Dashboard({ data, indoorTemp, indoorSensorCount, lastFreshReading, latestReading }: DashboardProps) {
  const currency = useCurrency();
  const status = data?.current_status;
  const statusFresh = status != null && data?.current_status_fresh !== false;
  const [optBrief, setOptBrief] = useState<OptimizerBrief | null>(null);

  useEffect(() => {
    fetch("/api/optimizer/status")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          setOptBrief({
            active_layer: d.active_layer,
            cop_trained: d.cop_model?.trained ?? false,
            demand_trained: d.demand_model?.trained ?? false,
            thermal_calibrated: d.thermal_model?.calibrated ?? false,
            planningData: d.planning_data_quality ?? null,
          });
        }
      })
      .catch(() => {});
  }, []);

  return (
    <>
      {currency.warning && <p className="text-warning text-sm">⚠ {currency.warning}</p>}
      {/* ── Live readings ── */}
      <h3 className="card-group-label">{statusFresh ? "Live readings" : "Latest readings"}</h3>
      {status && !statusFresh && (
        <p className="text-warning text-sm">
          Heat-pump readings are stale · last device sample {formatRelativeTime(status.ts)}.
        </p>
      )}
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
              ⚠ {status.outdoor_temp_fallback_reason.replace(/_/g, " ")}
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
            <span className="card-title">Indoor Temperature</span>
          </div>
          <div className="card-value temp">
            {indoorTemp != null ? `${indoorTemp.toFixed(1)}°C` : "—"}
          </div>
          <div className="card-subtitle">
            {indoorSensorCount > 0
              ? `${indoorSensorCount} sensor${indoorSensorCount !== 1 ? "s" : ""}`
              : "No sensors connected"}
          </div>
          {lastFreshReading && lastFreshReading !== latestReading && (
            <div className="card-subtitle text-warning text-sm">
              ⚠ Stale — fresh data {formatRelativeTime(lastFreshReading)}
            </div>
          )}
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
          {optBrief?.planningData && !optBrief.planningData.control_allowed && (
            <div className="card-subtitle text-warning text-sm" title={optBrief.planningData.reasons.join(" ")}>
              ⚠ New plans paused until fresh price and weather data is available
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Optimizer</span>
          </div>
          <div className="card-value" style={{ fontSize: "1.25rem" }}>
            {optBrief ? (
              <span
                title={LAYER_TOOLTIPS[optBrief.active_layer] || optBrief.active_layer}
                className={`opt-layer-badge ${optBrief.active_layer.includes("ml") ? "opt-layer-badge--ml" : optBrief.active_layer.includes("milp") ? "opt-layer-badge--milp" : ""}`}
              >
                {LAYER_LABELS[optBrief.active_layer] || optBrief.active_layer}
              </span>
            ) : "—"}
          </div>
          <div className="card-subtitle">
            {optBrief ? (
              <span className="ml-status-dots">
                <span className={`status-dot ${optBrief.cop_trained ? "status-dot--ok" : ""}`} title="COP (efficiency) model" />
                <span className={`status-dot ${optBrief.demand_trained ? "status-dot--ok" : ""}`} title="Demand (hot-water) model" />
                <span className={`status-dot ${optBrief.thermal_calibrated ? "status-dot--ok" : ""}`} title="Thermal (heat-up rate) model" />
                <span className="ml-status-label">
                  {[optBrief.cop_trained, optBrief.demand_trained, optBrief.thermal_calibrated].filter(Boolean).length}/3 learning models ready
                </span>
              </span>
            ) : "Loading..."}
          </div>
        </div>
      </div>
    </>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useCurrency, formatPricePerKwh, formatCost } from "./useCurrency";
import { LAYER_LABELS, LAYER_TOOLTIPS } from "@/lib/constants";

interface DashboardProps {
  data: {
    current_status: {
      mode: string | null;
      outdoor_temp: number | null;
      tank_temp: number | null;
      tank_target_temp: number | null;
      zone1_temp: number | null;
      quiet_mode: number | null;
    } | null;
    current_price: number | null;
    today_kwh: number;
    today_cost_eur: number;
  } | null;
  indoorTemp: number | null;
  indoorSensorCount: number;
}

interface OptimizerBrief {
  active_layer: string;
  cop_trained: boolean;
  demand_trained: boolean;
  thermal_calibrated: boolean;
}

export function Dashboard({ data, indoorTemp, indoorSensorCount }: DashboardProps) {
  const currency = useCurrency();
  const status = data?.current_status;
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
          });
        }
      })
      .catch(() => {});
  }, []);

  return (
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
        <div className="card-subtitle">Current</div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Tank Temperature</span>
        </div>
        <div className="card-value temp">
          {status?.tank_temp != null ? `${status.tank_temp.toFixed(1)}°C` : "—"}
        </div>
        <div className="card-subtitle">
          Target: {status?.tank_target_temp ?? "—"}°C
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
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Zone 1 Temperature</span>
        </div>
        <div className="card-value temp">
          {status?.zone1_temp != null ? `${status.zone1_temp.toFixed(1)}°C` : "—"}
        </div>
        <div className="card-subtitle">
          Mode: {status?.mode ?? "—"} | Quiet: {status?.quiet_mode === 1 ? "ON" : "OFF"}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Today's Consumption</span>
        </div>
        <div className="card-value kwh">
          {data?.today_kwh?.toFixed(1) ?? "0"} kWh
        </div>
        <div className="card-subtitle">
          Cost: {formatCost(data?.today_cost_eur, currency)}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Operation</span>
        </div>
        <div className="card-value" style={{ fontSize: "1.5rem" }}>
          {status?.mode ?? "Unknown"}
        </div>
        <div className="card-subtitle">
          Status: {status ? "Running" : "Offline"}
        </div>
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
              <span className={`status-dot ${optBrief.cop_trained ? "status-dot--ok" : ""}`} title="COP model" />
              <span className={`status-dot ${optBrief.demand_trained ? "status-dot--ok" : ""}`} title="Demand model" />
              <span className={`status-dot ${optBrief.thermal_calibrated ? "status-dot--ok" : ""}`} title="Thermal model" />
              <span className="ml-status-label">
                {[optBrief.cop_trained, optBrief.demand_trained, optBrief.thermal_calibrated].filter(Boolean).length}/3 models ready
              </span>
            </span>
          ) : "Loading..."}
        </div>
      </div>
    </div>
  );
}

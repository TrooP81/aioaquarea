"use client";

import { useCurrency, formatPricePerKwh, formatCost } from "./useCurrency";

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
}

export function Dashboard({ data }: DashboardProps) {
  const currency = useCurrency();
  const status = data?.current_status;

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
    </div>
  );
}

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { useTimeFormat, formatTime } from "./useTimeFormat";

interface ConsumptionPoint {
  ts: string;
  heat_kwh: number | null;
  cool_kwh: number | null;
  tank_kwh: number | null;
  total_kwh: number | null;
  outdoor_temp: number | null;
}

export function ConsumptionChart() {
  const [data, setData] = useState<ConsumptionPoint[]>([]);
  const [showHotWater, setShowHotWater] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timeFormat = useTimeFormat();

  const load = useCallback(() => {
    setLoading(true);
    fetch("/api/consumption/history?hours=24")
      .then((r) => {
        if (!r.ok) throw new Error(`Consumption API returned ${r.status}`);
        return r.json();
      })
      .then((value) => {
        setData(value);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load consumption"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const chartData = data.map((d) => ({
    time: formatTime(new Date(d.ts), timeFormat.hour12),
    heat: d.heat_kwh || 0,
    cool: d.cool_kwh || 0,
    tank: d.tank_kwh || 0,
    total: d.total_kwh || 0,
  }));

  if (loading && chartData.length === 0) {
    return (
      <div className="chart-container" role="region" aria-label="Energy consumption chart">
        <div className="chart-title">Heating Energy — Past 24h</div>
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "55%" }} />
          <p className="text-muted text-center">Loading consumption…</p>
        </div>
      </div>
    );
  }

  if (error && chartData.length === 0) {
    return (
      <div className="chart-container" role="alert">
        <div className="chart-title">Heating Energy — Past 24h</div>
        <p className="text-danger">{error}</p>
        <button className="btn btn-sm" onClick={load}>Retry</button>
      </div>
    );
  }

  const hasSpaceEnergy = chartData.some((point) => point.heat > 0 || point.cool > 0);
  const hasHotWaterEnergy = chartData.some((point) => point.tank > 0);

  return (
    <div className="chart-container" role="region" aria-label="Energy consumption chart">
      <div className="chart-title-row">
        <div className="chart-title">Heating Energy — Past 24h</div>
        <button
          type="button"
          className="btn btn-sm"
          aria-pressed={showHotWater}
          onClick={() => setShowHotWater((current) => !current)}
        >
          {showHotWater ? "Hide hot water" : "Show hot water"}
        </button>
      </div>
      <div className="chart-caption">
        Space heating and cooling electricity over the last 24 hours.
        {showHotWater ? " Hot-water electricity is included." : " Hot-water electricity is hidden to keep the comfort view focused."}
      </div>
      {!hasSpaceEnergy && !showHotWater ? (
        <div className="energy-zero-state" role="status">
          <strong>No room-heating or cooling electricity in the last 24 hours.</strong>
          <span>{hasHotWaterEnergy ? "Hot-water energy is available but hidden." : "The chart will expand when controllable room energy appears."}</span>
        </div>
      ) : (
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} interval={3} />
          <YAxis stroke="#94a3b8" fontSize={11} unit=" kWh" />
          <Tooltip
            contentStyle={{
              background: "#1e293b",
              border: "1px solid #334155",
              borderRadius: "8px",
            }}
            formatter={(value: number, name: string) => [`${value.toFixed(2)} kWh`, name]}
          />
          <Legend />
          <Bar
            dataKey="heat"
            stackId="1"
            fill="#f59e0b"
            name="Heating"
            radius={[0, 0, 0, 0]}
          />
          {showHotWater && (
            <Bar
              dataKey="tank"
              stackId="1"
              fill="#3b82f6"
              name="Hot Water"
              radius={[0, 0, 0, 0]}
            />
          )}
          <Bar
            dataKey="cool"
            stackId="1"
            fill="#22c55e"
            name="Cooling"
            radius={[2, 2, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
      )}
    </div>
  );
}

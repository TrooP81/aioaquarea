"use client";

import { useEffect, useState } from "react";
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
  const timeFormat = useTimeFormat();

  useEffect(() => {
    fetch("/api/consumption/history?hours=24")
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, []);

  const chartData = data.map((d) => ({
    time: formatTime(new Date(d.ts), timeFormat.hour12),
    heat: d.heat_kwh || 0,
    cool: d.cool_kwh || 0,
    tank: d.tank_kwh || 0,
    total: d.total_kwh || 0,
  }));

  if (chartData.length === 0) {
    return (
      <div className="chart-container" role="region" aria-label="Energy consumption chart">
        <div className="chart-title">Energy Consumption — 24h</div>
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "55%" }} />
          <p className="text-muted text-center">No consumption data yet. Data will appear after device polling.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-container" role="region" aria-label="Energy consumption chart">
      <div className="chart-title">Energy Consumption — 24h</div>
      <div className="chart-caption">
        Electricity drawn by the heat pump over the last 24 hours.
      </div>
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
          <Bar
            dataKey="tank"
            stackId="1"
            fill="#3b82f6"
            name="Hot Water"
            radius={[0, 0, 0, 0]}
          />
          <Bar
            dataKey="cool"
            stackId="1"
            fill="#22c55e"
            name="Cooling"
            radius={[2, 2, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

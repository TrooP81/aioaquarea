"use client";

import { useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";

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

  useEffect(() => {
    fetch("/api/consumption/history?hours=24")
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, []);

  const chartData = data.map((d) => ({
    time: new Date(d.ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    heat: d.heat_kwh || 0,
    cool: d.cool_kwh || 0,
    tank: d.tank_kwh || 0,
    total: d.total_kwh || 0,
  }));

  if (chartData.length === 0) {
    return (
      <div className="chart-container">
        <div className="chart-title">Energy Consumption — 24h</div>
        <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "3rem" }}>
          No consumption data yet. Data will appear after device polling.
        </p>
      </div>
    );
  }

  return (
    <div className="chart-container">
      <div className="chart-title">Energy Consumption — 24h</div>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={chartData}>
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
          <Area
            type="monotone"
            dataKey="heat"
            stackId="1"
            stroke="#f59e0b"
            fill="rgba(245, 158, 11, 0.3)"
            name="Heating"
          />
          <Area
            type="monotone"
            dataKey="tank"
            stackId="1"
            stroke="#3b82f6"
            fill="rgba(59, 130, 246, 0.3)"
            name="Hot Water"
          />
          <Area
            type="monotone"
            dataKey="cool"
            stackId="1"
            stroke="#22c55e"
            fill="rgba(34, 197, 94, 0.3)"
            name="Cooling"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

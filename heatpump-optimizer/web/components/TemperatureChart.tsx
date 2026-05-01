"use client";

import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from "recharts";

interface StatusPoint {
  ts: string;
  tank_temp: number | null;
  tank_target_temp: number | null;
  zone1_temp: number | null;
  zone1_target_temp: number | null;
  outdoor_temp: number | null;
}

export function TemperatureChart() {
  const [data, setData] = useState<StatusPoint[]>([]);

  useEffect(() => {
    fetch("/api/status/history?hours=24")
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, []);

  const chartData = data.map((d) => ({
    time: new Date(d.ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    tank: d.tank_temp,
    tankTarget: d.tank_target_temp,
    zone1: d.zone1_temp,
    zone1Target: d.zone1_target_temp,
    outdoor: d.outdoor_temp,
  }));

  if (chartData.length === 0) {
    return (
      <div className="chart-container">
        <div className="chart-title">Temperature History — 24h</div>
        <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "3rem" }}>
          No temperature data yet. Data will appear after device polling.
        </p>
      </div>
    );
  }

  return (
    <div className="chart-container">
      <div className="chart-title">Temperature History — 24h</div>
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} interval={3} />
          <YAxis stroke="#94a3b8" fontSize={11} unit="°C" />
          <Tooltip
            contentStyle={{
              background: "#1e293b",
              border: "1px solid #334155",
              borderRadius: "8px",
            }}
            formatter={(value: number, name: string) => [`${value}°C`, name]}
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="tank"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={false}
            name="Tank"
          />
          <Line
            type="monotone"
            dataKey="tankTarget"
            stroke="#3b82f6"
            strokeWidth={1}
            strokeDasharray="5 5"
            dot={false}
            name="Tank Target"
          />
          <Line
            type="monotone"
            dataKey="zone1"
            stroke="#f59e0b"
            strokeWidth={2}
            dot={false}
            name="Zone 1"
          />
          <Line
            type="monotone"
            dataKey="zone1Target"
            stroke="#f59e0b"
            strokeWidth={1}
            strokeDasharray="5 5"
            dot={false}
            name="Zone 1 Target"
          />
          <Line
            type="monotone"
            dataKey="outdoor"
            stroke="#22c55e"
            strokeWidth={2}
            dot={false}
            name="Outdoor"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

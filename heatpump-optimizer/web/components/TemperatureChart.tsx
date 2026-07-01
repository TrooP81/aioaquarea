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
} from "recharts";
import { useTimeFormat, formatTime } from "./useTimeFormat";

interface StatusPoint {
  ts: string;
  tank_temp: number | null;
  tank_target_temp: number | null;
  zone1_temp: number | null;
  zone1_target_temp: number | null;
  outdoor_temp: number | null;
}

interface IndoorTempPoint {
  timestamp: string;
  temperature: number;
}

export function TemperatureChart() {
  const [data, setData] = useState<StatusPoint[]>([]);
  const [indoorData, setIndoorData] = useState<IndoorTempPoint[]>([]);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    fetch("/api/status/history?hours=24")
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});

    fetch("/api/indoor-temp?hours=24")
      .then((r) => r.json())
      .then(setIndoorData)
      .catch(() => {});
  }, []);

  // Merge indoor temp data into the chart by matching to nearest 5-minute bucket
  const indoorByBucket = new Map<string, number>();
  indoorData.forEach((p) => {
    const d = new Date(p.timestamp);
    // Round to nearest 5 minutes
    d.setMinutes(Math.round(d.getMinutes() / 5) * 5, 0, 0);
    const key = formatTime(d, timeFormat.hour12);
    indoorByBucket.set(key, p.temperature);
  });

  const chartData = data.map((d) => {
    const raw = new Date(d.ts);
    // Round status timestamps to same 5-minute bucket
    raw.setMinutes(Math.round(raw.getMinutes() / 5) * 5, 0, 0);
    const time = formatTime(raw, timeFormat.hour12);
    return {
      time,
      tank: d.tank_temp,
      tankTarget: d.tank_target_temp,
      zone1: d.zone1_temp,
      zone1Target: d.zone1_target_temp,
      outdoor: d.outdoor_temp,
      indoor: indoorByBucket.get(time) ?? null,
    };
  });

  if (chartData.length === 0) {
    return (
      <div className="chart-container" role="region" aria-label="Temperature history chart">
        <div className="chart-title">Temperature History — 24h</div>
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "60%" }} />
          <p className="text-muted text-center">No temperature data yet. Data will appear after device polling.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-container" role="region" aria-label="Temperature history chart">
      <div className="chart-title">Temperature History — 24h</div>
      <div className="chart-caption">
        Measured outdoor, tank and zone temperatures over the last 24 hours.
      </div>
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
          <Line
            type="monotone"
            dataKey="indoor"
            stroke="#ec4899"
            strokeWidth={2}
            dot={false}
            name="Indoor"
            connectNulls
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

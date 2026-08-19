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
  const fiveMinuteBucket = (timestamp: string) =>
    Math.round(new Date(timestamp).getTime() / 300_000) * 300_000;
  const indoorByBucket = new Map<number, number[]>();
  indoorData.forEach((p) => {
    const key = fiveMinuteBucket(p.timestamp);
    const values = indoorByBucket.get(key) ?? [];
    values.push(p.temperature);
    indoorByBucket.set(key, values);
  });

  const chartData = data.map((d) => {
    const timestamp = fiveMinuteBucket(d.ts);
    const indoorValues = indoorByBucket.get(timestamp) ?? [];
    return {
      timestamp,
      tank: d.tank_temp,
      tankTarget: d.tank_target_temp,
      zone1: d.zone1_temp,
      // Panasonic emits -5°C while the weather-compensated heat curve owns
      // the target. It is a sentinel, not a temperature line to chart.
      zone1Target: d.zone1_target_temp != null && d.zone1_target_temp >= 15 && d.zone1_target_temp <= 65
        ? d.zone1_target_temp
        : null,
      outdoor: d.outdoor_temp,
      indoor: indoorValues.length > 0
        ? indoorValues.reduce((total, value) => total + value, 0) / indoorValues.length
        : null,
    };
  });
  const formatTimestamp = (timestamp: number, includeDate = false) =>
    includeDate
      ? new Intl.DateTimeFormat(undefined, {
          weekday: "short",
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
          hour12: timeFormat.hour12,
        }).format(new Date(timestamp))
      : formatTime(new Date(timestamp), timeFormat.hour12);

  if (chartData.length === 0) {
    return (
      <div className="chart-container" role="region" aria-label="Temperature history chart">
        <div className="chart-title">Temperature History — Past 24h</div>
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
      <div className="chart-title">Temperature History — Past 24h</div>
      <div className="chart-caption">
        Measured outdoor, tank and zone temperatures over the last 24 hours.
      </div>
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis
            dataKey="timestamp"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            stroke="#94a3b8"
            fontSize={11}
            tickCount={9}
            tickFormatter={(timestamp) => formatTimestamp(Number(timestamp))}
          />
          <YAxis stroke="#94a3b8" fontSize={11} unit="°C" />
          <Tooltip
            labelFormatter={(timestamp) => formatTimestamp(Number(timestamp), true)}
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
            name="Zone 1 target (when explicit)"
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

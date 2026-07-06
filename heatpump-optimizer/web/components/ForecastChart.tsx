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
import { useTimeFormat } from "./useTimeFormat";

interface WeatherPoint {
  ts: string;
  temperature: number | null;
  wind_speed: number | null;
  humidity: number | null;
  cloud_cover: number | null;
  irradiance: number | null;
}

export function ForecastChart() {
  const [data, setData] = useState<WeatherPoint[]>([]);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    fetch("/api/weather?hours=48")
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, []);

  const now = new Date();

  const chartData = data.map((d) => {
    const ts = new Date(d.ts);
    return {
      time: ts.toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: timeFormat.hour12,
      }),
      temperature: d.temperature,
      windSpeed: d.wind_speed,
      humidity: d.humidity,
      cloudCover: d.cloud_cover != null ? Math.round(d.cloud_cover * 100) : null,
      isForecast: ts > now,
    };
  });

  if (chartData.length === 0) {
    return (
      <div className="chart-container" role="region" aria-label="Weather forecast chart">
        <div className="chart-title">Weather Forecast — 48h</div>
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "65%" }} />
          <p className="text-muted text-center">No weather data yet. Data will appear after weather fetch.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-container" role="region" aria-label="Weather forecast chart">
      <div className="chart-title">Weather Forecast — 48h</div>
      <div className="chart-caption">
        Outdoor temperature and conditions for the next 48 hours, used to plan heating ahead.
      </div>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} interval={5} />
          <YAxis
            yAxisId="temp"
            stroke="#94a3b8"
            fontSize={11}
            unit="°C"
          />
          <YAxis
            yAxisId="wind"
            orientation="right"
            stroke="#94a3b8"
            fontSize={11}
            unit=" m/s"
          />
          {/* Hidden 0–100% axis backing the cloud-cover series. */}
          <YAxis yAxisId="cloud" domain={[0, 100]} hide />
          <Tooltip
            contentStyle={{
              background: "#1e293b",
              border: "1px solid #334155",
              borderRadius: "8px",
            }}
            formatter={(value: number, name: string) => {
              if (name === "Temperature") return [`${value}°C`, name];
              if (name === "Wind Speed") return [`${value} m/s`, name];
              if (name === "Humidity") return [`${value}%`, name];
              if (name === "Cloud Cover") return [`${value}%`, name];
              return [value, name];
            }}
          />
          <Legend />
          <Line
            yAxisId="temp"
            type="monotone"
            dataKey="temperature"
            stroke="#f59e0b"
            strokeWidth={2}
            dot={false}
            name="Temperature"
          />
          <Line
            yAxisId="wind"
            type="monotone"
            dataKey="windSpeed"
            stroke="#6366f1"
            strokeWidth={1.5}
            dot={false}
            name="Wind Speed"
          />
          <Line
            yAxisId="cloud"
            type="monotone"
            dataKey="cloudCover"
            stroke="#94a3b8"
            strokeWidth={1.5}
            strokeDasharray="4 3"
            dot={false}
            connectNulls
            name="Cloud Cover"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

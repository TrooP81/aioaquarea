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
  Area,
  ComposedChart,
  Bar,
} from "recharts";

interface PricePoint {
  ts: string;
  price_eur_per_kwh: number;
}

export function PriceChart() {
  const [prices, setPrices] = useState<PricePoint[]>([]);
  const [consumption, setConsumption] = useState<any[]>([]);

  useEffect(() => {
    fetch("/api/prices?hours=48")
      .then((r) => r.json())
      .then(setPrices)
      .catch(() => {});

    fetch("/api/consumption/history?hours=24")
      .then((r) => r.json())
      .then(setConsumption)
      .catch(() => {});
  }, []);

  const chartData = prices.map((p) => {
    const hour = new Date(p.ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    const now = new Date();
    const priceTime = new Date(p.ts);
    const isFuture = priceTime > now;

    return {
      time: hour,
      price: p.price_eur_per_kwh * 100, // Convert to cents
      isFuture,
    };
  });

  const consumptionData = consumption.map((c) => ({
    time: new Date(c.ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    total_kwh: c.total_kwh || 0,
    heat: c.heat_kwh || 0,
    tank: c.tank_kwh || 0,
  }));

  return (
    <>
      <div className="chart-container">
        <div className="chart-title">Electricity Price (€ cents/kWh) — 48h</div>
        {chartData.length > 0 ? (
          <ResponsiveContainer width="100%" height={250}>
            <ComposedChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis
                dataKey="time"
                stroke="#94a3b8"
                fontSize={11}
                interval={3}
              />
              <YAxis stroke="#94a3b8" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: "8px",
                }}
              />
              <Area
                type="stepAfter"
                dataKey="price"
                stroke="#3b82f6"
                fill="rgba(59, 130, 246, 0.1)"
                strokeWidth={2}
              />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "3rem" }}>
            No price data yet. Prices will appear after the first ENTSO-E fetch.
          </p>
        )}
      </div>

      <div className="chart-container">
        <div className="chart-title">Energy Consumption (kWh) — 24h</div>
        {consumptionData.length > 0 ? (
          <ResponsiveContainer width="100%" height={200}>
            <ComposedChart data={consumptionData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} />
              <YAxis stroke="#94a3b8" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: "8px",
                }}
              />
              <Bar dataKey="heat" fill="#f59e0b" stackId="a" name="Heating" />
              <Bar dataKey="tank" fill="#3b82f6" stackId="a" name="Hot Water" />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "3rem" }}>
            No consumption data yet. Data will appear after first device poll.
          </p>
        )}
      </div>
    </>
  );
}

"use client";

import { useEffect, useState } from "react";
import {
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Area,
  ComposedChart,
} from "recharts";

interface PricePoint {
  ts: string;
  price_eur_per_kwh: number;
}

export function PriceChart() {
  const [prices, setPrices] = useState<PricePoint[]>([]);

  useEffect(() => {
    fetch("/api/prices?hours=48")
      .then((r) => r.json())
      .then(setPrices)
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

  return (
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
          No price data yet. Prices will appear after the first price fetch.
        </p>
      )}
    </div>
  );
}

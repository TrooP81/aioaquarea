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
  ReferenceLine,
} from "recharts";
import { useCurrency, priceAxisLabel } from "./useCurrency";
import { useTimeFormat, formatTime } from "./useTimeFormat";

interface PricePoint {
  ts: string;
  price_eur_per_kwh: number;
}

export function PriceChart() {
  const [prices, setPrices] = useState<PricePoint[]>([]);
  const currency = useCurrency();
  const timeFormat = useTimeFormat();

  useEffect(() => {
    fetch("/api/prices?hours=48")
      .then((r) => r.json())
      .then(setPrices)
      .catch(() => {});
  }, []);

  const now = Date.now();
  const chartData = currency.loaded
    ? prices.map((p) => {
        return {
          timestamp: new Date(p.ts).getTime(),
          price: +(p.price_eur_per_kwh * currency.multiplier).toFixed(2),
        };
      })
    : [];
  const chartStart = chartData[0]?.timestamp;
  const chartEnd = chartData[chartData.length - 1]?.timestamp;
  const showNowMarker = chartStart != null && chartEnd != null && chartStart <= now && now <= chartEnd;
  const formatTimestamp = (timestamp: number) =>
    new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
      hour12: timeFormat.hour12,
    }).format(new Date(timestamp));

  return (
    <div className="chart-container" role="region" aria-label="Electricity price chart">
      <div className="chart-title">
        Electricity Price ({priceAxisLabel(currency)}) — Past 24h / Next 24h
      </div>
      <div className="chart-caption">
        Past prices are left of Now; forecast prices are right of it. The optimizer shifts heating
        toward the cheapest hours.
      </div>
      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={250}>
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
            <XAxis
              dataKey="timestamp"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              stroke="#94a3b8"
              fontSize={11}
              interval={3}
              tickFormatter={(timestamp) => formatTime(new Date(timestamp), timeFormat.hour12)}
            />
            <YAxis stroke="#94a3b8" fontSize={11} />
            <Tooltip
              labelFormatter={(timestamp) => formatTimestamp(Number(timestamp))}
              contentStyle={{
                background: "#1e293b",
                border: "1px solid #334155",
                borderRadius: "8px",
              }}
            />
            {showNowMarker && (
              <ReferenceLine
                x={now}
                isFront
                stroke="#f59e0b"
                strokeDasharray="4 4"
                strokeWidth={2}
                label={{
                  value: "Now",
                  position: "insideTopRight",
                  fill: "#f59e0b",
                  fontSize: 12,
                  fontWeight: 700,
                }}
              />
            )}
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
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <div className="chart-skeleton" style={{ width: "70%" }} />
          <p className="text-muted text-center">No price data yet. Prices will appear after the first price fetch.</p>
        </div>
      )}
    </div>
  );
}

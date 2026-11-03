"use client";

import { useEffect, useState } from "react";
import {
  Area, CartesianGrid, ComposedChart, Legend, Line,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { useRefresh } from "./RefreshContext";
import { useCurrency } from "./useCurrency";
import { Banner } from "./Banner";

interface ForecastPoint {
  hour: number;
  ts?: string;
  interval_start_utc?: string;
  interval_end_utc?: string;
  predicted_indoor_temp?: number | null;
  target?: number | null;
  control_target_c?: number | null;
}

interface ComfortForecast {
  current_indoor: number | null;
  forecast_status?: string;
  forecast_unavailable_reason?: string | null;
  forecast_with_plan: ForecastPoint[];
  forecast_no_heating: ForecastPoint[];
  target_schedule: ForecastPoint[];
  weather_forecast?: Array<{ ts: string; outdoor_temp?: number | null }>;
  price_forecast?: Array<{ ts: string; price_eur_per_kwh?: number | null }>;
}

function intervalStart(point: ForecastPoint): string | undefined {
  if (point.interval_start_utc) return point.interval_start_utc;
  if (!point.ts) return undefined;
  const end = Date.parse(point.ts);
  return Number.isFinite(end) ? new Date(end - 3_600_000).toISOString() : undefined;
}

function sameInstant(left: string, right: string): boolean {
  const leftMs = Date.parse(left);
  const rightMs = Date.parse(right);
  return Number.isFinite(leftMs) && leftMs === rightMs;
}

function targetAtControlStart(
  schedule: ForecastPoint[],
  point: ForecastPoint,
  start: string | undefined,
): number | undefined {
  if (typeof point.control_target_c === "number") return point.control_target_c;
  if (typeof point.target === "number") return point.target;
  if (start) {
    const instant = Date.parse(start);
    const interval = schedule.find((candidate) => {
      if (!candidate.interval_start_utc || !candidate.interval_end_utc) return false;
      return Date.parse(candidate.interval_start_utc) <= instant
        && instant < Date.parse(candidate.interval_end_utc);
    });
    if (interval) return interval.target ?? undefined;
  }
  if (!point.interval_start_utc && !point.interval_end_utc) {
    const target = schedule.find((candidate) => candidate.ts === point.ts)?.target
      ?? schedule.find((candidate) => candidate.hour === point.hour)?.target;
    return typeof target === "number" ? target : undefined;
  }
  return undefined;
}

/** The combined view uses the same persisted forecast as the detailed charts. */
export function ComfortImpactChart() {
  const { refreshEpoch } = useRefresh();
  const currency = useCurrency();
  const [forecast, setForecast] = useState<ComfortForecast | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetch("/api/thermal/indoor-forecast?hours=24", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Comfort forecast could not be loaded.");
        const body: ComfortForecast = await response.json();
        if (!Array.isArray(body.forecast_with_plan) || !Array.isArray(body.forecast_no_heating)
          || !Array.isArray(body.target_schedule)) throw new Error("Comfort forecast could not be loaded.");
        return body;
      })
      .then((body) => { if (!controller.signal.aborted) setForecast(body); })
      .catch(() => {
        if (!controller.signal.aborted) setError("Comfort forecast could not be loaded.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refreshEpoch, retry]);

  const points = forecast?.forecast_with_plan.map((point) => {
    const start = intervalStart(point);
    const weatherTs = start ?? point.ts;
    const weather = weatherTs
      ? forecast.weather_forecast?.find((candidate) => sameInstant(candidate.ts, weatherTs))
      : undefined;
    const price = weatherTs
      ? forecast.price_forecast?.find((candidate) => sameInstant(candidate.ts, weatherTs))
      : undefined;
    const noHeating = forecast.forecast_no_heating.find((candidate) => {
      if (point.interval_start_utc && candidate.interval_start_utc) {
        return candidate.interval_start_utc === point.interval_start_utc;
      }
      if (point.interval_end_utc && candidate.interval_end_utc) {
        return candidate.interval_end_utc === point.interval_end_utc;
      }
      if (point.ts && candidate.ts) return candidate.ts === point.ts;
      if (point.interval_start_utc || point.interval_end_utc) return false;
      return !candidate.interval_start_utc && !candidate.interval_end_utc
        && candidate.hour === point.hour;
    });
    return {
      hour: point.hour,
      indoor: point.predicted_indoor_temp,
      noHeating: noHeating?.predicted_indoor_temp,
      target: targetAtControlStart(forecast.target_schedule, point, start),
      outdoor: weather?.outdoor_temp,
      price: price?.price_eur_per_kwh != null && currency.loaded
        ? price.price_eur_per_kwh * currency.multiplier : null,
    };
  }) ?? [];
  const horizon = points.length ? Math.max(...points.map((point) => point.hour)) : 0;
  const unavailable = forecast?.forecast_status === "unavailable" || points.length === 0;

  return (
    <section className="chart-container comfort-impact-chart" aria-label="Indoor comfort, weather and price forecast">
      <h2 className="chart-title">Indoor Comfort, Weather &amp; Price{horizon > 0 ? ` — ${horizon}h` : ""}</h2>
      {loading ? <div className="chart-skeleton" /> : error ? (
        <Banner tone="warning"><p>{error}</p><button className="btn btn-sm" onClick={() => setRetry((value) => value + 1)}>Retry</button></Banner>
      ) : unavailable ? (
        <p className="text-muted">Comfort forecast unavailable. {forecast?.forecast_unavailable_reason?.replace(/_/g, " ")}</p>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={points}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="hour" unit="h" stroke="#94a3b8" />
              <YAxis yAxisId="temperature" unit="°C" stroke="#94a3b8" domain={["auto", "auto"]} />
              <YAxis yAxisId="price" orientation="right" stroke="#94a3b8" />
              <Tooltip contentStyle={{ background: "#1e293b", borderColor: "#334155" }} />
              <Legend />
              <Area yAxisId="temperature" dataKey="target" name="Scheduled indoor target" fill="#6366f1" fillOpacity={0.12} stroke="#6366f1" isAnimationActive={false} />
              <Line yAxisId="temperature" dataKey="indoor" name="Predicted indoor" stroke="#f59e0b" dot={false} isAnimationActive={false} />
              <Line yAxisId="temperature" dataKey="noHeating" name="No heating" stroke="#ef4444" dot={false} strokeDasharray="5 5" isAnimationActive={false} />
              <Line yAxisId="temperature" dataKey="outdoor" name="Outdoor" stroke="#38bdf8" dot={false} isAnimationActive={false} />
              <Line yAxisId="price" dataKey="price" name={`Price (${currency.priceLabel})`} stroke="#a78bfa" dot={false} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <p className="comfort-impact-note">Forecast evidence only, not a guarantee of comfort or permission to control the heat pump. Temperature and price use separate axes.</p>
        </>
      )}
    </section>
  );
}

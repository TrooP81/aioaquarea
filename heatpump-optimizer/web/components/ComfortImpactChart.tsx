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
  predicted_indoor_temp?: number | null;
  target?: number | null;
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
    // Join independent series by their server timestamps, never array position.
    const timestamp = point.ts ? Date.parse(point.ts) : NaN;
    const weather = forecast.weather_forecast?.find((weather) => Date.parse(weather.ts) === timestamp);
    const price = forecast.price_forecast?.find((price) => Date.parse(price.ts) === timestamp);
    return {
      hour: point.hour,
      indoor: point.predicted_indoor_temp,
      noHeating: forecast.forecast_no_heating.find((other) => other.hour === point.hour)?.predicted_indoor_temp,
      target: forecast.target_schedule.find((other) => other.hour === point.hour)?.target,
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

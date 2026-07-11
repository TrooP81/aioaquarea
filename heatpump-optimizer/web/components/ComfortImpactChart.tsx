"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCurrency } from "./useCurrency";

interface PricePoint {
  ts: string;
  price_eur_per_kwh: number;
}

interface PlannedAction {
  hour: number;
  action_type: string;
  status: string;
}

interface ForecastWeather {
  ts?: string;
  outdoor_temp: number;
  wind_speed: number;
  irradiance: number;
  precipitation: number;
}

/** Weather endpoint shape, retained as a fallback while older APIs are live. */
interface WeatherPoint {
  ts: string;
  temperature: number | null;
  wind_speed: number | null;
  irradiance: number | null;
  precipitation: number | null;
}

interface IndoorForecastData {
  current_indoor: number;
  outdoor_temp: number;
  forecast_with_plan: { hour: number; predicted_indoor_temp: number }[];
  forecast_no_heating: { hour: number; predicted_indoor_temp: number }[];
  target_schedule: { hour: number; target: number; comfort_hour: boolean }[];
  planned_actions: PlannedAction[];
  weather_forecast?: ForecastWeather[];
}

interface ChartPoint {
  time: string;
  indoor?: number;
  noHeating?: number;
  target?: number;
  outdoor?: number;
  price?: number;
  wind?: number;
  rain?: number;
  sunshine?: number;
}

const CONTROL_ACTIONS: Record<string, string> = {
  zone_temp_boost: "Boost",
  zone_temp_restore: "Restore",
  eco_mode_on: "Eco",
  eco_mode_off: "Normal",
  normal_mode_on: "Normal",
  comfort_mode_on: "Comfort",
  quiet_mode_on: "Quiet",
  quiet_mode_off: "Quiet off",
};

function nearestPrice(timestamp: string | undefined, prices: PricePoint[]): number | undefined {
  if (!timestamp || prices.length === 0) return undefined;
  const target = new Date(timestamp).getTime();
  const closest = prices.reduce((best, point) =>
    Math.abs(new Date(point.ts).getTime() - target) < Math.abs(new Date(best.ts).getTime() - target)
      ? point
      : best,
  );
  return Math.abs(new Date(closest.ts).getTime() - target) <= 90 * 60 * 1000
    ? closest.price_eur_per_kwh
    : undefined;
}

function nearestWeather(timestamp: string | undefined, weather: WeatherPoint[]): WeatherPoint | undefined {
  if (!timestamp || weather.length === 0) return undefined;
  const target = new Date(timestamp).getTime();
  const closest = weather.reduce((best, point) =>
    Math.abs(new Date(point.ts).getTime() - target) < Math.abs(new Date(best.ts).getTime() - target)
      ? point
      : best,
  );
  return Math.abs(new Date(closest.ts).getTime() - target) <= 90 * 60 * 1000 ? closest : undefined;
}

function ComfortTooltip({
  active,
  payload,
  label,
  priceLabel,
}: {
  active?: boolean;
  payload?: Array<{ payload?: ChartPoint }>;
  label?: string | number;
  priceLabel: string;
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;

  const rows = [
    ["Comfort forecast", point.indoor, "°C"],
    ["No heating", point.noHeating, "°C"],
    ["Comfort target", point.target, "°C"],
    ["Outdoor", point.outdoor, "°C"],
    ["Price", point.price, ` ${priceLabel}`],
    ["Wind", point.wind, " m/s"],
    ["Rain", point.rain, " mm/h"],
    ["Sun", point.sunshine, " W/m²"],
  ] as const;

  return (
    <div className="comfort-impact-tooltip">
      <strong>{label}</strong>
      {rows.map(([name, value, unit]) =>
        value == null ? null : (
          <div key={name}>
            <span>{name}</span>
            <b>{Number(value).toFixed(name === "Price" ? 2 : 1)}{unit}</b>
          </div>
        ),
      )}
    </div>
  );
}

/**
 * The default decision chart: comfort, weather, price, and heating control on
 * one shared hourly timeline. Detailed raw charts remain available on demand.
 */
export function ComfortImpactChart() {
  const [forecast, setForecast] = useState<IndoorForecastData | null>(null);
  const [prices, setPrices] = useState<PricePoint[]>([]);
  const [weather, setWeather] = useState<WeatherPoint[]>([]);
  const currency = useCurrency();

  useEffect(() => {
    Promise.all([
      fetch("/api/thermal/indoor-forecast?hours=24").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/prices?hours=48").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/weather?hours=48").then((r) => (r.ok ? r.json() : [])),
    ])
      .then(([indoor, priceRows, weatherRows]) => {
        setForecast(indoor);
        setPrices(Array.isArray(priceRows) ? priceRows : []);
        setWeather(Array.isArray(weatherRows) ? weatherRows : []);
      })
      .catch(() => {});
  }, []);

  const chartData = useMemo<ChartPoint[]>(() => {
    if (!forecast) return [];
    const data: ChartPoint[] = [
      {
        time: "Now",
        indoor: forecast.current_indoor,
        outdoor: forecast.outdoor_temp,
      },
    ];

    for (let index = 0; index < forecast.forecast_with_plan.length; index += 1) {
      const managed = forecast.forecast_with_plan[index];
      // Older live APIs do not include weather_forecast in this response. Use
      // the public weather feed and the forecast hour as a stable fallback.
      const timestamp = forecast.weather_forecast?.[index]?.ts
        ?? new Date(Date.now() + managed.hour * 3_600_000).toISOString();
      const embeddedWeather = forecast.weather_forecast?.[index];
      const fallbackWeather = nearestWeather(timestamp, weather);
      const price = nearestPrice(timestamp, prices);
      data.push({
        time: `+${managed.hour}h`,
        indoor: managed.predicted_indoor_temp,
        noHeating: forecast.forecast_no_heating[index]?.predicted_indoor_temp,
        target: forecast.target_schedule[index]?.target,
        outdoor: embeddedWeather?.outdoor_temp ?? fallbackWeather?.temperature ?? undefined,
        price: price == null || !currency.loaded ? undefined : +(price * currency.multiplier).toFixed(2),
        wind: embeddedWeather?.wind_speed ?? fallbackWeather?.wind_speed ?? undefined,
        rain: embeddedWeather?.precipitation ?? fallbackWeather?.precipitation ?? undefined,
        sunshine: embeddedWeather?.irradiance ?? fallbackWeather?.irradiance ?? undefined,
      });
    }
    return data;
  }, [currency.loaded, currency.multiplier, forecast, prices, weather]);

  const controls = (forecast?.planned_actions ?? []).filter(
    (action) => CONTROL_ACTIONS[action.action_type] && action.hour > 0 && action.hour <= 24,
  );

  if (!forecast) {
    return (
      <div className="chart-container" role="region" aria-label="Indoor comfort, weather and price chart">
        <div className="chart-title">Indoor Comfort, Weather & Price</div>
        <div className="chart-skeleton-wrapper">
          <div className="chart-skeleton" />
          <p className="text-muted text-center">Loading the comfort forecast…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-container comfort-impact-chart" role="region" aria-label="Indoor comfort, weather and price chart">
      <div className="chart-title">Indoor Comfort, Weather & Price — 24h</div>
      <div className="chart-caption">
        The comfort forecast follows the schedule target; compare it with no heating, outdoor weather, and electricity price to see why control changes are planned.
      </div>
      <ResponsiveContainer width="100%" height={360}>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} interval={2} />
          <YAxis yAxisId="temperature" stroke="#94a3b8" fontSize={11} unit="°C" domain={["auto", "auto"]} />
          <YAxis yAxisId="price" orientation="right" stroke="#60a5fa" fontSize={11} width={58} />
          <Tooltip content={(props) => <ComfortTooltip {...props} priceLabel={currency.priceLabel} />} />
          <Legend />
          <Area
            yAxisId="price"
            type="stepAfter"
            dataKey="price"
            fill="rgba(59, 130, 246, 0.14)"
            stroke="#60a5fa"
            strokeWidth={1.5}
            name={`Price (${currency.priceLabel})`}
          />
          <Line yAxisId="temperature" type="monotone" dataKey="outdoor" stroke="#38bdf8" strokeWidth={1.5} strokeDasharray="4 4" dot={false} name="Outdoor" />
          <Line yAxisId="temperature" type="monotone" dataKey="target" stroke="#a78bfa" strokeWidth={1.5} strokeDasharray="3 3" dot={false} name="Comfort target" />
          <Line yAxisId="temperature" type="monotone" dataKey="noHeating" stroke="#fb7185" strokeWidth={1.5} strokeDasharray="6 4" dot={false} name="No heating" />
          <Line yAxisId="temperature" type="monotone" dataKey="indoor" stroke="#f59e0b" strokeWidth={2.5} dot={false} connectNulls name="Comfort forecast" />
          {controls.map((action, index) => (
            <ReferenceLine
              key={`${action.hour}-${action.action_type}-${index}`}
              yAxisId="temperature"
              x={`+${action.hour}h`}
              stroke="#22c55e"
              strokeDasharray="3 3"
              label={{ value: CONTROL_ACTIONS[action.action_type], position: "top", fontSize: 10, fill: "#86efac" }}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
      <p className="comfort-impact-note">
        Hover a point for wind, rain, sunshine, and price. Green markers are planned heating-control changes; hot water is kept in the optional details below.
      </p>
    </div>
  );
}

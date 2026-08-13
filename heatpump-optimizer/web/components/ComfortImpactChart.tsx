"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCurrency } from "./useCurrency";

interface ComfortMiss {
  hour: number;
  ts?: string | null;
  predicted_c: number;
  target_c: number;
  shortfall_c: number;
}

interface ComfortRecommendation {
  title: string;
  setting_key?: string;
  current_value_c?: number;
  minimum_candidate_value_c?: number;
  confidence?: string;
  verification_required?: boolean;
  summary?: string;
}

interface IndoorForecastData {
  current_indoor: number | null;
  outdoor_temp: number | null;
  forecast_with_plan: Array<{
    hour: number;
    ts?: string | null;
    predicted_indoor_temp: number;
    space_heating_fraction?: number;
    prediction_lower_c?: number | null;
    prediction_upper_c?: number | null;
  }>;
  forecast_no_heating: Array<{ hour: number; predicted_indoor_temp: number }>;
  target_schedule: Array<{ hour: number; target: number; comfort_hour: boolean }>;
  planned_actions: Array<{ hour: number; action_type: string; status: string }>;
  weather_forecast: Array<{
    ts: string;
    outdoor_temp: number | null;
    wind_speed: number | null;
    irradiance: number | null;
    precipitation: number | null;
    input_status?: string;
    imputed_fields?: string[];
  }>;
  price_forecast: Array<{
    ts: string;
    price_eur_per_kwh: number | null;
    price_per_kwh?: number | null;
    currency?: string | null;
  }>;
  forecast_source: "active_plan" | "live_estimate" | "unavailable";
  forecast_status: string;
  forecast_unavailable_reason?: string | null;
  display_status?: "fresh" | "aging" | "stale" | "diverged" | "degraded" | "unavailable";
  plan_age_seconds?: number | null;
  sensor_age_seconds?: number | null;
  current_vs_plan_delta_c?: number | null;
  plan_id: number | null;
  plan_created_at?: string | null;
  comfort_assessment?: {
    state?: "on_target" | "at_risk" | "unavailable";
    summary?: string;
    first_miss?: ComfortMiss;
    worst_miss?: ComfortMiss;
    misses?: ComfortMiss[];
    controllability?: { status?: string; cutoff_c?: number };
    recommendations?: ComfortRecommendation[];
  };
}

interface ChartPoint {
  time: string;
  ts?: string;
  indoor?: number;
  forecastLower?: number;
  forecastUpper?: number;
  noHeating?: number;
  target?: number;
  outdoor?: number;
  price?: number;
  wind?: number;
  rain?: number;
  sunshine?: number;
  heatingFraction?: number;
  weatherImputed?: boolean;
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

function localTime(iso: string | null | undefined, fallback: string): string {
  if (!iso) return fallback;
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return fallback;
  return value.toLocaleString([], {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function ageLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "unknown age";
  if (seconds < 90) return "just updated";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min old`;
  return `${Math.round(seconds / 3600)} h old`;
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
    ["Plan forecast", point.indoor, "°C"],
    ["80% range", point.forecastLower != null && point.forecastUpper != null ? `${point.forecastLower.toFixed(1)}–${point.forecastUpper.toFixed(1)}` : undefined, "°C"],
    ["No heating", point.noHeating, "°C"],
    ["Comfort target", point.target, "°C"],
    ["Outdoor", point.outdoor, "°C"],
    ["Price", point.price, ` ${priceLabel}`],
    ["Wind", point.wind, " m/s"],
    ["Rain", point.rain, " mm/h"],
    ["Sun", point.sunshine, " W/m²"],
    ["Planned heat", point.heatingFraction == null ? undefined : point.heatingFraction * 100, "%"],
  ] as const;
  return (
    <div className="comfort-impact-tooltip">
      <strong>{label}</strong>
      {point.weatherImputed && <div className="text-warning text-xs">Some weather inputs are estimated</div>}
      {rows.map(([name, value, unit]) =>
        value == null ? null : (
          <div key={name}>
            <span>{name}</span>
            <b>{typeof value === "string" ? value : Number(value).toFixed(name === "Price" ? 2 : name === "Planned heat" ? 0 : 1)}{unit}</b>
          </div>
        ),
      )}
    </div>
  );
}

export function ComfortImpactChart() {
  const [forecast, setForecast] = useState<IndoorForecastData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showTable, setShowTable] = useState(false);
  const [showCounterfactual, setShowCounterfactual] = useState(true);
  const currency = useCurrency();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/thermal/indoor-forecast?hours=24");
      if (!response.ok) throw new Error(`Forecast API returned ${response.status}`);
      setForecast(await response.json());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load forecast");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const chartData = useMemo<ChartPoint[]>(() => {
    if (!forecast) return [];
    const data: ChartPoint[] = [{
      time: "Now",
      indoor: forecast.current_indoor ?? undefined,
      outdoor: forecast.outdoor_temp ?? undefined,
    }];
    forecast.forecast_with_plan.forEach((managed, index) => {
      const weather = forecast.weather_forecast[index];
      const rawPrice = forecast.price_forecast[index]?.price_per_kwh
        ?? forecast.price_forecast[index]?.price_eur_per_kwh;
      data.push({
        time: localTime(managed.ts ?? weather?.ts, `+${managed.hour}h`),
        ts: managed.ts ?? weather?.ts,
        indoor: managed.predicted_indoor_temp,
        forecastLower: managed.prediction_lower_c ?? undefined,
        forecastUpper: managed.prediction_upper_c ?? undefined,
        noHeating: forecast.forecast_no_heating[index]?.predicted_indoor_temp,
        target: forecast.target_schedule[index]?.target,
        outdoor: weather?.outdoor_temp ?? undefined,
        price: rawPrice == null || !currency.loaded ? undefined : +(rawPrice * currency.multiplier).toFixed(2),
        wind: weather?.wind_speed ?? undefined,
        rain: weather?.precipitation ?? undefined,
        sunshine: weather?.irradiance ?? undefined,
        heatingFraction: managed.space_heating_fraction,
        weatherImputed: weather?.input_status === "imputed",
      });
    });
    return data;
  }, [currency.loaded, currency.multiplier, forecast]);

  const allCurvesOverlap = chartData.slice(1).every((point) =>
    point.indoor == null || point.noHeating == null || Math.abs(point.indoor - point.noHeating) < 0.05,
  );
  const controls = (forecast?.planned_actions ?? []).filter(
    (action) => CONTROL_ACTIONS[action.action_type] && action.hour > 0 && action.hour <= 24,
  );
  const assessment = forecast?.comfort_assessment;
  const recommendation = assessment?.recommendations?.[0];
  const riskStart = assessment?.first_miss?.ts
    ? localTime(assessment.first_miss.ts, `+${assessment.first_miss.hour}h`)
    : assessment?.first_miss ? `+${assessment.first_miss.hour}h` : null;
  const riskEndMiss = assessment?.misses?.[Math.max((assessment?.misses?.length ?? 1) - 1, 0)];
  const riskEnd = riskEndMiss?.ts
    ? localTime(riskEndMiss.ts, `+${riskEndMiss.hour}h`)
    : riskEndMiss ? `+${riskEndMiss.hour}h` : riskStart;
  const status = forecast?.display_status ?? (forecast?.forecast_status === "available" ? "fresh" : "unavailable");
  const trustworthy = status === "fresh" && chartData.some((point) => point.forecastLower != null && point.forecastUpper != null);

  if (loading && !forecast) {
    return (
      <div className="chart-container" role="region" aria-label="Indoor comfort forecast">
        <div className="chart-title">Indoor Comfort, Weather & Price</div>
        <div className="chart-skeleton-wrapper"><div className="chart-skeleton" /></div>
      </div>
    );
  }

  if (error && !forecast) {
    return (
      <div className="chart-container" role="alert">
        <div className="chart-title">Indoor Comfort, Weather & Price</div>
        <p className="text-danger">{error}</p>
        <button className="btn btn-sm" onClick={load}>Retry forecast</button>
      </div>
    );
  }

  if (!forecast || forecast.forecast_status === "unavailable") {
    return (
      <div className="chart-container" role="status">
        <div className="chart-title">Indoor Comfort, Weather & Price</div>
        <p className="text-warning">Forecast unavailable: {(forecast?.forecast_unavailable_reason ?? "missing trusted input").replace(/_/g, " ")}.</p>
        <button className="btn btn-sm" onClick={load}>Retry forecast</button>
      </div>
    );
  }

  return (
    <div className="chart-container comfort-impact-chart" role="region" aria-label="Indoor comfort, weather and price forecast">
      <div className="comfort-forecast-heading">
        <div>
          <div className="chart-title">Indoor Comfort, Weather & Price — {forecast.forecast_with_plan.length}h</div>
          <div className="chart-caption">
            {forecast.forecast_source === "active_plan"
              ? `Plan #${forecast.plan_id ?? "—"} · ${ageLabel(forecast.plan_age_seconds)}`
              : "Live scenario · not frozen into a plan"}
          </div>
        </div>
        <span className={`forecast-trust-badge forecast-trust-badge--${status}`}>
          {trustworthy ? "Validated range" : status === "fresh" ? "Unvalidated scenario" : status.replace(/_/g, " ")}
        </span>
      </div>

      {status !== "fresh" && (
        <div className="forecast-data-warning" role="status">
          <strong>Use this forecast with caution.</strong>
          <span>
            {status === "diverged"
              ? ` Live indoor temperature differs from the plan start by ${Math.abs(forecast.current_vs_plan_delta_c ?? 0).toFixed(1)}°C.`
              : status === "degraded"
                ? " One or more weather inputs are estimated and are not plotted as observations."
                : ` Forecast or sensor input is ${status}.`}
          </span>
        </div>
      )}

      {assessment?.state === "at_risk" && (
        <div className="comfort-risk-card" role="alert">
          <div>
            <strong>Comfort risk {riskStart ? `from ${riskStart}` : ""}</strong>
            <p>{assessment.summary}</p>
            {recommendation && (
              <p className="text-muted text-sm">
                Manual test only: {recommendation.current_value_c?.toFixed(1)}°C → at least {recommendation.minimum_candidate_value_c?.toFixed(1)}°C.
                {" "}Confidence {recommendation.confidence ?? "unknown"}; verify the measured result before another change.
              </p>
            )}
          </div>
          {recommendation?.setting_key && (
            <a className="btn btn-sm btn-primary" href={`/settings?tab=optimizer#controller-heat-curve`}>
              Review heat cutoff
            </a>
          )}
        </div>
      )}

      {allCurvesOverlap && (
        <p className="forecast-overlap-note">
          No room heating is planned, so Plan forecast and No heating are the same scenario.
        </p>
      )}

      <div className="chart-series-controls" aria-label="Forecast series controls">
        <label>
          <input type="checkbox" checked={showCounterfactual} onChange={(event) => setShowCounterfactual(event.target.checked)} />
          Show no-heating comparison
        </label>
        <button className="btn btn-sm" onClick={() => setShowTable((value) => !value)} aria-expanded={showTable}>
          {showTable ? "Hide data table" : "Show data table"}
        </button>
        <button className="btn btn-sm" onClick={load} disabled={loading}>{loading ? "Refreshing…" : "Refresh"}</button>
      </div>

      <ResponsiveContainer width="100%" height={310}>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="time" stroke="#94a3b8" fontSize={11} interval={2} />
          <YAxis stroke="#94a3b8" fontSize={11} unit="°C" domain={["auto", "auto"]} />
          <Tooltip content={(props) => <ComfortTooltip {...props} priceLabel={currency.priceLabel} />} />
          <Legend />
          {riskStart && riskEnd && <ReferenceArea x1={riskStart} x2={riskEnd} fill="#ef4444" fillOpacity={0.09} />}
          <Line type="monotone" dataKey="target" stroke="#a78bfa" strokeWidth={1.5} strokeDasharray="3 3" dot={false} name="Comfort target" />
          {showCounterfactual && !allCurvesOverlap && (
            <Line type="monotone" dataKey="noHeating" stroke="#fb7185" strokeWidth={1.5} strokeDasharray="6 4" dot={false} name="No heating" />
          )}
          <Line type="monotone" dataKey="forecastLower" stroke="#f59e0b" strokeOpacity={0.35} strokeWidth={1} strokeDasharray="2 4" dot={false} name="80% range" />
          <Line type="monotone" dataKey="forecastUpper" stroke="#f59e0b" strokeOpacity={0.35} strokeWidth={1} strokeDasharray="2 4" dot={false} legendType="none" />
          <Line type="monotone" dataKey="indoor" stroke="#f59e0b" strokeWidth={2.5} strokeDasharray={trustworthy ? undefined : "7 4"} dot={false} connectNulls name={allCurvesOverlap ? "Plan / no heating" : "Plan forecast"} />
          {controls.map((action, index) => (
            <ReferenceLine
              key={`${action.hour}-${action.action_type}-${index}`}
              x={chartData[action.hour]?.time}
              stroke="#22c55e"
              strokeDasharray="3 3"
              label={{ value: CONTROL_ACTIONS[action.action_type], position: "top", fontSize: 10, fill: "#86efac" }}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>

      <div className="forecast-context-panels">
        <div className="forecast-context-panel">
          <strong>Weather</strong>
          <ResponsiveContainer width="100%" height={125}>
            <ComposedChart data={chartData}>
              <XAxis dataKey="time" hide />
              <YAxis yAxisId="temp" stroke="#38bdf8" fontSize={10} width={38} />
              <YAxis yAxisId="rain" orientation="right" stroke="#60a5fa" fontSize={10} width={34} />
              <Tooltip content={(props) => <ComfortTooltip {...props} priceLabel={currency.priceLabel} />} />
              <Line yAxisId="temp" type="monotone" dataKey="outdoor" stroke="#38bdf8" dot={false} name="Outdoor °C" />
              <Bar yAxisId="rain" dataKey="rain" fill="#2563eb" opacity={0.55} name="Rain mm/h" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <div className="forecast-context-panel">
          <strong>Price & planned room heat</strong>
          <ResponsiveContainer width="100%" height={125}>
            <ComposedChart data={chartData}>
              <XAxis dataKey="time" hide />
              <YAxis yAxisId="price" stroke="#60a5fa" fontSize={10} width={42} />
              <YAxis yAxisId="heat" orientation="right" domain={[0, 1]} hide />
              <Tooltip content={(props) => <ComfortTooltip {...props} priceLabel={currency.priceLabel} />} />
              <Area yAxisId="price" type="stepAfter" dataKey="price" fill="rgba(59,130,246,.18)" stroke="#60a5fa" name={`Price (${currency.priceLabel})`} />
              <Bar yAxisId="heat" dataKey="heatingFraction" fill="#22c55e" opacity={0.65} name="Planned heat" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      {showTable && (
        <div className="forecast-table-wrap">
          <table className="forecast-table">
            <caption>Hourly inputs and indoor-temperature forecast</caption>
            <thead><tr><th>Time</th><th>Plan</th><th>Target</th><th>Outdoor</th><th>Rain</th><th>Price</th><th>Heat</th></tr></thead>
            <tbody>
              {chartData.slice(1).map((point) => (
                <tr key={point.ts ?? point.time}>
                  <th>{point.time}</th>
                  <td>{point.indoor?.toFixed(1) ?? "—"}°C</td>
                  <td>{point.target?.toFixed(1) ?? "—"}°C</td>
                  <td>{point.weatherImputed ? "Estimated" : point.outdoor != null ? `${point.outdoor.toFixed(1)}°C` : "Missing"}</td>
                  <td>{point.rain != null ? `${point.rain.toFixed(1)} mm` : "Missing"}</td>
                  <td>{point.price != null ? `${point.price.toFixed(2)} ${currency.priceLabel}` : "Missing"}</td>
                  <td>{point.heatingFraction != null ? `${Math.round(point.heatingFraction * 100)}%` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

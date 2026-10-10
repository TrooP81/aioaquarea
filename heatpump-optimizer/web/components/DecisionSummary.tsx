"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { forecastDisplayStatusLabel, formatTime } from "@/lib/constants";
import { Banner } from "./Banner";
import { DataAge } from "./DataAge";
import { useTimeFormat } from "./useTimeFormat";
import type { ControlState } from "@/lib/api-types";

interface DecisionSummaryProps {
  plan: {
    id: number;
    optimizer_version: string;
  } | null;
  indoorTemp: number | null;
  indoorTimestamp: string | null;
  indoorStale: boolean;
  controlState: ControlState | null;
  onRetry: () => void;
}

interface ForecastBrief {
  forecast_status?: string;
  display_status?: string;
  comfort_assessment?: {
    state?: string;
    summary?: string;
    first_miss?: { ts?: string; hour?: number; predicted_c?: number; target_c?: number };
    worst_miss?: { shortfall_c?: number };
    controllability?: { status?: string };
    recommendations?: Array<{ setting_key?: string; confidence?: string }>;
  };
}

export function DecisionSummary({ plan, indoorTemp, indoorTimestamp, indoorStale, controlState, onRetry }: DecisionSummaryProps) {
  const [forecast, setForecast] = useState<ForecastBrief | null>(null);
  const [forecastLoading, setForecastLoading] = useState(true);
  const [forecastError, setForecastError] = useState<string | null>(null);
  const timeFormat = useTimeFormat();

  const loadForecast = () => {
    setForecastLoading(true);
    setForecastError(null);
    let active = true;
    fetch("/api/thermal/indoor-forecast?hours=24")
      .then((response) => {
        if (!response.ok) throw new Error(`Forecast returned ${response.status}`);
        return response.json();
      })
      .then((data) => {
        if (active) setForecast(data);
      })
      .catch(() => {
        if (active) setForecastError("Forecast details could not be loaded.");
      })
      .finally(() => { if (active) setForecastLoading(false); });
    return () => { active = false; };
  };

  useEffect(() => {
    const cleanup = loadForecast();
    return cleanup;
  }, [plan?.id]);

  const assessment = forecast?.comfort_assessment;
  const atRisk = assessment?.state === "at_risk";
  const missTime = assessment?.first_miss?.ts
    ? formatTime(assessment.first_miss.ts, timeFormat.hour12)
    : assessment?.first_miss?.hour != null ? `+${assessment.first_miss.hour}h` : null;
  const settingsLink = assessment?.recommendations?.[0]?.setting_key
    ? "/settings?tab=optimizer#controller-heat-curve"
    : null;

  return (
    <section className={`decision-summary ${atRisk ? "decision-summary--risk" : ""}`} aria-label="Current optimizer decision">
      <div className="decision-summary-header">
        <div>
          <span className="decision-summary-kicker">What matters now</span>
          <h2>{controlState?.headline ?? "Control state unavailable"}</h2>
        </div>
        <span className={`forecast-trust-badge forecast-trust-badge--${forecast?.display_status ?? "unavailable"}`}>
          {forecastDisplayStatusLabel(forecast?.display_status ?? "checking")}
        </span>
      </div>
      <div className="decision-summary-grid">
        <div>
          <span>Now</span>
          <strong>{indoorTemp != null ? `${indoorTemp.toFixed(1)}°C indoors` : "Waiting for indoor sensor"}</strong>
          <DataAge timestamp={indoorTimestamp} stale={indoorStale} />
          <small>{controlState?.detail ?? "Control state could not be loaded."}</small>
        </div>
        <div>
          <span>Next</span>
          <strong>No ordinary command</strong>
          <small>Ordinary writes are prohibited in this build.</small>
        </div>
        <div>
          <span>Why</span>
          <strong>{assessment?.summary ?? (plan ? "Legacy ordinary actions will not be dispatched." : "No immediate optimizer action is needed.")}</strong>
        </div>
        <div>
          <span>Expected impact</span>
          <strong>
            {atRisk
              ? `${assessment?.worst_miss?.shortfall_c?.toFixed(1) ?? "—"}°C below target${missTime ? ` around ${missTime}` : ""}`
              : "Forecast stays within the active target"}
          </strong>
          <div className="decision-summary-actions">
            <Link className="btn btn-sm" href="/?view=timeline">View plan</Link>
            {settingsLink && <Link className="btn btn-sm btn-primary" href={settingsLink}>Review blocker</Link>}
          </div>
        </div>
      </div>
      {!controlState && (
        <Banner tone="warning">
          <p>Control state unavailable.</p>
          <button className="btn btn-sm" onClick={onRetry}>Retry</button>
        </Banner>
      )}
      {forecastError && (
        <Banner tone="warning">
          <p>{forecastError}</p>
          <button className="btn btn-sm" onClick={loadForecast}>Retry</button>
        </Banner>
      )}
      {forecastLoading && <p className="text-muted text-sm">Loading forecast details...</p>}
      {!forecastLoading && !forecastError && !forecast && (
        <p className="text-muted text-sm">No forecast details are available yet.</p>
      )}
    </section>
  );
}

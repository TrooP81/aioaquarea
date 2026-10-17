"use client";

import { useCurrency, formatPricePerKwh } from "./useCurrency";
import { useTimeFormat, formatTime } from "./useTimeFormat";
import { ACTION_LABELS, STATUS_DISPLAY } from "@/lib/constants";
import { actionReason, type TimelineAction, type TimelinePoint } from "@/lib/timeline-data";
import type { ControlState } from "@/lib/api-types";

interface ComfortAssessment {
    state?: "on_target" | "at_risk" | "unavailable" | "degraded" | "conflict" | "room_overheat_suppression";
    summary?: string;
    first_miss?: { ts?: string; hour?: number; predicted_c?: number; target_c?: number; shortfall_c?: number };
    worst_miss?: { shortfall_c?: number };
    controllability?: { status?: string; cutoff_c?: number };
    recommendations?: Array<{ setting_key?: string; title?: string; summary?: string; current_value_c?: number; minimum_candidate_value_c?: number; confidence?: string; verification_required?: boolean }>;
}

function comfortHeading(state: ComfortAssessment["state"] | undefined, controlState: ControlState | null): string {
    if (controlState?.state === "comfort_at_risk" || state === "at_risk") return "Comfort risk";
    if (state === "degraded") return "Comfort warning";
    if (state === "conflict") return "Comfort conflict";
    if (state === "room_overheat_suppression") return "Room overheat suppression";
    if (state === "on_target") return "Comfort outlook";
    return "Comfort assessment unavailable";
}

function formatMissTime(miss: ComfortAssessment["first_miss"], hour12: boolean): string | null {
    if (!miss) return null;
    if (miss.ts && !Number.isNaN(new Date(miss.ts).getTime())) return formatTime(new Date(miss.ts), hour12);
    return miss.hour != null ? `+${miss.hour}h` : null;
}

function forecastSourceNote(forecast: Record<string, unknown> | null): string | null {
    const points = Array.isArray(forecast?.forecast_with_plan) ? forecast.forecast_with_plan : [];
    const baselineMode = (forecast?.space_heating_baseline as { effective_mode?: unknown } | undefined)?.effective_mode;
    const source = points.find((point): point is Record<string, unknown> => typeof point === "object" && point !== null && point.space_heating_source === "baseline")?.baseline_heating_source;
    if (baselineMode === "shadow") return "Automatic room heat is being evaluated but does not affect this displayed plan.";
    if (source === "history") return "Expected automatic room heat from recent history is included in this plan.";
    if (source === "default") return "Expected automatic room heat uses the configured default in this plan.";
    if (points.some((point) => typeof point === "object" && point !== null && (point as Record<string, unknown>).space_heating_source === "explicit_override")) return null;
    if (baselineMode) return "No room heating is expected in this plan.";
    return "No room heating is planned, so Plan forecast and No heating are the same scenario.";
}

export function ExplanationTimeline({ points, actions, forecast, controlState }: { points: TimelinePoint[]; actions: TimelineAction[]; forecast: Record<string, unknown> | null; controlState: ControlState | null }) {
    const currency = useCurrency();
    const time = useTimeFormat();
    const now = Date.now();
    const start = now - 24 * 3_600_000;
    const end = now + 24 * 3_600_000;
    const pricePoints = points.filter((point) => point.price != null);
    const maxPrice = Math.max(...pricePoints.map((point) => point.price ?? 0), 1);
    const percent = (value: number) => `${Math.max(0, Math.min(100, ((value - start) / (end - start)) * 100))}%`;
    const currentActions = actions.filter((action) => {
        const value = new Date(action.executed_at || action.scheduled_ts).getTime();
        return value >= start && value <= end;
    });
    const assessment = forecast?.comfort_assessment as ComfortAssessment | undefined;
    const recommendation = assessment?.recommendations?.[0];
    const riskState = controlState?.state === "comfort_at_risk" || assessment?.state === "at_risk";
    const showAssessment = assessment != null || controlState?.state === "comfort_at_risk";
    const missTime = formatMissTime(assessment?.first_miss, time.hour12);
    const summary = assessment?.summary ?? (controlState?.state === "comfort_at_risk" ? controlState.detail : "No forecast comfort assessment is available.");

    return (
        <section className="timeline-chart-container" aria-label="Explanation timeline">
            <div className="timeline-heading">
                <div><h2 className="chart-title">Why the system is acting</h2><p className="chart-caption">Past 24 hours of readings and outcomes, followed by the next 24 hours of plan and forecast.</p></div>
                <span className="timeline-price-label">Price ({currency.priceLabel})</span>
            </div>
            {showAssessment && (
                <section className={riskState ? "comfort-risk-card" : "comfort-assessment-card"} role={riskState ? "alert" : "status"}>
                    <div>
                        <strong>{comfortHeading(assessment?.state, controlState)}{riskState && missTime ? ` from ${missTime}` : ""}</strong>
                        <p>{summary}</p>
                        {assessment?.first_miss && (
                            <p className="text-muted text-sm">
                                {missTime ? `${missTime}: ` : ""}{assessment.first_miss.predicted_c?.toFixed(1) ?? "-"}°C forecast vs {assessment.first_miss.target_c?.toFixed(1) ?? "-"}°C target
                                {assessment.first_miss.shortfall_c != null ? ` (${assessment.first_miss.shortfall_c.toFixed(1)}°C below).` : "."}
                            </p>
                        )}
                        {assessment?.worst_miss?.shortfall_c != null && <p className="text-muted text-sm">Worst shortfall: {assessment.worst_miss.shortfall_c.toFixed(1)}°C below target.</p>}
                        {assessment?.controllability?.status && <p className="text-muted text-sm">Controllability: {assessment.controllability.status.replace(/_/g, " ")}{assessment.controllability.cutoff_c != null ? ` (cutoff ${assessment.controllability.cutoff_c.toFixed(1)}°C)` : ""}.</p>}
                        {recommendation && <p className="text-muted text-sm">{recommendation.title ? `${recommendation.title}. ` : ""}Manual test only: {recommendation.current_value_c?.toFixed(1) ?? "-"}°C → at least {recommendation.minimum_candidate_value_c?.toFixed(1) ?? "-"}°C. Confidence {recommendation.confidence ?? "unknown"}; verify the measured result before another change.</p>}
                    </div>
                    {recommendation?.setting_key && <a className="btn btn-sm btn-primary" href="/settings?tab=optimizer#controller-heat-curve">Review heat cutoff</a>}
                </section>
            )}
            {forecastSourceNote(forecast) && <p className="timeline-forecast-note">{forecastSourceNote(forecast)}</p>}
            <div className="timeline-scroll">
                <div className="explanation-timeline" data-testid="explanation-timeline" data-domain-start={new Date(start).toISOString()} data-domain-end={new Date(end).toISOString()}>
                    <div className="timeline-price-band" aria-label={`Stepped electricity price background in ${currency.priceLabel}`}>
                        {pricePoints.map((point) => <span key={point.timestamp} style={{ left: percent(point.timestamp), width: `${100 / Math.max(pricePoints.length, 1)}%`, height: `${((point.price ?? 0) / maxPrice) * 100}%` }} />)}
                    </div>
                    <div className="timeline-now" style={{ left: "50%" }}><span>Now</span></div>
                    <div className="timeline-temperature" aria-label="Indoor actual, forecast, comfort band, and target">
                        {points.filter((point) => point.actual != null || point.forecast != null).map((point) => <span key={point.timestamp} className={point.actual != null ? "timeline-actual" : "timeline-forecast"} style={{ left: percent(point.timestamp), bottom: `${Math.max(5, Math.min(90, ((point.actual ?? point.forecast ?? 0) - 10) * 7))}%` }} />)}
                        {points.some((point) => point.comfortMin != null) && <div className="timeline-comfort-band">Comfort band</div>}
                        {points.some((point) => point.target != null) && <div className="timeline-target">Hourly target</div>}
                    </div>
                    <div className="timeline-action-track" aria-label="Planned actions and outcomes">
                        {currentActions.map((action) => {
                            const occurredAt = new Date(action.executed_at || action.scheduled_ts).getTime();
                            const status = STATUS_DISPLAY[action.status] || { text: action.status, className: "" };
                            const label = ACTION_LABELS[action.action_type]?.label || action.action_type;
                            return <button key={action.id} className={`timeline-action-marker ${status.className}`} style={{ left: percent(occurredAt) }} aria-label={`${formatTime(new Date(occurredAt), time.hour12)}: ${label}, ${status.text}. ${actionReason(action)}`}><span>{label}</span><small>{status.text}: {actionReason(action)}</small></button>;
                        })}
                    </div>
                    <div className="timeline-axis"><span>{formatTime(new Date(start), time.hour12)}</span><span>Now</span><span>{formatTime(new Date(end), time.hour12)}</span></div>
                </div>
            </div>
            {pricePoints.length > 0 && <p className="timeline-price-summary">Current price series: {formatPricePerKwh(pricePoints[pricePoints.length - 1].price, currency)} ({currency.priceLabel}).</p>}
        </section>
    );
}
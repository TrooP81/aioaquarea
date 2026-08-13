"use client";

import { useEffect, useState } from "react";

export interface HeatCurveValues {
  outdoor_cold_c: number;
  supply_cold_c: number;
  outdoor_warm_c: number;
  supply_warm_c: number;
  heating_off_outdoor_c: number;
  delta_t_c: number;
}

interface HeatCurveAdviceResponse {
  status: "too_warm" | "too_cold" | "on_target" | "insufficient_data" | "verification_pending" | "outside_heating_season";
  indoor_error_c: number | null;
  current: HeatCurveValues;
  suggested: HeatCurveValues | null;
  reasons: string[];
  manual_only: boolean;
  readings: {
    indoor_temp_c: number | null;
    comfort_target_c: number;
    outdoor_temp_c: number | null;
    curve_supply_target_c: number | null;
    controller_heating_enabled: boolean | null;
  };
  verification: {
    status: "not_started" | "pending" | "verified";
    recommendation_available: boolean;
    summary: string;
    reasons: string[];
    elapsed_hours?: number;
    minimum_hours?: number;
    indoor_sample_count?: number;
    minimum_indoor_samples?: number;
    heating_condition_samples?: number;
    minimum_heating_condition_samples?: number;
    comfort_improvement_c?: number | null;
    effect_standard_error_c?: number | null;
    effect_evidence?: "low" | "medium" | "high";
    verification_decision?: "accepted" | "rejected" | "inconclusive";
  };
}

const CURVE_ROWS: Array<{ key: keyof HeatCurveValues; label: string }> = [
  { key: "outdoor_cold_c", label: "Cold outdoor point" },
  { key: "supply_cold_c", label: "Supply at cold point" },
  { key: "outdoor_warm_c", label: "Warm outdoor point" },
  { key: "supply_warm_c", label: "Supply at warm point" },
  { key: "heating_off_outdoor_c", label: "Heating-off outdoor temp" },
  { key: "delta_t_c", label: "ΔT" },
];

const STATUS_LABEL: Record<HeatCurveAdviceResponse["status"], string> = {
  too_warm: "Home is above target",
  too_cold: "Home is below target",
  on_target: "Home is close to target",
  insufficient_data: "Waiting for indoor data",
  verification_pending: "Verifying the latest controller change",
  outside_heating_season: "Comfort is not heat-pump controllable right now",
};

export function HeatCurveAdvice({
  onUseSuggestion,
}: {
  onUseSuggestion: (values: HeatCurveValues) => void;
}) {
  const [advice, setAdvice] = useState<HeatCurveAdviceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const loadAdvice = () => {
      fetch("/api/thermal/heat-curve-advice")
        .then(async (response) => {
          if (!response.ok) throw new Error("Unable to load controller advice");
          return response.json() as Promise<HeatCurveAdviceResponse>;
        })
        .then((data: unknown) => {
          if (
            !data ||
            typeof data !== "object" ||
            !("current" in data) ||
            !("suggested" in data) ||
            !("readings" in data) ||
            !("verification" in data) ||
            !("status" in data)
          ) {
            throw new Error("Invalid controller advice response");
          }
          if (active) {
            setAdvice(data as HeatCurveAdviceResponse);
            setError(null);
          }
        })
        .catch(() => {
          if (active) setError("Controller recommendation is unavailable right now.");
        });
    };
    loadAdvice();
    const refreshTimer = window.setInterval(loadAdvice, 5 * 60 * 1000);
    return () => {
      active = false;
      window.clearInterval(refreshTimer);
    };
  }, []);

  if (error) {
    return <p className="settings-form-hint">{error}</p>;
  }
  if (!advice) {
    return <p className="settings-form-hint">Loading controller comfort recommendation...</p>;
  }

  const { readings } = advice;
  const controllerState = readings.controller_heating_enabled === null
    ? "Controller state unavailable"
    : readings.controller_heating_enabled
      ? "Heating is enabled by the outdoor cutoff"
      : "Heating is currently off at this outdoor temperature";
  const waitingForVerification = !advice.verification.recommendation_available;

  return (
    <section className="plan-section settings-tab-panel" aria-labelledby="heat-curve-advice-title">
      <h2 id="heat-curve-advice-title" className="chart-title">Controller Heat Curve &amp; Comfort Plan</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Each saved Panasonic curve change is measured before another recommendation is shown. This never sends a setting to the heat pump.
      </p>

      <div className="tab-context" style={{ marginBottom: "1rem" }}>
        <strong>{STATUS_LABEL[advice.status]}</strong>
        <span>
          Indoor {readings.indoor_temp_c?.toFixed(1) ?? "—"}°C · target {readings.comfort_target_c.toFixed(1)}°C · outdoor {readings.outdoor_temp_c?.toFixed(1) ?? "—"}°C
        </span>
      </div>

      <div className="settings-form-row" style={{ marginBottom: "0.75rem" }}>
        <span className="settings-form-label">Controller status</span>
        <span className="settings-form-hint">
          {controllerState}
          {readings.curve_supply_target_c !== null ? ` · curve target ${readings.curve_supply_target_c.toFixed(1)}°C` : ""}
        </span>
      </div>

      {waitingForVerification ? (
        <>
          <div className="tab-context" style={{ marginBottom: "1rem" }}>
            <strong>Verification in progress</strong>
            <span>{advice.verification.summary}</span>
          </div>
          <div className="settings-form-row" style={{ marginBottom: "0.5rem" }}>
            <span className="settings-form-label">Observation time</span>
            <span className="settings-form-hint">
              {advice.verification.elapsed_hours?.toFixed(1) ?? "0.0"} / {advice.verification.minimum_hours ?? "—"} hours
            </span>
          </div>
          <div className="settings-form-row" style={{ marginBottom: "0.5rem" }}>
            <span className="settings-form-label">Indoor measurements</span>
            <span className="settings-form-hint">
              {advice.verification.indoor_sample_count ?? 0} / {advice.verification.minimum_indoor_samples ?? "—"}
            </span>
          </div>
          <div className="settings-form-row" style={{ marginBottom: "1rem" }}>
            <span className="settings-form-label">Cool-weather readings</span>
            <span className="settings-form-hint">
              {advice.verification.heating_condition_samples ?? 0} / {advice.verification.minimum_heating_condition_samples ?? "—"} below the heating-off threshold
            </span>
          </div>
          <ul className="plan-list" style={{ marginBottom: "1rem" }}>
            {advice.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
          <p className="settings-form-hint">
            New curve recommendations remain locked until this window has enough time, indoor readings, and weather where the controller can actually heat.
          </p>
        </>
      ) : (
        <>
          {!advice.suggested && (
            <div className="forecast-data-warning" role="status">
              <strong>No new recommendation.</strong>
              <span>
                {advice.status === "outside_heating_season"
                  ? " Waiting for heating conditions where a controller change can be measured."
                  : " The current evidence does not justify another controller change."}
              </span>
            </div>
          )}
          {advice.verification.status === "verified" && (
            <p className="settings-form-hint" style={{ marginBottom: "1rem" }}>
              {advice.verification.summary}
              {advice.verification.comfort_improvement_c !== null && advice.verification.comfort_improvement_c !== undefined
                ? ` Comfort-distance change: ${advice.verification.comfort_improvement_c > 0 ? "+" : ""}${advice.verification.comfort_improvement_c.toFixed(2)}°C.`
                : ""}
              {advice.verification.verification_decision
                ? ` Decision: ${advice.verification.verification_decision} (${advice.verification.effect_evidence || "low"} evidence).`
                : ""}
            </p>
          )}
          <div style={{ overflowX: "auto", marginBottom: "1rem" }}>
            <table className="data-table" aria-label="Current and recommended heat curve">
              <thead>
                <tr>
                  <th>Controller setting</th>
                  <th>Recorded</th>
                  <th>Recommended</th>
                </tr>
              </thead>
              <tbody>
                {CURVE_ROWS.map(({ key, label }) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td>{advice.current[key].toFixed(1)}°C</td>
                    <td>{advice.suggested ? `${advice.suggested[key].toFixed(1)}°C` : "No change proposed"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="plan-list" style={{ marginBottom: "1rem" }}>
            {advice.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
          {advice.suggested && (
            <button className="btn btn-primary" type="button" onClick={() => onUseSuggestion(advice.suggested!)}>
              Use recommendation as draft
            </button>
          )}
          <p className="settings-form-hint" style={{ marginTop: "0.75rem" }}>
            First change the values on the Panasonic controller. Then verify them below and select Save Settings so forecasts and new plans use the same curve.
          </p>
        </>
      )}
    </section>
  );
}
